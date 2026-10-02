"""TAK container adapter using compatible canonical APEv2 fields."""

from collections.abc import Callable, Mapping, MutableMapping, Sequence
from pathlib import Path
from typing import Protocol, cast

from mutagen.apev2 import APETextValue
from mutagen.tak import TAK

from metadata_polisher.domain.errors import Issue, MediaErrorCode
from metadata_polisher.domain.media import MediaReadResult, StreamInfo
from metadata_polisher.domain.metadata import (
    FieldReadState,
    FieldValue,
    MetadataChange,
    MetadataField,
    MetadataSnapshot,
    Position,
    metadata_value,
)
from metadata_polisher.formats.base import MediaFormatError, TagReadResult, VerificationResult
from metadata_polisher.formats.id3_policy import read_id3v1_tail


class _TakInfo(Protocol):
    length: float
    sample_rate: int
    channels: int
    bits_per_sample: int


class _TakFile(Protocol):
    tags: MutableMapping[str, object] | None
    info: _TakInfo

    def add_tags(self) -> None: ...

    def save(self) -> None: ...


type TakLoader = Callable[[Path], _TakFile]


# MusicBrainz Picard's published APEv2 mapping uses `Year` for its Date field.
# Mp3tag displays APE names literally, so it does not establish this convention.
# `Date` is a read alias; only an explicit reviewed date edit replaces it.
_SINGLE_KEYS: Mapping[MetadataField, tuple[str, ...]] = {
    MetadataField.TITLE: ("Title",),
    MetadataField.ALBUM: ("Album",),
    MetadataField.DATE: ("Year", "Date"),
}
_MULTI_KEYS: Mapping[MetadataField, tuple[str, ...]] = {
    MetadataField.ARTISTS: ("Artist",),
    MetadataField.ALBUM_ARTISTS: ("Album Artist",),
    MetadataField.COMPOSERS: ("Composer",),
    MetadataField.GENRES: ("Genre",),
}
_POSITION_KEYS: Mapping[MetadataField, tuple[str, ...]] = {
    MetadataField.TRACK: ("Track",),
    MetadataField.DISC: ("Disc",),
}
_DELETE_TAG = object()


def _load_tak(path: Path) -> _TakFile:
    return cast(_TakFile, TAK(path))  # type: ignore[no-untyped-call]


def _find_value(
    tags: Mapping[str, object] | None,
    keys: Sequence[str],
) -> tuple[bool, object | None]:
    if tags is None:
        return False, None

    try:
        keys_by_case = {key.casefold(): key for key in tags}

        for candidate in keys:
            actual_key = keys_by_case.get(candidate.casefold())

            if actual_key is not None:
                return True, tags[actual_key]
    except Exception as error:
        return True, error

    return False, None


def _read_text_values(
    tags: Mapping[str, object] | None,
    keys: Sequence[str],
) -> TagReadResult[tuple[str, ...]]:
    present, raw_value = _find_value(tags, keys)

    if not present:
        return TagReadResult((), FieldReadState.MISSING)

    if isinstance(raw_value, Exception):
        return TagReadResult((), FieldReadState.UNREADABLE, str(raw_value))

    # An APE item may be binary or external data even under a familiar name.
    # Keep that state unreadable instead of decoding arbitrary bytes as text.
    if not isinstance(raw_value, APETextValue):
        return TagReadResult((), FieldReadState.UNREADABLE, "APEv2 value was not UTF-8 text")

    try:
        values = tuple(raw_value)
    except Exception as error:
        return TagReadResult((), FieldReadState.UNREADABLE, str(error))

    if any(not isinstance(value, str) for value in values):
        return TagReadResult((), FieldReadState.UNREADABLE, "APEv2 text contained a non-string item")

    values = tuple(value for value in values if value != "")

    if not values:
        return TagReadResult((), FieldReadState.MISSING)

    return TagReadResult(values, FieldReadState.PRESENT)


def _read_single_value(
    tags: Mapping[str, object] | None,
    keys: Sequence[str],
) -> TagReadResult[str | None]:
    values: list[str] = []

    for key in keys:
        alias_read = _read_text_values(tags, (key,))

        if alias_read.read_state is FieldReadState.UNREADABLE:
            return TagReadResult(None, alias_read.read_state, alias_read.detail)

        values.extend(alias_read.value)

    # Compare the canonical key and all read aliases together. Choosing the
    # first dictionary entry would silently hide contradictory Year/Date tags.
    if len(set(values)) > 1:
        return TagReadResult(None, FieldReadState.UNREADABLE, f"Conflicting values for {'/'.join(keys)}")

    if not values:
        return TagReadResult(None, FieldReadState.MISSING)

    return TagReadResult(values[0], FieldReadState.PRESENT)


