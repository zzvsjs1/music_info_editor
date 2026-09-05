"""Shared semantic codec for ID3 tags used by MP3 and RIFF/WAVE adapters."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import cast

from mutagen.id3 import (
    TALB,
    TCOM,
    TCON,
    TDAT,
    TDRC,
    TIT2,
    TPE1,
    TPE2,
    TPOS,
    TRCK,
    TYER,
    ID3Tags,
    ID3TimeStamp,
    TextFrame,
)

from metadata_polisher.domain.errors import Issue, MediaErrorCode
from metadata_polisher.domain.metadata import (
    FieldReadState,
    MetadataChange,
    MetadataField,
    MetadataSnapshot,
    Position,
)
from metadata_polisher.formats.base import VerificationResult
from metadata_polisher.formats.id3_policy import changed_frame_ids

ID3_WRITE_VERSION = 4
ID3_TEXT_ENCODING_UTF8 = 3
ID3_TEXT_ENCODING_UTF16 = 1

_SINGLE_FRAME_IDS: Mapping[MetadataField, str] = {
    MetadataField.TITLE: "TIT2",
    MetadataField.ALBUM: "TALB",
    MetadataField.DATE: "TDRC",
}
_MULTI_FRAME_IDS: Mapping[MetadataField, str] = {
    MetadataField.ARTISTS: "TPE1",
    MetadataField.ALBUM_ARTISTS: "TPE2",
    MetadataField.COMPOSERS: "TCOM",
    MetadataField.GENRES: "TCON",
}
_FRAME_TYPES: Mapping[MetadataField, type[TextFrame]] = {
    MetadataField.TITLE: TIT2,
    MetadataField.ARTISTS: TPE1,
    MetadataField.ALBUM: TALB,
    MetadataField.ALBUM_ARTISTS: TPE2,
    MetadataField.COMPOSERS: TCOM,
    MetadataField.DATE: TDRC,
    MetadataField.GENRES: TCON,
}
_FRAME_IDS: Mapping[MetadataField, str] = {
    **_SINGLE_FRAME_IDS,
    **_MULTI_FRAME_IDS,
}


@dataclass(frozen=True)
class Id3TagReadResult:
    """Semantic ID3 decode result before a container adds stream information."""

    metadata: MetadataSnapshot
    field_states: Mapping[MetadataField, FieldReadState]
    issues: tuple[Issue, ...] = ()

    def __post_init__(self) -> None:
        copied_states = dict(self.field_states)
        required_fields = frozenset(MetadataField)
        supplied_fields = frozenset(copied_states)

        if supplied_fields != required_fields:
            missing = ", ".join(sorted(field.value for field in required_fields - supplied_fields))
            unexpected = ", ".join(sorted(str(field) for field in supplied_fields - required_fields))
            raise ValueError(
                "field_states must contain exactly one state for every MetadataField; "
                f"missing=[{missing}], unexpected=[{unexpected}]"
            )

        object.__setattr__(self, "field_states", MappingProxyType(copied_states))
        object.__setattr__(self, "issues", tuple(self.issues))


def _frame_values(
    tags: ID3Tags,
    frame_id: str,
) -> tuple[tuple[str, ...], FieldReadState, str | None]:
    frame = tags.get(frame_id)  # type: ignore[no-untyped-call]

    if frame is None:
        return (), FieldReadState.MISSING, None

    # TCON.genres expands legacy numeric genre notation correctly. Other text
    # frames retain their native ID3v2.4 list values without slash splitting.
    try:
        raw_values = getattr(frame, "genres", None) if frame_id == "TCON" else getattr(frame, "text", None)
    except Exception as error:
        # Mutagen calculates TCON genres lazily, so malformed legacy values can
        # fail while the property is accessed rather than while the file loads.
        return (), FieldReadState.UNREADABLE, str(error)

    if not isinstance(raw_values, (list, tuple)):
        return (), FieldReadState.UNREADABLE, f"{frame_id} did not contain a text sequence"

    if frame_id == "TDRC":
        if any(not isinstance(value, (str, ID3TimeStamp)) for value in raw_values):
            return (), FieldReadState.UNREADABLE, "TDRC contained a non-date value"

        values = tuple(str(value) for value in raw_values)
    else:
        if any(not isinstance(value, str) for value in raw_values):
            return (), FieldReadState.UNREADABLE, f"{frame_id} contained a non-string value"

        values = cast(tuple[str, ...], tuple(raw_values))

    values = tuple(value for value in values if value != "")

    if not values:
        return (), FieldReadState.MISSING, None

    return values, FieldReadState.PRESENT, None


def _single_frame_value(
    tags: ID3Tags,
    frame_id: str,
) -> tuple[str | None, FieldReadState, str | None]:
    values, state, detail = _frame_values(tags, frame_id)

    if state is not FieldReadState.PRESENT:
        return None, state, detail

    return values[0], state, None


def _parse_position(value: str) -> Position:
    # Position syntax permits a missing side, so "/12" means a known total
    # with no track number. The write policy may reject this read representation.
    parts = value.split("/")

    if len(parts) > 2:
        raise ValueError("ID3 position contained more than one slash")

    number = int(parts[0]) if parts[0] else None
    total = int(parts[1]) if len(parts) == 2 and parts[1] else None

    if number is None and total is None:
        raise ValueError("ID3 position contained neither a number nor a total")

    return Position(number=number, total=total)


def _read_position(
    tags: ID3Tags,
    frame_id: str,
) -> tuple[Position, FieldReadState, str | None]:
    values, state, detail = _frame_values(tags, frame_id)

    if state is not FieldReadState.PRESENT:
        return Position(), state, detail

    try:
        return _parse_position(values[0]), FieldReadState.PRESENT, None
    except (TypeError, ValueError) as error:
        return Position(), FieldReadState.UNREADABLE, str(error)


def _read_issue(field: MetadataField, detail: str | None) -> Issue:
    return Issue(
        code=MediaErrorCode.TAG_READ_FAILED,
        message=f"Could not read {field.value} metadata.",
        technical_detail=detail,
    )


def _normalise_single_change(frame_id: str, value: object) -> tuple[str, ...]:
    if value is None:
        return ()

    if not isinstance(value, str):
        raise TypeError(f"{frame_id} change must contain a string or None")

    return (value,)


def _normalise_multi_change(frame_id: str, value: object) -> tuple[str, ...]:
    if not isinstance(value, tuple) or any(not isinstance(item, str) for item in value):
        raise TypeError(f"{frame_id} change must contain a tuple of strings")

    return value


def _format_position(value: object) -> tuple[str, ...]:
    if not isinstance(value, Position):
        raise TypeError("ID3 position change must contain a Position value")

    if value.number is None and value.total is None:
        return ()

    number = "" if value.number is None else str(value.number)

    if value.total is None:
        return (number,)

    return (f"{number}/{value.total}",)


def _write_version(tags: ID3Tags) -> int:
    original = getattr(tags, "version", (2, 4, 0))

    if original[:2] not in {(2, 3), (2, 4)}:
        raise ValueError("Editing this ID3 version is unsupported; no automatic conversion is permitted.")

    return int(original[1])


def _read_v23_date(tags: ID3Tags) -> tuple[str | None, FieldReadState, str | None]:
    # v2.3 stores year, day/month and time separately. Reconstruct only the
    # precision actually present; a missing year cannot anchor the other parts.
    year, year_state, year_detail = _single_frame_value(tags, "TYER")
    day_month, day_state, day_detail = _single_frame_value(tags, "TDAT")
    time, time_state, time_detail = _single_frame_value(tags, "TIME")

    if FieldReadState.UNREADABLE in (year_state, day_state, time_state):
        return None, FieldReadState.UNREADABLE, year_detail or day_detail or time_detail

    if year is None:
        if day_month is not None or time is not None:
            return None, FieldReadState.UNREADABLE, "ID3v2.3 date/time is present without its year"

        return None, FieldReadState.MISSING, None

    try:
        datetime.strptime(year, "%Y")

        if len(year) != 4:
            raise ValueError("ID3v2.3 TYER must have four digits")

        value = year

        if day_month is not None:
            if len(day_month) != 4:
                raise ValueError("ID3v2.3 TDAT must contain DDMM")

            value += f"-{day_month[2:]}-{day_month[:2]}"
            datetime.strptime(value, "%Y-%m-%d")

        if time is not None:
            if day_month is None or len(time) != 4:
                raise ValueError("ID3v2.3 TIME requires a complete day and HHMM")

            datetime.strptime(time, "%H%M")
            value += f"T{time[:2]}:{time[2:]}"

    except ValueError as error:
        return None, FieldReadState.UNREADABLE, str(error)

    return value, FieldReadState.PRESENT, None


def _validate_v23_date(value: object) -> None:
    if value is None:
        return

    if not isinstance(value, str):
        raise TypeError("ID3 date must be a string or None")

    # TYER and TDAT cannot express a month without a day. Never manufacture a
    # first-of-month date or silently discard the user's known precision.
    if len(value) not in {4, 10}:
        raise ValueError("ID3v2.3 cannot represent this date precision; use a year or complete YYYY-MM-DD date.")

    try:
        datetime.strptime(value, "%Y" if len(value) == 4 else "%Y-%m-%d")
    except ValueError:
        raise ValueError("ID3v2.3 requires a valid year or complete YYYY-MM-DD date.") from None


def _replace_text_frame(
    tags: ID3Tags,
    field: MetadataField,
    values: Sequence[str],
) -> None:
    frame_id = _FRAME_IDS[field]
    tags.delall(frame_id)  # type: ignore[no-untyped-call]

    if values:
        frame_type = _FRAME_TYPES[field]
        encoding = ID3_TEXT_ENCODING_UTF16 if _write_version(tags) == 3 else ID3_TEXT_ENCODING_UTF8
        frame = frame_type(encoding=encoding, text=list(values))
        tags.add(frame)  # type: ignore[no-untyped-call]


def _metadata_value(snapshot: MetadataSnapshot, field: MetadataField) -> object:
    return getattr(snapshot, field.value)


def _expected_field_state(value: object) -> FieldReadState:
    if value is None or value == () or value == Position():
        return FieldReadState.MISSING

    return FieldReadState.PRESENT


class Id3TagCodec:
    """Translate managed semantic fields to and from an existing ID3 tag set."""

    def changes_require_tag_block(self, changes: tuple[MetadataChange, ...]) -> bool:
        """Validate all changes and identify whether any value needs an ID3 frame."""
        required: list[bool] = []

        for change in changes:
            if change.field in _SINGLE_FRAME_IDS:
                values = _normalise_single_change(_SINGLE_FRAME_IDS[change.field], change.new_value)
            elif change.field in _MULTI_FRAME_IDS:
                values = _normalise_multi_change(_MULTI_FRAME_IDS[change.field], change.new_value)
            else:
                values = _format_position(change.new_value)

            required.append(bool(values))

        return any(required)

    def validate_preserved_version(self, tags: ID3Tags, changes: tuple[MetadataChange, ...]) -> int:
        """Validate the complete edit before changing frames or saving any bytes."""
        version = _write_version(tags)

        for change in changes:
            if (
                isinstance(change.new_value, Position)
                and change.new_value.number is None
                and change.new_value.total is not None
            ):
                raise ValueError("An ID3 total without a number has no supported write representation.")

            if version == 3:
                if isinstance(change.new_value, tuple) and len(change.new_value) > 1:
                    raise ValueError("ID3v2.3 cannot safely represent multiple separate names/values for this edit.")

                if change.field is MetadataField.DATE:
                    _validate_v23_date(change.new_value)

        # Even untouched v2.3 frames pass through Mutagen during save. Reject
        # existing encodings or lists that would change as an incidental effect.
        if version == 3:
            changed = changed_frame_ids(changes)

            for frame in tags.values():  # type: ignore[no-untyped-call]
                if getattr(frame, "FrameID", "") in changed:
                    continue

                if getattr(frame, "encoding", 0) not in {0, 1}:
                    raise ValueError("Existing ID3v2.3 text encoding cannot be preserved safely by this edit.")

                values = getattr(frame, "text", ())

                if isinstance(values, (list, tuple)) and len(values) > 1:
                    raise ValueError("Existing ID3v2.3 multiple text values would be flattened; this edit is blocked.")

        return version

    def read(self, tags: ID3Tags) -> Id3TagReadResult:
        states: dict[MetadataField, FieldReadState] = {}
        issues: list[Issue] = []
        single_values: dict[MetadataField, str | None] = {}
        multi_values: dict[MetadataField, tuple[str, ...]] = {}

        for field, frame_id in _SINGLE_FRAME_IDS.items():
            if field is MetadataField.DATE and getattr(tags, "version", (2, 4, 0))[:2] == (2, 3):
                single_value, state, detail = _read_v23_date(tags)
            else:
                single_value, state, detail = _single_frame_value(tags, frame_id)
            single_values[field] = single_value
            states[field] = state

            if state is FieldReadState.UNREADABLE:
                issues.append(_read_issue(field, detail))

        for field, frame_id in _MULTI_FRAME_IDS.items():
            multi_value, state, detail = _frame_values(tags, frame_id)
            multi_values[field] = multi_value
            states[field] = state

            if state is FieldReadState.UNREADABLE:
                issues.append(_read_issue(field, detail))

        track, track_state, track_detail = _read_position(tags, "TRCK")
        disc, disc_state, disc_detail = _read_position(tags, "TPOS")
        states[MetadataField.TRACK] = track_state
        states[MetadataField.DISC] = disc_state

        if track_state is FieldReadState.UNREADABLE:
            issues.append(_read_issue(MetadataField.TRACK, track_detail))

        if disc_state is FieldReadState.UNREADABLE:
            issues.append(_read_issue(MetadataField.DISC, disc_detail))

        metadata = MetadataSnapshot(
            title=single_values[MetadataField.TITLE],
            artists=multi_values[MetadataField.ARTISTS],
            album=single_values[MetadataField.ALBUM],
            album_artists=multi_values[MetadataField.ALBUM_ARTISTS],
            composers=multi_values[MetadataField.COMPOSERS],
            track=track,
            disc=disc,
            date=single_values[MetadataField.DATE],
            genres=multi_values[MetadataField.GENRES],
        )

        return Id3TagReadResult(
            metadata=metadata,
            field_states=states,
            issues=tuple(issues),
        )

    def write_changes(self, tags: ID3Tags, changes: tuple[MetadataChange, ...]) -> None:
        # Public codec use receives the same all-or-nothing in-memory validation
        # guarantee as the container adapters.
        self.changes_require_tag_block(changes)
        version = self.validate_preserved_version(tags, changes)
        encoding = ID3_TEXT_ENCODING_UTF16 if version == 3 else ID3_TEXT_ENCODING_UTF8

        for change in changes:
            if change.field is MetadataField.DATE and version == 3:
                # A reviewed date owns all three legacy components; remove stale
                # day/time precision before writing the replacement year/date.
                for identifier in ("TYER", "TDAT", "TIME"):
                    tags.delall(identifier)  # type: ignore[no-untyped-call]

                if isinstance(change.new_value, str):
                    tags.add(TYER(encoding=0, text=[change.new_value[:4]]))  # type: ignore[no-untyped-call]

                    if len(change.new_value) == 10:
                        tags.add(TDAT(encoding=0, text=[change.new_value[8:10] + change.new_value[5:7]]))  # type: ignore[no-untyped-call]

            elif change.field in _SINGLE_FRAME_IDS:
                values = _normalise_single_change(_SINGLE_FRAME_IDS[change.field], change.new_value)
                _replace_text_frame(tags, change.field, values)
            elif change.field in _MULTI_FRAME_IDS:
                values = _normalise_multi_change(_MULTI_FRAME_IDS[change.field], change.new_value)
                _replace_text_frame(tags, change.field, values)
            elif change.field is MetadataField.TRACK:
                values = _format_position(change.new_value)
                tags.delall("TRCK")  # type: ignore[no-untyped-call]

                if values:
                    track_frame = TRCK(  # type: ignore[no-untyped-call]
                        encoding=encoding,
                        text=list(values),
                    )
                    tags.add(track_frame)  # type: ignore[no-untyped-call]
            elif change.field is MetadataField.DISC:
                values = _format_position(change.new_value)
                tags.delall("TPOS")  # type: ignore[no-untyped-call]

                if values:
                    disc_frame = TPOS(  # type: ignore[no-untyped-call]
                        encoding=encoding,
                        text=list(values),
                    )
                    tags.add(disc_frame)  # type: ignore[no-untyped-call]

    def verify(
        self,
        tags: ID3Tags,
        expected: MetadataSnapshot,
        changed_fields: frozenset[MetadataField],
    ) -> VerificationResult:
        """Verify both semantic values and readable/missing state after an ID3 write."""
        actual = self.read(tags)
        issues: list[Issue] = []

        # Stable field order makes multiple verification failures reproducible.
        # Value equality alone is insufficient when decoding reported UNREADABLE.
        for field in sorted(changed_fields, key=lambda item: item.value):
            expected_value = _metadata_value(expected, field)
            actual_value = _metadata_value(actual.metadata, field)
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

        return VerificationResult(ok=not issues, issues=tuple(issues))
