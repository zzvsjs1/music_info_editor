from pathlib import Path

import pytest

from metadata_polisher.application.changes import (
    ChangeValidationResult,
    FileChangeSet,
    RenameChange,
    RenameDecision,
)
from metadata_polisher.domain.errors import Issue, MediaErrorCode
from metadata_polisher.domain.media import LocalMediaFile, MediaReadResult, StreamInfo
from metadata_polisher.domain.metadata import (
    FieldReadState,
    MetadataChange,
    MetadataField,
    MetadataSnapshot,
    Position,
)
from metadata_polisher.execution.cancellation import NeverCancelledToken
from metadata_polisher.execution.events import (
    FileApplyStatus,
    FileCompleted,
    FileOperationEvent,
    FileStageChanged,
    FileTransactionStage,
)
from metadata_polisher.formats.base import VerificationResult
from metadata_polisher.infrastructure.filesystem import read_file_version
from metadata_polisher.infrastructure.transaction import (
    BackupPolicy,
    TransactionalFileWriter,
)


# Real filesystem operations are paired with a transparent text payload so
# copy/verify/publish ordering can be examined without format-library behaviour.
class SimpleTextAdapter:
    """Tiny real-file adapter used to exercise transaction ordering end to end."""

    format_id = "simple"
    extensions = frozenset((".simple",))

    def write_changes(self, path: Path, changes: tuple[MetadataChange, ...]) -> None:
        values = {
            line.partition("=")[0]: line.partition("=")[2]
            for line in path.read_text(encoding="utf-8").splitlines()
        }

        for change in changes:
            if change.field is not MetadataField.TITLE or not isinstance(change.new_value, str):
                raise TypeError("The simple adapter only supports a text title")

            values["title"] = change.new_value

        path.write_text(
            f"title={values['title']}\npayload={values['payload']}\n",
            encoding="utf-8",
        )

    def verify(
        self,
        path: Path,
        expected: MetadataSnapshot,
        changed_fields: frozenset[MetadataField],
        baseline_stream: StreamInfo,
    ) -> VerificationResult:
        text = path.read_text(encoding="utf-8")
        # The unchanged payload sentinel makes verification reject a correct
        # title written at the cost of the original media content in this fixture.
        expected_lines = f"title={expected.title}\npayload=audio-bytes-stay\n"
        expected_fields = frozenset((MetadataField.TITLE,))
        stream_unchanged = baseline_stream.codec == "SIMPLE"

        if text == expected_lines and changed_fields == expected_fields and stream_unchanged:
            return VerificationResult(ok=True)

        return VerificationResult(
            ok=False,
            issues=(
                Issue(
                    code=MediaErrorCode.VERIFICATION_FAILED,
                    message="Simple media content or stable stream properties changed.",
                ),
            ),
        )


class RecordingEventSink:
    def __init__(self) -> None:
        self.events: list[FileOperationEvent] = []

    def emit(self, event: FileOperationEvent) -> None:
        self.events.append(event)


def test_real_temporary_copy_is_verified_renamed_and_cleans_the_original(
    tmp_path: Path,
) -> None:
    album_directory = tmp_path / "album"
    album_directory.mkdir()
    source_path = album_directory / "raw.simple"
    destination_path = album_directory / "01. New title.simple"
    source_path.write_text(
        "title=Old title\npayload=audio-bytes-stay\n",
        encoding="utf-8",
    )
    source_metadata = MetadataSnapshot(
        title="Old title",
        track=Position(number=1, total=1),
    )
    source = LocalMediaFile(
        path=source_path,
        format_id="simple",
        read_result=MediaReadResult(
            metadata=source_metadata,
            field_states={
                field: (
                    FieldReadState.PRESENT
                    if getattr(source_metadata, field.value)
                    else FieldReadState.MISSING
                )
                for field in MetadataField
            },
            stream_info=StreamInfo(
                duration_seconds=10.0,
                sample_rate=44_100,
                channels=2,
                bit_depth=16,
                codec="SIMPLE",
            ),
        ),
        file_id="simple-file",
    )
    rename_change = RenameChange(source_path, destination_path)
    changes = FileChangeSet(
        file_id=source.file_id,
        metadata_changes=(
            MetadataChange(MetadataField.TITLE, "Old title", "New title"),
        ),
        rename_change=rename_change,
        final_metadata=MetadataSnapshot(
            title="New title",
            track=Position(number=1, total=1),
        ),
        rename_decision=RenameDecision.APPLY_RENAME,
        rename_preview=rename_change,
        validation=ChangeValidationResult(),
    )
    events = RecordingEventSink()

    result = TransactionalFileWriter().apply_file(
        source=source,
        changes=changes,
        adapter=SimpleTextAdapter(),  # type: ignore[arg-type]
        backup=BackupPolicy(
            enabled=False,
            root=None,
            scan_root=album_directory,
            operation_id="APPLY-0001",
        ),
        cancellation=NeverCancelledToken(),
        events=events,
    )

    assert result.status is FileApplyStatus.SUCCEEDED
    assert result.source_path == source_path
    assert result.final_path == destination_path
    assert not source_path.exists()
    assert destination_path.read_text(encoding="utf-8") == (
        "title=New title\npayload=audio-bytes-stay\n"
    )
    assert not tuple(album_directory.glob(".metadata-polisher-*.simple"))
    assert isinstance(events.events[-1], FileCompleted)


