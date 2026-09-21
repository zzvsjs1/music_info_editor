"""Results count a finished backup even when cancellation follows its copy."""

from dataclasses import replace

from PySide6.QtWidgets import QLabel

from metadata_polisher.application.apply import ApplyService, LocalApplyPreflightInspector
from metadata_polisher.domain.metadata import MetadataField
from metadata_polisher.infrastructure.filesystem import LocalFileSystem
from metadata_polisher.infrastructure.settings import BackupSettings
from metadata_polisher.infrastructure.transaction import TransactionalFileWriter
from tests.ui.test_apply_batch_completion import complete_apply, edit_file, scanned_window


def test_results_count_backup_completed_before_cancellation(qtbot, tmp_path, monkeypatch):
    class CancelAfterBackup(LocalFileSystem):
        def copy_file(self, source, destination, *, overwrite):
            super().copy_file(source, destination, overwrite=overwrite)

            if not overwrite:
                # ControlledExecutor runs the real writer synchronously here;
                # cancellation still passes through its normal safe boundary.
                window.operation_controller.cancel_active()

    service = ApplyService(
        preflight=LocalApplyPreflightInspector(), writer=TransactionalFileWriter(CancelAfterBackup()),
    )
    window, executor = scanned_window(qtbot, tmp_path, service=service)
    group = window.session_state.groups[0]
    source = group.group.files[0]
    original = source.path.read_bytes()
    window.library_controller.settings = replace(
        window.library_controller.settings, backup=BackupSettings(True, str(tmp_path / "backups")),
    )
    edit_file(window, group, MetadataField.TITLE, "Reviewed title")
    window.set_included_file_ids(frozenset({source.file_id}))
    complete_apply(qtbot, window, executor, monkeypatch)
    result = window.apply_controller.last_result
    backup = tmp_path / "backups" / result.operation_id / "First album" / "original.wav"

    assert backup.read_bytes() == original
    assert source.path.read_bytes() == original
    labels = " ".join(label.text() for label in window.findChildren(QLabel))
    assert "Backups: 1 completed" in labels
