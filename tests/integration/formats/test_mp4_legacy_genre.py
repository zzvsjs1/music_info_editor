"""A real legacy genre atom must not disappear through an unrelated tag edit."""

import struct

from mutagen.mp4 import MP4

from metadata_polisher.domain.metadata import MetadataChange, MetadataField
from metadata_polisher.formats.base import MediaFormatError
from metadata_polisher.formats.mp4 import Mp4Adapter
from tests.integration.formats.test_id3_preservation import make_encoded_fixture


def _replace_genre_atom(content: bytes) -> bytes:
    """Replace a seeded genre with an equal-sized legacy atom plus free padding.

    Retaining every ancestor's byte length keeps all existing audio chunk offsets
    valid even when moov precedes mdat. This operates independently of Mutagen.
    """
    result = bytearray()
    offset = 0

    while offset < len(content):
        size, kind = struct.unpack_from(">I4s", content, offset)
        assert 8 <= size <= len(content) - offset
        payload = content[offset + 8:offset + size]

        if kind in {b"moov", b"udta", b"ilst"}:
            payload = _replace_genre_atom(payload)
        elif kind == b"meta":
            payload = payload[:4] + _replace_genre_atom(payload[4:])
        elif kind == b"\xa9gen":
            payload = struct.pack(">I4sIIH", 18, b"data", 0, 0, 1)
            genre = struct.pack(">I4s", len(payload) + 8, b"gnre") + payload
            # Fill the removed bytes with a legal free atom. Keeping total
            # length unchanged preserves every ancestor size and audio offset.
            free_size = size - len(genre)
            assert free_size >= 8
            result += genre + struct.pack(">I4s", free_size, b"free") + bytes(free_size - 8)
            offset += size
            continue

        result += struct.pack(">I4s", len(payload) + 8, kind) + payload
        offset += size

    return bytes(result)


def test_unrelated_edit_preserves_legacy_genre_or_blocks_before_mutation(tmp_path) -> None:
    path = make_encoded_fixture(tmp_path, ".m4a")
    native = MP4(path)
    native["\xa9gen"] = ["Encoded legacy genre placeholder"]
    native.save()
    before = _replace_genre_atom(path.read_bytes())
    path.write_bytes(before)
    has_legacy_genre = b"gnre" in before
    assert has_legacy_genre
    assert Mp4Adapter().read(path).metadata.genres == ("Blues",)

    try:
        Mp4Adapter().write_changes(path, (MetadataChange(MetadataField.TITLE, None, "New title"),))
    except MediaFormatError as error:
        assert "genre" in str(error).casefold()
        unchanged = path.read_bytes() == before
        assert unchanged, "Blocked MP4 edit changed bytes"
    else:
        preserved_legacy_genre = b"gnre" in path.read_bytes()
        assert preserved_legacy_genre
        assert Mp4Adapter().read(path).metadata.genres == ("Blues",)


def test_explicit_genre_change_may_replace_legacy_atom_with_documented_text_atom(tmp_path) -> None:
    path = make_encoded_fixture(tmp_path, ".m4a")
    native = MP4(path)
    native["\xa9gen"] = ["Encoded legacy genre placeholder"]
    native.save()
    path.write_bytes(_replace_genre_atom(path.read_bytes()))
    Mp4Adapter().write_changes(path, (MetadataChange(MetadataField.GENRES, ("Blues",), ("Game", "ゲーム")),))
    after = path.read_bytes()

    explicitly_replaced = b"gnre" not in after and b"\xa9gen" in after
    assert explicitly_replaced
    assert Mp4Adapter().read(path).metadata.genres == ("Game", "ゲーム")
