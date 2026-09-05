from collections.abc import Callable
from pathlib import Path

import pytest

from metadata_polisher.application.changes import (
    ChangeValidationResult,
    FileChangeSet,
    RenameChange,
    RenameDecision,
)
from metadata_polisher.domain.errors import Issue, MediaErrorCode
from metadata_polisher.domain.media import (
    FilenameHints,
    LocalMediaFile,
    MediaReadResult,
    StreamInfo,
)
from metadata_polisher.domain.metadata import (
    FieldReadState,
    MetadataChange,
    MetadataField,
    MetadataSnapshot,
    Position,
)
from metadata_polisher.execution.cancellation import (
    MutableCancellationToken,
    NeverCancelledToken,
)
from metadata_polisher.execution.events import (
    FileApplyStatus,
    FileCompleted,
    FileFailed,
    FileOperationEvent,
    FileStageChanged,
    FileStarted,
    FileTransactionStage,
)
from metadata_polisher.formats.base import MediaFormatError, VerificationResult
from metadata_polisher.infrastructure.transaction import (
    BackupPolicy,
    FileApplyResult,
    TransactionalFileWriter,
)

SOURCE_PATH = Path("library/disc/track.flac")
RENAMED_PATH = Path("library/disc/01. New title.flac")
STREAM_INFO = StreamInfo(
    duration_seconds=180.5,
    sample_rate=44_100,
    channels=2,
    bit_depth=16,
    codec="FLAC",
)


def make_source(path: Path = SOURCE_PATH) -> LocalMediaFile:
    metadata = MetadataSnapshot(
        title="Old title",
        artists=("Artist",),
        album="Album",
        track=Position(number=1, total=1),
    )
    states = {
        field: (
            FieldReadState.PRESENT
            if getattr(metadata, field.value)
            else FieldReadState.MISSING
        )
        for field in MetadataField
    }

    return LocalMediaFile(
        path=path,
        format_id="flac",
        read_result=MediaReadResult(
            metadata=metadata,
            field_states=states,
            stream_info=STREAM_INFO,
        ),
        filename_hints=FilenameHints(),
        file_id="file-1",
    )


def make_change_set(
    source: LocalMediaFile,
    *,
    renamed_path: Path | None = None,
) -> FileChangeSet:
    final_metadata = MetadataSnapshot(
        title="New title",
        artists=source.read_result.metadata.artists,
        album=source.read_result.metadata.album,
        track=source.read_result.metadata.track,
    )
    rename_change = (
        RenameChange(old_path=source.path, new_path=renamed_path)
        if renamed_path is not None
        else None
    )

    return FileChangeSet(
        file_id=source.file_id,
        metadata_changes=(
            MetadataChange(
                field=MetadataField.TITLE,
                old_value="Old title",
                new_value="New title",
            ),
        ),
        rename_change=rename_change,
        final_metadata=final_metadata,
        rename_decision=(
            RenameDecision.APPLY_RENAME
            if rename_change is not None
            else RenameDecision.KEEP_FILENAME
        ),
        rename_preview=rename_change,
        validation=ChangeValidationResult(),
    )


def make_rename_only_change_set(
    source: LocalMediaFile,
    renamed_path: Path,
) -> FileChangeSet:
    rename_change = RenameChange(old_path=source.path, new_path=renamed_path)

    return FileChangeSet(
        file_id=source.file_id,
        metadata_changes=(),
        rename_change=rename_change,
        final_metadata=source.read_result.metadata,
        rename_decision=RenameDecision.APPLY_RENAME,
        rename_preview=rename_change,
        validation=ChangeValidationResult(),
    )


def make_no_op_change_set(source: LocalMediaFile) -> FileChangeSet:
    return FileChangeSet(
        file_id=source.file_id,
        metadata_changes=(),
        rename_change=None,
        final_metadata=source.read_result.metadata,
        rename_decision=RenameDecision.KEEP_FILENAME,
        rename_preview=None,
        validation=ChangeValidationResult(),
    )


