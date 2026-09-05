from collections.abc import Iterator
from pathlib import Path
from threading import Event, Lock, Thread, get_ident

import pytest

from metadata_polisher.execution.cancellation import (
    CancellationToken,
    OperationCancelledError,
)
from metadata_polisher.execution.events import (
    FileApplyStatus,
    FileTransactionStage,
    OperationCancelled,
    OperationCompleted,
    OperationEvent,
    OperationEventSink,
    OperationStarted,
)
from metadata_polisher.execution.executor import SerialBackgroundExecutor
from metadata_polisher.infrastructure.transaction import FileApplyResult


class RecordingEventSink:
    def __init__(self) -> None:
        self._events: list[OperationEvent] = []
        self._lock = Lock()

    def emit(self, event: OperationEvent) -> None:
        with self._lock:
            self._events.append(event)

    def snapshot(self) -> tuple[OperationEvent, ...]:
        with self._lock:
            return tuple(self._events)


@pytest.fixture
def executor() -> Iterator[SerialBackgroundExecutor]:
    worker = SerialBackgroundExecutor()

    try:
        yield worker
    finally:
        # Always join the worker after the fixture, including assertion failures,
        # so a background operation cannot leak into the next test.
        worker.shutdown()


def test_submit_returns_before_work_finishes_and_runs_off_the_calling_thread(
    executor: SerialBackgroundExecutor,
) -> None:
    # Handshake events hold the worker at a known point, avoiding assumptions
    # about how quickly either thread will be scheduled on the test machine.
    caller_thread = get_ident()
    work_started = Event()
    release_work = Event()
    sink = RecordingEventSink()

    def work(_cancellation: CancellationToken, _events: OperationEventSink) -> int:
        worker_thread = get_ident()
        work_started.set()
        assert release_work.wait(timeout=2)

        return worker_thread

    handle = executor.submit("SCAN-0001", work, sink)

    try:
        assert work_started.wait(timeout=2)
        assert handle.done() is False
    finally:
        release_work.set()

    assert handle.result(timeout=2) != caller_thread
    assert sink.snapshot() == (
        OperationStarted("SCAN-0001"),
        OperationCompleted("SCAN-0001"),
    )


def test_submitted_work_runs_serially_in_submission_order(
    executor: SerialBackgroundExecutor,
) -> None:
    first_started = Event()
    release_first = Event()
    calls: list[str] = []
    sink = RecordingEventSink()

    def first(_cancellation: CancellationToken, _events: OperationEventSink) -> str:
        calls.append("first-started")
        first_started.set()
        assert release_first.wait(timeout=2)
        calls.append("first-finished")

        return "first"

    def second(_cancellation: CancellationToken, _events: OperationEventSink) -> str:
        calls.append("second-started")

        return "second"

    first_handle = executor.submit("SCAN-0001", first, sink)
    second_handle = executor.submit("LOOKUP-0002", second, sink)

    try:
        assert first_started.wait(timeout=2)
        assert calls == ["first-started"]
        assert second_handle.done() is False
    finally:
        release_first.set()

    assert first_handle.result(timeout=2) == "first"
    assert second_handle.result(timeout=2) == "second"
    assert calls == ["first-started", "first-finished", "second-started"]


def test_each_submission_receives_a_distinct_cancellation_token(
    executor: SerialBackgroundExecutor,
) -> None:
    seen_tokens: list[CancellationToken] = []
    sink = RecordingEventSink()

    def remember_token(cancellation: CancellationToken, _events: OperationEventSink) -> None:
        seen_tokens.append(cancellation)

    first = executor.submit("SCAN-0001", remember_token, sink)
    second = executor.submit("SCAN-0002", remember_token, sink)

    first.result(timeout=2)
    second.result(timeout=2)

    assert len(seen_tokens) == 2
    assert seen_tokens[0] is not seen_tokens[1]


def test_cancellation_is_observed_cooperatively_between_work_items(
    executor: SerialBackgroundExecutor,
) -> None:
    first_item_finished = Event()
    continue_work = Event()
    processed: list[int] = []
    sink = RecordingEventSink()

    def work(cancellation: CancellationToken, _events: OperationEventSink) -> None:
        processed.append(1)
        first_item_finished.set()
        assert continue_work.wait(timeout=2)
        cancellation.raise_if_cancelled()
        processed.append(2)

    handle = executor.submit("APPLY-0001", work, sink)

    try:
        assert first_item_finished.wait(timeout=2)
        assert handle.is_cancel_requested() is False
        handle.cancel()
        assert handle.is_cancel_requested() is True
    finally:
        continue_work.set()

    with pytest.raises(OperationCancelledError, match="operation was cancelled"):
        handle.result(timeout=2)

    assert processed == [1]
    assert sink.snapshot() == (
        OperationStarted("APPLY-0001"),
        OperationCancelled("APPLY-0001"),
    )


