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
from metadata_polisher.infrastructure.filesystem import FileSystem, LocalFileSystem, read_file_version


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


@dataclass
class _TransactionProgress:
    """Current disk state shared by phase helpers and terminal recovery."""

    final_path: Path
    stage: FileTransactionStage = FileTransactionStage.NOT_STARTED
    temporary_path: Path | None = None
    completed_backup_path: Path | None = None


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
        progress = _TransactionProgress(final_path=source.path)
        events.emit(FileStarted(operation_id, source.file_id, source.path))

        try:
            cancellation.raise_if_cancelled()

            if not changes.metadata_changes and changes.rename_change is None:
                return self._succeeded(events, operation_id, source, source.path)

            self._check_source_version(source)

            if backup.enabled:
                progress.stage = FileTransactionStage.BACKING_UP
                self._emit_stage(events, operation_id, source, progress.stage)
                self._check_source_version(source)
                backup_result = self._back_up(source.path, backup)

                if isinstance(backup_result, Issue):
                    return self._failed(
                        events, operation_id, source, progress.final_path, progress.stage, (backup_result,),
                    )

                # Record completion before observing cancellation. BACKING_UP is
                # also the last stage when cancellation follows a successful copy.
                progress.completed_backup_path = backup_result
                cancellation.raise_if_cancelled()

            progress.stage = FileTransactionStage.COPYING_TEMPORARY
            self._emit_stage(events, operation_id, source, progress.stage)
            self._check_source_version(source)

            try:
                progress.temporary_path = self._filesystem.create_temporary_sibling(source.path)
                self._filesystem.copy_file(source.path, progress.temporary_path, overwrite=True)
            except OSError as error:
                cleanup_issues = self._remove_if_present(progress.temporary_path)
                issues = (
                    _issue_from_error(
                        MediaErrorCode.FILE_COPY_FAILED,
                        "Could not create the temporary media copy.",
                        error,
                    ),
                    *cleanup_issues,
                )

                return self._failed(
                    events, operation_id, source, progress.final_path, progress.stage,
                    issues, progress.completed_backup_path,
                )

            # A concurrent edit during copying also invalidates the reviewed
            # baseline, even if the private copy itself can still be verified.
            self._check_source_version(source)
            cancellation.raise_if_cancelled()

            # A rename-only transaction still copies and verifies the media,
            # but must never ask the adapter to create, rewrite or upgrade tags.
            if changes.metadata_changes:
                progress.stage = FileTransactionStage.WRITING_METADATA
                self._emit_stage(events, operation_id, source, progress.stage)

                try:
                    adapter.write_changes(progress.temporary_path, changes.metadata_changes)
                except MediaFormatError as error:
                    issues = (error.issue, *self._remove_if_present(progress.temporary_path))

                    return self._failed(
                        events, operation_id, source, progress.final_path, progress.stage,
                        issues, progress.completed_backup_path,
                    )
                except Exception as error:
                    issues = (
                        _issue_from_error(
                            MediaErrorCode.TAG_WRITE_FAILED,
                            "Could not write metadata to the temporary copy.",
                            error,
                        ),
                        *self._remove_if_present(progress.temporary_path),
                    )

                    return self._failed(
                        events, operation_id, source, progress.final_path, progress.stage,
                        issues, progress.completed_backup_path,
                    )

                cancellation.raise_if_cancelled()

            progress.stage = FileTransactionStage.VERIFYING_TEMPORARY
            self._emit_stage(events, operation_id, source, progress.stage)
            # Reopen the sibling and compare reviewed fields and stream facts
            # before the first operation that can replace or publish user media.
            verification = self._verify(adapter, progress.temporary_path, source, changes)

            if verification:
                issues = (*verification, *self._remove_if_present(progress.temporary_path))

                return self._failed(
                    events, operation_id, source, progress.final_path, progress.stage,
                    issues, progress.completed_backup_path,
                )

            cancellation.raise_if_cancelled()

            if changes.rename_change is None:
                return self._commit_same_path(source, progress, operation_id, events)

            return self._publish_rename(source, changes, adapter, progress, operation_id, events)
        except MediaFormatError as error:
            # A source conflict never grants permission to overwrite or delete
            # the external edit. A published rename may also have been changed
            # externally, so retain that path for recovery and require a rescan.
            cleanup_issues = self._remove_if_present(progress.temporary_path)

            return self._failed(
                events, operation_id, source, progress.final_path, progress.stage,
                (error.issue, *cleanup_issues), progress.completed_backup_path,
            )
        # Every cancellation check above occurs before publication. At those
        # points only the temporary sibling needs removal; the source is intact.
        except OperationCancelledError:
            cleanup_issues = self._remove_if_present(progress.temporary_path)

            if cleanup_issues:
                return self._failed(
                    events,
                    operation_id,
                    source,
                    progress.final_path,
                    progress.stage,
                    cleanup_issues,
                    progress.completed_backup_path,
                )

            result = FileApplyResult(
                source_path=source.path,
                final_path=source.path,
                status=FileApplyStatus.CANCELLED,
                completed_stage=progress.stage,
                completed_backup_path=progress.completed_backup_path,
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

    def _commit_same_path(
        self,
        source: LocalMediaFile,
        progress: _TransactionProgress,
        operation_id: str,
        events: OperationEventSink,
    ) -> FileApplyResult:
        """Replace the source only after its temporary sibling has verified."""
        assert progress.temporary_path is not None
        progress.stage = FileTransactionStage.COMMITTING
        self._emit_stage(events, operation_id, source, progress.stage)
        self._check_source_version(source)

        try:
            self._filesystem.replace_file(progress.temporary_path, source.path)
            progress.temporary_path = None
        except OSError as error:
            issues = (
                _issue_from_error(
                    MediaErrorCode.COMMIT_FAILED,
                    "Could not atomically replace the original media file.",
                    error,
                ),
                *self._remove_if_present(progress.temporary_path),
            )

            return self._failed(
                events, operation_id, source, progress.final_path, progress.stage,
                issues, progress.completed_backup_path,
            )

        # Cancellation is deliberately not observed after commit begins. The
        # current file must reach a safe terminal state before the batch stops.
        return self._succeeded(events, operation_id, source, source.path, progress.completed_backup_path)

    def _publish_rename(
        self,
        source: LocalMediaFile,
        changes: FileChangeSet,
        adapter: MediaFormatAdapter,
        progress: _TransactionProgress,
        operation_id: str,
        events: OperationEventSink,
    ) -> FileApplyResult:
        """Publish and verify a renamed copy before removing the original."""
        assert changes.rename_change is not None
        assert progress.temporary_path is not None
        destination = changes.rename_change.new_path
        progress.stage = FileTransactionStage.RENAMING
        self._emit_stage(events, operation_id, source, progress.stage)
        self._check_source_version(source)

        try:
            # Publish without replacement while retaining the original. A
            # destination appearing after preflight must cause a safe failure.
            self._filesystem.move_file_no_replace(progress.temporary_path, destination)
            progress.temporary_path = None
            progress.final_path = destination
        except FileExistsError as error:
            issues = (
                _issue_from_error(
                    MediaErrorCode.DESTINATION_EXISTS,
                    "The requested rename destination already exists.",
                    error,
                ),
                *self._remove_if_present(progress.temporary_path),
            )

            return self._failed(
                events, operation_id, source, source.path, progress.stage,
                issues, progress.completed_backup_path,
            )
        except OSError as error:
            issues = (
                _issue_from_error(
                    MediaErrorCode.RENAME_FAILED,
                    "Could not publish the verified renamed media file.",
                    error,
                ),
                *self._remove_if_present(progress.temporary_path),
            )

            return self._failed(
                events, operation_id, source, source.path, progress.stage,
                issues, progress.completed_backup_path,
            )

        # The shared progress record now identifies the published copy. If a
        # later source-version check fails, outer recovery must retain that
        # output and report its path rather than treating it as a temporary file.
        progress.stage = FileTransactionStage.VERIFYING_FINAL
        self._emit_stage(events, operation_id, source, progress.stage)
        final_verification = self._verify(adapter, destination, source, changes)

        if final_verification:
            cleanup_issues = self._remove_if_present(destination)
            progress.final_path = destination if cleanup_issues else source.path

            return self._failed(
                events,
                operation_id,
                source,
                progress.final_path,
                progress.stage,
                (*final_verification, *cleanup_issues),
                progress.completed_backup_path,
            )

        progress.stage = FileTransactionStage.CLEANING_ORIGINAL
        self._emit_stage(events, operation_id, source, progress.stage)
        self._check_source_version(source)

        # Delete the original only after the renamed output verifies. If cleanup
        # fails, retain both copies and report that exact partial completion.
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
                progress.stage,
                (issue,),
                progress.completed_backup_path,
            )

        return self._succeeded(events, operation_id, source, destination, progress.completed_backup_path)

    @staticmethod
    def _check_source_version(source: LocalMediaFile) -> None:
        # Synthetic callers can omit a version; every real scan supplies one.
        # Compare identity as well as timestamps to catch replaced source files.
        if source.file_version is None:
            return

        detail = None

        try:
            if read_file_version(source.path) == source.file_version:
                return
        except OSError as error:
            detail = f"{type(error).__name__}: {error}"

        raise MediaFormatError(
            path=source.path,
            issue=Issue(
                MediaErrorCode.SOURCE_CHANGED,
                "The source file changed since scanning. Rescan and review it again.",
                technical_detail=detail,
            ),
        )

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
