"""QML settings preserve review state, staged preferences and session secrets."""

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QMetaObject, QObject, Qt, QUrl, Signal
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtTest import QTest

from metadata_polisher.application.provider_connection import ProviderConnectionResult
from metadata_polisher.infrastructure.session_credentials import SessionCredentials
from metadata_polisher.infrastructure.settings import AppSettings, ProvidersSettings, load_settings
from metadata_polisher.session.state import SessionState, begin_operation, finish_operation
from metadata_polisher.ui.quick.settings import QuickSettings


class Bridge(QObject):
    completed = Signal(str, object)
    cancelled = Signal(str)
    failed = Signal(str, object)


class Host(QObject):
    changed = Signal()

    def __init__(self, path):
        super().__init__()
        self.app_settings = AppSettings()
        self.settings_file = path
        self.session_state = SessionState(root=None)
        self.credentials = SessionCredentials()
        self.bridge = Bridge(self)
        self._ids = SimpleNamespace(next_id=lambda prefix: prefix + "-1")
        self.lookupUi = self
        self.status = ""
        self.cancelled = False
        self.retired = 0
        self.invalidated = 0
        self.connection = None
        self.pending = None

    def set_state(self, state):
        self.session_state = state
        self.changed.emit()

    def set_status(self, message):
        self.status = message
        self.changed.emit()

    def connection_service(self, settings, *, network, credentials):
        self.connection = (settings, network, credentials)
        return object()

    def finish_connection_test(self):
        self.retired += 1

    def invalidate_session_credentials(self):
        self.invalidated += 1

    def submit_operation(self, operation_id, kind, group_ids, work, reducer):
        self.session_state = begin_operation(self.session_state, operation_id, kind, group_ids)
        self.pending = (operation_id, work, reducer)
        return True

    def cancelScan(self):
        self.cancelled = True


@pytest.fixture
def settings(qapp, tmp_path):
    if QQuickStyle.name() != "Fusion":
        QQuickStyle.setStyle("Fusion")

    host = Host(tmp_path / "settings.json")
    facade = QuickSettings(host)
    return host, facade


def test_preferences_are_staged_and_cancel_discards_edits(settings):
    host, facade = settings
    original = host.app_settings
    assert facade.open()
    facade.setField("template", "%title%")
    facade.setField("preferredLanguage", "ja")
    assert host.app_settings is original
    assert not host.settings_file.exists()
    assert facade.reject()
    assert not facade.opened
    assert facade.open()
    assert facade.draft["template"] == original.rename.template


@pytest.mark.parametrize(("name", "value"), [
    ("trackDigits", True),
    ("proxyPort", "8080"),
    ("backupEnabled", 1),
    ("template", None),
    ("providerId", 1),
    ("unknownField", "ignored"),
])
def test_settings_boundary_rejects_values_with_the_wrong_declared_type(settings, name, value):
    host, facade = settings
    assert facade.open()
    original = facade.draft
    facade.setField(name, value)

    assert facade.draft == original
    assert not host.settings_file.exists()


def test_qml_settings_map_is_detached_and_nullable_provider_edits_remain_valid(settings):
    _host, facade = settings
    assert facade.open()
    exported = facade.draft
    exported["template"] = "Do not publish this map mutation"
    assert facade.draft["template"] != exported["template"]

    facade.setField("providerId", None)
    assert facade.draft["providerId"] is None
    facade.setField("providerId", "vgmdb")
    assert facade.draft["providerId"] == "vgmdb"


@pytest.mark.parametrize(("key", "value", "message"), [
    ("template", "%unknown%", "Unknown"),
    ("preferredLanguage", " ", "preferred language"),
    ("backupEnabled", True, "backup directory"),
    ("trackDigits", 11, "10"),
])
def test_invalid_settings_remain_open_and_leave_state_unchanged(settings, key, value, message):
    host, facade = settings
    original = host.session_state
    facade.open()
    facade.setField(key, value)
    assert not facade.save()
    assert facade.opened
    field = "backupDirectory" if key == "backupEnabled" else key
    assert message.lower() in facade.fieldErrors[field].lower()
    assert facade.error == ""
    assert host.session_state is original
    assert not host.settings_file.exists()


def test_save_reports_all_field_errors_and_preserves_the_invalid_draft(settings):
    host, facade = settings
    facade.open()
    facade.setField("template", "%unknown%")
    facade.setField("backupEnabled", True)
    facade.setField("networkMode", "manual_proxy")
    facade.setField("proxyHost", "http://invalid-host")
    facade.setField("proxyPort", 0)
    original_settings, original_state = host.app_settings, host.session_state

    assert not facade.save()
    assert set(facade.fieldErrors) == {"template", "backupDirectory", "proxyHost", "proxyPort"}
    assert facade.error == ""
    assert facade.draft["template"] == "%unknown%"
    assert facade.draft["proxyHost"] == "http://invalid-host"
    assert host.app_settings is original_settings
    assert host.session_state is original_state
    assert not host.settings_file.exists()