# The in-memory file table exposes exact survivors at each failure boundary.
# Hooks request cancellation immediately after a copy, replacement or rename.
class FakeFileSystem:
    """Small stateful filesystem double exposing transaction side effects."""

    def __init__(self, source_path: Path = SOURCE_PATH) -> None:
        self.files: dict[Path, bytes] = {source_path: b"original"}
        self.temporary_path = source_path.with_name(f".{source_path.stem}.transaction{source_path.suffix}")
        self.faults: set[str] = set()
        self.after_copy: Callable[[], None] | None = None
        self.after_replace: Callable[[], None] | None = None
        self.after_move: Callable[[], None] | None = None

    def create_temporary_sibling(self, source: Path) -> Path:
        if "create_temporary" in self.faults:
            raise OSError("cannot create temporary file")

        self.files[self.temporary_path] = b""
        return self.temporary_path

    def create_directory(self, path: Path) -> None:
        del path

    def copy_file(self, source: Path, destination: Path, *, overwrite: bool) -> None:
        fault = "copy_temporary" if overwrite else "copy_backup"

        if fault in self.faults:
            raise OSError(f"{fault} failed")

        if not overwrite and destination in self.files:
            raise FileExistsError(destination)

        self.files[destination] = self.files[source]

        if self.after_copy is not None:
            self.after_copy()

    def replace_file(self, source: Path, destination: Path) -> None:
        if "replace" in self.faults:
            raise OSError("replace failed")

        self.files[destination] = self.files.pop(source)

        if self.after_replace is not None:
            self.after_replace()

    def move_file_no_replace(self, source: Path, destination: Path) -> None:
        if destination in self.files:
            raise FileExistsError(destination)

        if "rename" in self.faults:
            raise OSError("rename failed")

        self.files[destination] = self.files.pop(source)

        if self.after_move is not None:
            self.after_move()

    def remove_file(self, path: Path) -> None:
        if path == SOURCE_PATH and "remove_original" in self.faults:
            raise OSError("remove original failed")

        self.files.pop(path, None)

    def exists(self, path: Path) -> bool:
        return path in self.files


# Separate temporary and final verification failures distinguish pre-commit
# cleanup from a renamed output that must be removed while its source survives.
class FakeAdapter:
    format_id = "flac"
    extensions = frozenset((".flac",))

    def __init__(self, filesystem: FakeFileSystem) -> None:
        self.filesystem = filesystem
        self.fail_write = False
        self.fail_temporary_verification = False
        self.fail_final_verification = False
        self.after_verification: Callable[[Path], None] | None = None
        self.write_paths: list[Path] = []
        self.verification_paths: list[Path] = []
        self.verification_arguments: list[
            tuple[MetadataSnapshot, frozenset[MetadataField], StreamInfo]
        ] = []

    def write_changes(self, path: Path, changes: tuple[MetadataChange, ...]) -> None:
        self.write_paths.append(path)

        if self.fail_write:
            raise MediaFormatError(
                path=path,
                issue=Issue(
                    code=MediaErrorCode.TAG_WRITE_FAILED,
                    message="The fake adapter could not write metadata.",
                ),
            )

        assert changes
        self.filesystem.files[path] = b"written"

    def verify(
        self,
        path: Path,
        expected: MetadataSnapshot,
        changed_fields: frozenset[MetadataField],
        baseline_stream: StreamInfo,
    ) -> VerificationResult:
        self.verification_paths.append(path)
        self.verification_arguments.append((expected, changed_fields, baseline_stream))

        if self.after_verification is not None:
            self.after_verification(path)

        is_final = path == RENAMED_PATH

        if (is_final and self.fail_final_verification) or (
            not is_final and self.fail_temporary_verification
        ):
            return VerificationResult(
                ok=False,
                issues=(
                    Issue(
                        code=MediaErrorCode.VERIFICATION_FAILED,
                        message="The fake adapter could not verify metadata.",
                    ),
                ),
            )

        return VerificationResult(ok=True)


class RecordingEventSink:
    def __init__(self) -> None:
        self.events: list[FileOperationEvent] = []

    def emit(self, event: FileOperationEvent) -> None:
        self.events.append(event)


