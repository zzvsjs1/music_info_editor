"""Exercise the portable start-up boundary before workers or logging are created."""

# Start-up must check portable storage before creating services that might write.
# Mocks record that ordering while avoiding the real application event loop.


from types import SimpleNamespace
from unittest.mock import Mock

from PySide6.QtWidgets import QMessageBox

from metadata_polisher import __main__ as entrypoint


def test_startup_reports_unwritable_folder_without_constructing_application(qapp, monkeypatch, tmp_path):
    # A file in the logs location is a deterministic storage failure on Windows,
    # including accounts for which changing directory permission bits does nothing.
    monkeypatch.chdir(tmp_path)
    (tmp_path / "logs").write_text("Keep this file unchanged", encoding="utf-8")
    create = Mock()
    critical = Mock()
    monkeypatch.setattr(entrypoint, "create_application", create)
    monkeypatch.setattr(QMessageBox, "critical", critical)

    assert entrypoint.main() == 1

    create.assert_not_called()
    critical.assert_called_once()
    assert "writable directory" in critical.call_args.args[2]
    assert "beside the executable" in critical.call_args.args[2]
    assert not (tmp_path / "settings.json").exists()
    assert (tmp_path / "logs").read_text(encoding="utf-8") == "Keep this file unchanged"


def test_startup_checks_portable_paths_before_showing_window(qapp, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    fake_app = SimpleNamespace(exec=Mock(return_value=0))
    fake_window = SimpleNamespace(show=Mock())

    def create(argv):
        assert (tmp_path / "logs").is_dir()
        return fake_app, fake_window

    monkeypatch.setattr(entrypoint, "create_application", create)

    assert entrypoint.main() == 0

    fake_window.show.assert_called_once_with()
    fake_app.exec.assert_called_once_with()
