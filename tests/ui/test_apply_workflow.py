from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication, QTableView

from metadata_polisher.application.apply import ApplyFileOutcomeStatus, ApplyService
from metadata_polisher.bootstrap import create_application
from metadata_polisher.domain.metadata import MetadataField
from metadata_polisher.domain.review import FieldDecisionKind
from metadata_polisher.execution.events import FileApplyStatus, FileStageChanged, FileTransactionStage
from metadata_polisher.infrastructure.settings import RenameSettings
from metadata_polisher.infrastructure.transaction import FileApplyResult
from metadata_polisher.session.apply_preparation import reviewed_file_ids
from metadata_polisher.session.review_editing import apply_field_decision
from tests.ui.test_scan_workflow import ControlledExecutor
from tests.unit.application.test_apply_service import StubAdapter, make_preflight_snapshot
from tests.unit.session.test_review_editing import make_local_session, make_source


def changed_local_session():
    state = make_local_session(make_source("first.flac"), make_source("second.flac"))

    for index, source in enumerate(state.groups[0].group.files):
        state = apply_field_decision(state, "album", source.file_id, MetadataField.TITLE,
                                     FieldDecisionKind.USE_MANUAL, RenameSettings(), manual_value=f"Corrected {index}")

    return state


# Rejection is checked at the write boundary: prepared review data may exist,
# but neither an executor submission nor a filesystem change may follow Cancel.
def test_apply_confirmation_cancel_does_not_submit_or_write(qtbot, tmp_path):
    executor = ControlledExecutor()
    _, window = create_application([], executor=executor, settings_file=tmp_path / "settings.json")
    qtbot.addWidget(window)
    state = changed_local_session()
    window.set_session_state(state)
    window.set_included_file_ids(frozenset(reviewed_file_ids(state)))
    window.file_table_view.selectRow(0)
    shown = []
    timer = QTimer()
    timer.setSingleShot(True)

    def reject_summary():
        shown.append(QApplication.activeModalWidget())
        shown[-1].reject()

    timer.timeout.connect(reject_summary)
    timer.start(0)

    try:
        qtbot.mouseClick(window.apply_selected_button, Qt.MouseButton.LeftButton)
    finally:
        timer.stop()

    assert shown
    assert not executor.pending
    assert window.session_state is state


def test_apply_cancel_keeps_completed_result_and_never_starts_later_file(qtbot, tmp_path):
    started = []
    stages = []

    class Preflight:
        def inspect(self, source, proposed_changes, backup):
            return make_preflight_snapshot(source, StubAdapter())

    class Writer:
        def apply_file(self, source, changes, adapter, backup, cancellation, events):
            started.append(source.file_id)

            for stage in (FileTransactionStage.COPYING_TEMPORARY, FileTransactionStage.WRITING_METADATA,
                          FileTransactionStage.VERIFYING_TEMPORARY, FileTransactionStage.COMMITTING):
                events.emit(FileStageChanged(backup.operation_id, source.file_id, source.path, stage))
                QApplication.processEvents()
                stages.append(window.operation_stage_label.text())

            window.cancel_button.click()
            assert cancellation.is_cancelled()
            return FileApplyResult(source.path, source.path, FileApplyStatus.SUCCEEDED, FileTransactionStage.COMPLETED)

    executor = ControlledExecutor()
    service = ApplyService(preflight=Preflight(), writer=Writer())
    _, window = create_application(
        [], executor=executor, settings_file=tmp_path / "settings.json", apply_service=service,
    )
    qtbot.addWidget(window)
    window.set_session_state(changed_local_session())
    window.set_included_file_ids(frozenset(reviewed_file_ids(window.session_state)))
    QTimer.singleShot(0, lambda: QApplication.activeModalWidget().accept())
    qtbot.mouseClick(window.apply_all_button, Qt.MouseButton.LeftButton)
    assert len(executor.pending) == 1
    assert not window.settings_button.isEnabled()
    assert window.cancel_button.isEnabled()
    executor.run_next()
    qtbot.waitUntil(lambda: window.session_state.active_operation is None)
    assert len(started) == 1
    outcomes = window.apply_controller.last_result.groups[0].files
    assert outcomes[0].status is ApplyFileOutcomeStatus.APPLIED
    assert outcomes[1].status is ApplyFileOutcomeStatus.SKIPPED
    assert "Cancelled" in window.operation_stage_label.text()
    assert "1 completed" in window.workflow_message_label.text()
    assert any("Copying temporary" in stage for stage in stages)
    assert any("Committing" in stage for stage in stages)
    assert window.settings_button.isEnabled()
    assert not window.cancel_button.isEnabled()
    assert window.session_state.groups[0].requires_rescan
    assert window.included_file_ids == frozenset(
        source.file_id for source in window.session_state.groups[0].group.files
    )
    qtbot.mouseClick(window.apply_results_button, Qt.MouseButton.LeftButton)
    table = window.findChild(QTableView, "applyResultsTable")
    assert table.model().index(0, 1).data() == "applied"
    assert table.model().index(1, 1).data() == "skipped"


