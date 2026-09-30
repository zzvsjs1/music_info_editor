"""MPEG-4 audio adapter for managed iTunes-style metadata atoms."""

from collections.abc import Callable, Mapping, MutableMapping
from pathlib import Path
from typing import Protocol, cast

from mutagen.mp4 import MP4

from metadata_polisher.domain.errors import Issue, MediaErrorCode
from metadata_polisher.domain.media import MediaReadResult, StreamInfo
from metadata_polisher.domain.metadata import (
    FieldReadState,
    MetadataChange,
    MetadataField,
    MetadataSnapshot,
    Position,
)
from metadata_polisher.formats.base import MediaFormatError, TagReadResult, VerificationResult


class _Mp4Info(Protocol):
    length: float
    sample_rate: int
    channels: int
    bits_per_sample: int
    codec: str


class _Mp4File(Protocol):
    tags: MutableMapping[str, object] | None
    info: _Mp4Info

    def add_tags(self) -> None: ...

    def save(self) -> None: ...


type Mp4Loader = Callable[[Path], _Mp4File]


_SINGLE_ATOMS: Mapping[MetadataField, str] = {
    MetadataField.TITLE: "©nam",
    MetadataField.ALBUM: "©alb",
    MetadataField.DATE: "©day",
}
_MULTI_ATOMS: Mapping[MetadataField, str] = {
    MetadataField.ARTISTS: "©ART",
    MetadataField.ALBUM_ARTISTS: "aART",
    MetadataField.COMPOSERS: "©wrt",
    MetadataField.GENRES: "©gen",
}
_POSITION_ATOMS: Mapping[MetadataField, str] = {
    MetadataField.TRACK: "trkn",
    MetadataField.DISC: "disk",
}
_DELETE_ATOM = object()
_MP4_POSITION_MAX = (1 << 16) - 1


def _load_mp4(path: Path) -> _Mp4File:
    return cast(_Mp4File, MP4(path))  # type: ignore[no-untyped-call]


def _has_legacy_genre_atom(path: Path) -> bool:
    """Inspect only metadata containers, never search artwork/audio for byte strings.

    Mutagen turns gnre into ©gen while loading. An unrelated edit would otherwise
    silently rewrite the genre representation even though the adapter never set it.
    """
    if not path.is_file():
        return False

    with path.open("rb") as source:
        def visit(start: int, end: int, parent: bytes = b"", depth: int = 0) -> bool:
            offset = start

            while offset + 8 <= end:
                source.seek(offset)
                header = source.read(8)
                size = int.from_bytes(header[:4], "big")
                kind = header[4:]
                header_size = 8

                # MP4 size 1 introduces a 64-bit extended size; size 0 extends
                # to the enclosing boundary. Validate both before descending.
                if size == 1:
                    size = int.from_bytes(source.read(8), "big")
                    header_size = 16
                elif size == 0:
                    size = end - offset

                if size < header_size or size > end - offset:
                    raise ValueError("A metadata atom exceeds its MP4 container.")

                if parent == b"ilst" and kind == b"gnre":
                    return True

                if depth < 4 and kind in {b"moov", b"udta", b"meta", b"ilst"}:
                    # The meta full-box header has four version/flag bytes
                    # before its children, unlike the other containers here.
                    payload_start = offset + header_size + (4 if kind == b"meta" else 0)

                    if visit(payload_start, offset + size, kind, depth + 1):
                        return True

                offset += size

            return False

        return visit(0, path.stat().st_size)


def _validate_utf8(values: tuple[str, ...]) -> None:
    for value in values:
        # MP4 text atoms are UTF-8. Validate before loading the file so Mutagen
        # cannot reject a replacement after any preceding change was applied.
        value.encode("utf-8")


def _read_text_values(
    tags: Mapping[str, object] | None,
    atom: str,
) -> TagReadResult[tuple[str, ...]]:
    if tags is None or atom not in tags:
        return TagReadResult((), FieldReadState.MISSING)

    try:
        raw_value = tags[atom]
    except Exception as error:
        return TagReadResult((), FieldReadState.UNREADABLE, str(error))

    if not isinstance(raw_value, (list, tuple)):
        return TagReadResult((), FieldReadState.UNREADABLE, f"{atom} did not contain a text sequence")

    if any(not isinstance(value, str) for value in raw_value):
        return TagReadResult((), FieldReadState.UNREADABLE, f"{atom} contained a non-string value")

    values = tuple(value for value in raw_value if value != "")

    if not values:
        return TagReadResult((), FieldReadState.MISSING)

    return TagReadResult(values, FieldReadState.PRESENT)


def _read_single_value(
    tags: Mapping[str, object] | None,
    atom: str,
) -> TagReadResult[str | None]:
    text_read = _read_text_values(tags, atom)

    if text_read.read_state is not FieldReadState.PRESENT:
        return TagReadResult(None, text_read.read_state, text_read.detail)

    return TagReadResult(text_read.value[0], text_read.read_state)


