"""Populated layout and activation regressions for the native V1.1 workspace."""

from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QAbstractItemView, QApplication, QDialog

from metadata_polisher.domain.metadata import MetadataField
from metadata_polisher.domain.review import FieldDecisionKind
from metadata_polisher.session.state import GroupSelection, SessionState
from metadata_polisher.ui.main_window import MainWindow
from tests.ui.test_lookup_workflow import lookup_window
from tests.ui.test_main_window import make_group
from tests.ui.test_review_workflow import field_review, review_window


def test_groups_remain_left_of_files_with_review_in_an_independent_window(qtbot):
    window = MainWindow()
    qtbot.addWidget(window)
    window.resize(960, 700)
    window.show()

    assert window.main_splitter.count() == 2
    right = window.main_splitter.widget(1)
    assert right.isAncestorOf(window.file_table_view)
    assert not window.main_splitter.isAncestorOf(window.diff_table_view)
    assert isinstance(window.review_window, QDialog)
    assert window.review_window.isWindow()
    assert window.review_window.isAncestorOf(window.diff_table_view)
    assert window.review_window.windowModality() == Qt.WindowModality.NonModal
    assert window.width() <= 960
    assert window.height() <= 700
    assert window.diff_table_view.selectionMode() == QAbstractItemView.SelectionMode.ExtendedSelection


def test_field_is_a_named_column_and_refresh_preserves_manual_column_width(qtbot):
    window = MainWindow()
    qtbot.addWidget(window)
    headers = [window.diff_model.headerData(index, Qt.Orientation.Horizontal)
               for index in range(window.diff_model.columnCount())]

    assert headers[:5] == ["Field", "Status", "Existing", "Proposed", "Final"]
    window.diff_table_view.setColumnWidth(2, 213)
    window.set_session_state(window.session_state)
    assert window.diff_table_view.columnWidth(2) == 213


def test_long_japanese_and_latin_filenames_do_not_force_a_wide_window(qtbot):
    window = MainWindow()
    qtbot.addWidget(window)
    group = make_group("long-album", "長い曲名と説明_Original_Soundtrack_" * 6, "交響組曲 – Original soundtrack")
    window.set_session_state(SessionState(
        root=Path("library"), groups=(group,), selection=GroupSelection("long-album"),
    ))
    window.file_table_view.selectRow(0)
    window.show()
    window.resize(960, 700)

    assert window.width() <= 960
    assert window.height() <= 700


def populated_tracks_window(qtbot):
    window = MainWindow()
    qtbot.addWidget(window)
    group = make_group("album", "01. Opening.flac", "Original soundtrack")
    source = group.group.files[0]
    group = replace(group, group=replace(group.group, files=tuple(
        replace(source, file_id=f"track-{number}", path=source.path.with_name(f"{number:02}. Track.flac"))
        for number in range(1, 28)
    )))
    window.set_session_state(SessionState(
        root=Path("library"), groups=(group,), selection=GroupSelection("album"),
    ))
    window.resize(1180, 820)
    window.show()
    qtbot.wait(10)
    return window


# Review is a separate non-modal window in the current design. Opening it must
# neither consume the main table's height nor prevent navigation behind it.
def test_opening_review_keeps_the_full_track_workspace_available(qtbot):
    window = populated_tracks_window(qtbot)

    assert not window.diff_table_view.isVisible()
    assert window.file_table_view.viewport().height() >= 8 * window.file_table_view.rowHeight(0)
    assert 280 <= window.main_splitter.sizes()[0] <= 320
    track_height = window.file_table_view.viewport().height()

    window.file_table_view.selectRow(0)
    assert not window.diff_table_view.isVisible()
    qtbot.mouseClick(window.open_review_button, Qt.MouseButton.LeftButton)
    qtbot.wait(10)
    assert window.diff_table_view.isVisible()
    assert window.file_table_view.viewport().height() == track_height
    assert QApplication.activeModalWidget() is None

    # The main table remains usable while its review window is open.
    window.file_table_view.selectRow(1)
    assert window.review_target_file_ids() == ("track-2",)
    assert not window.included_file_ids

    qtbot.mouseClick(window.open_review_button, Qt.MouseButton.LeftButton)
    assert window.review_window.isVisible()


def test_empty_selection_leaves_review_open_and_the_scope_reachable(qtbot):
    window = populated_tracks_window(qtbot)
    window.file_table_view.selectRow(0)
    window.open_metadata_review()
    window.file_table_view.clearSelection()

    assert window.review_window.isVisible()
    assert window.review_scope_combo.isVisible()
    assert not window.review_target_file_ids()
    assert "Select" in window.review_window.target_label.text()

    window.file_table_view.selectRow(1)
    assert window.review_window.isVisible()
    assert window.review_target_file_ids() == ("track-2",)