def test_editing_one_invalid_field_revalidates_only_that_field(settings):
    _host, facade = settings
    facade.open()
    facade.setField("template", "%unknown%")
    facade.setField("backupEnabled", True)
    assert not facade.save()
    backup_error = facade.fieldErrors["backupDirectory"]

    facade.setField("template", "%still_unknown%")
    assert "template" in facade.fieldErrors
    assert facade.fieldErrors["backupDirectory"] == backup_error

    facade.setField("template", "%title%")
    assert facade.fieldErrors == {"backupDirectory": backup_error}


def test_field_validation_waits_for_blur_and_rechecks_conditional_errors(settings):
    _host, facade = settings
    facade.open()
    facade.setField("template", "%unknown%")
    assert facade.fieldErrors == {}
    facade.validateField("template")
    assert "template" in facade.fieldErrors

    facade.setField("backupEnabled", True)
    facade.setField("networkMode", "manual_proxy")
    assert not facade.save()
    assert {"backupDirectory", "proxyHost"} <= set(facade.fieldErrors)

    facade.setField("backupEnabled", False)
    facade.setField("networkMode", "direct")
    assert set(facade.fieldErrors) == {"template"}


def test_field_edits_do_not_hide_an_atomic_save_error(settings, monkeypatch):
    _host, facade = settings
    facade.open()

    def fail_save(*args):
        raise PermissionError("Read-only settings location")

    monkeypatch.setattr("metadata_polisher.ui.quick.settings.save_settings", fail_save)
    assert not facade.save()
    operation_error = facade.error
    facade.setField("template", "%title%")
    assert facade.error == operation_error
    assert facade.fieldErrors == {}


@pytest.mark.parametrize("name,path", (("Encoder", "encoder.exe"), ("", "")))
def test_tool_edits_do_not_hide_an_atomic_save_error(settings, monkeypatch, name, path):
    _host, facade = settings
    facade.open()

    def fail_save(*args):
        raise PermissionError("Read-only settings location")

    monkeypatch.setattr("metadata_polisher.ui.quick.settings.save_settings", fail_save)
    assert not facade.save()
    operation_error = facade.error
    facade.addTool(name, path)
    assert facade.error == operation_error


def test_atomic_save_failure_does_not_publish_prepared_preferences(settings, monkeypatch):
    host, facade = settings
    original_state, original_settings = host.session_state, host.app_settings
    facade.open()
    facade.setField("template", "%title%")

    def fail_save(*args):
        raise PermissionError("Read-only settings location")

    monkeypatch.setattr("metadata_polisher.ui.quick.settings.save_settings", fail_save)
    assert not facade.save()
    assert host.session_state is original_state
    assert host.app_settings is original_settings
    assert "could not be saved" in facade.error
    assert facade.opened


@pytest.mark.parametrize("change", ["state", "settings", "file"])
def test_stale_editor_does_not_replace_newer_state_or_disk(settings, change):
    host, facade = settings
    facade.open()
    facade.setField("template", "%title%")

    if change == "state":
        host.session_state = replace(host.session_state, revision=1)
    elif change == "settings":
        host.app_settings = replace(host.app_settings, providers=ProvidersSettings(None))
    else:
        host.settings_file.write_text("externally changed", encoding="utf-8")

    current_state, current_settings = host.session_state, host.app_settings
    assert not facade.save()
    assert host.session_state is current_state
    assert host.app_settings is current_settings

    if change == "file":
        assert host.settings_file.read_text(encoding="utf-8") == "externally changed"


def test_success_saves_typed_preferences_and_preserves_unedited_sections(settings):
    host, facade = settings
    facade.open()
    facade.setField("template", "%title%")
    facade.setField("providerId", None)
    facade.addTool("Encoder", "encoder.exe")
    assert facade.save()
    assert not facade.opened
    saved = load_settings(host.settings_file).settings
    assert saved == host.app_settings
    assert saved.rename.template == "%title%"
    assert saved.providers.selected_provider_id is None
    assert saved.external_tools == {"Encoder": "encoder.exe"}
    assert saved.ui == AppSettings().ui


def test_credentials_require_explicit_session_save_and_are_never_persisted(settings):
    host, facade = settings
    host.credentials.set_proxy("active", "original-secret")
    facade.open()
    facade.setCredentials("draft", "draft-secret")
    assert host.credentials.snapshot().proxy_auth == ("active", "original-secret")
    assert facade.save()
    assert "secret" not in host.settings_file.read_text(encoding="utf-8")
    assert facade.username == facade.password == ""
    assert host.credentials.snapshot().proxy_auth == ("active", "original-secret")

    facade.open()
    facade.setCredentials("saved", "session-secret")
    facade.saveSessionLogin()
    assert host.credentials.snapshot().proxy_auth == ("saved", "session-secret")
    assert host.invalidated == 1
    facade.forgetSessionLogin()
    assert host.credentials.snapshot().proxy_auth is None
    assert facade.username == facade.password == ""
    assert host.invalidated == 2


