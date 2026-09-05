"""Independent physical-tag checks on explicitly supplied disposable copies."""

import os
import shutil
import struct
from pathlib import Path

import pytest

from metadata_polisher.domain.metadata import MetadataChange, MetadataField, Position
from metadata_polisher.formats.flac import FlacAdapter
from metadata_polisher.formats.mp4 import Mp4Adapter


def _copy(tmp_path: Path, kind: str, suffix: str) -> Path:
    configured = os.environ.get(f"METADATA_POLISHER_TEST_{kind}_COPY")

    if not configured:
        pytest.skip(f"No explicit disposable {kind} copy supplied.")

    # Make another temporary copy even when the configured input is already
    # disposable; all writes remain confined to this test's own directory.
    destination = tmp_path / f"raw{suffix}"
    shutil.copy2(configured, destination)
    return destination


def _flac_blocks(content: bytes) -> tuple[list[tuple[int, bytes]], bytes]:
    assert content[:4] == b"fLaC"
    offset = 4
    blocks = []

    while True:
        header = content[offset:offset + 4]
        size = int.from_bytes(header[1:], "big")
        blocks.append((header[0] & 127, content[offset + 4:offset + 4 + size]))
        offset += 4 + size

        # FLAC reserves the high bit for the final-metadata-block marker; the
        # remaining bytes after that block are the compressed audio payload.
        if header[0] & 128:
            return blocks, content[offset:]


def _vorbis_comments(payload: bytes) -> dict[str, list[str]]:
    vendor_size = int.from_bytes(payload[:4], "little")
    offset = 4 + vendor_size
    count = int.from_bytes(payload[offset:offset + 4], "little")
    offset += 4
    comments: dict[str, list[str]] = {}

    for _ in range(count):
        size = int.from_bytes(payload[offset:offset + 4], "little")
        offset += 4
        name, value = payload[offset:offset + size].decode("utf-8").split("=", 1)
        comments.setdefault(name.upper(), []).append(value)
        offset += size

    return comments


def _atoms(content: bytes):
    offset = 0

    while offset + 8 <= len(content):
        size, kind = struct.unpack_from(">I4s", content, offset)
        header = 8

        if size == 1:
            size = int.from_bytes(content[offset + 8:offset + 16], "big")
            header = 16
        elif size == 0:
            size = len(content) - offset

        assert header <= size <= len(content) - offset
        yield kind, content[offset + header:offset + size]
        offset += size


def _mp4_tags(content: bytes) -> dict[bytes, tuple[tuple[bytes, bytes], ...]]:
    def find(data: bytes):
        for kind, payload in _atoms(data):
            if kind == b"ilst":
                return {key: tuple(_atoms(value)) for key, value in _atoms(payload)}

            if kind in {b"moov", b"udta", b"meta"}:
                found = find(payload[4:] if kind == b"meta" else payload)

                if found is not None:
                    return found

        return None

    result = find(content)
    assert result is not None
    return result


_VALUES = {
    MetadataField.TITLE: "Raw Title – 検証 🎵",
    MetadataField.ARTISTS: ("AC/DC", "演奏者 & Name; literal"),
    MetadataField.ALBUM_ARTISTS: ("Album Artist", "楽団"),
    MetadataField.COMPOSERS: ("Composer One", "作曲者 二"),
    MetadataField.GENRES: ("Game", "ゲーム"),
    MetadataField.DATE: "2024-02",
    MetadataField.TRACK: Position(3, 12),
    MetadataField.DISC: Position(None, 4),
}


@pytest.mark.localmedia
def test_flac_physical_comments_and_unmanaged_blocks(tmp_path):
    path = _copy(tmp_path, "FLAC", ".flac")
    before, audio = _flac_blocks(path.read_bytes())
    adapter = FlacAdapter()
    current = adapter.read(path).metadata
    adapter.write_changes(path, tuple(MetadataChange(field, getattr(current, field.value), value)
                                      for field, value in _VALUES.items()))
    after, observed_audio = _flac_blocks(path.read_bytes())
    comments = _vorbis_comments(next(payload for kind, payload in after if kind == 4))

    for field, key in ((MetadataField.TITLE, "TITLE"), (MetadataField.ARTISTS, "ARTIST"),
                       (MetadataField.ALBUM_ARTISTS, "ALBUMARTIST"), (MetadataField.COMPOSERS, "COMPOSER"),
                       (MetadataField.GENRES, "GENRE"), (MetadataField.DATE, "DATE")):
        value = _VALUES[field]
        assert comments[key] == (list(value) if isinstance(value, tuple) else [value])

    assert comments["TRACKNUMBER"] == ["3"] and comments["TRACKTOTAL"] == ["12"]
    assert "DISCNUMBER" not in comments and comments["DISCTOTAL"] == ["4"]
    unchanged_blocks = ([block for block in before if block[0] not in {1, 4}]
                        == [block for block in after if block[0] not in {1, 4}])
    assert unchanged_blocks, "Unmanaged FLAC metadata blocks changed"
    unchanged_audio = audio == observed_audio
    assert unchanged_audio, "Compressed FLAC payload changed"


@pytest.mark.localmedia
def test_mp4_physical_keys_data_types_pairs_and_unmanaged_atoms(tmp_path):
    path = _copy(tmp_path, "MP4", ".m4a")
    original = path.read_bytes()
    before = _mp4_tags(original)
    adapter = Mp4Adapter()
    current = adapter.read(path).metadata
    adapter.write_changes(path, tuple(MetadataChange(field, getattr(current, field.value), value)
                                      for field, value in _VALUES.items()))
    updated = path.read_bytes()
    after = _mp4_tags(updated)
    keys = {MetadataField.TITLE: b"\xa9nam", MetadataField.ARTISTS: b"\xa9ART",
            MetadataField.ALBUM_ARTISTS: b"aART", MetadataField.COMPOSERS: b"\xa9wrt",
            MetadataField.GENRES: b"\xa9gen", MetadataField.DATE: b"\xa9day"}

    for field, key in keys.items():
        values = []

        for kind, payload in after[key]:
            assert kind == b"data" and int.from_bytes(payload[:4], "big") == 1
            values.append(payload[8:].decode("utf-8"))

        expected = _VALUES[field]
        assert values == (list(expected) if isinstance(expected, tuple) else [expected])

    for key, expected in ((b"trkn", (3, 12)), (b"disk", (0, 4))):
        kind, payload = after[key][0]
        assert kind == b"data" and int.from_bytes(payload[:4], "big") == 0
        assert struct.unpack(">HH", payload[10:14]) == expected

    # Compare untouched atoms independently of semantic tag decoding, then
    # compare mdat payloads so artwork or compressed audio damage is visible.
    managed = {*keys.values(), b"trkn", b"disk", b"gnre"}
    retained = {key: value for key, value in before.items() if key not in managed}
    unchanged = retained == {key: value for key, value in after.items() if key not in managed}
    assert unchanged, "Unmanaged MP4 atoms or artwork changed"
    original_audio = tuple(payload for kind, payload in _atoms(original) if kind == b"mdat")
    observed_audio = tuple(payload for kind, payload in _atoms(updated) if kind == b"mdat")
    assert original_audio == observed_audio, "Compressed MP4 payload changed"
