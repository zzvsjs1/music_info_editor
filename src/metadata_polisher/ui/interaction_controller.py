"""Consistent mouse, keyboard and contextual entry points for existing commands."""

from PySide6.QtCore import QEvent, QItemSelectionModel, QModelIndex, QObject, QPoint, Qt
from PySide6.QtGui import QKeyEvent, QKeySequence, QShortcut
from PySide6.QtWidgets import QApplication, QMenu, QPushButton, QTableView, QTreeView, QWidget

from metadata_polisher.ui.main_window import MainWindow


class InteractionController(QObject):
    """Reuse button commands so shortcuts obey the same scope and busy guards."""

    def __init__(self, window: MainWindow) -> None:
        super().__init__(window)
        self._window = window
        self._shortcuts: list[QShortcut] = []

        for key, button in (("Ctrl+O", window.browse_button), ("F5", window.rescan_button),
                            ("F1", window.help_button),
                            ("Ctrl+E", window.open_review_button), ("Ctrl+L", window.find_selected_button),
                            ("Ctrl+Return", window.apply_selected_button),
                            ("Ctrl+Enter", window.apply_selected_button)):
            self._shortcut(window, key, button)

        for key, button in (("Ctrl+Z", window.undo_review_button), ("Ctrl+Return", window.review_apply_button),
                            ("F1", window.help_button),
                            ("Ctrl+Enter", window.review_apply_button),
                            ("Alt+Left", window.previous_file_button), ("Alt+Right", window.next_file_button)):
            self._shortcut(window.review_window, key, button)

        window.browse_button.setToolTip("Choose a music folder (Ctrl+O)")
        window.rescan_button.setToolTip("Rescan the library (F5), or press Enter in the folder path")
        window.open_review_button.setToolTip("Open metadata review (Ctrl+E, or double-click a file)")
        window.previous_file_button.setToolTip("Review the previous file in this album (Alt+Left)")
        window.next_file_button.setToolTip("Review the next file in this album (Alt+Right)")
        window.root_path_edit.returnPressed.connect(window.rescan_button.click)
        window.previous_file_button.clicked.connect(lambda: self.move_file(-1))
        window.next_file_button.clicked.connect(lambda: self.move_file(1))
        window.file_table_view.doubleClicked.connect(self.open_file)
        window.diff_table_view.doubleClicked.connect(self.edit_final_value)
        window.file_table_view.installEventFilter(self)
        window.diff_table_view.installEventFilter(self)

        for view in (window.file_table_view, window.diff_table_view, window.group_view):
            view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            view.customContextMenuRequested.connect(lambda point, table=view: self.show_context_menu(table, point))

        window.review_context_changed.connect(self.refresh_navigation)
        self.refresh_navigation()

    def _shortcut(self, owner: QWidget, key: str, button: QPushButton) -> None:
        shortcut = QShortcut(QKeySequence(key), owner)
        shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        shortcut.activated.connect(lambda: self._click(button))
        self._shortcuts.append(shortcut)

    def _click(self, button: QPushButton) -> None:
        # A manual editor or final confirmation is its own keyboard context.
        # In particular, review shortcuts must never confirm a disk write.
        if QApplication.activeModalWidget() is None:
            button.click()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if isinstance(event, QKeyEvent) and event.type() == QEvent.Type.KeyPress:
            if event.modifiers() != Qt.KeyboardModifier.NoModifier:
                return super().eventFilter(watched, event)

            if watched is self._window.file_table_view and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self.open_file(self._window.file_table_view.currentIndex(), keyboard=True)
                return True

            if watched is self._window.diff_table_view and event.key() == Qt.Key.Key_F2:
                self._window.manual_value_button.click()
                return True

        return super().eventFilter(watched, event)

    def open_file(self, index: QModelIndex, *, keyboard: bool = False) -> None:
        window = self._window

        if not index.isValid() or (index.column() == 0 and not keyboard) or not window.selected_file_ids():
            return

        # Activation explicitly reviews highlighted tracks even when an earlier
        # bulk review left the scope set to the whole library.
        window.review_scope_combo.setCurrentIndex(window.review_scope_combo.findData("selected"))
        window.open_metadata_review()

    def edit_final_value(self, index: QModelIndex) -> None:
        if index.isValid() and index.column() == 4:
            self._window.manual_value_button.click()

    def refresh_navigation(self) -> None:
        window = self._window
        rows = window.file_table_view.selectionModel().selectedRows()
        # Previous/Next has a defined neighbour only for a single highlighted
        # file; broader review scopes must not silently shrink to a focused row.
        individual = (window.session_state.active_operation is None
                      and window.review_scope_combo.currentData() == "selected" and len(rows) == 1)
        row = rows[0].row() if individual else -1
        window.previous_file_button.setEnabled(individual and row > 0)
        window.next_file_button.setEnabled(individual and row + 1 < window.file_model.rowCount())

    def move_file(self, offset: int) -> None:
        window = self._window
        button = window.previous_file_button if offset < 0 else window.next_file_button

        if not button.isEnabled():
            return

        row = window.file_table_view.selectionModel().selectedRows()[0].row() + offset
        window.file_table_view.selectRow(row)
        window.file_table_view.scrollTo(window.file_model.index(row, 0))

    def show_context_menu(self, view: QTableView | QTreeView, point: QPoint) -> None:
        index = view.indexAt(point)

        if not index.isValid():
            return

        # Right-click keeps an existing multi-selection. A new row becomes the
        # explicit target before any command is offered.
        selection = view.selectionModel()

        if not selection.isRowSelected(index.row(), index.parent()):
            selection.setCurrentIndex(index, QItemSelectionModel.SelectionFlag.ClearAndSelect
                                      | QItemSelectionModel.SelectionFlag.Rows)

        window = self._window
        buttons: tuple[QPushButton, ...]

        if view is window.file_table_view:
            buttons = (window.open_review_button, window.rename_files_button,
                       window.include_selected_button, window.exclude_selected_button)
            count = len(window.selected_file_ids())
            label = f"{count} selected files"
        elif view is window.diff_table_view:
            buttons = (window.manual_value_button, window.keep_existing_button, window.use_proposed_button,
                       window.clear_value_button, window.undo_review_button)
            label = f"{len(window.selected_fields())} fields · {len(window.review_target_file_ids())} files"
        else:
            buttons = (window.find_selected_button, window.choose_candidate_button, window.track_mapping_button,
                       window.edit_search_button, window.split_group_button, window.merge_groups_button,
                       window.disc_override_button)
            label = f"{len(window.selected_group_ids())} selected albums"

        menu = QMenu(view)
        menu.addSection(label)
        # A menu runs a nested event loop. Do not apply its captured context if
        # a worker result replaces the session while the user is choosing.
        snapshot = window.session_state

        for button in buttons:
            shortcut = {
                window.open_review_button: "Ctrl+E", window.find_selected_button: "Ctrl+L",
                window.manual_value_button: "F2", window.undo_review_button: "Ctrl+Z",
            }.get(button)
            action = menu.addAction(button.text() + (f"\t{shortcut}" if shortcut else ""))
            action.setEnabled(button.isEnabled())

            if view is window.file_table_view and button is window.open_review_button:
                action.triggered.connect(lambda: self.open_file(index, keyboard=True)
                                         if window.session_state is snapshot else None)
            else:
                action.triggered.connect(lambda _checked=False, target=button: target.click()
                                         if window.session_state is snapshot else None)

        menu.exec(view.viewport().mapToGlobal(point))
