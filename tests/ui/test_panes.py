"""Pane presentation works with supplied models without a main-window session."""

from PySide6.QtCore import Qt
from PySide6.QtGui import QTextCursor

from metadata_polisher.domain.metadata import MetadataField
from metadata_polisher.ui.models import FileTableModel, MetadataDiffModel
from tests.ui.test_main_window import make_group


def test_files_panes_follow_their_supplied_models_without_sharing_selection(qtbot):
    from metadata_polisher.ui.files_pane import FilesPane

    original = make_group("album", "original", "Original title")
    model = FileTableModel(original)
    pane = FilesPane(model)
    other = FilesPane(FileTableModel(original))
    qtbot.addWidget(pane)
    qtbot.addWidget(other)
    pane.show()
    pane.file_table_view.selectRow(0)

    # Highlighting is local to this pane and does not tick the write checkbox.
    assert pane.file_table_view.selectionModel().hasSelection()
    assert not other.file_table_view.selectionModel().hasSelection()
    assert model.index(0, 0).data(Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Unchecked

    model.set_group_state(make_group("album", "replacement", "Replacement title"))

    assert pane.file_table_view.model().index(0, 5).data() == "Replacement title"
    assert other.file_table_view.model().index(0, 5).data() == "Original title"


def test_review_pane_details_follow_model_replacement_and_preserve_copy_selection(qtbot):
    from metadata_polisher.ui.review_pane import ReviewPane

    source = make_group("album", "track", "Original title").group.files[0]
    model = MetadataDiffModel(source)
    pane = ReviewPane(model)
    qtbot.addWidget(pane)
    pane.show()
    pane.diff_table_view.selectRow(0)
    pane.field_details_button.click()
    pane.refresh_field_details((MetadataField.TITLE,))

    assert pane.field_details.isVisible()
    assert pane.field_details.isReadOnly()
    assert "Original title" in pane.field_details.toPlainText()

    cursor = pane.field_details.textCursor()
    cursor.select(QTextCursor.SelectionType.Document)
    pane.field_details.setTextCursor(cursor)
    copied_text = cursor.selectedText()
    pane.refresh_field_details((MetadataField.TITLE,))
    assert pane.field_details.textCursor().selectedText() == copied_text

    replacement = make_group("album", "track", "Replacement title").group.files[0]
    model.set_file(replacement, None)
    pane.refresh_field_details((MetadataField.TITLE,))

    assert "Replacement title" in pane.field_details.toPlainText()
    assert "Original title" not in pane.field_details.toPlainText()

    pane.refresh_field_details(())

    assert pane.field_details.isHidden()
    assert not pane.field_details.toPlainText()
    assert not pane.field_details_button.isEnabled()
