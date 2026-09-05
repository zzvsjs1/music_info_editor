from dataclasses import replace

from PySide6.QtCore import Qt

from metadata_polisher.application.changes import RenameDecision
from metadata_polisher.domain.metadata import MetadataField
from tests.ui.test_review_workflow import review_window


# Use the final reviewed disc value to expose previews accidentally derived
# from original tags; choosing the filename remains a separate session decision.
def test_rename_preview_uses_reviewed_disc_and_independent_rename_choice(qtbot, tmp_path):
    window = review_window(qtbot, tmp_path)
    window.diff_table_view.selectRow(list(MetadataField).index(MetadataField.DISC))
    qtbot.mouseClick(window.use_proposed_button, Qt.MouseButton.LeftButton)
    assert window.rename_proposed_label.toolTip() == "Proposed filename: 1.01. 序曲.flac"

    qtbot.mouseClick(window.keep_existing_button, Qt.MouseButton.LeftButton)
    assert window.rename_proposed_label.toolTip() == "Proposed filename: 01. 序曲.flac"
    qtbot.mouseClick(window.apply_rename_button, Qt.MouseButton.LeftButton)
    changed = window.session_state.groups[0].reviewed_files[0].change_set
    assert changed.rename_decision is RenameDecision.APPLY_RENAME
    assert changed.rename_change.new_path.name == "01. 序曲.flac"

    qtbot.mouseClick(window.keep_filename_button, Qt.MouseButton.LeftButton)
    changed = window.session_state.groups[0].reviewed_files[0].change_set
    assert changed.rename_change is None
    assert changed.rename_preview.new_path.name == "01. 序曲.flac"
    assert window.rename_template_label.toolTip() == "Template: [%discnumber%.]%tracknumber%. %title%"


def test_rename_setting_disables_apply_control(qtbot, tmp_path):
    window = review_window(qtbot, tmp_path)
    settings = window.library_controller.settings
    window.library_controller.settings = replace(settings, rename=replace(settings.rename, enabled=False))
    window.review_controller.refresh()
    assert not window.apply_rename_button.isEnabled()
    assert window.keep_filename_button.isEnabled()


def test_collision_block_is_visible_and_clears_when_filename_is_kept(qtbot, tmp_path):
    from tests.unit.session.test_review_editing import make_local_session, make_source

    state = make_local_session(make_source(), make_source("01. Overture.flac"))
    window = review_window(qtbot, tmp_path, state)
    qtbot.mouseClick(window.apply_rename_button, Qt.MouseButton.LeftButton)
    assert "Blocked:" in window.rename_validation_label.text()
    assert "DESTINATION_COLLISION" in window.rename_validation_label.text()
    qtbot.mouseClick(window.keep_filename_button, Qt.MouseButton.LeftButton)
    assert "Blocked:" not in window.rename_validation_label.text()
    assert "Warning:" in window.rename_validation_label.text()