def test_provider_test_captures_drafts_and_cancel_waits_for_terminal_signal(settings):
    host, facade = settings
    host.credentials.set_proxy("active", "active-secret")
    facade.open()
    facade.setField("networkMode", "manual_proxy")
    facade.setField("proxyHost", "localhost")
    facade.setCredentials("draft", "draft-secret")
    assert facade.testProvider()
    assert facade.testRunning
    assert host.connection[2].proxy_auth == ("draft", "draft-secret")
    assert host.credentials.snapshot().proxy_auth == ("active", "active-secret")
    assert not facade.reject()
    assert host.cancelled
    assert facade.opened
    operation_id = host.pending[0]
    host.session_state = finish_operation(host.session_state, operation_id)
    host.bridge.cancelled.emit(operation_id)
    assert not facade.testRunning
    assert host.retired == 1
    assert facade.save()


def test_provider_result_updates_only_the_matching_test_and_accepts_its_new_state(settings):
    host, facade = settings
    facade.open()
    assert facade.testProvider()
    operation_id = host.pending[0]
    host.bridge.completed.emit("unrelated", ProviderConnectionResult("unrelated", "vgmdb"))
    assert facade.testRunning
    host.session_state = finish_operation(host.session_state, operation_id)
    host.bridge.completed.emit(operation_id, ProviderConnectionResult(operation_id, "musicbrainz_direct"))
    assert "passed" in facade.testStatus
    assert host.retired == 1
    assert facade.save()


def test_unavailable_provider_is_preserved_for_explicit_correction(settings):
    host, facade = settings
    host.app_settings = replace(host.app_settings, providers=ProvidersSettings("future-provider"))
    facade.open()
    assert facade.draft["providerId"] == "future-provider"
    assert facade.providerOptions[-1]["label"] == "Unavailable stored provider"
    assert not facade.testEnabled
    assert facade.save()
    assert host.app_settings.providers.selected_provider_id == "future-provider"


def test_template_completion_preserves_unicode_offsets_and_surrounding_fields(settings):
    _host, facade = settings
    completion = facade.templateCompletion("😀 %tit% [%discnumber%.]", 7)
    assert completion["start"] == 3
    assert completion["end"] == 8
    assert completion["options"] == ["%title%"]
    assert facade.templateCompletion("literal text", 5) == {}


def test_settings_window_loads_closed_and_contains_all_original_tabs(settings, qtbot):
    _host, facade = settings
    engine = QQmlApplicationEngine()
    messages = []
    engine.warnings.connect(lambda errors: messages.extend(error.toString() for error in errors))
    engine.setInitialProperties({"settings": facade})
    qml = Path(__file__).parents[2] / "src/metadata_polisher/ui/quick/qml/SettingsWindow.qml"
    engine.load(QUrl.fromLocalFile(str(qml)))

    try:
        assert engine.rootObjects(), messages
        window = engine.rootObjects()[0]
        assert not window.property("visible")
        facade.open()
        qtbot.waitUntil(lambda: window.property("visible"))

        for name in ("renamingTab", "providersTab", "networkTab", "outputTab", "externalToolsTab"):
            assert window.findChild(QObject, name) is not None

        assert not messages
        facade.reject()
    finally:
        engine.deleteLater()


def test_escape_dismisses_template_suggestions_before_settings(settings, qtbot):
    _host, facade = settings
    engine = QQmlApplicationEngine()
    engine.setInitialProperties({"settings": facade})
    qml = Path(__file__).parents[2] / "src/metadata_polisher/ui/quick/qml/SettingsWindow.qml"
    engine.load(QUrl.fromLocalFile(str(qml)))

    try:
        window = engine.rootObjects()[0]
        facade.open()
        window.requestActivate()
        qtbot.waitUntil(window.isActive)
        editor = window.findChild(QObject, "settingsTemplate")
        editor.forceActiveFocus()
        editor.setProperty("text", "%tit")
        editor.setProperty("cursorPosition", 4)
        assert QMetaObject.invokeMethod(editor, "refreshCompletions")
        suggestions = window.findChild(QObject, "templateSuggestions")
        assert suggestions is not None
        qtbot.waitUntil(lambda: suggestions.property("opened"))
        QTest.keyClick(window, Qt.Key.Key_Escape)
        qtbot.waitUntil(lambda: not suggestions.property("opened"))
        assert facade.opened
        QTest.keyClick(window, Qt.Key.Key_Escape)
        qtbot.waitUntil(lambda: not facade.opened)
    finally:
        facade.reject()
        engine.deleteLater()