@pytest.mark.parametrize(
    "edit_stage,rename",
    (
        (FileTransactionStage.BACKING_UP, False),
        (FileTransactionStage.COPYING_TEMPORARY, False),
        (FileTransactionStage.COMMITTING, False),
        (FileTransactionStage.RENAMING, True),
        (FileTransactionStage.CLEANING_ORIGINAL, True),
    ),
)
def test_external_edits_at_write_boundaries_preserve_the_source(
    tmp_path: Path, edit_stage: FileTransactionStage, rename: bool,
) -> None:
    source_path = tmp_path / "raw.simple"
    destination = tmp_path / "renamed.simple"
    source_path.write_text("title=Old title\npayload=audio-bytes-stay\n", encoding="utf-8")
    source = LocalMediaFile(
        path=source_path,
        format_id="simple",
        file_id="simple-file",
        read_result=MediaReadResult(
            metadata=MetadataSnapshot(title="Old title"),
            field_states={field: FieldReadState.MISSING for field in MetadataField},
            stream_info=StreamInfo(10.0, 44_100, 2, 16, "SIMPLE"),
        ),
        file_version=read_file_version(source_path),
    )
    rename_change = RenameChange(source_path, destination) if rename else None
    changes = FileChangeSet(
        file_id=source.file_id,
        metadata_changes=(MetadataChange(MetadataField.TITLE, "Old title", "New title"),),
        rename_change=rename_change,
        final_metadata=MetadataSnapshot(title="New title"),
        rename_decision=RenameDecision.APPLY_RENAME if rename else RenameDecision.KEEP_FILENAME,
        rename_preview=rename_change,
        validation=ChangeValidationResult(),
    )
    external_text = "title=Old title\npayload=audio-bytes-stay\nartist=External artist\n"

    class ExternalEditor(RecordingEventSink):
        def emit(self, event: FileOperationEvent) -> None:
            super().emit(event)

            if isinstance(event, FileStageChanged) and event.stage is edit_stage:
                source_path.write_text(external_text, encoding="utf-8")

    result = TransactionalFileWriter().apply_file(
        source, changes, SimpleTextAdapter(),  # type: ignore[arg-type]
        BackupPolicy(True, tmp_path / "backups", tmp_path, "conflict"),
        NeverCancelledToken(), ExternalEditor(),
    )

    assert result.status is FileApplyStatus.FAILED
    assert result.issues[0].code is MediaErrorCode.SOURCE_CHANGED
    published = edit_stage is FileTransactionStage.CLEANING_ORIGINAL
    assert result.final_path == (destination if published else source_path)
    assert source_path.read_text(encoding="utf-8") == external_text
    assert destination.exists() is published
    assert not tuple(tmp_path.glob(".metadata-polisher-*.simple"))


def test_source_moved_by_an_external_editor_does_not_delete_its_new_path(tmp_path: Path) -> None:
    source_path = tmp_path / "raw.simple"
    destination = tmp_path / "renamed.simple"
    original = "title=Old title\npayload=audio-bytes-stay\n"
    source_path.write_text(original, encoding="utf-8")
    source = LocalMediaFile(
        path=source_path, format_id="simple", file_id="simple-file",
        read_result=MediaReadResult(
            metadata=MetadataSnapshot(title="Old title"),
            field_states={field: FieldReadState.MISSING for field in MetadataField},
            stream_info=StreamInfo(10.0, 44_100, 2, 16, "SIMPLE"),
        ),
        file_version=read_file_version(source_path),
    )
    rename = RenameChange(source_path, destination)
    changes = FileChangeSet(
        file_id=source.file_id,
        metadata_changes=(MetadataChange(MetadataField.TITLE, "Old title", "New title"),),
        rename_change=rename, final_metadata=MetadataSnapshot(title="New title"),
        rename_decision=RenameDecision.APPLY_RENAME, rename_preview=rename,
        validation=ChangeValidationResult(),
    )

    class MovingEditor(RecordingEventSink):
        def emit(self, event: FileOperationEvent) -> None:
            super().emit(event)

            if isinstance(event, FileStageChanged) and event.stage is FileTransactionStage.CLEANING_ORIGINAL:
                source_path.replace(destination)

    result = TransactionalFileWriter().apply_file(
        source, changes, SimpleTextAdapter(),  # type: ignore[arg-type]
        BackupPolicy(False, None, tmp_path, "conflict"), NeverCancelledToken(), MovingEditor(),
    )

    assert result.status is FileApplyStatus.FAILED
    assert result.issues[0].code is MediaErrorCode.SOURCE_CHANGED
    assert result.final_path == destination
    assert destination.read_text(encoding="utf-8") == original
