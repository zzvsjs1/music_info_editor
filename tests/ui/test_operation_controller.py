from dataclasses import dataclass, replace
from pathlib import Path

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QLabel, QProgressBar, QPushButton

from metadata_polisher.application.apply import (
    ApplyBatchResult,
    ApplyBatchStatus,
    ApplyFileOutcome,
    ApplyFileOutcomeStatus,
    ApplyGroupOutcome,
)
from metadata_polisher.application.changes import RenameDecision, build_change_set
from metadata_polisher.application.review import build_field_review_state, set_manual_decision
from metadata_polisher.domain.media import LocalMediaFile, MediaReadResult, StreamInfo
from metadata_polisher.domain.metadata import (
    FieldReadState,
    MetadataField,
    MetadataSnapshot,
    Position,
)
from metadata_polisher.domain.review import FieldReviewState
from metadata_polisher.execution.events import (
    FileApplyStatus,
    FileTransactionStage,
    OperationCancelled,
    OperationCompleted,
    OperationProgress,
    OperationStageChanged,
    ProviderStarted,
)
from metadata_polisher.infrastructure.reporting import ReportWriteResult, ReportWriteStatus
from metadata_polisher.infrastructure.transaction import FileApplyResult
from metadata_polisher.scanner.grouping import AlbumGroup, GroupingReason
from metadata_polisher.session.state import (
    GroupState,
    OperationKind,
    ResultApplicationStatus,
    SessionState,
    StaleResultReason,
    StateApplicationResult,
    apply_batch_result,
)
from metadata_polisher.ui.operation_controller import (
    OperationController,
    ResultReducerBinding,
)


@dataclass(frozen=True)
class WorkerResult:
    name: str


def make_apply_source() -> LocalMediaFile:
    states = {field: FieldReadState.MISSING for field in MetadataField}
    states[MetadataField.TITLE] = FieldReadState.PRESENT
    states[MetadataField.TRACK] = FieldReadState.PRESENT

    return LocalMediaFile(
        path=Path("library/Album/01.flac"),
        format_id="flac",
        read_result=MediaReadResult(
            metadata=MetadataSnapshot(title="Old title", track=Position(number=1)),
            field_states=states,
            stream_info=StreamInfo(180.0, 48_000, 2, 24, "FLAC"),
        ),
        file_id="file-1",
    )


def make_apply_reviews(source: LocalMediaFile) -> tuple[FieldReviewState, ...]:
    existing_values: dict[MetadataField, object | None] = {
        MetadataField.TITLE: source.read_result.metadata.title,
        MetadataField.ARTISTS: None,
        MetadataField.ALBUM: None,
        MetadataField.ALBUM_ARTISTS: None,
        MetadataField.COMPOSERS: None,
        MetadataField.TRACK: source.read_result.metadata.track,
        MetadataField.DISC: None,
        MetadataField.DATE: None,
        MetadataField.GENRES: None,
    }
    reviews = tuple(
        build_field_review_state(
            field=field,
            read_state=source.read_result.field_states[field],
            existing_value=existing_values[field],  # type: ignore[arg-type]
            proposals=(),
        )
        for field in MetadataField
    )

    return (set_manual_decision(reviews[0], "New title"), *reviews[1:])


class ManualHandle:
    def __init__(self, operation_id: str) -> None:
        self.operation_id = operation_id
        self.cancel_calls = 0

    def cancel(self) -> None:
        self.cancel_calls += 1


class ManualBridge(QObject):
    # Emit terminal/progress signals in deliberately chosen orders to exercise
    # late and duplicate callbacks independently of actual worker scheduling.
    operation_event = Signal(object)
    completed = Signal(str, object)
    cancelled = Signal(str)
    failed = Signal(str, object)

    def __init__(self, submit_error: BaseException | None = None) -> None:
        super().__init__()
        self.submit_error = submit_error
        self.submissions: list[tuple[str, object]] = []
        self.handles: dict[str, ManualHandle] = {}

    def submit(self, operation_id: str, work: object) -> ManualHandle:
        self.submissions.append((operation_id, work))

        if self.submit_error is not None:
            raise self.submit_error

        handle = ManualHandle(operation_id)
        self.handles[operation_id] = handle

        return handle


