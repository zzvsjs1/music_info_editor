"""Exercise real template completion gestures inside the settings dialogue."""

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QDialogButtonBox

from metadata_polisher.infrastructure.settings import AppSettings
from metadata_polisher.rename.template import TemplateField
from metadata_polisher.ui.dialogs.settings_dialog import SettingsDialog


@pytest.fixture
def template_editor(qtbot):
    dialog = SettingsDialog(AppSettings())
    qtbot.addWidget(dialog)
    dialog.show()
    editor = dialog.template_edit
    editor.clear()
    editor.setFocus()
    return dialog, editor


def visible_suggestions(editor):
    completer = editor.completer()
    assert completer is not None
    assert completer.popup().isVisible()
    model = completer.completionModel()
    return [model.index(row, 0).data() for row in range(model.rowCount())]


def test_percent_offers_every_supported_field_and_filters_as_the_user_types(qtbot, template_editor):
    _, editor = template_editor
    qtbot.keyClicks(editor, "%")

    assert set(visible_suggestions(editor)) == {f"%{field.value}%" for field in TemplateField}

    qtbot.keyClicks(editor, "al")

    assert set(visible_suggestions(editor)) == {"%album%", "%albumartist%"}

    qtbot.keyClicks(editor, "z")

    assert not editor.completer().popup().isVisible()
    assert editor.text() == "%alz"

    qtbot.keyClick(editor, Qt.Key.Key_Backspace)

    assert set(visible_suggestions(editor)) == {"%album%", "%albumartist%"}


@pytest.mark.parametrize("key", [Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Tab])
def test_keyboard_completion_preserves_the_template_and_does_not_save(qtbot, template_editor, key):
    dialog, editor = template_editor
    editor.setText("[%discnumber%.]%tracknumber%.  - %album%")
    editor.setCursorPosition(editor.text().index(" - "))
    qtbot.keyClicks(editor, "%ti")
    assert visible_suggestions(editor) == ["%title%"]

    qtbot.keyClick(editor, key)

    assert editor.text() == "[%discnumber%.]%tracknumber%. %title% - %album%"
    assert editor.cursorPosition() == editor.text().index(" - ")
    assert not editor.completer().popup().isVisible()
    assert dialog.isVisible()
    assert dialog.result() != QDialog.DialogCode.Accepted

    # Completing a field is one reversible edit, including its closing percent.
    qtbot.keyClick(editor, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)

    assert editor.text() == "[%discnumber%.]%tracknumber%. %ti - %album%"


def test_clicking_a_suggestion_replaces_the_existing_field_suffix(qtbot, template_editor):
    dialog, editor = template_editor
    editor.setText("[%album%] - %title%")
    editor.setCursorPosition(4)
    qtbot.keyClick(editor, Qt.Key.Key_Space, Qt.KeyboardModifier.ControlModifier)
    assert set(visible_suggestions(editor)) == {"%album%", "%albumartist%"}
    popup = editor.completer().popup()
    model = popup.model()
    index = next(model.index(row, 0) for row in range(model.rowCount())
                 if model.index(row, 0).data() == "%albumartist%")

    qtbot.mouseClick(popup.viewport(), Qt.MouseButton.LeftButton, pos=popup.visualRect(index).center())

    assert editor.text() == "[%albumartist%] - %title%"
    assert dialog.isVisible()


def test_escape_dismisses_suggestions_without_cancelling_settings(qtbot, template_editor):
    dialog, editor = template_editor
    qtbot.keyClicks(editor, "%ti")
    assert visible_suggestions(editor) == ["%title%"]

    qtbot.keyClick(editor, Qt.Key.Key_Escape)

    assert dialog.isVisible()
    assert editor.text() == "%ti"
    assert not editor.completer().popup().isVisible()

    qtbot.keyClick(editor, Qt.Key.Key_Space, Qt.KeyboardModifier.ControlModifier)

    assert visible_suggestions(editor) == ["%title%"]


