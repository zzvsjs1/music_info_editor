"""Exercise Windows file sharing at the publish, verify and cleanup boundaries."""

import os
import stat
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from metadata_polisher.domain.media import StreamInfo
from metadata_polisher.domain.metadata import MetadataField, MetadataSnapshot
from metadata_polisher.execution.cancellation import NeverCancelledToken
from metadata_polisher.execution.events import FileApplyStatus, FileStageChanged, FileTransactionStage
from metadata_polisher.formats.base import VerificationResult
from metadata_polisher.infrastructure.filesystem import LocalFileSystem, read_file_version
from metadata_polisher.infrastructure.transaction import TransactionalFileWriter
from tests.unit.infrastructure.test_transaction import (
    RecordingEventSink,
    disabled_backup,
    make_rename_only_change_set,
    make_source,
)

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Requires Windows file sharing semantics.")


class PayloadVerifier:
    def verify(
        self, path: Path, expected: MetadataSnapshot, changed_fields: frozenset[MetadataField], baseline: StreamInfo
    ) -> VerificationResult:
        return VerificationResult(ok=path.read_bytes() == b"owned")


@pytest.mark.parametrize("stage", [FileTransactionStage.VERIFYING_FINAL, FileTransactionStage.CLEANING_ORIGINAL])
def test_published_file_stays_protected_until_original_cleanup(tmp_path, stage):
    source = make_source(tmp_path / "source.flac")
    destination = tmp_path / "new.flac"
    external = tmp_path / "external.flac"
    source.path.write_bytes(b"owned")
    external.write_bytes(b"external")
    sharing_violations = []

    class ExternalEditor:
        def emit(self, event):
            if isinstance(event, FileStageChanged) and event.stage is stage:
                try:
                    if stage is FileTransactionStage.VERIFYING_FINAL:
                        external.replace(destination)
                    else:
                        destination.unlink()
                except PermissionError as error:
                    sharing_violations.append(error.winerror)

    result = TransactionalFileWriter().apply_file(
        source, make_rename_only_change_set(source, destination), PayloadVerifier(),
        disabled_backup(), NeverCancelledToken(), ExternalEditor(),
    )

    assert len(sharing_violations) == 1 and sharing_violations[0] in {5, 32}
    assert result.status is FileApplyStatus.SUCCEEDED
    assert destination.read_bytes() == b"owned"
    assert not source.path.exists()


def test_replacement_between_publication_and_guard_acquisition_is_preserved(tmp_path, monkeypatch):
    source = make_source(tmp_path / "source.flac")
    destination = tmp_path / "new.flac"
    external = tmp_path / "external.flac"
    source.path.write_bytes(b"owned")
    external.write_bytes(b"external")
    publish = LocalFileSystem.move_file_no_replace

    def replace_after_publish(filesystem, temporary, target):
        publish(filesystem, temporary, target)
        target.rename(tmp_path / "moved.flac")
        external.rename(target)

    monkeypatch.setattr(LocalFileSystem, "move_file_no_replace", replace_after_publish)

    result = TransactionalFileWriter().apply_file(
        source, make_rename_only_change_set(source, destination), PayloadVerifier(),
        disabled_backup(), NeverCancelledToken(), RecordingEventSink(),
    )

    assert result.status is FileApplyStatus.FAILED
    assert source.path.read_bytes() == b"owned"
    assert destination.read_bytes() == b"external"


def test_staging_replacement_after_verification_is_not_published(tmp_path):
    source = make_source(tmp_path / "source.flac")
    destination = tmp_path / "new.flac"
    source.path.write_bytes(b"owned")
    replacements = []

    class Editor:
        def emit(self, event):
            if isinstance(event, FileStageChanged) and event.stage is FileTransactionStage.RENAMING:
                staged, = tmp_path.glob(".metadata-polisher-*.flac")
                staged.rename(tmp_path / "moved.flac")
                staged.write_bytes(b"external")
                replacements.append(staged)

    class SameTagAndStreamFacts(PayloadVerifier):
        def verify(self, path, expected, changed_fields, baseline):
            return VerificationResult(ok=True)

    result = TransactionalFileWriter().apply_file(
        source, make_rename_only_change_set(source, destination), SameTagAndStreamFacts(),
        disabled_backup(), NeverCancelledToken(), Editor(),
    )
    assert result.status is FileApplyStatus.FAILED
    assert source.path.read_bytes() == b"owned"
    assert not destination.exists()
    assert replacements[0].read_bytes() == b"external"


