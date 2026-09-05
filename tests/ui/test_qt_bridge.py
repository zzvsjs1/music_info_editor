from collections.abc import Iterator
from threading import Event, get_ident
from typing import cast

import pytest

from metadata_polisher.execution.cancellation import CancellationToken
from metadata_polisher.execution.events import (
    OperationCancelled,
    OperationCompleted,
    OperationEvent,
    OperationEventSink,
    OperationStageChanged,
    OperationStarted,
)
from metadata_polisher.execution.executor import (
    OperationDoneCallback,
    OperationHandle,
    OperationWork,
    SerialBackgroundExecutor,
)
from metadata_polisher.ui.qt_bridge import QtOperationBridge


@pytest.fixture
def background_executor() -> Iterator[SerialBackgroundExecutor]:
    # Use a real worker thread here: a synchronous fake cannot establish that
    # both progress and terminal notifications cross onto Qt's owning thread.
    executor = SerialBackgroundExecutor()

    try:
        yield executor
    finally:
        executor.shutdown()


def test_worker_event_and_completion_are_queued_onto_the_ui_thread(
    qtbot,
    background_executor: SerialBackgroundExecutor,
) -> None:
    ui_thread = get_ident()
    bridge = QtOperationBridge(background_executor)
    received_events: list[OperationEvent] = []
    event_threads: list[int] = []
    completion_threads: list[int] = []

    def record_event(event: OperationEvent) -> None:
        received_events.append(event)
        event_threads.append(get_ident())

    def record_completion(_operation_id: str, _result: object) -> None:
        completion_threads.append(get_ident())

    bridge.operation_event.connect(record_event)
    bridge.completed.connect(record_completion)

    def work(_cancellation: CancellationToken, events: OperationEventSink) -> int:
        events.emit(OperationStageChanged("SCAN-0001", "reading_files"))

        return get_ident()

    with qtbot.waitSignal(bridge.completed, timeout=2000) as completed:
        bridge.submit("SCAN-0001", work)

    qtbot.waitUntil(lambda: len(received_events) == 3, timeout=2000)

    assert completed.args is not None
    assert completed.args[0] == "SCAN-0001"
    assert completed.args[1] != ui_thread
    assert received_events == [
        OperationStarted("SCAN-0001"),
        OperationStageChanged("SCAN-0001", "reading_files"),
        OperationCompleted("SCAN-0001"),
    ]
    assert event_threads == [ui_thread, ui_thread, ui_thread]
    assert completion_threads == [ui_thread]


def test_cooperative_worker_cancellation_is_reported_on_the_ui_thread(
    qtbot,
    background_executor: SerialBackgroundExecutor,
) -> None:
    ui_thread = get_ident()
    bridge = QtOperationBridge(background_executor)
    work_started = Event()
    # Hold the worker at an explicit safe boundary so the test controls when
    # cancellation is observed without depending on thread scheduling delays.
    cancellation_boundary = Event()
    cancelled_threads: list[int] = []
    completed: list[object] = []
    failed: list[object] = []
    received_events: list[OperationEvent] = []
    bridge.cancelled.connect(lambda _operation_id: cancelled_threads.append(get_ident()))
    bridge.completed.connect(lambda _operation_id, result: completed.append(result))
    bridge.failed.connect(lambda _operation_id, error: failed.append(error))
    bridge.operation_event.connect(received_events.append)

    def work(cancellation: CancellationToken, _events: OperationEventSink) -> None:
        work_started.set()
        assert cancellation_boundary.wait(timeout=2)
        cancellation.raise_if_cancelled()

    handle = bridge.submit("LOOKUP-0001", work)

    try:
        qtbot.waitUntil(work_started.is_set, timeout=2000)

        with qtbot.waitSignal(bridge.cancelled, timeout=2000) as cancelled:
            handle.cancel()
            cancellation_boundary.set()
    finally:
        cancellation_boundary.set()

    qtbot.waitUntil(lambda: OperationCancelled("LOOKUP-0001") in received_events, timeout=2000)

    assert cancelled.args == ["LOOKUP-0001"]
    assert cancelled_threads == [ui_thread]
    assert completed == []
    assert failed == []