@pytest.mark.parametrize("text", [r"\%", r"\%ti", "%title%", "plain text"])
def test_literal_and_finished_fields_do_not_offer_completion(qtbot, template_editor, text):
    _, editor = template_editor
    qtbot.keyClicks(editor, text)

    assert editor.completer() is not None
    assert not editor.completer().popup().isVisible()
    assert editor.text() == text


def test_arrows_choose_a_suggestion_and_save_uses_the_completed_template(qtbot, template_editor):
    dialog, editor = template_editor
    qtbot.keyClicks(editor, "%al")
    assert len(visible_suggestions(editor)) == 2
    qtbot.keyClick(editor, Qt.Key.Key_Down)
    chosen = editor.completer().popup().currentIndex().data()
    assert chosen == "%albumartist%"

    qtbot.keyClick(editor, Qt.Key.Key_Return)
    qtbot.mouseClick(dialog.buttons.button(QDialogButtonBox.StandardButton.Save), Qt.MouseButton.LeftButton)

    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.settings().rename.template == chosen


def test_completion_uses_qt_cursor_positions_after_non_bmp_characters(qtbot, template_editor):
    _, editor = template_editor
    editor.setText("🎵 [%ti] - %album%")
    # Qt counts the musical-note emoji as two UTF-16 units, Python as one character.
    editor.setCursorPosition(7)
    qtbot.keyClick(editor, Qt.Key.Key_Space, Qt.KeyboardModifier.ControlModifier)
    assert visible_suggestions(editor) == ["%title%"]

    qtbot.keyClick(editor, Qt.Key.Key_Tab)

    assert editor.text() == "🎵 [%title%] - %album%"
    assert editor.cursorPosition() == 11


def test_tab_retains_normal_focus_navigation_when_suggestions_are_hidden(qtbot, template_editor):
    dialog, editor = template_editor
    editor.setText("%title%")

    qtbot.keyClick(editor, Qt.Key.Key_Tab)

    assert dialog.track_digits_spin.hasFocus()


@pytest.mark.parametrize("key", [Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Tab, Qt.Key.Key_Escape])
def test_keys_sent_to_the_popup_do_not_save_or_cancel_settings(qtbot, template_editor, key):
    dialog, editor = template_editor
    qtbot.keyClicks(editor, "%ti")
    assert visible_suggestions(editor) == ["%title%"]

    qtbot.keyClick(editor.completer().popup(), key)

    assert editor.text() == ("%ti" if key == Qt.Key.Key_Escape else "%title%")
    assert not editor.completer().popup().isVisible()
    assert dialog.isVisible()
    assert dialog.result() != QDialog.DialogCode.Accepted


def test_select_all_dismisses_suggestions_even_if_the_cursor_does_not_move(qtbot, template_editor):
    _, editor = template_editor
    qtbot.keyClicks(editor, "%ti")
    assert visible_suggestions(editor) == ["%title%"]
    cursor = editor.cursorPosition()

    qtbot.keyClick(editor, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)

    assert editor.selectedText() == "%ti"
    assert editor.cursorPosition() == cursor
    assert not editor.completer().popup().isVisible()

    qtbot.keyClicks(editor, "%ar")

    assert visible_suggestions(editor) == ["%artist%"]


def test_moving_to_a_literal_dismisses_old_suggestions(qtbot, template_editor):
    _, editor = template_editor
    qtbot.keyClicks(editor, "prefix %ti")
    assert visible_suggestions(editor) == ["%title%"]

    editor.setCursorPosition(0)

    assert not editor.completer().popup().isVisible()


def test_case_insensitive_matching_inserts_a_valid_lowercase_field(qtbot, template_editor):
    _, editor = template_editor
    qtbot.keyClicks(editor, "%TI")
    assert visible_suggestions(editor) == ["%title%"]

    qtbot.keyClick(editor, Qt.Key.Key_Tab)

    assert editor.text() == "%title%"
