"""Protect the source actually displaced by a Windows same-path metadata commit."""

import ctypes
import errno
import os
from pathlib import Path

import pytest

from metadata_polisher.application.changes import RenameDecision, build_change_set
from metadata_polisher.application.review import set_manual_decision
from metadata_polisher.domain.errors import MediaErrorCode
from metadata_polisher.domain.metadata import MetadataField
from metadata_polisher.execution.cancellation import NeverCancelledToken
from metadata_polisher.execution.events import FileApplyStatus
from metadata_polisher.formats.registry import FormatRegistry
from metadata_polisher.formats.wave import WaveAdapter
from metadata_polisher.infrastructure import filesystem
from metadata_polisher.infrastructure.filesystem import LocalFileSystem
from metadata_polisher.infrastructure.transaction import BackupPolicy, TransactionalFileWriter
from metadata_polisher.scanner.scanner import scan_media
from metadata_polisher.session.review_previews import local_reviews
from tests.integration.formats.test_format_contract import make_wave_fixture
from tests.integration.formats.test_transaction_with_adapter import RecordingEventSink

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Requires native Windows file replacement.")


def commit_title(path, boundary):
    (source,) = scan_media(path.parent, FormatRegistry()).supported_files
    reviews = tuple(
        set_manual_decision(review, "Edited") if review.field is MetadataField.TITLE else review
        for review in local_reviews(source).reviews
    )
    return TransactionalFileWriter(boundary).apply_file(
        source, build_change_set(source, reviews, RenameDecision.KEEP_FILENAME), WaveAdapter(),
        BackupPolicy(False, None, path.parent, "replace"), NeverCancelledToken(), RecordingEventSink(),
    )


def test_in_place_external_writer_cannot_open_reviewed_source_at_native_commit(tmp_path):
    path = tmp_path / "sample.wav"
    make_wave_fixture(path)
    blocked = []

    class EditingBoundary(LocalFileSystem):
        def replace_file(self, temporary, destination, **kwargs):
            try:
                destination.write_bytes(b"external")
            except PermissionError as error:
                blocked.append(error.errno)
            super().replace_file(temporary, destination, **kwargs)

    result = commit_title(path, EditingBoundary())
    assert blocked == [errno.EACCES]
    assert result.status is FileApplyStatus.SUCCEEDED
    assert WaveAdapter().read(path).metadata.title == "Edited"
    assert list(tmp_path.iterdir()) == [path]


def test_path_swap_at_native_commit_preserves_external_bytes_and_reports_location(tmp_path):
    path = tmp_path / "sample.wav"
    moved = tmp_path / "moved.wav"
    make_wave_fixture(path)
    approved = path.read_bytes()

    class SwappingBoundary(LocalFileSystem):
        def replace_file(self, temporary, destination, **kwargs):
            destination.rename(moved)
            destination.write_bytes(b"external")
            super().replace_file(temporary, destination, **kwargs)

    result = commit_title(path, SwappingBoundary())
    assert result.status is FileApplyStatus.FAILED
    assert result.issues[0].code is MediaErrorCode.CLEANUP_FAILED
    assert moved.read_bytes() == approved
    assert WaveAdapter().read(path).metadata.title == "Edited"
    (recovered,) = [p for p in tmp_path.rglob("*") if p.is_file() and p not in (path, moved)]
    assert recovered.read_bytes() == b"external"
    assert str(recovered) in result.issues[0].message


def test_partially_failed_native_replacement_preserves_and_reports_both_files(tmp_path, monkeypatch):
    path = tmp_path / "sample.wav"
    make_wave_fixture(path)
    approved = path.read_bytes()
    locations = []

    def partial_replace(temporary: Path, destination: Path, recovery: Path) -> None:
        destination.rename(recovery)
        locations.extend((temporary, recovery))
        # ERROR_UNABLE_TO_MOVE_REPLACEMENT_2: Windows moved the old file but
        # could not publish the new one. Both files are still recoverable.
        raise ctypes.WinError(1177)

    monkeypatch.setattr(filesystem, "replace_with_recovery", partial_replace, raising=False)
    result = commit_title(path, LocalFileSystem())
    assert result.status is FileApplyStatus.FAILED
    assert result.issues[0].code is MediaErrorCode.CLEANUP_FAILED
    temporary, recovery = locations
    assert not path.exists()
    assert recovery.read_bytes() == approved
    assert WaveAdapter().read(temporary).metadata.title == "Edited"
    assert all(str(location) in result.issues[0].message for location in locations)


def test_external_replacement_after_publication_preserves_approved_original(tmp_path, monkeypatch):
    path = tmp_path / "sample.wav"
    moved = tmp_path / "moved.wav"
    make_wave_fixture(path)
    approved = path.read_bytes()
    publish = filesystem.replace_with_recovery
    recovered = []

    def swap_after_native_commit(temporary, destination, recovery):
        publish(temporary, destination, recovery)
        recovered.append(recovery)
        destination.rename(moved)
        destination.write_bytes(b"external")

    monkeypatch.setattr(filesystem, "replace_with_recovery", swap_after_native_commit)
    result = commit_title(path, LocalFileSystem())
    assert result.status is FileApplyStatus.FAILED
    assert result.issues[0].code is MediaErrorCode.CLEANUP_FAILED
    assert path.read_bytes() == b"external"
    assert WaveAdapter().read(moved).metadata.title == "Edited"
    assert recovered[0].read_bytes() == approved
    assert str(recovered[0]) in result.issues[0].message
