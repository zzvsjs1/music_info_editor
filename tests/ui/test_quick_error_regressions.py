"""Errors preserve recovery evidence without displacing reachable QML controls."""

from dataclasses import replace
from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPointF
from PySide6.QtQuick import QQuickItem, QQuickWindow

from metadata_polisher.application.lookup import LookupService
from metadata_polisher.execution.events import OperationStageChanged
from metadata_polisher.providers.coordinator import ProviderCoordinator
from metadata_polisher.session.state import GroupSelection, GroupState, OperationKind, SessionState
from metadata_polisher.ui.quick.application import create_quick_engine
from metadata_polisher.ui.quick.apply import QuickApply
from metadata_polisher.ui.quick.backend import QuickBackend
from metadata_polisher.ui.quick.lookup import QuickLookup
from tests.ui.helpers import ControlledExecutor, changed_local_session
from tests.ui.test_quick_apply import ResultService
from tests.unit.application.test_lookup_service import (
    LookupFakeProvider,
    make_candidate,
    make_group,
    make_media_file,
    make_medium,
)


@pytest.fixture
def error_backend(qapp):
    backend = QuickBackend(state=changed_local_session(), executor=ControlledExecutor())

    try:
        yield backend
    finally:
        backend.shutdown()


def _start_scan(backend):
    assert backend.submit_operation(
        "ERROR-SCAN", OperationKind.SCAN, (), lambda *_: None, lambda state, _result: state,
    )


def _fail_pending(backend, qtbot, message):
    handle, _work, _events = backend.executor.pending.pop(0)

    with qtbot.waitSignal(backend.bridge.failed):
        handle.future.set_exception(OSError(message))


def test_failed_scan_has_short_stage_and_retains_complete_diagnostics(error_backend, qtbot):
    backend = error_backend
    original_groups = backend.session_state.groups
    message = "Synthetic storage failure: " + "long-path-component-" * 250
    _start_scan(backend)
    _fail_pending(backend, qtbot, message)

    assert not backend.busy
    assert backend.progressVisible
    assert backend.session_state.groups == original_groups
    assert message in backend.status
    assert message in backend.progressLog
    assert backend.progressStage == "Failed"


@pytest.mark.parametrize("terminal", (False, True))
def test_progress_retains_at_most_two_hundred_actual_lines(error_backend, qtbot, terminal):
    backend = error_backend
    _start_scan(backend)
    lines = [f"Synthetic diagnostic line {index:03}" for index in range(500)]
    message = "\n".join(lines)

    if terminal:
        _fail_pending(backend, qtbot, message)
    else:
        backend._on_event(OperationStageChanged("ERROR-SCAN", message))

    # A multiline exception is one event but many text lines. Limit the actual
    # retained document while preserving the newest evidence of what happened.
    assert len(backend.progressLog.splitlines()) <= 200
    assert lines[-1] in backend.progressLog
    assert lines[0] not in backend.progressLog
    assert len(backend.progressStage) <= 120


def test_bulk_lookup_failure_preserves_cause_in_primary_status(qapp, qtbot):
    first = GroupState(group=make_group(make_media_file("01.flac", title="Opening")))
    second = GroupState(group=replace(make_group(make_media_file("02.flac", title="Opening")), group_id="second"))
    state = SessionState(root=Path("library"), groups=(first, second), selection=GroupSelection(first.group.group_id))
    backend = QuickBackend(state=state, executor=ControlledExecutor())
    candidate = make_candidate("catalogue", "one", title="Album Evidence", media=(make_medium("Opening"),))
    provider = LookupFakeProvider("catalogue", (candidate,))
    lookup = QuickLookup(backend, service=LookupService(ProviderCoordinator((provider,))))

    try:
        backend.selectGroupExtended(first.group.group_id, False, False)
        backend.selectGroupExtended(second.group.group_id, True, False)
        lookup.findSelected()
        _fail_pending(backend, qtbot, "Synthetic timeout XYZ")

        assert not backend.busy
        assert not backend.executor.pending
        assert "Synthetic timeout XYZ" in backend.status
        assert "failed" in backend.status.lower()
        assert "0 with provider issues" not in backend.status
        assert all(group.candidate_lookup is None for group in backend.session_state.groups)
    finally:
        lookup.close()
        backend.shutdown()


