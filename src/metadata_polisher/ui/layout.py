"""Portable, screen-bounded layout helpers for the native Qt views."""

from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, cast

from PySide6.QtCore import QAbstractItemModel, QEvent, QObject, QPoint, QSize, Qt, Signal
from PySide6.QtGui import QResizeEvent
from PySide6.QtWidgets import (
    QDialog,
    QHeaderView,
    QLabel,
    QMenu,
    QSizePolicy,
    QTableView,
    QTreeView,
    QWidget,
)

from metadata_polisher.infrastructure.settings import load_settings, save_settings

if TYPE_CHECKING:
    from metadata_polisher.ui.main_window import MainWindow


class ElidedLabel(QLabel):
    """Keep one readable line without letting a filename set window minimums."""

    def __init__(self, text: str, parent: QWidget) -> None:
        super().__init__(parent)
        self._full_text = text
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setText(text)

    def setText(self, text: str) -> None:
        self._full_text = text
        self.setToolTip(text)
        self.setAccessibleName(text)
        # Elide only the painted label; the tooltip and accessibility name keep
        # the full path/title available even in a narrow review window.
        super().setText(self.fontMetrics().elidedText(text, Qt.TextElideMode.ElideMiddle, max(40, self.width())))

    def minimumSizeHint(self) -> QSize:
        return QSize(0, self.fontMetrics().lineSpacing())

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self.setText(self._full_text)


class StatusLabel(QLabel):
    """Broadcast workflow feedback so both workspaces show the same outcome."""

    text_changed = Signal(str)

    def setText(self, text: str) -> None:
        super().setText(text)
        self.text_changed.emit(text)

    def clear(self) -> None:
        self.setText("")


def fit_initial_size(widget: QWidget, width: int, height: int) -> None:
    """Use logical screen dimensions and allow the layout to shrink with fonts."""
    available = widget.screen().availableGeometry()
    widget.resize(max(240, min(width, available.width() - 32)),
                  max(200, min(height, available.height() - 64)))

    if isinstance(widget, QDialog):
        ancestor = widget.parentWidget()

        while ancestor is not None:
            preferences = ancestor.findChild(
                LayoutPreferences, "portableLayout", Qt.FindChildOption.FindDirectChildrenOnly,
            )

            if preferences is not None:
                preferences.watch_dialog(widget)
                break

            ancestor = ancestor.parentWidget()


def configure_columns(view: QTableView | QTreeView, widths: Sequence[int]) -> None:
    """Set initial interactive widths once, preserving later user adjustments."""
    header = view.horizontalHeader() if isinstance(view, QTableView) else view.header()
    header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    header.setMinimumSectionSize(36)
    scale = view.fontMetrics().horizontalAdvance("M") / 10

    for column in range(view.model().columnCount()):
        header.resizeSection(column, round(widths[min(column, len(widths) - 1)] * scale))
        view.setColumnHidden(column, False)

    if isinstance(view, QTableView):
        view.verticalHeader().hide()

    if not header.property("columnMenuInstalled"):
        header.setProperty("columnMenuInstalled", True)
        header.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        header.customContextMenuRequested.connect(lambda point: _column_menu(view, point))


def _column_menu(view: QTableView | QTreeView, point: QPoint) -> None:
    header = view.horizontalHeader() if isinstance(view, QTableView) else view.header()
    menu = QMenu(view)

    for column in range(view.model().columnCount()):
        label = str(view.model().headerData(column, Qt.Orientation.Horizontal))
        action = menu.addAction(label)
        action.setCheckable(True)
        action.setChecked(not view.isColumnHidden(column))
        action.setEnabled(label != "Field")
        action.toggled.connect(lambda checked, index=column: view.setColumnHidden(index, not checked))

    menu.exec(header.mapToGlobal(point))


