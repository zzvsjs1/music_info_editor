"""Settings forms retain usable targets, spacing and staged keyboard edits."""

from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPointF, Qt, QUrl
from PySide6.QtGui import QFont, QFontMetricsF
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuick import QQuickItem
from PySide6.QtTest import QTest

from metadata_polisher.ui.quick.settings import QuickSettings
from tests.ui.test_quick_settings import Host


@pytest.fixture
def settings_controls_scene(qapp, qtbot, tmp_path):
    # Honour the process style so this scene can exercise both real Windows
    # controls and the offscreen runner's Fusion controls without switching a
    # style after other QML engines have already imported Qt Quick Controls.
    host = Host(tmp_path / "settings.json")
    facade = QuickSettings(host)
    engine = QQmlApplicationEngine()
    messages = []
    engine.warnings.connect(lambda errors: messages.extend(error.toString() for error in errors))
    engine.setInitialProperties({"settings": facade})
    qml = Path(__file__).parents[2] / "src/metadata_polisher/ui/quick/qml/SettingsWindow.qml"
    engine.load(QUrl.fromLocalFile(str(qml)))
    assert engine.rootObjects(), messages
    window = engine.rootObjects()[0]
    facade.open()
    qtbot.waitUntil(window.isVisible)

    try:
        yield window, host, facade
    finally:
        facade.reject()
        window.hide()
        engine.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        assert not messages, messages


def _item(window, name):
    item = window.findChild(QQuickItem, name)
    assert item is not None, name
    return item


def _label_and_control(window, text):
    # Find the actual visual pair so these checks measure the reported spacing,
    # including controls whose names are implementation details of a native style.
    label = next(item for item in window.findChildren(QQuickItem)
                 if item.inherits("QQuickLabel") and item.property("text") == text)
    siblings = label.parentItem().childItems()
    field = siblings[siblings.index(label) + 1]

    if _is_form_control(field):
        return label, field

    # QML aliases can expose a generated control type that has no registered
    # Python converter. Traverse actual items instead, measuring the visible
    # native editor while ignoring a specialised field's hidden default input.
    control = next(item for item in field.findChildren(QQuickItem)
                   if item.isVisible() and _is_form_control(item))
    return label, control


def _inside(window, item):
    top_left = item.mapToScene(QPointF())
    assert top_left.x() >= -1
    assert top_left.x() + item.width() <= window.width() + 1
    assert top_left.y() >= -1
    assert top_left.y() + item.height() <= window.height() + 1


def _is_form_control(item):
    if not any(item.inherits(name) for name in ("QQuickTextField", "QQuickComboBox", "QQuickSpinBox")):
        return False

    # A native selector can contain a TextField for its selected value. That
    # content item sits inside the selector's padding and is not another form
    # target; only its enclosing ComboBox/SpinBox owns the minimum target size.
    parent = item.parentItem()

    while parent is not None:
        if parent.inherits("QQuickComboBox") or parent.inherits("QQuickSpinBox"):
            return False

        parent = parent.parentItem()

    return True


@pytest.mark.parametrize("font_size", [9, 14])
def test_network_rows_share_readable_control_targets_and_gaps(settings_controls_scene, qtbot, font_size):
    window, _host, _facade = settings_controls_scene
    window.setProperty("font", QFont("Segoe UI", font_size))
    window.resize(900, 700)
    _item(window, "settingsTabs").setProperty("currentIndex", 2)
    qtbot.wait(80)
    controls = []

    for text in ("External services route", "HTTP proxy host", "Port",
                 "Session proxy username", "Session proxy password"):
        label, control = _label_and_control(window, text)
        controls.append(control)
        metrics = QFontMetricsF(control.property("font"))
        assert control.height() >= max(36, metrics.height() + 14) - 1, text
        editor = control.property("contentItem")
        # Windows paints the SpinBox frame through its inner TextField. The
        # readable inset therefore belongs to that editor, rather than moving
        # the entire frame inward and separating it from the native arrows.
        padded = editor if (control.inherits("QQuickSpinBox")
                            and editor.inherits("QQuickTextField")) else control
        assert padded.property("leftPadding") >= 12, text
        assert padded.property("rightPadding") >= 12, text
        assert padded.property("topPadding") >= 7, text
        assert padded.property("bottomPadding") >= 7, text
        label_right = label.mapToScene(QPointF(label.width(), 0)).x()
        assert control.mapToScene(QPointF()).x() - label_right >= 8, text

    # Rows within each grid must remain separated after a larger font expands
    # their native controls. The explanatory route text sits between the grids.
    for before, after in ((controls[0], controls[1]), (controls[1], controls[2]),
                          (controls[3], controls[4])):
        bottom = before.mapToScene(QPointF(0, before.height())).y()
        assert after.mapToScene(QPointF()).y() - bottom >= 10

    left_edges = [control.mapToScene(QPointF()).x() for control in controls]
    assert max(left_edges) - min(left_edges) <= 1


def test_every_settings_page_uses_the_shared_single_line_control_size(settings_controls_scene, qtbot):
    window, _host, _facade = settings_controls_scene
    window.setProperty("font", QFont("Segoe UI", 14))
    window.resize(600, 700)
    tabs = _item(window, "settingsTabs")

    for tab_index in range(5):
        tabs.setProperty("currentIndex", tab_index)
        qtbot.wait(30)
        controls = [item for item in window.findChildren(QQuickItem)
                    if item.isVisible() and _is_form_control(item)]
        assert controls, tab_index

        for control in controls:
            assert control.height() >= 36, (tab_index, control.metaObject().className())
            left = control.mapToScene(QPointF()).x()
            assert left >= 0
            assert left + control.width() <= window.width(), tab_index


