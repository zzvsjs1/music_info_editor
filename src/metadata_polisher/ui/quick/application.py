"""Load the desktop scene from installed package data, including frozen builds."""

import logging
from collections.abc import Sequence
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QEvent, QUrl
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine, QQmlError
from PySide6.QtQuick import QQuickWindow

from metadata_polisher.infrastructure.paths import ApplicationPaths, ensure_writable_application_dir
from metadata_polisher.infrastructure.settings import load_settings
from metadata_polisher.ui.quick.backend import QuickBackend


def create_quick_engine(backend: QuickBackend, *, warnings: list[str] | None = None) -> QQmlApplicationEngine:
    """The caller retains backend and engine until the window/event loop exits."""
    # Qt selects its native platform style unless the caller explicitly chooses
    # one through the standard environment or command-line options. Forcing a
    # style here would override both Windows appearance and test configuration.
    engine = QQmlApplicationEngine()

    captured = warnings if warnings is not None else []

    def capture_warnings(errors: list[QQmlError]) -> None:
        captured.extend(error.toString() for error in errors)

    engine.warnings.connect(capture_warnings)

    engine.setInitialProperties({"backend": backend})
    engine.load(QUrl.fromLocalFile(str(Path(__file__).parent / "qml" / "Main.qml")))
    if not engine.rootObjects() or not isinstance(engine.rootObjects()[0], QQuickWindow):
        raise RuntimeError("The Qt Quick interface could not load.\n" + "\n".join(captured))

    return engine


def _show_startup_error(application: QGuiApplication, message: str) -> None:
    """Report a portable-storage failure without starting workers or Widgets."""
    engine = QQmlApplicationEngine()
    engine.setInitialProperties({"message": message})
    engine.load(QUrl.fromLocalFile(str(Path(__file__).parent / "qml" / "StartupError.qml")))
    try:
        if engine.rootObjects():
            application.exec()
    finally:
        # The event loop has stopped. Process the scheduled destruction while
        # QGuiApplication still exists, including the scene's rendering resources.
        engine.deleteLater()
        QCoreApplication.sendPostedEvents(engine, QEvent.Type.DeferredDelete)


def run_quick(argv: Sequence[str]) -> int:
    """Retain the engine and interface until cooperative shutdown is complete."""
    existing = QGuiApplication.instance()
    if existing is None:
        application = QGuiApplication(list(argv))
    elif isinstance(existing, QGuiApplication):
        application = existing
    else:
        raise RuntimeError("A non-GUI Qt application already exists")

    paths = ApplicationPaths.detect()
    writable = ensure_writable_application_dir(paths)
    if not writable.ok:
        _show_startup_error(application, writable.message or "Storage is not writable.")
        return 1

    loaded = load_settings(paths.settings_file)
    backend = QuickBackend(settings=loaded.settings, settings_file=paths.settings_file, app_dir=paths.app_dir)
    if loaded.error or loaded.warnings:
        backend.set_status("\n".join(filter(None, (loaded.error, *loaded.warnings))))
    engine: QQmlApplicationEngine | None = None
    try:
        engine = create_quick_engine(backend)
        logging.getLogger(__name__).info("Qt Quick interface ready")
        window = engine.rootObjects()[0]
        assert isinstance(window, QQuickWindow)
        # A transient Quick window can outlive its owner. Once the main window
        # accepts a close (including its discard guard), finish the whole app.
        # Reusable secondary windows can reject Close to hide their facade.
        # QGuiApplication.quit() asks them to close again and can be vetoed even
        # after Main has accepted its own close. Its guard already protects
        # pending work and active writes, so finish the event loop directly.
        window.visibleChanged.connect(lambda visible: None if visible else application.exit(0))
        application.aboutToQuit.connect(backend.shutdown)
        return application.exec()
    finally:
        # Even a failed QML load must join the executor. Do this before disposing
        # the bridge so no worker callback can target a destroyed QObject.
        backend.shutdown()
        if engine is not None:
            engine.deleteLater()
            # A deleteLater scheduled after exec() returns has no event loop to
            # deliver it. Destroy the scene now, before Python starts disposing
            # the application and the native render thread during interpreter exit.
            QCoreApplication.sendPostedEvents(engine, QEvent.Type.DeferredDelete)
