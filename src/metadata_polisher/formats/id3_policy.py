"""Conservative native ID3 preservation checks around the existing adapter save."""

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from mutagen.id3 import ID3Tags

from metadata_polisher.domain.metadata import MetadataChange, MetadataField


@dataclass(frozen=True)
class Id3RawSnapshot:
    """Original physical version/frames, read without library translation."""

    version: int
    frames: Mapping[str, tuple[bytes, ...]]


def _synchsafe(value: bytes) -> int:
    if len(value) != 4 or any(byte > 127 for byte in value):
        raise ValueError("The original ID3 size is malformed; editing is unsupported.")

    # Synchsafe size bytes contribute seven bits each, rather than eight:
    # a final byte of 1 means one byte, and the preceding byte of 1 means 128.
    return sum(byte << shift for byte, shift in zip(value, (21, 14, 7, 0), strict=True))


def _tag_offset(path: Path, *, wave: bool) -> int | None:
    with path.open("rb") as source:
        if not wave:
            return 0 if source.read(3) == b"ID3" else None

        header = source.read(12)

        if header[:4] != b"RIFF" or header[8:] != b"WAVE":
            raise ValueError("The WAVE container header is malformed.")

        end = min(path.stat().st_size, int.from_bytes(header[4:8], "little") + 8)

        while source.tell() + 8 <= end:
            chunk = source.read(8)
            size = int.from_bytes(chunk[4:], "little")

            if size > end - source.tell():
                raise ValueError("A WAVE chunk exceeds its container; editing is unsupported.")

            if chunk[:4].lower() == b"id3 ":
                return source.tell()

            # RIFF chunks are padded to even byte boundaries. The padding byte
            # is outside the declared size and must be skipped separately.
            source.seek(size + size % 2, 1)

    return None


def snapshot_id3(path: Path, *, wave: bool = False) -> Id3RawSnapshot | None:
    """Inspect real tag bytes, while retaining injected-loader adapter unit tests."""
    if not path.is_file():
        return None

    offset = _tag_offset(path, wave=wave)

    if offset is None:
        return None

    with path.open("rb") as source:
        source.seek(offset)
        header = source.read(10)

        if len(header) != 10 or header[:3] != b"ID3":
            raise ValueError("The original ID3 header is malformed; editing is unsupported.")

        version = header[3]

        if version not in {3, 4}:
            raise ValueError(f"Preserving ID3v2.{version} edits is unsupported; no conversion was requested.")

        size = _synchsafe(header[6:])

        if size > path.stat().st_size - source.tell():
            raise ValueError("The original ID3 block exceeds the file; editing is unsupported.")

        payload = source.read(size)

    # Mutagen reserialises header/frame flags. Until those specialised forms have
    # dedicated preservation coverage, a metadata edit must not erase them.
    if header[4] or header[5]:
        raise ValueError("Preserving ID3 revision/header flags is unsupported for this edit.")

    frames: dict[str, list[bytes]] = {}
    cursor = 0

    while cursor + 10 <= len(payload) and payload[cursor]:
        frame_header = payload[cursor:cursor + 10]

        try:
            identifier = frame_header[:4].decode("ascii")
        except UnicodeDecodeError:
            raise ValueError("The original ID3 frame identifier is malformed.") from None

        # Frame headers are ten bytes in both versions, but only v2.4 uses
        # synchsafe frame sizes; v2.3 uses a normal big-endian integer.
        frame_size = _synchsafe(frame_header[4:8]) if version == 4 else int.from_bytes(frame_header[4:8], "big")

        if frame_size > len(payload) - cursor - 10:
            raise ValueError("The original ID3 frame exceeds its tag; editing is unsupported.")

        if frame_header[8:] != b"\x00\x00":
            raise ValueError("Preserving specialised ID3 frame flags is unsupported for this edit.")

        frames.setdefault(identifier, []).append(payload[cursor + 10:cursor + 10 + frame_size])
        cursor += 10 + frame_size

    if any(payload[cursor:]):
        raise ValueError("The original ID3 trailing data is malformed; editing is unsupported.")

    return Id3RawSnapshot(version, MappingProxyType({key: tuple(values) for key, values in frames.items()}))


def validate_loaded_frames(snapshot: Id3RawSnapshot | None, tags: ID3Tags) -> None:
    """Reject frames lost or collapsed by the library before making any change."""
    if getattr(tags, "unknown_frames", ()):
        raise ValueError("Unknown ID3 frames require separate preservation support; this edit is blocked.")

    if snapshot is None:
        return

    # Compare counts before editing: the library may collapse duplicate frames
    # or discard unsupported ones while still returning a usable tag object.
    native_counts = Counter(str(getattr(frame, "FrameID", "")) for frame in tags.values())  # type: ignore[no-untyped-call]

    if native_counts != {identifier: len(values) for identifier, values in snapshot.frames.items()}:
        raise ValueError("The original ID3 contains unreadable or duplicate frames; this edit is blocked.")


_CHANGED_IDS: Mapping[MetadataField, frozenset[str]] = {
    MetadataField.TITLE: frozenset({"TIT2"}),
    MetadataField.ARTISTS: frozenset({"TPE1"}),
    MetadataField.ALBUM: frozenset({"TALB"}),
    MetadataField.ALBUM_ARTISTS: frozenset({"TPE2"}),
    MetadataField.COMPOSERS: frozenset({"TCOM"}),
    MetadataField.GENRES: frozenset({"TCON"}),
    MetadataField.TRACK: frozenset({"TRCK"}),
    MetadataField.DISC: frozenset({"TPOS"}),
    MetadataField.DATE: frozenset({"TDRC", "TYER", "TDAT", "TIME"}),
}


def changed_frame_ids(changes: tuple[MetadataChange, ...]) -> frozenset[str]:
    return frozenset(identifier for change in changes for identifier in _CHANGED_IDS[change.field])


def verify_preserved_frames(
    before: Id3RawSnapshot | None,
    after: Id3RawSnapshot | None,
    changes: tuple[MetadataChange, ...],
) -> None:
    """Reject an unsafe saved temporary file before the transaction can commit it."""
    if before is None:
        return

    if after is None or before.version != after.version:
        raise ValueError("The saved ID3 version did not preserve the original supported version.")

    # Exclude only explicitly reviewed frames from the byte comparison. An
    # unrelated title edit must preserve artwork, dates and all unmanaged frames.
    changed = changed_frame_ids(changes)
    original = {key: value for key, value in before.frames.items() if key not in changed}
    observed = {key: value for key, value in after.frames.items() if key not in changed}

    if original != observed:
        raise ValueError("Unchanged ID3 frame bytes were altered; the temporary write cannot be committed.")


def read_id3v1_tail(path: Path) -> bytes | None:
    """Preserve the unmanaged ID3v1 block without importing it into ID3v2 fields."""
    if not path.is_file() or path.stat().st_size < 128:
        return None

    with path.open("rb") as source:
        source.seek(-128, 2)
        tail = source.read(128)

    return tail if tail.startswith(b"TAG") else None


def restore_id3v1_tail(path: Path, original: bytes | None) -> None:
    """Undo Mutagen's implicit ID3v1 rewrite within the same temporary transaction."""
    if original is None:
        return

    with path.open("r+b") as target:
        target.seek(-128, 2)

        if target.read(3) != b"TAG":
            raise ValueError("The ID3v1 preservation boundary changed unexpectedly; the write is blocked.")

        target.seek(-128, 2)
        target.write(original)
