"""Safe per-file temporary-copy metadata transactions."""

from dataclasses import dataclass
from pathlib import Path

from metadata_polisher.application.changes import ChangeSetStatus, FileChangeSet
from metadata_polisher.domain.errors import Issue, MediaErrorCode
from metadata_polisher.domain.media import LocalMediaFile
from metadata_polisher.execution.cancellation import (
    CancellationToken,
    OperationCancelledError,
)
from metadata_polisher.execution.events import (
    FileApplyStatus,
    FileCompleted,
    FileFailed,
    FileStageChanged,
    FileStarted,
    FileTransactionStage,
    OperationEventSink,
)
from metadata_polisher.formats.base import (
    MediaFormatAdapter,
    MediaFormatError,
)
from metadata_polisher.infrastructure.filesystem import FileSystem, LocalFileSystem


@dataclass(frozen=True)
class BackupPolicy:
    """Optional operation-scoped backup location and source path boundary."""

    enabled: bool
    root: Path | None
    scan_root: Path
    operation_id: str

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise TypeError("enabled must be a bool")

        if self.root is not None and not isinstance(self.root, Path):
            raise TypeError("root must be a Path or None")

        if not isinstance(self.scan_root, Path):
            raise TypeError("scan_root must be a Path")

        if not isinstance(self.operation_id, str) or not self.operation_id.strip():
            raise ValueError("operation_id must be a non-blank string")

        operation_component = Path(self.operation_id)

        if operation_component.name != self.operation_id or self.operation_id in {".", ".."}:
            raise ValueError("operation_id must be one safe path component")

        if self.enabled and self.root is None:
            raise ValueError("enabled backup requires a root")


@dataclass(frozen=True)
class FileApplyResult:
    """Terminal, reportable outcome of one requested file transaction."""

    source_path: Path
    final_path: Path
    status: FileApplyStatus
    completed_stage: FileTransactionStage
    issues: tuple[Issue, ...] = ()
    completed_backup_path: Path | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.source_path, Path) or not isinstance(self.final_path, Path):
            raise TypeError("source_path and final_path must be Path values")

        if not isinstance(self.status, FileApplyStatus):
            raise TypeError("status must be a FileApplyStatus")

        if not isinstance(self.completed_stage, FileTransactionStage):
            raise TypeError("completed_stage must be a FileTransactionStage")

        if self.completed_backup_path is not None and not isinstance(self.completed_backup_path, Path):
            raise TypeError("completed_backup_path must be a Path or None")

        copied_issues = tuple(self.issues)

        if any(not isinstance(issue, Issue) for issue in copied_issues):
            raise TypeError("issues must contain only Issue values")

        if self.status is FileApplyStatus.FAILED and not copied_issues:
            raise ValueError("a failed file result requires at least one issue")

        if self.status is not FileApplyStatus.FAILED and copied_issues:
            raise ValueError("only a failed file result may contain issues")

        object.__setattr__(self, "issues", copied_issues)


def _issue_from_error(
    code: MediaErrorCode,
    message: str,
    error: BaseException,
) -> Issue:
    return Issue(
        code=code,
        message=message,
        technical_detail=f"{type(error).__name__}: {error}",
    )


