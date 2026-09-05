"""Native progress windows do not own or abandon background workers."""

from PySide6.QtCore import Qt


def test_lookup_hide_does_not_cancel_and_unknown_work_has_no_percentage(qtbot) -> None:
    from metadata_polisher.ui.operation_progress_dialog import OperationProgressDialog

    dialog = OperationProgressDialog("LOOKUP-1", writing=False)
    qtbot.addWidget(dialog)
    cancellations: list[bool] = []
    dialog.cancel_requested.connect(lambda: cancellations.append(True))
    dialog.show()
    dialog.update_status("Loading candidate tracks", "musicbrainz", "Album", 0, 0)

    assert not dialog.isModal()
    assert "No files are being changed" in dialog.safety_label.text()
    assert "musicbrainz" in dialog.context_label.text()
    assert "Album" in dialog.context_label.text()
    assert dialog.progress_bar.minimum() == dialog.progress_bar.maximum() == 0
    assert "Elapsed" in dialog.elapsed_label.text()
    qtbot.mouseClick(dialog.hide_button, Qt.MouseButton.LeftButton)

    assert not dialog.isVisible()
    assert cancellations == []
    dialog.finish("Completed")


# Escape and title-bar Close enter through different Qt methods but share one
# cooperative cancellation boundary; neither may dismiss an active write early.
def test_write_escape_and_close_request_one_cancellation_until_safe_terminal(qtbot) -> None:
    from metadata_polisher.ui.operation_progress_dialog import OperationProgressDialog

    dialog = OperationProgressDialog("APPLY-1", writing=True)
    qtbot.addWidget(dialog)
    cancellations: list[bool] = []
    dialog.cancel_requested.connect(lambda: cancellations.append(True))
    dialog.show()
    qtbot.keyClick(dialog, Qt.Key.Key_Escape)
    dialog.close()
    dialog.update_status("Committing", "", "01.flac", 0, 2)

    assert cancellations == [True]
    assert dialog.isVisible()
    assert "Cancelling" in dialog.stage_label.text()
    assert not dialog.cancel_button.isEnabled()
    dialog.finish("Cancelled: 1 completed, 1 not started")
    dialog.close()

    assert not dialog.isVisible()
    assert cancellations == [True]


def test_expanded_progress_details_redact_credentials_and_stop_at_terminal(qtbot) -> None:
    from metadata_polisher.ui.operation_progress_dialog import OperationProgressDialog

    dialog = OperationProgressDialog("LOOKUP-1", writing=False)
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.append_log("Retry waiting; password=SENTINEL_PROGRESS_SECRET")
    qtbot.mouseClick(dialog.details_button, Qt.MouseButton.LeftButton)

    assert dialog.detail_log.isVisible()
    assert "Retry waiting" in dialog.detail_log.toPlainText()
    assert "SENTINEL_PROGRESS_SECRET" not in dialog.detail_log.toPlainText()
    assert "REDACTED" in dialog.detail_log.toPlainText()
    dialog.finish("Failed")
    terminal = dialog.stage_label.text()
    dialog.update_status("Late searching", "vgmdb", "", 1, 2)

    assert dialog.stage_label.text() == terminal
    assert not dialog.cancel_button.isEnabled()


def test_parent_close_cancels_cooperatively_and_next_operation_discards_previous_dialog(qtbot) -> None:
    from PySide6.QtWidgets import QLabel, QProgressBar, QWidget

    from metadata_polisher.session.state import OperationKind, ResultApplicationStatus, StateApplicationResult
    from metadata_polisher.ui.operation_controller import OperationController, ResultReducerBinding
    from tests.ui.test_operation_controller import ManualBridge, StateStore, WorkerResult, work

    parent = QWidget()
    qtbot.addWidget(parent)
    parent.show()
    bridge = ManualBridge()
    store = StateStore()
    controller = OperationController(
        bridge=bridge,
        get_state=store.get,
        set_state=store.set,
        result_reducers=(ResultReducerBinding(
            WorkerResult, lambda state, _result: StateApplicationResult(state, ResultApplicationStatus.APPLIED, None),
        ),),
        conflicting_controls=(),
        stage_label=QLabel(parent),
        progress_bar=QProgressBar(parent),
        parent=parent,
    )
    controller.start("FIRST", OperationKind.SCAN, (), work)
    first = controller.progress_dialog
    assert first is not None
    parent.close()

    assert parent.isVisible()
    assert bridge.handles["FIRST"].cancel_calls == 1
    assert store.state.active_operation is not None
    bridge.cancelled.emit("FIRST")
    controller.start("SECOND", OperationKind.SCAN, (), work)

    assert controller.progress_dialog is not first
    assert not first.isVisible()
    bridge.cancelled.emit("SECOND")
    parent.close()
    assert not parent.isVisible()
