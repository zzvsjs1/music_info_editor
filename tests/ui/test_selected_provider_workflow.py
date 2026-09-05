"""Provider preference changes remain local and preserve reviewed evidence."""

import wave

import httpx
import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox, QDialog, QFileDialog

from metadata_polisher.application.lookup import LookupService
from metadata_polisher.bootstrap import create_application
from metadata_polisher.infrastructure.settings import AppSettings, ProvidersSettings, load_settings, save_settings
from metadata_polisher.providers.coordinator import ProviderCoordinator
from metadata_polisher.ui.dialogs.settings_dialog import SettingsDialog
from tests.ui.test_review_workflow import review_window
from tests.ui.test_scan_workflow import ControlledExecutor
from tests.unit.application.test_lookup_service import LookupFakeProvider
from tests.unit.session.test_lookup_editing import make_selected_session


def provider_selector(dialog: SettingsDialog) -> QComboBox:
    selectors = [
        combo for combo in dialog.findChildren(QComboBox)
        if combo.findData("musicbrainz_direct") >= 0 and combo.findData("vgmdb") >= 0
    ]

    assert len(selectors) == 1, "Settings must expose one implemented-provider selector."
    return selectors[0]


def forbid_http(monkeypatch) -> list[httpx.Request]:
    # Fail at the shared HTTP boundary so local Settings/scan operations cannot
    # silently probe any provider, including one other than the visible selection.
    requests: list[httpx.Request] = []

    def record(_client, request, *args, **kwargs):
        requests.append(request)
        raise AssertionError("This local action must not send an HTTP request.")

    monkeypatch.setattr(httpx.Client, "send", record)
    return requests


@pytest.mark.parametrize("selected", ["musicbrainz_direct", "vgmdb", None])
def test_opening_settings_and_changing_selection_is_local(qtbot, monkeypatch, selected):
    requests = forbid_http(monkeypatch)
    original = AppSettings()
    dialog = SettingsDialog(original)
    qtbot.addWidget(dialog)
    dialog.show()
    selector = provider_selector(dialog)
    index = selector.findData(selected)

    assert index >= 0
    selector.setCurrentIndex(index)

    assert dialog.settings().providers.selected_provider_id == selected
    assert original.providers.selected_provider_id == "musicbrainz_direct"
    assert requests == []
    dialog.reject()


def test_provider_preference_save_retains_candidates_mapping_and_review(qtbot, tmp_path, monkeypatch):
    requests = forbid_http(monkeypatch)
    window = review_window(qtbot, tmp_path)
    original_state = window.session_state

    def change_provider(dialog):
        selector = provider_selector(dialog)
        selector.setCurrentIndex(selector.findData("vgmdb"))
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(SettingsDialog, "exec", change_provider)
    qtbot.mouseClick(window.settings_button, Qt.MouseButton.LeftButton)

    assert window.library_controller.settings.providers.selected_provider_id == "vgmdb"
    assert load_settings(tmp_path / "settings.json").settings.providers.selected_provider_id == "vgmdb"
    assert window.session_state.groups == original_state.groups
    assert window.session_state.groups[0].selected_release is original_state.groups[0].selected_release
    assert window.session_state.groups[0].reviewed_files == original_state.groups[0].reviewed_files
    assert requests == []


# An unknown ID must remain a visible configuration problem; presenting a valid
# fallback would conceal an unauthorised provider change on the next request.
def test_unavailable_stored_provider_is_visible_without_silent_selection(qtbot, monkeypatch):
    requests = forbid_http(monkeypatch)
    original = AppSettings(providers=ProvidersSettings(selected_provider_id="unavailable-adapter"))
    dialog = SettingsDialog(original)
    qtbot.addWidget(dialog)
    selector = provider_selector(dialog)

    assert selector.currentData() == "unavailable-adapter"
    assert "unavailable" in selector.currentText().casefold()
    assert dialog.settings().providers.selected_provider_id == "unavailable-adapter"
    assert requests == []


def test_startup_and_local_only_scan_send_no_provider_requests(qtbot, tmp_path, monkeypatch):
    requests = forbid_http(monkeypatch)
    settings_file = tmp_path / "settings.json"
    save_settings(settings_file, AppSettings(providers=ProvidersSettings(selected_provider_id=None)))
    root = tmp_path / "library"
    root.mkdir()

    # Generated silence is disposable scan input. No original library media is
    # involved, and the local format reader must work without a chosen provider.
    with wave.open(str(root / "01. Theme.wav"), "wb") as audio:
        audio.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
        audio.writeframes(b"\x00\x00" * 80)

    executor = ControlledExecutor()
    _, window = create_application([], executor=executor, settings_file=settings_file)
    qtbot.addWidget(window)
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *args: str(root))

    assert requests == []
    qtbot.mouseClick(window.browse_button, Qt.MouseButton.LeftButton)

    with qtbot.waitSignal(window.operation_bridge.completed):
        executor.run_next()

    assert len(window.session_state.groups) == 1
    assert window.session_state.root == root
    assert requests == []


@pytest.mark.parametrize("cancel", [False, True])
def test_explicit_provider_test_has_visible_progress_and_preserves_review(
    qtbot, tmp_path, monkeypatch, cancel,
):
    executor = ControlledExecutor()
    _, window = create_application([], executor=executor, settings_file=tmp_path / "settings.json")
    qtbot.addWidget(window)
    window.set_session_state(make_selected_session())
    original_groups = window.session_state.groups
    provider = LookupFakeProvider("vgmdb", ())
    captured = []

    def connection_service(settings, **_configuration):
        captured.append(settings.selected_provider_id)
        return LookupService(ProviderCoordinator((provider,)))

    monkeypatch.setattr(window.lookup_controller, "connection_service", connection_service)

    def test_in_dialog(dialog):
        selector = provider_selector(dialog)
        selector.setCurrentIndex(selector.findData("vgmdb"))
        assert provider.search_calls == []
        qtbot.mouseClick(dialog.test_provider_button, Qt.MouseButton.LeftButton)
        assert captured == ["vgmdb"]
        assert len(executor.pending) == 1
        assert not selector.isEnabled()
        assert not dialog.provider_test_progress.isHidden()
        assert "Testing" in dialog.provider_test_status.text()

        if cancel:
            qtbot.mouseClick(dialog.cancel_provider_test_button, Qt.MouseButton.LeftButton)

            with qtbot.waitSignal(window.operation_bridge.cancelled):
                executor.run_next()

            assert "cancelled" in dialog.provider_test_status.text().casefold()
            assert provider.search_calls == []
        else:
            with qtbot.waitSignal(window.operation_bridge.completed):
                executor.run_next()

            assert "passed" in dialog.provider_test_status.text()
            assert len(provider.search_calls) == 1

        assert selector.isEnabled()
        assert dialog.provider_test_progress.isHidden()
        assert window.session_state.active_operation is None
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(SettingsDialog, "exec", test_in_dialog)
    qtbot.mouseClick(window.settings_button, Qt.MouseButton.LeftButton)

    assert window.session_state.groups == original_groups
    assert window.library_controller.settings.providers.selected_provider_id == "vgmdb"