class StateStore:
    # Retain every installed immutable snapshot so tests can observe whether
    # result reduction happened before active-operation lineage was cleared.
    def __init__(self) -> None:
        self.state = SessionState(root=None)
        self.installed: list[SessionState] = []

    def get(self) -> SessionState:
        return self.state

    def set(self, state: SessionState) -> None:
        self.state = state
        self.installed.append(state)


def make_controller(
    bridge: ManualBridge,
    store: StateStore,
    reducer,
) -> tuple[OperationController, QPushButton, QPushButton, QLabel, QProgressBar]:
    first = QPushButton("First")
    second = QPushButton("Second")
    stage = QLabel("Idle")
    progress = QProgressBar()
    progress.setRange(0, 100)
    progress.setValue(0)
    controller = OperationController(
        bridge=bridge,  # type: ignore[arg-type]
        get_state=store.get,
        set_state=store.set,
        result_reducers=(ResultReducerBinding(WorkerResult, reducer),),
        conflicting_controls=(first, second),
        stage_label=stage,
        progress_bar=progress,
    )

    return controller, first, second, stage, progress


def work(_token, _events) -> WorkerResult:
    return WorkerResult("unused")


def test_completed_payload_is_reduced_before_finish_and_semantic_terminal_is_informational(
    qapp,
) -> None:
    del qapp
    bridge = ManualBridge()
    store = StateStore()
    reducer_active_ids: list[str | None] = []

    def reducer(state: SessionState, result: object) -> StateApplicationResult:
        assert result == WorkerResult("done")
        reducer_active_ids.append(
            state.active_operation.operation_id
            if state.active_operation is not None
            else None
        )

        return StateApplicationResult(
            state=replace(state, revision=state.revision + 1),
            status=ResultApplicationStatus.STALE,
            reason=StaleResultReason.OPERATION_MISMATCH,
        )

    controller, first, second, _stage, _progress = make_controller(bridge, store, reducer)
    controller.start("SCAN-1", OperationKind.SCAN, (), work)
    started = store.state

    assert started.active_operation is not None
    assert not first.isEnabled()
    assert not second.isEnabled()

    bridge.operation_event.emit(OperationCompleted("SCAN-1"))

    assert store.state is started
    assert reducer_active_ids == []
    assert not first.isEnabled()

    bridge.completed.emit("SCAN-1", WorkerResult("done"))

    assert reducer_active_ids == ["SCAN-1"]
    assert store.installed[-2].revision == 1
    assert store.installed[-2].active_operation is started.active_operation
    assert store.installed[-1].active_operation is None
    assert store.state.revision == 1
    assert first.isEnabled()
    assert second.isEnabled()


def test_late_terminal_callbacks_never_clear_or_enable_a_newer_operation(qapp) -> None:
    del qapp
    bridge = ManualBridge()
    store = StateStore()

    def reducer(state: SessionState, _result: object) -> StateApplicationResult:
        return StateApplicationResult(
            state=replace(state, revision=state.revision + 1),
            status=ResultApplicationStatus.STALE,
            reason=StaleResultReason.OPERATION_MISMATCH,
        )

    controller, first, _second, _stage, _progress = make_controller(bridge, store, reducer)
    controller.start("OLD", OperationKind.SCAN, (), work)
    bridge.cancelled.emit("OLD")
    controller.start("NEW", OperationKind.SCAN, (), work)
    revision_before_late_result = store.state.revision

    bridge.completed.emit("OLD", WorkerResult("old-late"))
    bridge.cancelled.emit("OLD")
    bridge.failed.emit("OLD", RuntimeError("late failure"))

    assert store.state.active_operation is not None
    assert store.state.active_operation.operation_id == "NEW"
    assert store.state.revision == revision_before_late_result + 1
    assert not first.isEnabled()

    bridge.cancelled.emit("NEW")

    assert store.state.active_operation is None
    assert first.isEnabled()


