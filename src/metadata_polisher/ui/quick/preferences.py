"""Portable logical window preferences with guarded automatic persistence."""

from dataclasses import replace
from typing import TYPE_CHECKING

from PySide6.QtCore import Property, QEvent, QObject, QPoint, QRect, Qt, Signal, Slot
from PySide6.QtGui import QGuiApplication, QWindow

from metadata_polisher.infrastructure.settings import UiSettings, load_settings, save_settings

if TYPE_CHECKING:
    from metadata_polisher.ui.quick.backend import QuickBackend


class QuickLayout(QObject):
    """Own presentation preferences without overwriting external settings edits."""

    changed = Signal()

    def __init__(self, host: QuickBackend) -> None:
        super().__init__(host)
        self.host = host
        self._ui = host.app_settings.ui
        self._windows: dict[QWindow, str] = {}
        self._shown: set[QWindow] = set()
        self._normal_geometries: dict[QWindow, QRect] = {}
        loaded = load_settings(host.settings_file) if host.settings_file else None
        self._can_save = loaded is None or (
            loaded.error is None and not any("'ui.version'" in warning for warning in loaded.warnings)
        )

    albumWidth = Property(
        int, lambda self: self._ui.horizontal_sizes[0] if self._ui.horizontal_sizes else 300, notify=changed
    )

    @Slot(int)
    def setAlbumWidth(self, width: int) -> None:
        if width >= 100:
            remaining = self._ui.horizontal_sizes[1] if len(self._ui.horizontal_sizes) > 1 else max(100, 1180 - width)
            self._ui = replace(self._ui, horizontal_sizes=(width, remaining))

    @Slot(str, list, result=list)
    def columnWidths(self, key: str, defaults: list[int]) -> list[int]:
        values = self._ui.column_widths.get(key, ())
        return list(values) if len(values) == len(defaults) and all(value >= 36 for value in values) else defaults

    @Slot(str, list, result=list)
    def hiddenColumns(self, key: str, defaults: list[int]) -> list[int]:
        return list(self._ui.hidden_columns.get(key, tuple(defaults)))

    @Slot(str, list, list)
    def saveColumns(self, key: str, widths: list[int], hidden: list[int]) -> None:
        valid_widths = tuple(max(36, int(value)) for value in widths)
        valid_hidden = tuple(int(value) for value in hidden if 0 <= value < len(widths))
        self._ui = replace(
            self._ui,
            column_widths={**self._ui.column_widths, key: valid_widths},
            hidden_columns={**self._ui.hidden_columns, key: valid_hidden},
        )

    @Slot(QObject, str)
    def watchWindow(self, window: QObject, key: str) -> None:
        """Restore an owned dialogue and retain only sizes the user has seen."""
        if not isinstance(window, QWindow) or window in self._windows:
            return

        self._windows[window] = key
        self.restoreWindow(window, key)
        self._remember_normal_geometry(window)
        window.installEventFilter(self)
        window.destroyed.connect(lambda: self._forget_window(window))

        if window.isVisible():
            self._shown.add(window)

    def _forget_window(self, window: QWindow) -> None:
        # Engine teardown can precede the backend's final cleanup. Removing the
        # wrapper here prevents a later save from calling an already-deleted Qt object.
        self._windows.pop(window, None)
        self._shown.discard(window)
        self._normal_geometries.pop(window, None)

    def _remember_normal_geometry(self, window: QWindow) -> None:
        # QWindow has no normalGeometry accessor. Remember ordinary moves and
        # resizes before maximisation replaces its public geometry with the
        # screen rectangle. Hidden maximised windows retain their window state.
        if window.windowState() == Qt.WindowState.WindowNoState:
            rect = window.geometry()

            if rect.width() > 0 and rect.height() > 0:
                self._normal_geometries[window] = QRect(rect)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if isinstance(watched, QWindow) and watched in self._windows:
            if event.type() == QEvent.Type.Show:
                # QML's declarative dimensions can be reapplied after close.
                # Reopening must retain the last visible size in this session,
                # just as recreating the Widgets dialogue restores that size.
                if self._windows[watched] != "MainWindow" or watched not in self._shown:
                    self.restoreWindow(watched, self._windows[watched])

                self._shown.add(watched)
                self._remember_normal_geometry(watched)

            elif event.type() == QEvent.Type.Hide and watched in self._shown:
                self.captureWindow(watched, self._windows[watched])

            elif event.type() in (QEvent.Type.Move, QEvent.Type.Resize):
                self._remember_normal_geometry(watched)

        return super().eventFilter(watched, event)

    @Slot(QObject, str)
    def restoreWindow(self, window: QObject, key: str) -> None:
        if not isinstance(window, QWindow):
            return

        screen = window.screen() or QGuiApplication.primaryScreen()

        if key == "MainWindow" and len(self._ui.geometry) == 4:
            x, y, width, height = self._ui.geometry
            screen = next((candidate for candidate in QGuiApplication.screens()
                           if candidate.availableGeometry().contains(QPoint(x, y))), screen)
            area = screen.availableGeometry()
            width, height = min(width, area.width() - 32), min(height, area.height() - 64)
            # Restore onto a visible screen even when a previously used monitor
            # has been disconnected since the last run.
            x = max(area.left(), min(x, area.right() - width))
            y = max(area.top(), min(y, area.bottom() - height))
            rect = QRect(x, y, max(240, width), max(200, height))
            self._normal_geometries[window] = rect
            window.setGeometry(rect)

        elif key in self._ui.dialog_sizes:
            sizes = self._ui.dialog_sizes[key]

            if len(sizes) == 2 and all(value > 0 for value in sizes):
                area = screen.availableGeometry()
                window.resize(min(sizes[0], area.width() - 32), min(sizes[1], area.height() - 64))

        # Older preferences can contain only this flag. Its validity does not
        # depend on having closed the window once in its ordinary state.
        if key == "MainWindow" and self._ui.maximised:
            self._remember_normal_geometry(window)
            window.showMaximized()

    @Slot(QObject, str)
    def captureWindow(self, window: QObject, key: str) -> None:
        if not isinstance(window, QWindow):
            return

        maximised = window.windowState() == Qt.WindowState.WindowMaximized
        self._remember_normal_geometry(window)
        rect = self._normal_geometries.get(window)

        if key == "MainWindow":
            self._ui = replace(self._ui, maximised=maximised)

            if rect is not None:
                self._ui = replace(self._ui, geometry=(rect.x(), rect.y(), rect.width(), rect.height()))

        elif rect is not None:
            self._ui = replace(self._ui, dialog_sizes={**self._ui.dialog_sizes, key: (rect.width(), rect.height())})

    @Slot()
    def reset(self) -> None:
        self._ui = UiSettings()
        self.changed.emit()

    def persist(self) -> bool:
        host = self.host
        if not self._can_save or host.settings_file is None:
            return False
        loaded = load_settings(host.settings_file)
        if (
            loaded.error
            or loaded.settings != host.app_settings
            or any("'ui.version'" in warning for warning in loaded.warnings)
        ):
            return False

        # A modeless dialogue may still be open when Main closes. Include it in
        # the same guarded save rather than depending solely on Hide delivery.
        for window in tuple(self._shown):
            # QQuickWindow can report its original requested size after Hide.
            # Hidden windows were captured before that transition; only refresh
            # windows which are still visible, or their saved size would regress.
            if window.isVisible():
                self.captureWindow(window, self._windows[window])

        settings = replace(host.app_settings, ui=self._ui)
        try:
            save_settings(host.settings_file, settings)
        except OSError as error:
            host.set_status(f"Layout could not be saved: {error}")
            return False
        host.app_settings = settings
        return True
