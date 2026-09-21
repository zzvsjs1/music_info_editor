"""Discarding candidate-dependent regroup decisions requires an explicit choice."""

from dataclasses import replace

from PySide6.QtCore import QItemSelectionModel, Qt
from PySide6.QtWidgets import QDialog, QLabel, QMessageBox

from metadata_polisher.domain.metadata import MetadataField
from metadata_polisher.domain.review import FieldDecisionKind
from metadata_polisher.infrastructure.settings import RenameSettings
from metadata_polisher.matching.track_mapping import map_tracks
from metadata_polisher.session.review_editing import apply_field_decision
from metadata_polisher.session.state import GroupState
from metadata_polisher.ui.dialogs.group_dialogs import MergeGroupsDialog
from tests.ui.test_review_workflow import review_window
from tests.unit.session.test_lookup_editing import make_selected_session
from tests.unit.session.test_review_editing import make_local_session, make_source


def test_merge_explains_candidate_loss_and_cancel_preserves_exact_session(qtbot, tmp_path, monkeypatch):
    state = make_selected_session()
    source = state.groups[0].group.files[0]
    state = apply_field_decision(
        state, "album", source.file_id, MetadataField.TITLE, FieldDecisionKind.USE_PROPOSAL,
        RenameSettings(), proposal_index=1,
    )
    other = make_local_session(make_source("other.flac")).groups[0]
    other = GroupState(replace(other.group, group_id="other"))
    state = replace(state, groups=(*state.groups, other))
    window = review_window(qtbot, tmp_path, state)
    selection = window.group_view.selectionModel()
    selection.select(window.group_model.index(1, 0), QItemSelectionModel.SelectionFlag.Select
                     | QItemSelectionModel.SelectionFlag.Rows)
    before = window.session_state
    messages = []

    def cancel(dialog):
        messages.append(" ".join(label.text() for label in dialog.findChildren(QLabel)))
        return QDialog.DialogCode.Rejected

    monkeypatch.setattr(MergeGroupsDialog, "exec", cancel)
    qtbot.mouseClick(window.merge_groups_button, Qt.MouseButton.LeftButton)

    assert messages and "candidate" in messages[0].casefold()
    assert "1" in messages[0]
    assert window.session_state is before


def test_split_with_manual_mapping_can_be_cancelled_before_membership_changes(qtbot, tmp_path, monkeypatch):
    state = make_selected_session()
    group = state.groups[0]
    files = (*group.group.files, make_source("other.flac"))
    mapping = map_tracks(files, group.selected_release.candidate.candidate, selected_medium_index=0)
    group = replace(group, group=replace(group.group, files=files), selected_metadata=None,
                    automatic_track_mapping=mapping, manual_track_mapping=mapping)
    window = review_window(qtbot, tmp_path, replace(state, groups=(group,)))
    before = window.session_state
    shown = []

    def cancel(dialog):
        shown.append(dialog.text())
        assert dialog.defaultButton() is dialog.button(QMessageBox.StandardButton.Cancel)
        return QMessageBox.StandardButton.Cancel

    monkeypatch.setattr(QMessageBox, "exec", cancel)
    qtbot.mouseClick(window.split_group_button, Qt.MouseButton.LeftButton)

    assert shown and "mapping" in shown[0].casefold()
    assert window.session_state is before


def test_merge_with_stale_group_keeps_healthy_review_and_shows_recovery(qtbot, tmp_path, monkeypatch):
    state = make_local_session(make_source("healthy.flac"))
    state = apply_field_decision(
        state, "album", state.groups[0].group.files[0].file_id, MetadataField.TITLE,
        FieldDecisionKind.USE_MANUAL, RenameSettings(), manual_value="Retain this review",
    )
    stale = make_local_session(make_source("stale.flac")).groups[0]
    stale = GroupState(replace(stale.group, group_id="stale"), requires_rescan=True)
    window = review_window(qtbot, tmp_path, replace(state, groups=(*state.groups, stale)))
    window.group_view.selectionModel().select(
        window.group_model.index(1, 0), QItemSelectionModel.SelectionFlag.Select
        | QItemSelectionModel.SelectionFlag.Rows,
    )
    before = window.session_state
    monkeypatch.setattr(MergeGroupsDialog, "exec", lambda _dialog: QDialog.DialogCode.Accepted)
    window.library_controller.merge_groups()

    assert window.session_state is before
    assert "rescan" in window.workflow_message_label.text().casefold()
