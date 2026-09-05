"""Shared destructive format assertions, for disposable fixtures or temporary copies only."""

import struct
import zlib
from dataclasses import replace
from pathlib import Path
from typing import Any

from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, TXXX
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm
from mutagen.tak import TAK
from mutagen.wave import WAVE

from metadata_polisher.domain.metadata import FieldReadState, MetadataChange, MetadataField, MetadataSnapshot, Position
from metadata_polisher.formats.base import MediaFormatAdapter

# Unicode, literal punctuation in names, multi-values and leap-day precision
# exercise representations that simple ASCII single-value fixtures would miss.
CONTRACT_METADATA = MetadataSnapshot(
    title="序曲 – Café 🎵",
    artists=("演奏者 一", "Performer Two"),
    album="検証用アルバム – Album",
    album_artists=("Album Artist", "演奏団"),
    composers=("作曲家 一", "Composer; Two"),
    track=Position(3, 12),
    disc=Position(2, 4),
    date="2024-02-29",
    genres=("Game", "サウンドトラック"),
)
_MARKER_KEY = "METADATA_POLISHER_CONTRACT"
_MARKER_VALUE = "Unmanaged sentinel – 保持してください"
_MP4_MARKER = f"----:com.apple.iTunes:{_MARKER_KEY}"


def make_test_png() -> bytes:
    """Generate one pink RGB pixel without external artwork or image dependencies."""
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload))

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x80"))
        + chunk(b"IEND", b"")
    )


def _load_native(path: Path, format_id: str) -> Any:
    return {"flac": FLAC, "mp3": MP3, "mp4": MP4, "wave": WAVE, "tak": TAK}[format_id](path)


def _inject_unmanaged(audio: Any, format_id: str) -> None:
    if audio.tags is None:
        audio.add_tags()

    tags = audio.tags
    png = make_test_png()

    if format_id == "flac":
        tags[_MARKER_KEY] = [_MARKER_VALUE]
        picture = Picture()
        picture.type = 3
        picture.mime = "image/png"
        picture.desc = _MARKER_KEY
        picture.width = picture.height = 1
        picture.depth = 24
        picture.data = png
        audio.add_picture(picture)
    elif format_id in {"mp3", "wave"}:
        tags.add(TXXX(encoding=3, desc=_MARKER_KEY, text=[_MARKER_VALUE]))
        tags.add(APIC(encoding=3, mime="image/png", type=3, desc=_MARKER_KEY, data=png))
    elif format_id == "mp4":
        tags[_MP4_MARKER] = [MP4FreeForm(_MARKER_VALUE.encode("utf-8"))]
        tags["covr"] = [*tags.get("covr", []), MP4Cover(png, imageformat=MP4Cover.FORMAT_PNG)]
    elif format_id == "tak":
        tags[_MARKER_KEY] = _MARKER_VALUE

        # Existing APEv2 cover entries are retained verbatim. A tagless fixture
        # receives a conventional filename-NUL-image binary front-cover value.
        if not any(key.casefold().startswith("cover art (") for key in tags):
            tags["Cover Art (Front)"] = b"contract.png\x00" + png
    else:
        raise AssertionError(f"Unexpected contract format: {format_id}")

    audio.save(**({"v2_version": 4} if format_id in {"mp3", "wave"} else {}))


def _preservation_snapshot(audio: Any, format_id: str) -> tuple[object, ...]:
    tags = audio.tags
    assert tags is not None

    if format_id == "flac":
        assert tags[_MARKER_KEY] == [_MARKER_VALUE]
        return tuple(tags[_MARKER_KEY]), tuple(sorted(picture.write() for picture in audio.pictures))

    if format_id in {"mp3", "wave"}:
        marker = tuple(str(value) for value in tags.getall(f"TXXX:{_MARKER_KEY}")[0].text)
        assert marker == (_MARKER_VALUE,)
        artwork = tuple(sorted(
            (frame.HashKey, frame.mime, int(frame.type), frame.desc, frame.data)
            for frame in tags.getall("APIC")
        ))
        return marker, artwork

    if format_id == "mp4":
        marker = tuple(bytes(value) for value in tags[_MP4_MARKER])
        assert marker == (_MARKER_VALUE.encode("utf-8"),)
        return marker, tuple((bytes(cover), cover.imageformat) for cover in tags.get("covr", []))

    assert format_id == "tak"
    assert str(tags[_MARKER_KEY]) == _MARKER_VALUE
    return str(tags[_MARKER_KEY]), tuple(sorted(
        (key, bytes(tags[key])) for key in tags if key.casefold().startswith("cover art (")
    ))


