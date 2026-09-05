"""Cooperative cancellation primitives independent of Qt."""

from threading import Event
from typing import Protocol


class OperationCancelledError(RuntimeError):
    """Raised only when work reaches a safe cooperative cancellation point."""


class CancellationToken(Protocol):
    """Read-only cancellation view accepted by long-running application work."""

    def is_cancelled(self) -> bool: ...

    def raise_if_cancelled(self) -> None: ...


class NeverCancelledToken:
    """Cancellation token for synchronous callers that never request a stop."""

    def is_cancelled(self) -> bool:
        return False

    def raise_if_cancelled(self) -> None:
        return


class MutableCancellationToken:
    """Thread-safe token whose state may be changed by the controlling thread."""

    def __init__(self) -> None:
        # Event safely shares the stop request between threads. It carries no
        # interruption mechanism; the worker decides which boundaries are safe.
        self._cancelled = Event()

    def cancel(self) -> None:
        """Request cancellation without interrupting the active worker."""
        self._cancelled.set()

    def is_cancelled(self) -> bool:
        return self._cancelled.is_set()

    def raise_if_cancelled(self) -> None:
        if self.is_cancelled():
            raise OperationCancelledError("The operation was cancelled.")
