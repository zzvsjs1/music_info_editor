import pytest
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication

from metadata_polisher.infrastructure.settings import load_settings
from tests.ui.test_review_workflow import review_window


@pytest.mark.parametrize("save_fails", [False, True])
# Exercise both persistence outcomes: the new template's preview may be ready
# in memory, but only a successful save may publish it into the live session.
def test_settings_save_rebuilds_preview_only_after_success(qtbot, tmp_path, monkeypatch, save_fails):
    window = review_window(qtbot, tmp_path)
    original_state = window.session_state
    original_settings = window.library_controller.settings
    shown = []

    if save_fails:
        def deny_save(*args):
            raise PermissionError("Read-only settings location")

        monkeypatch.setattr("metadata_polisher.ui.settings_controller.save_settings", deny_save)

    def edit_settings():
        dialog = QApplication.activeModalWidget()
        shown.append(dialog)
        dialog.template_edit.setText("%title%")
        dialog.provider_combo.setCurrentIndex(dialog.provider_combo.findData("vgmdb"))
        dialog.accept()

    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(edit_settings)
    timer.start(0)

    try:
        qtbot.mouseClick(window.settings_button, Qt.MouseButton.LeftButton)
    finally:
        timer.stop()

    assert shown

    if save_fails:
        assert window.session_state is original_state
        assert window.library_controller.settings is original_settings
        assert "could not be saved" in window.workflow_message_label.text()
        assert not (tmp_path / "settings.json").exists()
    else:
        assert load_settings(tmp_path / "settings.json").settings.rename.template == "%title%"
        assert window.rename_proposed_label.toolTip() == "Proposed filename: 序曲.flac"
        assert window.session_state.groups[0].selected_release is original_state.groups[0].selected_release
        assert (
            window.session_state.groups[0].reviewed_files[0].reviews
            == original_state.groups[0].reviewed_files[0].reviews
        )