def disabled_backup() -> BackupPolicy:
    return BackupPolicy(
        enabled=False,
        root=None,
        scan_root=Path("library"),
        operation_id="APPLY-0001",
    )


def apply_with(
    filesystem: FakeFileSystem,
    adapter: FakeAdapter,
    *,
    renamed_path: Path | None = None,
    cancellation: MutableCancellationToken | NeverCancelledToken | None = None,
    backup: BackupPolicy | None = None,
) -> tuple[FileApplyResult, RecordingEventSink]:
    source = make_source()
    events = RecordingEventSink()
    result = TransactionalFileWriter(filesystem).apply_file(
        source=source,
        changes=make_change_set(source, renamed_path=renamed_path),
        adapter=adapter,  # type: ignore[arg-type]
        backup=backup or disabled_backup(),
        cancellation=cancellation or NeverCancelledToken(),
        events=events,
    )

    return result, events


def issue_codes(result: FileApplyResult) -> tuple[MediaErrorCode, ...]:
    return tuple(issue.code for issue in result.issues)  # type: ignore[return-value]


def test_same_name_write_replaces_original_only_after_temporary_verification() -> None:
    filesystem = FakeFileSystem()
    adapter = FakeAdapter(filesystem)

    result, events = apply_with(filesystem, adapter)

    assert result.status is FileApplyStatus.SUCCEEDED  # type: ignore[attr-defined]
    assert result.final_path == SOURCE_PATH  # type: ignore[attr-defined]
    assert result.completed_stage is FileTransactionStage.COMPLETED  # type: ignore[attr-defined]
    assert result.issues == ()  # type: ignore[attr-defined]
    assert filesystem.files == {SOURCE_PATH: b"written"}
    assert adapter.verification_paths == [filesystem.temporary_path]
    assert adapter.verification_arguments == [
        (
            make_change_set(make_source()).final_metadata,
            frozenset((MetadataField.TITLE,)),
            STREAM_INFO,
        )
    ]
    assert [type(event) for event in events.events] == [
        FileStarted,
        FileStageChanged,
        FileStageChanged,
        FileStageChanged,
        FileStageChanged,
        FileStageChanged,
        FileCompleted,
    ]
    assert [
        event.stage
        for event in events.events
        if isinstance(event, FileStageChanged)
    ] == [
        FileTransactionStage.COPYING_TEMPORARY,
        FileTransactionStage.WRITING_METADATA,
        FileTransactionStage.VERIFYING_TEMPORARY,
        FileTransactionStage.COMMITTING,
        FileTransactionStage.COMPLETED,
    ]


@pytest.mark.parametrize("fault", ("create_temporary", "copy_temporary"))
def test_temporary_copy_failure_leaves_original_untouched(fault: str) -> None:
    filesystem = FakeFileSystem()
    filesystem.faults.add(fault)
    adapter = FakeAdapter(filesystem)

    result, events = apply_with(filesystem, adapter)

    assert result.status is FileApplyStatus.FAILED  # type: ignore[attr-defined]
    assert result.completed_stage is FileTransactionStage.COPYING_TEMPORARY  # type: ignore[attr-defined]
    assert issue_codes(result) == (MediaErrorCode.FILE_COPY_FAILED,)
    assert filesystem.files == {SOURCE_PATH: b"original"}
    assert isinstance(events.events[-1], FileFailed)


def test_adapter_write_failure_removes_temporary_and_preserves_original() -> None:
    filesystem = FakeFileSystem()
    adapter = FakeAdapter(filesystem)
    adapter.fail_write = True

    result, _events = apply_with(filesystem, adapter)

    assert result.status is FileApplyStatus.FAILED  # type: ignore[attr-defined]
    assert result.completed_stage is FileTransactionStage.WRITING_METADATA  # type: ignore[attr-defined]
    assert issue_codes(result) == (MediaErrorCode.TAG_WRITE_FAILED,)
    assert filesystem.files == {SOURCE_PATH: b"original"}