def _assert_raw_representation(audio: Any, format_id: str) -> None:
    """Check physical keys independently of the adapter's reader/writer tables."""
    expected = CONTRACT_METADATA
    tags = audio.tags
    assert tags is not None

    if format_id == "flac":
        for key, values in (
            ("TITLE", [expected.title]), ("ARTIST", list(expected.artists)),
            ("ALBUM", [expected.album]), ("ALBUMARTIST", list(expected.album_artists)),
            ("COMPOSER", list(expected.composers)), ("GENRE", list(expected.genres)),
            ("DATE", [expected.date]), ("TRACKNUMBER", ["3"]), ("TRACKTOTAL", ["12"]),
            ("DISCNUMBER", ["2"]), ("DISCTOTAL", ["4"]),
        ):
            assert tags[key] == values, key

        assert "TOTALTRACKS" not in tags and "TOTALDISCS" not in tags
    elif format_id in {"mp3", "wave"}:
        assert tags.version[:2] == (2, 4)

        for key, values in (
            ("TIT2", (expected.title,)), ("TPE1", expected.artists), ("TALB", (expected.album,)),
            ("TPE2", expected.album_artists), ("TCOM", expected.composers), ("TCON", expected.genres),
            ("TDRC", (expected.date,)), ("TRCK", ("3/12",)), ("TPOS", ("2/4",)),
        ):
            assert tuple(str(value) for value in tags[key].text) == values, key
    elif format_id == "mp4":
        for key, values in (
            ("©nam", [expected.title]), ("©ART", list(expected.artists)), ("©alb", [expected.album]),
            ("aART", list(expected.album_artists)), ("©wrt", list(expected.composers)),
            ("©gen", list(expected.genres)), ("©day", [expected.date]), ("trkn", [(3, 12)]), ("disk", [(2, 4)]),
        ):
            assert tags[key] == values, key
    else:
        assert format_id == "tak"

        for key, value in (
            ("Title", expected.title), ("Album", expected.album), ("Year", expected.date),
            ("Track", "3/12"), ("Disc", "2/4"),
        ):
            assert str(tags[key]) == value, key

        for key, values in (
            ("Artist", expected.artists), ("Album Artist", expected.album_artists),
            ("Composer", expected.composers), ("Genre", expected.genres),
        ):
            assert tuple(tags[key]) == values, key
            assert bytes(tags[key]) == "\x00".join(values).encode("utf-8"), key

        assert "Date" not in tags


def assert_format_contract(path: Path, adapter: MediaFormatAdapter) -> None:
    """Write/read/verify a disposable file, preserving native unmanaged tags and artwork.

    Callers must generate a fixture or copy a source into their temporary test
    directory first. This helper rejects the protected local media/plans folders
    even if reached through a symlink; it never discovers or copies user media.
    """
    resolved = path.resolve()

    if any(part.casefold() in {"musics", ".plans", "plans"} for part in resolved.parts):
        raise ValueError("The format contract requires a disposable temporary copy.")

    assert adapter.can_handle(path)
    original = adapter.read(path)
    _inject_unmanaged(_load_native(path, adapter.format_id), adapter.format_id)
    preservation = _preservation_snapshot(_load_native(path, adapter.format_id), adapter.format_id)
    before = adapter.read(path)
    assert before.stream_info == original.stream_info
    changes = tuple(
        MetadataChange(field, getattr(before.metadata, field.value), getattr(CONTRACT_METADATA, field.value))
        for field in MetadataField
    )
    adapter.write_changes(path, changes)
    reread = adapter.read(path)
    assert reread.metadata == CONTRACT_METADATA
    assert all(state is FieldReadState.PRESENT for state in reread.field_states.values())
    verified = adapter.verify(path, CONTRACT_METADATA, frozenset(MetadataField), before.stream_info)
    assert verified.ok, verified.issues
    assert reread.stream_info == before.stream_info
    native = _load_native(path, adapter.format_id)
    # Independent native-key assertions prevent a reader and writer with the
    # same wrong mapping from validating each other's output.
    _assert_raw_representation(native, adapter.format_id)
    assert _preservation_snapshot(native, adapter.format_id) == preservation

    # Clearing both compound positions checks that a reader/writer pair cannot
    # retain an old total or leave an unreadable empty physical tag behind.
    cleared = replace(CONTRACT_METADATA, composers=(), genres=(), track=Position(), disc=Position())
    cleared_fields = frozenset((MetadataField.COMPOSERS, MetadataField.TRACK, MetadataField.DISC, MetadataField.GENRES))
    adapter.write_changes(path, tuple(
        MetadataChange(field, getattr(CONTRACT_METADATA, field.value), getattr(cleared, field.value))
        for field in MetadataField if field in cleared_fields
    ))
    after_clear = adapter.read(path)
    assert after_clear.metadata == cleared
    assert all(after_clear.field_states[field] is FieldReadState.MISSING for field in cleared_fields)
    verified = adapter.verify(path, cleared, cleared_fields, before.stream_info)
    assert verified.ok, verified.issues
    assert _preservation_snapshot(_load_native(path, adapter.format_id), adapter.format_id) == preservation
