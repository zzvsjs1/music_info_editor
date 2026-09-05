from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import cast

import pytest

from metadata_polisher.domain.errors import (
    Issue,
    MatchingErrorCode,
    MediaErrorCode,
    ProviderErrorCode,
)
from metadata_polisher.domain.media import (
    FilenameHintConfidence,
    FilenameHintReason,
    FilenameHints,
    LocalMediaFile,
    MediaReadResult,
    StreamInfo,
    UnsupportedMediaFile,
    UnsupportedMediaStatus,
)
from metadata_polisher.domain.metadata import (
    FieldReadState,
    MetadataField,
    MetadataSnapshot,
)


def make_stream_info() -> StreamInfo:
    return StreamInfo(
        duration_seconds=185.25,
        sample_rate=44_100,
        channels=2,
        bit_depth=16,
        codec="FLAC",
    )


def make_field_states(
    overrides: dict[MetadataField, FieldReadState] | None = None,
) -> dict[MetadataField, FieldReadState]:
    states = {field: FieldReadState.MISSING for field in MetadataField}
    states.update(overrides or {})

    return states


# The value can be None in both cases; the state map carries the distinction
# between missing Title and unreadable Album across adapter boundaries.
def test_media_read_result_retains_distinct_field_states_immutably() -> None:
    result = MediaReadResult(
        metadata=MetadataSnapshot(title=None, album=None),
        field_states=make_field_states({MetadataField.ALBUM: FieldReadState.UNREADABLE}),
        stream_info=make_stream_info(),
    )

    assert result.field_states[MetadataField.TITLE] is FieldReadState.MISSING
    assert result.field_states[MetadataField.ALBUM] is FieldReadState.UNREADABLE

    with pytest.raises(TypeError):
        cast(dict[MetadataField, FieldReadState], result.field_states)[MetadataField.TITLE] = (
            FieldReadState.PRESENT
        )


# Omitted field states would force downstream code to invent defaults.
# Require an explicit state for every managed field at construction.
def test_media_read_result_rejects_incomplete_field_state_mapping() -> None:
    with pytest.raises(ValueError, match="every MetadataField"):
        MediaReadResult(
            metadata=MetadataSnapshot(),
            field_states={MetadataField.TITLE: FieldReadState.MISSING},
            stream_info=make_stream_info(),
        )


def test_local_media_file_retains_path_read_data_and_filename_evidence(tmp_path: Path) -> None:
    path = tmp_path / "Disc 2 - 03 - Theme.flac"
    read_result = MediaReadResult(
        metadata=MetadataSnapshot(title="Theme"),
        field_states=make_field_states({MetadataField.TITLE: FieldReadState.PRESENT}),
        stream_info=make_stream_info(),
    )
    hints = FilenameHints(
        disc_number=2,
        track_number=3,
        probable_title="Theme",
        confidence=FilenameHintConfidence.HIGH,
        reason=FilenameHintReason.LABELLED_DISC_TRACK_PREFIX,
    )

    media_file = LocalMediaFile(
        path=path,
        format_id="flac",
        read_result=read_result,
        filename_hints=hints,
    )

    assert media_file.path == path
    assert media_file.file_id == str(path)
    assert media_file.read_result is read_result
    assert media_file.filename_hints is hints
    assert media_file.filename_hints.confidence is FilenameHintConfidence.HIGH

    with pytest.raises(FrozenInstanceError):
        media_file.path = tmp_path / "changed.flac"


def test_unsupported_media_file_retains_path_and_explicit_status(tmp_path: Path) -> None:
    path = tmp_path / "track.opus"

    unsupported = UnsupportedMediaFile(path=path)

    assert unsupported.path == path
    assert unsupported.status is UnsupportedMediaStatus.NOT_SUPPORTED_YET
    assert unsupported.status.value == "not_supported_yet"


def test_filename_hints_default_to_no_confidence_when_no_evidence_exists() -> None:
    hints = FilenameHints()

    assert hints.confidence is None
    assert hints.reason is None


def test_media_result_normalises_issue_sequence_to_tuple() -> None:
    issue = Issue(
        code=MediaErrorCode.TAG_READ_FAILED,
        message="Could not read tags.",
        technical_detail="Invalid header at byte 12",
    )
    issues = [issue]

    result = MediaReadResult(
        metadata=MetadataSnapshot(),
        field_states=make_field_states(),
        stream_info=make_stream_info(),
        issues=issues,  # type: ignore[arg-type]
    )
    # The caller still owns this list; clearing it must not rewrite a read
    # result that has already crossed into immutable application state.
    issues.clear()

    assert result.issues == (issue,)


def test_error_codes_and_issue_fields_are_stable() -> None:
    assert ProviderErrorCode.NETWORK_TIMEOUT.value == "NETWORK_TIMEOUT"
    assert MediaErrorCode.UNSUPPORTED_FORMAT.value == "UNSUPPORTED_FORMAT"
    assert MatchingErrorCode.PARTIAL_TRACK_MAPPING.value == "PARTIAL_TRACK_MAPPING"

    issue = Issue(
        code=ProviderErrorCode.INVALID_RESPONSE,
        message="Provider response was invalid.",
    )

    assert issue.technical_detail is None
    assert issue.code is ProviderErrorCode.INVALID_RESPONSE
