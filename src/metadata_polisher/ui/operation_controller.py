"""UI-thread coordination for one major background operation at a time."""

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import cast

from PySide6.QtCore import QEvent, QObject, Signal, Slot
from PySide6.QtWidgets import QLabel, QProgressBar, QWidget

from metadata_polisher.execution.events import (
    FileCompleted,
    FileFailed,
    FileStageChanged,
    FileStarted,
    OperationCancelled,
    OperationCompleted,
    OperationProgress,
    OperationStageChanged,
    OperationStarted,
    ProviderCompleted,
    ProviderFailed,
    ProviderStarted,
)
from metadata_polisher.execution.executor import OperationHandle, OperationWork
from metadata_polisher.infrastructure.logging_setup import redact_sensitive_text
from metadata_polisher.session.state import (
    OperationKind,
    SessionState,
    StateApplicationResult,
    begin_operation,
    finish_operation,
)
from metadata_polisher.ui.operation_progress_dialog import OperationProgressDialog
from metadata_polisher.ui.qt_bridge import QtOperationBridge

LOGGER = logging.getLogger(__name__)

type StateGetter = Callable[[], SessionState]
type StateSetter = Callable[[SessionState], None]
type ResultReducer = Callable[[SessionState, object], StateApplicationResult]


@dataclass(frozen=True)
class ResultReducerBinding:
    """Bind one immutable worker result type to its session reducer."""

    result_type: type[object]
    reducer: ResultReducer

    def __post_init__(self) -> None:
        if not isinstance(self.result_type, type):
            raise TypeError("result_type must be a type")

        if not callable(self.reducer):
            raise TypeError("reducer must be callable")


