"""Backup completion remains explicit across the transaction's terminal paths."""

from pathlib import Path

import pytest

from metadata_polisher.execution.cancellation import MutableCancellationToken
from metadata_polisher.execution.events import FileApplyStatus
from metadata_polisher.infrastructure.transaction import BackupPolicy
from tests.unit.infrastructure.test_transaction import SOURCE_PATH, FakeAdapter, FakeFileSystem, apply_with


@pytest.mark.parametrize("terminal", ["cancel", "write_failure", "copy_failure", "success", "backup_failure"])
def test_completed_backup_path_survives_cancellation_and_later_failures(terminal):
    filesystem = FakeFileSystem()
    adapter = FakeAdapter(filesystem)
    token = MutableCancellationToken()

    if terminal == "cancel":
        filesystem.after_copy = token.cancel
    elif terminal == "write_failure":
        adapter.fail_write = True
    elif terminal == "copy_failure":
        filesystem.faults.add("copy_temporary")
    elif terminal == "backup_failure":
        filesystem.faults.add("copy_backup")

    result, _ = apply_with(
        filesystem, adapter, cancellation=token,
        backup=BackupPolicy(True, Path("backups"), Path("library"), "APPLY-backup"),
    )
    destination = Path("backups/APPLY-backup/disc/track.flac")

    if terminal == "backup_failure":
        assert result.status is FileApplyStatus.FAILED
        assert getattr(result, "completed_backup_path", None) is None
        assert destination not in filesystem.files
    else:
        assert filesystem.files[destination] == b"original"
        assert getattr(result, "completed_backup_path", None) == destination

    if terminal == "cancel":
        assert result.status is FileApplyStatus.CANCELLED
        assert filesystem.files[SOURCE_PATH] == b"original"
