"""The QML interface uses real session commands without writing music files."""

import hashlib
import wave
from dataclasses import replace
from pathlib import Path

import pytest
from PySide6.QtCore import Qt

from metadata_polisher.domain.metadata import FieldReadState, MetadataField
from metadata_polisher.execution.cancellation import OperationCancelledError
from metadata_polisher.session.state import GroupSelection, SessionState
from tests.ui.helpers import ControlledExecutor, make_group


@pytest.fixture
def backend(qapp):
    from metadata_polisher.ui.quick.backend import QuickBackend

    first = make_group("album", "first", "First title")
    second = make_group("album", "second", "Second title").group.files[0]
    group = replace(first, group=replace(first.group, files=(*first.group.files, second)))
    state = SessionState(root=Path("library"), groups=(group,), selection=GroupSelection("album"))
    instance = QuickBackend(state=state, executor=ControlledExecutor())
    yield instance
    instance.shutdown()


def test_selection_inclusion_and_qml_roles_are_independent(backend):
    backend.selectFile("first", False)
    assert backend.selectedFileIds == ["first"]
    assert backend.includedFileIds == []

    backend.setIncluded("second", True)
    backend.selectFile("second", True)
    assert backend.selectedFileIds == ["first", "second"]
    assert backend.includedFileIds == ["second"]

    roles = {bytes(name): role for role, name in backend.files.roleNames().items()}
    assert backend.files.index(1, 2).data(roles[b"stableId"]) == "second"
    assert backend.files.index(1, 0).data(roles[b"included"]) is True
    assert backend.files.index(0, 0).data(roles[b"highlighted"]) is True
    assert backend.review.index(0, 0).data(roles[b"stableId"]) == "title"


def test_manual_edit_uses_session_validation_and_undo_preserves_selection(backend):
    backend.selectFile("first", False)
    backend.selectField("title")
    backend.setIncluded("second", True)
    assert backend.beginEdit()
    assert backend.editValue == "First title"
    assert not backend.commitEdit("")
    assert backend.editing
    assert backend.editError
    assert backend.session_state.revision == 0

    assert backend.commitEdit("Changed title")
    assert backend.review.index(0, 2).data() == "First title"
    assert backend.review.index(0, 4).data() == "Changed title"
    assert backend.selectedFileIds == ["first"]
    assert backend.includedFileIds == ["second"]
    assert backend.canUndo

    backend.undo()
    assert backend.review.index(0, 4).data() == "First title"
    assert backend.includedFileIds == ["second"]


def test_edit_cannot_follow_a_changed_selection(backend):
    backend.selectFile("first", False)
    backend.selectField("title")
    assert backend.beginEdit()
    backend.selectFile("second", False)
    assert not backend.commitEdit("Must not reach either file")
    assert backend.session_state.revision == 0


def test_batch_title_edit_uses_each_original_value_and_undo(backend):
    backend.selectAllFiles()
    backend.selectField("title")
    assert backend.beginEdit()
    assert backend.editValue == ""
    assert backend.commitEdit("Common title")
    reviews = backend.session_state.groups[0].reviewed_files
    assert [item.reviews[0].manual_value for item in reviews] == ["Common title", "Common title"]
    backend.undo()
    assert backend.review.index(0, 4).data() == "Mixed values"


@pytest.mark.parametrize(("field", "text", "expected"), [
    ("artists", "One; punctuation\nTwo", "One; punctuation; Two"),
    ("track", "2/12", "2/12"),
])
def test_typed_values_reach_existing_review_services(backend, field, text, expected):
    backend.selectFile("first", False)
    backend.selectField(field)
    assert backend.beginEdit()
    assert backend.commitEdit(text)
    row = list(MetadataField).index(MetadataField(field))
    assert backend.review.index(row, 4).data() == expected


def test_unreadable_field_cannot_be_edited(qapp):
    from metadata_polisher.ui.quick.backend import QuickBackend

    group = make_group("album", "file", "Title")
    source = group.group.files[0]
    states = dict(source.read_result.field_states)
    states[MetadataField.TITLE] = FieldReadState.UNREADABLE
    source = replace(source, read_result=replace(source.read_result, field_states=states))
    group = replace(group, group=replace(group.group, files=(source,)))
    instance = QuickBackend(state=SessionState(root=Path("library"), groups=(group,)), executor=ControlledExecutor())
    try:
        instance.selectFile("file", False)
        instance.selectField("title")
        assert not instance.canEdit
        assert not instance.beginEdit()
    finally:
        instance.shutdown()


def test_scan_is_read_only_and_clears_old_choices_only_on_success(backend, qtbot, tmp_path):
    source = tmp_path / "01. Theme.wav"
    with wave.open(str(source), "wb") as audio:
        audio.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
        audio.writeframes(b"\x00\x00" * 80)
    before = (hashlib.sha256(source.read_bytes()).hexdigest(), source.stat().st_mtime_ns)

    backend.selectFile("first", False)
    backend.setIncluded("first", True)
    assert not backend.scan(str(tmp_path), False)
    assert not backend.busy
    assert backend.scan(str(tmp_path), True)
    assert backend.busy
    assert not backend.beginEdit()
    backend.setIncluded("second", True)
    assert backend.includedFileIds == ["first"]
    backend.executor.run_next()
    qtbot.waitUntil(lambda: not backend.busy)

    assert backend.session_state.root == tmp_path
    assert backend.files.rowCount() == 1
    assert backend.files.index(0, 2).data() == source.name
    assert backend.includedFileIds == []
    assert backend.selectedFileIds == []
    assert (hashlib.sha256(source.read_bytes()).hexdigest(), source.stat().st_mtime_ns) == before


