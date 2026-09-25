"""Allow-listed operation evidence and redacted, copyable QML diagnostics."""

import json
import logging
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, QUrl, Slot
from PySide6.QtGui import QDesktopServices, QGuiApplication

from metadata_polisher.application.apply import ApplyBatchResult
from metadata_polisher.application.lookup import GroupLookupResult
from metadata_polisher.infrastructure.diagnostic_trace import DetailedTraceSink
from metadata_polisher.infrastructure.diagnostics import build_group_diagnostic_summary, sanitise_diagnostic_data
from metadata_polisher.infrastructure.logging_setup import configure_logging, redact_sensitive_text
from metadata_polisher.matching.release_scoring import MatchEvidence

if TYPE_CHECKING:
    from metadata_polisher.ui.quick.backend import QuickBackend


class QuickDiagnostics(QObject):
    """Project redacted evidence without making provider calls or reading media."""

    def __init__(self, host: QuickBackend) -> None:
        super().__init__(host)
        self.host = host
        self.logger = logging.getLogger(__name__)
        self.trace = DetailedTraceSink(self.logger, enabled=False)
        self.configure()
        host.bridge.operation_event.connect(self._on_event)
        host.bridge.failed.connect(self.failed)
        host.bridge.completed.connect(self.completed)
        host.bridge.cancelled.connect(self.cancelled)

    def configure(self) -> None:
        if self.host.app_dir is not None:
            enabled = self.host.app_settings.diagnostics.detailed_tracing
            self.logger = configure_logging(self.host.app_dir / "logs", detailed_tracing=enabled)
            self.trace = DetailedTraceSink(self.logger, enabled=enabled)

    @Slot(object)
    def _on_event(self, event: object) -> None:
        operation_id = getattr(event, "operation_id", None)

        if isinstance(operation_id, str):
            # An explicit allow-list keeps future event fields (including any
            # credentials or request objects) out of traces by default.
            details = {
                name: getattr(event, name)
                for name in ("file_id", "engine_id", "source_id", "stage", "current", "total", "status")
                if hasattr(event, name)
            }
            self.trace.record(operation_id, type(event).__name__, details)

    @Slot(str, object)
    def failed(self, operation_id: str, error: object) -> None:
        self.logger.error("Operation %s failed: %s", operation_id, redact_sensitive_text(str(error)))

    @Slot(str, object)
    def completed(self, operation_id: str, result: object) -> None:
        self.logger.info("Operation %s completed: %s", operation_id, type(result).__name__)

        if not self.trace.enabled:
            return

        if isinstance(result, GroupLookupResult):
            group = next(
                (item for item in self.host.session_state.groups if item.group.group_id == result.group_id), None
            )

            # The coordinator reduces first. Attribute evidence to the result's
            # group, and record it only if that exact lookup receipt survived.
            # A later selection or an obsolete completion is not new evidence.
            if group is not None and group.candidate_lookup == result.candidate_lookup:
                self.trace.record(operation_id, "lookup_result", build_group_diagnostic_summary(group))

        elif isinstance(result, ApplyBatchResult):
            # A transaction receipt remains authoritative even when its session
            # lineage is old. Retain each final path and recovery reason code.
            self.trace.record(
                operation_id,
                "apply_result",
                {
                    "status": result.status.value,
                    "files": [
                        {
                            "file_id": item.file_id,
                            "status": item.status.value,
                            "final_path": str(item.final_path),
                            "reason_codes": [code.value for code in item.reason_codes],
                        }
                        for group in result.groups
                        for item in group.files
                    ],
                    "report_status": result.report_result.status.value,
                },
            )

    @Slot(str)
    def cancelled(self, operation_id: str) -> None:
        self.logger.info("Operation %s cancelled", operation_id)

    def summary(self) -> str:
        group = self.host._group()
        document = build_group_diagnostic_summary(group) if group else {"groups": len(self.host.session_state.groups)}
        return json.dumps(sanitise_diagnostic_data(document), ensure_ascii=False, sort_keys=True, indent=2)

    def _evidence(self, track: bool) -> tuple[MatchEvidence, ...]:
        group = self.host._group()
        evidence: tuple[MatchEvidence, ...] = ()

        if group is not None:
            if track and group.effective_track_mapping is not None:
                mapping = group.effective_track_mapping
                selected = self.host._selected
                evidence = mapping.evidence + tuple(
                    item
                    for pair in mapping.mappings
                    if not selected or pair.local_file_id in selected
                    for item in pair.evidence
                )
            elif not track and group.selected_release is not None and group.release_ranking is not None:
                evidence = next(
                    (
                        entry.result.evidence
                        for entry in group.release_ranking.entries
                        if entry.identity == group.selected_release.identity
                    ),
                    (),
                )

        return evidence

    def evidence_rows(self, track: bool) -> list[dict[str, str]]:
        # Keep zero contributions as evidence too; selecting only positive
        # reasons would give the user a misleading account of an ambiguous match.
        return [
            {
                "reason": redact_sensitive_text(item.code),
                "contribution": f"{item.contribution:g}",
                "detail": redact_sensitive_text(item.detail),
            }
            for item in self._evidence(track)
        ]

    def explanation(self, track: bool) -> str:
        return "\n\n".join(
            f"{row['reason']} · {row['contribution']}\n{row['detail']}" for row in self.evidence_rows(track)
        )

    def copy(self) -> None:
        QGuiApplication.clipboard().setText(self.summary())

    def open_logs(self) -> None:
        if self.host.app_dir is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.host.app_dir / "logs")))
