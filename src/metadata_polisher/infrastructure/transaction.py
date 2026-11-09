"""Safe per-file temporary-copy metadata transactions."""

from contextlib import ExitStack, nullcontext
from dataclasses import dataclass
from pathlib import Path

from metadata_polisher.application.changes import ChangeSetStatus, FileChangeSet
from metadata_polisher.domain.errors import Issue, MediaErrorCode
from metadata_polisher.domain.media import FileVersion, LocalMediaFile
from metadata_polisher.domain.metadata import MetadataSnapshot
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
from metadata_polisher.infrastructure.filesystem import (
    FileSystem,
    LocalFileSystem,
    ReplacementRecoveryError,
    read_file_version,
)


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
    temporary_version: FileVersion | None = None
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

            self._check_writable_source(source.path)

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
            self._check_writable_source(source.path)
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

            return self._verify_and_publish(source, changes, adapter, progress, operation_id, events, cancellation)
        except MediaFormatError as error:
            # A source conflict never grants permission to overwrite or delete
            # the external edit. A published rename may also have been changed
            # externally, so retain that path for recovery and require a rescan.
            cleanup_issues = self._remove_if_present(
                progress.temporary_path, expected_version=progress.temporary_version,
            )

            return self._failed(
                events, operation_id, source, progress.final_path, progress.stage,
                (error.issue, *cleanup_issues), progress.completed_backup_path,
            )
        # Every cancellation check above occurs before publication. At those
        # points only the temporary sibling needs removal; the source is intact.
        except OperationCancelledError:
            cleanup_issues = self._remove_if_present(
                progress.temporary_path, expected_version=progress.temporary_version,
            )

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

    def _verify_and_publish(
        self, source: LocalMediaFile, changes: FileChangeSet, adapter: MediaFormatAdapter,
        progress: _TransactionProgress, operation_id: str, events: OperationEventSink, cancellation: CancellationToken,
    ) -> FileApplyResult:
        temporary = progress.temporary_path
        assert temporary is not None
        progress.stage = FileTransactionStage.VERIFYING_TEMPORARY
        with ExitStack() as pins:
            try:
                # Keep the verified object alive through publication. ReplaceFileW
                # needs broader sharing than a rename. Both verifications use a
                # separate short guard that denies writers and directory changes.
                staging_guard = None
                if changes.rename_change is not None:
                    staging_guard = self._filesystem.pin_file(temporary, allow_rename=True)
                elif source.file_version is not None:
                    staging_guard = self._filesystem.pin_replacement_identity(temporary)

                if staging_guard is not None:
                    observed = pins.enter_context(staging_guard)
                    if not observed[1]:
                        raise OSError(f"The filesystem did not supply a stable file ID for {temporary}.")
                    progress.temporary_version = observed

                identity = progress.temporary_version
                read_guard = self._filesystem.pin_file(temporary) if identity is not None else nullcontext()
                with read_guard as reading:
                    if reading != identity:
                        raise OSError(f"The staged file at {temporary} changed before verification.")
                    self._emit_stage(events, operation_id, source, progress.stage)
                    verification = self._verify(adapter, temporary, source, changes)
            except OSError as error:
                issue = _issue_from_error(
                    MediaErrorCode.CLEANUP_FAILED, f"Could not protect the staging file at {temporary}.", error,
                )
                cleanup: tuple[Issue, ...] = ()
                if progress.temporary_version is not None:
                    cleanup = self._remove_if_present(temporary, expected_version=progress.temporary_version)
                return self._failed(events, operation_id, source, source.path, progress.stage,
                                    (issue, *cleanup), progress.completed_backup_path)

            if verification:
                issues = (*verification, *self._remove_if_present(temporary, expected_version=identity))
                return self._failed(events, operation_id, source, source.path, progress.stage,
                                    issues, progress.completed_backup_path)

            cancellation.raise_if_cancelled()
            if changes.rename_change is None:
                return self._commit_same_path(source, changes, adapter, progress, operation_id, events, identity)

            assert identity is not None
            return self._publish_rename(source, changes, adapter, progress, operation_id, events, identity)

    def _commit_same_path(
        self,
        source: LocalMediaFile,
        changes: FileChangeSet,
        adapter: MediaFormatAdapter,
        progress: _TransactionProgress,
        operation_id: str,
        events: OperationEventSink,
        verified_temporary_version: FileVersion | None,
    ) -> FileApplyResult:
        """Replace the source only after its temporary sibling has verified."""
        assert progress.temporary_path is not None
        progress.stage = FileTransactionStage.COMMITTING
        self._emit_stage(events, operation_id, source, progress.stage)
        self._check_source_version(source)

        try:
            if source.file_version is None:
                self._filesystem.replace_file(progress.temporary_path, source.path)
            else:
                assert verified_temporary_version is not None
                self._replace_reviewed_file(
                    source, changes, adapter, progress.temporary_path, verified_temporary_version,
                )
            progress.temporary_path = None
        except ReplacementRecoveryError as error:
            # The staged name can contain the only remaining edited copy.
            # Leave all recovery entries exactly as reported by Windows.
            return self._failed(
                events, operation_id, source, source.path, progress.stage,
                (_issue_from_error(MediaErrorCode.CLEANUP_FAILED, str(error), error),), progress.completed_backup_path,
            )
        except OSError as error:
            self._check_source_version(source)
            issues = (
                _issue_from_error(
                    MediaErrorCode.COMMIT_FAILED,
                    (
                        "Access denied while saving this file. Close any app using it and check folder permissions."
                        if isinstance(error, PermissionError)
                        else "Could not save the edited file. Choose Open log folder in Diagnostics for details."
                    ),
                    error,
                ),
                *self._remove_if_present(progress.temporary_path, expected_version=verified_temporary_version),
            )

            return self._failed(
                events, operation_id, source, progress.final_path, progress.stage,
                issues, progress.completed_backup_path,
            )

        # Cancellation is deliberately not observed after commit begins. The
        # current file must reach a safe terminal state before the batch stops.
        return self._succeeded(events, operation_id, source, source.path, progress.completed_backup_path)

    def _replace_reviewed_file(
        self, source: LocalMediaFile, changes: FileChangeSet, adapter: MediaFormatAdapter,
        temporary: Path, verified_temporary_version: FileVersion,
    ) -> None:
        # Allow Windows to move the reviewed object into recovery, but keep
        # writers out while confirming the user's approved old values.
        with (
            self._filesystem.replacement_recovery_path(source.path) as recovery,
            self._filesystem.pin_file(source.path, allow_rename=True) as reviewed,
        ):
            if not reviewed[1] or reviewed != source.file_version:
                raise MediaFormatError(
                    path=source.path,
                    issue=Issue(MediaErrorCode.SOURCE_CHANGED, "The reviewed file changed. Rescan before saving."),
                )

            self._check_reviewed_source(source, changes, adapter)
            self._filesystem.replace_file(temporary, source.path, recovery=recovery)

            try:
                # The API retained the entry actually displaced. Keep it until
                # the published name, contents and old file are all proven.
                with self._filesystem.pin_file(source.path) as published:
                    if published[:4] != verified_temporary_version[:4]:
                        raise OSError("The published file differs from the verified edit.")
                    if self._verify(adapter, source.path, source, changes):
                        raise OSError("Could not verify the published metadata and stream properties.")

                    with self._filesystem.pin_file(recovery, allow_rename=True) as displaced:
                        if displaced[:2] != reviewed[:2]:
                            raise OSError("A different file occupied the reviewed path at commit.")
                        self._filesystem.remove_file(recovery, expected_version=displaced)
            except OSError as error:
                raise ReplacementRecoveryError(
                    f"Could not confirm the replacement of {source.path}. "
                    f"Check the displaced file at {recovery} before rescanning."
                ) from error

    def _publish_rename(
        self,
        source: LocalMediaFile,
        changes: FileChangeSet,
        adapter: MediaFormatAdapter,
        progress: _TransactionProgress,
        operation_id: str,
        events: OperationEventSink,
        identity: FileVersion,
    ) -> FileApplyResult:
        """Publish and verify a renamed copy before removing the original."""
        assert changes.rename_change is not None
        assert progress.temporary_path is not None
        destination = changes.rename_change.new_path
        progress.stage = FileTransactionStage.RENAMING
        self._emit_stage(events, operation_id, source, progress.stage)
        self._check_source_version(source)

        self._check_reviewed_source(source, changes, adapter)

        with ExitStack() as handles:
            try:
                current = handles.enter_context(self._filesystem.pin_file(progress.temporary_path, allow_rename=True))
                if current != identity:
                    raise OSError(f"The verified file at {progress.temporary_path} changed before publication.")
                self._filesystem.move_file_no_replace(progress.temporary_path, destination)
                progress.temporary_path = None
            except OSError as error:
                exists = isinstance(error, FileExistsError)
                issue = _issue_from_error(
                    MediaErrorCode.DESTINATION_EXISTS if exists else MediaErrorCode.RENAME_FAILED,
                    "The requested rename destination already exists." if exists
                    else "Could not publish the verified renamed media file.",
                    error,
                )
                return self._failed(
                    events, operation_id, source, source.path, progress.stage,
                    (issue, *self._remove_if_present(progress.temporary_path, expected_version=identity)),
                    progress.completed_backup_path,
                )

            progress.stage = FileTransactionStage.VERIFYING_FINAL
            try:
                with self._filesystem.pin_file(destination) as published:
                    # Publication changes filesystem timestamps. The volume and
                    # file ID from the still-open sibling identify the same object.
                    if published[:2] != identity[:2]:
                        raise OSError(f"The published file at {destination} was replaced.")

                    progress.final_path = destination
                    self._emit_stage(events, operation_id, source, progress.stage)
                    verification = self._verify(adapter, destination, source, changes)

                    if not verification:
                        progress.stage = FileTransactionStage.CLEANING_ORIGINAL
                        self._emit_stage(events, operation_id, source, progress.stage)
                        self._check_source_version(source)
                        self._check_reviewed_source(source, changes, adapter)
                        # The destination guard denies deletion and writing until
                        # the checked source entry has been removed.
                        self._filesystem.remove_file(source.path, expected_version=source.file_version)
                        return self._succeeded(
                            events, operation_id, source, destination, progress.completed_backup_path,
                        )

                # Normal verification opens for read cannot coexist with DELETE
                # access on Windows. After releasing the guard, reacquire deletion
                # access and compare its version on the very handle used to remove.
                cleanup_issues = self._remove_if_present(destination, expected_version=published)
                return self._failed(
                    events, operation_id, source, source.path, progress.stage,
                    (*verification, *cleanup_issues), progress.completed_backup_path,
                )
            except OSError as error:
                message = (
                    "The renamed file was verified, but the original could not be removed."
                    if progress.stage is FileTransactionStage.CLEANING_ORIGINAL
                    else f"Could not confirm ownership of {destination}. The original was retained. Rescan the folder."
                )
                return self._failed(
                    events, operation_id, source, progress.final_path, progress.stage,
                    (_issue_from_error(MediaErrorCode.CLEANUP_FAILED, message, error),), progress.completed_backup_path,
                )

    def _check_reviewed_source(
        self, source: LocalMediaFile, changes: FileChangeSet, adapter: MediaFormatAdapter,
    ) -> None:
        if source.file_version is None or not changes.metadata_changes:
            return

        # Even a real filesystem change timestamp may be coarse. Reopen the
        # original and require the old values approved in the summary, using
        # the adapter's existing field/read-state and stream checks.
        issues = self._verify(adapter, source.path, source, changes, expected=source.read_result.metadata)
        if issues:
            raise MediaFormatError(
                path=source.path,
                issue=Issue(
                    MediaErrorCode.SOURCE_CHANGED,
                    "Could not confirm the reviewed source values. Rescan and review the file again.",
                    technical_detail="\n".join(issue.technical_detail or issue.message for issue in issues),
                ),
            )

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
        *,
        expected: MetadataSnapshot | None = None,
    ) -> tuple[Issue, ...]:
        # Unchanged fields may contain unsupported metadata. Verification targets
        # only reviewed edits, alongside the original stable stream properties.
        changed_fields = frozenset(change.field for change in changes.metadata_changes)

        try:
            result = adapter.verify(
                path,
                changes.final_metadata if expected is None else expected,
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

    def _check_writable_source(self, path: Path) -> None:
        if not self._filesystem.is_writable_file(path):
            raise MediaFormatError(
                path=path,
                issue=Issue(
                    MediaErrorCode.PERMISSION_DENIED,
                    "The source file is read-only or write access was denied.",
                ),
            )

    def _remove_if_present(
        self, path: Path | None, *, expected_version: FileVersion | None = None,
    ) -> tuple[Issue, ...]:
        if path is None or not self._filesystem.exists(path):
            return ()

        try:
            self._filesystem.remove_file(path, expected_version=expected_version)
        except OSError as error:
            return (
                _issue_from_error(
                    MediaErrorCode.CLEANUP_FAILED,
                    f"Could not remove the incomplete transaction output at {path}.",
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
