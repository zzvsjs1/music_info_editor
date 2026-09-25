"""Whole-library commands retain identities and operation lineage in Qt Quick."""

from dataclasses import replace
from pathlib import Path

import pytest

from metadata_polisher.session.state import GroupSelection, OperationKind, SessionState
from metadata_polisher.ui.quick.backend import QuickBackend
from tests.ui.helpers import ControlledExecutor, make_group


@pytest.fixture
def backend(qapp):
    state = SessionState(
        root=Path("library"),
        groups=(make_group("a", "one", "One"), make_group("b", "two", "Two")),
        selection=GroupSelection("a"),
    )
    instance = QuickBackend(state=state, executor=ControlledExecutor())
    yield instance
    instance.shutdown()


def test_review_window_scope_and_inclusion_remain_independent(backend):
    assert not backend.reviewVisible
    backend.selectFile("one", False)
    backend.openReview()
    backend.setReviewScope("library")
    assert backend.scopeFileIds == ["one", "two"]
    backend.includeScope()
    assert backend.selectedFileIds == ["one"]
    assert backend.includedFileIds == ["one", "two"]
    backend.closeReview()
    backend.selectGroup("b")
    assert not backend.reviewVisible
    assert backend.includedFileIds == ["one", "two"]


def test_cross_album_field_confirmation_captures_scope_and_rejects_stale_state(backend):
    backend.setReviewScope("library")
    backend.selectField("album")
    backend.reviewAction("clear")
    assert backend.confirmationVisible
    assert backend.session_state.revision == 0
    backend.set_state(replace(backend.session_state, revision=1))
    assert not backend.confirmAction()
    assert backend.session_state.revision == 1


def test_multi_group_highlighting_and_pending_lookup_choices(backend):
    backend.selectGroupExtended("b", True, False)
    assert backend.selected_group_ids == ("a", "b")
    assert backend.groupId == "b"
    group = replace(backend.session_state.groups[0], language_override="ja")
    backend.set_state(replace(backend.session_state, groups=(group, backend.session_state.groups[1])))
    assert backend.hasPendingWork


def test_generic_operation_reduces_before_terminal_observers_and_ignores_duplicates(backend, qtbot):
    calls = []
    result = object()

    def reduce(state, value):
        assert state.active_operation.operation_id == "test"
        assert value is result
        calls.append(value)
        return replace(state, revision=state.revision + 1)

    observed = []
    backend.bridge.completed.connect(lambda *_: observed.append(backend.session_state))
    assert backend.submit_operation("test", OperationKind.PROVIDER_TEST, (), lambda *_: result, reduce)
    with qtbot.waitSignal(backend.bridge.completed):
        backend.executor.run_next()

    assert observed[0].active_operation is None
    assert observed[0].revision == 1
    backend.bridge.completed.emit("test", result)
    assert len(calls) == 1


def test_cancellation_notifies_queue_immediately_and_only_once(backend):
    cancelled = []
    backend.cancellation_requested.connect(cancelled.append)
    assert backend.submit_operation("test", OperationKind.PROVIDER_TEST, (), lambda *_: None, lambda s, _: s)
    backend.cancelScan()
    backend.cancelScan()
    assert cancelled == ["test"]
    assert backend.cancelling


def test_field_multiselection_uses_per_field_values_and_undo(backend):
    backend.selectFile("one", False)
    backend.selectField("title")
    backend.selectFieldExtended("album", True, False)
    assert backend.selectedFields == ["title", "album"]
    backend.reviewAction("keep_existing")
    assert backend.canUndo
    reviews = backend.session_state.groups[0].reviewed_files[0].reviews
    assert sum(r.decision_origin.value == "user" for r in reviews) == 2
    backend.undo()
    assert not backend.canUndo


def test_shift_range_uses_last_clicked_anchor_and_replaces_unrelated_highlights(backend):
    group = backend.session_state.groups[0]
    files = tuple(make_group('a', key, key).group.files[0] for key in ('one', 'two', 'three', 'four'))
    backend.set_state(replace(backend.session_state, groups=(replace(group, group=replace(group.group, files=files)),)))
    backend.selectFileExtended('one', False, False)
    backend.selectFileExtended('four', True, False)
    backend.selectFileExtended('three', False, True)
    assert backend.selectedFileIds == ['three', 'four']
    assert backend.currentFileRow == 2
    backend.selectFileExtended('two', False, True)
    assert backend.selectedFileIds == ['two', 'three', 'four']