def test_cancelled_scan_keeps_the_previous_session(backend, qtbot, tmp_path):
    original_groups = backend.session_state.groups
    backend.selectFile("first", False)
    backend.setIncluded("second", True)
    assert backend.scan(str(tmp_path), True)
    backend.cancelScan()
    backend.executor.run_next()
    qtbot.waitUntil(lambda: not backend.busy)
    assert backend.session_state.groups == original_groups
    assert backend.includedFileIds == ["second"]
    assert backend.selectedFileIds == ["first"]
    assert "cancel" in backend.status.lower()


def test_failed_and_late_completions_do_not_replace_the_session(backend, qtbot, tmp_path):
    assert backend.scan(str(tmp_path), False)
    handle, _, _ = backend.executor.pending.pop()
    handle.future.set_exception(OSError("Read failed"))
    qtbot.waitUntil(lambda: not backend.busy)
    assert backend.files.rowCount() == 2
    assert "Read failed" in backend.status

    assert backend.scan(str(tmp_path), False)
    active = backend.session_state.active_operation
    backend.bridge.failed.emit(handle.operation_id, OperationCancelledError())
    assert backend.session_state.active_operation == active
    backend.executor.run_next()
    qtbot.waitUntil(lambda: not backend.busy)


def test_shutdown_cancels_pending_scan_and_rejects_new_work(backend, tmp_path):
    assert backend.scan(str(tmp_path), False)
    handle = backend.executor.pending[0][0]
    backend.shutdown()
    assert handle.is_cancel_requested()
    assert not backend.scan(str(tmp_path), False)


def test_invalid_ids_and_fields_do_not_change_the_session(backend):
    backend.selectFile("missing", False)
    backend.setIncluded("missing", True)
    backend.selectGroup("missing")
    backend.selectField("missing")
    assert backend.selectedFileIds == []
    assert backend.includedFileIds == []
    assert backend.groupId == "album"
    assert backend.selectedField == "title"
    assert not backend.files.setData(backend.files.index(0, 0), "bad", Qt.ItemDataRole.EditRole)


def test_scan_reducer_rejects_a_result_with_obsolete_session_lineage(backend, qtbot, tmp_path):
    groups = backend.session_state.groups
    assert backend.scan(str(tmp_path), False)
    # Simulate a future command that changes evidence while a scan is running.
    # The interface must keep the canonical reducer's revision check intact.
    backend.session_state = replace(backend.session_state, revision=1)
    backend.executor.run_next()
    qtbot.waitUntil(lambda: not backend.busy)
    assert backend.session_state.groups == groups
    assert "outdated" in backend.status


def test_editor_rejects_an_obsolete_revision_even_with_same_selected_ids(backend):
    backend.selectFile("first", False)
    backend.selectField("title")
    assert backend.beginEdit()
    backend.session_state = replace(backend.session_state, revision=1)
    assert not backend.commitEdit("Obsolete edit")
    assert "revision" in backend.editError
    assert backend.session_state.groups[0].reviewed_files == ()


def test_keep_and_clear_are_explicit_undoable_decisions(backend):
    backend.selectFile("first", False)
    backend.selectField("title")
    backend.reviewAction("keep_existing")
    assert backend.hasPendingWork
    assert backend.review.index(0, 4).data() == "First title"
    backend.reviewAction("clear")
    assert backend.review.index(0, 4).data() == "Empty"
    backend.undo()
    assert backend.review.index(0, 4).data() == "First title"


def test_changing_album_clears_highlighting_but_retains_inclusion(backend):
    other = make_group("other", "third", "Other title")
    backend.session_state = replace(backend.session_state, groups=(*backend.session_state.groups, other))
    backend.selectFile("first", False)
    backend.setIncluded("first", True)
    backend.selectGroup("other")
    assert backend.selectedFileIds == []
    assert backend.includedFileIds == ["first"]
    assert backend.files.index(0, 2).data() == "third.flac"
    backend.moveFile(1)
    assert backend.selectedFileIds == ["third"]
    assert backend.currentFileRow == 0


def test_worker_runs_off_ui_thread_and_publishes_on_ui_thread(qapp, qtbot, tmp_path):
    from threading import get_ident

    from metadata_polisher.application.scanning import ScanLibraryService
    from metadata_polisher.formats.registry import FormatRegistry
    from metadata_polisher.scanner.scanner import ScanResult
    from metadata_polisher.ui.quick.backend import QuickBackend

    worker_threads = []
    ui_threads = []

    def scanner(root, registry, *, cancellation, on_progress):
        worker_threads.append(get_ident())
        on_progress(1, 1)
        return ScanResult((), (), ())

    instance = QuickBackend(scanner=ScanLibraryService(FormatRegistry(), scanner=scanner))
    instance.changed.connect(lambda: ui_threads.append(get_ident()))
    try:
        assert instance.scan(str(tmp_path), False)
        qtbot.waitUntil(lambda: not instance.busy)
        assert worker_threads and worker_threads[0] != get_ident()
        assert set(ui_threads) == {get_ident()}
    finally:
        instance.shutdown()
