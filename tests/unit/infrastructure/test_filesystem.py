from pathlib import Path

import pytest

from metadata_polisher.infrastructure.filesystem import LocalFileSystem


def test_exclusive_copy_never_deletes_or_replaces_an_existing_destination(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.flac"
    destination = tmp_path / "existing-backup.flac"
    # Give the destination recognisably different bytes. A failed exclusive
    # copy must preserve that file, including during exception cleanup.
    source.write_bytes(b"new backup bytes")
    destination.write_bytes(b"existing backup bytes")

    with pytest.raises(FileExistsError):
        LocalFileSystem().copy_file(source, destination, overwrite=False)

    assert destination.read_bytes() == b"existing backup bytes"