def test_pre_cancelled_queued_operation_never_starts_its_work(
    executor: SerialBackgroundExecutor,
) -> None:
    blocker_started = Event()
    release_blocker = Event()
    queued_work_called = Event()
    first_sink = RecordingEventSink()
    queued_sink = RecordingEventSink()

    def blocker(_cancellation: CancellationToken, _events: OperationEventSink) -> None:
        blocker_started.set()
        assert release_blocker.wait(timeout=2)

    def queued_work(_cancellation: CancellationToken, _events: OperationEventSink) -> None:
        queued_work_called.set()

    blocker_handle = executor.submit("SCAN-0001", blocker, first_sink)
    queued_handle = executor.submit("LOOKUP-0002", queued_work, queued_sink)

    try:
        assert blocker_started.wait(timeout=2)
        queued_handle.cancel()
    finally:
        release_blocker.set()

    blocker_handle.result(timeout=2)

    with pytest.raises(OperationCancelledError, match="operation was cancelled"):
        queued_handle.result(timeout=2)

    assert queued_work_called.is_set() is False
    assert queued_sink.snapshot() == (OperationCancelled("LOOKUP-0002"),)


def test_normal_typed_cancelled_result_is_not_reclassified_by_executor(
    executor: SerialBackgroundExecutor,
) -> None:
    ready_to_return = Event()
    allow_return = Event()
    sink = RecordingEventSink()
    cancelled_file_result = FileApplyResult(
        source_path=Path("album/track.flac"),
        final_path=Path("album/track.flac"),
        status=FileApplyStatus.CANCELLED,
        completed_stage=FileTransactionStage.COPYING_TEMPORARY,
    )

    def work(
        _cancellation: CancellationToken,
        _events: OperationEventSink,
    ) -> FileApplyResult:
        ready_to_return.set()
        assert allow_return.wait(timeout=2)

        return cancelled_file_result

    handle = executor.submit("APPLY-0001", work, sink)

    try:
        assert ready_to_return.wait(timeout=2)
        handle.cancel()
    finally:
        allow_return.set()

    assert handle.result(timeout=2) is cancelled_file_result
    assert sink.snapshot() == (
        OperationStarted("APPLY-0001"),
        OperationCompleted("APPLY-0001"),
    )


def test_worker_exception_is_preserved_for_the_result_caller(
    executor: SerialBackgroundExecutor,
) -> None:
    failure = LookupError("provider contract broke")
    sink = RecordingEventSink()

    def work(_cancellation: CancellationToken, _events: OperationEventSink) -> None:
        raise failure

    handle = executor.submit("LOOKUP-0001", work, sink)

    with pytest.raises(LookupError) as captured:
        handle.result(timeout=2)

    assert captured.value is failure
    assert sink.snapshot() == (OperationStarted("LOOKUP-0001"),)


def test_shutdown_does_not_force_cancel_running_work() -> None:
    executor = SerialBackgroundExecutor()
    work_started = Event()
    release_work = Event()
    sink = RecordingEventSink()

    def work(cancellation: CancellationToken, _events: OperationEventSink) -> str:
        work_started.set()
        assert release_work.wait(timeout=2)
        assert cancellation.is_cancelled() is False

        return "finished safely"

    handle = executor.submit("APPLY-0001", work, sink)

    try:
        assert work_started.wait(timeout=2)
        executor.shutdown(wait=False)
        assert handle.done() is False
    finally:
        release_work.set()

    assert handle.result(timeout=2) == "finished safely"
    executor.shutdown()


def test_later_waiting_shutdown_joins_after_an_initial_non_waiting_shutdown() -> None:
    executor = SerialBackgroundExecutor()
    work_started = Event()
    release_work = Event()
    waiting_shutdown_started = Event()
    waiting_shutdown_returned = Event()
    sink = RecordingEventSink()

    def work(_cancellation: CancellationToken, _events: OperationEventSink) -> None:
        work_started.set()
        assert release_work.wait(timeout=2)

    handle = executor.submit("APPLY-0001", work, sink)
    assert work_started.wait(timeout=2)
    executor.shutdown(wait=False)

    def wait_for_shutdown() -> None:
        waiting_shutdown_started.set()
        executor.shutdown(wait=True)
        waiting_shutdown_returned.set()

    waiting_thread = Thread(target=wait_for_shutdown)
    waiting_thread.start()

    try:
        assert waiting_shutdown_started.wait(timeout=2)
        assert waiting_shutdown_returned.is_set() is False
    finally:
        release_work.set()

    assert waiting_shutdown_returned.wait(timeout=2)
    waiting_thread.join(timeout=2)
    assert waiting_thread.is_alive() is False
    assert handle.result(timeout=2) is None


def test_shutdown_is_idempotent_and_rejects_later_submissions() -> None:
    executor = SerialBackgroundExecutor()
    sink = RecordingEventSink()

    executor.shutdown()
    executor.shutdown()

    with pytest.raises(RuntimeError, match="shut down"):
        executor.submit("SCAN-0001", lambda _token, _events: None, sink)
