"""Describe session-only decisions before replacing a library or closing it."""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox

from metadata_polisher.application.changes import RenameDecision
from metadata_polisher.domain.review import DecisionOrigin
from metadata_polisher.scanner.grouping import GroupingReason
from metadata_polisher.session.state import SessionState

if TYPE_CHECKING:
    from metadata_polisher.ui.main_window import MainWindow


@dataclass(frozen=True)
class PendingWorkSummary:
    """Counts of deliberate work, rather than automatically proposed changes."""

    field_decision_count: int = 0
    reviewed_file_count: int = 0
    rename_file_count: int = 0
    included_file_count: int = 0
    edited_group_count: int = 0
    undo_action_count: int = 0

    @property
    def has_pending_work(self) -> bool:
        return any((
            self.field_decision_count,
            self.rename_file_count,
            self.included_file_count,
            self.edited_group_count,
            self.undo_action_count,
        ))

    def description(self) -> str:
        """Keep the different decisions visible without adding overlapping totals."""
        lines = []

        if self.field_decision_count:
            fields = "decision" if self.field_decision_count == 1 else "decisions"
            files = "file" if self.reviewed_file_count == 1 else "files"
            lines.append(
                f"{self.field_decision_count} metadata {fields} in {self.reviewed_file_count} {files}"
                " (including Keep existing)",
            )

        if self.rename_file_count:
            files = "file" if self.rename_file_count == 1 else "files"
            lines.append(f"{self.rename_file_count} {files} chosen for renaming")

        if self.included_file_count:
            files = "file" if self.included_file_count == 1 else "files"
            lines.append(f"{self.included_file_count} {files} included for writing")

        if self.edited_group_count:
            groups = "group" if self.edited_group_count == 1 else "groups"
            lines.append(f"{self.edited_group_count} {groups} with grouping or metadata lookup choices")

        if self.undo_action_count:
            actions = "action" if self.undo_action_count == 1 else "actions"
            lines.append(f"{self.undo_action_count} review {actions} in undo history")

        return "\n".join(lines)


def pending_work_summary(
    state: SessionState, included_file_ids: frozenset[str] = frozenset(),
) -> PendingWorkSummary:
    """Count only intent that scanning or exiting would discard from memory."""
    field_decisions = 0
    reviewed_files = 0
    rename_files = 0
    edited_groups = 0
    known_file_ids = {source.file_id for group in state.groups for source in group.group.files}

    for group in state.groups:
        # A selected release is an explicit chooser action. Merely receiving
        # candidates or building default field reviews is not a user decision.
        if (
            group.group.reason in {GroupingReason.MANUAL_SPLIT, GroupingReason.MANUAL_MERGE}
            or group.selected_release is not None
            or group.manual_track_mapping is not None
            or group.language_override is not None
            or group.disc_number_override is not None
            or group.search_query_override is not None
        ):
            edited_groups += 1

        for reviewed in group.reviewed_files:
            # Count explicit intent even when Keep existing or a manual value
            # produces no disk difference; losing that resolved choice is work loss.
            decisions = sum(review.decision_origin is DecisionOrigin.USER for review in reviewed.reviews)
            field_decisions += decisions
            reviewed_files += bool(decisions)

            if reviewed.change_set is not None and reviewed.change_set.rename_decision is RenameDecision.APPLY_RENAME:
                # Count rename intent even when its preview is currently blocked
                # or happens to match the original filename.
                rename_files += 1

    return PendingWorkSummary(
        field_decision_count=field_decisions,
        reviewed_file_count=reviewed_files,
        rename_file_count=rename_files,
        included_file_count=len(included_file_ids & known_file_ids),
        edited_group_count=edited_groups,
        undo_action_count=len(state.review_undo),
    )


def confirm_discard_pending(window: MainWindow, action: str) -> bool:
    """Require an explicit Discard choice; Enter and Escape both preserve work."""
    summary = pending_work_summary(window.session_state, window.included_file_ids)

    if not summary.has_pending_work:
        return True

    dialog = QMessageBox(window)
    dialog.setObjectName("discardPendingWorkDialog")
    dialog.setWindowTitle("Discard pending work?")
    dialog.setIcon(QMessageBox.Icon.Warning)
    dialog.setTextFormat(Qt.TextFormat.PlainText)
    dialog.setText(f"Discard pending work and {action}?")
    replacement = (
        "Closing the application removes these session choices and undo history."
        if action == "exit"
        else "A successful scan replaces the current groups, review choices and undo history."
    )
    dialog.setInformativeText(
        f"{summary.description()}\n\n{replacement}\n"
        "Choose Cancel to continue reviewing. Completed music-file changes remain saved.",
    )
    dialog.setStandardButtons(QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Discard)
    # Accidental Enter/Escape must preserve the pending session. Discard needs
    # an explicit choice and never claims to undo completed filesystem changes.
    dialog.setDefaultButton(QMessageBox.StandardButton.Cancel)
    dialog.setEscapeButton(QMessageBox.StandardButton.Cancel)

    return dialog.exec() == QMessageBox.StandardButton.Discard
