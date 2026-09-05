"""Plain-Python serial background execution with cooperative cancellation."""

from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from threading import Lock
from typing import Protocol

from metadata_polisher.execution.cancellation import (
    CancellationToken,
    MutableCancellationToken,
    OperationCancelledError,
)
from metadata_polisher.execution.events import (
    OperationCancelled,
    OperationCompleted,
    OperationEventSink,
    OperationStarted,
)

type OperationWork[T] = Callable[[CancellationToken, OperationEventSink], T]
type OperationDoneCallback[T] = Callable[[OperationHandle[T]], None]


class OperationHandle[T](Protocol):
    """Controllable result of one submitted operation."""

    @property
    def operation_id(self) -> str: ...

    def cancel(self) -> None: ...

    def is_cancel_requested(self) -> bool: ...

    def done(self) -> bool: ...

    def result(self, timeout: float | None = None) -> T: ...

    def add_done_callback(self, callback: OperationDoneCallback[T]) -> None: ...


class ProcessingExecutor(Protocol):
    """Future-compatible boundary for expensive application work."""

    def submit[T](
        self,
        operation_id: str,
        work: OperationWork[T],
        events: OperationEventSink,
    ) -> OperationHandle[T]: ...

    def shutdown(self, wait: bool = True) -> None: ...


class _SerialOperationHandle[T]:
    """Expose only cooperative cancellation over an internal Future."""

    def __init__(
        self,
        operation_id: str,
        cancellation: MutableCancellationToken,
        future: Future[T],
    ) -> None:
        self._operation_id = operation_id
        self._cancellation = cancellation
        self._future = future

    @property
    def operation_id(self) -> str:
        return self._operation_id

    def cancel(self) -> None:
        # Future.cancel() could discard queued work without giving it a chance
        # to emit the semantic cancellation event expected by the UI.
        self._cancellation.cancel()

    def is_cancel_requested(self) -> bool:
        return self._cancellation.is_cancelled()

    def done(self) -> bool:
        return self._future.done()

    def result(self, timeout: float | None = None) -> T:
        return self._future.result(timeout=timeout)

    def add_done_callback(self, callback: OperationDoneCallback[T]) -> None:
        def invoke_with_public_handle(_future: Future[T]) -> None:
            callback(self)

        self._future.add_done_callback(invoke_with_public_handle)


class SerialBackgroundExecutor:
    """Run submitted operations in order on exactly one background worker."""

    def __init__(self) -> None:
        self._worker = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="metadata-polisher",
        )
        self._state_lock = Lock()
        self._shut_down = False

    def submit[T](
        self,
        operation_id: str,
        work: OperationWork[T],
        events: OperationEventSink,
    ) -> OperationHandle[T]:
        # Constructing the first event validates the operation identity on the
        # caller thread, before an invalid request can enter the worker queue.
        started_event = OperationStarted(operation_id)
        cancellation = MutableCancellationToken()

        # Submission and shutdown share a lock so a new operation cannot slip
        # into the queue after the executor has stopped accepting work.
        with self._state_lock:
            if self._shut_down:
                raise RuntimeError("The processing executor has been shut down.")

            future = self._worker.submit(
                self._run_operation,
                started_event,
                work,
                cancellation,
                events,
            )

        return _SerialOperationHandle(operation_id, cancellation, future)

    @staticmethod
    def _run_operation[T](
        started_event: OperationStarted,
        work: OperationWork[T],
        cancellation: MutableCancellationToken,
        events: OperationEventSink,
    ) -> T:
        operation_id = started_event.operation_id

        # A cancelled queued operation still runs this small entry point to
        # publish its cancellation event, but never begins the supplied work.
        try:
            cancellation.raise_if_cancelled()
        except OperationCancelledError:
            events.emit(OperationCancelled(operation_id))
            raise

        events.emit(started_event)

        try:
            result = work(cancellation, events)
        except OperationCancelledError:
            events.emit(OperationCancelled(operation_id))
            raise

        # Do not inspect the token after a normal return. A transactional task
        # may return a typed partial/cancelled result after completing the safe
        # boundary for its current file, and that result must remain intact.
        events.emit(OperationCompleted(operation_id))

        return result

    def shutdown(self, wait: bool = True) -> None:
        """Stop accepting work without forcing cancellation of queued/running work."""
        if type(wait) is not bool:
            raise TypeError("wait must be a bool")

        with self._state_lock:
            self._shut_down = True

        # ThreadPoolExecutor permits repeated shutdown calls. Calling it again
        # with wait=True is important when an earlier UI close phase requested
        # a non-waiting stop and final composition teardown must later join.
        self._worker.shutdown(wait=wait, cancel_futures=False)
