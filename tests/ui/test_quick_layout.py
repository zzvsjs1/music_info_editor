"""Automatic QML layout storage preserves external and invalid preferences."""

from dataclasses import replace

from metadata_polisher.infrastructure.settings import AppSettings, load_settings, save_settings
from metadata_polisher.ui.quick.backend import QuickBackend
from metadata_polisher.ui.quick.preferences import QuickLayout
from tests.ui.helpers import ControlledExecutor


def test_layout_round_trip_preserves_non_ui_preferences_and_external_edits(qapp, tmp_path):
    path = tmp_path / "settings.json"
    original = AppSettings(external_tools={"Player": "player.exe"})
    save_settings(path, original)
    host = QuickBackend(settings=original, settings_file=path, executor=ControlledExecutor())
    try:
        layout = QuickLayout(host)
        layout.setAlbumWidth(350)
        layout.saveColumns("MainWindow/filesView", [56, 120, 300], [1])
        assert layout.persist()
        loaded = load_settings(path).settings
        assert loaded.ui.horizontal_sizes[0] == 350
        assert loaded.external_tools == original.external_tools
        external = replace(loaded, external_tools={"Editor": "external.exe"})
        save_settings(path, external)
        layout.setAlbumWidth(410)
        assert not layout.persist()
        assert load_settings(path).settings == external
    finally:
        host.shutdown()


def test_corrupt_settings_are_never_replaced_by_layout_save(qapp, tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{invalid", encoding="utf-8")
    host = QuickBackend(settings_file=path, executor=ControlledExecutor())
    try:
        layout = QuickLayout(host)
        layout.setAlbumWidth(350)
        assert not layout.persist()
        assert path.read_text(encoding="utf-8") == "{invalid"
    finally:
        host.shutdown()