def test_progress_is_scoped_and_cancel_stays_busy_until_bridge_terminal(qapp) -> None:
    del qapp
    bridge = ManualBridge()
    store = StateStore()

    def reducer(state: SessionState, _result: object) -> StateApplicationResult:
        return StateApplicationResult(state, ResultApplicationStatus.APPLIED, None)

    controller, first, _second, stage, progress = make_controller(bridge, store, reducer)
    controller.start("LOOK", OperationKind.SCAN, (), work)
    initial_text = stage.text()
    initial_value = progress.value()

    bridge.operation_event.emit(OperationStageChanged("FOREIGN", "wrong_stage"))
    bridge.operation_event.emit(OperationProgress("FOREIGN", "wrong_stage", 3, 4))

    assert stage.text() == initial_text
    assert progress.value() == initial_value

    bridge.operation_event.emit(OperationStageChanged("LOOK", "reading_files"))
    bridge.operation_event.emit(OperationProgress("LOOK", "reading_files", 2, 4))

    assert stage.text() == "Reading files"
    assert progress.maximum() == 4
    assert progress.value() == 2
    assert controller.cancel_active()
    assert bridge.handles["LOOK"].cancel_calls == 1
    assert store.state.active_operation is not None
    assert not first.isEnabled()

    bridge.operation_event.emit(OperationCancelled("LOOK"))

    assert store.state.active_operation is not None
    assert not first.isEnabled()

    bridge.cancelled.emit("LOOK")

    assert store.state.active_operation is None
    assert first.isEnabled()


def test_active_provider_is_visible_and_foreign_provider_events_are_ignored(qapp) -> None:
    del qapp
    bridge = ManualBridge()
    store = StateStore()

    def reducer(state: SessionState, _result: object) -> StateApplicationResult:
        return StateApplicationResult(state, ResultApplicationStatus.APPLIED, None)

    controller, _first, _second, stage, progress = make_controller(bridge, store, reducer)
    controller.start("LOOKUP", OperationKind.SCAN, (), work)
    bridge.operation_event.emit(OperationStageChanged("LOOKUP", "enriching_selection"))
    bridge.operation_event.emit(OperationProgress("LOOKUP", "enriching_selection", 0, 0))
    bridge.operation_event.emit(ProviderStarted("LOOKUP", "musicbrainz"))

    assert "musicbrainz" in stage.text().casefold()
    assert "enrich" in stage.text().casefold()
    assert progress.minimum() == progress.maximum() == 0
    visible = stage.text()

    bridge.operation_event.emit(ProviderStarted("OLD-LOOKUP", "vgmdb"))

    assert stage.text() == visible


def test_requested_cancellation_remains_visible_during_safe_boundary_events(qapp) -> None:
    del qapp
    bridge = ManualBridge()
    store = StateStore()

    def reducer(state: SessionState, _result: object) -> StateApplicationResult:
        return StateApplicationResult(state, ResultApplicationStatus.APPLIED, None)

    controller, first, _second, stage, progress = make_controller(bridge, store, reducer)
    controller.start("APPLY", OperationKind.SCAN, (), work)
    assert controller.cancel_active()

    # A current transaction can still finish safely and enqueue progress after
    # cancellation. That must not make the cancellation request disappear.
    bridge.operation_event.emit(OperationStageChanged("APPLY", "applying_files"))
    bridge.operation_event.emit(OperationProgress("APPLY", "applying_files", 1, 3))

    assert "cancelling" in stage.text().casefold()
    assert progress.value() == 1
    assert not first.isEnabled()


def test_duplicate_completed_payload_is_reduced_only_once(qapp) -> None:
    del qapp
    bridge = ManualBridge()
    store = StateStore()
    received: list[object] = []

    def reducer(state: SessionState, result: object) -> StateApplicationResult:
        received.append(result)

        return StateApplicationResult(
            replace(state, revision=state.revision + 1),
            ResultApplicationStatus.APPLIED,
            None,
        )

    controller, first, _second, _stage, _progress = make_controller(bridge, store, reducer)
    controller.start("APPLY", OperationKind.SCAN, (), work)
    result = WorkerResult("completed")
    bridge.completed.emit("APPLY", result)
    finished = store.state
    bridge.completed.emit("APPLY", result)

    assert received == [result]
    assert store.state is finished
    assert first.isEnabled()


def test_late_cancel_or_failure_cannot_erase_the_terminal_results_summary(qapp) -> None:
    del qapp
    bridge = ManualBridge()
    store = StateStore()

    def reducer(state: SessionState, _result: object) -> StateApplicationResult:
        return StateApplicationResult(state, ResultApplicationStatus.APPLIED, None)

    controller, _first, _second, stage, _progress = make_controller(bridge, store, reducer)
    controller.start("APPLY", OperationKind.SCAN, (), work)
    bridge.completed.emit("APPLY", WorkerResult("completed"))
    # The Apply presentation slot runs after the state-owning reducer slot.
    stage.setText("Completed: 1 file written")
    bridge.cancelled.emit("APPLY")
    bridge.failed.emit("APPLY", RuntimeError("late notification"))

    assert stage.text() == "Completed: 1 file written"