def test_close_and_escape_preserve_the_review_window_size_and_selection(qtbot):
    window = populated_tracks_window(qtbot)
    window.file_table_view.selectRow(0)
    window.open_metadata_review()
    review = window.review_window
    review.resize(860, 600)
    expected_size = review.size()
    snapshot = window.session_state
    review.close()

    assert not review.isVisible()
    assert window.isVisible()
    assert window.session_state is snapshot
    window.open_review_button.click()

    assert window.review_window is review
    assert review.isVisible()
    assert review.size() == expected_size
    assert window.selected_file_ids() == ("track-1",)

    qtbot.keyClick(review, Qt.Key.Key_Escape)
    assert not review.isVisible()
    assert window.session_state is snapshot


def test_closing_review_preserves_pending_decisions_and_write_inclusion(qtbot, tmp_path):
    window = review_window(qtbot, tmp_path)
    window.show()
    file_id = window.selected_file_ids()[0]
    window.set_included_file_ids(frozenset({file_id}))
    window.clear_value_button.click()
    snapshot = window.session_state
    assert field_review(window, MetadataField.TITLE).decision is FieldDecisionKind.CLEAR
    window.review_window.close()
    window.open_review_button.click()

    assert window.session_state is snapshot
    assert field_review(window, MetadataField.TITLE).decision is FieldDecisionKind.CLEAR
    assert window.included_file_ids == frozenset({file_id})
    assert window.processing_executor.pending == []


def test_enter_in_review_does_not_accept_a_decision_or_open_apply(qtbot, tmp_path):
    window = review_window(qtbot, tmp_path)
    window.show()
    window.set_included_file_ids(frozenset(window.selected_file_ids()))
    snapshot = window.session_state
    unexpected_dialogs = []

    def dismiss_unexpected_dialog():
        dialog = QApplication.activeModalWidget()

        if dialog is not None:
            unexpected_dialogs.append(dialog)
            dialog.reject()

    # If Enter accidentally activates Apply, dismiss its modal summary so the
    # regression reports the error instead of leaving the test blocked.
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(dismiss_unexpected_dialog)
    timer.start(0)

    try:
        qtbot.keyClick(window.diff_table_view, Qt.Key.Key_Return)
    finally:
        timer.stop()

    assert not unexpected_dialogs
    assert window.session_state is snapshot
    assert window.processing_executor.pending == []


def test_review_snapshot_refresh_keeps_window_visibility_and_highlight(qtbot):
    window = populated_tracks_window(qtbot)
    window.file_table_view.selectRow(3)
    window.open_metadata_review()
    old = window.session_state.groups[0]
    window.set_session_state(replace(window.session_state, groups=(replace(old, language_override="ja"),)))

    assert window.selected_file_ids() == ("track-4",)
    assert window.diff_table_view.isVisible()

    window.review_window.close()
    window.set_session_state(replace(window.session_state, groups=(old,)))
    assert not window.diff_table_view.isVisible()


def test_group_and_included_review_scopes_work_without_highlight(qtbot):
    window = populated_tracks_window(qtbot)
    window.review_scope_combo.setCurrentIndex(window.review_scope_combo.findData("group"))
    window.open_metadata_review()

    assert not window.selected_file_ids()
    assert window.diff_table_view.isVisible()
    assert "27" in window.review_window.target_label.text()

    window.set_included_file_ids(frozenset({"track-3"}))
    window.review_scope_combo.setCurrentIndex(window.review_scope_combo.findData("included"))
    assert window.diff_table_view.isVisible()
    assert window.review_target_file_ids() == ("track-3",)
    window.set_included_file_ids(frozenset())
    assert window.review_window.isVisible()
    # An empty Included scope must leave the route back to local review open.
    assert window.review_scope_combo.isVisible()
    window.file_table_view.selectRow(1)
    window.review_scope_combo.setCurrentIndex(window.review_scope_combo.findData("selected"))
    assert window.diff_table_view.isVisible()
    assert window.review_target_file_ids() == ("track-2",)


def test_album_width_stays_near_default_when_window_grows(qtbot):
    window = populated_tracks_window(qtbot)
    before = window.main_splitter.sizes()[0]
    window.resize(1500, 820)
    qtbot.wait(10)

    assert abs(window.main_splitter.sizes()[0] - before) <= 2


def test_compact_windows_keep_tracks_and_review_actions_accessible(qtbot):
    window = populated_tracks_window(qtbot)
    window.file_table_view.selectRow(0)
    window.open_metadata_review()
    window.resize(960, 700)
    window.review_window.resize(800, 480)
    qtbot.wait(10)

    assert window.width() == 960
    assert window.height() == 700
    assert window.file_table_view.viewport().height() >= 8 * window.file_table_view.rowHeight(0)
    assert window.review_window.width() <= 800
    assert window.review_window.height() <= 480
    scroll = window.review_window.scroll_area
    scroll.ensureWidgetVisible(window.rename_previews_button)
    qtbot.wait(10)
    position = window.rename_previews_button.mapTo(scroll.viewport(), window.rename_previews_button.rect().center())
    assert scroll.viewport().rect().contains(position)