class OperationController(QObject):
    """Apply bridge results before ending their captured session operation."""

    controller_failed = Signal(str, object)
    cancellation_requested = Signal(str)

    def __init__(
        self,
        *,
        bridge: QtOperationBridge,
        get_state: StateGetter,
        set_state: StateSetter,
        result_reducers: Sequence[ResultReducerBinding],
        conflicting_controls: Sequence[QWidget],
        stage_label: QLabel,
        progress_bar: QProgressBar,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)

        if not callable(get_state) or not callable(set_state):
            raise TypeError("get_state and set_state must be callable")

        bindings = tuple(result_reducers)

        if any(not isinstance(binding, ResultReducerBinding) for binding in bindings):
            raise TypeError("result_reducers must contain ResultReducerBinding values")

        result_types = tuple(binding.result_type for binding in bindings)

        if len(result_types) != len(set(result_types)):
            raise ValueError("each worker result type must have exactly one reducer")

        controls = tuple(conflicting_controls)

        if any(not isinstance(control, QWidget) for control in controls):
            raise TypeError("conflicting_controls must contain QWidget values")

        if not isinstance(stage_label, QLabel):
            raise TypeError("stage_label must be a QLabel")

        if not isinstance(progress_bar, QProgressBar):
            raise TypeError("progress_bar must be a QProgressBar")

        self._bridge = bridge
        self._get_state = get_state
        self._set_state = set_state
        self._bindings = bindings
        self._controls = controls
        self._stage_label = stage_label
        self._progress_bar = progress_bar
        self._active_handle: OperationHandle[object] | None = None
        # Busy state is a temporary mask over domain eligibility. Remember each
        # control separately so finishing work does not enable every action.
        self._control_eligibility: tuple[bool, ...] | None = None
        self._reduced_operations: set[str] = set()
        self._cancelling_id: str | None = None
        self._stage_text = "Starting…"
        self._provider = ""
        self._file = ""
        self.progress_dialog: OperationProgressDialog | None = None
        self._dialog_kind: OperationKind | None = None
        self._dialog_parent = parent if isinstance(parent, QWidget) else None

        if self._dialog_parent is not None:
            self._dialog_parent.installEventFilter(self)

        bridge.operation_event.connect(self._on_operation_event)
        bridge.completed.connect(self._on_completed)
        bridge.cancelled.connect(self._on_cancelled)
        bridge.failed.connect(self._on_failed)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if (
            watched is self._dialog_parent
            and event.type() == QEvent.Type.Close
            and self._get_state().active_operation is not None
        ):
            # Keep Qt delivering the worker's safe terminal payload and results.
            # The user can close the main window once that operation has settled.
            self.cancel_active()
            event.ignore()

            return True

        return super().eventFilter(watched, event)

    def start[T](
        self,
        operation_id: str,
        kind: OperationKind,
        target_group_ids: Sequence[str],
        work: OperationWork[T],
    ) -> OperationHandle[T]:
        """Capture lineage, disable conflicting controls, and submit work."""
        current = self._get_state()
        started = begin_operation(current, operation_id, kind, target_group_ids)
        self._enter_busy_state()
        self._open_progress(operation_id, kind)

        try:
            # Capture the prior eligibility first: a state-aware view may disable
            # its buttons as soon as the active operation snapshot is installed.
            self._set_state(started)
            handle = self._bridge.submit(operation_id, work)
        except Exception:
            state_after_failure = self._get_state()
            recovered = finish_operation(state_after_failure, operation_id)
            self._restore_after_terminal(operation_id, recovered, status="Failed to start")

            if recovered is not state_after_failure:
                self._set_state(recovered)

            raise

        active = self._get_state().active_operation

        if active is not None and active.operation_id == operation_id:
            self._active_handle = cast(OperationHandle[object], handle)

        return handle

    def cancel_active(self) -> bool:
        """Request cooperative cancellation without ending the busy UI state."""
        active = self._get_state().active_operation
        handle = self._active_handle

        if (
            active is None
            or handle is None
            or handle.operation_id != active.operation_id
        ):
            return False

        if self._cancelling_id == active.operation_id:
            return True

        handle.cancel()
        self._cancelling_id = active.operation_id

        if self.progress_dialog is not None:
            self.progress_dialog.request_cancel()
        # A Future can already be finished while Qt still has its completion
        # queued. Notify batch coordinators now so they cannot start later work
        # when that successful result is delivered.
        self.cancellation_requested.emit(active.operation_id)
        self._render_stage()

        return True

    @Slot(object)
    def _on_operation_event(self, event: object) -> None:
        # The executor emits these semantic events before the Future callback.
        # Only the bridge completion carries the typed payload a reducer needs.
        if isinstance(event, (OperationCompleted, OperationCancelled)):
            return

        operation_id: str | None = None

        if isinstance(
            event,
            (
                OperationStarted, OperationStageChanged, OperationProgress,
                FileStarted, FileStageChanged, FileCompleted, FileFailed,
                ProviderStarted, ProviderCompleted, ProviderFailed,
            ),
        ):
            operation_id = event.operation_id

        if operation_id is None or not self._is_current(operation_id):
            return

        if isinstance(event, OperationStarted):
            self._stage_text = "Starting…"
        elif isinstance(event, (OperationStageChanged, OperationProgress)):
            self._stage_text = _humanise_stage(event.stage)

        if isinstance(event, OperationProgress):
            if event.total == 0:
                self._progress_bar.setRange(0, 0)
            else:
                self._progress_bar.setRange(0, event.total)
                self._progress_bar.setValue(event.current)

        if isinstance(event, FileStarted):
            self._file = event.source_path.name
        elif isinstance(event, FileStageChanged):
            self._file = event.source_path.name
            self._stage_text = _humanise_stage(event.stage.value)
        elif isinstance(event, (FileCompleted, FileFailed)):
            self._file = ""
        elif isinstance(event, ProviderStarted):
            self._provider = event.engine_id

        self._render_stage()

        if self.progress_dialog is not None:
            if isinstance(event, ProviderFailed):
                self.progress_dialog.append_log(f"{event.engine_id}: {event.issue.code.value}")
            elif isinstance(event, (OperationStageChanged, FileStageChanged, ProviderStarted)):
                self.progress_dialog.append_log(self._stage_label.text())

    def _render_stage(self) -> None:
        context = self._file or self._provider
        message = f"{context}: {self._stage_text}" if context else self._stage_text

        if self._cancelling_id is not None:
            message = f"Cancelling… {message}"

        self._stage_label.setText(redact_sensitive_text(message))

        if self.progress_dialog is not None:
            total = self._progress_bar.maximum() if self._progress_bar.maximum() > 0 else 0
            self.progress_dialog.update_status(
                message, self._provider, self._file,
                max(0, self._progress_bar.value()), total,
            )

    def _open_progress(self, operation_id: str, kind: OperationKind) -> None:
        if self.progress_dialog is not None:
            self.progress_dialog.finish("Finished")
            self.progress_dialog.close()
            self.progress_dialog.deleteLater()
            self.progress_dialog = None

        if self._dialog_parent is None:
            return

        self._dialog_kind = kind
        self.progress_dialog = OperationProgressDialog(
            operation_id, writing=kind is OperationKind.APPLY, parent=self._dialog_parent,
        )
        self.progress_dialog.cancel_requested.connect(self.cancel_active)
        self.progress_dialog.show()

    @Slot(str, object)
    def _on_completed(self, operation_id: str, result: object) -> None:
        if operation_id in self._reduced_operations:
            return

        # A first late Apply result still reconciles irreversible disk effects.
        # Only repeat payloads are rejected; checking active ID alone is unsafe.
        self._reduced_operations.add(operation_id)
        binding = next(
            (
                candidate
                for candidate in self._bindings
                if type(result) is candidate.result_type
            ),
            None,
        )

        if binding is None:
            self._handle_reducer_failure(
                operation_id,
                TypeError(f"No reducer is registered for {type(result).__name__}"),
            )

            return

        try:
            reduced = binding.reducer(self._get_state(), result)

            if not isinstance(reduced, StateApplicationResult):
                raise TypeError("a result reducer must return StateApplicationResult")

            # Install even a STALE result. Apply reducers may conservatively mark
            # groups for rescan because the filesystem effect already happened.
            self._set_state(reduced.state)
        except Exception as error:
            LOGGER.exception("Could not reduce operation result %s", operation_id)
            self._handle_reducer_failure(operation_id, error)

            return

        # The reducer needs active-operation lineage to validate its result.
        # Clear that marker only after installing the reduced snapshot.
        finished = finish_operation(reduced.state, operation_id)
        self._restore_after_terminal(operation_id, finished)

        if finished is not reduced.state:
            self._set_state(finished)

    @Slot(str)
    def _on_cancelled(self, operation_id: str) -> None:
        if self._is_current(operation_id):
            self._finish_without_result(operation_id, status="Cancelled")

    @Slot(str, object)
    def _on_failed(self, operation_id: str, error: object) -> None:
        was_current = self._is_current(operation_id)

        if not was_current:
            return

        self._finish_without_result(operation_id, status="Failed")

        if was_current:
            self.controller_failed.emit(operation_id, error)

    def _finish_without_result(self, operation_id: str, *, status: str = "Failed") -> None:
        current = self._get_state()
        finished = finish_operation(current, operation_id)
        self._restore_after_terminal(operation_id, finished, status=status)

        if finished is not current:
            self._set_state(finished)

    def _handle_reducer_failure(self, operation_id: str, error: BaseException) -> None:
        self._finish_without_result(operation_id)
        self.controller_failed.emit(operation_id, error)

    def _enter_busy_state(self) -> None:
        self._cancelling_id = None
        self._stage_text = "Starting…"
        self._provider = ""
        self._file = ""
        self._control_eligibility = tuple(control.isEnabled() for control in self._controls)

        for control in self._controls:
            control.setEnabled(False)

        self._stage_label.setText("Starting…")
        self._progress_bar.setRange(0, 0)
        self._progress_bar.setValue(0)
        self._progress_bar.setFormat("%v / %m in this stage")

    def _restore_after_terminal(
        self,
        operation_id: str,
        state: SessionState,
        *,
        status: str = "Completed",
    ) -> None:
        handle = self._active_handle

        if handle is not None and handle.operation_id == operation_id:
            self._active_handle = None

        # A late terminal callback for old work must not dismiss the progress or
        # restore controls belonging to a newer operation already in flight.
        if state.active_operation is not None:
            return

        self._cancelling_id = None

        if self.progress_dialog is not None and self.progress_dialog.operation_id == operation_id:
            self.progress_dialog.finish(status)

            if self._dialog_kind is OperationKind.APPLY:
                # ApplyController presents the one actual results dialogue.
                self.progress_dialog.close()

        eligibility = self._control_eligibility

        if eligibility is not None:
            # Restore the temporary busy mask before publishing the idle state.
            # The view then recalculates eligibility from the new result, so a
            # previously enabled action cannot override a fresh domain block.
            for control, enabled in zip(self._controls, eligibility, strict=True):
                control.setEnabled(enabled)

            self._control_eligibility = None

        self._stage_label.setText("Idle")
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(0)

    def _is_current(self, operation_id: str) -> bool:
        active = self._get_state().active_operation

        return active is not None and active.operation_id == operation_id


def _humanise_stage(stage: str) -> str:
    return stage.replace("_", " ").strip().capitalize()
