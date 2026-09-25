"""QtQuick confirmation and results over the shared transactional Apply service."""

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Property, QObject, Signal, Slot

from metadata_polisher.application.apply import (
    ApplyBatchRequest,
    ApplyBatchResult,
    ApplyBatchStatus,
    ApplyFileOutcome,
    ApplyFileOutcomeStatus,
    ApplyService,
    LocalApplyPreflightInspector,
)
from metadata_polisher.application.apply_summary import ApplySummary, build_apply_summary
from metadata_polisher.application.changes import ChangeIssueSeverity, RenameDecision
from metadata_polisher.domain.errors import MediaErrorCode
from metadata_polisher.execution.cancellation import CancellationToken
from metadata_polisher.execution.events import FileSkipReason, OperationEventSink
from metadata_polisher.infrastructure.logging_setup import redact_sensitive_text
from metadata_polisher.infrastructure.reporting import ProcessingReportWriter, ReportWriteStatus
from metadata_polisher.infrastructure.settings import AppSettings
from metadata_polisher.infrastructure.transaction import TransactionalFileWriter
from metadata_polisher.session.apply_preparation import prepare_apply_request, reviewed_file_ids
from metadata_polisher.session.review_editing import apply_rename_choices
from metadata_polisher.session.state import (
    OperationKind,
    SessionState,
    StateApplicationResult,
    apply_batch_result,
    mark_groups_requires_rescan,
)
from metadata_polisher.ui.models.diff_model import FIELD_LABELS

if TYPE_CHECKING:
    from metadata_polisher.ui.quick.backend import QuickBackend


@dataclass(frozen=True)
class _Confirmation:
    """The displayed summary authorises exactly these objects and memberships."""

    state: SessionState
    settings: AppSettings
    included: frozenset[str]
    request: ApplyBatchRequest
    summary: ApplySummary


