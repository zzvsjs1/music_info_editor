"""Exercise the scanned edit baseline against an external, timestamp-preserving tagger."""

import os
from dataclasses import replace
from pathlib import Path

import pytest

from metadata_polisher.application.changes import ChangeValidationResult, FileChangeSet, RenameDecision
from metadata_polisher.domain.errors import MediaErrorCode
from metadata_polisher.domain.metadata import MetadataChange, MetadataField
from metadata_polisher.execution.cancellation import NeverCancelledToken
from metadata_polisher.execution.events import FileApplyStatus, FileStageChanged, FileTransactionStage, OperationEvent
from metadata_polisher.formats.registry import FormatRegistry
from metadata_polisher.formats.wave import WaveAdapter
from metadata_polisher.infrastructure import transaction
from metadata_polisher.infrastructure.transaction import BackupPolicy, TransactionalFileWriter
from metadata_polisher.scanner.scanner import scan_media
from tests.integration.formats.test_format_contract import make_wave_fixture


@pytest.mark.parametrize("edit_at_commit", [False, True], ids=["after-scan", "after-copy-verification"])
@pytest.mark.parametrize("coarse_version", [False, True], ids=["filesystem-clock", "unchanged-clock"])
def test_external_equal_length_tag_edit_with_restored_mtime_survives_apply(
    tmp_path: Path, edit_at_commit: bool, coarse_version: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "sample.wav"
    make_wave_fixture(path)
    adapter = WaveAdapter()
    field = MetadataField.TITLE
    adapter.write_changes(path, (MetadataChange(field, None, "first"),))
    (source,) = scan_media(tmp_path, FormatRegistry((adapter,))).supported_files
    original_stat = path.stat()

    if coarse_version:
        # The filesystem is allowed to report two edits within one timestamp
        # tick. The reviewed field precondition must still protect the user's edit.
        monkeypatch.setattr(transaction, "read_file_version", lambda _path: source.file_version)

    changes = FileChangeSet(
        file_id=source.file_id,
        metadata_changes=(MetadataChange(field, "first", "final"),),
        rename_change=None,
        final_metadata=replace(source.read_result.metadata, title="final"),
        rename_decision=RenameDecision.KEEP_FILENAME,
        rename_preview=None,
        validation=ChangeValidationResult(),
    )

    def edit_externally() -> None:
        adapter.write_changes(path, (MetadataChange(field, "first", "human"),))
        os.utime(path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
        changed_stat = path.stat()
        assert (changed_stat.st_ino, changed_stat.st_size, changed_stat.st_mtime_ns) == (
            original_stat.st_ino, original_stat.st_size, original_stat.st_mtime_ns,
        )

    class Events:
        def emit(self, event: OperationEvent) -> None:
            if (
                edit_at_commit
                and isinstance(event, FileStageChanged)
                and event.stage is FileTransactionStage.COMMITTING
            ):
                edit_externally()

    if not edit_at_commit:
        edit_externally()

    result = TransactionalFileWriter().apply_file(
        source, changes, adapter, BackupPolicy(False, None, tmp_path, "stale-title"), NeverCancelledToken(), Events(),
    )

    assert result.status is FileApplyStatus.FAILED
    assert result.issues[0].code is MediaErrorCode.SOURCE_CHANGED
    assert adapter.read(path).metadata.title == "human"
    assert list(tmp_path.iterdir()) == [path]