def test_temporary_verification_failure_removes_temporary_and_preserves_original() -> None:
    filesystem = FakeFileSystem()
    adapter = FakeAdapter(filesystem)
    adapter.fail_temporary_verification = True

    result, _events = apply_with(filesystem, adapter)

    assert result.status is FileApplyStatus.FAILED  # type: ignore[attr-defined]
    assert result.completed_stage is FileTransactionStage.VERIFYING_TEMPORARY  # type: ignore[attr-defined]
    assert issue_codes(result) == (MediaErrorCode.VERIFICATION_FAILED,)
    assert filesystem.files == {SOURCE_PATH: b"original"}


def test_same_name_commit_failure_removes_temporary_and_preserves_original() -> None:
    filesystem = FakeFileSystem()
    filesystem.faults.add("replace")
    adapter = FakeAdapter(filesystem)

    result, _events = apply_with(filesystem, adapter)

    assert result.status is FileApplyStatus.FAILED  # type: ignore[attr-defined]
    assert result.completed_stage is FileTransactionStage.COMMITTING  # type: ignore[attr-defined]
    assert issue_codes(result) == (MediaErrorCode.COMMIT_FAILED,)
    assert filesystem.files == {SOURCE_PATH: b"original"}


def test_existing_rename_destination_is_never_overwritten() -> None:
    filesystem = FakeFileSystem()
    filesystem.files[RENAMED_PATH] = b"unrelated"
    adapter = FakeAdapter(filesystem)

    result, _events = apply_with(filesystem, adapter, renamed_path=RENAMED_PATH)

    assert result.status is FileApplyStatus.FAILED  # type: ignore[attr-defined]
    assert result.completed_stage is FileTransactionStage.RENAMING  # type: ignore[attr-defined]
    assert issue_codes(result) == (MediaErrorCode.DESTINATION_EXISTS,)
    assert filesystem.files == {
        SOURCE_PATH: b"original",
        RENAMED_PATH: b"unrelated",
    }


def test_rename_publishes_then_verifies_before_removing_original() -> None:
    filesystem = FakeFileSystem()
    adapter = FakeAdapter(filesystem)

    result, events = apply_with(filesystem, adapter, renamed_path=RENAMED_PATH)

    assert result.status is FileApplyStatus.SUCCEEDED  # type: ignore[attr-defined]
    assert result.final_path == RENAMED_PATH  # type: ignore[attr-defined]
    assert filesystem.files == {RENAMED_PATH: b"written"}
    assert adapter.verification_paths == [filesystem.temporary_path, RENAMED_PATH]
    assert [
        event.stage
        for event in events.events
        if isinstance(event, FileStageChanged)
    ][-4:] == [
        FileTransactionStage.RENAMING,
        FileTransactionStage.VERIFYING_FINAL,
        FileTransactionStage.CLEANING_ORIGINAL,
        FileTransactionStage.COMPLETED,
    ]


def test_rename_move_failure_removes_temporary_and_preserves_original() -> None:
    filesystem = FakeFileSystem()
    filesystem.faults.add("rename")
    adapter = FakeAdapter(filesystem)

    result, _events = apply_with(filesystem, adapter, renamed_path=RENAMED_PATH)

    assert result.status is FileApplyStatus.FAILED  # type: ignore[attr-defined]
    assert result.completed_stage is FileTransactionStage.RENAMING  # type: ignore[attr-defined]
    assert issue_codes(result) == (MediaErrorCode.RENAME_FAILED,)
    assert filesystem.files == {SOURCE_PATH: b"original"}


def test_failed_final_rename_verification_deletes_output_and_keeps_original() -> None:
    filesystem = FakeFileSystem()
    adapter = FakeAdapter(filesystem)
    adapter.fail_final_verification = True

    result, _events = apply_with(filesystem, adapter, renamed_path=RENAMED_PATH)

    assert result.status is FileApplyStatus.FAILED  # type: ignore[attr-defined]
    assert result.final_path == SOURCE_PATH  # type: ignore[attr-defined]
    assert result.completed_stage is FileTransactionStage.VERIFYING_FINAL  # type: ignore[attr-defined]
    assert issue_codes(result) == (MediaErrorCode.VERIFICATION_FAILED,)
    assert filesystem.files == {SOURCE_PATH: b"original"}


