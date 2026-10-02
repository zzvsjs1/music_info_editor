"""Native FLAC/Vorbis comment adapter with canonical managed-field writes."""

from collections.abc import Callable, Mapping, MutableMapping, Sequence
from pathlib import Path
from typing import Protocol, cast

from mutagen.flac import FLAC

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


class _FlacInfo(Protocol):
    length: float
    sample_rate: int
    channels: int
    bits_per_sample: int


class _FlacFile(Protocol):
    tags: MutableMapping[str, list[str]] | None
    info: _FlacInfo

    def add_tags(self) -> None: ...

    def save(self) -> None: ...


type FlacLoader = Callable[[Path], _FlacFile]


_SINGLE_KEYS: Mapping[MetadataField, str] = {
    MetadataField.TITLE: "TITLE",
    MetadataField.ALBUM: "ALBUM",
    MetadataField.DATE: "DATE",
}
_MULTI_KEYS: Mapping[MetadataField, str] = {
    MetadataField.ARTISTS: "ARTIST",
    MetadataField.ALBUM_ARTISTS: "ALBUMARTIST",
    MetadataField.COMPOSERS: "COMPOSER",
    MetadataField.GENRES: "GENRE",
}
_TRACK_NUMBER_KEYS = ("TRACKNUMBER",)
_TRACK_TOTAL_KEYS = ("TRACKTOTAL", "TOTALTRACKS")
_DISC_NUMBER_KEYS = ("DISCNUMBER",)
_DISC_TOTAL_KEYS = ("DISCTOTAL", "TOTALDISCS")


def _load_flac(path: Path) -> _FlacFile:
    return cast(_FlacFile, FLAC(path))  # type: ignore[no-untyped-call]


def _find_value(tags: Mapping[str, object] | None, keys: Sequence[str]) -> tuple[bool, object | None]:
    if tags is None:
        return False, None

    # Mutagen's VCommentDict deliberately iterates (key, value) pairs rather than
    # dictionary keys, so all key-oriented operations must use keys() explicitly.
    keys_by_case = {key.casefold(): key for key in tags.keys()}  # noqa: SIM118

    for candidate in keys:
        actual_key = keys_by_case.get(candidate.casefold())

        if actual_key is not None:
            return True, tags[actual_key]

    return False, None


def _read_text_values(
    tags: Mapping[str, object] | None,
    keys: Sequence[str],
) -> TagReadResult[tuple[str, ...]]:
    present, raw_value = _find_value(tags, keys)

    if not present:
        return TagReadResult((), FieldReadState.MISSING)

    if not isinstance(raw_value, (list, tuple)):
        return TagReadResult((), FieldReadState.UNREADABLE, "Vorbis value was not a list of strings")

    if any(not isinstance(value, str) for value in raw_value):
        return TagReadResult((), FieldReadState.UNREADABLE, "Vorbis value contained a non-string item")

    values = tuple(value for value in raw_value if value != "")

    if not values:
        return TagReadResult((), FieldReadState.MISSING)

    return TagReadResult(values, FieldReadState.PRESENT)


def _read_single_value(
    tags: Mapping[str, object] | None,
    key: str,
) -> TagReadResult[str | None]:
    text_read = _read_text_values(tags, (key,))

    if text_read.read_state is not FieldReadState.PRESENT:
        return TagReadResult(None, text_read.read_state, text_read.detail)

    if len(set(text_read.value)) > 1:
        return TagReadResult(None, FieldReadState.UNREADABLE, f"Conflicting repeated {key} values")

    return TagReadResult(text_read.value[0], text_read.read_state)