def test_submission_and_reducer_failures_restore_controls(qapp, qtbot) -> None:
    del qapp
    submission_error = RuntimeError("executor unavailable")
    failing_bridge = ManualBridge(submission_error)
    submission_store = StateStore()

    def identity_reducer(state: SessionState, _result: object) -> StateApplicationResult:
        return StateApplicationResult(state, ResultApplicationStatus.APPLIED, None)

    submission_controller, first, _second, _stage, _progress = make_controller(
        failing_bridge,
        submission_store,
        identity_reducer,
    )

    with pytest.raises(RuntimeError, match="executor unavailable"):
        submission_controller.start("SCAN-SUBMIT", OperationKind.SCAN, (), work)

    assert submission_store.state.active_operation is None
    assert first.isEnabled()

    reducer_bridge = ManualBridge()
    reducer_store = StateStore()

    def failing_reducer(_state: SessionState, _result: object) -> StateApplicationResult:
        raise ValueError("invalid worker payload")

    reducer_controller, reducer_control, _second, _stage, _progress = make_controller(
        reducer_bridge,
        reducer_store,
        failing_reducer,
    )
    reducer_controller.start("SCAN-REDUCE", OperationKind.SCAN, (), work)

    with qtbot.waitSignal(reducer_controller.controller_failed) as failure:
        reducer_bridge.completed.emit("SCAN-REDUCE", WorkerResult("bad"))

    assert failure.args is not None
    assert failure.args[0] == "SCAN-REDUCE"
    assert isinstance(failure.args[1], ValueError)
    assert reducer_store.state.active_operation is None
    assert reducer_control.isEnabled()


def test_control_eligibility_is_restored_and_second_start_cannot_replace_active_work(
    qapp,
) -> None:
    del qapp
    bridge = ManualBridge()
    store = StateStore()

    def reducer(state: SessionState, _result: object) -> StateApplicationResult:
        return StateApplicationResult(state, ResultApplicationStatus.APPLIED, None)

    controller, first, second, _stage, _progress = make_controller(bridge, store, reducer)
    first.setEnabled(False)
    controller.start("FIRST", OperationKind.SCAN, (), work)

    with pytest.raises(ValueError, match="already active"):
        controller.start("SECOND", OperationKind.SCAN, (), work)

    assert tuple(bridge.handles) == ("FIRST",)
    assert not first.isEnabled()
    assert not second.isEnabled()

    bridge.cancelled.emit("FIRST")

    assert not first.isEnabled()
    assert second.isEnabled()


def test_control_eligibility_is_captured_before_state_setter_disables_for_busy_state(
    qapp,
) -> None:
    del qapp
    bridge = ManualBridge()
    store = StateStore()
    domain_disabled = QPushButton("Domain disabled")
    normally_enabled = QPushButton("Normally enabled")
    domain_disabled.setEnabled(False)
    stage = QLabel("Idle")
    progress = QProgressBar()

    def set_state(state: SessionState) -> None:
        store.set(state)
        domain_disabled.setEnabled(False)
        normally_enabled.setEnabled(state.active_operation is None)

    def reducer(state: SessionState, _result: object) -> StateApplicationResult:
        return StateApplicationResult(state, ResultApplicationStatus.APPLIED, None)

    controller = OperationController(
        bridge=bridge,  # type: ignore[arg-type]
        get_state=store.get,
        set_state=set_state,
        result_reducers=(ResultReducerBinding(WorkerResult, reducer),),
        conflicting_controls=(domain_disabled, normally_enabled),
        stage_label=stage,
        progress_bar=progress,
    )
    controller.start("STATEFUL", OperationKind.SCAN, (), work)
    bridge.completed.emit("STATEFUL", WorkerResult("done"))

    assert not domain_disabled.isEnabled()
    assert normally_enabled.isEnabled()


