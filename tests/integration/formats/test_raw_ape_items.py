"""Independent APE item bytes verify conventions without claiming a TAK decode."""

import struct

import pytest
from mutagen.apev2 import APEv2

from metadata_polisher.domain.errors import MediaErrorCode
from metadata_polisher.domain.metadata import MetadataChange, MetadataField, Position
from metadata_polisher.formats.base import MediaFormatError
from metadata_polisher.formats.tak import TakAdapter
from tests.unit.formats.test_tak import FakeTakFile


def _raw_ape_items(path):
    content = path.read_bytes()
    footer = content[-32:]
    assert footer[:8] == b"APETAGEX"
    version, size, count = struct.unpack_from("<III", footer, 8)
    assert version == 2000
    # The footer size includes items and footer, not a preceding optional
    # header. Start from that boundary to parse items independently of Mutagen.
    offset = len(content) - size
    items = {}

    for _ in range(count):
        value_size, flags = struct.unpack_from("<II", content, offset)
        key_end = content.index(b"\x00", offset + 8)
        key = content[offset + 8:key_end].decode("ascii")
        value_start = key_end + 1
        items[key] = (flags, content[value_start:value_start + value_size])
        offset = value_start + value_size

    assert offset == len(content) - 32
    return items


def test_ape_raw_keys_text_values_and_binary_items_follow_documented_conventions(tmp_path) -> None:
    path = tmp_path / "metadata-only.ape"
    tags = APEv2()
    tags["Cover Art (Front)"] = b"cover.png\x00binary sentinel"
    tags["Date"] = "1999"
    tags.save(path)

    class PersistedApeFile(FakeTakFile):
        def save(self):
            self.tags.save(path)

    adapter = TakAdapter(loader=lambda _: PersistedApeFile(APEv2(path)))
    adapter.write_changes(path, (
        MetadataChange(MetadataField.ALBUM_ARTISTS, (), ("Artist/One", "Artist; Two")),
        MetadataChange(MetadataField.DISC, Position(), Position(2, 4)),
        MetadataChange(MetadataField.DATE, "1999", "2024-02"),
    ))
    raw = _raw_ape_items(path)

    # Raw flags 0 denote text with NUL-separated names; flags 2 denote binary
    # artwork that stays opaque. Name punctuation must remain literal.
    assert raw["Album Artist"] == (0, b"Artist/One\x00Artist; Two")
    assert raw["Disc"] == (0, b"2/4")
    assert raw["Year"] == (0, b"2024-02")
    assert "Date" not in raw
    assert raw["Cover Art (Front)"] == (2, b"cover.png\x00binary sentinel")


@pytest.mark.parametrize("tail_kind", ("id3v1", "lyrics3_id3v1", "lyrics3"))
def test_ape_edits_block_unmanaged_legacy_tails_before_mutagen_can_remove_them(tmp_path, tail_kind) -> None:
    path = tmp_path / "legacy-tail.ape"
    tags = APEv2()
    tags["Title"] = "Original title"
    tags.save(path)
    id3v1 = b"TAG" + b"Unmanaged ID3v1 sentinel".ljust(125, b"\x00")
    lyrics = b"LYRICSBEGIN" + b"Unmanaged Lyrics3 sentinel"
    lyrics3 = lyrics + f"{len(lyrics):06d}".encode("ascii") + b"LYRICS200"
    tail = id3v1 if tail_kind == "id3v1" else lyrics3 + (id3v1 if tail_kind == "lyrics3_id3v1" else b"")

    with path.open("ab") as target:
        target.write(tail)

    original = path.read_bytes()

    class PersistedApeFile(FakeTakFile):
        def save(self):
            self.save_calls += 1
            self.tags.save(path)

    audio = PersistedApeFile(APEv2(path))
    adapter = TakAdapter(loader=lambda _: audio)

    # Existing legacy blocks are outside the managed APE fields. Blocking the
    # edit is safer than relying on a save which truncates everything after APE.
    with pytest.raises(MediaFormatError, match="preserv.*(?:ID3v1|Lyrics3)") as blocked:
        adapter.write_changes(path, (MetadataChange(MetadataField.TITLE, "Original title", "Reviewed title"),))

    assert blocked.value.issue.code is MediaErrorCode.TAG_WRITE_FAILED
    assert audio.save_calls == 0
    assert str(audio.tags["Title"]) == "Original title"
    assert path.read_bytes() == original

    # No metadata edit, including a rename-only transaction, must invoke the
    # preservation blocker or rewrite any of the original tag bytes.
    adapter.write_changes(path, ())
    assert path.read_bytes() == original
