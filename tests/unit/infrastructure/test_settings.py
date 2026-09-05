import json
from pathlib import Path

import pytest

from metadata_polisher.infrastructure.settings import (
    AppSettings,
    BackupSettings,
    DiagnosticsSettings,
    GeneralSettings,
    MatchingSettings,
    ProvidersSettings,
    RenameSettings,
    ReportsSettings,
    load_settings,
    save_settings,
)


def test_missing_settings_file_uses_defaults(tmp_path: Path) -> None:
    result = load_settings(tmp_path / "settings.json")

    assert result.settings == AppSettings()
    assert result.warnings == ()
    assert result.error is None


def test_settings_round_trip_preserves_typed_values(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    settings = AppSettings(
        general=GeneralSettings(last_root_folder=r"D:\Music"),
        rename=RenameSettings(
            enabled=False,
            template="%tracknumber% - %title%",
            minimum_track_digits=3,
            minimum_disc_digits=2,
        ),
        matching=MatchingSettings(preferred_language="ja"),
        providers=ProvidersSettings(selected_provider_id="vgmdb"),
        backup=BackupSettings(enabled=True, directory=r"E:\Backups"),
        reports=ReportsSettings(enabled=True, directory=r"E:\Reports"),
        diagnostics=DiagnosticsSettings(detailed_tracing=True),
        external_tools={"ffprobe": r"C:\Tools\ffprobe.exe"},
    )

    save_settings(settings_file, settings)
    result = load_settings(settings_file)

    assert result.settings == settings
    assert result.warnings == ()
    assert result.error is None


def test_missing_nested_field_uses_only_that_fields_default(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    # Omit only some rename leaves while retaining valid neighbours, so the
    # test distinguishes leaf defaults from replacing the whole settings section.
    settings_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "rename": {
                    "enabled": False,
                    "minimum_track_digits": 4,
                },
            }
        ),
        encoding="utf-8",
    )

    result = load_settings(settings_file)

    assert not result.settings.rename.enabled
    assert result.settings.rename.minimum_track_digits == 4
    assert result.settings.rename.minimum_disc_digits == 1
    assert result.settings.rename.template == "[%discnumber%.]%tracknumber%. %title%"
    assert result.warnings == ()


def test_invalid_digit_width_warns_and_uses_default(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "rename": {
                    "minimum_track_digits": -2,
                    "minimum_disc_digits": True,
                },
            }
        ),
        encoding="utf-8",
    )

    result = load_settings(settings_file)

    assert result.settings.rename.minimum_track_digits == 2
    assert result.settings.rename.minimum_disc_digits == 1
    assert any("rename.minimum_track_digits" in warning for warning in result.warnings)
    assert any("rename.minimum_disc_digits" in warning for warning in result.warnings)


def test_wrong_shaped_nested_section_warns_with_full_path_and_keeps_siblings(
    tmp_path: Path,
) -> None:
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "providers": {
                    "musicbrainz": ["not", "an", "object"],
                    "vgmdb": {"enabled": False, "priority": 75},
                },
            }
        ),
        encoding="utf-8",
    )

    result = load_settings(settings_file)

    assert result.settings.providers.selected_provider_id == "musicbrainz_direct"
    assert any("providers.musicbrainz" in warning for warning in result.warnings)


# Loading damaged settings is recoverable in memory; preserving the original
# document lets the user inspect or repair it instead of losing its contents.
def test_corrupt_json_uses_session_defaults_without_touching_file(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    corrupt_contents = '{"schema_version": 1, invalid'
    settings_file.write_text(corrupt_contents, encoding="utf-8")

    result = load_settings(settings_file)

    assert result.settings == AppSettings()
    assert result.error is not None
    assert result.warnings
    assert settings_file.read_text(encoding="utf-8") == corrupt_contents


def test_external_tools_preserve_arbitrary_string_paths(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    configured_tools = {
        "custom analyser": r"C:\Portable Tools\analyse.exe",
        "unicode-tool": r"D:\ツール\probe.exe",
    }
    settings_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "external_tools": configured_tools,
            }
        ),
        encoding="utf-8",
    )

    result = load_settings(settings_file)

    assert result.settings.external_tools == configured_tools
    assert result.warnings == ()


def test_schema_version_one_is_accepted(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    settings_file.write_text('{"schema_version": 1}', encoding="utf-8")

    result = load_settings(settings_file)

    assert result.settings.schema_version == 1
    assert result.error is None
    assert result.warnings == ()


def test_app_settings_rejects_an_unsupported_schema_version() -> None:
    with pytest.raises(ValueError, match="Unsupported settings schema_version"):
        AppSettings(schema_version=2)