@pytest.mark.parametrize("key", [Qt.Key.Key_Return, Qt.Key.Key_Tab])
def test_template_editor_accepts_a_token_without_replacing_neighbouring_unicode_text(settings, qtbot, key):
    host, facade = settings
    original = host.app_settings
    engine = QQmlApplicationEngine()
    engine.setInitialProperties({"settings": facade})
    qml = Path(__file__).parents[2] / "src/metadata_polisher/ui/quick/qml/SettingsWindow.qml"
    engine.load(QUrl.fromLocalFile(str(qml)))

    try:
        window = engine.rootObjects()[0]
        facade.open()
        window.requestActivate()
        qtbot.waitUntil(window.isActive)
        editor = window.findChild(QObject, "settingsTemplate")
        editor.forceActiveFocus()
        editor.setProperty("text", "😀 %tit% [%discnumber%.]")
        editor.setProperty("cursorPosition", 7)
        assert QMetaObject.invokeMethod(editor, "refreshCompletions")
        suggestions = window.findChild(QObject, "templateSuggestions")
        qtbot.waitUntil(lambda: suggestions.property("opened"))

        # The emoji occupies two UTF-16 units. Both the text editor and facade
        # must retain the surrounding text while replacing the incomplete token.
        QTest.keyClick(window, key)
        expected = "😀 %title% [%discnumber%.]"
        qtbot.waitUntil(lambda: editor.property("text") == expected)
        assert facade.draft["template"] == expected
        assert not suggestions.property("opened")
        assert facade.opened
        assert host.app_settings is original
    finally:
        facade.reject()
        engine.deleteLater()


def test_evidence_window_retains_zero_reasons_and_copyable_full_details(qapp, qtbot):
    if QQuickStyle.name() != "Fusion":
        QQuickStyle.setStyle("Fusion")

    engine = QQmlApplicationEngine()
    detail = "Retained contradiction. " * 150
    engine.setInitialProperties({"rows": [
        {"reason": "TITLE_MATCH", "contribution": "20", "detail": "Exact title"},
        {"reason": "UNKNOWN_TOTAL", "contribution": "0", "detail": detail},
    ]})
    qml = Path(__file__).parents[2] / "src/metadata_polisher/ui/quick/qml/EvidenceWindow.qml"
    engine.load(QUrl.fromLocalFile(str(qml)))

    try:
        assert engine.rootObjects()
        window = engine.rootObjects()[0]
        initial_height = window.height()
        assert not window.property("visible")
        window.show()
        table = window.findChild(QObject, "matchEvidenceTable")
        assert table.property("count") == 2
        table.setProperty("currentIndex", 1)
        details = window.findChild(QObject, "matchEvidenceDetails")
        assert details.property("text") == detail
        assert details.property("readOnly")
        assert details.property("selectByMouse")
        assert window.height() == initial_height
        window.close()
    finally:
        engine.deleteLater()


@pytest.mark.parametrize("change", ["keyboard", "sorted_insertion"])
def test_external_tool_removal_follows_visible_stable_selection(settings, qtbot, change):
    from PySide6.QtCore import QPointF
    from PySide6.QtQuick import QQuickItem

    from tests.ui.test_quick_window import click_item

    host, facade = settings
    host.app_settings = replace(host.app_settings, external_tools={"Bravo": "b.exe", "Charlie": "c.exe"})
    engine = QQmlApplicationEngine()
    engine.setInitialProperties({"settings": facade})
    qml = Path(__file__).parents[2] / "src/metadata_polisher/ui/quick/qml/SettingsWindow.qml"
    engine.load(QUrl.fromLocalFile(str(qml)))

    try:
        window = engine.rootObjects()[0]
        facade.open()
        window.requestActivate()
        qtbot.waitUntil(window.isActive)
        click_item(window, "externalToolsTab")
        click_item(window, "settingsToolsTable", QPointF(70, 18))
        table = window.findChild(QQuickItem, "settingsToolsTable")
        assert table.property("selectedName") == "Bravo"

        if change == "keyboard":
            table.forceActiveFocus()
            QTest.keyClick(window, Qt.Key.Key_Down)
            assert table.property("currentIndex") == 1
            click_item(window, "removeExternalTool")
            assert facade.tools == [{"name": "Bravo", "path": "b.exe"}]
        else:
            facade.addTool("Alpha", "a.exe")
            qtbot.waitUntil(lambda: table.property("count") == 3)
            assert table.property("currentIndex") == 1
            assert table.property("selectedName") == "Bravo"
            click_item(window, "removeExternalTool")
            assert facade.tools == [{"name": "Alpha", "path": "a.exe"}, {"name": "Charlie", "path": "c.exe"}]
    finally:
        facade.reject()
        engine.deleteLater()