def _parse_position(value: str) -> Position:
    parts = value.split("/")

    if len(parts) > 2:
        raise ValueError("APEv2 position contained more than one slash")

    number = int(parts[0]) if parts[0] else None
    total = int(parts[1]) if len(parts) == 2 and parts[1] else None

    if number is None and total is None:
        raise ValueError("APEv2 position contained neither a number nor a total")

    return Position(number=number, total=total)


def _read_position(
    tags: Mapping[str, object] | None,
    keys: Sequence[str],
) -> TagReadResult[Position]:
    text_read = _read_text_values(tags, keys)

    if text_read.read_state is not FieldReadState.PRESENT:
        return TagReadResult(Position(), text_read.read_state, text_read.detail)

    if len(text_read.value) != 1:
        return TagReadResult(Position(), FieldReadState.UNREADABLE, "APEv2 position contained multiple values")

    try:
        return TagReadResult(_parse_position(text_read.value[0]), FieldReadState.PRESENT)
    except (TypeError, ValueError) as error:
        return TagReadResult(Position(), FieldReadState.UNREADABLE, str(error))


def _read_issue(field: MetadataField, detail: str | None) -> Issue:
    return Issue(
        code=MediaErrorCode.TAG_READ_FAILED,
        message=f"Could not read {field.value} metadata.",
        technical_detail=detail,
    )


def _validate_utf8(values: tuple[str, ...]) -> None:
    for value in values:
        # APETextValue delays UTF-8 encoding until save(). Validate before loading
        # the target so an encoding failure cannot leave partial in-memory edits.
        value.encode("utf-8")


def _validate_legacy_tail(path: Path) -> None:
    """Block unmanaged legacy tails before APEv2 saving can truncate them."""
    if read_id3v1_tail(path) is not None:
        raise ValueError("Preserving unmanaged ID3v1/Lyrics3 tails during APEv2 editing is unsupported.")

    if not path.is_file() or path.stat().st_size < 9:
        return

    with path.open("rb") as source:
        source.seek(-9, 2)
        marker = source.read(9)

    # Lyrics3v1 and v2 use different closing markers. Refuse either physical
    # trailer without interpreting its lyrics or silently moving/deleting it.
    if marker in {b"LYRICSEND", b"LYRICS200"}:
        raise ValueError("Preserving unmanaged Lyrics3 tails during APEv2 editing is unsupported.")


def _format_position(value: Position) -> str | object:
    if value.number is None and value.total is None:
        return _DELETE_TAG

    number = "" if value.number is None else str(value.number)

    if value.total is None:
        return number

    return f"{number}/{value.total}"


def _encode_change(change: MetadataChange) -> tuple[tuple[str, ...], str, object]:
    value = change.new_value

    if change.field in _SINGLE_KEYS:
        keys = _SINGLE_KEYS[change.field]

        if value is None:
            return keys, keys[0], _DELETE_TAG

        if not isinstance(value, str):
            raise TypeError(f"{keys[0]} change must contain a string or None")

        _validate_utf8((value,))

        return keys, keys[0], value

    if change.field in _MULTI_KEYS:
        keys = _MULTI_KEYS[change.field]

        if not isinstance(value, tuple) or any(not isinstance(item, str) for item in value):
            raise TypeError(f"{keys[0]} change must contain a tuple of strings")

        _validate_utf8(value)

        return keys, keys[0], list(value) if value else _DELETE_TAG

    keys = _POSITION_KEYS[change.field]

    if not isinstance(value, Position):
        raise TypeError(f"{keys[0]} change must contain a Position value")

    return keys, keys[0], _format_position(value)


def _remove_keys(tags: MutableMapping[str, object], keys: Sequence[str]) -> None:
    keys_to_remove = {key.casefold() for key in keys}

    for existing_key in tuple(tags.keys()):
        if existing_key.casefold() in keys_to_remove:
            del tags[existing_key]


def _expected_field_state(value: FieldValue | None) -> FieldReadState:
    if value is None or value == () or value == Position():
        return FieldReadState.MISSING

    return FieldReadState.PRESENT


