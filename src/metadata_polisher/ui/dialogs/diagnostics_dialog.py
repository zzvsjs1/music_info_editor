"""Read-only diagnostics, copy action and explanations over retained evidence."""

import json
from collections.abc import Mapping, Sequence
from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from metadata_polisher.infrastructure.diagnostics import sanitise_diagnostic_data
from metadata_polisher.matching.release_scoring import MatchEvidence
from metadata_polisher.ui.dialogs.match_explanation_dialog import MatchExplanationDialog


class DiagnosticsDialog(QDialog):
    def __init__(
        self,
        document: Mapping[str, object],
        logs_dir: Path,
        parent: QWidget | None = None,
        *,
        release_evidence: Sequence[MatchEvidence] = (),
        track_evidence: Sequence[MatchEvidence] = (),
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Diagnostic summary")
        self.resize(950, 650)
        self.explanation_dialog: MatchExplanationDialog | None = None
        layout = QVBoxLayout(self)
        self.summary_edit = QPlainTextEdit(self)
        self.summary_edit.setReadOnly(True)
        # Redact before rendering, so selecting text and Copy both expose the
        # same safe document rather than relying on clipboard-only filtering.
        self.summary_edit.setPlainText(json.dumps(sanitise_diagnostic_data(document), ensure_ascii=False,
                                                sort_keys=True, indent=2))
        layout.addWidget(self.summary_edit)
        actions = QHBoxLayout()
        self.copy_button = QPushButton("Copy diagnostic summary", self)
        self.open_logs_button = QPushButton("Open log folder", self)
        self.why_match_button = QPushButton("Why this match?", self)
        self.why_track_mapping_button = QPushButton("Why this track mapping?", self)

        for button in (self.copy_button, self.open_logs_button, self.why_match_button, self.why_track_mapping_button):
            actions.addWidget(button)

        layout.addLayout(actions)
        self.copy_button.clicked.connect(lambda: QApplication.clipboard().setText(self.summary_edit.toPlainText()))
        self.open_logs_button.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(logs_dir))))
        self.why_match_button.setEnabled(bool(release_evidence))
        self.why_track_mapping_button.setEnabled(bool(track_evidence))
        self.why_match_button.clicked.connect(lambda: self._show_explanation(release_evidence, "Why this match?"))
        self.why_track_mapping_button.clicked.connect(
            lambda: self._show_explanation(track_evidence, "Why this track mapping?")
        )
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _show_explanation(self, evidence: Sequence[MatchEvidence], title: str) -> None:
        if self.explanation_dialog is not None:
            self.explanation_dialog.close()

        dialog = MatchExplanationDialog(evidence, self)
        dialog.setWindowTitle(title)
        self.explanation_dialog = dialog
        dialog.show()
