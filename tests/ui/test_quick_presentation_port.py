"""Protect explicit styling and the secondary windows' portable preferences."""

import json
import os
import subprocess
import sys
from dataclasses import replace

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtQuick import QQuickWindow

from metadata_polisher.infrastructure.settings import AppSettings, UiSettings, load_settings, save_settings
from metadata_polisher.ui.quick.application import create_quick_engine
from metadata_polisher.ui.quick.backend import QuickBackend
from tests.ui.helpers import ControlledExecutor


@pytest.mark.parametrize("style", ["Basic", "Windows"])
def test_qml_loader_preserves_the_explicit_controls_style(tmp_path, style):
    # Style selection is process-wide and must happen before the first Controls
    # import. A subprocess exercises the real loader without polluting other UI tests.
    script = """
import json
from PySide6.QtGui import QGuiApplication
from PySide6.QtQuickControls2 import QQuickStyle
from metadata_polisher.ui.quick.application import create_quick_engine
from metadata_polisher.ui.quick.backend import QuickBackend
app = QGuiApplication([])
backend = QuickBackend()
warnings = []
engine = create_quick_engine(backend, warnings=warnings)
print(json.dumps({'style': QQuickStyle.name(), 'loaded': bool(engine.rootObjects()), 'warnings': warnings}))
backend.shutdown()
"""
    environment = {
        **os.environ,
        "QT_QPA_PLATFORM": "offscreen",
        "QT_QUICK_BACKEND": "software",
        "QT_QUICK_CONTROLS_STYLE": style,
    }
    result = subprocess.run(
        [sys.executable, "-c", script],
        env=environment,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
        cwd=tmp_path,
    )
    observed = json.loads(result.stdout.strip().splitlines()[-1])

    assert observed["loaded"]
    assert observed["style"] == style
    assert observed["warnings"] == []


def test_help_restores_and_saves_its_size_even_while_still_open(qapp, qtbot, tmp_path):
    path = tmp_path / "settings.json"
    settings = replace(AppSettings(), ui=UiSettings(dialog_sizes={"WorkflowHelpDialog": (650, 470)}))
    save_settings(path, settings)
    backend = QuickBackend(settings=settings, settings_file=path, executor=ControlledExecutor())
    engine = create_quick_engine(backend)
    main = engine.rootObjects()[0]
    help_window = main.findChild(QQuickWindow, "helpWindow")
    assert help_window is not None

    try:
        help_window.show()
        qtbot.waitUntil(lambda: help_window.isVisible())
        qtbot.wait(60)

        assert (help_window.width(), help_window.height()) == (650, 470)
        help_window.resize(680, 510)
        qtbot.waitUntil(lambda: help_window.width() == 680)
        backend.shutdown()

        assert load_settings(path).settings.ui.dialog_sizes["WorkflowHelpDialog"] == (680, 510)
    finally:
        backend.shutdown()

        for child in main.findChildren(QQuickWindow):
            child.hide()

        main.hide()
        engine.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_help_keeps_the_resized_dimensions_when_reopened(qapp, qtbot, tmp_path):
    backend = QuickBackend(settings_file=tmp_path / "settings.json", executor=ControlledExecutor())
    engine = create_quick_engine(backend)
    main = engine.rootObjects()[0]
    window = main.findChild(QQuickWindow, "helpWindow")
    assert window is not None

    try:
        window.show()
        qtbot.waitUntil(window.isVisible)
        window.resize(680, 510)
        qtbot.waitUntil(lambda: window.width() == 680)
        window.close()
        qtbot.waitUntil(lambda: not window.isVisible())
        window.show()
        qtbot.waitUntil(window.isVisible)

        assert (window.width(), window.height()) == (680, 510)
    finally:
        backend.shutdown()
        window.hide()
        main.hide()
        engine.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