def _stream_info(audio: _TakFile) -> StreamInfo:
    return StreamInfo(
        duration_seconds=audio.info.length,
        sample_rate=audio.info.sample_rate,
        channels=audio.info.channels,
        bit_depth=audio.info.bits_per_sample,
        codec="tak",
    )


class TakAdapter:
    """Read, update, and verify managed APEv2 metadata on TAK streams."""

    format_id = "tak"
    extensions = frozenset({".tak"})

    def __init__(self, loader: TakLoader = _load_tak) -> None:
        self._loader = loader

    def can_handle(self, path: Path) -> bool:
        self._loader(path)
        return True

    def read(self, path: Path) -> MediaReadResult:
        try:
            audio = self._loader(path)
        except Exception as error:
            raise MediaFormatError.from_cause(
                path=path,
                code=MediaErrorCode.TAG_READ_FAILED,
                message="Could not read TAK metadata.",
                cause=error,
            ) from error

        tags = cast(Mapping[str, object] | None, audio.tags)
        states: dict[MetadataField, FieldReadState] = {}
        issues: list[Issue] = []
        single_values: dict[MetadataField, str | None] = {}
        multi_values: dict[MetadataField, tuple[str, ...]] = {}

        for field, keys in _SINGLE_KEYS.items():
            single_read = _read_single_value(tags, keys)
            single_values[field] = single_read.value
            states[field] = single_read.read_state

            if single_read.read_state is FieldReadState.UNREADABLE:
                issues.append(_read_issue(field, single_read.detail))

        for field, keys in _MULTI_KEYS.items():
            multi_read = _read_text_values(tags, keys)
            multi_values[field] = multi_read.value
            states[field] = multi_read.read_state

            if multi_read.read_state is FieldReadState.UNREADABLE:
                issues.append(_read_issue(field, multi_read.detail))

        track_read = _read_position(tags, _POSITION_KEYS[MetadataField.TRACK])
        disc_read = _read_position(tags, _POSITION_KEYS[MetadataField.DISC])
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

        # Encode the entire edit before loading or changing tags. A clear of an
        # already absent value must not create an otherwise unnecessary tag block.
        encoded_changes = tuple(_encode_change(change) for change in changes)
        needs_tag_block = any(value is not _DELETE_TAG for _, _, value in encoded_changes)

        # Mutagen's APEv2 save truncates existing trailing ID3v1/Lyrics3 blocks.
        # Until exact preservation is supported, reject the edit before loading
        # or changing tags; the transaction then retains the original file.
        try:
            _validate_legacy_tail(path)
        except (OSError, ValueError) as error:
            raise MediaFormatError.from_cause(
                path=path,
                code=MediaErrorCode.TAG_WRITE_FAILED,
                message=f"Cannot safely preserve TAK metadata: {error}",
                cause=error,
            ) from error

        try:
            audio = self._loader(path)
        except Exception as error:
            raise MediaFormatError.from_cause(
                path=path,
                code=MediaErrorCode.TAG_WRITE_FAILED,
                message="Could not open the TAK file for metadata writing.",
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
                    message="Could not create a TAK APEv2 tag block.",
                    cause=error,
                ) from error

        if audio.tags is None:
            raise MediaFormatError(
                path=path,
                issue=Issue(
                    code=MediaErrorCode.TAG_WRITE_FAILED,
                    message="TAK tag creation did not produce a writable APEv2 tag object.",
                ),
            )

        for keys, canonical_key, value in encoded_changes:
            if value is _DELETE_TAG:
                _remove_keys(audio.tags, keys)
                continue

            # Mutagen list assignment is the only path that creates a NUL-backed
            # multi-value APETextValue; tuples would be treated as binary data.
            audio.tags[canonical_key] = value
            aliases = tuple(key for key in keys if key.casefold() != canonical_key.casefold())
            _remove_keys(audio.tags, aliases)

        try:
            audio.save()
        except Exception as error:
            raise MediaFormatError.from_cause(
                path=path,
                code=MediaErrorCode.TAG_WRITE_FAILED,
                message="Could not save TAK metadata.",
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
            expected_value = metadata_value(expected, field)
            actual_value = metadata_value(actual.metadata, field)
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
                    message="Stable TAK stream properties changed during metadata writing.",
                )
            )

        return VerificationResult(ok=not issues, issues=tuple(issues))
