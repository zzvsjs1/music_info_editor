"""Review scopes retain healthy members and exclude unsupported media identities."""

from dataclasses import replace

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from metadata_polisher.bootstrap import create_application
from metadata_polisher.domain.metadata import FieldReadState, MetadataField
from metadata_polisher.domain.review import DecisionOrigin, FieldDecisionKind
from metadata_polisher.session.state import mark_groups_requires_rescan
from tests.ui.test_review_workflow import review_window
from tests.ui.test_scan_workflow import ControlledExecutor
from tests.unit.session.test_batch_review import make_batch_session
from tests.unit.session.test_review_editing import make_local_session, make_source


def blocked_first_session():
    """The first album is stale; later tracks retain distinct candidate titles."""
    state = make_batch_session()
    first = make_local_session(make_source("blocked.flac")).groups[0]
    first = replace(first, group=replace(first.group, group_id="blocked"))
    return mark_groups_requires_rescan(replace(state, groups=(first, *state.groups)), ("blocked",))


def test_unsupported_multi_selection_has_no_review_or_write_targets(qtbot, tmp_path):
    for name in ("one.opus", "two.ogg"):
        (tmp_path / name).write_bytes(b"Disposable unsupported fixture")

    executor = ControlledExecutor()
    _, window = create_application([], executor=executor, settings_file=tmp_path / "settings.json")
    qtbot.addWidget(window)
    window.root_path_edit.setText(str(tmp_path))
    window.rescan_button.click()
    executor.run_next()
    qtbot.waitUntil(lambda: window.session_state.active_operation is None)
    window.group_view.setCurrentIndex(window.group_model.index(0, 0))

    with qtbot.captureExceptions() as exceptions:
        window.file_table_view.selectAll()

    assert not exceptions
    assert len(window.selected_file_ids()) == 2
    assert window.review_target_file_ids() == ()
    assert not window.include_selected_button.isEnabled()
    assert not window.include_review_scope_button.isEnabled()
    assert not window.manual_value_button.isEnabled()
    assert window.diff_model.rowCount() == 0
    assert "unsupported" in window.review_window.target_label.text().lower()


@pytest.mark.parametrize(("button", "decision", "expected"), [
    ("clear_value_button", FieldDecisionKind.CLEAR, (None, None, None)),
    ("keep_existing_button", FieldDecisionKind.KEEP_EXISTING, ("Local first", "Local second", "Local third")),
    ("use_proposed_button", FieldDecisionKind.USE_PROPOSAL, ("Candidate 1", "Candidate 2", "Candidate 3")),
    ("manual_value_button", FieldDecisionKind.USE_MANUAL, ("Shared correction",) * 3),
])
def test_batch_actions_edit_healthy_members_after_a_blocked_first_album(qtbot, tmp_path, button, decision, expected):
    state = blocked_first_session()
    window = review_window(qtbot, tmp_path, state)
    window.review_scope_combo.setCurrentIndex(window.review_scope_combo.findData("library"))
    window.diff_table_view.selectRow(0)
    action = getattr(window, button)
    assert action.isEnabled()

    if decision is FieldDecisionKind.USE_MANUAL:
        def enter_value():
            dialog = QApplication.activeModalWidget()
            dialog.text_edit.setPlainText("Shared correction")
            dialog.accept()

        QTimer.singleShot(0, enter_value)

    action.click()
    assert window.session_state.groups[0] == state.groups[0]
    reviews = window.session_state.groups[1].reviewed_files
    assert tuple(item.change_set.final_metadata.title for item in reviews) == expected
    assert all(next(field for field in item.reviews if field.field is MetadataField.TITLE).decision is decision
               for item in reviews)
    assert "3 affected" in window.workflow_message_label.text()
    assert "1 blocked" in window.workflow_message_label.text()
    assert not window.included_file_ids
    assert window.processing_executor.pending == []


def test_unreadable_first_field_does_not_disable_other_selected_fields(qtbot, tmp_path):
    state = make_local_session(make_source("one.flac"))
    group = state.groups[0]
    source = group.group.files[0]
    states = dict(source.read_result.field_states)
    states[MetadataField.TITLE] = FieldReadState.UNREADABLE
    source = replace(source, read_result=replace(source.read_result, field_states=states))
    state = replace(state, groups=(replace(group, group=replace(group.group, files=(source,))),))
    window = review_window(qtbot, tmp_path, state)
    from PySide6.QtCore import QItemSelectionModel

    window.diff_table_view.selectionModel().select(
        window.diff_model.index(list(MetadataField).index(MetadataField.ALBUM), 0),
        QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows,
    )
    assert window.clear_value_button.isEnabled()
    window.clear_value_button.click()
    assert "1 affected" in window.workflow_message_label.text()
    assert "1 blocked" in window.workflow_message_label.text()


def test_safe_additions_can_reach_a_later_reviewed_album(qtbot, tmp_path):
    state = blocked_first_session()
    window = review_window(qtbot, tmp_path, state)
    window.review_scope_combo.setCurrentIndex(window.review_scope_combo.findData("library"))
    assert window.accept_safe_additions_button.isEnabled()
    window.accept_safe_additions_button.click()

    assert window.session_state.groups[0] == state.groups[0]
    assert all(next(field for field in item.reviews if field.field is MetadataField.GENRES).decision_origin
               is DecisionOrigin.USER for item in window.session_state.groups[1].reviewed_files)
    assert "blocked" in window.workflow_message_label.text()
    assert not window.processing_executor.pending
