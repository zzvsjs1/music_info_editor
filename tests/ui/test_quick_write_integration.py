"""A disposable WAV proves the complete Quick scan/review/confirmation/write path."""

import hashlib
import wave

from mutagen.wave import WAVE

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
    finally:
        host.shutdown()
