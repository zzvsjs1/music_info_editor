"""Queue plain-Python operation notifications onto the Qt UI thread."""

from collections.abc import Callable
from typing import cast

from PySide6.QtCore import QObject, Qt, Signal, Slot

from metadata_polisher.execution.cancellation import OperationCancelledError
from metadata_polisher.execution.events import OperationEvent
from metadata_polisher.execution.executor import (
    OperationHandle,
    OperationWork,
    ProcessingExecutor,
)


class _QueuedOperationEventSink:
    """Keep the core sink protocol separate from QObject's legacy emit API."""

    def __init__(self, enqueue: Callable[[OperationEvent], None]) -> None:
        self._enqueue = enqueue

    def emit(self, event: OperationEvent) -> None:
        self._enqueue(event)


class QtOperationBridge(QObject):
    """Translate executor callbacks into queued Qt signals without UI policy."""

    operation_event = Signal(object)
    completed = Signal(str, object)
    cancelled = Signal(str)
    failed = Signal(str, object)

    # Both callback entry points can run on a worker. Keeping their only action
    # as emitting a private queued signal makes the public boundary consistently
    # execute on this object's Qt thread, even for an already-complete Future.
    _event_enqueued = Signal(object)
    _done_enqueued = Signal(object)

    def __init__(
        self,
        executor: ProcessingExecutor,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._executor = executor
        self._pending_handles: dict[str, OperationHandle[object]] = {}
        self._event_sink = _QueuedOperationEventSink(self._enqueue_event)
        self._event_enqueued.connect(
            self._deliver_event,
            Qt.ConnectionType.QueuedConnection,
        )
        self._done_enqueued.connect(
            self._deliver_done,
            Qt.ConnectionType.QueuedConnection,
        )

    def submit[T](
        self,
        operation_id: str,
        work: OperationWork[T],
    ) -> OperationHandle[T]:
        """Submit core work and return its cooperative control handle."""
        if operation_id in self._pending_handles:
            raise ValueError("this operation already has a pending completion")

        handle = self._executor.submit(operation_id, work, self._event_sink)
        self._pending_handles[operation_id] = cast(OperationHandle[object], handle)
        # Register ownership before the callback: an already-complete Future may
        # invoke it immediately, although public delivery still goes through Qt.
        handle.add_done_callback(self._enqueue_done)

        return handle

    def _enqueue_event(self, event: OperationEvent) -> None:
        self._event_enqueued.emit(event)

    @Slot(object)
    def _deliver_event(self, event: object) -> None:
        self.operation_event.emit(event)

    def _enqueue_done[T](self, handle: OperationHandle[T]) -> None:
        self._done_enqueued.emit(handle)

    @Slot(object)
    def _deliver_done(self, raw_handle: object) -> None:
        handle = cast(OperationHandle[object], raw_handle)

        # Identity distinguishes the exact pending handle from duplicate or late
        # callbacks, including a different handle that reuses an operation ID.
        if self._pending_handles.get(handle.operation_id) is not handle:
            return

        # A conforming Future callback is invoked only after completion. The
        # guard protects the UI thread from a faulty controllable/test executor.
        if not handle.done():
            return

        # Claim the terminal delivery before emitting any public signal, whose
        # slots may submit another operation or process queued Qt events.
        del self._pending_handles[handle.operation_id]

        try:
            result = handle.result()
        except OperationCancelledError:
            self.cancelled.emit(handle.operation_id)
        except Exception as error:
            self.failed.emit(handle.operation_id, error)
        else:
            self.completed.emit(handle.operation_id, result)
