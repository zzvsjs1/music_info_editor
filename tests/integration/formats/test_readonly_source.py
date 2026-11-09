"""The offline Apply preflight and direct writer preserve a read-only source."""

import shutil
import stat

from metadata_polisher.application.apply import LocalApplyPreflightInspector
from metadata_polisher.application.changes import RenameDecision, build_change_set
from metadata_polisher.application.review import set_manual_decision
from metadata_polisher.domain.errors import MediaErrorCode
from metadata_polisher.domain.metadata import MetadataField
from metadata_polisher.execution.cancellation import NeverCancelledToken
from metadata_polisher.execution.events import FileApplyStatus
from metadata_polisher.formats.registry import FormatRegistry
from metadata_polisher.formats.wave import WaveAdapter
from metadata_polisher.infrastructure.transaction import BackupPolicy, TransactionalFileWriter
from metadata_polisher.scanner.scanner import scan_media
from metadata_polisher.session.review_previews import local_reviews
from tests.integration.formats.test_format_contract import make_wave_fixture
from tests.integration.formats.test_transaction_with_adapter import RecordingEventSink


def test_preflight_and_direct_writer_reject_read_only_source_without_leaving_a_copy(tmp_path):
    path = tmp_path / "read-only.wav"
    make_wave_fixture(path)
    original = path.read_bytes()
    path.chmod(stat.S_IREAD)
    try:
        (source,) = scan_media(tmp_path, FormatRegistry()).supported_files
        reviews = tuple(
            set_manual_decision(review, "Edited") if review.field is MetadataField.TITLE else review
            for review in local_reviews(source).reviews
        )
        changes = build_change_set(source, reviews, RenameDecision.KEEP_FILENAME)
        backup = BackupPolicy(False, None, tmp_path, "read-only")
        snapshot = LocalApplyPreflightInspector().inspect(source, changes, backup)
        result = TransactionalFileWriter().apply_file(
            source, changes, WaveAdapter(), backup, NeverCancelledToken(), RecordingEventSink(),
        )
        checked = build_change_set(source, reviews, RenameDecision.KEEP_FILENAME, validation=snapshot.validation)
        assert any("read-only" in issue.message for issue in checked.validation.issues)
        assert result.status is FileApplyStatus.FAILED
        assert result.issues[0].code is MediaErrorCode.PERMISSION_DENIED
        assert "read-only" in result.issues[0].message
        assert path.read_bytes() == original
        assert not path.stat().st_mode & stat.S_IWRITE
        assert list(tmp_path.iterdir()) == [path]
    finally:
        # A failing regression may have left its own read-only temporary copy.
        for child in tmp_path.iterdir():
            child.chmod(stat.S_IREAD | stat.S_IWRITE)


def test_source_becoming_read_only_during_copy_leaves_no_orphan(tmp_path, monkeypatch):
    path = tmp_path / "becomes-read-only.wav"
    make_wave_fixture(path)
    original = path.read_bytes()
    (source,) = scan_media(tmp_path, FormatRegistry()).supported_files
    reviews = tuple(
        set_manual_decision(review, "Edited") if review.field is MetadataField.TITLE else review
        for review in local_reviews(source).reviews
    )
    changes = build_change_set(source, reviews, RenameDecision.KEEP_FILENAME)
    copy = shutil.copy2

    def make_read_only_then_copy(src, dst):
        src.chmod(stat.S_IREAD)
        return copy(src, dst)

    monkeypatch.setattr(shutil, "copy2", make_read_only_then_copy)
    try:
        result = TransactionalFileWriter().apply_file(
            source, changes, WaveAdapter(), BackupPolicy(False, None, tmp_path, "late-read-only"),
            NeverCancelledToken(), RecordingEventSink(),
        )
        assert result.status is FileApplyStatus.FAILED
        assert result.issues[0].code in (MediaErrorCode.SOURCE_CHANGED, MediaErrorCode.PERMISSION_DENIED)
        assert len(result.issues) == 1
        assert path.read_bytes() == original
        assert not path.stat().st_mode & stat.S_IWRITE
        assert list(tmp_path.iterdir()) == [path]
    finally:
        for child in tmp_path.iterdir():
            child.chmod(stat.S_IREAD | stat.S_IWRITE)
