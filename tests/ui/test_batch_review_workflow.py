"""The populated Qt selection drives the same stable-ID batch review service."""

from PySide6.QtCore import QItemSelectionModel, Qt

from metadata_polisher.domain.metadata import MetadataField
from tests.ui.test_review_workflow import review_window
from tests.unit.session.test_batch_review import make_batch_session


# Distinct titles expose an erroneous implementation that copies the first
# proposal to every selected file or treats Mixed values as writable metadata.
def test_selected_files_use_their_own_titles_and_show_mixed_values(qtbot, tmp_path):
    window = review_window(qtbot, tmp_path, make_batch_session())
    window.file_table_view.selectAll()
    window.diff_table_view.selectRow(0)

    assert window.diff_model.index(0, 2).data() == "Mixed values"
    qtbot.mouseClick(window.use_proposed_button, Qt.MouseButton.LeftButton)
    values = tuple(item.change_set.final_metadata.title for item in window.session_state.groups[0].reviewed_files)
    assert values == ("Candidate 1", "Candidate 2", "Candidate 3")
    assert "3 affected" in window.workflow_message_label.text()
    qtbot.mouseClick(window.undo_review_button, Qt.MouseButton.LeftButton)
    assert tuple(item.change_set.final_metadata.title for item in window.session_state.groups[0].reviewed_files) == (
        "Local first", "Local second", "Local third",
    )


def test_selected_fields_clear_for_all_selected_files_without_submitting_write(qtbot, tmp_path):
    window = review_window(qtbot, tmp_path, make_batch_session())
    window.file_table_view.selectAll()
    selection = window.diff_table_view.selectionModel()
    album = window.diff_model.index(list(MetadataField).index(MetadataField.ALBUM), 0)
    selection.select(album, QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)
    qtbot.mouseClick(window.clear_value_button, Qt.MouseButton.LeftButton)

    assert all(item.change_set.final_metadata.title is None and item.change_set.final_metadata.album is None
               for item in window.session_state.groups[0].reviewed_files)
    assert len(window.selected_fields()) == 2
    assert window.processing_executor.pending == []
