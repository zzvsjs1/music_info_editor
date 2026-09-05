"""Ordered, state-independent orchestration of reviewed file transactions."""

import logging
import os
import shutil
from collections.abc import Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Protocol, cast

from metadata_polisher.application.changes import (
    DEFAULT_RENAME_TEMPLATE,
    ChangeSetStatus,
    ChangeValidationFacts,
    FileChangeSet,
    RenameDecision,
    build_change_set,
)
from metadata_polisher.domain.errors import Issue, MatchingErrorCode, MediaErrorCode
from metadata_polisher.domain.media import LocalMediaFile, MediaReadResult
from metadata_polisher.domain.metadata import MetadataField
from metadata_polisher.domain.review import FieldReviewState, ReviewReasonCode
from metadata_polisher.execution.cancellation import CancellationToken
from metadata_polisher.execution.events import (
    FileApplyStatus,
    FileSkipped,
    FileSkipReason,
    FileTransactionStage,
    OperationEvent,
    OperationEventSink,
    OperationProgress,
    OperationStageChanged,
)
from metadata_polisher.formats.base import MediaFormatAdapter, MediaFormatError
from metadata_polisher.formats.registry import FormatRegistry
from metadata_polisher.infrastructure.reporting import (
    ProcessingReportRequest,
    ReportErrorCode,
    ReportFileEntry,
    ReportOperationResult,
    ReportOutputPolicy,
    ReportReasonCode,
    ReportWriteResult,
    ReportWriteStatus,
    SelectedReleaseReference,
)
from metadata_polisher.infrastructure.transaction import (
    BackupPolicy,
    FileApplyResult,
)
from metadata_polisher.matching.release_scoring import MatchReasonCode
from metadata_polisher.rename.template import FilenameRenderPolicy
from metadata_polisher.rename.windows import windows_collision_key
from metadata_polisher.scanner.filename_hints import extract_filename_hints

LOGGER = logging.getLogger(__name__)


class ApplyStage(StrEnum):
    """Stable stages emitted while one selected batch is processed."""

    PREFLIGHTING = "preflighting"
    APPLYING_FILES = "applying_files"
    REFRESHING_FILES = "refreshing_files"
    WRITING_REPORT = "writing_report"


class ApplyBatchStatus(StrEnum):
    """Audio-operation result, independent from optional report writing."""

    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ApplyFileOutcomeStatus(StrEnum):
    """Actual application-level result for one selected file."""

    APPLIED = "applied"
    FAILED = "failed"
    CANCELLED = "cancelled"
    NO_CHANGES = "no_changes"
    SKIPPED = "skipped"


ApplySkipReason = FileSkipReason


def _validate_identifier(name: str, value: object) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")

    if not value.strip():
        raise ValueError(f"{name} must be non-blank")


def _validate_revision(name: str, value: object) -> None:
    if type(value) is not int:
        raise TypeError(f"{name} must be an integer")

    if value < 0:
        raise ValueError(f"{name} cannot be negative")


def _windows_path_key(path: Path) -> tuple[str, ...]:
    return tuple(windows_collision_key(component) for component in path.parts)


