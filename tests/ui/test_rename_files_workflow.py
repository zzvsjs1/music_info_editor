"""Rename preview choices remain staged until the existing final confirmation."""

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication

from metadata_polisher.application.changes import RenameDecision
from metadata_polisher.ui.dialogs.rename_files_dialog import RenameFilesDialog
from tests.ui.test_review_workflow import review_window
from tests.unit.session.test_batch_review import make_batch_session


def test_cancel_rename_preparation_preserves_entire_session(qtbot, tmp_path):
    window = review_window(qtbot, tmp_path, make_batch_session())
    window.select_all_files_button.click()
    original = window.session_state
    QTimer.singleShot(0, lambda: QApplication.activeModalWidget().reject())
    window.rename_files_button.click()
    assert window.session_state is original
    assert not window.included_file_ids
    assert window.processing_executor.pending == []


def test_chosen_renames_reach_shared_summary_with_pending_tags(qtbot, tmp_path):
    window = review_window(qtbot, tmp_path, make_batch_session())
    window.select_all_files_button.click()
    original = window.session_state
    summaries = []

    def cancel_summary():
        dialog = QApplication.activeModalWidget()
        summaries.append(dialog)
        assert dialog.file_table.model().rowCount() == 2
        # Existing automatic safe additions must still be disclosed at confirmation.
        assert "value added" in dialog.summary_label.text() or "values added" in dialog.summary_label.text()
        dialog.reject()

    def choose():
        dialog = QApplication.activeModalWidget()
        assert isinstance(dialog, RenameFilesDialog)
        assert "Local first" in dialog.table.model().index(0, 2).data()
        dialog.table.model().item(1, 0).setCheckState(Qt.CheckState.Unchecked)
        QTimer.singleShot(0, cancel_summary)
        dialog.accept()

    QTimer.singleShot(0, choose)
    window.rename_files_button.click()
    changes = window.session_state.groups[0].reviewed_files
    assert changes[0].change_set.rename_decision is RenameDecision.APPLY_RENAME
    assert changes[1].change_set.rename_decision is RenameDecision.KEEP_FILENAME
    assert changes[2].change_set.rename_decision is RenameDecision.APPLY_RENAME
    assert window.included_file_ids == {changes[0].file_id, changes[2].file_id}
    assert tuple(item.reviews for item in changes) == tuple(item.reviews for item in original.groups[0].reviewed_files)
    assert len(summaries) == 1
    assert window.processing_executor.pending == []


# Modal dialogues process queued events. Acceptance must reject a draft based
# on the old snapshot if another event installed newer review decisions.
def test_stale_rename_draft_cannot_replace_newer_review(qtbot, tmp_path):
    window = review_window(qtbot, tmp_path, make_batch_session())

    def change_while_open():
        dialog = QApplication.activeModalWidget()
        window.clear_value_button.click()
        dialog.accept()

    QTimer.singleShot(0, change_while_open)
    window.rename_files_button.click()
    assert "library changed" in window.workflow_message_label.text()
    assert not window.included_file_ids
    assert window.processing_executor.pending == []


def test_no_choices_or_blocked_group_cannot_continue(qtbot, tmp_path):
    from metadata_polisher.infrastructure.settings import RenameSettings
    from metadata_polisher.session.state import mark_groups_requires_rescan

    window = review_window(qtbot, tmp_path, make_batch_session())
    state = mark_groups_requires_rescan(window.session_state, ("album",))
    dialog = RenameFilesDialog(state, window.selected_file_ids(), RenameSettings(), window)
    qtbot.addWidget(dialog)
    assert not dialog.continue_button.isEnabled()
    assert "1 blocked" in dialog.status_label.text()
    dialog.table.model().item(0, 0).setCheckState(Qt.CheckState.Unchecked)
    assert not dialog.continue_button.isEnabled()
    assert "0 files chosen" in dialog.status_label.text()


# Unticking is an explicit removal of earlier filename intent, not merely an
# omission from this draft. One accepted dialogue remains one review undo step.
def test_reopening_and_unticking_removes_existing_rename_as_one_undo_action(qtbot, tmp_path):
    from metadata_polisher.infrastructure.settings import RenameSettings
    from metadata_polisher.session.review_editing import undo_last_review_action

    window = review_window(qtbot, tmp_path, make_batch_session())
    window.apply_rename_button.click()
    window.include_selected_button.click()
    original = window.session_state
    window.select_all_files_button.click()
    dialog = RenameFilesDialog(original, window.selected_file_ids(), RenameSettings(), window)
    qtbot.addWidget(dialog)
    dialog.table.model().item(0, 0).setCheckState(Qt.CheckState.Unchecked)
    draft = dialog.draft_state()

    assert draft.groups[0].reviewed_files[0].change_set.rename_decision is RenameDecision.KEEP_FILENAME
    assert all(item.change_set.rename_decision is RenameDecision.APPLY_RENAME
               for item in draft.groups[0].reviewed_files[1:])
    assert len(draft.review_undo) == len(original.review_undo) + 1
    undone = undo_last_review_action(draft, RenameSettings())
    assert tuple(item.change_set.rename_decision for item in undone.groups[0].reviewed_files) == (
        RenameDecision.APPLY_RENAME, RenameDecision.KEEP_FILENAME, RenameDecision.KEEP_FILENAME,
    )


def test_unsupported_highlighting_cannot_start_rename(qtbot, tmp_path):
    from metadata_polisher.bootstrap import create_application
    from tests.ui.test_scan_workflow import ControlledExecutor

    (tmp_path / "unsupported.opus").write_bytes(b"Unsupported disposable fixture")
    executor = ControlledExecutor()
    _, window = create_application([], executor=executor, settings_file=tmp_path / "settings.json")
    qtbot.addWidget(window)
    window.root_path_edit.setText(str(tmp_path))
    window.rescan_button.click()
    executor.run_next()
    qtbot.waitUntil(lambda: window.session_state.active_operation is None)
    window.group_view.setCurrentIndex(window.group_model.index(0, 0))
    window.file_table_view.selectRow(0)
    assert not window.rename_files_button.isEnabled()
    window.apply_controller.rename_files()
    assert QApplication.activeModalWidget() is None
    assert not executor.pending
