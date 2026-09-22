"""A reusable filename-template editor with field-aware completion."""

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QCompleter, QLineEdit, QWidget

from metadata_polisher.rename.completion import TemplateCompletion, template_completion
from metadata_polisher.rename.template import TemplateField


def _qt_length(text: str) -> int:
    """Qt cursor offsets count UTF-16 units rather than Python characters."""
    return len(text.encode("utf-16-le")) // 2


class TemplateLineEdit(QLineEdit):
    """Complete only the current percent-delimited field, preserving Undo."""

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self._inserting_completion = False
        self._completer = QCompleter([f"%{field.value}%" for field in TemplateField], self)
        self._completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)

        # QLineEdit.setCompleter would connect activation to replacing the whole
        # line. Attach the popup directly and perform a token-sized insertion.
        self._completer.setWidget(self)
        self._completer.activated.connect(self._insert_completion)
        popup = self._completer.popup()
        assert popup is not None
        self._popup = popup
        self._popup.installEventFilter(self)
        self.textEdited.connect(self._show_completions)
        self.cursorPositionChanged.connect(self._refresh_visible_completions)
        self.selectionChanged.connect(self._refresh_visible_completions)
        self.setToolTip("Type % to choose a field. Enter or Tab inserts it; Escape closes suggestions. "
                        "Ctrl+Space reopens suggestions inside a field.")

    def completer(self) -> QCompleter:
        return self._completer

    def _completion(self) -> TemplateCompletion | None:
        # Convert before consulting the pure grammar helper, then convert back
        # for selection. An emoji before the field must not shift either end.
        text = self.text()
        cursor = len(text.encode("utf-16-le")[: self.cursorPosition() * 2].decode("utf-16-le"))
        return template_completion(text, cursor)

    def _show_completions(self) -> None:
        if self._inserting_completion:
            return

        completion = self._completion()

        if self.isReadOnly() or self.hasSelectedText() or completion is None:
            self._popup.hide()
            return

        self._completer.setCompletionPrefix(completion.prefix)

        if self._completer.completionCount() == 0:
            self._popup.hide()
            return

        popup = self._popup
        rectangle = self.cursorRect()
        rectangle.setWidth(popup.sizeHintForColumn(0) + popup.verticalScrollBar().sizeHint().width())
        self._completer.complete(rectangle)
        popup.setCurrentIndex(self._completer.completionModel().index(0, 0))

    def _refresh_visible_completions(self) -> None:
        # A mouse or arrow movement must never leave a suggestion tied to an
        # earlier field. Moving the cursor alone does not open a hidden popup.
        if self._popup.isVisible():
            self._show_completions()

    def _insert_completion(self, token: str) -> None:
        completion = self._completion()

        if self.isReadOnly() or self.hasSelectedText() or completion is None:
            return

        text = self.text()
        self._inserting_completion = True
        self._popup.hide()

        try:
            # insert() replaces a selection through QLineEdit's normal editing
            # machinery, so one Undo restores the user's unfinished field.
            start = _qt_length(text[: completion.start])
            length = _qt_length(text[completion.start : completion.end])
            self.setSelection(start, length)
            self.insert(token)
        finally:
            self._inserting_completion = False

    def _handle_completion_key(self, event: QKeyEvent) -> bool:
        popup = self._popup

        if popup.isVisible():
            if event.key() in (Qt.Key.Key_Up, Qt.Key.Key_Down):
                # Route navigation here as well as acceptance: while typing,
                # focus can remain on the line edit even with a visible popup.
                model = self._completer.completionModel()
                step = 1 if event.key() == Qt.Key.Key_Down else -1
                row = (popup.currentIndex().row() + step) % model.rowCount()
                index = model.index(row, 0)
                popup.setCurrentIndex(index)
                popup.scrollTo(index)
                event.accept()
                return True

            if event.key() == Qt.Key.Key_Escape:
                popup.hide()
                event.accept()
                return True

            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Tab):
                token = popup.currentIndex().data()

                if isinstance(token, str):
                    self._insert_completion(token)

                event.accept()
                return True

        if event.key() == Qt.Key.Key_Space and event.modifiers() == Qt.KeyboardModifier.ControlModifier:
            self._show_completions()
            event.accept()
            return True

        return False

    def event(self, event: QEvent) -> bool:
        # Handle Tab before QWidget interprets it as focus navigation. The same
        # keys must not reach the dialogue's Save or Cancel actions while choosing.
        if (event.type() == QEvent.Type.KeyPress and isinstance(event, QKeyEvent)
                and self._handle_completion_key(event)):
            return True

        return super().event(event)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        # Real keyboard input can arrive at the popup rather than the editor;
        # keep acceptance and dismissal identical for both event destinations.
        if (watched is self._popup and event.type() == QEvent.Type.KeyPress
                and isinstance(event, QKeyEvent) and self._handle_completion_key(event)):
            return True

        return super().eventFilter(watched, event)
