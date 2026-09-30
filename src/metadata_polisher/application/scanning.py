"""Local-only scan and grouping use case with semantic progress."""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from metadata_polisher.domain.media import LocalMediaFile
from metadata_polisher.execution.cancellation import CancellationToken, NeverCancelledToken
from metadata_polisher.execution.events import (
    OperationEvent,
    OperationEventSink,
    OperationProgress,
    OperationStageChanged,
)
from metadata_polisher.scanner.grouping import GroupingResult, group_scanned_files
from metadata_polisher.scanner.scanner import FormatRegistry, ScanResult, scan_media


class ScanFunction(Protocol):
    """Injectable local scanner boundary used by the application service."""

    def __call__(
        self,
        root: Path,
        registry: FormatRegistry,
        *,
        cancellation: CancellationToken | None = None,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> ScanResult: ...


class GroupFilesFunction(Protocol):
    """Injectable pure grouping boundary used after a completed scan."""

    def __call__(self, files: Iterable[LocalMediaFile]) -> GroupingResult: ...


class ScanLibraryStage(StrEnum):
    """Stable stages for one local library scan."""

    SCANNING_FILES = "scanning_files"
    GROUPING_FILES = "grouping_files"


def _validate_non_blank(name: str, value: object) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")

    if not value.strip():
        raise ValueError(f"{name} must be non-blank")


def _validate_revision(name: str, value: object) -> None:
    if type(value) is not int:
        raise TypeError(f"{name} must be an integer")

    if value < 0:
        raise ValueError(f"{name} cannot be negative")


@dataclass(frozen=True)
class ScanLibraryResult:
    """State-independent scan result labelled with captured session lineage."""

    operation_id: str
    base_session_revision: int
    base_library_revision: int
    root: Path
    scan_result: ScanResult
    grouping_result: GroupingResult

    def __post_init__(self) -> None:
        _validate_non_blank("operation_id", self.operation_id)
        _validate_revision("base_session_revision", self.base_session_revision)
        _validate_revision("base_library_revision", self.base_library_revision)

        if self.base_library_revision > self.base_session_revision:
            raise ValueError("base_library_revision cannot exceed base_session_revision")

        if not isinstance(self.root, Path):
            raise TypeError("root must be a Path")

        if not isinstance(self.scan_result, ScanResult):
            raise TypeError("scan_result must be a ScanResult")

        if not isinstance(self.grouping_result, GroupingResult):
            raise TypeError("grouping_result must be a GroupingResult")

        grouped_files = tuple(
            file for group in self.grouping_result.groups for file in group.files
        )
        scanned_ids = tuple(file.file_id for file in self.scan_result.supported_files)
        grouped_ids = tuple(file.file_id for file in grouped_files)

        if len(scanned_ids) != len(set(scanned_ids)):
            raise ValueError("scan_result supported file IDs must be unique")

        if len(grouped_ids) != len(set(grouped_ids)):
            raise ValueError("grouping_result file IDs must be unique")

        scanned_by_id = {
            file.file_id: file for file in self.scan_result.supported_files
        }
        grouped_by_id = {file.file_id: file for file in grouped_files}

        if len(scanned_ids) != len(grouped_ids) or scanned_by_id != grouped_by_id:
            raise ValueError("grouping_result must contain every scanned supported file exactly once")


class _DiscardEventSink:
    def emit(self, _event: OperationEvent) -> None:
        return


class ScanLibraryService:
    """Synchronously scan and group local files without provider or UI dependencies."""

    def __init__(
        self,
        registry: FormatRegistry,
        *,
        scanner: ScanFunction = scan_media,
        grouper: GroupFilesFunction = group_scanned_files,
    ) -> None:
        self._registry = registry
        self._scanner = scanner
        self._grouper = grouper

    def scan_library(
        self,
        *,
        operation_id: str,
        base_session_revision: int,
        base_library_revision: int,
        root: Path,
        cancellation: CancellationToken | None = None,
        events: OperationEventSink | None = None,
    ) -> ScanLibraryResult:
        """Run scanner then grouping, returning data for central state application."""
        _validate_non_blank("operation_id", operation_id)
        _validate_revision("base_session_revision", base_session_revision)
        _validate_revision("base_library_revision", base_library_revision)

        if base_library_revision > base_session_revision:
            raise ValueError("base_library_revision cannot exceed base_session_revision")

        if not isinstance(root, Path):
            raise TypeError("root must be a Path")

        active_cancellation = cancellation if cancellation is not None else NeverCancelledToken()
        active_events = events if events is not None else _DiscardEventSink()
        active_cancellation.raise_if_cancelled()
        active_events.emit(OperationStageChanged(operation_id, ScanLibraryStage.SCANNING_FILES))

        def emit_scan_progress(current: int, total: int) -> None:
            active_events.emit(
                OperationProgress(
                    operation_id,
                    ScanLibraryStage.SCANNING_FILES,
                    current,
                    total,
                )
            )

        scan_result = self._scanner(
            root,
            self._registry,
            cancellation=active_cancellation,
            on_progress=emit_scan_progress,
        )
        active_cancellation.raise_if_cancelled()

        if not scan_result.complete:
            # Use the existing worker-failure path so an incomplete directory
            # snapshot cannot clear current groups or pending review decisions.
            detail = "\n".join(
                f"{issue.message}\n{issue.technical_detail or ''}".rstrip()
                for issue in scan_result.issues
            )
            raise OSError(f"The folder could not be scanned completely.\n{detail}")

        active_events.emit(OperationStageChanged(operation_id, ScanLibraryStage.GROUPING_FILES))
        active_events.emit(OperationProgress(operation_id, ScanLibraryStage.GROUPING_FILES, 0, 1))
        active_cancellation.raise_if_cancelled()
        # Unsupported files remain in the scan result for display, but cannot
        # contribute to an album's matching evidence or writable membership.
        grouping_result = self._grouper(scan_result.supported_files)
        active_cancellation.raise_if_cancelled()
        active_events.emit(OperationProgress(operation_id, ScanLibraryStage.GROUPING_FILES, 1, 1))

        # Carry the versions captured at dispatch. Only the UI-thread reducer
        # can decide whether this completed scan still belongs to the session.
        return ScanLibraryResult(
            operation_id=operation_id,
            base_session_revision=base_session_revision,
            base_library_revision=base_library_revision,
            root=root,
            scan_result=scan_result,
            grouping_result=grouping_result,
        )