def _read_position_component(
    tags: Mapping[str, object] | None,
    keys: Sequence[str],
) -> TagReadResult[int | None]:
    """Compare every repeat/alias numerically instead of choosing dictionary order."""
    # Equivalent spellings such as "02" and "2" agree numerically. Different
    # totals across aliases must remain unreadable until the user resolves them.
    numbers: set[int] = set()

    for key in keys:
        text_read = _read_text_values(tags, (key,))

        if text_read.read_state is FieldReadState.UNREADABLE:
            return TagReadResult(None, text_read.read_state, text_read.detail)

        try:
            numbers.update(int(value) for value in text_read.value)
        except ValueError:
            return TagReadResult(None, FieldReadState.UNREADABLE, f"Invalid numeric {key} value")

    if len(numbers) > 1:
        return TagReadResult(None, FieldReadState.UNREADABLE, f"Conflicting values for {'/'.join(keys)}")

    if not numbers:
        return TagReadResult(None, FieldReadState.MISSING)

    return TagReadResult(next(iter(numbers)), FieldReadState.PRESENT)


def _read_position(
    tags: Mapping[str, object] | None,
    number_keys: Sequence[str],
    total_keys: Sequence[str],
) -> TagReadResult[Position]:
    # Number tags may contain either "1" or "1/2". Collect the two components
    # independently so an omitted side stays unknown, while every supplied
    # number and total must agree across repeats and separate total aliases.
    numbers: set[int] = set()
    totals: set[int] = set()

    for key in number_keys:
        text_read = _read_text_values(tags, (key,))

        if text_read.read_state is FieldReadState.UNREADABLE:
            return TagReadResult(Position(), text_read.read_state, text_read.detail)

        for value in text_read.value:
            number_text, separator, total_text = value.partition("/")

            try:
                number = int(number_text) if number_text else None
                inline_total = int(total_text) if separator and total_text else None

                if number is None and inline_total is None:
                    raise ValueError("Position contained neither a number nor a total")

                # Validate each representation before combining it with others;
                # negatives and malformed totals must never become missing data.
                position = Position(number=number, total=inline_total)
            except ValueError as error:
                return TagReadResult(Position(), FieldReadState.UNREADABLE, f"Invalid {key} position: {error}")

            if position.number is not None:
                numbers.add(position.number)

            if position.total is not None:
                totals.add(position.total)

    # Separate total tags remain numeric-only. An inline total does not take
    # precedence over contradictory DISCTOTAL/TOTALDISCS (or track aliases).
    total_read = _read_position_component(tags, total_keys)

    if total_read.read_state is FieldReadState.UNREADABLE:
        return TagReadResult(Position(), total_read.read_state, total_read.detail)

    if total_read.value is not None:
        totals.add(total_read.value)

    if len(numbers) > 1 or len(totals) > 1:
        return TagReadResult(
            Position(), FieldReadState.UNREADABLE, f"Conflicting values for {'/'.join((*number_keys, *total_keys))}"
        )

    if not numbers and not totals:
        return TagReadResult(Position(), FieldReadState.MISSING)

    try:
        position = Position(number=next(iter(numbers), None), total=next(iter(totals), None))
    except (TypeError, ValueError) as error:
        return TagReadResult(Position(), FieldReadState.UNREADABLE, str(error))

    return TagReadResult(position, FieldReadState.PRESENT)


def _read_issue(field: MetadataField, detail: str | None) -> Issue:
    return Issue(
        code=MediaErrorCode.TAG_READ_FAILED,
        message=f"Could not read {field.value} metadata.",
        technical_detail=detail,
    )


def _remove_keys(tags: MutableMapping[str, list[str]], keys: Sequence[str]) -> None:
    keys_to_remove = {key.casefold() for key in keys}

    for existing_key in tuple(tags.keys()):
        if existing_key.casefold() in keys_to_remove:
            del tags[existing_key]


def _write_single(tags: MutableMapping[str, list[str]], key: str, value: object) -> None:
    if value is not None and not isinstance(value, str):
        raise TypeError(f"{key} change must contain a string or None")

    _remove_keys(tags, (key,))

    if value is None:
        return

    tags[key] = [value]