def _copy_sequence[T](name: str, values: object, item_type: type[T]) -> tuple[T, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise TypeError(f"{name} must be an ordered sequence")

    copied = tuple(values)

    if any(not isinstance(value, item_type) for value in copied):
        raise TypeError(f"{name} contains an unsupported value")

    return cast(tuple[T, ...], copied)


def _copy_reason_codes(values: object) -> tuple[ReportReasonCode, ...]:
    copied: tuple[ReviewReasonCode | MatchReasonCode | MatchingErrorCode, ...] = _copy_sequence(
        "reason_codes",
        values,
        (ReviewReasonCode, MatchReasonCode, MatchingErrorCode),  # type: ignore[arg-type]
    )

    if len(copied) != len(set(copied)):
        raise ValueError("reason_codes must be unique")

    return cast(tuple[ReportReasonCode, ...], copied)


@dataclass(frozen=True)
class ApplyFileRequest:
    """One file's immutable source, reviewed decisions, and rebuild inputs."""

    source: LocalMediaFile
    reviews: tuple[FieldReviewState, ...]
    rename_decision: RenameDecision
    track_mapping_resolved: bool
    reason_codes: tuple[ReportReasonCode, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.source, LocalMediaFile):
            raise TypeError("source must be a LocalMediaFile")

        reviews = _copy_sequence("reviews", self.reviews, FieldReviewState)

        if tuple(review.field for review in reviews) != tuple(MetadataField):
            raise ValueError("reviews must contain every MetadataField in declaration order")

        if not isinstance(self.rename_decision, RenameDecision):
            raise TypeError("rename_decision must be a RenameDecision")

        if type(self.track_mapping_resolved) is not bool:
            raise TypeError("track_mapping_resolved must be a bool")

        object.__setattr__(self, "reviews", reviews)
        object.__setattr__(self, "reason_codes", _copy_reason_codes(self.reason_codes))


@dataclass(frozen=True)
class ApplyGroupRequest:
    """One selected album with captured revision and deterministic file order."""

    group_id: str
    base_group_revision: int
    selected_release: SelectedReleaseReference | None
    files: tuple[ApplyFileRequest, ...]

    def __post_init__(self) -> None:
        _validate_identifier("group_id", self.group_id)
        _validate_revision("base_group_revision", self.base_group_revision)

        if self.selected_release is not None and not isinstance(
            self.selected_release,
            SelectedReleaseReference,
        ):
            raise TypeError("selected_release must be a SelectedReleaseReference or None")

        files = _copy_sequence("files", self.files, ApplyFileRequest)
        file_ids = tuple(item.source.file_id for item in files)

        if not files:
            raise ValueError("an Apply group must contain at least one file")

        if len(file_ids) != len(set(file_ids)):
            raise ValueError("group file IDs must be unique")

        object.__setattr__(self, "files", files)


@dataclass(frozen=True)
class ApplyBatchRequest:
    """Complete immutable input captured before an Apply worker starts."""

    operation_id: str
    base_session_revision: int
    base_library_revision: int
    scan_root: Path
    groups: tuple[ApplyGroupRequest, ...]
    backup: BackupPolicy
    report: ReportOutputPolicy
    rename_template: str = DEFAULT_RENAME_TEMPLATE
    rename_policy: FilenameRenderPolicy = FilenameRenderPolicy()

    def __post_init__(self) -> None:
        _validate_identifier("operation_id", self.operation_id)
        _validate_revision("base_session_revision", self.base_session_revision)
        _validate_revision("base_library_revision", self.base_library_revision)

        if self.base_library_revision > self.base_session_revision:
            raise ValueError("base_library_revision cannot exceed base_session_revision")

        if not isinstance(self.scan_root, Path):
            raise TypeError("scan_root must be a Path")

        groups = _copy_sequence("groups", self.groups, ApplyGroupRequest)
        group_ids = tuple(group.group_id for group in groups)
        file_ids = tuple(item.source.file_id for group in groups for item in group.files)
        source_paths = tuple(item.source.path for group in groups for item in group.files)

        if not groups:
            raise ValueError("an Apply batch must contain at least one group")

        if len(group_ids) != len(set(group_ids)):
            raise ValueError("group IDs must be unique")

        if len(file_ids) != len(set(file_ids)):
            raise ValueError("batch file IDs must be unique")

        if len(source_paths) != len(set(source_paths)):
            raise ValueError("batch source paths must be unique")

        if len(source_paths) != len({_windows_path_key(path) for path in source_paths}):
            raise ValueError("batch source paths must be Windows-distinct")

        if any(group.base_group_revision > self.base_session_revision for group in groups):
            raise ValueError("base_group_revision cannot exceed base_session_revision")

        if not isinstance(self.backup, BackupPolicy):
            raise TypeError("backup must be a BackupPolicy")

        if self.backup.operation_id != self.operation_id:
            raise ValueError("backup operation_id must match the batch operation_id")

        if self.backup.scan_root != self.scan_root:
            raise ValueError("backup scan_root must match the batch scan_root")

        if not isinstance(self.report, ReportOutputPolicy):
            raise TypeError("report must be a ReportOutputPolicy")

        if not isinstance(self.rename_template, str):
            raise TypeError("rename_template must be a string")

        if not isinstance(self.rename_policy, FilenameRenderPolicy):
            raise TypeError("rename_policy must be a FilenameRenderPolicy")

        # Apply operation IDs must already be safe for an optional report filename.
        # Validate before any worker can modify audio, even when reporting is disabled.
        ProcessingReportRequest(
            operation_id=self.operation_id,
            result=ReportOperationResult.SUCCEEDED,
            files=(),
        )

        object.__setattr__(self, "groups", groups)


@dataclass(frozen=True)
class FilePreflightSnapshot:
    """Fresh adapter resolution and filesystem facts for one requested write."""

    file_id: str
    adapter: MediaFormatAdapter | None
    validation: ChangeValidationFacts
    capacity: FileCapacitySnapshot

    def __post_init__(self) -> None:
        _validate_identifier("file_id", self.file_id)

        if not isinstance(self.validation, ChangeValidationFacts):
            raise TypeError("validation must be ChangeValidationFacts")

        if not isinstance(self.capacity, FileCapacitySnapshot):
            raise TypeError("capacity must be a FileCapacitySnapshot")

        if (self.adapter is not None) is not self.validation.adapter_available:
            raise ValueError("adapter and adapter_available must agree")


@dataclass(frozen=True)
class PathStorageSnapshot:
    """Read-only free-space evidence for one filesystem storage identity."""

    storage_id: str
    free_bytes: int

    def __post_init__(self) -> None:
        _validate_identifier("storage_id", self.storage_id)

        if type(self.free_bytes) is not int:
            raise TypeError("free_bytes must be an integer")

        if self.free_bytes < 0:
            raise ValueError("free_bytes cannot be negative")


@dataclass(frozen=True)
class FileCapacitySnapshot:
    """Source size and temp/backup storage captured during fresh preflight."""

    source_size_bytes: int
    temporary_storage: PathStorageSnapshot
    backup_storage: PathStorageSnapshot | None = None
    backup_target: Path | None = None

    def __post_init__(self) -> None:
        if type(self.source_size_bytes) is not int:
            raise TypeError("source_size_bytes must be an integer")

        if self.source_size_bytes < 0:
            raise ValueError("source_size_bytes cannot be negative")

        if not isinstance(self.temporary_storage, PathStorageSnapshot):
            raise TypeError("temporary_storage must be a PathStorageSnapshot")

        if self.backup_storage is not None and not isinstance(
            self.backup_storage,
            PathStorageSnapshot,
        ):
            raise TypeError("backup_storage must be a PathStorageSnapshot or None")

        if self.backup_target is not None and not isinstance(self.backup_target, Path):
            raise TypeError("backup_target must be a Path or None")

        if (self.backup_storage is None) is not (self.backup_target is None):
            raise ValueError("backup storage and target must either both be present or absent")


class ApplyPathProbe(Protocol):
    """Read-only local path operations used by production Apply preflight."""

    def resolve(self, path: Path) -> Path: ...

    def is_readable_file(self, path: Path) -> bool: ...

    def is_writable_directory(self, path: Path) -> bool: ...

    def directory_names(self, path: Path) -> tuple[str, ...] | None: ...

    def file_size(self, path: Path) -> int | None: ...

    def storage(self, path: Path) -> PathStorageSnapshot | None: ...

    def nearest_existing_directory(self, path: Path) -> Path | None: ...

    def exists(self, path: Path) -> bool: ...

    def is_directory(self, path: Path) -> bool: ...


class _FormatResolver(Protocol):
    def detect(self, path: Path) -> MediaFormatAdapter | None: ...


class LocalApplyPathProbe:
    """Best-effort production probe which never creates or modifies a path."""

    def resolve(self, path: Path) -> Path:
        return path.resolve(strict=False)

    def is_readable_file(self, path: Path) -> bool:
        if not path.is_file():
            return False

        try:
            with path.open("rb"):
                return True
        except OSError:
            return False

    def is_writable_directory(self, path: Path) -> bool:
        return path.is_dir() and os.access(path, os.W_OK)

    def directory_names(self, path: Path) -> tuple[str, ...] | None:
        try:
            names = (entry.name for entry in path.iterdir())

            return tuple(sorted(names, key=lambda name: (name.casefold(), name)))
        except OSError:
            return None

    def file_size(self, path: Path) -> int | None:
        try:
            return path.stat().st_size if path.is_file() else None
        except OSError:
            return None

    def storage(self, path: Path) -> PathStorageSnapshot | None:
        existing = path if path.exists() else self.nearest_existing_directory(path)

        if existing is None:
            return None

        try:
            storage_id = str(existing.stat().st_dev)
            free_bytes = shutil.disk_usage(existing).free

            return PathStorageSnapshot(storage_id, free_bytes)
        except OSError:
            return None

    def nearest_existing_directory(self, path: Path) -> Path | None:
        current = path

        while not current.exists():
            parent = current.parent

            if parent == current:
                return None

            current = parent

        return current if current.is_dir() else current.parent

    def exists(self, path: Path) -> bool:
        return path.exists()

    def is_directory(self, path: Path) -> bool:
        return path.is_dir()


class LocalApplyPreflightInspector:
    """Capture current adapter, path, directory, and storage facts without writes."""

    def __init__(
        self,
        registry: _FormatResolver | None = None,
        probe: ApplyPathProbe | None = None,
    ) -> None:
        self._registry = registry if registry is not None else FormatRegistry()
        self._probe = probe if probe is not None else LocalApplyPathProbe()

    def inspect(
        self,
        source: LocalMediaFile,
        proposed_changes: FileChangeSet,
        backup: BackupPolicy,
    ) -> FilePreflightSnapshot:
        if not isinstance(source, LocalMediaFile):
            raise TypeError("source must be a LocalMediaFile")

        if not isinstance(proposed_changes, FileChangeSet):
            raise TypeError("proposed_changes must be a FileChangeSet")

        if proposed_changes.file_id != source.file_id:
            raise ValueError("proposed_changes must refer to source")

        if not isinstance(backup, BackupPolicy):
            raise TypeError("backup must be a BackupPolicy")

        source_path = source.path
        source_resolved = self._probe.resolve(source_path)
        scan_root_resolved = self._probe.resolve(backup.scan_root)
        source_within_scan = source_resolved.is_relative_to(scan_root_resolved)
        source_readable = source_within_scan and self._probe.is_readable_file(source_path)
        directory_writable = self._probe.is_writable_directory(source_path.parent)
        adapter = self._registry.detect(source_path) if source_readable else None
        source_size = self._probe.file_size(source_path)
        temporary_storage = self._probe.storage(source_path.parent)
        temporary_space_known = source_size is not None and temporary_storage is not None
        directory_names = self._probe.directory_names(source_path.parent)
        directory_listing_available = directory_names is not None
        existing_names = directory_names or ()
        backup_storage: PathStorageSnapshot | None = None
        backup_target: Path | None = None
        backup_root_writable = True
        backup_destination_available = True
        backup_space_known = True

        if backup.enabled:
            assert backup.root is not None
            backup_root_resolved = self._probe.resolve(backup.root)
            nearest_existing_path: Path | None = backup.root

            while nearest_existing_path is not None and not self._probe.exists(
                nearest_existing_path
            ):
                parent = nearest_existing_path.parent

                if parent == nearest_existing_path:
                    nearest_existing_path = None

                    break

                nearest_existing_path = parent

            backup_root_kind_valid = (
                nearest_existing_path is not None
                and self._probe.is_directory(nearest_existing_path)
            )
            nearest_backup_directory = (
                nearest_existing_path if backup_root_kind_valid else None
            )
            backup_root_writable = (
                backup_root_kind_valid
                and nearest_backup_directory is not None
                and self._probe.is_writable_directory(nearest_backup_directory)
                and not backup_root_resolved.is_relative_to(scan_root_resolved)
            )

            try:
                relative_source = source_resolved.relative_to(scan_root_resolved)
                backup_target = backup.root / backup.operation_id / relative_source
            except ValueError:
                backup_root_writable = False

            if backup_target is not None:
                operation_root = backup.root / backup.operation_id
                path_to_check = backup.root
                backup_path_kind_valid = True

                for component in backup_target.parent.relative_to(backup.root).parts:
                    path_to_check /= component

                    if self._probe.exists(path_to_check) and not self._probe.is_directory(
                        path_to_check
                    ):
                        backup_path_kind_valid = False

                        break

                # V1 writes each operation into a fresh directory. Reusing an
                # existing operation path could merge an incomplete old backup.
                backup_destination_available = (
                    backup_root_kind_valid
                    and backup_path_kind_valid
                    and not self._probe.exists(operation_root)
                    and not self._probe.exists(backup_target)
                )

            if nearest_backup_directory is not None:
                backup_storage = self._probe.storage(nearest_backup_directory)

            backup_space_known = source_size is not None and backup_storage is not None

        # Unknown storage is represented by zero capacity plus a failed fact,
        # never by an optimistic default that could authorise a write.
        safe_temporary_storage = temporary_storage or PathStorageSnapshot(
            f"unavailable-temp:{source.file_id}",
            0,
        )

        if backup.enabled and backup_storage is None:
            backup_storage = PathStorageSnapshot(f"unavailable-backup:{source.file_id}", 0)

        if backup.enabled and backup_target is None:
            backup_target = backup.root / backup.operation_id / source.file_id  # type: ignore[operator]

        return FilePreflightSnapshot(
            file_id=source.file_id,
            adapter=adapter,
            validation=ChangeValidationFacts(
                source_readable=source_readable,
                directory_writable=directory_writable,
                directory_listing_available=directory_listing_available,
                adapter_available=adapter is not None,
                temporary_space_sufficient=temporary_space_known,
                backup_root_writable=backup_root_writable,
                backup_destination_available=backup_destination_available,
                backup_space_sufficient=backup_space_known,
                track_mapping_resolved=True,
                existing_names=existing_names,
            ),
            capacity=FileCapacitySnapshot(
                source_size_bytes=source_size or 0,
                temporary_storage=safe_temporary_storage,
                backup_storage=backup_storage,
                backup_target=backup_target,
            ),
        )


class ApplyPreflightInspector(Protocol):
    """Resolve current filesystem facts without mutating selected media."""

    def inspect(
        self,
        source: LocalMediaFile,
        proposed_changes: FileChangeSet,
        backup: BackupPolicy,
    ) -> FilePreflightSnapshot: ...


class ApplyFileWriter(Protocol):
    """Transactional writer boundary used after every preflight succeeds."""

    def apply_file(
        self,
        source: LocalMediaFile,
        changes: FileChangeSet,
        adapter: MediaFormatAdapter,
        backup: BackupPolicy,
        cancellation: CancellationToken,
        events: OperationEventSink,
    ) -> FileApplyResult: ...


class ApplyReportWriter(Protocol):
    """Optional processing-report boundary kept separate from audio results."""

    def write(
        self,
        *,
        policy: ReportOutputPolicy,
        request: ProcessingReportRequest,
    ) -> ReportWriteResult: ...


@dataclass(frozen=True)
class ApplyFileOutcome:
    """Reviewed plan plus the actual transaction or explicit non-write result."""

    file_id: str
    source_path: Path
    selected_release: SelectedReleaseReference | None
    reviews: tuple[FieldReviewState, ...]
    change_set: FileChangeSet
    status: ApplyFileOutcomeStatus
    transaction_result: FileApplyResult | None = None
    skip_reason: ApplySkipReason | None = None
    reason_codes: tuple[ReportReasonCode, ...] = ()
    refreshed_source: LocalMediaFile | None = None
    refresh_issues: tuple[Issue, ...] = ()

    def __post_init__(self) -> None:
        _validate_identifier("file_id", self.file_id)

        if not isinstance(self.source_path, Path):
            raise TypeError("source_path must be a Path")

        if self.selected_release is not None and not isinstance(
            self.selected_release,
            SelectedReleaseReference,
        ):
            raise TypeError("selected_release must be a SelectedReleaseReference or None")

        reviews = _copy_sequence("reviews", self.reviews, FieldReviewState)

        if tuple(review.field for review in reviews) != tuple(MetadataField):
            raise ValueError("reviews must contain every MetadataField in declaration order")

        if not isinstance(self.change_set, FileChangeSet):
            raise TypeError("change_set must be a FileChangeSet")

        if self.change_set.file_id != self.file_id:
            raise ValueError("change_set must refer to file_id")

        for rename in (self.change_set.rename_preview, self.change_set.rename_change):
            if rename is not None and rename.old_path != self.source_path:
                raise ValueError("rename source must match source_path")

        if not isinstance(self.status, ApplyFileOutcomeStatus):
            raise TypeError("status must be an ApplyFileOutcomeStatus")

        # An outcome claiming to have entered a transaction must carry matching
        # writer evidence. Skips and no-ops instead explain why no writer ran.
        if self.status in {
            ApplyFileOutcomeStatus.APPLIED,
            ApplyFileOutcomeStatus.FAILED,
            ApplyFileOutcomeStatus.CANCELLED,
        }:
            if not isinstance(self.transaction_result, FileApplyResult):
                raise ValueError("a transactional outcome requires a FileApplyResult")

            expected_status = {
                ApplyFileOutcomeStatus.APPLIED: FileApplyStatus.SUCCEEDED,
                ApplyFileOutcomeStatus.FAILED: FileApplyStatus.FAILED,
                ApplyFileOutcomeStatus.CANCELLED: FileApplyStatus.CANCELLED,
            }[self.status]

            if self.transaction_result.status is not expected_status:
                raise ValueError("transaction status does not match the outcome status")

            if self.transaction_result.source_path != self.source_path:
                raise ValueError("transaction source_path must match outcome source_path")

            if not (
                self.change_set.metadata_changes
                or self.change_set.rename_change is not None
            ):
                raise ValueError("a transactional outcome requires a real operation")

            if self.change_set.status is ChangeSetStatus.BLOCKED:
                raise ValueError("a blocked change set cannot enter a transaction")

            if self.skip_reason is not None:
                raise ValueError("a transactional outcome cannot have a skip_reason")
        elif self.transaction_result is not None:
            raise ValueError("a non-transactional outcome cannot contain a FileApplyResult")

        if self.status is ApplyFileOutcomeStatus.SKIPPED:
            if not isinstance(self.skip_reason, ApplySkipReason):
                raise ValueError("a skipped outcome requires a skip_reason")

            if self.skip_reason is ApplySkipReason.NO_CHANGES:
                raise ValueError("NO_CHANGES uses its explicit outcome status")
        elif self.skip_reason is not None:
            raise ValueError("only a skipped outcome may have a skip_reason")

        real_operation = bool(
            self.change_set.metadata_changes or self.change_set.rename_change is not None
        )

        if self.status is ApplyFileOutcomeStatus.NO_CHANGES and (
            real_operation or self.change_set.status is ChangeSetStatus.BLOCKED
        ):
            raise ValueError("a NO_CHANGES outcome cannot contain a requested operation")

        if (
            self.status is ApplyFileOutcomeStatus.SKIPPED
            and self.skip_reason is not ApplySkipReason.CANCELLED_BEFORE_START
            and not (real_operation or self.change_set.status is ChangeSetStatus.BLOCKED)
        ):
            raise ValueError("a skipped outcome requires a requested operation")

        if self.transaction_result is not None:
            result = self.transaction_result
            destination = (
                self.change_set.rename_change.new_path
                if self.change_set.rename_change is not None
                else self.source_path
            )

            if self.status is ApplyFileOutcomeStatus.APPLIED:
                if result.completed_stage is not FileTransactionStage.COMPLETED:
                    raise ValueError("an applied transaction must have completed")

                if result.final_path != destination:
                    raise ValueError("an applied transaction has an unexpected final path")
            elif self.status is ApplyFileOutcomeStatus.CANCELLED:
                cancellable_stages = {
                    FileTransactionStage.NOT_STARTED,
                    FileTransactionStage.BACKING_UP,
                    FileTransactionStage.COPYING_TEMPORARY,
                    FileTransactionStage.WRITING_METADATA,
                    FileTransactionStage.VERIFYING_TEMPORARY,
                }

                if (
                    result.completed_stage not in cancellable_stages
                    or result.final_path != self.source_path
                ):
                    raise ValueError("a cancelled transaction must retain its source path")
            elif (
                result.completed_stage is FileTransactionStage.COMPLETED
                or result.final_path not in {self.source_path, destination}
            ):
                raise ValueError("a failed transaction has inconsistent stage or final path")

            if (
                self.status is ApplyFileOutcomeStatus.FAILED
                and result.final_path == destination
                and destination != self.source_path
                and result.completed_stage
                not in {
                    FileTransactionStage.VERIFYING_FINAL,
                    FileTransactionStage.CLEANING_ORIGINAL,
                }
            ):
                raise ValueError(
                    "a failed transaction at the rename destination requires a post-commit stage"
                )

        object.__setattr__(self, "reviews", reviews)
        object.__setattr__(self, "reason_codes", _copy_reason_codes(self.reason_codes))
        refresh_issues = _copy_sequence("refresh_issues", self.refresh_issues, Issue)

        if self.refreshed_source is not None:
            if not isinstance(self.refreshed_source, LocalMediaFile):
                raise TypeError("refreshed_source must be a LocalMediaFile or None")

            if (
                self.status is not ApplyFileOutcomeStatus.APPLIED
                or self.refreshed_source.file_id != self.file_id
                or self.refreshed_source.path != self.final_path
                or refresh_issues
            ):
                raise ValueError("a refresh receipt must describe this successfully written final file")

        if refresh_issues and self.status is not ApplyFileOutcomeStatus.APPLIED:
            raise ValueError("only a successful write may have a separate refresh failure")

        object.__setattr__(self, "refresh_issues", refresh_issues)

    @property
    def final_path(self) -> Path:
        """Return the last path reported by a transaction, or the untouched source."""
        if self.transaction_result is None:
            return self.source_path

        return self.transaction_result.final_path

    @property
    def filesystem_changed(self) -> bool:
        """Whether a later reducer must conservatively rescan this source group."""
        if self.transaction_result is None:
            return False

        return (
            self.transaction_result.status is FileApplyStatus.SUCCEEDED
            or self.transaction_result.final_path != self.source_path
            or any(
                issue.code is MediaErrorCode.CLEANUP_FAILED
                for issue in self.transaction_result.issues
            )
        )

    @property
    def requires_rescan(self) -> bool:
        """A verified reread resolves the snapshot only for matching session lineage."""
        return self.filesystem_changed and self.refreshed_source is None


@dataclass(frozen=True)
class ApplyGroupOutcome:
    """Ordered file truth for one group at its captured revision."""

    group_id: str
    base_group_revision: int
    files: tuple[ApplyFileOutcome, ...]

    def __post_init__(self) -> None:
        _validate_identifier("group_id", self.group_id)
        _validate_revision("base_group_revision", self.base_group_revision)
        files = _copy_sequence("files", self.files, ApplyFileOutcome)

        if not files:
            raise ValueError("an Apply group outcome must contain at least one file")

        if len(files) != len({file.file_id for file in files}):
            raise ValueError("group outcome file IDs must be unique")

        if len(files) != len({_windows_path_key(file.source_path) for file in files}):
            raise ValueError("group outcome source paths must be unique")

        failure_seen = False

        for file in files:
            if failure_seen and file.transaction_result is not None:
                raise ValueError("a failed file must stop later transactions in its group")

            if (
                file.skip_reason is ApplySkipReason.ALBUM_STOPPED_AFTER_FAILURE
                and not failure_seen
            ):
                raise ValueError("album-stop skips require an earlier failed file in the group")

            if file.status is ApplyFileOutcomeStatus.FAILED:
                failure_seen = True

        object.__setattr__(self, "files", files)


def _derive_batch_status(groups: Sequence[ApplyGroupOutcome]) -> ApplyBatchStatus:
    # Derive the summary from file outcomes, not the token's final value. A token
    # set after the last successful file must not relabel completed work cancelled.
    files = tuple(file for group in groups for file in group.files)

    if any(
        file.status is ApplyFileOutcomeStatus.CANCELLED
        or file.skip_reason is ApplySkipReason.CANCELLED_BEFORE_START
        for file in files
    ):
        return ApplyBatchStatus.CANCELLED

    failure_present = any(
        file.status is ApplyFileOutcomeStatus.FAILED
        or file.skip_reason
        in {
            ApplySkipReason.BATCH_PREFLIGHT_BLOCKED,
            ApplySkipReason.ALBUM_STOPPED_AFTER_FAILURE,
        }
        for file in files
    )

    if failure_present:
        if any(file.status is ApplyFileOutcomeStatus.APPLIED for file in files):
            return ApplyBatchStatus.PARTIAL

        return ApplyBatchStatus.FAILED

    return ApplyBatchStatus.SUCCEEDED


@dataclass(frozen=True)
class ApplyBatchResult:
    """Ordered, stale-safe worker result retaining every selected file outcome."""

    operation_id: str
    base_session_revision: int
    base_library_revision: int
    status: ApplyBatchStatus
    groups: tuple[ApplyGroupOutcome, ...]
    report_result: ReportWriteResult

    def __post_init__(self) -> None:
        _validate_identifier("operation_id", self.operation_id)
        _validate_revision("base_session_revision", self.base_session_revision)
        _validate_revision("base_library_revision", self.base_library_revision)

        if self.base_library_revision > self.base_session_revision:
            raise ValueError("base_library_revision cannot exceed base_session_revision")

        if not isinstance(self.status, ApplyBatchStatus):
            raise TypeError("status must be an ApplyBatchStatus")

        groups = _copy_sequence("groups", self.groups, ApplyGroupOutcome)

        if not groups:
            raise ValueError("groups must contain at least one ApplyGroupOutcome")

        if len(groups) != len({group.group_id for group in groups}):
            raise ValueError("result group IDs must be unique")

        if any(group.base_group_revision > self.base_session_revision for group in groups):
            raise ValueError("base_group_revision cannot exceed base_session_revision")

        files = tuple(file for group in groups for file in group.files)

        if len(files) != len({file.file_id for file in files}):
            raise ValueError("result file IDs must be globally unique")

        if len(files) != len({_windows_path_key(file.source_path) for file in files}):
            raise ValueError("result source paths must be globally unique")

        # Batch preflight happens before all writes. A result containing both a
        # preflight blocker and a completed transaction would be impossible.
        blocked_files = tuple(
            file for file in files if file.change_set.status is ChangeSetStatus.BLOCKED
        )
        preflight_skipped = tuple(
            file
            for file in files
            if file.skip_reason is ApplySkipReason.BATCH_PREFLIGHT_BLOCKED
        )

        if blocked_files:
            if any(file.transaction_result is not None for file in files):
                raise ValueError("a blocked batch cannot contain transaction results")

            for file in files:
                requested = bool(
                    file.change_set.metadata_changes
                    or file.change_set.rename_change is not None
                    or file.change_set.status is ChangeSetStatus.BLOCKED
                )

                if requested and file.skip_reason is not ApplySkipReason.BATCH_PREFLIGHT_BLOCKED:
                    raise ValueError(
                        "a blocked batch requires BATCH_PREFLIGHT_BLOCKED for every request"
                    )

                if not requested and file.status is not ApplyFileOutcomeStatus.NO_CHANGES:
                    raise ValueError("a blocked batch must preserve true no-change outcomes")

        if preflight_skipped:
            if not any(file.change_set.status is ChangeSetStatus.BLOCKED for file in files):
                raise ValueError("a batch preflight blocker is required for these skips")

            if any(file.transaction_result is not None for file in files):
                raise ValueError("a blocked batch cannot contain transaction results")

        # Walk in execution order to validate causality: once cancellation has
        # stopped the batch, every later file must be recorded as never started.
        safe_cancellation_boundary_seen = False
        cancellation_seen = False

        for file in files:
            if cancellation_seen and not (
                file.status is ApplyFileOutcomeStatus.SKIPPED
                and file.skip_reason is ApplySkipReason.CANCELLED_BEFORE_START
            ):
                raise ValueError("no transaction or non-cancel outcome may run after cancellation")

            if (
                file.skip_reason is ApplySkipReason.CANCELLED_BEFORE_START
                and not (
                    file.change_set.metadata_changes
                    or file.change_set.rename_change is not None
                    or file.change_set.status is ChangeSetStatus.BLOCKED
                )
                and not safe_cancellation_boundary_seen
            ):
                raise ValueError(
                    "cancelling a no-change file requires an earlier transaction or cancellation"
                )

            if (
                file.transaction_result is not None
                or file.skip_reason is ApplySkipReason.CANCELLED_BEFORE_START
            ):
                safe_cancellation_boundary_seen = True

            if (
                file.status is ApplyFileOutcomeStatus.CANCELLED
                or file.skip_reason is ApplySkipReason.CANCELLED_BEFORE_START
            ):
                cancellation_seen = True

        if self.status is not _derive_batch_status(groups):
            raise ValueError("status must equal the result derived from file outcomes")

        if not isinstance(self.report_result, ReportWriteResult):
            raise TypeError("report_result must be a ReportWriteResult")

        object.__setattr__(self, "groups", groups)


@dataclass(frozen=True)
class _DraftFile:
    request: ApplyFileRequest
    change_set: FileChangeSet
    validation: ChangeValidationFacts


@dataclass(frozen=True)
class _PreparedFile:
    request: ApplyFileRequest
    change_set: FileChangeSet
    adapter: MediaFormatAdapter | None
    validation: ChangeValidationFacts
    capacity: FileCapacitySnapshot | None


class _DiscardEventSink:
    def emit(self, event: OperationEvent) -> None:
        del event


class ApplyService:
    """Preflight an entire batch, then run file-atomic transactions in order."""

    def __init__(
        self,
        *,
        preflight: ApplyPreflightInspector,
        writer: ApplyFileWriter,
        report_writer: ApplyReportWriter | None = None,
    ) -> None:
        self._preflight = preflight
        self._writer = writer
        self._report_writer = report_writer

    def apply(
        self,
        request: ApplyBatchRequest,
        *,
        cancellation: CancellationToken,
        events: OperationEventSink | None = None,
    ) -> ApplyBatchResult:
        """Apply one captured selection without reading mutable session state."""
        if not isinstance(request, ApplyBatchRequest):
            raise TypeError("request must be an ApplyBatchRequest")

        active_events = events if events is not None else _DiscardEventSink()
        total_files = sum(len(group.files) for group in request.groups)
        active_events.emit(OperationStageChanged(request.operation_id, ApplyStage.PREFLIGHTING))
        active_events.emit(OperationProgress(request.operation_id, ApplyStage.PREFLIGHTING, 0, total_files))
        draft_groups: list[tuple[ApplyGroupRequest, tuple[_DraftFile, ...]]] = []

        # Derive every pure draft before the first external probe. This preserves
        # no-change truth even if cancellation stops preflight part-way through.
        for group in request.groups:
            drafts: list[_DraftFile] = []

            for file_request in group.files:
                initial_facts = ChangeValidationFacts(
                    track_mapping_resolved=file_request.track_mapping_resolved,
                )
                proposed = build_change_set(
                    file_request.source,
                    file_request.reviews,
                    file_request.rename_decision,
                    request.rename_template,
                    request.rename_policy,
                    initial_facts,
                )
                drafts.append(_DraftFile(file_request, proposed, initial_facts))

            draft_groups.append((group, tuple(drafts)))

        prepared_groups: list[tuple[ApplyGroupRequest, tuple[_PreparedFile, ...]]] = []
        prepared_by_file_id: dict[str, _PreparedFile] = {}
        prepared_count = 0
        preflight_cancelled = False

        for group, group_drafts in draft_groups:
            prepared_files: list[_PreparedFile] = []

            for draft in group_drafts:
                file_request = draft.request
                proposed = draft.change_set
                operation_requested = self._needs_external_preflight(proposed)

                if operation_requested:
                    if cancellation.is_cancelled():
                        preflight_cancelled = True

                        break

                    snapshot = self._preflight.inspect(
                        file_request.source,
                        proposed,
                        request.backup,
                    )

                    if snapshot.file_id != file_request.source.file_id:
                        raise ValueError("preflight snapshot must refer to the inspected file")

                    fresh_facts = replace(
                        snapshot.validation,
                        track_mapping_resolved=file_request.track_mapping_resolved,
                        backup_root_writable=(
                            snapshot.validation.backup_root_writable
                            if request.backup.enabled
                            else True
                        ),
                        backup_destination_available=(
                            snapshot.validation.backup_destination_available
                            if request.backup.enabled
                            else True
                        ),
                        backup_space_sufficient=(
                            snapshot.validation.backup_space_sufficient
                            if request.backup.enabled
                            else True
                        ),
                    )
                    change_set = build_change_set(
                        file_request.source,
                        file_request.reviews,
                        file_request.rename_decision,
                        request.rename_template,
                        request.rename_policy,
                        fresh_facts,
                    )
                    adapter = snapshot.adapter
                else:
                    change_set = proposed
                    adapter = None
                    fresh_facts = draft.validation
                    capacity = None

                if operation_requested:
                    capacity = snapshot.capacity

                prepared = _PreparedFile(
                    file_request,
                    change_set,
                    adapter,
                    fresh_facts,
                    capacity,
                )
                prepared_files.append(prepared)
                prepared_by_file_id[file_request.source.file_id] = prepared
                prepared_count += 1
                active_events.emit(
                    OperationProgress(
                        request.operation_id,
                        ApplyStage.PREFLIGHTING,
                        prepared_count,
                        total_files,
                    )
                )

                if cancellation.is_cancelled() and prepared_count < total_files:
                    preflight_cancelled = True

                    break

            prepared_groups.append((group, tuple(prepared_files)))

            if preflight_cancelled:
                break

        if preflight_cancelled:
            cancellation_groups = [
                (
                    group,
                    tuple(
                        prepared_by_file_id.get(
                            draft.request.source.file_id,
                            _PreparedFile(
                                draft.request,
                                draft.change_set,
                                None,
                                draft.validation,
                                None,
                            ),
                        )
                        for draft in drafts
                    ),
                )
                for group, drafts in draft_groups
            ]
            return self._finish_without_transactions(
                request,
                cancellation_groups,
                batch_blocked=any(
                    prepared.change_set.status is ChangeSetStatus.BLOCKED
                    for _group, prepared_files in cancellation_groups
                    for prepared in prepared_files
                ),
                events=active_events,
            )

        # Individual files may pass while the batch still collides or exhausts
        # backup space. Resolve shared constraints before entering any writer.
        prepared_groups = self._apply_batch_capacity(request, prepared_groups)
        prepared_groups = self._apply_batch_path_collisions(request, prepared_groups)
        batch_blocked = any(
            prepared.change_set.status is ChangeSetStatus.BLOCKED
            for _group, prepared_files in prepared_groups
            for prepared in prepared_files
        )

        requested_operation_present = any(
            self._has_file_operation(prepared.change_set)
            for _group, prepared_files in prepared_groups
            for prepared in prepared_files
        )

        if (
            batch_blocked
            or cancellation.is_cancelled()
            or not requested_operation_present
        ):
            return self._finish_without_transactions(
                request,
                prepared_groups,
                batch_blocked=batch_blocked,
                events=active_events,
            )

        active_events.emit(OperationStageChanged(request.operation_id, ApplyStage.APPLYING_FILES))
        active_events.emit(OperationProgress(request.operation_id, ApplyStage.APPLYING_FILES, 0, total_files))
        completed_count = 0
        group_outcomes: list[ApplyGroupOutcome] = []
        cancelled = False
        transaction_seen = False

        # A failure stops the remaining writes in that album only. Cancellation
        # persists across albums, while successful earlier files remain committed.
        for group, prepared_batch_files in prepared_groups:
            file_outcomes: list[ApplyFileOutcome] = []
            album_failed = False

            for prepared in prepared_batch_files:
                if not self._has_file_operation(prepared.change_set):
                    if cancelled or (
                        transaction_seen and cancellation.is_cancelled()
                    ):
                        cancelled = True
                        outcome = self._skipped(
                            group,
                            prepared,
                            ApplySkipReason.CANCELLED_BEFORE_START,
                        )
                    else:
                        outcome = self._no_changes(group, prepared)
                elif cancelled or cancellation.is_cancelled():
                    cancelled = True
                    outcome = self._skipped(
                        group,
                        prepared,
                        ApplySkipReason.CANCELLED_BEFORE_START,
                    )
                elif album_failed:
                    outcome = self._skipped(
                        group,
                        prepared,
                        ApplySkipReason.ALBUM_STOPPED_AFTER_FAILURE,
                    )
                else:
                    if prepared.adapter is None:
                        raise ValueError("a writable prepared file requires an adapter")

                    # The writer owns cancellation inside this file transaction;
                    # this loop checks the token again before starting the next.
                    transaction = self._writer.apply_file(
                        prepared.request.source,
                        prepared.change_set,
                        prepared.adapter,
                        request.backup,
                        cancellation,
                        active_events,
                    )
                    transaction_seen = True
                    self._validate_writer_result(prepared, transaction)
                    outcome = self._transaction_outcome(group, prepared, transaction)

                    if transaction.status is FileApplyStatus.FAILED:
                        album_failed = True

                    if transaction.status is FileApplyStatus.CANCELLED:
                        cancelled = True

                file_outcomes.append(outcome)

                if outcome.status is ApplyFileOutcomeStatus.NO_CHANGES:
                    active_events.emit(
                        FileSkipped(
                            request.operation_id,
                            outcome.file_id,
                            outcome.source_path,
                            FileSkipReason.NO_CHANGES,
                        )
                    )
                elif outcome.status is ApplyFileOutcomeStatus.SKIPPED:
                    assert outcome.skip_reason is not None
                    active_events.emit(
                        FileSkipped(
                            request.operation_id,
                            outcome.file_id,
                            outcome.source_path,
                            outcome.skip_reason,
                        )
                    )

                # Progress counts resolved outcomes, including skipped files,
                # rather than claiming that every completed item was written.
                completed_count += 1
                active_events.emit(
                    OperationProgress(
                        request.operation_id,
                        ApplyStage.APPLYING_FILES,
                        completed_count,
                        total_files,
                    )
                )

            group_outcomes.append(
                ApplyGroupOutcome(group.group_id, group.base_group_revision, tuple(file_outcomes))
            )

        # Populate the result from an actual reread after commit. Proposed final
        # values alone cannot prove what the adapter persisted on disk.
        group_outcomes = self._refresh_written_files(request, group_outcomes, prepared_by_file_id, active_events)
        status = _derive_batch_status(group_outcomes)
        # Optional report failure is separate from audio-write success; deriving
        # status first prevents a logging problem from rewriting transaction truth.
        report_result = self._write_report(
            request,
            tuple(group_outcomes),
            status,
            active_events,
        )

        return ApplyBatchResult(
            operation_id=request.operation_id,
            base_session_revision=request.base_session_revision,
            base_library_revision=request.base_library_revision,
            status=status,
            groups=tuple(group_outcomes),
            report_result=report_result,
        )

    @staticmethod
    def _refresh_written_files(
        request: ApplyBatchRequest,
        groups: list[ApplyGroupOutcome],
        prepared: dict[str, _PreparedFile],
        events: OperationEventSink,
    ) -> list[ApplyGroupOutcome]:
        """Read completed final paths without rewriting or reopening later files.

        Cancellation stops further transactions, but cannot undo an already
        committed file. Its bounded local reread therefore still runs so that
        the UI receives actual disk metadata instead of the proposed values.
        """
        total = sum(outcome.status is ApplyFileOutcomeStatus.APPLIED for group in groups for outcome in group.files)

        if not total:
            return groups

        events.emit(OperationStageChanged(request.operation_id, ApplyStage.REFRESHING_FILES))
        events.emit(OperationProgress(request.operation_id, ApplyStage.REFRESHING_FILES, 0, total))
        updated: list[ApplyGroupOutcome] = []
        completed = 0

        for group in groups:
            files: list[ApplyFileOutcome] = []

            for outcome in group.files:
                if outcome.status is not ApplyFileOutcomeStatus.APPLIED:
                    files.append(outcome)
                    continue

                original = prepared[outcome.file_id]

                try:
                    assert original.adapter is not None
                    read_result = original.adapter.read(outcome.final_path)

                    if not isinstance(read_result, MediaReadResult):
                        raise TypeError("the format adapter did not return a metadata read result")

                    refreshed = replace(
                        original.request.source,
                        path=outcome.final_path,
                        read_result=read_result,
                        filename_hints=extract_filename_hints(outcome.final_path),
                    )
                    files.append(replace(outcome, refreshed_source=refreshed))
                except Exception as error:
                    # Refresh/report diagnostics never relabel a completed disk
                    # transaction as failed. The reducer keeps its source stale.
                    detail = (
                        error.issue.technical_detail if isinstance(error, MediaFormatError)
                        else type(error).__name__
                    )
                    issue = Issue(
                        MediaErrorCode.TAG_READ_FAILED,
                        "The file was written, but its metadata could not be refreshed. Rescan this file.",
                        technical_detail=detail,
                    )
                    files.append(replace(outcome, refresh_issues=(issue,)))

                completed += 1
                events.emit(OperationProgress(request.operation_id, ApplyStage.REFRESHING_FILES, completed, total))

            updated.append(replace(group, files=tuple(files)))

        return updated

    @staticmethod
    def _has_file_operation(change_set: FileChangeSet) -> bool:
        return (
            change_set.status is ChangeSetStatus.BLOCKED
            or bool(change_set.metadata_changes or change_set.rename_change)
        )

    @staticmethod
    def _needs_external_preflight(change_set: FileChangeSet) -> bool:
        return bool(change_set.metadata_changes or change_set.rename_change)

    def _finish_without_transactions(
        self,
        request: ApplyBatchRequest,
        groups: Sequence[tuple[ApplyGroupRequest, tuple[_PreparedFile, ...]]],
        *,
        batch_blocked: bool,
        events: OperationEventSink,
    ) -> ApplyBatchResult:
        """Project preflight/cancellation truth without entering the writer stage."""
        skip_reason = (
            ApplySkipReason.BATCH_PREFLIGHT_BLOCKED
            if batch_blocked
            else ApplySkipReason.CANCELLED_BEFORE_START
        )
        group_outcomes = tuple(
            ApplyGroupOutcome(
                group.group_id,
                group.base_group_revision,
                tuple(
                    self._no_changes(group, prepared)
                    if not self._has_file_operation(prepared.change_set)
                    else self._skipped(group, prepared, skip_reason)
                    for prepared in prepared_files
                ),
            )
            for group, prepared_files in groups
        )
        status = _derive_batch_status(group_outcomes)
        self._emit_non_transaction_events(request.operation_id, group_outcomes, events)
        report_result = self._write_report(request, group_outcomes, status, events)

        return ApplyBatchResult(
            operation_id=request.operation_id,
            base_session_revision=request.base_session_revision,
            base_library_revision=request.base_library_revision,
            status=status,
            groups=group_outcomes,
            report_result=report_result,
        )

    @staticmethod
    def _emit_non_transaction_events(
        operation_id: str,
        groups: Sequence[ApplyGroupOutcome],
        events: OperationEventSink,
    ) -> None:
        for group in groups:
            for outcome in group.files:
                if outcome.status is ApplyFileOutcomeStatus.NO_CHANGES:
                    reason = FileSkipReason.NO_CHANGES
                elif outcome.status is ApplyFileOutcomeStatus.SKIPPED:
                    assert outcome.skip_reason is not None
                    reason = outcome.skip_reason
                else:
                    continue

                events.emit(
                    FileSkipped(
                        operation_id,
                        outcome.file_id,
                        outcome.source_path,
                        reason,
                    )
                )

    def _write_report(
        self,
        request: ApplyBatchRequest,
        groups: tuple[ApplyGroupOutcome, ...],
        status: ApplyBatchStatus,
        events: OperationEventSink,
    ) -> ReportWriteResult:
        if not request.report.enabled:
            return ReportWriteResult(ReportWriteStatus.DISABLED, None, None)

        events.emit(OperationStageChanged(request.operation_id, ApplyStage.WRITING_REPORT))
        events.emit(OperationProgress(request.operation_id, ApplyStage.WRITING_REPORT, 0, 1))

        try:
            report_request = ProcessingReportRequest(
                operation_id=request.operation_id,
                result=ReportOperationResult(status.value),
                files=tuple(
                    self._report_entry(file)
                    for group in groups
                    for file in group.files
                ),
            )
        except Exception:
            # The transaction result remains authoritative even if malformed
            # lower-boundary data cannot be projected into the safe schema.
            LOGGER.exception("The optional Apply report could not be constructed safely.")
            result = ReportWriteResult(
                ReportWriteStatus.FAILED,
                None,
                ReportErrorCode.INVALID_REPORT,
            )
        else:
            if self._report_writer is None:
                result = ReportWriteResult(
                    ReportWriteStatus.FAILED,
                    None,
                    ReportErrorCode.INVALID_REPORT,
                )
            else:
                try:
                    result = self._report_writer.write(
                        policy=request.report,
                        request=report_request,
                    )

                    if not isinstance(result, ReportWriteResult):
                        raise TypeError("report writer must return a ReportWriteResult")
                except Exception:
                    # A report is secondary audit output. An unexpected boundary
                    # failure must never erase or relabel completed audio outcomes.
                    LOGGER.exception("The optional Apply report writer failed unexpectedly.")
                    result = ReportWriteResult(
                        ReportWriteStatus.FAILED,
                        None,
                        ReportErrorCode.WRITE_FAILED,
                    )

        events.emit(OperationProgress(request.operation_id, ApplyStage.WRITING_REPORT, 1, 1))

        return result

    @staticmethod
    def _report_entry(outcome: ApplyFileOutcome) -> ReportFileEntry:
        if outcome.transaction_result is not None:
            return ReportFileEntry.from_apply_result(
                selected_release=outcome.selected_release,
                change_set=outcome.change_set,
                reviews=outcome.reviews,
                apply_result=outcome.transaction_result,
                reason_codes=outcome.reason_codes,
            )

        return ReportFileEntry.from_skipped(
            selected_release=outcome.selected_release,
            source_path=outcome.source_path,
            change_set=outcome.change_set,
            reviews=outcome.reviews,
            skip_reason=(
                FileSkipReason.NO_CHANGES
                if outcome.status is ApplyFileOutcomeStatus.NO_CHANGES
                else cast(FileSkipReason, outcome.skip_reason)
            ),
            reason_codes=outcome.reason_codes,
        )

    @staticmethod
    def _apply_batch_capacity(
        request: ApplyBatchRequest,
        groups: Sequence[tuple[ApplyGroupRequest, tuple[_PreparedFile, ...]]],
    ) -> list[tuple[ApplyGroupRequest, tuple[_PreparedFile, ...]]]:
        """Evaluate serial temp peak and cumulative backups from one frozen snapshot."""
        operation_files = tuple(
            prepared
            for _group, prepared_files in groups
            for prepared in prepared_files
            if ApplyService._needs_external_preflight(prepared.change_set)
        )
        free_by_storage: dict[str, int] = {}
        temporary_required: dict[str, int] = {}
        backup_required: dict[str, int] = {}
        backup_target_counts: dict[tuple[str, ...], int] = {}

        for prepared in operation_files:
            capacity = prepared.capacity

            if capacity is None:
                raise ValueError("an operation-bearing preflight requires capacity evidence")

            temporary = capacity.temporary_storage
            # Multiple probes of one volume may disagree as free space changes.
            # Use the smallest observation rather than adding those observations.
            free_by_storage[temporary.storage_id] = min(
                free_by_storage.get(temporary.storage_id, temporary.free_bytes),
                temporary.free_bytes,
            )
            # Transactions run serially, so only the largest sibling copy is
            # needed at once: sizes 10, 20 and 30 require a temporary peak of 30.
            temporary_required[temporary.storage_id] = max(
                temporary_required.get(temporary.storage_id, 0),
                capacity.source_size_bytes,
            )

            if request.backup.enabled:
                if capacity.backup_storage is None or capacity.backup_target is None:
                    raise ValueError("enabled backup preflight requires storage and target evidence")

                backup = capacity.backup_storage
                free_by_storage[backup.storage_id] = min(
                    free_by_storage.get(backup.storage_id, backup.free_bytes),
                    backup.free_bytes,
                )
                # Backups remain after each file finishes. For the same three
                # sizes their requirement is cumulative: 10 + 20 + 30 = 60.
                backup_required[backup.storage_id] = (
                    backup_required.get(backup.storage_id, 0)
                    + capacity.source_size_bytes
                )
                target_key = _windows_path_key(capacity.backup_target)
                backup_target_counts[target_key] = backup_target_counts.get(target_key, 0) + 1
            elif capacity.backup_storage is not None or capacity.backup_target is not None:
                raise ValueError("disabled backup preflight cannot contain backup capacity evidence")

        temporary_insufficient = {
            storage_id
            for storage_id, required in temporary_required.items()
            if required > free_by_storage[storage_id]
        }
        backup_insufficient = {
            storage_id
            for storage_id, required in backup_required.items()
            if required > free_by_storage[storage_id]
        }

        # If temporary copies and backups share a volume, reserve both budgets
        # together. Passing each budget separately would overcommit free space.
        for storage_id in temporary_required.keys() & backup_required.keys():
            combined_required = (
                temporary_required[storage_id] + backup_required[storage_id]
            )

            if combined_required > free_by_storage[storage_id]:
                backup_insufficient.add(storage_id)

        rebuilt_by_file_id: dict[str, _PreparedFile] = {}

        for _group, prepared_files in groups:
            for prepared in prepared_files:
                if not ApplyService._needs_external_preflight(prepared.change_set):
                    rebuilt_by_file_id[prepared.request.source.file_id] = prepared

                    continue

                assert prepared.capacity is not None
                capacity = prepared.capacity
                temporary_ok = (
                    capacity.temporary_storage.storage_id not in temporary_insufficient
                )
                backup_ok = True
                target_ok = True

                if request.backup.enabled:
                    assert capacity.backup_storage is not None
                    assert capacity.backup_target is not None
                    backup_ok = capacity.backup_storage.storage_id not in backup_insufficient
                    target_ok = (
                        backup_target_counts[_windows_path_key(capacity.backup_target)] == 1
                    )

                validation = replace(
                    prepared.validation,
                    temporary_space_sufficient=(
                        prepared.validation.temporary_space_sufficient and temporary_ok
                    ),
                    backup_destination_available=(
                        prepared.validation.backup_destination_available and target_ok
                    ),
                    backup_space_sufficient=(
                        prepared.validation.backup_space_sufficient and backup_ok
                    ),
                )
                change_set = build_change_set(
                    prepared.request.source,
                    prepared.request.reviews,
                    prepared.request.rename_decision,
                    request.rename_template,
                    request.rename_policy,
                    validation,
                )
                rebuilt_by_file_id[prepared.request.source.file_id] = replace(
                    prepared,
                    change_set=change_set,
                    validation=validation,
                )

        return [
            (
                group,
                tuple(rebuilt_by_file_id[item.request.source.file_id] for item in prepared_files),
            )
            for group, prepared_files in groups
        ]

    @staticmethod
    def _apply_batch_path_collisions(
        request: ApplyBatchRequest,
        groups: Sequence[tuple[ApplyGroupRequest, tuple[_PreparedFile, ...]]],
    ) -> list[tuple[ApplyGroupRequest, tuple[_PreparedFile, ...]]]:
        """Rebuild with selected source/target names visible as one Windows batch."""
        indexed = tuple(
            prepared
            for _group, prepared_files in groups
            for prepared in prepared_files
        )
        rebuilt_by_file_id: dict[str, _PreparedFile] = {}

        for prepared in indexed:
            preview = prepared.change_set.rename_preview

            if preview is None or prepared.request.rename_decision is not RenameDecision.APPLY_RENAME:
                rebuilt_by_file_id[prepared.request.source.file_id] = prepared

                continue

            is_case_only_rename = (
                preview.old_path.name != preview.new_path.name
                and windows_collision_key(preview.old_path.name)
                == windows_collision_key(preview.new_path.name)
            )
            parent_key = _windows_path_key(preview.new_path.parent)
            # Reserve original names as well as other planned destinations.
            # A later rename must not depend on an earlier file vacating its name.
            batch_names = [
                other.request.source.path.name
                for other in indexed
                if _windows_path_key(other.request.source.path.parent) == parent_key
            ]
            batch_names.extend(
                other.change_set.rename_preview.new_path.name
                for other in indexed
                if other is not prepared
                and other.change_set.rename_preview is not None
                and other.request.rename_decision is RenameDecision.APPLY_RENAME
                and _windows_path_key(other.change_set.rename_preview.new_path.parent) == parent_key
            )
            existing_names = list(prepared.validation.existing_names)

            for name in batch_names:
                if name not in existing_names:
                    existing_names.append(name)

            validation = replace(
                prepared.validation,
                case_only_rename_supported=(
                    prepared.validation.case_only_rename_supported
                    and not is_case_only_rename
                ),
                existing_names=tuple(existing_names),
            )
            change_set = build_change_set(
                prepared.request.source,
                prepared.request.reviews,
                prepared.request.rename_decision,
                request.rename_template,
                request.rename_policy,
                validation,
            )
            rebuilt_by_file_id[prepared.request.source.file_id] = replace(
                prepared,
                change_set=change_set,
                validation=validation,
            )

        return [
            (
                group,
                tuple(rebuilt_by_file_id[item.request.source.file_id] for item in prepared_files),
            )
            for group, prepared_files in groups
        ]

    @staticmethod
    def _transaction_outcome(
        group: ApplyGroupRequest,
        prepared: _PreparedFile,
        result: FileApplyResult,
    ) -> ApplyFileOutcome:
        status = {
            FileApplyStatus.SUCCEEDED: ApplyFileOutcomeStatus.APPLIED,
            FileApplyStatus.FAILED: ApplyFileOutcomeStatus.FAILED,
            FileApplyStatus.CANCELLED: ApplyFileOutcomeStatus.CANCELLED,
        }[result.status]

        return ApplyFileOutcome(
            file_id=prepared.request.source.file_id,
            source_path=prepared.request.source.path,
            selected_release=group.selected_release,
            reviews=prepared.request.reviews,
            change_set=prepared.change_set,
            status=status,
            transaction_result=result,
            reason_codes=prepared.request.reason_codes,
        )

    @staticmethod
    def _no_changes(group: ApplyGroupRequest, prepared: _PreparedFile) -> ApplyFileOutcome:
        return ApplyFileOutcome(
            file_id=prepared.request.source.file_id,
            source_path=prepared.request.source.path,
            selected_release=group.selected_release,
            reviews=prepared.request.reviews,
            change_set=prepared.change_set,
            status=ApplyFileOutcomeStatus.NO_CHANGES,
            reason_codes=prepared.request.reason_codes,
        )

    @staticmethod
    def _skipped(
        group: ApplyGroupRequest,
        prepared: _PreparedFile,
        reason: ApplySkipReason,
    ) -> ApplyFileOutcome:
        return ApplyFileOutcome(
            file_id=prepared.request.source.file_id,
            source_path=prepared.request.source.path,
            selected_release=group.selected_release,
            reviews=prepared.request.reviews,
            change_set=prepared.change_set,
            status=ApplyFileOutcomeStatus.SKIPPED,
            skip_reason=reason,
            reason_codes=prepared.request.reason_codes,
        )

    @staticmethod
    def _validate_writer_result(prepared: _PreparedFile, result: FileApplyResult) -> None:
        if not isinstance(result, FileApplyResult):
            raise TypeError("writer must return a FileApplyResult")

        source = prepared.request.source

        if result.source_path != source.path:
            raise ValueError("writer result source_path must match the requested source")

        if result.status is FileApplyStatus.SUCCEEDED:
            expected_final_path = (
                prepared.change_set.rename_change.new_path
                if prepared.change_set.rename_change is not None
                else source.path
            )

            if result.final_path != expected_final_path:
                raise ValueError("successful writer result final_path is inconsistent")