def test_reset_layout_restores_review_window_size_without_reopening_it(qtbot):
    window = populated_tracks_window(qtbot)
    window.open_metadata_review()
    initial_size = window.review_window.size()
    window.review_window.resize(800, 480)
    window.diff_table_view.setColumnWidth(2, 250)
    window.review_window.close()
    window.reset_layout()

    assert not window.review_window.isVisible()
    window.open_metadata_review()
    assert window.review_window.size() == initial_size
    assert window.diff_table_view.columnWidth(2) != 250


def test_reset_layout_keeps_a_closed_maximised_review_window_hidden(qtbot):
    window = populated_tracks_window(qtbot)
    window.open_metadata_review()
    window.review_window.showMaximized()
    qtbot.wait(10)
    window.review_window.close()
    window.reset_layout()

    assert not window.review_window.isVisible()
    window.open_metadata_review()
    assert not window.review_window.isMaximized()


def test_closing_the_main_window_hides_its_open_review_window(qtbot):
    window = populated_tracks_window(qtbot)
    window.file_table_view.selectRow(0)
    window.open_metadata_review()
    window.close()

    assert not window.isVisible()
    assert not window.review_window.isVisible()


def test_lookup_opens_review_with_no_highlight_and_keeps_it_open(qtbot, tmp_path):
    window, executor, _ = lookup_window(qtbot, tmp_path)
    window.show()
    assert not window.selected_file_ids()
    assert not window.diff_table_view.isVisible()
    qtbot.mouseClick(window.find_selected_button, Qt.MouseButton.LeftButton)

    assert window.diff_table_view.isVisible()
    assert window.review_target_file_ids()
    assert not window.included_file_ids

    with qtbot.waitSignal(window.operation_bridge.completed):
        executor.run_next()

    assert window.diff_table_view.isVisible()


def test_review_can_close_and_reopen_during_lookup_without_reenabling_edits(qtbot, tmp_path):
    window, executor, _ = lookup_window(qtbot, tmp_path)
    window.show()
    window.find_selected_button.click()
    window.diff_table_view.selectRow(0)
    active_operation = window.session_state.active_operation
    assert active_operation is not None
    assert not window.manual_value_button.isEnabled()
    assert not window.apply_rename_button.isEnabled()
    assert not window.review_scope_combo.isEnabled()

    window.review_window.close()
    assert window.session_state.active_operation is active_operation
    assert len(executor.pending) == 1
    window.open_review_button.click()
    assert window.review_window.isVisible()
    assert not window.manual_value_button.isEnabled()
    assert not window.apply_rename_button.isEnabled()
    assert not window.review_scope_combo.isEnabled()

    # Finishing a job must respect an explicit close, while restoring eligibility.
    window.review_window.close()
    with qtbot.waitSignal(window.operation_bridge.completed):
        executor.run_next()

    assert not window.review_window.isVisible()
    assert window.session_state.active_operation is None
    assert window.review_scope_combo.isEnabled()


def _candidate_dialog(qtbot, tmp_path):
    window, executor, _ = lookup_window(qtbot, tmp_path)
    qtbot.mouseClick(window.find_selected_button, Qt.MouseButton.LeftButton)

    with qtbot.waitSignal(window.operation_bridge.completed):
        executor.run_next()

    return window.lookup_controller.candidate_dialog


# Sorting changes the visible row order and one gesture can emit two Qt signals;
# assert one chosen identity to catch both wrong-candidate and duplicate dispatch.
def test_sorted_candidate_activation_uses_stable_identity_once(qtbot, tmp_path):
    dialog = _candidate_dialog(qtbot, tmp_path)
    expected = dialog._entries[-1].identity
    model = dialog.table.model()
    # Deliberately reorder the source rows. The command must follow the record
    # stored on the cell, rather than use that cell's new visible row number.
    row = model.takeRow(model.rowCount() - 1)
    model.insertRow(0, row)
    dialog.table.selectRow(0)
    chosen = []
    dialog.candidate_selected.connect(chosen.append)
    index = model.index(0, 0)
    dialog.table.doubleClicked.emit(index)
    dialog.table.activated.emit(index)

    assert chosen == [expected]


def test_enter_chooses_current_candidate_and_blank_selection_does_not(qtbot, tmp_path):
    dialog = _candidate_dialog(qtbot, tmp_path)
    chosen = []
    dialog.candidate_selected.connect(chosen.append)
    dialog.table.clearSelection()
    qtbot.keyClick(dialog.table, Qt.Key.Key_Return)
    assert chosen == []

    dialog.table.selectRow(0)
    qtbot.keyClick(dialog.table, Qt.Key.Key_Return)
    assert chosen == [dialog._entries[0].identity]