def test_small_large_font_settings_keeps_footer_outside_scrollable_forms(settings_controls_scene, qtbot):
    window, _host, _facade = settings_controls_scene
    window.setProperty("font", QFont("Segoe UI", 14))
    window.resize(600, 360)
    tabs = _item(window, "settingsTabs")
    pages = _item(window, "settingsPages")

    for tab_index in range(5):
        tabs.setProperty("currentIndex", tab_index)
        qtbot.wait(30)
        assert (window.width(), window.height()) == (600, 360)
        assert pages.height() >= 100
        page_bottom = pages.mapToScene(QPointF(0, pages.height())).y()

        for name in ("saveSettingsButton", "cancelSettingsButton"):
            button = _item(window, name)
            _inside(window, button)
            assert button.mapToScene(QPointF()).y() - page_bottom >= 8


def test_narrow_settings_stacks_labels_above_controls_and_keeps_actions_reachable(settings_controls_scene, qtbot):
    window, _host, _facade = settings_controls_scene
    window.setProperty("font", QFont("Segoe UI", 14))
    window.resize(420, 360)
    _item(window, "settingsTabs").setProperty("currentIndex", 2)
    qtbot.wait(80)
    assert window.width() == 420

    for text in ("External services route", "HTTP proxy host", "Port",
                 "Session proxy username", "Session proxy password"):
        label, control = _label_and_control(window, text)
        label_bottom = label.mapToScene(QPointF(0, label.height())).y()
        assert control.mapToScene(QPointF()).y() >= label_bottom + 3, text
        left = control.mapToScene(QPointF()).x()
        assert left >= 0
        assert left + control.width() <= window.width(), text

    for name in ("saveSettingsButton", "cancelSettingsButton"):
        _inside(window, _item(window, name))


def test_settings_field_errors_are_inline_and_save_focuses_the_first_error(settings_controls_scene, qtbot):
    from tests.ui.test_quick_window import click_item

    window, _host, facade = settings_controls_scene
    facade.setField("template", "%unknown%")
    facade.setField("backupEnabled", True)
    facade.setField("networkMode", "manual_proxy")
    facade.setField("proxyHost", "http://invalid-host")
    _item(window, "settingsTabs").setProperty("currentIndex", 2)
    window.requestActivate()
    qtbot.waitUntil(window.isActive)
    click_item(window, "saveSettingsButton")
    qtbot.waitUntil(lambda: _item(window, "settingsTabs").property("currentIndex") == 0)
    qtbot.waitUntil(lambda: _item(window, "settingsTemplate").hasActiveFocus())

    for name, field in (("settingsTemplate", "template"),
                        ("settingsProxyHost", "proxyHost"),
                        ("settingsBackupDirectory", "backupDirectory")):
        message = _item(window, name + "Error")
        assert message.property("text") == facade.fieldErrors[field]
        assert message.property("readOnly")
        assert message.property("selectByMouse")

    assert facade.opened
    assert facade.error == ""


def test_network_keyboard_edits_keep_disabled_fields_and_draft_credentials(settings_controls_scene, qtbot):
    window, host, facade = settings_controls_scene
    window.resize(950, 700)
    _item(window, "settingsTabs").setProperty("currentIndex", 2)
    window.requestActivate()
    qtbot.waitUntil(window.isActive)
    route = _label_and_control(window, "External services route")[1]
    proxy_host = _label_and_control(window, "HTTP proxy host")[1]
    port = _label_and_control(window, "Port")[1]
    username = _label_and_control(window, "Session proxy username")[1]
    password = _label_and_control(window, "Session proxy password")[1]
    assert not proxy_host.isEnabled()
    assert not username.isEnabled() and not password.isEnabled()
    original_settings = host.app_settings

    route.forceActiveFocus()
    QTest.keyClick(window, Qt.Key.Key_Down)
    qtbot.waitUntil(lambda: facade.draft["networkMode"] == "manual_proxy")
    assert proxy_host.isEnabled() and username.isEnabled() and password.isEnabled()

    # The padded number editor must still pass the native arrow-key actions to
    # SpinBox, including its valueModified signal back to the settings draft.
    original_port = port.property("value")
    port.forceActiveFocus()
    QTest.keyClick(window, Qt.Key.Key_Up)
    assert port.property("value") == facade.draft["proxyPort"] == original_port + 1
    QTest.keyClick(window, Qt.Key.Key_Down)
    assert port.property("value") == facade.draft["proxyPort"] == original_port
    username.forceActiveFocus()

    for character in "draft-user":
        QTest.keyClick(window, character)

    QTest.keyClick(window, Qt.Key.Key_Tab)
    assert password.hasActiveFocus()

    for character in "draft-secret":
        QTest.keyClick(window, character)

    assert facade.username == "draft-user"
    assert facade.password == "draft-secret"
    assert password.property("displayText")
    assert "draft-secret" not in password.property("displayText")
    assert host.credentials.snapshot().proxy_auth is None
    assert host.app_settings is original_settings
    assert not host.settings_file.exists()

    # Cancellation clears the form draft without silently activating the login
    # or persisting route changes merely because a larger control was edited.
    QTest.keyClick(window, Qt.Key.Key_Escape)
    qtbot.waitUntil(lambda: not facade.opened)
    assert host.credentials.snapshot().proxy_auth is None
    assert host.app_settings is original_settings