def test_terminal_state_eligibility_wins_over_the_temporary_busy_snapshot(qapp) -> None:
    del qapp
    bridge = ManualBridge()
    store = StateStore()
    control = QPushButton("Only valid at revision zero")
    stage = QLabel("Idle")
    progress = QProgressBar()

    def set_state(state: SessionState) -> None:
        store.set(state)
        control.setEnabled(state.active_operation is None and state.revision == 0)

    def reducer(state: SessionState, _result: object) -> StateApplicationResult:
        return StateApplicationResult(
            replace(state, revision=1),
            ResultApplicationStatus.APPLIED,
            None,
        )

    controller = OperationController(
        bridge=bridge,  # type: ignore[arg-type]
        get_state=store.get,
        set_state=set_state,
        result_reducers=(ResultReducerBinding(WorkerResult, reducer),),
        conflicting_controls=(control,),
        stage_label=stage,
        progress_bar=progress,
    )
    controller.start("ELIGIBILITY", OperationKind.SCAN, (), work)
    bridge.completed.emit("ELIGIBILITY", WorkerResult("done"))

    assert store.state.revision == 1
    assert store.state.active_operation is None
    assert not control.isEnabled()


def test_current_bridge_failure_is_forwarded_after_state_and_controls_recover(
    qapp,
    qtbot,
) -> None:
    del qapp
    bridge = ManualBridge()
    store = StateStore()

    def reducer(state: SessionState, _result: object) -> StateApplicationResult:
        return StateApplicationResult(state, ResultApplicationStatus.APPLIED, None)

    controller, first, _second, _stage, _progress = make_controller(bridge, store, reducer)
    controller.start("FAILED", OperationKind.SCAN, (), work)
    error = RuntimeError("worker failed")

    with qtbot.waitSignal(controller.controller_failed) as failure:
        bridge.failed.emit("FAILED", error)

    assert failure.args == ["FAILED", error]
    assert store.state.active_operation is None
    assert first.isEnabled()


def test_cancelled_apply_result_uses_completed_payload_path_before_finish(qapp) -> None:
    del qapp
    source = make_apply_source()
    group = GroupState(
        group=AlbumGroup(
            group_id="group-1",
            files=(source,),
            album_title="Album",
            reason=GroupingReason.DIRECTORY_ALBUM_CONSISTENT,
        )
    )
    bridge = ManualBridge()
    store = StateStore()
    store.state = SessionState(root=Path("library"), groups=(group,))
    control = QPushButton("Apply")
    stage = QLabel("Idle")
    progress = QProgressBar()
    reduced_statuses: list[ApplyBatchStatus] = []

    def reduce_apply(state: SessionState, raw_result: object) -> StateApplicationResult:
        assert isinstance(raw_result, ApplyBatchResult)
        reduced_statuses.append(raw_result.status)

        return apply_batch_result(state, raw_result)

    controller = OperationController(
        bridge=bridge,  # type: ignore[arg-type]
        get_state=store.get,
        set_state=store.set,
        result_reducers=(ResultReducerBinding(ApplyBatchResult, reduce_apply),),
        conflicting_controls=(control,),
        stage_label=stage,
        progress_bar=progress,
    )
    controller.start("APPLY-1", OperationKind.APPLY, ("group-1",), work)
    active = store.state.active_operation
    assert active is not None
    reviews = make_apply_reviews(source)
    change_set = build_change_set(source, reviews, RenameDecision.KEEP_FILENAME)
    result = ApplyBatchResult(
        operation_id="APPLY-1",
        base_session_revision=active.base_session_revision,
        base_library_revision=active.base_library_revision,
        status=ApplyBatchStatus.CANCELLED,
        groups=(
            ApplyGroupOutcome(
                group_id="group-1",
                base_group_revision=0,
                files=(
                    ApplyFileOutcome(
                        file_id=source.file_id,
                        source_path=source.path,
                        selected_release=None,
                        reviews=reviews,
                        change_set=change_set,
                        status=ApplyFileOutcomeStatus.CANCELLED,
                        transaction_result=FileApplyResult(
                            source_path=source.path,
                            final_path=source.path,
                            status=FileApplyStatus.CANCELLED,
                            completed_stage=FileTransactionStage.WRITING_METADATA,
                        ),
                    ),
                ),
            ),
        ),
        report_result=ReportWriteResult(ReportWriteStatus.DISABLED, None, None),
    )

    bridge.operation_event.emit(OperationCompleted("APPLY-1"))
    assert store.state.active_operation is active

    bridge.completed.emit("APPLY-1", result)

    assert reduced_statuses == [ApplyBatchStatus.CANCELLED]
    assert store.state.active_operation is None
    assert control.isEnabled()