def test_original_cleanup_failure_keeps_verified_rename_and_original() -> None:
    filesystem = FakeFileSystem()
    filesystem.faults.add("remove_original")
    adapter = FakeAdapter(filesystem)

    result, _events = apply_with(filesystem, adapter, renamed_path=RENAMED_PATH)

    assert result.status is FileApplyStatus.FAILED  # type: ignore[attr-defined]
    assert result.final_path == RENAMED_PATH  # type: ignore[attr-defined]
    assert result.completed_stage is FileTransactionStage.CLEANING_ORIGINAL  # type: ignore[attr-defined]
    assert issue_codes(result) == (MediaErrorCode.CLEANUP_FAILED,)
    assert filesystem.files == {
        SOURCE_PATH: b"original",
        RENAMED_PATH: b"written",
    }


def test_rename_only_change_skips_tag_write_but_verifies_both_outputs() -> None:
    source = make_source()
    filesystem = FakeFileSystem()
    adapter = FakeAdapter(filesystem)
    events = RecordingEventSink()

    result = TransactionalFileWriter(filesystem).apply_file(
        source=source,
        changes=make_rename_only_change_set(source, RENAMED_PATH),
        adapter=adapter,  # type: ignore[arg-type]
        backup=disabled_backup(),
        cancellation=NeverCancelledToken(),
        events=events,
    )

    assert result.status is FileApplyStatus.SUCCEEDED
    assert adapter.write_paths == []
    assert adapter.verification_paths == [filesystem.temporary_path, RENAMED_PATH]
    assert adapter.verification_arguments == [
        (source.read_result.metadata, frozenset(), STREAM_INFO),
        (source.read_result.metadata, frozenset(), STREAM_INFO),
    ]
    assert filesystem.files == {RENAMED_PATH: b"original"}
    assert FileTransactionStage.WRITING_METADATA not in {
        event.stage
        for event in events.events
        if isinstance(event, FileStageChanged)
    }


def test_pre_cancelled_file_returns_without_creating_a_temporary_copy() -> None:
    filesystem = FakeFileSystem()
    adapter = FakeAdapter(filesystem)
    cancellation = MutableCancellationToken()
    cancellation.cancel()

    result, events = apply_with(
        filesystem,
        adapter,
        cancellation=cancellation,
    )

    assert result.status is FileApplyStatus.CANCELLED
    assert result.completed_stage is FileTransactionStage.NOT_STARTED
    assert result.final_path == SOURCE_PATH
    assert result.issues == ()
    assert filesystem.files == {SOURCE_PATH: b"original"}
    assert isinstance(events.events[-1], FileCompleted)
    assert events.events[-1].status is FileApplyStatus.CANCELLED  # type: ignore[union-attr]


def test_cancellation_after_temporary_copy_aborts_before_metadata_write() -> None:
    filesystem = FakeFileSystem()
    adapter = FakeAdapter(filesystem)
    cancellation = MutableCancellationToken()
    filesystem.after_copy = cancellation.cancel

    result, _events = apply_with(
        filesystem,
        adapter,
        cancellation=cancellation,
    )

    assert result.status is FileApplyStatus.CANCELLED
    assert result.completed_stage is FileTransactionStage.COPYING_TEMPORARY
    assert adapter.write_paths == []
    assert filesystem.files == {SOURCE_PATH: b"original"}


def test_cancellation_after_temporary_verification_aborts_before_commit() -> None:
    filesystem = FakeFileSystem()
    adapter = FakeAdapter(filesystem)
    cancellation = MutableCancellationToken()
    adapter.after_verification = lambda path: cancellation.cancel()

    result, _events = apply_with(
        filesystem,
        adapter,
        cancellation=cancellation,
    )

    assert result.status is FileApplyStatus.CANCELLED
    assert result.completed_stage is FileTransactionStage.VERIFYING_TEMPORARY
    assert filesystem.files == {SOURCE_PATH: b"original"}