def test_worker_exception_identity_is_preserved_by_failed_signal(
    qtbot,
    background_executor: SerialBackgroundExecutor,
) -> None:
    ui_thread = get_ident()
    bridge = QtOperationBridge(background_executor)
    failure = LookupError("provider parsing failed")
    failure_threads: list[int] = []
    received_failure: list[BaseException] = []

    def record_failure(_operation_id: str, error: BaseException) -> None:
        received_failure.append(error)
        failure_threads.append(get_ident())

    bridge.failed.connect(record_failure)

    def work(_cancellation: CancellationToken, _events: OperationEventSink) -> None:
        raise failure

    with qtbot.waitSignal(bridge.failed, timeout=2000) as failed:
        bridge.submit("LOOKUP-0001", work)

    assert failed.args == ["LOOKUP-0001", failure]
    assert received_failure == [failure]
    assert failure_threads == [ui_thread]


class ControllableHandle:
    def __init__(self, operation_id: str, result: object) -> None:
        self._operation_id = operation_id
        self._result = result
        self._done = False
        self._callbacks: list[OperationDoneCallback[object]] = []
        self.done_checks = 0
        self.result_calls = 0

    @property
    def operation_id(self) -> str:
        return self._operation_id

    def cancel(self) -> None:
        return

    def done(self) -> bool:
        self.done_checks += 1

        return self._done

    def result(self, timeout: float | None = None) -> object:
        del timeout
        self.result_calls += 1

        if not self._done:
            raise AssertionError("result() was called before completion")

        return self._result

    def add_done_callback(self, callback: OperationDoneCallback[object]) -> None:
        self._callbacks.append(callback)

    def fire_done_callback(self, *, done: bool) -> None:
        self._done = done

        for callback in self._callbacks:
            callback(self)


class ControllableExecutor:
    def __init__(self, result: object) -> None:
        self._result = result
        self.handle: ControllableHandle | None = None
        self.event_sink: OperationEventSink | None = None
        self.shutdown_calls = 0

    def submit[T](
        self,
        operation_id: str,
        work: OperationWork[T],
        events: OperationEventSink,
    ) -> OperationHandle[T]:
        del work
        self.event_sink = events
        self.handle = ControllableHandle(operation_id, self._result)

        return cast(OperationHandle[T], self.handle)

    def shutdown(self, wait: bool = True) -> None:
        del wait
        self.shutdown_calls += 1


def test_bridge_only_reads_result_after_done_and_forwards_objects_unchanged(qtbot) -> None:
    expected_result = object()
    executor = ControllableExecutor(expected_result)
    bridge = QtOperationBridge(executor)
    received_events: list[OperationEvent] = []
    completions: list[tuple[str, object]] = []
    bridge.operation_event.connect(received_events.append)
    bridge.completed.connect(lambda operation_id, result: completions.append((operation_id, result)))
    handle = bridge.submit("SCAN-0001", lambda _token, _events: object())
    stage = OperationStageChanged("SCAN-0001", "reading_files")
    assert executor.event_sink is not bridge
    assert executor.handle is not None

    executor.event_sink.emit(stage)

    assert received_events == []
    qtbot.waitUntil(lambda: received_events == [stage], timeout=2000)
    assert received_events[0] is stage

    executor.handle.fire_done_callback(done=False)
    assert completions == []
    qtbot.waitUntil(lambda: executor.handle is not None and executor.handle.done_checks == 1, timeout=2000)
    assert executor.handle.result_calls == 0

    executor.handle.fire_done_callback(done=True)
    assert completions == []
    qtbot.waitUntil(lambda: len(completions) == 1, timeout=2000)

    assert handle is executor.handle
    assert completions == [("SCAN-0001", expected_result)]
    assert executor.handle.result_calls == 1
    assert executor.shutdown_calls == 0


def test_repeated_done_callback_presents_one_terminal_result(qtbot, qapp) -> None:
    expected = object()
    executor = ControllableExecutor(expected)
    bridge = QtOperationBridge(executor)
    completed: list[object] = []
    bridge.completed.connect(lambda _operation_id, result: completed.append(result))
    bridge.submit("APPLY-ONCE", lambda _token, _events: expected)
    assert executor.handle is not None

    executor.handle.fire_done_callback(done=True)
    executor.handle.fire_done_callback(done=True)
    qtbot.waitUntil(lambda: bool(completed), timeout=2000)
    # Drain both queued callbacks. The second must be ignored before result()
    # is consumed again or another results presentation is emitted.
    qapp.processEvents()

    assert completed == [expected]
    assert executor.handle.result_calls == 1
