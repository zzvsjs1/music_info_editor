"""Independent APE item bytes verify conventions without claiming a TAK decode."""

import struct

from mutagen.apev2 import APEv2

from metadata_polisher.domain.metadata import MetadataChange, MetadataField, Position
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
