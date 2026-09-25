from dataclasses import replace

import pytest
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication

from metadata_polisher.bootstrap import create_application
from metadata_polisher.domain.metadata import MetadataField
from metadata_polisher.domain.review import FieldDecisionKind
from tests.ui.test_scan_workflow import ControlledExecutor
from tests.unit.session.test_lookup_editing import make_selected_session


def review_window(qtbot, tmp_path, state=None):
    # Start with an explicit file and field highlight. A current Qt index alone
    # is insufficient authorisation for the controller to edit a review target.
    _, window = create_application([], executor=ControlledExecutor(), settings_file=tmp_path / "settings.json")
    qtbot.addWidget(window)
    window.set_session_state(state or make_selected_session())
    window.file_table_view.selectRow(0)
    window.open_metadata_review()
    window.diff_table_view.selectRow(list(MetadataField).index(MetadataField.TITLE))
    return window


def focus_review_shortcuts(qtbot, window):
    """Give synthetic key events the same active review window as real input."""
    window.show()
    window.review_window.open_review()
    window.diff_table_view.setFocus()
    QApplication.processEvents()

    # Unattended Windows runners may refuse a foreground activation request.
    # Supply Qt's active shortcut context only when native activation failed.
    if QApplication.activeWindow() is not window.review_window:
        QApplication.setActiveWindow(window.review_window)
        window.diff_table_view.setFocus()

    qtbot.waitUntil(lambda: QApplication.activeWindow() is window.review_window
                    and window.diff_table_view.hasFocus())


def field_review(window, field):
    return next(review for review in window.session_state.groups[0].reviewed_files[0].reviews if review.field is field)


def test_field_decisions_rebuild_changes_and_keep_selection(qtbot, tmp_path):
    window = review_window(qtbot, tmp_path)
    original = window.session_state
    old_change = original.groups[0].reviewed_files[0].change_set
    qtbot.mouseClick(window.clear_value_button, Qt.MouseButton.LeftButton)
    assert field_review(window, MetadataField.TITLE).decision is FieldDecisionKind.CLEAR
    assert window.session_state.groups[0].reviewed_files[0].change_set.final_metadata.title is None
    assert window.session_state.groups[0].reviewed_files[0].change_set is not old_change
    assert window.diff_table_view.currentIndex().row() == 0

    qtbot.mouseClick(window.keep_existing_button, Qt.MouseButton.LeftButton)
    assert field_review(window, MetadataField.TITLE).decision is FieldDecisionKind.KEEP_EXISTING
    assert window.session_state.groups[0].reviewed_files[0].change_set.final_metadata.title == "序曲"

    window.proposal_combo.setCurrentIndex(1)
    qtbot.mouseClick(window.use_proposed_button, Qt.MouseButton.LeftButton)
    assert field_review(window, MetadataField.TITLE).decision is FieldDecisionKind.USE_PROPOSAL
    assert window.session_state.groups[0].reviewed_files[0].change_set.final_metadata.title == "Overture"

    def enter_manual():
        dialog = QApplication.activeModalWidget()
        dialog.text_edit.setPlainText("My title")
        dialog.accept()

    QTimer.singleShot(0, enter_manual)
    qtbot.mouseClick(window.manual_value_button, Qt.MouseButton.LeftButton)
    assert field_review(window, MetadataField.TITLE).manual_value == "My title"
    assert window.diff_model.index(0, 4).data() == "My title"
    assert original.groups[0].reviewed_files[0].proposals == window.session_state.groups[0].reviewed_files[0].proposals
    assert original.groups[0].group.files == window.session_state.groups[0].group.files


def test_manual_edit_is_available_before_any_provider_lookup(qtbot, tmp_path):
    state = make_selected_session()
    group = state.groups[0]
    group = type(group)(group=group.group)
    window = review_window(qtbot, tmp_path, replace(state, groups=(group,)))
    button = window.manual_value_button

    def enter_manual():
        dialog = QApplication.activeModalWidget()
        dialog.text_edit.setPlainText("Local correction")
        dialog.accept()

    QTimer.singleShot(0, enter_manual)
    qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
    assert window.session_state.groups[0].reviewed_files[0].change_set.final_metadata.title == "Local correction"
    assert window.session_state.groups[0].selected_release is None


def test_bulk_safe_additions_preserves_conflicting_and_explicit_values(qtbot, tmp_path):
    from metadata_polisher.domain.metadata import MetadataSnapshot, Position
    from metadata_polisher.domain.review import DecisionOrigin, FieldConfidence
    from tests.unit.session.test_review_editing import make_selected_session as selected_session
    from tests.unit.session.test_review_editing import proposal

    state = selected_session((
        proposal(MetadataField.ARTISTS, ("Artist",)),
        proposal(MetadataField.GENRES, ("New genre",)),
        proposal(MetadataField.COMPOSERS, ("New composer",)),
        proposal(MetadataField.DATE, "2025", FieldConfidence.LOW),
    ), metadata=MetadataSnapshot(title="Overture", album="Album", track=Position(1),
                                genres=("My genre",), composers=("My composer",)))
    window = review_window(qtbot, tmp_path, state)
    original = window.session_state.groups[0].reviewed_files[0].proposals
    qtbot.mouseClick(window.accept_safe_additions_button, Qt.MouseButton.LeftButton)
    assert field_review(window, MetadataField.ARTISTS).decision_origin is DecisionOrigin.USER
    final = window.session_state.groups[0].reviewed_files[0].change_set.final_metadata
    assert final.artists == ("Artist",)
    assert final.genres == ("My genre",)
    assert final.composers == ("My composer",)
    assert final.date is None
    assert window.session_state.groups[0].reviewed_files[0].proposals is original


@pytest.mark.parametrize("field", [MetadataField.ARTISTS, MetadataField.DISC])
def test_missing_multi_value_and_position_fields_can_be_edited(qtbot, tmp_path, field):
    state = make_selected_session()
    state = replace(state, groups=(type(state.groups[0])(group=state.groups[0].group),))
    window = review_window(qtbot, tmp_path, state)
    window.diff_table_view.selectRow(list(MetadataField).index(field))
    timer = QTimer()
    timer.setSingleShot(True)

    def enter_manual():
        dialog = QApplication.activeModalWidget()

        if field is MetadataField.DISC:
            dialog.number_spin.setValue(2)
        else:
            dialog.text_edit.setPlainText("First artist\nSecond artist")

        dialog.accept()

    timer.timeout.connect(enter_manual)
    timer.start(0)

    try:
        window.review_controller.edit_manual()
    finally:
        timer.stop()

    assert field_review(window, field).decision is FieldDecisionKind.USE_MANUAL
