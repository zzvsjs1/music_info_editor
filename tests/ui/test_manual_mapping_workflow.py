from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication

from metadata_polisher.application.changes import ChangeIssueCode
from tests.ui.test_review_workflow import review_window
from tests.unit.session.test_mapping_editing import partial_session


# A partial automatic mapping leaves explicit work for the user. Accepting a
# manual assignment must rebuild review through the service, not just its label.
def test_partial_mapping_opens_editor_and_rebuilds_applicable_changes(qtbot, tmp_path):
    window = review_window(qtbot, tmp_path, partial_session())
    file_id = window.current_file_id()

    def assign_track():
        dialog = QApplication.activeModalWidget()
        selector = dialog.selectors[file_id]
        selector.setCurrentIndex(selector.findData(0))
        dialog.accept()

    QTimer.singleShot(0, assign_track)
    qtbot.mouseClick(window.track_mapping_button, Qt.MouseButton.LeftButton)
    reviewed = window.session_state.groups[0].reviewed_files[0]
    assert reviewed.track_mapping_resolved
    assert ChangeIssueCode.UNRESOLVED_TRACK_MAPPING not in {item.code for item in reviewed.change_set.validation.issues}
    assert window.current_file_id() == file_id
    assert window.file_model.index(0, 10).data() == "Manual"
