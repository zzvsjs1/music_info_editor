"""Invalid saved padding is reported and corrected before preview construction."""

import json

import pytest

from metadata_polisher.infrastructure.settings import RenameSettings, load_settings


@pytest.mark.parametrize(("field", "default"), (("minimum_track_digits", 2), ("minimum_disc_digits", 1)))
def test_oversized_saved_padding_uses_safe_default_with_visible_warning(tmp_path, field, default):
    path = tmp_path / "settings.json"
    document = {"schema_version": 1, "rename": {"template": "%title%", field: 2_147_483_647}}
    original = json.dumps(document)
    path.write_text(original, encoding="utf-8")

    result = load_settings(path)

    assert getattr(result.settings.rename, field) == default
    assert result.settings.rename.template == "%title%"
    assert any(field in warning and "10" in warning and "default" in warning for warning in result.warnings)
    assert path.read_text(encoding="utf-8") == original


@pytest.mark.parametrize("field", ("minimum_track_digits", "minimum_disc_digits"))
def test_direct_settings_reject_unsafe_padding_instead_of_silently_clamping(field):
    with pytest.raises(ValueError, match=rf"{field}.*10"):
        RenameSettings(**{field: 11})
