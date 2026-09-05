"""Read-only presentation of deterministic release-scoring evidence."""

from collections.abc import Sequence

from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QPlainTextEdit,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from metadata_polisher.matching.release_scoring import MatchEvidence
from metadata_polisher.ui.layout import configure_columns, fit_initial_size


class MatchExplanationDialog(QDialog):
    """Show existing reason codes and contributions without recomputing scores."""

    def __init__(self, evidence: Sequence[MatchEvidence], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Why this match?")
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("The score is a deterministic ranking, not a probability.", self))
        self.table = QTableView(self)
        self.table.setObjectName("matchEvidenceTable")
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        model = QStandardItemModel(0, 3, self.table)
        model.setHorizontalHeaderLabels(("Reason", "Contribution", "Detail"))

        for item in evidence:
            # Display the supplied contribution, including zero-valued reasons.
            # Omitting those rows would conceal contradictions and unknown data.
            cells = [
                QStandardItem(item.code),
                QStandardItem(f"{item.contribution:g}"),
                QStandardItem(item.detail),
            ]

            for cell in cells:
                cell.setEditable(False)
                cell.setToolTip(cell.text())

            model.appendRow(cells)

        self.table.setModel(model)
        configure_columns(self.table, (210, 95, 420))
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table)
        self.details = QPlainTextEdit(self)
        self.details.setReadOnly(True)
        self.details.setPlaceholderText("Select a reason to read its full explanation.")
        self.details.setMaximumHeight(self.fontMetrics().lineSpacing() * 7)
        # The lower text area exposes the full supplied reason when a table cell
        # is elided; it reads evidence and never reruns the scoring algorithm.
        self.table.selectionModel().currentChanged.connect(
            lambda index, previous: self.details.setPlainText(str(index.siblingAtColumn(2).data() or "")),
        )
        layout.addWidget(self.details)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        fit_initial_size(self, 880, 600)
