"""Regression coverage for intentional mouse and keyboard command entry points."""

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication

from metadata_polisher.domain.metadata import MetadataField
from tests.ui.test_file_selection_actions import library_session
from tests.ui.test_review_workflow import review_window
from tests.unit.session.test_batch_review import make_batch_session


def test_file_activation_opens_review_for_highlighted_files_only(qtbot, tmp_path):
    window = review_window(qtbot, tmp_path, library_session())
    window.review_window.close()
    window.review_scope_combo.setCurrentIndex(window.review_scope_combo.findData("library"))
    window.show()
    view = window.file_table_view
    point = view.visualRect(window.file_model.index(0, 2)).center()
    qtbot.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=point)
    qtbot.mouseDClick(view.viewport(), Qt.MouseButton.LeftButton, pos=point)

    assert window.review_window.isVisible()
    assert window.review_scope_combo.currentData() == "selected"
    assert len(window.review_target_file_ids()) == 1
    assert not window.included_file_ids
    assert window.processing_executor.pending == []

    window.review_window.close()
    qtbot.keyClick(view, Qt.Key.Key_Return)
    assert window.review_window.isVisible()


def test_next_previous_preserve_field_selection_and_file_identity(qtbot, tmp_path):
    window = review_window(qtbot, tmp_path, make_batch_session())
    first = window.selected_file_ids()
    assert window.selected_fields() == (MetadataField.TITLE,)
    assert not window.previous_file_button.isEnabled()
    window.next_file_button.click()

    assert window.selected_fields() == (MetadataField.TITLE,)
    assert window.manual_value_button.isEnabled()
    assert window.selected_file_ids() != first
    assert "02.flac" in window.review_window.target_label.text()
    window.previous_file_button.click()
    assert window.selected_file_ids() == first

    window.select_all_files_button.click()
    assert not window.next_file_button.isEnabled()
    assert not window.previous_file_button.isEnabled()


def test_f2_and_final_double_click_open_one_manual_editor(qtbot, tmp_path):
    window = review_window(qtbot, tmp_path, make_batch_session())
    opened = []

    def edit():
        dialog = QApplication.activeModalWidget()
        opened.append(dialog.windowTitle())
        dialog.text_edit.setPlainText("Corrected title")
        dialog.accept()

    QTimer.singleShot(0, edit)
    qtbot.keyClick(window.diff_table_view, Qt.Key.Key_F2)
    assert len(opened) == 1
    assert window.diff_model.index(0, 4).data() == "Corrected title"

    point = window.diff_table_view.visualRect(window.diff_model.index(0, 4)).center()
    qtbot.mouseClick(window.diff_table_view.viewport(), Qt.MouseButton.LeftButton, pos=point)
    QTimer.singleShot(0, edit)
    qtbot.mouseDClick(window.diff_table_view.viewport(), Qt.MouseButton.LeftButton, pos=point)
    assert len(opened) == 2
    assert window.processing_executor.pending == []


def test_folder_enter_uses_scan_command(qtbot, tmp_path):
    from metadata_polisher.bootstrap import create_application
    from tests.ui.test_scan_workflow import ControlledExecutor

    _, window = create_application([], executor=ControlledExecutor(), settings_file=tmp_path / "settings.json")
    qtbot.addWidget(window)
    window.root_path_edit.setText(str(tmp_path))
    qtbot.keyClick(window.root_path_edit, Qt.Key.Key_Return)
    assert len(window.processing_executor.pending) == 1


def test_ctrl_z_works_without_highlighted_files(qtbot, tmp_path):
    window = review_window(qtbot, tmp_path, make_batch_session())
    before = window.diff_model.index(0, 4).data()
    window.clear_value_button.click()
    window.clear_file_selection_button.click()
    window.review_window.activateWindow()
    window.diff_table_view.setFocus()
    qtbot.waitUntil(lambda: window.diff_table_view.hasFocus())
    qtbot.keyClick(window.diff_table_view, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    window.file_table_view.selectRow(0)
    assert window.diff_model.index(0, 4).data() == before


# A shortcut may open the shared summary, but it is not itself write consent.
# Cancelling the modal summary must therefore leave the executor queue empty.
def test_ctrl_enter_opens_confirmation_without_writing(qtbot, tmp_path):
    window = review_window(qtbot, tmp_path, make_batch_session())
    window.include_selected_button.click()
    window.review_window.activateWindow()
    window.diff_table_view.setFocus()
    qtbot.waitUntil(lambda: window.diff_table_view.hasFocus())
    opened = []

    def cancel():
        dialog = QApplication.activeModalWidget()
        opened.append(dialog.windowTitle())
        dialog.reject()

    QTimer.singleShot(0, cancel)
    qtbot.keyClick(window.diff_table_view, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
    assert opened == ["Review Apply changes"]
    assert window.processing_executor.pending == []


def test_file_context_review_matches_the_menu_selected_scope(qtbot, tmp_path):
    window = review_window(qtbot, tmp_path, library_session())
    window.review_scope_combo.setCurrentIndex(window.review_scope_combo.findData("library"))
    window.review_window.close()
    window.show()

    def choose_review():
        menu = QApplication.activePopupWidget()
        next(action for action in menu.actions() if action.text().startswith("Metadata review")).trigger()
        menu.close()

    QTimer.singleShot(0, choose_review)
    view = window.file_table_view
    view.customContextMenuRequested.emit(view.visualRect(window.file_model.index(0, 2)).center())
    assert window.review_window.isVisible()
    assert window.review_scope_combo.currentData() == "selected"
    assert len(window.review_target_file_ids()) == 1