def test_apply_reconciliation_failure_closes_progress_and_retains_receipts(error_backend, qtbot):
    backend = error_backend
    apply = QuickApply(backend, apply_service=ResultService())
    included = frozenset(source.file_id for group in backend.session_state.groups for source in group.group.files)
    backend.set_included_file_ids(included)
    assert apply.beginApply() and apply.confirmApply()
    operation_id = backend.session_state.active_operation.operation_id

    def reject_reconciliation(_state, _result):
        raise RuntimeError("Synthetic reconciliation failure")

    # The worker still returns validated receipts; only their in-memory
    # reconciliation fails. Showing unknown outcomes would lose write truth.
    backend._reducers[operation_id] = reject_reconciliation

    with qtbot.waitSignal(backend.bridge.completed):
        backend.executor.run_next()

    assert not backend.busy
    assert apply.resultsVisible
    assert all(row["status"] == "applied" for row in apply.resultsRows)
    assert "reconciliation failed" in apply.resultsText
    assert backend.session_state.groups[0].requires_rescan
    assert backend._included == included
    assert not backend.progressVisible


def test_long_failure_keeps_progress_actions_inside_the_window(error_backend, qtbot):
    backend = error_backend
    warnings = []
    engine = create_quick_engine(backend, warnings=warnings)
    main = engine.rootObjects()[0]
    progress = main.findChild(QQuickWindow, "operationProgressWindow")
    assert progress is not None

    try:
        _start_scan(backend)
        _fail_pending(backend, qtbot, "Synthetic failure: " + "long-path-component-" * 250)
        qtbot.waitUntil(progress.isVisible)
        qtbot.wait(80)
        actions = [item for item in progress.findChildren(QQuickItem)
                   if item.property("text") in {"Close", "Copy details"} and item.isVisible()]
        assert actions

        for action in actions:
            bottom = action.mapToScene(QPointF(0, action.height())).y()
            assert 0 <= bottom <= progress.height()

        details = progress.findChild(QQuickItem, "progressLog")
        assert details is not None and details.property("readOnly") and details.property("selectByMouse")
        assert details.isVisible()
    finally:
        for child in main.findChildren(QQuickWindow):
            child.hide()

        main.hide()
        engine.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        assert not warnings, warnings


def test_progress_log_follows_latest_events_until_the_reader_scrolls_back(error_backend, qtbot):
    backend = error_backend
    warnings = []
    engine = create_quick_engine(backend, warnings=warnings)
    main = engine.rootObjects()[0]
    progress = main.findChild(QQuickWindow, "operationProgressWindow")
    assert progress is not None

    try:
        _start_scan(backend)
        toggle = next(item for item in progress.findChildren(QQuickItem)
                      if item.property("text") == "Show details" and item.property("checkable"))
        toggle.setProperty("checked", True)
        qtbot.wait(80)
        details = progress.findChild(QQuickItem, "progressLog")
        assert details is not None
        flickable = details.parentItem()

        while flickable is not None and flickable.property("contentY") is None:
            flickable = flickable.parentItem()

        assert flickable is not None

        for index in range(80):
            backend._on_event(OperationStageChanged("ERROR-SCAN", f"Synthetic stage {index:03}"))

        qtbot.wait(80)
        bottom = float(flickable.property("contentHeight")) - flickable.height()
        assert bottom > 0
        assert float(flickable.property("contentY")) >= bottom - 2

        # Reading an earlier message is a deliberate choice. New worker events
        # must not repeatedly pull the viewport away from the reader's place.
        flickable.setProperty("contentY", 0.0)
        backend._on_event(OperationStageChanged("ERROR-SCAN", "Synthetic final stage"))
        qtbot.wait(80)
        assert abs(float(flickable.property("contentY"))) <= 2
    finally:
        for child in main.findChildren(QQuickWindow):
            child.hide()

        main.hide()
        engine.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        assert not warnings, warnings