def _read_position(
    tags: Mapping[str, object] | None,
    atom: str,
) -> TagReadResult[Position]:
    if tags is None or atom not in tags:
        return TagReadResult(Position(), FieldReadState.MISSING)

    try:
        raw_value = tags[atom]
    except Exception as error:
        return TagReadResult(Position(), FieldReadState.UNREADABLE, str(error))

    if not isinstance(raw_value, (list, tuple)):
        return TagReadResult(Position(), FieldReadState.UNREADABLE, f"{atom} did not contain a position sequence")

    if not raw_value:
        return TagReadResult(Position(), FieldReadState.MISSING)

    if len(raw_value) != 1:
        return TagReadResult(Position(), FieldReadState.UNREADABLE, f"{atom} contained multiple positions")

    pair = raw_value[0]

    if not isinstance(pair, (list, tuple)) or len(pair) != 2:
        return TagReadResult(Position(), FieldReadState.UNREADABLE, f"{atom} did not contain a number/total pair")

    number, total = pair

    if type(number) is not int or type(total) is not int:
        return TagReadResult(Position(), FieldReadState.UNREADABLE, f"{atom} contained a non-integer position")

    if not 0 <= number <= _MP4_POSITION_MAX or not 0 <= total <= _MP4_POSITION_MAX:
        return TagReadResult(Position(), FieldReadState.UNREADABLE, f"{atom} contained an out-of-range position")

    # MP4 uses zero for an absent component; the domain uses None so absence
    # cannot be mistaken for a real zero-numbered track during matching.
    position = Position(number=number or None, total=total or None)

    if position == Position():
        return TagReadResult(position, FieldReadState.MISSING)

    return TagReadResult(position, FieldReadState.PRESENT)


def _read_issue(field: MetadataField, detail: str | None) -> Issue:
    return Issue(
        code=MediaErrorCode.TAG_READ_FAILED,
        message=f"Could not read {field.value} metadata.",
        technical_detail=detail,
    )


def _encode_change(change: MetadataChange) -> tuple[str, object]:
    value = change.new_value

    if change.field in _SINGLE_ATOMS:
        atom = _SINGLE_ATOMS[change.field]

        if value is None:
            return atom, _DELETE_ATOM

        if not isinstance(value, str):
            raise TypeError(f"{atom} change must contain a string or None")

        _validate_utf8((value,))

        return atom, [value]

    if change.field in _MULTI_ATOMS:
        atom = _MULTI_ATOMS[change.field]

        if not isinstance(value, tuple) or any(not isinstance(item, str) for item in value):
            raise TypeError(f"{atom} change must contain a tuple of strings")

        _validate_utf8(value)

        return atom, list(value) if value else _DELETE_ATOM

    atom = _POSITION_ATOMS[change.field]

    if not isinstance(value, Position):
        raise TypeError(f"{atom} change must contain a Position value")

    components = tuple(component for component in (value.number, value.total) if component is not None)

    if any(component == 0 or component > _MP4_POSITION_MAX for component in components):
        raise ValueError(f"{atom} position components must be between 1 and {_MP4_POSITION_MAX}")

    if not components:
        return atom, _DELETE_ATOM

    return atom, [(value.number or 0, value.total or 0)]


def _metadata_value(snapshot: MetadataSnapshot, field: MetadataField) -> object:
    return getattr(snapshot, field.value)


def _expected_field_state(value: object) -> FieldReadState:
    if value is None or value == () or value == Position():
        return FieldReadState.MISSING

    return FieldReadState.PRESENT


def _stream_info(audio: _Mp4File) -> StreamInfo:
    return StreamInfo(
        duration_seconds=audio.info.length,
        sample_rate=audio.info.sample_rate,
        channels=audio.info.channels,
        bit_depth=audio.info.bits_per_sample,
        codec=audio.info.codec,
    )


