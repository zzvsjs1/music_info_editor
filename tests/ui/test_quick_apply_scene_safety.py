"""Discard scope and write cancellation remain explicit in the real QML scene."""

from dataclasses import replace

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtQuick import QQuickItem, QQuickWindow
from PySide6.QtTest import QTest

from metadata_polisher.ui.quick.application import create_quick_engine
from metadata_polisher.ui.quick.backend import QuickBackend
from tests.ui.helpers import ControlledExecutor, changed_local_session, make_group
from tests.ui.test_quick_apply import ResultService


def test_pending_work_description_includes_choices_outside_the_current_group(qapp):
    state = changed_local_session()
    untouched = make_group("other-album", "other-file", "Other album")
    backend = QuickBackend(state=replace(state, groups=(*state.groups, untouched)), executor=ControlledExecutor())

    try:
        backend.set_included_file_ids(frozenset({state.groups[0].group.files[0].file_id}))
        backend.selectGroup("other-album")

        # The currently visible album has no edits. Discard still affects all
        # groups, including deliberate decisions and inclusion elsewhere.
        assert backend.groupId == "other-album"
        assert backend.hasPendingWork
        description = getattr(backend, "pendingWorkDescription", "")
        assert "2 metadata decisions in 2 files" in description
        assert "1 file included for writing" in description
        assert "2 review actions in undo history" in description

        backend.set_included_file_ids(frozenset())
        assert "included for writing" not in backend.pendingWorkDescription
        assert "2 metadata decisions in 2 files" in backend.pendingWorkDescription
    finally:
        backend.shutdown()


@pytest.mark.parametrize("first_action", ("escape", "close"))
def test_apply_escape_and_title_close_request_one_cancellation_until_terminal(qapp, qtbot, first_action):
    backend = QuickBackend(state=changed_local_session(), executor=ControlledExecutor())
    included = frozenset(source.file_id for source in backend.session_state.groups[0].group.files)
    backend.set_included_file_ids(included)
    service = ResultService()
    service.stop_after_first = True
    backend.applyUi._service = service
    warnings = []
    engine = create_quick_engine(backend, warnings=warnings)
    main = engine.rootObjects()[0]
    progress = main.findChild(QQuickWindow, "operationProgressWindow")
    results = main.findChild(QQuickWindow, "applyResultsWindow")
    assert progress is not None and results is not None
    cancellations = []
    backend.cancellation_requested.connect(cancellations.append)

    try:
        assert backend.applyUi.beginApply() and backend.applyUi.confirmApply()
        handle, work, events = backend.executor.pending.pop(0)

        # Fabricate a truthful partial receipt using the disposable service,
        # then delay its delivery. This models a worker reaching a safe file
        # boundary while the UI still owns an active operation; no media is read
        # or written, and cancellation must not invent a rollback afterwards.
        receipt = work(handle.token, events)
        progress.requestActivate()
        assert QTest.qWaitForWindowActive(progress, 2000)
        qtbot.wait(40)

        for action in (first_action, "close" if first_action == "escape" else "escape"):
            if action == "escape":
                QTest.keyClick(progress, Qt.Key.Key_Escape)
            else:
                progress.close()

            qtbot.wait(20)
            assert backend.busy and backend.cancelling
            assert progress.isVisible() and main.isVisible()
            assert cancellations == [handle.operation_id]
            assert handle.is_cancel_requested()

        cancel = progress.findChild(QQuickItem, "progressCancelButton")
        assert cancel is not None and not cancel.isEnabled()

        with qtbot.waitSignal(backend.bridge.completed):
            handle.future.set_result(receipt)

        qtbot.waitUntil(results.isVisible)
        assert not backend.busy and not progress.isVisible()
        assert cancellations == [handle.operation_id]
        assert [row["status"] for row in backend.applyUi.resultsRows] == ["applied", "not_started"]
        assert "1 completed" in backend.applyUi.resultsText
        assert "1 not started" in backend.applyUi.resultsText
        assert len(backend.includedFileIds) == 1
    finally:
        for child in main.findChildren(QQuickWindow):
            child.hide()

        main.hide()
        engine.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        backend.shutdown()
        assert not warnings, warnings
