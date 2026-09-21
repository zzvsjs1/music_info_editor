"""Confirmation and background orchestration over immutable reviewed Apply requests."""

from PySide6.QtCore import QObject, Qt, Slot
from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QTableView, QVBoxLayout

from metadata_polisher.application.apply import (
    ApplyBatchRequest,
    ApplyBatchResult,
    ApplyBatchStatus,
    ApplyFileOutcome,
    ApplyFileOutcomeStatus,
    ApplyService,
)
from metadata_polisher.application.apply_summary import build_apply_summary
from metadata_polisher.domain.errors import MediaErrorCode
from metadata_polisher.execution.cancellation import CancellationToken
from metadata_polisher.execution.events import FileSkipReason, OperationEventSink
from metadata_polisher.infrastructure.logging_setup import redact_sensitive_text
from metadata_polisher.infrastructure.reporting import ReportWriteStatus
from metadata_polisher.session.apply_preparation import prepare_apply_request, reviewed_file_ids
from metadata_polisher.session.state import OperationKind, mark_groups_requires_rescan
from metadata_polisher.ui.dialogs.apply_summary_dialog import ApplySummaryDialog
from metadata_polisher.ui.dialogs.rename_files_dialog import RenameFilesDialog
from metadata_polisher.ui.dialogs.rename_preview_dialog import RenamePreviewDialog
from metadata_polisher.ui.layout import configure_columns, fit_initial_size
from metadata_polisher.ui.main_window import MainWindow


