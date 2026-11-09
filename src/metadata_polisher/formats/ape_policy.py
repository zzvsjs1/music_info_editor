"""Preserve read-only APEv2 items which Mutagen's value model does not represent."""

import io
import struct
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from mutagen.apev2 import APEv2

_SIGNATURE = b"APETAGEX"
_HAS_HEADER = 1 << 31
_IS_HEADER = 1 << 29
_READ_ONLY = 1


@dataclass(frozen=True)
class _TagLayout:
    tag_start: int
    items_start: int
    items_end: int
    count: int


def _layout(source: BinaryIO) -> _TagLayout | None:
    length = source.seek(0, 2)
    if length < 32:
        return None

    # Mutagen's TAK decoder requires the audio magic at byte zero, so a TAK
    # file can only carry its APE tag at the end. Legacy trailers are checked
    # separately by the adapter before looking for this footer.
    source.seek(-32, 2)
    footer = source.read(32)
    if not footer.startswith(_SIGNATURE):
        return None

    version, size, count, flags = struct.unpack_from("<IIII", footer, 8)
    items_start, items_end = length - size, length - 32
    tag_start = items_start - (32 if flags & _HAS_HEADER else 0)
    if not 0 <= tag_start <= items_start <= items_end or version != 2000 or any(footer[24:]):
        raise ValueError("Invalid or unsupported APEv2 footer.")

    if flags & _HAS_HEADER:
        source.seek(tag_start)
        header = source.read(32)
        if header[:20] != footer[:20] or any(header[24:]):
            raise ValueError("The declared APEv2 header does not match the footer.")
        header_flags = struct.unpack_from("<I", header, 20)[0]
        if header_flags & ~_READ_ONLY != ((flags | _IS_HEADER) & ~_READ_ONLY):
            raise ValueError("The declared APEv2 header flags do not match the footer.")
        flags |= header_flags & _READ_ONLY
    elif items_start >= 32:
        source.seek(items_start - 32)
        if source.read(8) == _SIGNATURE:
            raise ValueError("The APEv2 footer does not declare its preceding header.")

    if flags & _READ_ONLY:
        raise ValueError("The APEv2 tag is read-only.")
    if flags & ~_HAS_HEADER:
        raise ValueError("Cannot preserve unknown APEv2 footer flags.")

    return _TagLayout(tag_start, items_start, items_end, count)


def _items(source: BinaryIO, layout: _TagLayout) -> Iterator[tuple[str, int, int, int]]:
    offset = layout.items_start
    seen = set()
    for _ in range(layout.count):
        source.seek(offset)
        # APE keys contain at most 255 ASCII bytes followed by a NUL. Seeking
        # past each value avoids loading covers just to check their flags.
        prefix = source.read(min(8 + 256, layout.items_end - offset))
        terminator = prefix.find(b"\0", 8)
        if terminator < 0:
            raise ValueError("Invalid APEv2 item header or key.")
        key = prefix[8:terminator].decode("ascii")
        size, flags = struct.unpack_from("<II", prefix)
        end = offset + terminator + 1 + size
        if end > layout.items_end or flags & ~7 or (flags & 6) == 6:
            raise ValueError(f"Cannot preserve APEv2 item {key!r} with invalid size or flags.")
        if key.casefold() in seen:
            raise ValueError(f"Cannot safely edit duplicate APEv2 item {key!r}.")
        seen.add(key.casefold())
        yield key, flags, offset, end
        offset = end

    if offset != layout.items_end:
        raise ValueError("The APEv2 item count does not match the tag size.")


@dataclass(frozen=True)
class ProtectedApeItems:
    tag_start: int
    raw_items: dict[str, bytes]

    def save(self, path: Path, tags: object) -> None:
        if not isinstance(tags, APEv2):
            raise ValueError("Cannot preserve read-only APEv2 items with this tag writer.")

        # Serialise metadata alone and restore the original protected items
        # before writing any bytes to the target. Mutagen can reorder items.
        encoded = io.BytesIO()
        tags.save(encoded)
        layout = _layout(encoded)
        if layout is None:
            raise ValueError("The edited APEv2 tag has no footer.")

        restored = set()
        for key, flags, start, end in _items(encoded, layout):
            raw = self.raw_items.get(key)
            if raw is None:
                continue
            encoded.seek(start)
            item = bytearray(encoded.read(end - start))
            struct.pack_into("<I", item, 4, flags | _READ_ONLY)
            if item != raw:
                raise ValueError(f"The edited tag changed protected APEv2 item {key!r}.")
            encoded.seek(start + 4)
            encoded.write(raw[4:8])
            restored.add(key)

        if restored != self.raw_items.keys():
            raise ValueError("The edited tag omitted a read-only APEv2 item.")

        with path.open("r+b") as target:
            target.seek(self.tag_start)
            target.write(encoded.getbuffer())
            target.truncate()


def protected_ape_items(path: Path, edited_keys: set[str]) -> ProtectedApeItems | None:
    if not path.is_file():
        return None

    protected = {}
    with path.open("rb") as source:
        layout = _layout(source)
        if layout is None:
            return None

        for key, flags, start, end in _items(source, layout):
            if flags & _READ_ONLY:
                if key.casefold() in edited_keys:
                    raise ValueError(f"The APEv2 item {key!r} is read-only.")
                source.seek(start)
                protected[key] = source.read(end - start)

    return ProtectedApeItems(layout.tag_start, protected) if protected else None