def test_default_writer_renames_generated_audio_with_backup_and_report(qtbot, tmp_path):
    import json
    import wave

    from mutagen.wave import WAVE

    from metadata_polisher.application.changes import RenameDecision
    from metadata_polisher.infrastructure.settings import AppSettings, BackupSettings, ReportsSettings, save_settings
    from metadata_polisher.session.review_editing import set_rename_decision

    root = tmp_path / "library"
    root.mkdir()
    source = root / "original.wav"

    with wave.open(str(source), "wb") as audio:
        audio.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
        audio.writeframes(b"\0\0" * 80)

    original_bytes = source.read_bytes()
    settings_file = tmp_path / "settings.json"
    settings = AppSettings(rename=RenameSettings(template="%title%"),
                           backup=BackupSettings(True, str(tmp_path / "backups")), reports=ReportsSettings(True))
    save_settings(settings_file, settings)
    executor = ControlledExecutor()
    _, window = create_application([], executor=executor, settings_file=settings_file)
    qtbot.addWidget(window)
    window.root_path_edit.setText(str(root))
    qtbot.mouseClick(window.rescan_button, Qt.MouseButton.LeftButton)
    executor.run_next()
    qtbot.waitUntil(lambda: window.session_state.active_operation is None)
    group = window.session_state.groups[0]
    file_id = group.group.files[0].file_id
    state = apply_field_decision(window.session_state, group.group.group_id, file_id, MetadataField.TITLE,
                                 FieldDecisionKind.USE_MANUAL, settings.rename, manual_value="Reviewed title")
    state = set_rename_decision(state, group.group.group_id, (file_id,), RenameDecision.APPLY_RENAME, settings.rename)
    window.set_session_state(state)
    window.set_included_file_ids(frozenset(reviewed_file_ids(state)))
    QTimer.singleShot(0, lambda: QApplication.activeModalWidget().accept())
    qtbot.mouseClick(window.apply_all_button, Qt.MouseButton.LeftButton)
    executor.run_next()
    qtbot.waitUntil(lambda: window.session_state.active_operation is None)
    result = window.apply_controller.last_result
    assert result.groups[0].files[0].status is ApplyFileOutcomeStatus.APPLIED
    # Completion opens one persistent results view without a second click.
    tables = window.findChildren(QTableView, "applyResultsTable")
    assert len(tables) == 1
    assert tables[0].isVisible()
    output = root / "Reviewed title.wav"
    assert WAVE(output).tags["TIT2"].text == ["Reviewed title"]
    assert not source.exists()
    assert (tmp_path / "backups" / result.operation_id / "original.wav").read_bytes() == original_bytes
    assert result.report_result.path.parent == tmp_path / "reports"
    report = json.loads(result.report_result.path.read_text(encoding="utf-8"))
    assert report["operation"]["id"] == result.operation_id
    assert "Reviewed title" in result.report_result.path.read_text(encoding="utf-8")


