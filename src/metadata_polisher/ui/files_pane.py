"""File-table presentation with separate highlighting and write-batch controls."""

from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from metadata_polisher.ui.layout import configure_columns
from metadata_polisher.ui.models import FileTableModel


class FilesPane(QWidget):
    """Present a supplied file model while controllers own command behaviour."""

    def __init__(self, model: FileTableModel, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self.include_selected_button = self._button("Add to write batch", "includeSelectedButton")
        self.exclude_selected_button = self._button("Remove from batch", "excludeSelectedButton")
        self.select_all_files_button = self._button("Select all", "selectAllFilesButton")
        self.select_all_files_button.setToolTip("Select all files in the displayed group (Ctrl+A)")
        self.clear_file_selection_button = self._button("Clear selection", "clearFileSelectionButton")
        self.clear_file_selection_button.setToolTip("Clear highlighted files (Ctrl+Shift+A)")
        self.open_review_button = self._button("Metadata review…", "openReviewButton")
        self.rename_files_button = self._button("Rename files…", "renameFilesButton")

        self.selection_scope_label = QLabel("0 highlighted · 0 included", self)
        self.selection_scope_label.setWordWrap(True)
        self.file_guidance_label = QLabel("Choose a folder to scan your music library.", self)
        self.file_guidance_label.setWordWrap(True)

        self.file_table_view = QTableView(self)
        self.file_table_view.setObjectName("fileTableView")
        self.file_table_view.setModel(model)
        self.file_table_view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.file_table_view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Files / tracks", self))
        selection = QHBoxLayout()
        selection.addWidget(self.select_all_files_button)
        selection.addWidget(self.clear_file_selection_button)
        selection.addStretch(1)
        selection.addWidget(self.rename_files_button)
        selection.addWidget(self.open_review_button)
        layout.addLayout(selection)
        layout.addWidget(self.file_guidance_label)
        layout.addWidget(self.file_table_view, 1)

        # Keep write membership visibly separate from the highlighted review
        # targets. The pane only exposes controls; session commands connect them.
        inclusion = QHBoxLayout()
        inclusion.addWidget(self.selection_scope_label, 1)
        inclusion.addWidget(self.include_selected_button)
        inclusion.addWidget(self.exclude_selected_button)
        layout.addLayout(inclusion)

    def _button(self, text: str, object_name: str) -> QPushButton:
        button = QPushButton(text, self)
        button.setObjectName(object_name)

        return button

    def reset_layout(self) -> None:
        """Restore default columns when startup or an explicit reset requests it."""
        configure_columns(self.file_table_view, (56, 100, 260, 65, 65, 240, 160, 160, 70, 65, 90, 100))

        for column in (6, 7, 10, 11):
            self.file_table_view.setColumnHidden(column, True)

        # Reserve eight tracks, the header and the horizontal scrollbar using
        # the current Qt metrics, so display scaling retains the same row budget.
        self.file_table_view.setMinimumHeight(
            8 * self.file_table_view.verticalHeader().defaultSectionSize()
            + self.file_table_view.horizontalHeader().height()
            + self.file_table_view.horizontalScrollBar().sizeHint().height()
            + 2 * self.file_table_view.frameWidth(),
        )
