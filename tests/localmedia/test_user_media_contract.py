"""Opt-in real-format verification that writes only to temporary copies."""

import hashlib
import os
import shutil
from pathlib import Path

import pytest

from metadata_polisher.formats.registry import FormatRegistry
from tests.support.format_contract import assert_format_contract


def _digest(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


@pytest.mark.localmedia
@pytest.mark.parametrize("format_id,extensions", [
    ("flac", (".flac",)),
    ("mp3", (".mp3",)),
    ("mp4", (".m4a", ".mp4")),
    ("wave", (".wav",)),
    ("tak", (".tak",)),
])
def test_real_user_media_contract_only_modifies_a_temporary_copy(tmp_path, format_id, extensions):
    supplied_root = os.environ.get("METADATA_POLISHER_TEST_MEDIA_ROOT")
    media_root = Path(supplied_root) if supplied_root else Path(__file__).resolve().parents[2] / "musics"

    if not media_root.is_dir():
        pytest.skip("The optional musics directory is absent.")

    media_root = media_root.resolve()
    # Resolve discovered paths before selection so a symlink outside the
    # chosen media root cannot become the representative for this opt-in check.
    candidates = sorted((path for path in media_root.rglob("*")
                         if path.suffix.casefold() in extensions and path.is_file()
                         and path.resolve().is_relative_to(media_root)),
                        key=lambda path: str(path.relative_to(media_root)).casefold())

    if not candidates:
        pytest.skip(f"No {format_id} representative was supplied in the selected copy directory.")

    # Select at most one source per format. Both the byte digest and metadata
    # timestamp are checked afterwards; every Mutagen write receives only the
    # distinct destination inside pytest's temporary directory.
    source = candidates[0]
    original_stat = source.stat()
    original_digest = _digest(source)
    destination = tmp_path / source.name
    assert destination.resolve().is_relative_to(tmp_path.resolve())
    assert not destination.resolve().is_relative_to(media_root)
    shutil.copy2(source, destination)
    adapter = FormatRegistry().detect(destination)
    assert adapter is not None, f"The selected {format_id} sample was not recognised: {source.name}"
    assert adapter.format_id == format_id

    try:
        assert_format_contract(destination, adapter)
    finally:
        # Check source preservation even if a disposable-copy contract assertion
        # fails; a failed write test must never hide an altered original.
        assert source.stat().st_size == original_stat.st_size
        assert source.stat().st_mtime_ns == original_stat.st_mtime_ns
        assert _digest(source) == original_digest