def test_cancellation_before_worker_start_is_reported(qtbot, tmp_path):
    from metadata_polisher.execution.cancellation import OperationCancelledError

    executor = ControlledExecutor()
    _, window = create_application([], executor=executor, settings_file=tmp_path / "settings.json")
    qtbot.addWidget(window)
    window.set_session_state(changed_local_session())
    window.set_included_file_ids(frozenset(reviewed_file_ids(window.session_state)))
    QTimer.singleShot(0, lambda: QApplication.activeModalWidget().accept())
    qtbot.mouseClick(window.apply_all_button, Qt.MouseButton.LeftButton)
    handle, _, _ = executor.pending.pop()
    window.cancel_button.click()
    handle.future.set_exception(OperationCancelledError())
    qtbot.waitUntil(lambda: window.session_state.active_operation is None)
    assert window.operation_stage_label.text() == "Cancelled"
    assert "before any files were processed" in window.workflow_message_label.text()
    assert window.apply_controller.last_result is None
    assert window.apply_results_button.isEnabled()
    table = window.findChild(QTableView, "applyResultsTable")
    assert table is not None and table.isVisible()
    assert all(table.model().index(row, 1).data() == "not_started" for row in range(table.model().rowCount()))
    assert window.apply_all_button.isEnabled()
    assert not window.cancel_button.isEnabled()


# A presentation/reconciliation failure can happen after the writer succeeds.
# Actual disk outcomes must survive even when the session cannot accept them.
def test_apply_reducer_failure_retains_actual_write_results_and_marks_source_stale(qtbot, tmp_path, monkeypatch):
    import wave
    from dataclasses import replace

    from mutagen.wave import WAVE

    from metadata_polisher.application.apply import ApplyBatchResult

    root = tmp_path / "library"
    root.mkdir()
    source = root / "original.wav"

    with wave.open(str(source), "wb") as audio:
        audio.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
        audio.writeframes(b"\0\0" * 80)

    executor = ControlledExecutor()
    _, window = create_application([], executor=executor, settings_file=tmp_path / "settings.json")
    qtbot.addWidget(window)
    window.root_path_edit.setText(str(root))
    qtbot.mouseClick(window.rescan_button, Qt.MouseButton.LeftButton)
    executor.run_next()
    qtbot.waitUntil(lambda: window.session_state.active_operation is None)
    group = window.session_state.groups[0]
    source_id = group.group.files[0].file_id
    state = apply_field_decision(
        window.session_state, group.group.group_id, source_id, MetadataField.TITLE,
        FieldDecisionKind.USE_MANUAL, RenameSettings(), manual_value="Written despite a reducer failure",
    )
    window.set_session_state(state)
    window.set_included_file_ids(frozenset(reviewed_file_ids(state)))

    def broken_reducer(_state, _result):
        raise ValueError("Synthetic session reconciliation failure")

    controller = window.operation_controller
    monkeypatch.setattr(controller, "_bindings", tuple(
        replace(binding, reducer=broken_reducer) if binding.result_type is ApplyBatchResult else binding
        for binding in controller._bindings
    ))
    QTimer.singleShot(0, lambda: QApplication.activeModalWidget().accept())
    qtbot.mouseClick(window.apply_all_button, Qt.MouseButton.LeftButton)
    executor.run_next()
    qtbot.waitUntil(lambda: window.session_state.active_operation is None)

    assert WAVE(source).tags["TIT2"].text == ["Written despite a reducer failure"]
    result = window.apply_controller.last_result
    assert result.groups[0].files[0].status is ApplyFileOutcomeStatus.APPLIED
    assert window.session_state.groups[0].requires_rescan
    assert window.included_file_ids == frozenset((source_id,))
    assert window.session_state.written_files == ()
    assert "reconcil" in window.workflow_message_label.text().casefold()
    assert "1 completed" in window.workflow_message_label.text()
    tables = window.findChildren(QTableView, "applyResultsTable")
    assert len(tables) == 1 and tables[0].isVisible()
    assert tables[0].model().index(0, 1).data() == "applied"
    assert window.settings_button.isEnabled()
    assert not window.cancel_button.isEnabled()
