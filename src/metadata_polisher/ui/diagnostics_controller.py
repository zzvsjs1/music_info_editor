"""Compose portable logging and expose immutable operation evidence to the UI."""

from pathlib import Path

from PySide6.QtCore import QObject, Slot

from metadata_polisher.application.apply import ApplyBatchResult
from metadata_polisher.application.lookup import GroupLookupResult
from metadata_polisher.infrastructure.diagnostic_trace import DetailedTraceSink
from metadata_polisher.infrastructure.diagnostics import build_group_diagnostic_summary
from metadata_polisher.infrastructure.logging_setup import configure_logging
from metadata_polisher.infrastructure.settings import AppSettings
from metadata_polisher.matching.release_scoring import MatchEvidence
from metadata_polisher.session.state import GroupSelection
from metadata_polisher.ui.dialogs.diagnostics_dialog import DiagnosticsDialog
from metadata_polisher.ui.main_window import MainWindow


class DiagnosticsController(QObject):
    """Record allow-listed semantic events; copied summaries use the same redactor."""

    def __init__(self, window: MainWindow, app_dir: Path) -> None:
        super().__init__(window)
        self._window = window
        self._logs_dir = app_dir / "logs"
        assert window.library_controller is not None
        enabled = window.library_controller.settings.diagnostics.detailed_tracing
        self._logger = configure_logging(self._logs_dir, detailed_tracing=enabled)
        self._trace = DetailedTraceSink(self._logger, enabled=enabled)
        self.dialog: DiagnosticsDialog | None = None
        window.diagnostics_button.clicked.connect(self.show_diagnostics)
        assert window.operation_bridge is not None
        window.operation_bridge.operation_event.connect(self._event)
        window.operation_bridge.completed.connect(self._completed)
        window.operation_bridge.cancelled.connect(self._cancelled)
        window.operation_bridge.failed.connect(self._failed)
        assert window.settings_controller is not None
        window.settings_controller.settings_changed.connect(self._settings_changed)

    @Slot(object)
    def _settings_changed(self, settings: object) -> None:
        if isinstance(settings, AppSettings):
            enabled = settings.diagnostics.detailed_tracing
            self._logger = configure_logging(self._logs_dir, detailed_tracing=enabled)
            self._trace.enabled = enabled

    @Slot(object)
    def _event(self, event: object) -> None:
        operation_id = getattr(event, "operation_id", None)

        if not isinstance(operation_id, str):
            return

        # Avoid serialising arbitrary provider objects or exception internals.
        # These fields belong to the application's explicit semantic event API.
        details = {name: getattr(event, name) for name in
                   ("file_id", "engine_id", "source_id", "stage", "current", "total", "status")
                   if hasattr(event, name)}
        self._trace.record(operation_id, type(event).__name__, details)

    @Slot(str, object)
    def _completed(self, operation_id: str, result: object) -> None:
        self._logger.info("Operation %s completed: %s", operation_id, type(result).__name__)

        if not self._trace.enabled:
            return

        if isinstance(result, GroupLookupResult):
            group = next((item for item in self._window.session_state.groups
                          if item.group.group_id == result.group_id), None)

            if group is not None and group.candidate_lookup == result.candidate_lookup:
                self._trace.record(operation_id, "lookup_result", build_group_diagnostic_summary(group))
        elif isinstance(result, ApplyBatchResult):
            self._trace.record(operation_id, "apply_result", {
                "status": result.status.value,
                "files": [{"file_id": item.file_id, "status": item.status.value,
                           "final_path": str(item.final_path),
                           "reason_codes": [code.value for code in item.reason_codes]}
                          for group in result.groups for item in group.files],
                "report_status": result.report_result.status.value,
            })

    @Slot(str)
    def _cancelled(self, operation_id: str) -> None:
        self._logger.info("Operation %s cancelled", operation_id)

    @Slot(str, object)
    def _failed(self, operation_id: str, error: object) -> None:
        self._logger.error("Operation %s failed: %s", operation_id, error)

    @Slot()
    def show_diagnostics(self) -> None:
        state = self._window.session_state
        group = next((item for item in state.groups if isinstance(state.selection, GroupSelection)
                      and item.group.group_id == state.selection.group_id), None)
        document = build_group_diagnostic_summary(group) if group is not None else {"groups": len(state.groups)}
        release_evidence: tuple[MatchEvidence, ...] = ()
        track_evidence: tuple[MatchEvidence, ...] = ()

        if group is not None:
            if group.release_ranking is not None and group.selected_release is not None:
                # Explain the chosen release/medium identity, which need not be
                # the first entry in the ranking after an explicit user choice.
                release_evidence = next((entry.result.evidence for entry in group.release_ranking.entries
                                         if entry.identity == group.selected_release.identity), ())

            mapping = group.effective_track_mapping

            if mapping is not None:
                # Include alignment-wide reasons as well as the focused track's
                # pair evidence; either alone can omit why an assignment is weak.
                file_id = self._window.current_file_id()
                track_evidence = mapping.evidence + tuple(item for pair in mapping.mappings
                                                          if file_id is None or pair.local_file_id == file_id
                                                          for item in pair.evidence)

        if self.dialog is not None:
            self.dialog.close()

        self.dialog = DiagnosticsDialog(document, self._logs_dir, self._window,
                                        release_evidence=release_evidence, track_evidence=track_evidence)
        self.dialog.show()