class ApplyController(QObject):
    """Submit only the exact reviewed snapshot accepted in the summary dialog."""

    def __init__(self, window: MainWindow, service: ApplyService) -> None:
        super().__init__(window)
        self._window = window
        self._service = service
        self.last_result: ApplyBatchResult | None = None
        self._operation_ids: set[str] = set()
        self._failed_reconciliations: set[str] = set()
        self._results_dialog: QDialog | None = None
        self._rename_dialog: RenamePreviewDialog | None = None
        self._last_request: ApplyBatchRequest | None = None
        self._terminal_label: str | None = None
        self._terminal_message = ""
        window.apply_selected_button.clicked.connect(self.apply_selected)
        window.apply_all_button.clicked.connect(self.apply_all)
        window.apply_results_button.clicked.connect(self.show_results)
        window.rename_previews_button.clicked.connect(self.show_rename_previews)
        window.rename_files_button.clicked.connect(self.rename_files)
        window.review_context_changed.connect(self.refresh)
        window.file_table_view.selectionModel().selectionChanged.connect(self.refresh)
        window.group_view.selectionModel().selectionChanged.connect(self.refresh)
        assert window.operation_controller is not None
        window.cancel_button.clicked.connect(window.operation_controller.cancel_active)
        window.operation_controller.controller_failed.connect(self._record_controller_failure)
        assert window.operation_bridge is not None
        window.operation_bridge.completed.connect(self._completed)
        window.operation_bridge.cancelled.connect(self._cancelled)
        window.operation_bridge.failed.connect(self._failed)
        self.refresh()

    def _included_ids(self) -> tuple[str, ...]:
        """Use stable batch membership in source order, independently of highlighting."""
        return tuple(
            source.file_id for group in self._window.session_state.groups for source in group.group.files
            if source.file_id in self._window.included_file_ids
        )

    @Slot()
    def refresh(self) -> None:
        state = self._window.session_state
        available = set(reviewed_file_ids(state))
        selected = self._included_ids()
        idle = state.active_operation is None
        self._window.apply_selected_button.setEnabled(idle and bool(selected) and set(selected).issubset(available))
        self._window.review_apply_button.setEnabled(self._window.apply_selected_button.isEnabled())
        self._window.apply_all_button.setEnabled(idle and bool(selected) and set(selected).issubset(available))
        self._window.file_model.set_inclusion_enabled(idle)
        self._window.rename_previews_button.setEnabled(idle and bool(self._window.review_target_file_ids()))
        self._window.cancel_button.setEnabled(not idle)
        self._window.apply_results_button.setEnabled(self._terminal_label is not None)
        files_selected = self._window.selected_file_ids()
        supported_ids = {source.file_id for group in state.groups for source in group.group.files}
        rename = self._window.library_controller.settings.rename if self._window.library_controller else None
        self._window.rename_files_button.setEnabled(
            idle and bool(files_selected) and set(files_selected).issubset(supported_ids)
            and bool(rename and rename.enabled),
        )
        self._window.rename_files_button.setToolTip(
            "Preview and prepare filenames for the highlighted files" if rename and rename.enabled
            else "Enable filename renaming in Settings first",
        )

        if not idle:
            guidance = "Wait for the current operation to finish."
        elif not selected:
            guidance = "Include files in the write batch to review and apply changes."
        elif not set(selected).issubset(available):
            guidance = "Some included files need a rescan or metadata review before writing."
        else:
            guidance = f"Review changes for {len(selected)} included files (Ctrl+Enter)."

        self._window.apply_guidance_label.setText(guidance)
        self._window.review_apply_guidance_label.setText(guidance)
        self._window.apply_selected_button.setToolTip(guidance)
        self._window.review_apply_button.setToolTip(guidance)

    @Slot()
    def rename_files(self) -> None:
        window = self._window
        library = window.library_controller
        ids = window.selected_file_ids()
        snapshot = window.session_state
        supported_ids = {source.file_id for group in snapshot.groups for source in group.group.files}

        if (not ids or not set(ids).issubset(supported_ids) or snapshot.active_operation is not None
                or library is None or not library.settings.rename.enabled):
            return

        settings = library.settings
        inclusion = window.included_file_ids
        dialog = RenameFilesDialog(snapshot, ids, settings.rename, window)

        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        # The displayed filenames belong to this precise review and template.
        # Modal event delivery must not install a draft over a newer session.
        if (window.session_state is not snapshot or library.settings != settings
                or window.included_file_ids != inclusion):
            window.workflow_message_label.setText(
                "The library changed. Open Rename files again to refresh its previews.",
            )
            return

        window.set_session_state(dialog.draft_state())
        window.set_included_file_ids(inclusion | frozenset(dialog.chosen_file_ids()))
        self.apply_selected()

    @Slot()
    def apply_selected(self) -> None:
        self._confirm_and_start(self._included_ids())

    @Slot()
    def apply_all(self) -> None:
        self._confirm_and_start(self._included_ids())

    def _confirm_and_start(self, file_ids: tuple[str, ...]) -> None:
        window = self._window
        state = window.session_state

        if state.active_operation is not None or window.library_controller is None:
            return

        settings = window.library_controller.settings
        included_snapshot = window.included_file_ids
        operation_id = window.operation_ids.next_id("APPLY")

        try:
            # Freeze the validated request before opening confirmation. The
            # worker closure below receives this exact object, not fresh widget
            # selections or a newly derived batch after the user accepts.
            request = prepare_apply_request(state, file_ids, settings, operation_id)
        except ValueError as error:
            window.workflow_message_label.setText(str(error))
            return

        selected_ids = set(file_ids)
        changes = tuple(review.change_set for group in state.groups for review in group.reviewed_files
                        if review.file_id in selected_ids and review.change_set is not None)
        summary = build_apply_summary(changes, backup_enabled=settings.backup.enabled,
                                      report_enabled=settings.reports.enabled)
        sources = {source.file_id: source for group in state.groups for source in group.group.files}
        dialog = ApplySummaryDialog(
            summary, window.review_window if window.review_window.isVisible() else window,
            files=tuple((sources[change.file_id], change) for change in changes),
        )

        if dialog.exec() != QDialog.DialogCode.Accepted or not summary.can_apply:
            return

        # Qt can deliver events while the modal summary is open. Any change to
        # the review, settings or write membership requires a fresh summary.
        if (
            window.session_state is not state or window.library_controller.settings != settings
            or window.included_file_ids != included_snapshot
        ):
            window.workflow_message_label.setText(
                "The review or included files changed while confirmation was open. Review & apply again."
            )
            return

        service = self._service

        def work(token: CancellationToken, events: OperationEventSink) -> ApplyBatchResult:
            return service.apply(request, cancellation=token, events=events)

        self._operation_ids.add(operation_id)
        self._last_request = request
        self.last_result = None
        self._terminal_label = None
        self._discard_results_dialog()
        window.workflow_message_label.clear()
        assert window.operation_controller is not None
        try:
            window.operation_controller.start(operation_id, OperationKind.APPLY,
                                              tuple(group.group_id for group in request.groups), work)
        except Exception as error:
            self._failed(operation_id, error)

        self.refresh()

    @Slot()
    def show_rename_previews(self) -> None:
        window = self._window

        if window.session_state.active_operation is not None:
            return

        selected = set(window.review_target_file_ids())

        if not selected:
            return

        reviewed = {item.file_id: item for group in window.session_state.groups for item in group.reviewed_files}
        files = tuple(
            (source, reviewed.get(source.file_id))
            for group in window.session_state.groups for source in group.group.files
            if source.file_id in selected
        )

        if self._rename_dialog is not None:
            self._rename_dialog.close()

        self._rename_dialog = RenamePreviewDialog(files, window.review_window)
        self._rename_dialog.show()

    @Slot(str, object)
    def _record_controller_failure(self, operation_id: str, _error: object) -> None:
        if operation_id in self._operation_ids:
            # The bridge still delivers the actual typed payload to this slot
            # after its state reducer fails. Retain it rather than replacing
            # successful writes with the unknown-worker-failure presentation.
            self._failed_reconciliations.add(operation_id)

    @Slot(str, object)
    def _completed(self, operation_id: str, result: object) -> None:
        if operation_id not in self._operation_ids or not isinstance(result, ApplyBatchResult):
            return

        self._operation_ids.remove(operation_id)
        reconciliation_failed = operation_id in self._failed_reconciliations
        self._failed_reconciliations.discard(operation_id)
        self.last_result = result
        files = tuple(item for group in result.groups for item in group.files)

        if reconciliation_failed:
            changed_pairs = {(item.file_id, item.source_path) for item in files if item.filesystem_changed}
            affected = tuple(
                group.group.group_id for group in self._window.session_state.groups
                if any((source.file_id, source.path) in changed_pairs for source in group.group.files)
            )

            if affected:
                self._window.set_session_state(mark_groups_requires_rescan(self._window.session_state, affected))

        else:
            self._consume_completed_inclusion(result)

        completed = sum(item.status is ApplyFileOutcomeStatus.APPLIED for item in files)
        failed = sum(item.status is ApplyFileOutcomeStatus.FAILED for item in files)
        # Not-started files are a subset of skipped outcomes. Subtract them from
        # skipped so each file contributes to exactly one displayed outcome count.
        not_started = sum(item.skip_reason is FileSkipReason.CANCELLED_BEFORE_START for item in files)
        skipped = sum(item.status is ApplyFileOutcomeStatus.SKIPPED for item in files) - not_started
        cancelled = sum(item.status is ApplyFileOutcomeStatus.CANCELLED for item in files)
        unchanged = sum(item.status is ApplyFileOutcomeStatus.NO_CHANGES for item in files)
        warnings = (
            reconciliation_failed or result.report_result.status is ReportWriteStatus.FAILED
            or any(item.refresh_issues for item in files)
        )
        status = {
            ApplyBatchStatus.SUCCEEDED: "Completed with warnings" if warnings else "Completed",
            ApplyBatchStatus.PARTIAL: "Partially completed",
            ApplyBatchStatus.CANCELLED: "Cancelled",
            ApplyBatchStatus.FAILED: "Failed",
        }[result.status]
        message = (
            f"{status}: {completed} completed, {failed} failed, {skipped} skipped, "
            f"{not_started} not started, {cancelled} cancelled at a safe boundary, {unchanged} unchanged."
        )

        if any(item.requires_rescan for item in files):
            message += " Rescan affected groups before further edits."

        if reconciliation_failed:
            message += (
                " The results could not be reconciled with the session. Actual file results were retained; "
                "rescan affected groups before further edits."
            )

        if result.report_result.status is ReportWriteStatus.WRITTEN:
            message += f" Report: {result.report_result.path}"
        elif result.report_result.status is ReportWriteStatus.FAILED:
            message += " Audio results were retained, but the report could not be written."

        self._window.operation_stage_label.setText(status)
        self._window.workflow_message_label.setText(message)
        self._terminal_label = status
        self._terminal_message = message
        self.refresh()
        self.show_results()

    def _consume_completed_inclusion(self, result: ApplyBatchResult) -> None:
        """Remove completed work from the next batch without losing pending edits."""
        state = self._window.session_state
        request = self._last_request

        if (request is None or result.operation_id != request.operation_id
                or state.library_revision != request.base_library_revision):
            return

        # Pair group and file identities with the accepted source snapshot.
        # Completion must not consume inclusion belonging to a newer review.
        confirmed = {
            (group.group_id, item.source.file_id): item
            for group in request.groups for item in group.files
        }
        outcomes = {(group.group_id, item.file_id): item for group in result.groups for item in group.files}
        receipts = {receipt.source.file_id: receipt.source for receipt in state.written_files}
        completed: set[str] = set()

        for group in state.groups:
            if group.requires_rescan:
                continue

            reviews = {review.file_id: review for review in group.reviewed_files}

            for source in group.group.files:
                key = (group.group.group_id, source.file_id)
                original = confirmed.get(key)
                outcome = outcomes.get(key)

                if original is None or outcome is None or outcome.source_path != original.source.path:
                    continue

                review = reviews.get(source.file_id)

                # The reducer removes a written review only after accepting its
                # fresh disk snapshot. Retain stale results and any later edit,
                # including when a partial or cancelled batch wrote other files.
                verified_write = (
                    outcome.status is ApplyFileOutcomeStatus.APPLIED and review is None
                    and source == outcome.refreshed_source and receipts.get(source.file_id) == source
                )

                # A no-op has no disk refresh receipt. Its confirmed source and
                # decisions must still match, and it must still have no changes.
                unchanged_review = (
                    outcome.status is ApplyFileOutcomeStatus.NO_CHANGES and source == original.source
                    and review is not None and review.reviews == original.reviews
                    and review.change_set is not None and not review.change_set.metadata_changes
                    and review.change_set.rename_change is None
                )

                if verified_write or unchanged_review:
                    completed.add(source.file_id)

        if completed:
            self._window.set_included_file_ids(self._window.included_file_ids - completed)

    @Slot(str)
    def _cancelled(self, operation_id: str) -> None:
        if operation_id not in self._operation_ids:
            return

        # ApplyService returns typed outcomes after it starts processing files.
        # The executor's early cancellation path has no such batch result.
        self._operation_ids.remove(operation_id)
        self._failed_reconciliations.discard(operation_id)
        self.last_result = None
        self._terminal_label = "Cancelled"
        self._terminal_message = (
            "Apply cancelled before any files were processed. All confirmed files were not started."
        )
        self._window.operation_stage_label.setText("Cancelled")
        self._window.workflow_message_label.setText("Apply cancelled before any files were processed.")
        self.refresh()
        self.show_results()

    @Slot(str, object)
    def _failed(self, operation_id: str, error: object) -> None:
        if operation_id not in self._operation_ids:
            return

        self._operation_ids.remove(operation_id)
        self._failed_reconciliations.discard(operation_id)
        self.last_result = None
        self._terminal_label = "Failed"
        self._terminal_message = (
            "Apply failed before returning file results. File outcomes are unknown; "
            "rescan the affected groups before further edits."
        )
        # A worker exception can occur after an irreversible file effect. No
        # synthetic success/failure row may imply that the old snapshot is fresh.
        if self._last_request is not None:
            known = {group.group.group_id for group in self._window.session_state.groups}
            affected = tuple(group.group_id for group in self._last_request.groups if group.group_id in known)

            if affected:
                self._window.set_session_state(mark_groups_requires_rescan(self._window.session_state, affected))

        self._window.operation_stage_label.setText("Failed")
        self._window.workflow_message_label.setText(self._terminal_message)
        self.refresh()
        self.show_results()

    def _discard_results_dialog(self) -> None:
        if self._results_dialog is not None:
            self._results_dialog.close()
            self._results_dialog.setParent(None)
            self._results_dialog.deleteLater()
            self._results_dialog = None

    @Slot()
    def show_results(self) -> None:
        result = self.last_result

        if self._terminal_label is None:
            return

        if self._results_dialog is not None:
            self._results_dialog.show()
            self._results_dialog.raise_()
            self._results_dialog.activateWindow()
            return

        dialog = QDialog(self._window)
        dialog.setWindowTitle("Apply results")
        layout = QVBoxLayout(dialog)
        summary = QLabel(self._terminal_message, dialog)
        summary.setTextFormat(Qt.TextFormat.PlainText)
        summary.setWordWrap(True)
        layout.addWidget(summary)
        table = QTableView(dialog)
        table.setObjectName("applyResultsTable")
        model = QStandardItemModel(0, 4, table)
        model.setHorizontalHeaderLabels(("File", "Result", "Final path", "Details"))

        rows: list[tuple[str, str, str, str]] = []

        if result is not None:
            rows = [self._result_row(outcome) for group in result.groups for outcome in group.files]
        elif self._last_request is not None:
            status = "not_started" if self._terminal_label == "Cancelled" else "unknown"
            rows = [
                (file.source.path.name, status, str(file.source.path), self._terminal_message)
                for group in self._last_request.groups for file in group.files
            ]

        for row in rows:
            cells = [QStandardItem(redact_sensitive_text(value)) for value in row]

            for cell in cells:
                cell.setEditable(False)

            model.appendRow(cells)

        table.setModel(model)
        configure_columns(table, (230, 140, 300, 400))
        layout.addWidget(table)
        details = QLabel(self._result_details(), dialog)
        details.setTextFormat(Qt.TextFormat.PlainText)
        details.setWordWrap(True)
        layout.addWidget(details)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, dialog)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        self._results_dialog = dialog
        fit_initial_size(dialog, 1080, 560)
        dialog.show()

    @staticmethod
    def _result_row(outcome: ApplyFileOutcome) -> tuple[str, str, str, str]:
        reasons = [issue.message for issue in outcome.change_set.validation.issues]
        reasons.extend(issue.message for issue in outcome.refresh_issues)

        if outcome.transaction_result is not None:
            reasons.extend(issue.message for issue in outcome.transaction_result.issues)

            if (outcome.final_path != outcome.source_path and any(
                issue.code is MediaErrorCode.CLEANUP_FAILED for issue in outcome.transaction_result.issues
            )):
                reasons.append("Both the original and renamed files were retained.")

        if outcome.skip_reason is not None:
            reasons.append(outcome.skip_reason.value.replace("_", " "))

        return outcome.source_path.name, outcome.status.value, str(outcome.final_path), "; ".join(reasons)

    def _result_details(self) -> str:
        request = self._last_request
        result = self.last_result

        if result is None:
            return "Tag updates / renames / backups / report: not run." if self._terminal_label == "Cancelled" else (
                "Tag updates / renames / backups / report: outcome unavailable. Inspect the affected files."
            )

        files = tuple(outcome for group in result.groups for outcome in group.files)
        written = tuple(outcome for outcome in files if outcome.status is ApplyFileOutcomeStatus.APPLIED)
        updates = sum(bool(outcome.change_set.metadata_changes) for outcome in written)
        renames = sum(outcome.change_set.rename_change is not None for outcome in written)
        backups = "disabled"

        if request is not None and request.backup.enabled:
            backed_up = sum(
                outcome.transaction_result is not None and outcome.transaction_result.completed_backup_path is not None
                for outcome in files
            )
            backups = f"{backed_up} completed before file processing"

        report = result.report_result
        report_text = {
            ReportWriteStatus.DISABLED: "disabled",
            ReportWriteStatus.WRITTEN: f"written to {report.path}",
            ReportWriteStatus.FAILED: f"failed ({report.error_code}); completed media writes remain completed",
        }[report.status]
        return (
            f"Verified completed tag updates: {updates}; renames: {renames}. "
            f"Backups: {backups}. Report: {report_text}."
        )
