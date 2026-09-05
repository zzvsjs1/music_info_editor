"""Explicit file selection makes batch filenames accessible without approving writes."""

from dataclasses import replace

from PySide6.QtCore import Qt

from metadata_polisher.application.changes import RenameDecision
from metadata_polisher.session.state import mark_groups_requires_rescan
from tests.ui.test_review_workflow import review_window
from tests.unit.session.test_batch_review import make_batch_session, selected_ids
from tests.unit.session.test_review_editing import make_local_session, make_source


def library_session():
    """Keep a second album outside the currently displayed table's three rows."""
    state = make_batch_session()
    other = make_local_session(make_source("other.flac")).groups[0]
    other = replace(other, group=replace(other.group, group_id="other"))

    return replace(state, groups=(*state.groups, other))


def test_select_all_button_changes_review_scope_without_changing_write_inclusion(qtbot, tmp_path):
    state = library_session()
    window = review_window(qtbot, tmp_path, state)
    included = frozenset((state.groups[1].group.files[0].file_id,))
    window.set_included_file_ids(included)
    window.review_scope_combo.setCurrentIndex(window.review_scope_combo.findData("included"))
    qtbot.mouseClick(window.select_all_files_button, Qt.MouseButton.LeftButton)

    visible_ids = tuple(source.file_id for source in state.groups[0].group.files)
    assert set(window.selected_file_ids()) == set(visible_ids)
    assert set(window.review_target_file_ids()) == set(visible_ids)
    assert window.review_scope_combo.currentData() == "selected"
    assert window.included_file_ids == included
    assert window.session_state is state
    assert window.processing_executor.pending == []


# Begin with a broader scope to expose stale scope controls: Ctrl+A explicitly
# targets the displayed files and must leave independent write inclusion alone.
def test_ctrl_a_selects_displayed_tracks_and_resets_a_wider_review_scope(qtbot, tmp_path):
    state = library_session()
    window = review_window(qtbot, tmp_path, state)
    window.review_scope_combo.setCurrentIndex(window.review_scope_combo.findData("library"))
    window.show()
    window.activateWindow()
    window.file_table_view.setFocus()
    qtbot.waitUntil(lambda: window.file_table_view.hasFocus())
    qtbot.keyClick(window.file_table_view, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)

    assert set(window.review_target_file_ids()) == {
        source.file_id for source in state.groups[0].group.files
    }
    assert window.review_scope_combo.currentData() == "selected"
    assert not window.included_file_ids


def test_clear_selection_keeps_the_independent_write_batch(qtbot, tmp_path):
    window = review_window(qtbot, tmp_path, make_batch_session())
    included = frozenset(selected_ids(window.session_state))
    window.set_included_file_ids(included)
    qtbot.mouseClick(window.select_all_files_button, Qt.MouseButton.LeftButton)
    qtbot.mouseClick(window.clear_file_selection_button, Qt.MouseButton.LeftButton)

    assert window.selected_file_ids() == window.review_target_file_ids() == ()
    assert window.included_file_ids == included
    assert not window.apply_rename_button.isEnabled()
    assert not window.keep_filename_button.isEnabled()


def test_select_all_prepares_each_filename_and_requires_explicit_write_inclusion(qtbot, tmp_path):
    state = make_batch_session()
    window = review_window(qtbot, tmp_path, state)
    before_reviews = tuple(item.reviews for item in state.groups[0].reviewed_files)
    qtbot.mouseClick(window.select_all_files_button, Qt.MouseButton.LeftButton)
    qtbot.mouseClick(window.apply_rename_button, Qt.MouseButton.LeftButton)
    changes = tuple(item.change_set for item in window.session_state.groups[0].reviewed_files)

    assert all(change.rename_decision is RenameDecision.APPLY_RENAME for change in changes)
    assert tuple(change.rename_change.new_path.name for change in changes) == (
        "1.01. Local first.flac", "1.02. Local second.flac", "1.03. Local third.flac",
    )
    assert tuple(item.reviews for item in window.session_state.groups[0].reviewed_files) == before_reviews
    assert set(window.selected_file_ids()) == set(selected_ids(state))
    assert not window.included_file_ids
    assert "3 of 3" in window.rename_validation_label.text()
    assert window.processing_executor.pending == []

    qtbot.mouseClick(window.include_selected_button, Qt.MouseButton.LeftButton)

    assert window.included_file_ids == frozenset(selected_ids(state))
    assert window.processing_executor.pending == []


def test_library_scope_prepares_renames_and_includes_every_group_only_when_requested(qtbot, tmp_path):
    state = library_session()
    window = review_window(qtbot, tmp_path, state)
    window.review_scope_combo.setCurrentIndex(window.review_scope_combo.findData("library"))

    assert window.review_target_file_ids() == selected_ids(state)
    qtbot.mouseClick(window.apply_rename_button, Qt.MouseButton.LeftButton)

    assert all(item.change_set.rename_decision is RenameDecision.APPLY_RENAME
               for group in window.session_state.groups for item in group.reviewed_files)
    assert "4 of 4" in window.rename_validation_label.text()
    assert not window.included_file_ids

    qtbot.mouseClick(window.include_review_scope_button, Qt.MouseButton.LeftButton)

    assert window.included_file_ids == frozenset(selected_ids(state))
    assert window.processing_executor.pending == []


# A blocked first member must not suppress review of later valid groups. The
# batch result accounts for blocked members while retaining applicable choices.
def test_library_renames_report_a_stale_first_group_and_still_review_other_groups(qtbot, tmp_path):
    state = mark_groups_requires_rescan(library_session(), ("album",))
    window = review_window(qtbot, tmp_path, state)
    window.review_scope_combo.setCurrentIndex(window.review_scope_combo.findData("library"))

    assert window.apply_rename_button.isEnabled()
    qtbot.mouseClick(window.apply_rename_button, Qt.MouseButton.LeftButton)

    assert window.session_state.groups[0] == state.groups[0]
    assert window.session_state.groups[1].reviewed_files[0].change_set.rename_decision is RenameDecision.APPLY_RENAME
    assert "1 affected" in window.workflow_message_label.text()
    assert "3 blocked" in window.workflow_message_label.text()
    assert "1 of 4" in window.rename_validation_label.text()
    assert not window.included_file_ids
    assert window.processing_executor.pending == []


def test_batch_filename_summary_counts_choices_beyond_the_first_file(qtbot, tmp_path):
    window = review_window(qtbot, tmp_path, make_batch_session())
    window.file_table_view.selectRow(1)
    qtbot.mouseClick(window.apply_rename_button, Qt.MouseButton.LeftButton)
    qtbot.mouseClick(window.select_all_files_button, Qt.MouseButton.LeftButton)

    assert "1 of 3" in window.rename_validation_label.text()
    assert "Filename kept" not in window.rename_validation_label.text()
