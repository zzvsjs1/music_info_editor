"""Inspectable filename intent for every file in an explicitly selected scope."""

from collections.abc import Sequence

from PySide6.QtCore import Qt
from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QHeaderView, QLabel, QTableView, QVBoxLayout, QWidget

from metadata_polisher.application.changes import RenameDecision
from metadata_polisher.domain.media import LocalMediaFile
from metadata_polisher.session.state import ReviewedFileState
from metadata_polisher.ui.layout import fit_initial_size


class RenamePreviewDialog(QDialog):
    """Read-only session previews; opening this dialogue never changes review intent."""

    def __init__(
        self,
        files: Sequence[tuple[LocalMediaFile, ReviewedFileState | None]],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Filename previews")
        layout = QVBoxLayout(self)
        notice = QLabel(f"{len(files)} files in this review scope. No files are being changed.", self)
        notice.setWordWrap(True)
        layout.addWidget(notice)
        self.table = QTableView(self)
        self.table.setObjectName("renamePreviewTable")
        model = QStandardItemModel(0, 4, self.table)
        model.setHorizontalHeaderLabels(("Current filename", "Proposed filename", "Decision", "Validation"))

        # Derive each destination from its own reviewed ChangeSet: a multi-file
        # preview cannot use the first track's title or number for every row.
        for source, reviewed in files:
            changes = reviewed.change_set if reviewed else None
            preview = changes.rename_preview if changes else None
            included = changes is not None and changes.rename_decision is RenameDecision.APPLY_RENAME
            issues = "; ".join(issue.message for issue in changes.validation.issues) if changes else "No review yet"
            values = (
                source.path.name, preview.new_path.name if preview else "No filename change",
                "Include rename" if included else "Keep filename", issues or "Ready",
            )
            cells = [QStandardItem(value) for value in values]

            for cell in cells:
                cell.setEditable(False)
                cell.setToolTip(cell.text())
                cell.setData(source.file_id, Qt.ItemDataRole.UserRole)

            model.appendRow(cells)

        self.table.setModel(model)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.setColumnWidth(0, 240)
        self.table.setColumnWidth(1, 300)
        self.table.setColumnWidth(2, 130)
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        fit_initial_size(self, 1000, 600)