class TransactionalFileWriter:
    """Apply one validated ChangeSet without modifying its source pre-commit."""

    def __init__(self, filesystem: FileSystem | None = None) -> None:
        self._filesystem = filesystem or LocalFileSystem()

    def apply_file(
        self,
        source: LocalMediaFile,
        changes: FileChangeSet,
        adapter: MediaFormatAdapter,
        backup: BackupPolicy,
        cancellation: CancellationToken,
        events: OperationEventSink,
    ) -> FileApplyResult:
        # Reject mismatched or blocked review data before emitting a start event
        # or creating files. The writer consumes a validated per-file decision.
        self._validate_request(source, changes, backup)
        operation_id = backup.operation_id
        stage = FileTransactionStage.NOT_STARTED
        temporary_path: Path | None = None
        completed_backup_path: Path | None = None
        final_path = source.path
        events.emit(FileStarted(operation_id, source.file_id, source.path))

        try:
            cancellation.raise_if_cancelled()

            if not changes.metadata_changes and changes.rename_change is None:
                return self._succeeded(events, operation_id, source, source.path)

            if backup.enabled:
                stage = FileTransactionStage.BACKING_UP
                self._emit_stage(events, operation_id, source, stage)
                backup_result = self._back_up(source.path, backup)

                if isinstance(backup_result, Issue):
                    return self._failed(events, operation_id, source, final_path, stage, (backup_result,))

                # Record completion before observing cancellation. BACKING_UP is
                # also the last stage when cancellation follows a successful copy.
                completed_backup_path = backup_result
                cancellation.raise_if_cancelled()

            stage = FileTransactionStage.COPYING_TEMPORARY
            self._emit_stage(events, operation_id, source, stage)

            try:
                temporary_path = self._filesystem.create_temporary_sibling(source.path)
                self._filesystem.copy_file(source.path, temporary_path, overwrite=True)
            except OSError as error:
                cleanup_issues = self._remove_if_present(temporary_path)
                issues = (
                    _issue_from_error(
                        MediaErrorCode.FILE_COPY_FAILED,
                        "Could not create the temporary media copy.",
                        error,
                    ),
                    *cleanup_issues,
                )

                return self._failed(events, operation_id, source, final_path, stage, issues, completed_backup_path)

            cancellation.raise_if_cancelled()

            # A rename-only transaction still copies and verifies the media,
            # but must never ask the adapter to create, rewrite or upgrade tags.
            if changes.metadata_changes:
                stage = FileTransactionStage.WRITING_METADATA
                self._emit_stage(events, operation_id, source, stage)

                try:
                    adapter.write_changes(temporary_path, changes.metadata_changes)
                except MediaFormatError as error:
                    issues = (error.issue, *self._remove_if_present(temporary_path))

                    return self._failed(events, operation_id, source, final_path, stage, issues, completed_backup_path)
                except Exception as error:
                    issues = (
                        _issue_from_error(
                            MediaErrorCode.TAG_WRITE_FAILED,
                            "Could not write metadata to the temporary copy.",
                            error,
                        ),
                        *self._remove_if_present(temporary_path),
                    )

                    return self._failed(events, operation_id, source, final_path, stage, issues, completed_backup_path)

                cancellation.raise_if_cancelled()

            stage = FileTransactionStage.VERIFYING_TEMPORARY
            self._emit_stage(events, operation_id, source, stage)
            # Reopen the sibling and compare reviewed fields and stream facts
            # before the first operation that can replace or publish user media.
            verification = self._verify(adapter, temporary_path, source, changes)

            if verification:
                issues = (*verification, *self._remove_if_present(temporary_path))

                return self._failed(events, operation_id, source, final_path, stage, issues, completed_backup_path)

            cancellation.raise_if_cancelled()

            if changes.rename_change is None:
                stage = FileTransactionStage.COMMITTING
                self._emit_stage(events, operation_id, source, stage)

                try:
                    self._filesystem.replace_file(temporary_path, source.path)
                    temporary_path = None
                except OSError as error:
                    issues = (
                        _issue_from_error(
                            MediaErrorCode.COMMIT_FAILED,
                            "Could not atomically replace the original media file.",
                            error,
                        ),
                        *self._remove_if_present(temporary_path),
                    )

                    return self._failed(events, operation_id, source, final_path, stage, issues, completed_backup_path)

                # Cancellation is deliberately not observed after commit begins.
                # The current file must reach a safe terminal state.
                return self._succeeded(events, operation_id, source, source.path, completed_backup_path)

            destination = changes.rename_change.new_path
            stage = FileTransactionStage.RENAMING
            self._emit_stage(events, operation_id, source, stage)

            try:
                # Publish without replacement while retaining the original. A
                # destination appearing after preflight must cause a safe failure.
                self._filesystem.move_file_no_replace(temporary_path, destination)
                temporary_path = None
                final_path = destination
            except FileExistsError as error:
                issues = (
                    _issue_from_error(
                        MediaErrorCode.DESTINATION_EXISTS,
                        "The requested rename destination already exists.",
                        error,
                    ),
                    *self._remove_if_present(temporary_path),
                )

                return self._failed(events, operation_id, source, source.path, stage, issues, completed_backup_path)
            except OSError as error:
                issues = (
                    _issue_from_error(
                        MediaErrorCode.RENAME_FAILED,
                        "Could not publish the verified renamed media file.",
                        error,
                    ),
                    *self._remove_if_present(temporary_path),
                )

                return self._failed(events, operation_id, source, source.path, stage, issues, completed_backup_path)

            stage = FileTransactionStage.VERIFYING_FINAL
            self._emit_stage(events, operation_id, source, stage)
            final_verification = self._verify(adapter, destination, source, changes)

            # A rename has a second verification boundary at the published path.
            # Remove a failed output, preserving the original as the valid copy.
            if final_verification:
                cleanup_issues = self._remove_if_present(destination)
                final_path = destination if cleanup_issues else source.path

                return self._failed(
                    events,
                    operation_id,
                    source,
                    final_path,
                    stage,
                    (*final_verification, *cleanup_issues),
                    completed_backup_path,
                )

            stage = FileTransactionStage.CLEANING_ORIGINAL
            self._emit_stage(events, operation_id, source, stage)

            # Delete the original only after the renamed output verifies. If
            # cleanup fails, retain both files and report that exact partial state.
            try:
                self._filesystem.remove_file(source.path)
            except OSError as error:
                issue = _issue_from_error(
                    MediaErrorCode.CLEANUP_FAILED,
                    "The renamed file was verified, but the original could not be removed.",
                    error,
                )

                return self._failed(
                    events,
                    operation_id,
                    source,
                    destination,
                    stage,
                    (issue,),
                    completed_backup_path,
                )

            return self._succeeded(events, operation_id, source, destination, completed_backup_path)
        # Every cancellation check above occurs before publication. At those
        # points only the temporary sibling needs removal; the source is intact.
        except OperationCancelledError:
            cleanup_issues = self._remove_if_present(temporary_path)

            if cleanup_issues:
                return self._failed(
                    events,
                    operation_id,
                    source,
                    final_path,
                    stage,
                    cleanup_issues,
                    completed_backup_path,
                )

            result = FileApplyResult(
                source_path=source.path,
                final_path=source.path,
                status=FileApplyStatus.CANCELLED,
                completed_stage=stage,
                completed_backup_path=completed_backup_path,
            )
            events.emit(
                FileCompleted(
                    operation_id,
                    source.file_id,
                    source.path,
                    source.path,
                    result.status,
                    result.completed_stage,
                )
            )

            return result

    def _validate_request(
        self,
        source: LocalMediaFile,
        changes: FileChangeSet,
        backup: BackupPolicy,
    ) -> None:
        if not isinstance(source, LocalMediaFile):
            raise TypeError("source must be a LocalMediaFile")

        if not isinstance(changes, FileChangeSet):
            raise TypeError("changes must be a FileChangeSet")

        if not isinstance(backup, BackupPolicy):
            raise TypeError("backup must be a BackupPolicy")

        if changes.status is ChangeSetStatus.BLOCKED:
            raise ValueError("a blocked ChangeSet cannot reach the transactional writer")

        if changes.file_id != source.file_id:
            raise ValueError("ChangeSet file_id does not match the source file")

        if changes.rename_change is not None and changes.rename_change.old_path != source.path:
            raise ValueError("rename source does not match the source file path")

    def _back_up(self, source_path: Path, policy: BackupPolicy) -> Path | Issue:
        assert policy.root is not None

        try:
            relative_path = source_path.relative_to(policy.scan_root)
            destination = policy.root / policy.operation_id / relative_path
            self._filesystem.create_directory(destination.parent)
            self._filesystem.copy_file(source_path, destination, overwrite=False)
        except (OSError, ValueError) as error:
            return _issue_from_error(
                MediaErrorCode.BACKUP_FAILED,
                "Could not create the operation backup before modification.",
                error,
            )

        return destination

    def _verify(
        self,
        adapter: MediaFormatAdapter,
        path: Path,
        source: LocalMediaFile,
        changes: FileChangeSet,
    ) -> tuple[Issue, ...]:
        # Unchanged fields may contain unsupported metadata. Verification targets
        # only reviewed edits, alongside the original stable stream properties.
        changed_fields = frozenset(change.field for change in changes.metadata_changes)

        try:
            result = adapter.verify(
                path,
                changes.final_metadata,
                changed_fields,
                source.read_result.stream_info,
            )
        except MediaFormatError as error:
            return (
                _issue_from_error(
                    MediaErrorCode.VERIFICATION_FAILED,
                    "Could not reopen and verify the media file.",
                    error,
                ),
            )
        except Exception as error:
            return (
                _issue_from_error(
                    MediaErrorCode.VERIFICATION_FAILED,
                    "Could not reopen and verify the media file.",
                    error,
                ),
            )

        if result.ok:
            return ()

        if result.issues:
            return result.issues

        return (
            Issue(
                code=MediaErrorCode.VERIFICATION_FAILED,
                message="The media file did not match the reviewed metadata and stream properties.",
            ),
        )

    def _remove_if_present(self, path: Path | None) -> tuple[Issue, ...]:
        if path is None or not self._filesystem.exists(path):
            return ()

        try:
            self._filesystem.remove_file(path)
        except OSError as error:
            return (
                _issue_from_error(
                    MediaErrorCode.CLEANUP_FAILED,
                    "Could not remove an incomplete transaction output.",
                    error,
                ),
            )

        return ()

    @staticmethod
    def _emit_stage(
        events: OperationEventSink,
        operation_id: str,
        source: LocalMediaFile,
        stage: FileTransactionStage,
    ) -> None:
        events.emit(FileStageChanged(operation_id, source.file_id, source.path, stage))

    def _succeeded(
        self,
        events: OperationEventSink,
        operation_id: str,
        source: LocalMediaFile,
        final_path: Path,
        completed_backup_path: Path | None = None,
    ) -> FileApplyResult:
        self._emit_stage(
            events,
            operation_id,
            source,
            FileTransactionStage.COMPLETED,
        )
        result = FileApplyResult(
            source_path=source.path,
            final_path=final_path,
            status=FileApplyStatus.SUCCEEDED,
            completed_stage=FileTransactionStage.COMPLETED,
            completed_backup_path=completed_backup_path,
        )
        events.emit(
            FileCompleted(
                operation_id,
                source.file_id,
                source.path,
                final_path,
                result.status,
                result.completed_stage,
            )
        )

        return result

    @staticmethod
    def _failed(
        events: OperationEventSink,
        operation_id: str,
        source: LocalMediaFile,
        final_path: Path,
        stage: FileTransactionStage,
        issues: tuple[Issue, ...],
        completed_backup_path: Path | None = None,
    ) -> FileApplyResult:
        result = FileApplyResult(
            source_path=source.path,
            final_path=final_path,
            status=FileApplyStatus.FAILED,
            completed_stage=stage,
            issues=issues,
            completed_backup_path=completed_backup_path,
        )
        events.emit(
            FileFailed(
                operation_id,
                source.file_id,
                source.path,
                final_path,
                stage,
                issues,
            )
        )

        return result
