from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from metadata_polisher.domain.errors import Issue, MediaErrorCode
from metadata_polisher.execution.events import (
    FileApplyStatus,
    FileCompleted,
    FileFailed,
    FileStageChanged,
    FileStarted,
    FileTransactionStage,
    OperationCancelled,
    OperationCompleted,
    OperationProgress,
    OperationStageChanged,
    OperationStarted,
    ProviderCompleted,
    ProviderFailed,
    ProviderStarted,
)


def test_file_events_expose_typed_semantics_and_are_immutable() -> None:
    # Distinct original/final paths make rename facts observable without any
    # filesystem work. Immutability protects those facts between thread handlers.
    source_path = Path("album/track.flac")
    final_path = Path("album/01. Track.flac")
    issue = Issue(
        code=MediaErrorCode.TAG_WRITE_FAILED,
        message="The metadata write failed.",
    )

    started = FileStarted("APPLY-0001", "file-1", source_path)
    stage = FileStageChanged(
        "APPLY-0001",
        "file-1",
        source_path,
        FileTransactionStage.WRITING_METADATA,
    )
    completed = FileCompleted(
        "APPLY-0001",
        "file-1",
        source_path,
        final_path,
        FileApplyStatus.SUCCEEDED,
        FileTransactionStage.COMPLETED,
    )
    failed = FileFailed(
        "APPLY-0001",
        "file-1",
        source_path,
        source_path,
        FileTransactionStage.WRITING_METADATA,
        (issue,),
    )

    assert started.source_path == source_path
    assert stage.stage is FileTransactionStage.WRITING_METADATA
    assert completed.status is FileApplyStatus.SUCCEEDED
    assert completed.final_path == final_path
    assert failed.issues == (issue,)

    with pytest.raises(FrozenInstanceError):
        stage.stage = FileTransactionStage.COMPLETED  # type: ignore[misc]


def test_operation_events_expose_only_semantic_lifecycle_values() -> None:
    issue = Issue(
        code=MediaErrorCode.TAG_READ_FAILED,
        message="The provider result could not be interpreted.",
    )

    started = OperationStarted(operation_id="LOOKUP-0001")
    stage = OperationStageChanged(operation_id="LOOKUP-0001", stage="searching")
    progress = OperationProgress(
        operation_id="LOOKUP-0001",
        stage="searching",
        current=2,
        total=5,
    )
    provider_started = ProviderStarted(operation_id="LOOKUP-0001", engine_id="musicbrainz")
    provider_completed = ProviderCompleted(operation_id="LOOKUP-0001", engine_id="musicbrainz")
    provider_failed = ProviderFailed(
        operation_id="LOOKUP-0001",
        engine_id="vgmdb",
        issue=issue,
    )
    completed = OperationCompleted(operation_id="LOOKUP-0001")
    cancelled = OperationCancelled(operation_id="LOOKUP-0002")

    assert started.operation_id == "LOOKUP-0001"
    assert stage.stage == "searching"
    assert (progress.current, progress.total) == (2, 5)
    assert provider_started.engine_id == "musicbrainz"
    assert provider_completed.engine_id == "musicbrainz"
    assert provider_failed.issue is issue
    assert completed.operation_id == "LOOKUP-0001"
    assert cancelled.operation_id == "LOOKUP-0002"

    with pytest.raises(FrozenInstanceError):
        progress.current = 3  # type: ignore[misc]


@pytest.mark.parametrize(
    ("event_factory", "message"),
    [
        pytest.param(lambda: OperationStarted(" "), "operation_id", id="blank-operation-id"),
        pytest.param(
            lambda: OperationStageChanged("SCAN-0001", "\t"),
            "stage",
            id="blank-stage",
        ),
        pytest.param(
            lambda: ProviderStarted("LOOKUP-0001", " "),
            "engine_id",
            id="blank-engine-id",
        ),
        pytest.param(
            lambda: FileStarted("APPLY-0001", " ", Path("track.flac")),
            "file_id",
            id="blank-file-id",
        ),
    ],
)
def test_event_identities_and_stages_must_be_non_blank(event_factory, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        event_factory()


@pytest.mark.parametrize(
    ("current", "total", "error_type", "message"),
    [
        pytest.param(True, 1, TypeError, "current", id="boolean-current"),
        pytest.param(0, False, TypeError, "total", id="boolean-total"),
        pytest.param(-1, 1, ValueError, "current", id="negative-current"),
        pytest.param(2, 1, ValueError, "current", id="current-above-total"),
    ],
)
def test_operation_progress_requires_bounded_exact_integers(
    current: object,
    total: object,
    error_type: type[Exception],
    message: str,
) -> None:
    with pytest.raises(error_type, match=message):
        OperationProgress(
            operation_id="SCAN-0001",
            stage="reading",
            current=current,  # type: ignore[arg-type]
            total=total,  # type: ignore[arg-type]
        )


def test_provider_failure_requires_one_structured_issue() -> None:
    with pytest.raises(TypeError, match="issue"):
        ProviderFailed(
            operation_id="LOOKUP-0001",
            engine_id="vgmdb",
            issue=object(),  # type: ignore[arg-type]
        )