class Mp4Adapter:
    """Read, update, and verify managed atoms in audio MP4 containers."""

    format_id = "mp4"
    extensions = frozenset({".m4a", ".mp4"})

    def __init__(self, loader: Mp4Loader = _load_mp4) -> None:
        self._loader = loader

    def can_handle(self, path: Path) -> bool:
        audio = self._loader(path)

        # Mutagen also opens video-only MP4 containers. A non-empty audio codec
        # proves that its parser found the audio track supported by this adapter.
        return bool(audio.info.codec)

    def read(self, path: Path) -> MediaReadResult:
        try:
            audio = self._loader(path)
        except Exception as error:
            raise MediaFormatError.from_cause(
                path=path,
                code=MediaErrorCode.TAG_READ_FAILED,
                message="Could not read MPEG-4 audio metadata.",
                cause=error,
            ) from error

        tags = cast(Mapping[str, object] | None, audio.tags)
        states: dict[MetadataField, FieldReadState] = {}
        issues: list[Issue] = []
        single_values: dict[MetadataField, str | None] = {}
        multi_values: dict[MetadataField, tuple[str, ...]] = {}

        for field, atom in _SINGLE_ATOMS.items():
            single_read = _read_single_value(tags, atom)
            single_values[field] = single_read.value
            states[field] = single_read.read_state

            if single_read.read_state is FieldReadState.UNREADABLE:
                issues.append(_read_issue(field, single_read.detail))

        for field, atom in _MULTI_ATOMS.items():
            multi_read = _read_text_values(tags, atom)
            multi_values[field] = multi_read.value
            states[field] = multi_read.read_state

            if multi_read.read_state is FieldReadState.UNREADABLE:
                issues.append(_read_issue(field, multi_read.detail))

        track_read = _read_position(tags, _POSITION_ATOMS[MetadataField.TRACK])
        disc_read = _read_position(tags, _POSITION_ATOMS[MetadataField.DISC])
        states[MetadataField.TRACK] = track_read.read_state
        states[MetadataField.DISC] = disc_read.read_state

        if track_read.read_state is FieldReadState.UNREADABLE:
            issues.append(_read_issue(MetadataField.TRACK, track_read.detail))

        if disc_read.read_state is FieldReadState.UNREADABLE:
            issues.append(_read_issue(MetadataField.DISC, disc_read.detail))

        return MediaReadResult(
            metadata=MetadataSnapshot(
                title=single_values[MetadataField.TITLE],
                artists=multi_values[MetadataField.ARTISTS],
                album=single_values[MetadataField.ALBUM],
                album_artists=multi_values[MetadataField.ALBUM_ARTISTS],
                composers=multi_values[MetadataField.COMPOSERS],
                track=track_read.value,
                disc=disc_read.value,
                date=single_values[MetadataField.DATE],
                genres=multi_values[MetadataField.GENRES],
            ),
            field_states=states,
            stream_info=_stream_info(audio),
            issues=tuple(issues),
        )

    def write_changes(self, path: Path, changes: tuple[MetadataChange, ...]) -> None:
        if not changes:
            return

        if MetadataField.GENRES not in {change.field for change in changes} and _has_legacy_genre_atom(path):
            raise MediaFormatError(
                path=path,
                issue=Issue(
                    code=MediaErrorCode.TAG_WRITE_FAILED,
                    message=("This MP4 contains a legacy genre atom that the library cannot preserve during "
                             "an unrelated edit. Review an explicit Genre change before writing this file."),
                ),
            )

        # Encode every requested atom before touching the loaded mapping. This
        # keeps a later invalid value from leaving earlier in-memory edits.
        encoded_changes = tuple(_encode_change(change) for change in changes)
        needs_tag_block = any(value is not _DELETE_ATOM for _, value in encoded_changes)

        try:
            audio = self._loader(path)
        except Exception as error:
            raise MediaFormatError.from_cause(
                path=path,
                code=MediaErrorCode.TAG_WRITE_FAILED,
                message="Could not open the MPEG-4 audio file for metadata writing.",
                cause=error,
            ) from error

        if audio.tags is None:
            if not needs_tag_block:
                return

            try:
                audio.add_tags()
            except Exception as error:
                raise MediaFormatError.from_cause(
                    path=path,
                    code=MediaErrorCode.TAG_WRITE_FAILED,
                    message="Could not create an MPEG-4 metadata atom table.",
                    cause=error,
                ) from error

        if audio.tags is None:
            raise MediaFormatError(
                path=path,
                issue=Issue(
                    code=MediaErrorCode.TAG_WRITE_FAILED,
                    message="MPEG-4 tag creation did not produce a writable atom table.",
                ),
            )

        for atom, value in encoded_changes:
            if value is _DELETE_ATOM:
                if atom in audio.tags:
                    del audio.tags[atom]
            else:
                # MP4Tags renders and validates before replacing its mapping entry,
                # so direct assignment preserves the old atom if rendering fails.
                audio.tags[atom] = value

        try:
            audio.save()
        except Exception as error:
            raise MediaFormatError.from_cause(
                path=path,
                code=MediaErrorCode.TAG_WRITE_FAILED,
                message="Could not save MPEG-4 audio metadata.",
                cause=error,
            ) from error

    def verify(
        self,
        path: Path,
        expected: MetadataSnapshot,
        changed_fields: frozenset[MetadataField],
        baseline_stream: StreamInfo,
    ) -> VerificationResult:
        actual = self.read(path)
        issues: list[Issue] = []

        for field in sorted(changed_fields, key=lambda item: item.value):
            expected_value = _metadata_value(expected, field)
            actual_value = _metadata_value(actual.metadata, field)
            expected_state = _expected_field_state(expected_value)

            if actual.field_states[field] is not expected_state or actual_value != expected_value:
                issues.append(
                    Issue(
                        code=MediaErrorCode.VERIFICATION_FAILED,
                        message=f"Written {field.value} metadata did not match the reviewed value.",
                        technical_detail=(
                            f"Expected state {expected_state.value}, "
                            f"observed state {actual.field_states[field].value}."
                        ),
                    )
                )

        if actual.stream_info != baseline_stream:
            issues.append(
                Issue(
                    code=MediaErrorCode.VERIFICATION_FAILED,
                    message="Stable MPEG-4 audio properties changed during metadata writing.",
                )
            )

        return VerificationResult(ok=not issues, issues=tuple(issues))
