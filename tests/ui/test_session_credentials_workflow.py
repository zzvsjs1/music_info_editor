"""Draft proxy credentials cannot silently replace the active RAM session."""

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QLineEdit

from metadata_polisher.application.lookup import LookupService
from metadata_polisher.bootstrap import create_application
from metadata_polisher.infrastructure.settings import AppSettings
from metadata_polisher.providers.coordinator import ProviderCoordinator
from metadata_polisher.ui.dialogs.settings_dialog import SettingsDialog
from tests.ui.test_scan_workflow import ControlledExecutor
from tests.unit.application.test_lookup_service import LookupFakeProvider
from tests.unit.providers.test_network_session import PASSWORD, USERNAME, credentials_type


def test_network_credentials_are_masked_and_drafts_do_not_mutate_the_snapshot(qtbot):
    credentials = credentials_type()()
    credentials.set_proxy("existing-user", "existing-password")
    snapshot = credentials.snapshot()
    dialog = SettingsDialog(AppSettings(), credentials=snapshot)
    qtbot.addWidget(dialog)

    assert dialog.proxy_password_edit.echoMode() is QLineEdit.EchoMode.Password
    dialog.proxy_username_edit.setText(USERNAME)
    dialog.proxy_password_edit.setText(PASSWORD)

    assert dialog.session_proxy_auth() == (USERNAME, PASSWORD)
    assert credentials.snapshot().proxy_auth == ("existing-user", "existing-password")
    assert snapshot.proxy_auth == ("existing-user", "existing-password")
    assert "password" not in repr(dialog.settings()).casefold()
    dialog.reject()
    assert credentials.snapshot().proxy_auth == ("existing-user", "existing-password")


@pytest.mark.parametrize("action", ["cancel", "save_session", "forget"])
def test_only_explicit_session_actions_change_active_credentials(qtbot, tmp_path, monkeypatch, action):
    _, window = create_application([], executor=ControlledExecutor(), settings_file=tmp_path / "settings.json")
    qtbot.addWidget(window)
    credentials = window.lookup_controller.session_credentials
    credentials.set_proxy("existing-user", "existing-password")

    def edit(dialog):
        dialog.proxy_username_edit.setText(USERNAME)
        dialog.proxy_password_edit.setText(PASSWORD)

        if action == "save_session":
            qtbot.mouseClick(dialog.save_session_login_button, Qt.MouseButton.LeftButton)
        elif action == "forget":
            qtbot.mouseClick(dialog.forget_session_login_button, Qt.MouseButton.LeftButton)

        return QDialog.DialogCode.Rejected

    monkeypatch.setattr(SettingsDialog, "exec", edit)
    qtbot.mouseClick(window.settings_button, Qt.MouseButton.LeftButton)
    expected = (USERNAME, PASSWORD) if action == "save_session" else None if action == "forget" else (
        "existing-user", "existing-password",
    )

    assert credentials.snapshot().proxy_auth == expected

    for path in tmp_path.rglob("*"):
        if path.is_file():
            contents = path.read_bytes()
            assert USERNAME.encode() not in contents
            assert PASSWORD.encode() not in contents


# Testing unsaved credentials is a request-scoped action. Compare the active
# snapshot afterwards so a successful test cannot silently activate that login.
def test_connection_test_uses_draft_proxy_auth_without_activating_it(qtbot, tmp_path, monkeypatch):
    executor = ControlledExecutor()
    _, window = create_application([], executor=executor, settings_file=tmp_path / "settings.json")
    qtbot.addWidget(window)
    credentials = window.lookup_controller.session_credentials
    credentials.set_proxy("existing-user", "existing-password")
    captured = []
    provider = LookupFakeProvider("vgmdb", ())

    def service(settings, *, network, credentials):
        captured.append((settings.selected_provider_id, network, credentials))
        return LookupService(ProviderCoordinator((provider,)))

    monkeypatch.setattr(window.lookup_controller, "connection_service", service)

    def edit(dialog):
        dialog.provider_combo.setCurrentIndex(dialog.provider_combo.findData("vgmdb"))
        dialog.network_mode_combo.setCurrentIndex(dialog.network_mode_combo.findData("manual_proxy"))
        dialog.proxy_host_edit.setText("proxy.invalid")
        dialog.proxy_port_spin.setValue(3128)
        dialog.proxy_username_edit.setText(USERNAME)
        dialog.proxy_password_edit.setText(PASSWORD)
        qtbot.mouseClick(dialog.test_provider_button, Qt.MouseButton.LeftButton)

        with qtbot.waitSignal(window.operation_bridge.completed):
            executor.run_next()

        assert captured[0][0] == "vgmdb"
        assert captured[0][1].mode == "manual_proxy"
        assert captured[0][2].proxy_auth == (USERNAME, PASSWORD)
        assert credentials.snapshot().proxy_auth == ("existing-user", "existing-password")
        return QDialog.DialogCode.Rejected

    monkeypatch.setattr(SettingsDialog, "exec", edit)
    qtbot.mouseClick(window.settings_button, Qt.MouseButton.LeftButton)

    assert credentials.snapshot().proxy_auth == ("existing-user", "existing-password")
