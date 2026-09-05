"""Manual review input retains semantic types and requires explicit non-empty values."""

import pytest
from PySide6.QtWidgets import QDialog

from metadata_polisher.domain.metadata import MetadataField, Position
from metadata_polisher.ui.dialogs.manual_value_dialog import ManualValueDialog


@pytest.mark.parametrize("field", [MetadataField.TITLE, MetadataField.ALBUM, MetadataField.DATE])
def test_manual_text_value_preserves_the_entered_text(qtbot, field):
    dialog = ManualValueDialog(field, "Existing value")
    qtbot.addWidget(dialog)
    assert dialog.text_edit.toPlainText() == "Existing value"

    dialog.text_edit.setPlainText("手動; edited value")
    dialog.accept()

    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.value() == "手動; edited value"


@pytest.mark.parametrize(
    "field",
    [MetadataField.ARTISTS, MetadataField.ALBUM_ARTISTS, MetadataField.COMPOSERS, MetadataField.GENRES],
)
# Punctuation may belong to an artist/composer name. Only explicit line breaks
# separate values, so punctuation cannot silently split one contributor into two.
def test_manual_multi_value_uses_one_value_per_line_without_splitting_punctuation(qtbot, field):
    dialog = ManualValueDialog(field, ("Existing artist", "Another artist"))
    qtbot.addWidget(dialog)
    assert dialog.text_edit.toPlainText() == "Existing artist\nAnother artist"

    dialog.text_edit.setPlainText("新しい名前\nArtist; with punctuation\nAC/DC")
    dialog.accept()

    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.value() == ("新しい名前", "Artist; with punctuation", "AC/DC")


@pytest.mark.parametrize("field", [MetadataField.TRACK, MetadataField.DISC])
def test_manual_position_edits_number_and_total_independently(qtbot, field):
    dialog = ManualValueDialog(field, Position(number=2, total=12))
    qtbot.addWidget(dialog)
    assert dialog.number_spin.value() == 2
    assert dialog.total_spin.value() == 12

    dialog.number_spin.setValue(3)
    dialog.total_spin.setValue(0)
    dialog.accept()

    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.value() == Position(number=3, total=None)


@pytest.mark.parametrize("field", [MetadataField.TRACK, MetadataField.DISC])
def test_manual_position_can_supply_only_a_total(qtbot, field):
    dialog = ManualValueDialog(field, None)
    qtbot.addWidget(dialog)
    assert dialog.number_spin.value() == 0
    assert dialog.total_spin.value() == 0

    dialog.total_spin.setValue(15)
    dialog.accept()

    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.value() == Position(number=None, total=15)


@pytest.mark.parametrize("field", list(MetadataField))
# Empty input is a correction opportunity, not permission to remove a field;
# removal has its own explicit Clear action outside this editor.
def test_empty_manual_input_stays_open_instead_of_becoming_a_clear_decision(qtbot, field):
    dialog = ManualValueDialog(field, None)
    qtbot.addWidget(dialog)
    dialog.show()

    dialog.accept()

    assert dialog.result() != QDialog.DialogCode.Accepted
    assert dialog.isVisible()
    assert dialog.error_label.text()


@pytest.mark.parametrize(
    ("field", "invalid_value", "valid_value", "expected_value"),
    [
        (MetadataField.TITLE, "  \n ", "Corrected title", "Corrected title"),
        (
            MetadataField.ARTISTS,
            "One artist\n\nAnother artist",
            "One artist\nAnother artist",
            ("One artist", "Another artist"),
        ),
    ],
)
def test_invalid_value_can_be_corrected_without_closing_the_dialog(
    qtbot, field, invalid_value, valid_value, expected_value
):
    dialog = ManualValueDialog(field, None)
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.text_edit.setPlainText(invalid_value)

    dialog.accept()

    assert dialog.isVisible()
    assert dialog.error_label.text()

    dialog.text_edit.setPlainText(valid_value)
    dialog.accept()

    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.value() == expected_value
