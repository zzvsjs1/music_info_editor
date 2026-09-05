"""Settings are staged in memory and external paths require explicit selection."""

import shutil

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QFileDialog

from metadata_polisher.infrastructure.process import ProcessRunner
from metadata_polisher.infrastructure.settings import (
    AppSettings,
    BackupSettings,
    DiagnosticsSettings,
    GeneralSettings,
    MatchingSettings,
    ProvidersSettings,
    RenameSettings,
    ReportsSettings,
)
from metadata_polisher.ui.dialogs.settings_dialog import SettingsDialog


def configured_settings():
    return AppSettings(
        general=GeneralSettings(last_root_folder=r"D:\Music"),
        rename=RenameSettings(False, "%tracknumber% - %title%", 3, 2),
        matching=MatchingSettings("ja"),
        providers=ProvidersSettings("vgmdb"),
        backup=BackupSettings(True, r"E:\Backups"),
        reports=ReportsSettings(True, r"E:\Reports"),
        diagnostics=DiagnosticsSettings(True),
        external_tools={"existing-helper": r"C:\Tools\existing-helper.exe"},
    )


# Accepting an unchanged dialogue should preserve the entire typed snapshot,
# including preferences that are not represented by an editable control.
def test_existing_settings_round_trip_without_losing_unedited_configuration(qtbot):
    original = configured_settings()
    dialog = SettingsDialog(original)
    qtbot.addWidget(dialog)
    assert not dialog.rename_enabled.isChecked()
    assert dialog.template_edit.text() == original.rename.template
    assert dialog.track_digits_spin.value() == 3
    assert dialog.disc_digits_spin.value() == 2
    assert dialog.preferred_language_edit.text() == "ja"
    assert dialog.provider_combo.currentData() == "vgmdb"
    assert dialog.external_tools_table.rowCount() == 1

    dialog.accept()

    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.settings() == original


def test_edits_return_typed_settings_and_leave_the_input_snapshot_unchanged(qtbot):
    original = configured_settings()
    dialog = SettingsDialog(original)
    qtbot.addWidget(dialog)
    dialog.rename_enabled.setChecked(True)
    dialog.template_edit.setText("%title%")
    dialog.track_digits_spin.setValue(4)
    dialog.disc_digits_spin.setValue(3)
    dialog.preferred_language_edit.setText("en")
    dialog.provider_combo.setCurrentIndex(dialog.provider_combo.findData("musicbrainz_direct"))
    dialog.backup_enabled.setChecked(False)
    dialog.backup_directory_edit.setText(r"E:\New backups")
    dialog.reports_enabled.setChecked(True)
    dialog.reports_directory_edit.clear()
    dialog.detailed_trace_check.setChecked(False)

    dialog.accept()

    changed = dialog.settings()
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert changed.rename == RenameSettings(True, "%title%", 4, 3)
    assert changed.matching == MatchingSettings("en")
    assert changed.providers == ProvidersSettings("musicbrainz_direct")
    assert changed.backup == BackupSettings(False, r"E:\New backups")
    assert changed.reports == ReportsSettings(True, "")
    assert changed.diagnostics == DiagnosticsSettings(False)
    assert changed.general == original.general
    assert changed.external_tools == original.external_tools
    assert original == configured_settings()


@pytest.mark.parametrize("template", ["%unknown%", "[%title%", "%title"])
def test_invalid_rename_template_keeps_settings_editable(qtbot, template):
    dialog = SettingsDialog(AppSettings())
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.template_edit.setText(template)

    dialog.accept()

    assert dialog.result() != QDialog.DialogCode.Accepted
    assert dialog.isVisible()
    assert dialog.error_label.text()

    dialog.template_edit.setText("%title%")
    dialog.accept()

    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.settings().rename.template == "%title%"


def test_enabled_backups_require_a_directory_but_reports_allow_the_application_default(qtbot):
    dialog = SettingsDialog(AppSettings())
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.backup_enabled.setChecked(True)
    dialog.reports_enabled.setChecked(True)

    dialog.accept()

    assert dialog.isVisible()
    assert "backup" in dialog.error_label.text().lower()

    dialog.backup_directory_edit.setText(r"D:\Backups")
    dialog.accept()

    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.settings().reports == ReportsSettings(True, "")


# File pickers stage preferences only. Directory creation belongs to later
# operations that need the path, not to opening or cancelling Settings.
def test_directory_browsing_only_stages_selected_paths_without_creating_directories(qtbot, monkeypatch, tmp_path):
    backup = str(tmp_path / "selected-backups")
    reports = str(tmp_path / "selected-reports")
    selections = iter((backup, reports))
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *_args, **_kwargs: next(selections))
    dialog = SettingsDialog(AppSettings())
    qtbot.addWidget(dialog)
    qtbot.mouseClick(dialog.backup_browse_button, Qt.MouseButton.LeftButton)
    qtbot.mouseClick(dialog.reports_browse_button, Qt.MouseButton.LeftButton)
    dialog.accept()

    assert dialog.settings().backup.directory == backup
    assert dialog.settings().reports.directory == reports
    assert list(tmp_path.iterdir()) == []


def test_external_tool_paths_are_browsed_and_removed_without_searching_or_running_them(qtbot, monkeypatch, tmp_path):
    def unexpected_discovery(*_args, **_kwargs):
        pytest.fail("Settings must not automatically search for or execute external tools")

    monkeypatch.setattr(shutil, "which", unexpected_discovery)
    monkeypatch.setattr(ProcessRunner, "run", unexpected_discovery)
    chosen_path = str(tmp_path / "selected-helper.exe")
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *_args, **_kwargs: (chosen_path, ""))
    original = AppSettings()
    dialog = SettingsDialog(original)
    qtbot.addWidget(dialog)
    assert dialog.external_tools_table.rowCount() == 0
    dialog.tool_name_edit.setText("selected-helper")
    qtbot.mouseClick(dialog.add_tool_button, Qt.MouseButton.LeftButton)

    assert dict(dialog.settings().external_tools) == {"selected-helper": chosen_path}
    assert dialog.external_tools_table.rowCount() == 1

    dialog.external_tools_table.selectRow(0)
    qtbot.mouseClick(dialog.remove_tool_button, Qt.MouseButton.LeftButton)
    dialog.accept()

    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dict(dialog.settings().external_tools) == {}
    assert dict(original.external_tools) == {}
    assert list(tmp_path.iterdir()) == []


def test_external_tool_add_requires_an_explicit_name_before_browsing(qtbot, monkeypatch):
    def unexpected_browse(*_args, **_kwargs):
        pytest.fail("A missing tool name must be corrected before selecting an executable")

    monkeypatch.setattr(QFileDialog, "getOpenFileName", unexpected_browse)
    dialog = SettingsDialog(AppSettings())
    qtbot.addWidget(dialog)
    qtbot.mouseClick(dialog.add_tool_button, Qt.MouseButton.LeftButton)

    assert dialog.external_tools_table.rowCount() == 0
    assert dialog.error_label.text()


def test_cancelled_directory_browse_preserves_the_configured_path(qtbot, monkeypatch):
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *_args, **_kwargs: "")
    original = configured_settings()
    dialog = SettingsDialog(original)
    qtbot.addWidget(dialog)
    qtbot.mouseClick(dialog.backup_browse_button, Qt.MouseButton.LeftButton)

    assert dialog.backup_directory_edit.text() == original.backup.directory