def test_unavailable_file_id_cannot_authorise_publication(tmp_path, monkeypatch):
    from metadata_polisher.infrastructure import filesystem

    source = make_source(tmp_path / "source.flac")
    destination = tmp_path / "new.flac"
    source.path.write_bytes(b"owned")
    real_version = filesystem.read_descriptor_version

    def unavailable_id(descriptor):
        volume, _, size, modified, changed = real_version(descriptor)
        return volume, 0, size, modified, changed

    monkeypatch.setattr(filesystem, "read_descriptor_version", unavailable_id)
    result = TransactionalFileWriter().apply_file(
        source, make_rename_only_change_set(source, destination), PayloadVerifier(),
        disabled_backup(), NeverCancelledToken(), RecordingEventSink(),
    )
    assert result.status is FileApplyStatus.FAILED
    assert source.path.read_bytes() == b"owned"
    assert not destination.exists()


def test_failed_final_verification_cannot_remove_a_later_replacement(tmp_path, monkeypatch):
    source = make_source(tmp_path / "source.flac")
    destination = tmp_path / "new.flac"
    source.path.write_bytes(b"owned")
    remove = LocalFileSystem.remove_file

    class FailPublishedVerification(PayloadVerifier):
        def verify(self, path, expected, changed_fields, baseline):
            return VerificationResult(ok=path != destination)

    def replace_before_rollback(filesystem, path, *, expected_version=None):
        if path == destination:
            destination.rename(tmp_path / "moved.flac")
            destination.write_bytes(b"external")
        return remove(filesystem, path, expected_version=expected_version)

    monkeypatch.setattr(LocalFileSystem, "remove_file", replace_before_rollback)
    result = TransactionalFileWriter().apply_file(
        source, make_rename_only_change_set(source, destination), FailPublishedVerification(),
        disabled_backup(), NeverCancelledToken(), RecordingEventSink(),
    )
    assert result.status is FileApplyStatus.FAILED
    assert result.final_path == source.path
    assert source.path.read_bytes() == b"owned"
    assert destination.read_bytes() == b"external"


def test_failed_verification_reports_retained_destination_when_a_reader_prevents_removal(tmp_path):
    source = make_source(tmp_path / "source.flac")
    source.path.write_bytes(b"owned")
    destination = tmp_path / "renamed.flac"

    with ExitStack() as reader:
        class FailPublishedVerification(PayloadVerifier):
            def verify(self, path, expected, changed_fields, baseline):
                if path == destination:
                    # A normal Windows reader allows verification to finish but
                    # prevents the rollback from obtaining DELETE access.
                    reader.enter_context(path.open("rb"))
                    return VerificationResult(ok=False)

                return super().verify(path, expected, changed_fields, baseline)

        result = TransactionalFileWriter().apply_file(
            source, make_rename_only_change_set(source, destination), FailPublishedVerification(),
            disabled_backup(), NeverCancelledToken(), RecordingEventSink(),
        )

    assert result.status is FileApplyStatus.FAILED
    assert result.final_path == destination
    assert source.path.read_bytes() == destination.read_bytes() == b"owned"


def test_rename_through_a_file_symlink_preserves_its_target(tmp_path):
    target = tmp_path / "target.flac"
    target.write_bytes(b"owned")
    source = make_source(tmp_path / "source.flac")
    try:
        source.path.symlink_to(target)
    except OSError as error:
        if error.winerror == 1314:
            pytest.skip("Windows file symlink creation requires Developer Mode or privilege.")
        raise
    source = replace(source, file_version=read_file_version(source.path))
    destination = tmp_path / "new.flac"

    result = TransactionalFileWriter().apply_file(
        source, make_rename_only_change_set(source, destination), PayloadVerifier(),
        disabled_backup(), NeverCancelledToken(), RecordingEventSink(),
    )
    assert result.status is FileApplyStatus.FAILED
    assert target.read_bytes() == b"owned"
    assert source.path.is_symlink()


@pytest.mark.parametrize("deleting", [False, True])
def test_guard_checks_the_final_entry_for_reparse_attributes(tmp_path, monkeypatch, deleting):
    from metadata_polisher.infrastructure import windows_file_guard

    path = tmp_path / "entry.flac"
    path.write_bytes(b"owned")
    flags = []
    create_file = windows_file_guard._create_file

    def open_entry(name, access, share, security, creation, attributes, template):
        flags.append(attributes)
        return create_file(name, access, share, security, creation, attributes, template)

    monkeypatch.setattr(windows_file_guard, "_create_file", open_entry)
    # Exercise rejection without depending on the Windows symlink creation
    # privilege. The guard must read this attribute from the acquired handle.
    attributes = SimpleNamespace(st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
    monkeypatch.setattr(os, "fstat", lambda fd: attributes)
    with pytest.raises(OSError, match="reparse"), windows_file_guard.open_guard(path, for_deletion=deleting):
        pytest.fail("A reparse-point entry acquired a transaction guard.")
    assert flags == [0x00200000]  # FILE_FLAG_OPEN_REPARSE_POINT
    assert path.read_bytes() == b"owned"
