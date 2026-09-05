"""Semantic events emitted by background and transactional operations."""

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from metadata_polisher.domain.errors import Issue


class FileApplyStatus(StrEnum):
    """Terminal outcome of one requested file transaction."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class FileSkipReason(StrEnum):
    """Stable reason a selected file never entered a transaction."""

    NO_CHANGES = "NO_CHANGES"
    BATCH_PREFLIGHT_BLOCKED = "BATCH_PREFLIGHT_BLOCKED"
    ALBUM_STOPPED_AFTER_FAILURE = "ALBUM_STOPPED_AFTER_FAILURE"
    CANCELLED_BEFORE_START = "CANCELLED_BEFORE_START"


# Stages describe work reached, not percentage complete. Failure and
# cancellation retain the stage so results can explain what happened on disk.
class FileTransactionStage(StrEnum):
    """Stable semantic stages suitable for progress UI and diagnostics."""

    NOT_STARTED = "not_started"
    BACKING_UP = "backing_up"
    COPYING_TEMPORARY = "copying_temporary"
    WRITING_METADATA = "writing_metadata"
    VERIFYING_TEMPORARY = "verifying_temporary"
    COMMITTING = "committing"
    RENAMING = "renaming"
    VERIFYING_FINAL = "verifying_final"
    CLEANING_ORIGINAL = "cleaning_original"
    COMPLETED = "completed"


def _validate_non_blank_string(name: str, value: object) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")

    if not value.strip():
        raise ValueError(f"{name} must be non-blank")


def _validate_file_identity(operation_id: str, file_id: str, source_path: Path) -> None:
    _validate_non_blank_string("operation_id", operation_id)
    _validate_non_blank_string("file_id", file_id)

    if not isinstance(source_path, Path):
        raise TypeError("source_path must be a Path")


@dataclass(frozen=True)
class OperationStarted:
    """A submitted operation began executing on its worker."""

    operation_id: str

    def __post_init__(self) -> None:
        _validate_non_blank_string("operation_id", self.operation_id)


@dataclass(frozen=True)
class OperationStageChanged:
    """An operation entered a domain-defined stage."""

    operation_id: str
    stage: str

    def __post_init__(self) -> None:
        _validate_non_blank_string("operation_id", self.operation_id)
        _validate_non_blank_string("stage", self.stage)


@dataclass(frozen=True)
class OperationProgress:
    """Bounded progress within one domain-defined operation stage."""

    operation_id: str
    stage: str
    current: int
    total: int

    def __post_init__(self) -> None:
        _validate_non_blank_string("operation_id", self.operation_id)
        _validate_non_blank_string("stage", self.stage)

        if type(self.current) is not int:
            raise TypeError("current must be an int")

        if type(self.total) is not int:
            raise TypeError("total must be an int")

        if self.current < 0:
            raise ValueError("current must be at least zero")

        if self.total < 0:
            raise ValueError("total must be at least zero")

        if self.current > self.total:
            raise ValueError("current must not exceed total")


@dataclass(frozen=True)
class ProviderStarted:
    """One provider began participating in an operation."""

    operation_id: str
    engine_id: str

    def __post_init__(self) -> None:
        _validate_non_blank_string("operation_id", self.operation_id)
        _validate_non_blank_string("engine_id", self.engine_id)


@dataclass(frozen=True)
class ProviderCompleted:
    """One provider completed its contribution to an operation."""

    operation_id: str
    engine_id: str

    def __post_init__(self) -> None:
        _validate_non_blank_string("operation_id", self.operation_id)
        _validate_non_blank_string("engine_id", self.engine_id)


@dataclass(frozen=True)
class ProviderFailed:
    """One provider failed with a structured, safe issue."""

    operation_id: str
    engine_id: str
    issue: Issue

    def __post_init__(self) -> None:
        _validate_non_blank_string("operation_id", self.operation_id)
        _validate_non_blank_string("engine_id", self.engine_id)

        if not isinstance(self.issue, Issue):
            raise TypeError("issue must be an Issue")


@dataclass(frozen=True)
class OperationCancelled:
    """An operation observed cancellation at a cooperative boundary."""

    operation_id: str

    def __post_init__(self) -> None:
        _validate_non_blank_string("operation_id", self.operation_id)


@dataclass(frozen=True)
class OperationCompleted:
    """An operation returned normally, including typed partial results."""

    operation_id: str

    def __post_init__(self) -> None:
        _validate_non_blank_string("operation_id", self.operation_id)


@dataclass(frozen=True)
class FileStarted:
    """A transactional writer accepted one file for processing."""

    operation_id: str
    file_id: str
    source_path: Path

    def __post_init__(self) -> None:
        _validate_file_identity(self.operation_id, self.file_id, self.source_path)


@dataclass(frozen=True)
class FileStageChanged:
    """A file reached a new semantic transaction stage."""

    operation_id: str
    file_id: str
    source_path: Path
    stage: FileTransactionStage

    def __post_init__(self) -> None:
        _validate_file_identity(self.operation_id, self.file_id, self.source_path)

        if not isinstance(self.stage, FileTransactionStage):
            raise TypeError("stage must be a FileTransactionStage")


@dataclass(frozen=True)
class FileCompleted:
    """A file ended successfully or at a cooperative cancellation boundary."""

    operation_id: str
    file_id: str
    source_path: Path
    final_path: Path
    status: FileApplyStatus
    completed_stage: FileTransactionStage

    def __post_init__(self) -> None:
        _validate_file_identity(self.operation_id, self.file_id, self.source_path)

        if not isinstance(self.final_path, Path):
            raise TypeError("final_path must be a Path")

        if self.status not in {FileApplyStatus.SUCCEEDED, FileApplyStatus.CANCELLED}:
            raise ValueError("FileCompleted status must be succeeded or cancelled")

        if not isinstance(self.completed_stage, FileTransactionStage):
            raise TypeError("completed_stage must be a FileTransactionStage")


@dataclass(frozen=True)
class FileFailed:
    """A file transaction ended with one or more structured issues."""

    operation_id: str
    file_id: str
    source_path: Path
    final_path: Path
    completed_stage: FileTransactionStage
    issues: tuple[Issue, ...]

    def __post_init__(self) -> None:
        _validate_file_identity(self.operation_id, self.file_id, self.source_path)

        if not isinstance(self.final_path, Path):
            raise TypeError("final_path must be a Path")

        if not isinstance(self.completed_stage, FileTransactionStage):
            raise TypeError("completed_stage must be a FileTransactionStage")

        copied_issues = tuple(self.issues)

        if not copied_issues or any(not isinstance(issue, Issue) for issue in copied_issues):
            raise ValueError("FileFailed issues must contain at least one Issue")

        object.__setattr__(self, "issues", copied_issues)


@dataclass(frozen=True)
class FileSkipped:
    """A selected file did not enter the transactional writer for a typed reason."""

    operation_id: str
    file_id: str
    source_path: Path
    reason: FileSkipReason

    def __post_init__(self) -> None:
        _validate_file_identity(self.operation_id, self.file_id, self.source_path)

        if not isinstance(self.reason, FileSkipReason):
            raise TypeError("reason must be a FileSkipReason")


# Keep event payloads immutable and typed across the worker/UI boundary; the
# sink receives facts and does not decide how the desktop should display them.
type FileOperationEvent = (
    FileStarted | FileStageChanged | FileCompleted | FileFailed | FileSkipped
)
type OperationEvent = (
    OperationStarted
    | OperationStageChanged
    | OperationProgress
    | ProviderStarted
    | ProviderCompleted
    | ProviderFailed
    | FileOperationEvent
    | OperationCancelled
    | OperationCompleted
)


class OperationEventSink(Protocol):
    """Observer boundary for semantic operation events."""

    def emit(self, event: OperationEvent) -> None: ...