class QuickApply(QObject):
    """Stage choices in memory; submit only the immutable accepted write batch."""

    changed = Signal()

    def __init__(self, host: QuickBackend, apply_service: ApplyService | None = None) -> None:
        super().__init__(host)
        self._host = host
        self._service = (
            apply_service
            if apply_service is not None
            else ApplyService(
                preflight=LocalApplyPreflightInspector(),
                writer=TransactionalFileWriter(),
                report_writer=ProcessingReportWriter((host.app_dir or Path.cwd()) / "reports"),
            )
        )

        # A confirmation captures the precise session, settings and inclusion
        # membership shown to the user. Presentation rows cannot authorise work.
        self._confirmation: _Confirmation | None = None
        self._summary_rows: list[dict[str, str]] = []
        self._summary_error = ""

        # Keep operation receipts after closing their window so results can be
        # reopened without repeating any write or reconciliation.
        self._requests: dict[str, ApplyBatchRequest] = {}
        self._failed_reconciliations: set[str] = set()
        self._last_request: ApplyBatchRequest | None = None
        self.last_result: ApplyBatchResult | None = None
        self._terminal_label = ""
        self._terminal_message = ""
        self._results_visible = False
        self._result_rows: list[dict[str, str]] = []

        # Renaming has a private draft. Neither row focus nor a draft checkbox
        # changes the live session or its separate write-batch inclusion.
        self._rename_original: SessionState | None = None
        self._rename_draft: SessionState | None = None
        self._rename_settings: AppSettings | None = None
        self._rename_inclusion: frozenset[str] = frozenset()
        self._rename_ids: tuple[str, ...] = ()
        self._rename_chosen: frozenset[str] = frozenset()
        self._rename_rows: list[dict[str, object]] = []
        self._rename_summary = ""
        self._rename_can_accept = False
        self._rename_error = ""
        self._previews_visible = False
        self._preview_rows: list[dict[str, str]] = []

        host.changed.connect(self.changed)
        host.bridge.completed.connect(self._completed)
        host.bridge.cancelled.connect(self._cancelled)
        host.bridge.failed.connect(self._failed)
        host.controller_failed.connect(self._record_controller_failure)

    def _idle(self) -> bool:
        return self._host.session_state.active_operation is None

    def _included_ids(self) -> tuple[str, ...]:
        # Preserve the library's deterministic order rather than iterating the
        # inclusion set, whose ordering has no user-facing meaning.
        return tuple(
            source.file_id
            for group in self._host.session_state.groups
            for source in group.group.files
            if source.file_id in self._host._included
        )

    def _can_apply(self) -> bool:
        selected = self._included_ids()
        return self._idle() and bool(selected) and set(selected).issubset(reviewed_file_ids(self._host.session_state))

    def _can_rename(self) -> bool:
        supported = {source.file_id for group in self._host.session_state.groups for source in group.group.files}
        return (
            self._idle()
            and bool(self._host._selected)
            and set(self._host._selected).issubset(supported)
            and self._host.app_settings.rename.enabled
        )

    def _guidance(self) -> str:
        if not self._idle():
            return "Wait for the current operation to finish."

        if not self._included_ids():
            return "Include files in the write batch to review and apply changes."

        if not self._can_apply():
            return "Some included files need a rescan or metadata review before writing."

        return f"Review changes for {len(self._included_ids())} included files (Ctrl+Enter)."

    def _summary_text(self) -> str:
        confirmation = self._confirmation

        if confirmation is None:
            return ""

        summary = confirmation.summary
        lines = [f"{summary.file_count} files selected", f"{summary.write_file_count} files with changes", ""]

        for label, count, fields in (
            ("values added", summary.addition_count, summary.additions),
            ("existing values replaced", summary.replacement_count, summary.replacements),
            ("values cleared", summary.removal_count, summary.removals),
        ):
            lines.append(f"{count} {label}")
            lines.extend(f"    {FIELD_LABELS[item.field]}: {item.count}" for item in fields)

        lines.extend(
            (
                f"{summary.rename_count} filenames to rename",
                f"{len(summary.blocking_issues)} blocking conflicts",
                "",
                f"Permanent backup: {'On' if summary.backup_enabled else 'Off'}",
                f"JSON report: {'On' if summary.report_enabled else 'Off'}",
            )
        )
        lines.extend(
            f"{issue.file_id} — {issue.code.value}"
            f"{f' ({FIELD_LABELS[issue.field]})' if issue.field is not None else ''}: {issue.message}"
            for issue in summary.blocking_issues
        )

        if not summary.can_apply and not summary.blocking_issues:
            lines.append("No changes are selected for Apply.")

        return redact_sensitive_text("\n".join(lines))

    canApply = Property(bool, _can_apply, notify=changed)
    canRename = Property(bool, _can_rename, notify=changed)
    hasResults = Property(bool, lambda self: bool(self._terminal_label), notify=changed)
    guidance = Property(str, _guidance, notify=changed)
    summaryVisible = Property(bool, lambda self: self._confirmation is not None, notify=changed)
    summaryText = Property(str, _summary_text, notify=changed)
    summaryRows = Property(list, lambda self: self._summary_rows, notify=changed)
    summaryCanApply = Property(
        bool,
        lambda self: self._confirmation is not None and self._confirmation.summary.can_apply and self._idle(),
        notify=changed,
    )
    summaryError = Property(str, lambda self: self._summary_error, notify=changed)
    resultsVisible = Property(bool, lambda self: self._results_visible, notify=changed)
    resultsText = Property(str, lambda self: self._terminal_message, notify=changed)
    resultsRows = Property(list, lambda self: self._result_rows, notify=changed)
    renameVisible = Property(bool, lambda self: self._rename_original is not None, notify=changed)
    renameRows = Property(list, lambda self: self._rename_rows, notify=changed)
    renameSummary = Property(str, lambda self: self._rename_summary, notify=changed)
    renameCanAccept = Property(bool, lambda self: self._rename_can_accept and self._idle(), notify=changed)
    renameError = Property(str, lambda self: self._rename_error, notify=changed)
    previewsVisible = Property(bool, lambda self: self._previews_visible, notify=changed)
    previewRows = Property(list, lambda self: self._preview_rows, notify=changed)

    @Slot(result=bool)
    def beginApply(self) -> bool:
        if not self._idle():
            return False

        host = self._host
        state, settings = host.session_state, host.app_settings
        selected = self._included_ids()

        try:
            request = prepare_apply_request(state, selected, settings, host._ids.next_id("APPLY"))
        except ValueError as error:
            host.set_status(str(error))
            return False

        changes = tuple(
            item.change_set
            for group in state.groups
            for item in group.reviewed_files
            if item.file_id in selected and item.change_set is not None
        )
        summary = build_apply_summary(
            changes,
            backup_enabled=settings.backup.enabled,
            report_enabled=settings.reports.enabled,
        )
        sources = {source.file_id: source for group in state.groups for source in group.group.files}
        rows = []

        for change in changes:
            source = sources[change.file_id]
            rename = change.rename_decision is RenameDecision.APPLY_RENAME and change.rename_preview is not None
            final_name = change.rename_preview.new_path.name if rename and change.rename_preview else source.path.name

            # Keep the compact count in the table, but expose the actual fields
            # in its tooltip and copyable details before authorising a write.
            fields = ", ".join(FIELD_LABELS[item.field] for item in change.metadata_changes)
            rows.append(
                {
                    "id": source.file_id,
                    "file": source.path.name,
                    "fields": str(len(change.metadata_changes)),
                    "fieldNames": fields or "No tag changes",
                    "decision": "Include rename" if rename else "Keep filename",
                    "final": final_name,
                }
            )

        self._confirmation = _Confirmation(state, settings, host._included, request, summary)
        self._summary_rows, self._summary_error = rows, ""
        self.changed.emit()
        return True

    @Slot()
    def cancelApply(self) -> None:
        self._confirmation = None
        self._summary_error = ""
        self.changed.emit()

    @Slot(result=bool)
    def confirmApply(self) -> bool:
        captured = self._confirmation
        host = self._host

        if captured is None or not captured.summary.can_apply or not self._idle():
            return False

        if (
            host.session_state is not captured.state
            or host.app_settings != captured.settings
            or host._included != captured.included
        ):
            self._summary_error = (
                "The review, settings or included files changed while confirmation was open. Review & apply again."
            )
            host.set_status(self._summary_error)
            self.changed.emit()
            return False

        request, service = captured.request, self._service

        def work(token: CancellationToken, events: OperationEventSink) -> ApplyBatchResult:
            # Only this confirmed immutable object crosses the write boundary.
            return service.apply(request, cancellation=token, events=events)

        def reduce(state: SessionState, result: object) -> StateApplicationResult:
            if not isinstance(result, ApplyBatchResult):
                raise TypeError("Apply returned an unexpected result.")

            return apply_batch_result(state, result)

        self._requests[request.operation_id] = request
        self._last_request = request
        self.last_result = None
        self._terminal_label = ""
        self._results_visible = False
        self._confirmation = None
        self._result_rows = []
        host.set_status("")
        self.changed.emit()
        submitted = host.submit_operation(
            request.operation_id,
            OperationKind.APPLY,
            tuple(group.group_id for group in request.groups),
            work,
            reduce,
        )

        if not submitted:
            self._failed(request.operation_id, None)

        return submitted

    @Slot(result=bool)
    def beginRename(self) -> bool:
        if not self._can_rename():
            return False

        host = self._host
        self._rename_original, self._rename_draft = host.session_state, host.session_state
        self._rename_settings = host.app_settings
        self._rename_inclusion = host._included
        self._rename_ids = host._selected
        self._rename_chosen = frozenset(self._rename_ids)
        self._rename_error = ""
        self._refresh_rename()
        return True

    @Slot(str, bool)
    def setRenameIncluded(self, file_id: str, included: bool) -> None:
        if self._rename_original is None or file_id not in self._rename_ids or not self._idle():
            return

        self._rename_chosen = self._rename_chosen | {file_id} if included else self._rename_chosen - {file_id}
        self._refresh_rename()

    def _refresh_rename(self) -> None:
        original, settings = self._rename_original, self._rename_settings

        if original is None or settings is None:
            return

        selected = tuple(file_id for file_id in self._rename_ids if file_id in self._rename_chosen)
        previous = {
            item.file_id
            for group in original.groups
            for item in group.reviewed_files
            if item.change_set and item.change_set.rename_decision is RenameDecision.APPLY_RENAME
        }
        keep = tuple(
            file_id for file_id in self._rename_ids if file_id not in self._rename_chosen and file_id in previous
        )
        draft = original
        blocked: dict[str, str] = {}

        try:
            if selected or keep:
                # Rebuild from the original on every toggle. Intermediate choices
                # must not accumulate undo entries or keep an unticked old rename.
                result = apply_rename_choices(original, selected, keep, settings.rename)
                draft = result.state
                blocked = {item.file_id: item.reason for item in result.blocked}
        except (TypeError, ValueError) as error:
            self._rename_error = str(error)
            self._rename_can_accept = False
            self.changed.emit()
            return

        self._rename_draft = draft
        sources = {source.file_id: source for group in original.groups for source in group.group.files}
        changes = {item.file_id: item.change_set for group in draft.groups for item in group.reviewed_files}
        rows: list[dict[str, object]] = []

        for file_id in self._rename_ids:
            change = changes.get(file_id)
            preview = change.rename_preview if change else None
            issues = change.validation.issues if change and file_id in self._rename_chosen else ()
            errors = [issue.message for issue in issues if issue.severity is ChangeIssueSeverity.BLOCKING]

            if errors:
                blocked[file_id] = "; ".join(errors)

            status = blocked.get(file_id) or ("; ".join(issue.message for issue in issues) if issues else "Ready")

            if file_id not in self._rename_chosen:
                status = blocked.get(file_id) or "Keep filename"

            rows.append(
                {
                    "id": file_id,
                    "included": file_id in self._rename_chosen,
                    "current": sources[file_id].path.name,
                    "preview": preview.new_path.name if preview else "—",
                    "validation": status,
                }
            )

        self._rename_rows = rows
        self._rename_summary = (
            f"Filename template: {settings.rename.template}\n"
            f"{len(selected)} files chosen · {len(keep)} previous renames removed · {len(blocked)} blocked. "
            + ("Correct or untick blocked files to continue." if blocked else "No files have been changed.")
        )
        self._rename_can_accept = bool(selected or keep) and not blocked
        self._rename_error = ""
        self.changed.emit()

    @Slot(result=bool)
    def acceptRename(self) -> bool:
        host = self._host

        if (
            self._rename_original is None
            or self._rename_draft is None
            or not self._rename_can_accept
            or not self._idle()
        ):
            return False

        if (
            host.session_state is not self._rename_original
            or host.app_settings != self._rename_settings
            or host._included != self._rename_inclusion
            or host._selected != self._rename_ids
        ):
            self._rename_error = "The library, settings or selection changed. Open Rename files again."
            self.changed.emit()
            return False

        # Accepting moves only the reviewed draft into the session. The next
        # confirmation still has to approve the exact batch before submission.
        draft = self._rename_draft
        inclusion = self._rename_inclusion | self._rename_chosen
        self._rename_original = None
        host.set_state(draft)
        host.set_included_file_ids(inclusion)
        self.changed.emit()
        return self.beginApply()

    @Slot()
    def cancelRename(self) -> None:
        self._rename_original, self._rename_draft = None, None
        self._rename_can_accept = False
        self.changed.emit()

    @Slot()
    def showPreviews(self) -> None:
        if not self._idle():
            return

        selected = set(self._host._scope_ids())

        if not selected:
            return

        state = self._host.session_state
        reviews = {item.file_id: item for group in state.groups for item in group.reviewed_files}
        rows: list[dict[str, str]] = []

        for group in state.groups:
            for source in group.group.files:
                if source.file_id not in selected:
                    continue

                reviewed = reviews.get(source.file_id)
                changes = reviewed.change_set if reviewed is not None else None
                preview = changes.rename_preview if changes else None
                rename = changes is not None and changes.rename_decision is RenameDecision.APPLY_RENAME
                issues = "; ".join(issue.message for issue in changes.validation.issues) if changes else "No review yet"
                rows.append(
                    {
                        "id": source.file_id,
                        "current": source.path.name,
                        "preview": preview.new_path.name if preview else "No filename change",
                        "decision": "Include rename" if rename else "Keep filename",
                        "validation": issues or "Ready",
                    }
                )

        self._preview_rows, self._previews_visible = rows, True
        self.changed.emit()

    @Slot()
    def closePreviews(self) -> None:
        self._previews_visible = False
        self.changed.emit()

    @Slot(str, object)
    def _record_controller_failure(self, operation_id: str, _error: object) -> None:
        if operation_id in self._requests:
            # A reducer exception still precedes the bridge's typed result.
            # Preserve transaction truth instead of inventing unknown outcomes.
            self._failed_reconciliations.add(operation_id)

    @Slot(str, object)
    def _completed(self, operation_id: str, result: object) -> None:
        request = self._requests.get(operation_id)

        if request is None:
            return

        if not isinstance(result, ApplyBatchResult) or result.operation_id != operation_id:
            self._failed(operation_id, None)
            return

        del self._requests[operation_id]
        reconciliation_failed = operation_id in self._failed_reconciliations
        self._failed_reconciliations.discard(operation_id)
        self.last_result, self._last_request = result, request
        files = tuple(item for group in result.groups for item in group.files)

        if reconciliation_failed:
            changed = {(item.file_id, item.source_path) for item in files if item.filesystem_changed}
            affected = tuple(
                group.group.group_id
                for group in self._host.session_state.groups
                if any((source.file_id, source.path) in changed for source in group.group.files)
            )

            if affected:
                self._host.set_state(mark_groups_requires_rescan(self._host.session_state, affected))
        else:
            self._consume_inclusion(request, result)

        completed = sum(item.status is ApplyFileOutcomeStatus.APPLIED for item in files)
        failed = sum(item.status is ApplyFileOutcomeStatus.FAILED for item in files)
        not_started = sum(item.skip_reason is FileSkipReason.CANCELLED_BEFORE_START for item in files)
        skipped = sum(item.status is ApplyFileOutcomeStatus.SKIPPED for item in files) - not_started
        cancelled = sum(item.status is ApplyFileOutcomeStatus.CANCELLED for item in files)
        unchanged = sum(item.status is ApplyFileOutcomeStatus.NO_CHANGES for item in files)
        warnings = (
            reconciliation_failed
            or result.report_result.status is ReportWriteStatus.FAILED
            or any(item.refresh_issues for item in files)
        )
        self._terminal_label = {
            ApplyBatchStatus.SUCCEEDED: "Completed with warnings" if warnings else "Completed",
            ApplyBatchStatus.PARTIAL: "Partially completed",
            ApplyBatchStatus.CANCELLED: "Cancelled",
            ApplyBatchStatus.FAILED: "Failed",
        }[result.status]
        message = (
            f"{self._terminal_label}: {completed} completed, {failed} failed, {skipped} skipped, "
            f"{not_started} not started, {cancelled} cancelled at a safe boundary, {unchanged} unchanged."
        )

        if any(item.requires_rescan for item in files):
            message += " Rescan affected groups before further edits."

        if reconciliation_failed:
            message += " Actual file results were retained; reconciliation failed. Rescan affected groups."

        self._terminal_message = redact_sensitive_text(message + "\n\n" + self._result_details(request, result))
        self._result_rows = [self._result_row(item) for item in files]
        self._results_visible = True
        self._host.set_status(self._terminal_message)
        self.changed.emit()

    def _consume_inclusion(self, request: ApplyBatchRequest, result: ApplyBatchResult) -> None:
        state = self._host.session_state

        if state.library_revision != request.base_library_revision:
            return

        confirmed = {(group.group_id, item.source.file_id): item for group in request.groups for item in group.files}
        outcomes = {(group.group_id, item.file_id): item for group in result.groups for item in group.files}
        receipts = {receipt.source.file_id: receipt.source for receipt in state.written_files}
        completed: set[str] = set()

        for group in state.groups:
            if group.requires_rescan:
                continue

            reviews = {review.file_id: review for review in group.reviewed_files}

            for source in group.group.files:
                key = (group.group.group_id, source.file_id)
                original, outcome = confirmed.get(key), outcomes.get(key)

                if original is None or outcome is None or outcome.source_path != original.source.path:
                    continue

                review = reviews.get(source.file_id)
                # Positive refresh receipts and absence of a later review are
                # required. Equal IDs alone cannot consume a newer edit.
                verified = (
                    outcome.status is ApplyFileOutcomeStatus.APPLIED
                    and review is None
                    and source == outcome.refreshed_source
                    and receipts.get(source.file_id) == source
                )
                unchanged = (
                    outcome.status is ApplyFileOutcomeStatus.NO_CHANGES
                    and source == original.source
                    and review is not None
                    and review.reviews == original.reviews
                    and review.change_set is not None
                    and not review.change_set.metadata_changes
                    and review.change_set.rename_change is None
                )

                if verified or unchanged:
                    completed.add(source.file_id)

        if completed:
            self._host.set_included_file_ids(self._host._included - completed)

    @Slot(str)
    def _cancelled(self, operation_id: str) -> None:
        request = self._requests.pop(operation_id, None)

        if request is None:
            return

        self._failed_reconciliations.discard(operation_id)
        self.last_result, self._last_request = None, request
        self._terminal_label = "Cancelled"
        self._terminal_message = (
            "Apply cancelled before any files were processed. All confirmed files were not started.\n\n"
            "Tag updates / renames / backups / report: not run."
        )
        self._terminal_without_receipt(request, "not_started")

    @Slot(str, object)
    def _failed(self, operation_id: str, _error: object) -> None:
        request = self._requests.pop(operation_id, None)

        if request is None:
            return

        self._failed_reconciliations.discard(operation_id)
        self.last_result, self._last_request = None, request
        self._terminal_label = "Failed"
        self._terminal_message = (
            "Apply failed before returning file results. File outcomes are unknown; "
            "rescan the affected groups before further edits.\n\n"
            "Tag updates / renames / backups / report: outcome unavailable. Inspect the affected files."
        )
        known = {group.group.group_id for group in self._host.session_state.groups}
        affected = tuple(group.group_id for group in request.groups if group.group_id in known)

        if affected:
            self._host.set_state(mark_groups_requires_rescan(self._host.session_state, affected))

        self._terminal_without_receipt(request, "unknown")

    def _terminal_without_receipt(self, request: ApplyBatchRequest, status: str) -> None:
        self._result_rows = [
            {
                "id": item.source.file_id,
                "file": item.source.path.name,
                "status": status,
                "final": str(item.source.path),
                "details": self._terminal_message,
            }
            for group in request.groups
            for item in group.files
        ]
        self._results_visible = True
        self._host.set_status(self._terminal_message)
        self.changed.emit()

    @staticmethod
    def _result_row(outcome: ApplyFileOutcome) -> dict[str, str]:
        reasons = [issue.message for issue in outcome.change_set.validation.issues]
        reasons.extend(issue.message for issue in outcome.refresh_issues)
        transaction = outcome.transaction_result

        if transaction is not None:
            reasons.extend(issue.message for issue in transaction.issues)

            if outcome.final_path != outcome.source_path and any(
                issue.code is MediaErrorCode.CLEANUP_FAILED for issue in transaction.issues
            ):
                reasons.append("Both the original and renamed files were retained.")

        if outcome.skip_reason is not None:
            reasons.append(outcome.skip_reason.value.replace("_", " "))

        status = "not_started" if outcome.skip_reason is FileSkipReason.CANCELLED_BEFORE_START else outcome.status.value
        values = {
            "file": outcome.source_path.name,
            "status": status,
            "final": str(outcome.final_path),
            "details": "; ".join(reasons),
        }
        values = {key: redact_sensitive_text(value) for key, value in values.items()}

        # Stable identity is an internal command key, not display text. Do not
        # run it through presentation redaction or replace it with a row index.
        return {"id": outcome.file_id, **values}

    @staticmethod
    def _result_details(request: ApplyBatchRequest, result: ApplyBatchResult) -> str:
        files = tuple(item for group in result.groups for item in group.files)
        written = tuple(item for item in files if item.status is ApplyFileOutcomeStatus.APPLIED)
        updates = sum(bool(item.change_set.metadata_changes) for item in written)
        renames = sum(item.change_set.rename_change is not None for item in written)
        backups = "disabled"

        if request.backup.enabled:
            count = sum(
                item.transaction_result is not None and item.transaction_result.completed_backup_path is not None
                for item in files
            )
            backups = f"{count} completed before file processing"

        report = result.report_result
        report_text = {
            ReportWriteStatus.DISABLED: "disabled",
            ReportWriteStatus.WRITTEN: f"written to {report.path}",
            ReportWriteStatus.FAILED: f"failed ({report.error_code}); completed media writes remain completed",
        }[report.status]
        return (
            f"Verified completed tag updates: {updates}; renames: {renames}. Backups: {backups}. Report: {report_text}."
        )

    @Slot()
    def showResults(self) -> None:
        if self._terminal_label:
            self._results_visible = True
            self.changed.emit()

    @Slot()
    def closeResults(self) -> None:
        self._results_visible = False
        self.changed.emit()
