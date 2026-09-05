"""Translate review controls into pure session edits and refresh their projection."""

from PySide6.QtCore import QObject, Slot
from PySide6.QtWidgets import QDialog, QMessageBox

from metadata_polisher.application.changes import ChangeIssueSeverity, RenameDecision
from metadata_polisher.domain.metadata import FieldReadState, MetadataField, Position
from metadata_polisher.domain.review import FieldDecisionKind, FieldValue
from metadata_polisher.session.mapping_editing import apply_manual_track_mapping
from metadata_polisher.session.review_editing import (
    BatchReviewAction,
    BatchReviewCommand,
    apply_batch_review,
    apply_field_decision,
    review_undo_targets,
    undo_last_review_action,
)
from metadata_polisher.session.state import GroupSelection, GroupState
from metadata_polisher.ui.dialogs.manual_value_dialog import ManualValueDialog
from metadata_polisher.ui.dialogs.track_mapping_dialog import TrackMappingDialog
from metadata_polisher.ui.main_window import MainWindow
from metadata_polisher.ui.models.diff_model import FIELD_LABELS, format_field_value


class ReviewController(QObject):
    """Keep all review decisions in the session service, never in Qt models."""

    def __init__(self, window: MainWindow) -> None:
        super().__init__(window)
        self._window = window
        window.keep_existing_button.clicked.connect(lambda: self._decide(FieldDecisionKind.KEEP_EXISTING))
        window.use_proposed_button.clicked.connect(lambda: self._decide(FieldDecisionKind.USE_PROPOSAL))
        window.clear_value_button.clicked.connect(lambda: self._decide(FieldDecisionKind.CLEAR))
        window.manual_value_button.clicked.connect(self.edit_manual)
        window.track_mapping_button.clicked.connect(self.edit_mapping)
        window.accept_safe_additions_button.clicked.connect(self.accept_safe)
        window.keep_filename_button.clicked.connect(lambda: self._rename(RenameDecision.KEEP_FILENAME))
        window.apply_rename_button.clicked.connect(lambda: self._rename(RenameDecision.APPLY_RENAME))
        window.select_all_files_button.clicked.connect(self.select_all_files)
        window.clear_file_selection_button.clicked.connect(self.clear_file_selection)
        window.undo_review_button.clicked.connect(self.undo_review)
        window.review_context_changed.connect(self.refresh)
        self.refresh()

    def _group(self) -> GroupState | None:
        state = self._window.session_state

        if not isinstance(state.selection, GroupSelection):
            return None

        return next(item for item in state.groups if item.group.group_id == state.selection.group_id)

    def _editable_group(self) -> GroupState | None:
        group = self._group()

        if group is None or group.requires_rescan or self._window.session_state.active_operation is not None:
            return None

        return group

    def _review_target(self) -> tuple[GroupState | None, str | None]:
        """Resolve the scoped file independently from the highlighted table row."""
        file_ids = self._window.review_target_file_ids()

        if not file_ids:
            return None, None

        file_id = file_ids[0]
        group = next((group for group in self._window.session_state.groups
                      if any(source.file_id == file_id for source in group.group.files)), None)

        return group, file_id

    def _can_review(self, group: GroupState | None) -> bool:
        return (
            group is not None and not group.requires_rescan
            and self._window.session_state.active_operation is None
        )

    def _can_review_scope(self, file_ids: tuple[str, ...]) -> bool:
        """Allow valid batch members even when the first album needs a rescan."""
        selected = frozenset(file_ids)

        if self._window.session_state.active_operation is not None or not selected:
            return False

        # The session service reports blocked members individually. Checking
        # just the first displayed file would prevent every other selected
        # album from having its filename reviewed until that album is rescanned.
        return any(
            not group.requires_rescan and any(source.file_id in selected for source in group.group.files)
            for group in self._window.session_state.groups
        )

    @staticmethod
    def _manual_fields_compatible(fields: tuple[MetadataField, ...]) -> bool:
        """A common value needs one input type across all highlighted fields."""
        # A single editor can provide one semantic value type. In particular,
        # text, a tuple of names and a number/total pair cannot be interchanged.
        sequence_fields = {
            MetadataField.ARTISTS, MetadataField.ALBUM_ARTISTS, MetadataField.COMPOSERS, MetadataField.GENRES,
        }
        types = {
            "position" if field in {MetadataField.TRACK, MetadataField.DISC}
            else "sequence" if field in sequence_fields else "text"
            for field in fields
        }

        return len(types) == 1

    def _refresh_undo(self) -> None:
        """Describe the same available action that the session-wide Undo executes."""
        window = self._window
        targets = review_undo_targets(window.session_state)
        window.undo_review_button.setEnabled(bool(targets) and window.library_controller is not None)

        if window.session_state.active_operation is not None:
            window.undo_review_button.setToolTip("Wait for the current operation before undoing review decisions.")
            return

        if not targets:
            window.undo_review_button.setToolTip(
                "No pending review action can be undone. Completed writes are excluded.",
            )
            return

        albums = {
            source.file_id: group.group.album_title or group.group.group_id
            for group in window.session_state.groups for source in group.group.files
        }
        details = []

        for target in targets[:8]:
            before = {review.field: review for review in target.before.reviews}
            fields = [FIELD_LABELS[review.field] for review in target.after.reviews
                      if before.get(review.field) != review]
            before_rename = (target.before.change_set.rename_decision
                             if target.before.change_set else RenameDecision.KEEP_FILENAME)
            after_rename = (target.after.change_set.rename_decision
                            if target.after.change_set else RenameDecision.KEEP_FILENAME)

            if before_rename is not after_rename:
                fields.append("Filename")

            details.append(f"{albums[target.source.file_id]} · {target.source.path.name}: {', '.join(fields)}")

        if len(targets) > len(details):
            details.append(f"… and {len(targets) - len(details)} more files")

        count = f"{len(targets)} {'file' if len(targets) == 1 else 'files'}"
        window.undo_review_button.setToolTip("\n".join((
            f"Undo the last available review action ({count}):", *details,
            "This changes pending review decisions only; completed disk writes are not undone.",
        )))

    @Slot()
    def select_all_files(self) -> None:
        """Highlight the displayed music files and make that review scope explicit."""
        window = self._window

        if window.session_state.active_operation is not None or not window.file_model.rowCount():
            return

        # An existing Included files or All library files scope can contain
        # different files. Selecting this table must visibly return review to
        # the highlighted rows, without adding them to the write batch.
        window.review_scope_combo.setCurrentIndex(window.review_scope_combo.findData("selected"))
        window.file_table_view.selectAll()

    @Slot()
    def clear_file_selection(self) -> None:
        """Clear highlighted review targets while retaining explicit write inclusion."""
        window = self._window

        if window.session_state.active_operation is not None:
            return

        window.review_scope_combo.setCurrentIndex(window.review_scope_combo.findData("selected"))
        window.file_table_view.clearSelection()

    @Slot()
    def refresh(self) -> None:
        window = self._window
        group, file_id = self._review_target()
        fields = window.selected_fields()
        field = fields[0] if fields else None
        source = next((item for item in group.group.files if item.file_id == file_id), None) if group else None
        reviewed = next((item for item in group.reviewed_files if item.file_id == file_id), None) if group else None
        review = next((item for item in reviewed.reviews if item.field is field), None) if reviewed else None
        editable = self._can_review(group)
        file_ids = window.review_target_file_ids()
        scope_editable = self._can_review_scope(file_ids)
        multiple = len(file_ids) > 1 or len(fields) > 1
        file_label = "file" if len(file_ids) == 1 else "files"
        field_label = "field" if len(fields) == 1 else "fields"
        window.review_scope_label.setText(
            f"{len(file_ids)} {file_label} · {len(fields)} {field_label} in this review action",
        )
        self._refresh_undo()
        window.manual_value_button.setText("Set common value…" if multiple else "Manual…")
        window.use_proposed_button.setText("Use each file's candidate" if multiple else "Use candidate")
        mapping_group = self._editable_group()
        window.track_mapping_button.setEnabled(
            mapping_group is not None and mapping_group.effective_track_mapping is not None
        )
        field_editable = (
            editable and source is not None and field is not None
            and source.read_result.field_states[field] in (FieldReadState.PRESENT, FieldReadState.MISSING)
        )

        for button in (window.keep_existing_button, window.manual_value_button, window.clear_value_button):
            button.setEnabled(field_editable)

        manual_compatible = self._manual_fields_compatible(fields)
        window.manual_value_button.setEnabled(field_editable and manual_compatible)
        window.manual_value_button.setToolTip(
            "Choose fields of the same value type to set a common value; edit text, lists and positions separately."
            if fields and not manual_compatible else "Edit the highlighted fields (F2, or double-click Final)."
        )

        window.proposal_combo.clear()

        if review is not None:
            for index, proposal in enumerate(review.proposals):
                sources = ", ".join(dict.fromkeys(member.provenance.source_id for member in proposal.members))
                language = f" · {proposal.language}" if proposal.language else ""
                window.proposal_combo.addItem(
                    f"{format_field_value(proposal.value)}{language} · {sources} · {proposal.confidence.value}", index,
                )

            if review.selected_proposal in review.proposals:
                window.proposal_combo.setCurrentIndex(review.proposals.index(review.selected_proposal))

        has_proposal = field_editable and review is not None and bool(review.proposals)
        window.proposal_combo.setEnabled(has_proposal and not multiple)
        window.proposal_combo.setVisible(not multiple)
        window.use_proposed_button.setEnabled(field_editable and bool(file_ids) if multiple else has_proposal)
        window.accept_safe_additions_button.setEnabled(editable and group is not None and bool(group.reviewed_files))
        window.keep_filename_button.setEnabled(scope_editable)
        renaming_enabled = window.library_controller is not None and window.library_controller.settings.rename.enabled
        window.apply_rename_button.setEnabled(scope_editable and renaming_enabled)
        window.rename_validation_label.clear()

        if window.library_controller is not None:
            window.rename_template_label.setText(f"Template: {window.library_controller.settings.rename.template}")

        if len(file_ids) > 1:
            self._show_batch_rename_status(file_ids)

        elif reviewed is not None and reviewed.change_set is not None:
            change = reviewed.change_set
            decision_label = (
                "Rename will apply" if change.rename_decision is RenameDecision.APPLY_RENAME else "Filename kept"
            )
            issues = [
                f"{'Blocked' if issue.severity is ChangeIssueSeverity.BLOCKING else 'Warning'}: "
                f"{issue.message} ({issue.code.value})"
                for issue in change.validation.issues
            ]
            window.rename_validation_label.setText("\n".join((decision_label, *issues)))

    def _show_batch_rename_status(self, file_ids: tuple[str, ...]) -> None:
        """Summarise the whole scope instead of presenting its first filename as all files."""
        window = self._window
        selected = frozenset(file_ids)
        changes = tuple(
            reviewed.change_set
            for group in window.session_state.groups for reviewed in group.reviewed_files
            if reviewed.file_id in selected and reviewed.change_set is not None
        )
        included = sum(change.rename_decision is RenameDecision.APPLY_RENAME for change in changes)
        issues = tuple(issue for change in changes for issue in change.validation.issues)
        blocking = sum(issue.severity is ChangeIssueSeverity.BLOCKING for issue in issues)
        warnings = len(issues) - blocking
        status = [f"Renames included: {included} of {len(file_ids)} files."]

        if issues:
            status.append(f"Blocking issues: {blocking} · Warnings: {warnings}. See Filename previews…")

        window.rename_validation_label.setText("\n".join(status))

    def _decide(self, decision: FieldDecisionKind, value: FieldValue | None = None) -> None:
        window = self._window
        group, file_id = self._review_target()
        file_ids, fields = window.review_target_file_ids(), window.selected_fields()

        # Qt retains a current index when Ctrl-click deselects that row. Only
        # highlighted stable field identities authorise a review command.
        field = fields[0] if fields else None

        if (
            not self._can_review(group) or group is None or file_id is None or field is None
            or window.library_controller is None
        ):
            return

        if len(file_ids) > 1 or len(fields) > 1:
            # Batch candidate/keep actions are resolved per file by the service.
            # Reusing the first visible proposal would duplicate one track's title.
            action = {
                FieldDecisionKind.KEEP_EXISTING: BatchReviewAction.KEEP_EXISTING,
                FieldDecisionKind.USE_PROPOSAL: BatchReviewAction.USE_CANDIDATE,
                FieldDecisionKind.USE_MANUAL: BatchReviewAction.SET_COMMON_VALUE,
                FieldDecisionKind.CLEAR: BatchReviewAction.CLEAR,
            }[decision]
            self._batch(action, fields, value)
            return

        try:
            state = apply_field_decision(
                window.session_state, group.group.group_id, file_id, field, decision,
                window.library_controller.settings.rename,
                manual_value=value, proposal_index=max(0, window.proposal_combo.currentIndex()),
            )
        except (TypeError, ValueError) as exc:
            window.workflow_message_label.setText(str(exc))
            return

        window.set_session_state(state)

    def _batch(
        self, action: BatchReviewAction, fields: tuple[MetadataField, ...], value: FieldValue | None = None,
    ) -> None:
        window = self._window
        library = window.library_controller
        file_ids = window.review_target_file_ids()
        snapshot = window.session_state

        if library is None or not file_ids:
            return

        groups = [group for group in snapshot.groups
                  if any(source.file_id in file_ids for source in group.group.files)]
        # Shared album fields across groups need an explicit scope acknowledgement;
        # ordinary per-track decisions must retain each file's own proposed value.
        cross_group = len(groups) > 1 and bool(set(fields) & {
            MetadataField.ALBUM, MetadataField.ALBUM_ARTISTS, MetadataField.DATE,
        }) and action in {BatchReviewAction.USE_CANDIDATE, BatchReviewAction.SET_COMMON_VALUE, BatchReviewAction.CLEAR}

        if cross_group:
            answer = QMessageBox.warning(
                window.review_window, "Review scope spans albums",
                f"This action affects {len(file_ids)} files across {len(groups)} groups.\n"
                "Continue with this explicitly selected album-field scope?",
                QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )

            if answer != QMessageBox.StandardButton.Ok or window.session_state is not snapshot:
                return

        try:
            # Stable file/field IDs plus the captured revision identify exactly
            # what the user reviewed, even if a later projection reorders rows.
            result = apply_batch_review(snapshot, BatchReviewCommand(
                file_ids, fields, snapshot.revision, action, value, cross_group,
            ), library.settings.rename)
        except (TypeError, ValueError) as error:
            window.workflow_message_label.setText(str(error))
            return

        window.set_session_state(result.state)
        window.workflow_message_label.setText(
            f"Review: {len(result.affected)} affected · {len(result.skipped)} skipped · {len(result.blocked)} blocked "
            f"file/field decisions across {len(file_ids)} files. No files changed.",
        )
        window.workflow_message_label.setToolTip("\n".join(
            f"{item.file_id} · {item.field.value if item.field else 'Filename'}: {item.reason}"
            for item in (*result.skipped, *result.blocked)
        ))

    @Slot()
    def undo_review(self) -> None:
        window = self._window

        if window.library_controller is None or not review_undo_targets(window.session_state):
            return

        try:
            window.set_session_state(undo_last_review_action(
                window.session_state, window.library_controller.settings.rename,
            ))
        except ValueError as error:
            window.workflow_message_label.setText(str(error))
            return

        window.workflow_message_label.setText("Last review action undone in memory. No disk writes were undone.")

    @Slot()
    def edit_manual(self) -> None:
        window = self._window
        group, file_id = self._review_target()
        captured_files = window.review_target_file_ids()
        captured_fields = window.selected_fields()
        field = captured_fields[0] if captured_fields else None

        if (
            not self._can_review(group) or group is None or file_id is None or field is None
            or not self._manual_fields_compatible(captured_fields)
        ):
            return

        source = next(item for item in group.group.files if item.file_id == file_id)
        reviewed = next((item for item in group.reviewed_files if item.file_id == file_id), None)
        metadata = (
            reviewed.change_set.final_metadata if reviewed and reviewed.change_set else source.read_result.metadata
        )
        value = getattr(metadata, field.value)

        # Missing semantic sequences/positions are represented by empty values
        # in a snapshot. The editor uses None to open an empty input, while its
        # acceptance validation still rejects an empty manual decision.
        if value in (None, (), Position()) or isinstance(value, str) and not value.strip():
            value = None

        dialog = ManualValueDialog(field, value, window.review_window)
        snapshot = window.session_state

        if len(captured_files) > 1 or len(captured_fields) > 1:
            labels = ", ".join(FIELD_LABELS[item] for item in captured_fields)
            count = f"{len(captured_files)} {'file' if len(captured_files) == 1 else 'files'}"
            dialog.setWindowTitle(f"Set common {labels} for {count}")

        # A modal dialog still processes queued worker signals. Never apply
        # its value to a newer scan or to another file selected meanwhile.
        if (
            dialog.exec() == QDialog.DialogCode.Accepted
            and window.session_state is snapshot
            and self._review_target()[1] == file_id
            and window.review_target_file_ids() == captured_files
            and window.selected_fields() == captured_fields
        ):
            self._decide(FieldDecisionKind.USE_MANUAL, dialog.value())

    @Slot()
    def accept_safe(self) -> None:
        window = self._window
        group, _file_id = self._review_target()

        if self._can_review(group) and window.library_controller is not None:
            self._batch(BatchReviewAction.ACCEPT_SAFE_ADDITIONS, tuple(MetadataField))

    def _rename(self, decision: RenameDecision) -> None:
        window = self._window
        file_ids = window.review_target_file_ids()

        # Empty field scope makes filename intent independent of tag decisions.
        # Including a rename therefore cannot approve unrelated metadata changes.
        if self._can_review_scope(file_ids) and window.library_controller is not None:
            self._batch(BatchReviewAction.INCLUDE_RENAMES if decision is RenameDecision.APPLY_RENAME
                        else BatchReviewAction.KEEP_FILENAMES, ())

    @Slot()
    def edit_mapping(self) -> None:
        window = self._window
        group = self._editable_group()

        if (
            group is None or group.selected_release is None or group.effective_track_mapping is None
            or window.library_controller is None
        ):
            return

        snapshot = window.session_state
        dialog = TrackMappingDialog(
            group.group.files, group.selected_release.candidate.candidate, group.effective_track_mapping, window,
        )

        # A modal dialogue still runs Qt's event loop. Accept its mapping only
        # while the library snapshot used to construct the choices is current.
        if dialog.exec() == QDialog.DialogCode.Accepted and window.session_state is snapshot:
            settings = window.library_controller.settings
            window.set_session_state(apply_manual_track_mapping(
                snapshot, group.group.group_id, dialog.mapping(), settings.rename,
                preferred_language=settings.matching.preferred_language,
            ))