def _write_multi(tags: MutableMapping[str, list[str]], key: str, value: object) -> None:
    if not isinstance(value, tuple) or any(not isinstance(item, str) for item in value):
        raise TypeError(f"{key} change must contain a tuple of strings")

    _remove_keys(tags, (key,))

    if value:
        tags[key] = list(value)


def _write_position(
    tags: MutableMapping[str, list[str]],
    value: object,
    *,
    number_keys: Sequence[str],
    total_keys: Sequence[str],
) -> None:
    if not isinstance(value, Position):
        raise TypeError("Position change must contain a Position value")

    # A reviewed position replaces every alias for that position, then writes
    # the first key in each list as the single canonical representation.
    _remove_keys(tags, (*number_keys, *total_keys))

    if value.number is not None:
        tags[number_keys[0]] = [str(value.number)]

    if value.total is not None:
        tags[total_keys[0]] = [str(value.total)]


def _change_requires_tag_block(change: MetadataChange) -> bool:
    """Validate a managed value and report whether it has a physical representation."""
    value = change.new_value

    if change.field in _SINGLE_KEYS:
        if value is not None and not isinstance(value, str):
            raise TypeError(f"{_SINGLE_KEYS[change.field]} change must contain a string or None")

        return value is not None

    if change.field in _MULTI_KEYS:
        if not isinstance(value, tuple) or any(not isinstance(item, str) for item in value):
            raise TypeError(f"{_MULTI_KEYS[change.field]} change must contain a tuple of strings")

        return bool(value)

    if not isinstance(value, Position):
        raise TypeError("Position change must contain a Position value")

    return value.number is not None or value.total is not None


def _expected_field_state(value: FieldValue | None) -> FieldReadState:
    if value is None or value == () or value == Position():
        return FieldReadState.MISSING

    return FieldReadState.PRESENT


