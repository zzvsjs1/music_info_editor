"""Stage explicit filename choices before opening the shared write confirmation."""

from PySide6.QtCore import Qt
from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from metadata_polisher.application.changes import ChangeIssueSeverity, RenameDecision
from metadata_polisher.infrastructure.settings import RenameSettings
from metadata_polisher.session.review_editing import apply_rename_choices
from metadata_polisher.session.state import SessionState
from metadata_polisher.ui.layout import fit_initial_size


class RenameFilesDialog(QDialog):
    """Preview a private immutable draft; Cancel never changes the live session."""

    def __init__(self, state: SessionState, file_ids: tuple[str, ...], settings: RenameSettings,
                 parent: QWidget) -> None:
        super().__init__(parent)
        self.setWindowTitle("Rename files")
        self.setObjectName("renameFilesDialog")
        self._original = state
        self._settings = settings
        self._file_ids = file_ids
        self._draft = state
        self._selected_ids: tuple[str, ...] = ()
        sources = {source.file_id: source for group in state.groups for source in group.group.files}
        if not file_ids or not set(file_ids).issubset(sources):
            raise ValueError("Choose supported music files from the scanned library.")

        layout = QVBoxLayout(self)
        notice = QLabel(
            f"Choose filenames to prepare for {len(file_ids)} files. "
            "The next screen reviews the write batch, including pending metadata changes.", self,
        )
        notice.setWordWrap(True)
        layout.addWidget(notice)
        template = QLabel(f"Filename template: {settings.template}\nChange the template in Settings.", self)
        template.setTextFormat(Qt.TextFormat.PlainText)
        template.setWordWrap(True)
        layout.addWidget(template)
        self.table = QTableView(self)
        self.table.setObjectName("renameFilesTable")
        self._model = QStandardItemModel(0, 4, self)
        self._model.setHorizontalHeaderLabels(("Rename", "Current filename", "Preview", "Validation"))

        for file_id in file_ids:
            source = sources[file_id]
            cells = [QStandardItem(text) for text in ("", source.path.name, "", "")]

            for cell in cells:
                cell.setEditable(False)
                cell.setData(file_id, Qt.ItemDataRole.UserRole)

            cells[0].setCheckable(True)
            cells[0].setCheckState(Qt.CheckState.Checked)
            cells[1].setToolTip(str(source.path))
            self._model.appendRow(cells)

        self.table.setModel(self._model)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.setColumnWidth(0, 65)
        self.table.setColumnWidth(1, 230)
        self.table.setColumnWidth(2, 290)
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table)
        self.status_label = QLabel(self)
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel, self)
        self.continue_button = buttons.addButton(
            "Include chosen files and review changes…", QDialogButtonBox.ButtonRole.AcceptRole,
        )

        for button in buttons.findChildren(QPushButton):
            button.setAutoDefault(False)

        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._model.itemChanged.connect(self._refresh)
        self._refresh()
        fit_initial_size(self, 1050, 600)

    def _refresh(self) -> None:
        self._selected_ids = tuple(file_id for row, file_id in enumerate(self._file_ids)
                                   if self._model.item(row, 0).checkState() == Qt.CheckState.Checked)
        # Rebuild every checkbox combination from the captured session, so
        # toggling repeatedly does not accumulate intermediate undo actions.
        self._draft = self._original
        blocked: dict[str, str] = {}

        # Unticking an existing rename must remove that earlier intent as well
        # as excluding it from this dialogue's newly chosen filenames.
        old_renames = {item.file_id for group in self._original.groups for item in group.reviewed_files
                       if item.change_set and item.change_set.rename_decision is RenameDecision.APPLY_RENAME}
        keep_ids = tuple(file_id for file_id in self._file_ids
                         if file_id not in self._selected_ids and file_id in old_renames)

        if self._selected_ids or keep_ids:
            result = apply_rename_choices(self._original, self._selected_ids, keep_ids, self._settings)
            self._draft = result.state
            blocked = {item.file_id: item.reason for item in result.blocked}

        changes = {item.file_id: item.change_set for group in self._draft.groups for item in group.reviewed_files}
        selected = set(self._selected_ids)
        # Updating derived preview/status cells emits itemChanged too. Suppress
        # those notifications to avoid recursively rebuilding the same draft.
        self._model.blockSignals(True)

        try:
            for row, file_id in enumerate(self._file_ids):
                change = changes.get(file_id)
                preview = change.rename_preview if change else None
                name = preview.new_path.name if preview else "—"
                issues = change.validation.issues if change and file_id in selected else ()
                errors = [issue.message for issue in issues if issue.severity is ChangeIssueSeverity.BLOCKING]

                if errors:
                    blocked[file_id] = "; ".join(errors)

                status = blocked.get(file_id) or ("; ".join(issue.message for issue in issues)
                                                 if issues else "Ready")

                if file_id not in selected:
                    status = blocked.get(file_id) or "Keep filename"

                self._model.item(row, 2).setText(name)
                self._model.item(row, 2).setToolTip(name)
                self._model.item(row, 3).setText(status)
                self._model.item(row, 3).setToolTip(status)
        finally:
            self._model.blockSignals(False)

        count = len(self._selected_ids)
        self.status_label.setText(
            f"{count} files chosen · {len(keep_ids)} previous renames removed · {len(blocked)} blocked. "
            + ("Correct or untick blocked files to continue." if blocked else "No files have been changed."),
        )
        self.continue_button.setEnabled(bool(count or keep_ids) and not blocked)

    def draft_state(self) -> SessionState:
        return self._draft

    def chosen_file_ids(self) -> tuple[str, ...]:
        return self._selected_ids

    def accept(self) -> None:
        # Acceptance exposes a draft to the caller; only the later shared Apply
        # summary can authorise a background request that writes audio files.
        if self.continue_button.isEnabled():
            super().accept()