# Once replacement begins, cancellation must wait for a safe terminal result;
# reporting an aborted transaction here would misdescribe the committed file.
def test_cancellation_requested_after_replace_does_not_interrupt_current_file() -> None:
    filesystem = FakeFileSystem()
    adapter = FakeAdapter(filesystem)
    cancellation = MutableCancellationToken()
    filesystem.after_replace = cancellation.cancel

    result, _events = apply_with(
        filesystem,
        adapter,
        cancellation=cancellation,
    )

    assert cancellation.is_cancelled() is True
    assert result.status is FileApplyStatus.SUCCEEDED
    assert filesystem.files == {SOURCE_PATH: b"written"}


def test_cancellation_requested_after_rename_publish_finishes_current_file() -> None:
    filesystem = FakeFileSystem()
    adapter = FakeAdapter(filesystem)
    cancellation = MutableCancellationToken()
    filesystem.after_move = cancellation.cancel

    result, _events = apply_with(
        filesystem,
        adapter,
        renamed_path=RENAMED_PATH,
        cancellation=cancellation,
    )

    assert cancellation.is_cancelled() is True
    assert result.status is FileApplyStatus.SUCCEEDED
    assert filesystem.files == {RENAMED_PATH: b"written"}


def test_enabled_backup_mirrors_scan_relative_path_before_modification() -> None:
    filesystem = FakeFileSystem()
    adapter = FakeAdapter(filesystem)
    backup = BackupPolicy(
        enabled=True,
        root=Path("backups"),
        scan_root=Path("library"),
        operation_id="APPLY-0042",
    )
    backup_path = Path("backups/APPLY-0042/disc/track.flac")

    result, events = apply_with(filesystem, adapter, backup=backup)

    assert result.status is FileApplyStatus.SUCCEEDED
    assert filesystem.files == {
        SOURCE_PATH: b"written",
        backup_path: b"original",
    }
    assert [
        event.stage
        for event in events.events
        if isinstance(event, FileStageChanged)
    ][0] is FileTransactionStage.BACKING_UP


@pytest.mark.parametrize(
    ("backup", "fault"),
    (
        (
            BackupPolicy(
                enabled=True,
                root=Path("backups"),
                scan_root=Path("library"),
                operation_id="APPLY-0001",
            ),
            "copy_backup",
        ),
        (
            BackupPolicy(
                enabled=True,
                root=Path("backups"),
                scan_root=Path("different-root"),
                operation_id="APPLY-0001",
            ),
            None,
        ),
    ),
)
def test_backup_failure_stops_before_a_temporary_or_metadata_write(
    backup: BackupPolicy,
    fault: str | None,
) -> None:
    filesystem = FakeFileSystem()

    if fault is not None:
        filesystem.faults.add(fault)

    adapter = FakeAdapter(filesystem)

    result, _events = apply_with(filesystem, adapter, backup=backup)

    assert result.status is FileApplyStatus.FAILED
    assert result.completed_stage is FileTransactionStage.BACKING_UP
    assert issue_codes(result) == (MediaErrorCode.BACKUP_FAILED,)
    assert adapter.write_paths == []
    assert filesystem.files == {SOURCE_PATH: b"original"}


def test_semantic_no_op_does_not_create_an_enabled_backup() -> None:
    source = make_source()
    filesystem = FakeFileSystem()
    adapter = FakeAdapter(filesystem)
    events = RecordingEventSink()
    backup = BackupPolicy(
        enabled=True,
        root=Path("backups"),
        scan_root=Path("library"),
        operation_id="APPLY-0001",
    )

    result = TransactionalFileWriter(filesystem).apply_file(
        source=source,
        changes=make_no_op_change_set(source),
        adapter=adapter,  # type: ignore[arg-type]
        backup=backup,
        cancellation=NeverCancelledToken(),
        events=events,
    )

    assert result.status is FileApplyStatus.SUCCEEDED
    assert filesystem.files == {SOURCE_PATH: b"original"}
    assert adapter.write_paths == []
    assert adapter.verification_paths == []
    assert FileTransactionStage.BACKING_UP not in {
        event.stage
        for event in events.events
        if isinstance(event, FileStageChanged)
    }
