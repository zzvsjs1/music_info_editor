"""The sole QML runtime checks portable storage before constructing services."""

from unittest.mock import Mock

from PySide6.QtCore import QObject
from PySide6.QtQuick import QQuickWindow

from metadata_polisher.ui.quick import application


def test_startup_reports_unwritable_folder_without_constructing_services(qapp, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "logs").write_text("Keep this file unchanged", encoding="utf-8")
    construct = Mock()
    shown = Mock()
    monkeypatch.setattr(application, "QuickBackend", construct)
    monkeypatch.setattr(application, "_show_startup_error", shown)

    assert application.run_quick(["metadata-polisher"]) == 1

    construct.assert_not_called()
    shown.assert_called_once()
    assert "writable directory" in shown.call_args.args[1]
    assert "beside the executable" in shown.call_args.args[1]
    assert not (tmp_path / "settings.json").exists()
    assert (tmp_path / "logs").read_text(encoding="utf-8") == "Keep this file unchanged"


def test_startup_checks_portable_paths_and_keeps_engine_alive_until_shutdown(qapp, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    real_backend = application.QuickBackend
    events = []
    instances = []
    window = QQuickWindow()

    def construct(**kwargs):
        assert (tmp_path / "logs").is_dir()
        events.append("backend")
        backend = real_backend(**kwargs)
        instances.append(backend)
        return backend

    class Engine(QObject):
        def __init__(self):
            super().__init__()
            self.destroyed.connect(lambda: events.append("engine destroyed"))

        def rootObjects(self):
            return [window]

        def deleteLater(self):
            events.append("engine disposed")
            super().deleteLater()

    def execute():
        assert events == ["backend"]
        assert not instances[0]._closed
        events.append("event loop")
        return 19

    monkeypatch.setattr(application, "QuickBackend", construct)
    monkeypatch.setattr(application, "create_quick_engine", lambda _backend: Engine())
    monkeypatch.setattr(qapp, "exec", execute)

    assert application.run_quick(["metadata-polisher"]) == 19
    assert events == ["backend", "event loop", "engine disposed", "engine destroyed"]
    assert instances[0]._closed
