"""Inspecting full values and help must never accept edits or write inclusion."""

from dataclasses import replace

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from metadata_polisher.bootstrap import create_application
from metadata_polisher.session.state import GroupSelection
from tests.ui.test_review_workflow import focus_review_shortcuts, review_window
from tests.ui.test_scan_workflow import ControlledExecutor
from tests.unit.session.test_review_editing import make_local_session, make_source


def test_full_value_details_are_read_only_and_follow_field_selection(qtbot, tmp_path):
    source = make_source("long.flac")
    title = "長いタイトル / " * 60
    metadata = replace(source.read_result.metadata, title=title)
    source = replace(source, read_result=replace(source.read_result, metadata=metadata))
    window = review_window(qtbot, tmp_path, make_local_session(source))
    snapshot = window.session_state
    window.field_details_button.click()

    assert window.field_details.isVisible()
    assert window.field_details.isReadOnly()
    assert title in window.field_details.toPlainText()
    assert "Existing:" in window.field_details.toPlainText()
    assert "Final:" in window.field_details.toPlainText()
    assert window.field_details.maximumHeight() < 200
    window.diff_table_view.clearSelection()
    assert not window.field_details.toPlainText()
    assert window.session_state is snapshot
    assert not window.included_file_ids
    assert not window.processing_executor.pending


def test_f1_opens_non_modal_help_without_changing_review(qtbot, tmp_path):
    window = review_window(qtbot, tmp_path)
    snapshot = window.session_state
    focus_review_shortcuts(qtbot, window)
    qtbot.keyClick(window.diff_table_view, Qt.Key.Key_F1)
    qtbot.waitUntil(lambda: window.help_dialog is not None and window.help_dialog.isVisible())

    assert not window.help_dialog.isModal()
    assert window.session_state is snapshot
    assert not window.included_file_ids
    assert not window.processing_executor.pending


def test_empty_library_and_field_scopes_explain_the_next_action(qtbot, tmp_path):
    _, window = create_application([], executor=ControlledExecutor(), settings_file=tmp_path / "settings.json")
    qtbot.addWidget(window)
    assert "folder" in window.file_guidance_label.text().lower()
    state = make_local_session(make_source("track.flac"))
    window.set_session_state(replace(state, selection=None))
    assert "album" in window.file_guidance_label.text().lower()
    window.set_session_state(replace(state, selection=GroupSelection("album")))
    window.file_table_view.selectRow(0)
    window.open_metadata_review()
    assert "field" in window.field_guidance_label.text().lower()
    assert not window.included_file_ids


def test_finished_scan_hides_stage_counter_and_describes_scanned_files(qtbot, tmp_path):
    executor = ControlledExecutor()
    _, window = create_application([], executor=executor, settings_file=tmp_path / "settings.json")
    qtbot.addWidget(window)
    window.root_path_edit.setText(str(tmp_path))
    window.rescan_button.click()
    executor.run_next()
    qtbot.waitUntil(lambda: window.session_state.active_operation is None)

    assert "Scan complete" in window.operation_stage_label.text()
    assert "0 files" in window.operation_stage_label.text()
    assert window.operation_progress_bar.isHidden()


def test_final_values_use_spare_width_without_resetting_other_user_columns(qtbot, tmp_path):
    window = review_window(qtbot, tmp_path)
    table = window.diff_table_view
    table.setColumnWidth(2, 213)
    window.review_window.resize(1080, 760)
    QApplication.processEvents()

    final_cell = table.visualRect(window.diff_model.index(0, 4))
    assert abs(final_cell.right() - table.viewport().rect().right()) <= 2
    window.set_session_state(window.session_state)
    assert table.columnWidth(2) == 213
