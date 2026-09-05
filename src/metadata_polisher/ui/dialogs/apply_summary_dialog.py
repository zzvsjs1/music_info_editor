"""Final native confirmation of immutable, reviewed Apply counts."""

from PySide6.QtCore import Qt
from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QLabel,
    QScrollArea,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from metadata_polisher.application.apply_summary import ApplySummary, FieldChangeCount
from metadata_polisher.application.changes import FileChangeSet, RenameDecision
from metadata_polisher.domain.media import LocalMediaFile
from metadata_polisher.ui.layout import fit_initial_size
from metadata_polisher.ui.models.diff_model import FIELD_LABELS


def _count_text(count: int, singular: str, plural: str) -> str:
    return f"{count} {singular if count == 1 else plural}"


def _field_count_lines(counts: tuple[FieldChangeCount, ...]) -> tuple[str, ...]:
    return tuple(f"    {FIELD_LABELS[item.field]}: {item.count}" for item in counts)


class ApplySummaryDialog(QDialog):
    """Display precomputed decisions and refuse confirmation when they are blocked."""

    def __init__(
        self, summary: ApplySummary, parent: QWidget | None = None,
        *, files: tuple[tuple[LocalMediaFile, FileChangeSet], ...] = (),
    ) -> None:
        super().__init__(parent)

        if not isinstance(summary, ApplySummary):
            raise TypeError("summary must be an ApplySummary")

        self._summary = summary
        self.setWindowTitle("Review Apply changes")
        layout = QVBoxLayout(self)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        content = QWidget(scroll)
        content_layout = QVBoxLayout(content)
        # Selected files include no-ops; files with changes count planned writes.
        # Keeping both counts prevents inclusion from being mistaken for a change.
        lines = (
            _count_text(summary.file_count, "file selected", "files selected"),
            _count_text(summary.write_file_count, "file with changes", "files with changes"),
            "",
            _count_text(summary.addition_count, "value added", "values added"),
            *_field_count_lines(summary.additions),
            _count_text(summary.replacement_count, "existing value replaced", "existing values replaced"),
            *_field_count_lines(summary.replacements),
            _count_text(summary.removal_count, "value cleared", "values cleared"),
            *_field_count_lines(summary.removals),
            _count_text(summary.rename_count, "filename to rename", "filenames to rename"),
            _count_text(len(summary.blocking_issues), "blocking conflict", "blocking conflicts"),
            "",
            f"Permanent backup: {'On' if summary.backup_enabled else 'Off'}",
            f"JSON report: {'On' if summary.report_enabled else 'Off'}",
        )
        self.summary_label = QLabel("\n".join(lines), content)
        self.summary_label.setTextFormat(Qt.TextFormat.PlainText)
        self.summary_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.summary_label.setWordWrap(True)
        content_layout.addWidget(self.summary_label)
        issue_lines = tuple(
            f"{issue.file_id} — {issue.code.value}"
            f"{f' ({FIELD_LABELS[issue.field]})' if issue.field is not None else ''}: {issue.message}"
            for issue in summary.blocking_issues
        )
        issues_text = "\n".join(issue_lines)

        if not issues_text and not summary.can_apply:
            issues_text = "No changes are selected for Apply."

        self.issues_label = QLabel(issues_text, content)
        self.issues_label.setTextFormat(Qt.TextFormat.PlainText)
        self.issues_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.issues_label.setWordWrap(True)
        content_layout.addWidget(self.issues_label)
        content_layout.addStretch()
        scroll.setWidget(content)
        layout.addWidget(scroll)

        if files:
            self.file_table = QTableView(self)
            self.file_table.setObjectName("applySummaryFilesTable")
            model = QStandardItemModel(0, 4, self.file_table)
            model.setHorizontalHeaderLabels(("File", "Tag fields", "Filename decision", "Final filename"))

            for source, changes in files:
                # A filename preview is meaningful only for its own source. Fail
                # before showing a misleading confirmation for mismatched data.
                if source.file_id != changes.file_id:
                    raise ValueError("The confirmation changes must belong to their source file.")

                rename = changes.rename_decision is RenameDecision.APPLY_RENAME and changes.rename_preview is not None
                final_name = (
                    changes.rename_preview.new_path.name if rename and changes.rename_preview else source.path.name
                )
                values = (
                    source.path.name, str(len(changes.metadata_changes)),
                    "Include rename" if rename else "Keep filename", final_name,
                )
                cells = [QStandardItem(value) for value in values]

                for cell in cells:
                    cell.setEditable(False)
                    cell.setToolTip(cell.text())
                    cell.setData(source.file_id, Qt.ItemDataRole.UserRole)

                fields = ", ".join(FIELD_LABELS[change.field] for change in changes.metadata_changes)
                cells[1].setToolTip(fields or "No tag changes")
                model.appendRow(cells)

            self.file_table.setModel(model)
            self.file_table.verticalHeader().setVisible(False)
            self.file_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
            self.file_table.setColumnWidth(0, 230)
            self.file_table.setColumnWidth(1, 70)
            self.file_table.setColumnWidth(2, 130)
            self.file_table.horizontalHeader().setStretchLastSection(True)
            layout.addWidget(self.file_table)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Apply | QDialogButtonBox.StandardButton.Cancel, self,
        )
        self.apply_button = buttons.button(QDialogButtonBox.StandardButton.Apply)
        self.apply_button.setText("Apply changes")
        self.apply_button.setEnabled(summary.can_apply)
        self.apply_button.clicked.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        fit_initial_size(self, 700, 620)

    def accept(self) -> None:
        """Keep the same guard for button, keyboard and programmatic confirmation."""
        if self._summary.can_apply:
            super().accept()