class LayoutPreferences(QObject):
    """Retain window preferences through the existing beside-executable JSON."""

    def __init__(self, window: MainWindow, path: Path) -> None:
        super().__init__(window)
        self.setObjectName("portableLayout")
        self._window = window
        self._path = path
        loaded = load_settings(path)
        self._ui = loaded.settings.ui
        self._can_save = loaded.error is None and not any("'ui.version'" in warning for warning in loaded.warnings)
        self._restored = False
        window.installEventFilter(self)
        self._restore_geometry()
        # Review is constructed before preferences exist, unlike later modal
        # dialogues. Register it here and keep its existing MainWindow column
        # keys so moving the controls does not discard the user's widths.
        self.watch_dialog(window.review_window)

    def _restore_geometry(self) -> None:
        window = self._window
        values = self._ui.geometry

        if len(values) == 4 and values[2] > 0 and values[3] > 0:
            from PySide6.QtGui import QGuiApplication

            target = next((screen for screen in QGuiApplication.screens()
                           if screen.availableGeometry().contains(QPoint(values[0], values[1]))), window.screen())
            # Monitor positions may change between runs. Bound both size and
            # position to an available screen so the title bar remains reachable.
            area = target.availableGeometry()
            width = max(400, min(values[2], area.width() - 32))
            height = max(300, min(values[3], area.height() - 64))
            window.resize(width, height)
            window.move(max(area.left(), min(values[0], area.right() - width)),
                        max(area.top(), min(values[1], area.bottom() - height)))

        if self._ui.maximised:
            window.setWindowState(window.windowState() | Qt.WindowState.WindowMaximized)

    def _views(self, widget: QWidget) -> tuple[QTableView | QTreeView, ...]:
        return (*widget.findChildren(QTableView), *widget.findChildren(QTreeView))

    @staticmethod
    def _view_key(widget: QWidget, view: QTableView | QTreeView, index: int) -> str:
        return f"{type(widget).__name__}/{view.objectName() or index}"

    def _restore_columns(self, widget: QWidget) -> None:
        for index, view in enumerate(self._views(widget)):
            model = cast(QAbstractItemModel | None, view.model())

            if model is None:
                continue

            key = self._view_key(widget, view, index)

            for column, width in enumerate(self._ui.column_widths.get(key, ())):
                if column < model.columnCount() and width >= 36:
                    view.setColumnWidth(column, min(width, widget.screen().availableGeometry().width()))

            if key in self._ui.hidden_columns:
                for column in range(model.columnCount()):
                    label = model.headerData(column, Qt.Orientation.Horizontal)
                    # Field names anchor every diff row. A saved hidden-column
                    # preference must not leave metadata values without labels.
                    view.setColumnHidden(column, label != "Field" and column in self._ui.hidden_columns[key])

    def _capture_columns(self, widget: QWidget) -> None:
        widths = dict(self._ui.column_widths)
        hidden = dict(self._ui.hidden_columns)

        for index, view in enumerate(self._views(widget)):
            model = cast(QAbstractItemModel | None, view.model())

            # Owned windows can receive Hide during Qt teardown after their
            # models have gone. There are no usable columns left to persist.
            if model is None:
                continue

            key = self._view_key(widget, view, index)
            widths[key] = tuple(view.columnWidth(column) for column in range(model.columnCount()))
            hidden[key] = tuple(column for column in range(model.columnCount()) if view.isColumnHidden(column))

        self._ui = replace(self._ui, column_widths=widths, hidden_columns=hidden)

    def watch_dialog(self, dialog: QDialog) -> None:
        dialog.installEventFilter(self)
        sizes = self._ui.dialog_sizes.get(type(dialog).__name__, ())

        if len(sizes) == 2 and all(value > 0 for value in sizes):
            area = dialog.screen().availableGeometry()
            dialog.resize(min(sizes[0], area.width() - 32), min(sizes[1], area.height() - 64))

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self._window:
            if event.type() == QEvent.Type.Show and not self._restored:
                self._restored = True

                sizes = self._ui.horizontal_sizes

                if len(sizes) == 2 and all(value > 0 for value in sizes):
                    self._window.main_splitter.setSizes(list(sizes))

                self._restore_columns(self._window)

            elif event.type() == QEvent.Type.Close:
                self._save()

        elif isinstance(watched, QDialog):
            if event.type() == QEvent.Type.Show and watched is not self._window.review_window:
                self._restore_columns(watched)

            elif event.type() == QEvent.Type.Hide:
                if watched is not self._window.review_window:
                    self._capture_columns(watched)

                self._capture_dialog_size(watched)

        return super().eventFilter(watched, event)

    def _capture_dialog_size(self, dialog: QDialog) -> None:
        # Keep the normal size when maximised, and capture review even when it
        # is still open as the main window saves its final layout.
        rect = dialog.normalGeometry() if dialog.isMaximized() else dialog.geometry()
        dialog_sizes = dict(self._ui.dialog_sizes)
        dialog_sizes[type(dialog).__name__] = (rect.width(), rect.height())
        self._ui = replace(self._ui, dialog_sizes=dialog_sizes)

    def _save(self) -> None:
        library = self._window.library_controller
        current = load_settings(self._path)

        # Automatic layout persistence must never replace a corrupt/unsupported
        # user document with defaults. Explicit Settings saves remain separate.
        if not self._can_save or library is None or current.error is not None:
            return

        # A newer layout schema must survive intact. Likewise, an external edit
        # after launch belongs to the user; closing a window is not permission to
        # replace it. Explicit Settings saves update both this document and the
        # controller's settings, so those authorised changes still persist.
        if any("'ui.version'" in warning for warning in current.warnings) or current.settings != library.settings:
            return

        self._capture_columns(self._window)
        self._capture_dialog_size(self._window.review_window)
        rect = self._window.normalGeometry()
        self._ui = replace(self._ui, geometry=(rect.x(), rect.y(), rect.width(), rect.height()),
                           maximised=self._window.isMaximized(),
                           horizontal_sizes=tuple(self._window.main_splitter.sizes()))
        settings = replace(library.settings, ui=self._ui)

        try:
            save_settings(self._path, settings)
        except OSError as error:
            self._window.workflow_message_label.setText(f"Layout could not be saved: {error}")
            return

        library.settings = settings