class FlacAdapter:
    """Read and update only V1-managed Vorbis comments on FLAC files."""

    format_id = "flac"
    extensions = frozenset({".flac"})

    def __init__(self, loader: FlacLoader = _load_flac) -> None:
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
                message="Could not read FLAC metadata.",
                cause=error,
            ) from error

        tags = cast(Mapping[str, object] | None, audio.tags)
        states: dict[MetadataField, FieldReadState] = {}
        issues: list[Issue] = []
        single_values: dict[MetadataField, str | None] = {}
        multi_values: dict[MetadataField, tuple[str, ...]] = {}

        for field, key in _SINGLE_KEYS.items():
            single_read = _read_single_value(tags, key)
            single_values[field] = single_read.value
            states[field] = single_read.read_state

            if single_read.read_state is FieldReadState.UNREADABLE:
                issues.append(_read_issue(field, single_read.detail))

        for field, key in _MULTI_KEYS.items():
            multi_read = _read_text_values(tags, (key,))
            multi_values[field] = multi_read.value
            states[field] = multi_read.read_state

            if multi_read.read_state is FieldReadState.UNREADABLE:
                issues.append(_read_issue(field, multi_read.detail))

        track_read = _read_position(tags, _TRACK_NUMBER_KEYS, _TRACK_TOTAL_KEYS)
        disc_read = _read_position(tags, _DISC_NUMBER_KEYS, _DISC_TOTAL_KEYS)
        states[MetadataField.TRACK] = track_read.read_state
        states[MetadataField.DISC] = disc_read.read_state

        if track_read.read_state is FieldReadState.UNREADABLE:
            issues.append(_read_issue(MetadataField.TRACK, track_read.detail))

        if disc_read.read_state is FieldReadState.UNREADABLE:
            issues.append(_read_issue(MetadataField.DISC, disc_read.detail))

        metadata = MetadataSnapshot(
            title=single_values[MetadataField.TITLE],
            artists=multi_values[MetadataField.ARTISTS],
            album=single_values[MetadataField.ALBUM],
            album_artists=multi_values[MetadataField.ALBUM_ARTISTS],
            composers=multi_values[MetadataField.COMPOSERS],
            track=track_read.value,
            disc=disc_read.value,
            date=single_values[MetadataField.DATE],
            genres=multi_values[MetadataField.GENRES],
        )
        stream_info = StreamInfo(
            duration_seconds=audio.info.length,
            sample_rate=audio.info.sample_rate,
            channels=audio.info.channels,
            bit_depth=audio.info.bits_per_sample,
            codec=self.format_id,
        )

        return MediaReadResult(
            metadata=metadata,
            field_states=states,
            stream_info=stream_info,
            issues=tuple(issues),
        )

    def write_changes(self, path: Path, changes: tuple[MetadataChange, ...]) -> None:
        if not changes:
            return

        # Validate every change before Mutagen or an existing tag object is
        # touched, preventing a later invalid field from leaving partial edits.
        validated_requirements = tuple(_change_requires_tag_block(change) for change in changes)
        needs_tag_block = any(validated_requirements)
        try:
            audio = self._loader(path)
        except Exception as error:
            raise MediaFormatError.from_cause(
                path=path,
                code=MediaErrorCode.TAG_WRITE_FAILED,
                message="Could not open the FLAC file for metadata writing.",
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
                    message="Could not create a FLAC tag block.",
                    cause=error,
                ) from error

        if audio.tags is None:
            raise MediaFormatError(
                path=path,
                issue=Issue(
                    code=MediaErrorCode.TAG_WRITE_FAILED,
                    message="FLAC tag block creation did not produce a writable tag object.",
                ),
            )

        # Work on the existing comment dictionary: rebuilding all tags here
        # would erase unrelated fields such as ReplayGain or catalogue IDs.
        tags = audio.tags

        for change in changes:
            if change.field in _SINGLE_KEYS:
                _write_single(tags, _SINGLE_KEYS[change.field], change.new_value)
            elif change.field in _MULTI_KEYS:
                _write_multi(tags, _MULTI_KEYS[change.field], change.new_value)
            elif change.field is MetadataField.TRACK:
                _write_position(
                    tags,
                    change.new_value,
                    number_keys=_TRACK_NUMBER_KEYS,
                    total_keys=_TRACK_TOTAL_KEYS,
                )
            elif change.field is MetadataField.DISC:
                _write_position(
                    tags,
                    change.new_value,
                    number_keys=_DISC_NUMBER_KEYS,
                    total_keys=_DISC_TOTAL_KEYS,
                )

        try:
            audio.save()
        except Exception as error:
            raise MediaFormatError.from_cause(
                path=path,
                code=MediaErrorCode.TAG_WRITE_FAILED,
                message="Could not save FLAC metadata.",
                cause=error,
            ) from error

    def verify(
        self,
        path: Path,
        expected: MetadataSnapshot,
        changed_fields: frozenset[MetadataField],
        baseline_stream: StreamInfo,
    ) -> VerificationResult:
        # Reopen through the normal reader so verification observes saved bytes,
        # including an unreadable field that merely decodes to an empty value.
        actual = self.read(path)
        issues: list[Issue] = []

        for field in sorted(changed_fields, key=lambda item: item.value):
            expected_value = metadata_value(expected, field)
            actual_value = metadata_value(actual.metadata, field)
            expected_state = _expected_field_state(expected_value)
            actual_state = actual.field_states[field]

            if actual_state is not expected_state or actual_value != expected_value:
                issues.append(
                    Issue(
                        code=MediaErrorCode.VERIFICATION_FAILED,
                        message=f"Written {field.value} metadata did not match the reviewed value.",
                        technical_detail=(
                            f"Expected state {expected_state.value}, observed state {actual_state.value}."
                        ),
                    )
                )

        if actual.stream_info != baseline_stream:
            issues.append(
                Issue(
                    code=MediaErrorCode.VERIFICATION_FAILED,
                    message="Stable FLAC stream properties changed during metadata writing.",
                )
            )

        return VerificationResult(ok=not issues, issues=tuple(issues))
