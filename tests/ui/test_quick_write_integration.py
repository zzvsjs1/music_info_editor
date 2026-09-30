"""A disposable WAV proves the complete Quick scan/review/confirmation/write path."""

import hashlib
import wave

import pytest
from mutagen.id3 import TIT2, TPE1
from mutagen.wave import WAVE

from metadata_polisher.infrastructure.filesystem import LocalFileSystem, read_file_version
from metadata_polisher.ui.quick.apply import QuickApply
from metadata_polisher.ui.quick.backend import QuickBackend
from tests.ui.helpers import ControlledExecutor


def test_real_write_occurs_only_after_explicit_frozen_confirmation(qapp, qtbot, tmp_path):
    path = tmp_path / "01. Original.wav"
    with wave.open(str(path), "wb") as stream:
        stream.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
        stream.writeframes(b"\0\0" * 800)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    host = QuickBackend(executor=ControlledExecutor(), app_dir=tmp_path)
    facade = QuickApply(host)
    try:
        assert host.scan(str(tmp_path), False)
        with qtbot.waitSignal(host.bridge.completed):
            host.executor.run_next()
        file_id = host.session_state.groups[0].group.files[0].file_id
        host.selectFile(file_id, False)
        host.selectField("title")
        assert host.beginEdit()
        assert host.commitEdit("Reviewed title")
        host.setIncluded(file_id, True)
        assert facade.beginApply()
        assert hashlib.sha256(path.read_bytes()).hexdigest() == before
        assert not host.executor.pending
        facade.cancelApply()
        assert hashlib.sha256(path.read_bytes()).hexdigest() == before
        assert facade.beginApply()
        assert facade.confirmApply()
        with qtbot.waitSignal(host.bridge.completed):
            host.executor.run_next()
        assert str(WAVE(path).tags["TIT2"]) == "Reviewed title"
        assert host.includedFileIds == []
        assert facade.resultsVisible
        assert facade.last_result is not None
        assert host.session_state.written_files
        # The data chunk survives the real transactional metadata update.
        with wave.open(str(path), "rb") as stream:
            assert stream.readframes(800) == b"\0\0" * 800

        # A successful replacement must install its new file version, allowing
        # a second explicit review without mistaking our own first write for an
        # external edit to the original scan snapshot.
        refreshed = host.session_state.groups[0].group.files[0]
        assert refreshed.file_version == read_file_version(path)
        host.selectFile(file_id, False)
        host.selectField("title")
        assert host.beginEdit() and host.commitEdit("Second reviewed title")
        host.setIncluded(file_id, True)
        assert facade.beginApply() and facade.confirmApply()

        with qtbot.waitSignal(host.bridge.completed):
            host.executor.run_next()

        assert str(WAVE(path).tags["TIT2"]) == "Second reviewed title"
        assert not host.session_state.groups[0].requires_rescan
    finally:
        host.shutdown()


@pytest.mark.parametrize("external_edit", ("before_apply", "after_copy"))
def test_apply_rejects_external_changes_and_requires_a_rescan(qapp, qtbot, tmp_path, monkeypatch, external_edit):
    path = tmp_path / "01.wav"
    with wave.open(str(path), "wb") as stream:
        stream.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
        stream.writeframes(b"\0\0" * 800)

    audio = WAVE(path)
    audio.add_tags()
    audio.tags.add(TIT2(encoding=3, text=["Scanned title"]))
    audio.save()
    host = QuickBackend(executor=ControlledExecutor(), app_dir=tmp_path)

    def edit_original():
        audio = WAVE(path)
        audio.tags.add(TPE1(encoding=3, text=["External artist"]))
        audio.save()

    try:
        assert host.scan(str(tmp_path), False)
        with qtbot.waitSignal(host.bridge.completed):
            host.executor.run_next()

        file_id = host.session_state.groups[0].group.files[0].file_id
        host.selectFile(file_id, False)
        host.selectField("title")
        assert host.beginEdit() and host.commitEdit("Reviewed title")
        host.setIncluded(file_id, True)

        if external_edit == "before_apply":
            edit_original()
        else:
            original_copy = LocalFileSystem.copy_file

            def copy_then_edit(filesystem, source, destination, *, overwrite):
                original_copy(filesystem, source, destination, overwrite=overwrite)
                # The concurrent edit happens after the private copy exists,
                # so copying the newest bytes at transaction start is insufficient.
                edit_original()

            monkeypatch.setattr(LocalFileSystem, "copy_file", copy_then_edit)

        assert host.applyUi.beginApply() and host.applyUi.confirmApply()
        with qtbot.waitSignal(host.bridge.completed):
            host.executor.run_next()

        actual = WAVE(path).tags
        assert str(actual["TIT2"]) == "Scanned title"
        assert str(actual["TPE1"]) == "External artist"
        assert host.session_state.groups[0].requires_rescan
        assert file_id in host.includedFileIds
        assert not tuple(tmp_path.glob(".metadata-polisher-*.wav"))
    finally:
        host.shutdown()
