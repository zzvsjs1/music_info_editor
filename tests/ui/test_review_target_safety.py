"""Review commands follow highlighted identities and Undo follows valid history."""

from dataclasses import replace

import pytest
from PySide6.QtCore import QItemSelectionModel, Qt
from PySide6.QtWidgets import QDialog

from metadata_polisher.domain.metadata import MetadataField
from metadata_polisher.domain.review import DecisionOrigin, FieldDecisionKind
from metadata_polisher.infrastructure.settings import RenameSettings
from metadata_polisher.session.review_editing import apply_field_decision, review_undo_targets
from metadata_polisher.session.state import GroupSelection, OperationKind, begin_operation, mark_groups_requires_rescan
from metadata_polisher.ui.dialogs.manual_value_dialog import ManualValueDialog
from tests.ui.test_review_workflow import field_review, review_window
from tests.unit.session.test_batch_review import make_batch_session
from tests.unit.session.test_review_editing import make_local_session, make_source


def deselect_focused_field(qtbot, window, field):
    # Reproduce Qt's retained keyboard focus after deselection instead of simply
    # clearing the current index, which would hide the wrong-field regression.
    """Reproduce Ctrl-click adding and then removing a row without moving focus."""
    view = window.diff_table_view
    index = window.diff_model.index(list(MetadataField).index(field), 0)
    view.scrollTo(index)

    for _click in range(2):
        qtbot.mouseClick(
            view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ControlModifier,
            pos=view.visualRect(index).center(),
        )

    assert window.current_field() is field
    assert window.selected_fields() == (MetadataField.TITLE,)


@pytest.mark.parametrize(("button_name", "decision", "expected"), [
    ("keep_existing_button", FieldDecisionKind.KEEP_EXISTING, "序曲"),
    ("use_proposed_button", FieldDecisionKind.USE_PROPOSAL, "Overture"),
    ("clear_value_button", FieldDecisionKind.CLEAR, None),
    ("manual_value_button", FieldDecisionKind.USE_MANUAL, "Corrected title"),
])
def test_field_actions_never_edit_the_deselected_current_row(
    qtbot, tmp_path, monkeypatch, button_name, decision, expected,
):
    window = review_window(qtbot, tmp_path)
    album_before = field_review(window, MetadataField.ALBUM)
    deselect_focused_field(qtbot, window, MetadataField.ALBUM)
    entered = []

    def enter_manual(dialog):
        entered.append((dialog.windowTitle(), dialog.text_edit.toPlainText()))
        dialog.text_edit.setPlainText("Corrected title")
        dialog.accept()
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(ManualValueDialog, "exec", enter_manual)

    if decision is FieldDecisionKind.USE_PROPOSAL:
        # The selected Title has two language variants; the focused Album does
        # not. Both the chooser contents and command must use the Title row.
        assert window.proposal_combo.count() == 2
        window.proposal_combo.setCurrentIndex(1)

    assert getattr(window, button_name).isEnabled()
    qtbot.mouseClick(getattr(window, button_name), Qt.MouseButton.LeftButton)
    title = field_review(window, MetadataField.TITLE)

    assert title.decision is decision
    assert title.decision_origin is DecisionOrigin.USER
    assert window.session_state.groups[0].reviewed_files[0].change_set.final_metadata.title == expected
    assert field_review(window, MetadataField.ALBUM) == album_before
    assert window.processing_executor.pending == []

    if decision is FieldDecisionKind.USE_MANUAL:
        assert entered == [("Manual value: title", "序曲")]


def test_candidate_availability_uses_the_selected_field(qtbot, tmp_path):
    window = review_window(qtbot, tmp_path)
    deselect_focused_field(qtbot, window, MetadataField.COMPOSERS)

    assert window.proposal_combo.isEnabled()
    assert window.proposal_combo.count() == 2
    assert window.use_proposed_button.isEnabled()

    # In the reverse selection, a focused Title cannot supply a candidate to
    # the highlighted Composer field, which has no provider proposal.
    window.diff_table_view.selectionModel().select(
        window.diff_model.index(list(MetadataField).index(MetadataField.COMPOSERS), 0),
        QItemSelectionModel.SelectionFlag.ClearAndSelect | QItemSelectionModel.SelectionFlag.Rows,
    )
    window.diff_table_view.selectionModel().setCurrentIndex(
        window.diff_model.index(0, 0), QItemSelectionModel.SelectionFlag.NoUpdate,
    )

    assert window.selected_fields() == (MetadataField.COMPOSERS,)
    assert not window.proposal_combo.isEnabled()
    assert window.proposal_combo.count() == 0
    assert not window.use_proposed_button.isEnabled()


def test_no_highlighted_fields_disables_actions_and_ignores_direct_dispatch(qtbot, tmp_path, monkeypatch):
    window = review_window(qtbot, tmp_path)
    window.diff_table_view.clearSelection()
    snapshot = window.session_state

    def unexpected_dialog(_dialog):
        pytest.fail("An empty field selection must not open a manual editor")

    monkeypatch.setattr(ManualValueDialog, "exec", unexpected_dialog)

    assert window.current_field() is MetadataField.TITLE
    assert window.selected_fields() == ()

    for name in ("keep_existing_button", "use_proposed_button", "clear_value_button", "manual_value_button"):
        assert not getattr(window, name).isEnabled()

    assert not window.proposal_combo.isEnabled()
    assert window.proposal_combo.count() == 0

    for decision in (FieldDecisionKind.KEEP_EXISTING, FieldDecisionKind.USE_PROPOSAL, FieldDecisionKind.CLEAR):
        window.review_controller._decide(decision)

    window.review_controller.edit_manual()
    assert window.session_state is snapshot


def test_manual_common_value_requires_compatible_field_types(qtbot, tmp_path, monkeypatch):
    window = review_window(qtbot, tmp_path)
    selection = window.diff_table_view.selectionModel()
    artists = window.diff_model.index(list(MetadataField).index(MetadataField.ARTISTS), 0)
    selection.select(artists, QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)

    def unexpected_dialog(_dialog):
        pytest.fail("Text and multi-value fields require separate manual input")

    monkeypatch.setattr(ManualValueDialog, "exec", unexpected_dialog)
    snapshot = window.session_state

    assert not window.manual_value_button.isEnabled()
    assert "same value type" in window.manual_value_button.toolTip()
    assert window.clear_value_button.isEnabled()
    window.review_controller.edit_manual()
    assert window.session_state is snapshot


def test_manual_common_text_edits_only_compatible_highlighted_fields(qtbot, tmp_path, monkeypatch):
    window = review_window(qtbot, tmp_path)
    selection = window.diff_table_view.selectionModel()
    album = window.diff_model.index(list(MetadataField).index(MetadataField.ALBUM), 0)
    selection.select(album, QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)
    genres_before = field_review(window, MetadataField.GENRES)
    selection.setCurrentIndex(
        window.diff_model.index(list(MetadataField).index(MetadataField.GENRES), 0),
        QItemSelectionModel.SelectionFlag.NoUpdate,
    )
    dialogue_titles = []

    def enter_manual(dialog):
        dialogue_titles.append(dialog.windowTitle())
        dialog.text_edit.setPlainText("Shared text")
        dialog.accept()
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(ManualValueDialog, "exec", enter_manual)

    assert window.manual_value_button.isEnabled()
    qtbot.mouseClick(window.manual_value_button, Qt.MouseButton.LeftButton)

    assert field_review(window, MetadataField.TITLE).manual_value == "Shared text"
    assert field_review(window, MetadataField.ALBUM).manual_value == "Shared text"
    assert field_review(window, MetadataField.GENRES) == genres_before
    assert dialogue_titles == ["Set common Title, Album for 1 file"]


def test_manual_editor_rejects_changed_highlighted_fields(qtbot, tmp_path, monkeypatch):
    window = review_window(qtbot, tmp_path)
    snapshot = window.session_state

    def change_selection_during_dialog(dialog):
        dialog.text_edit.setPlainText("Stale edit")
        window.diff_table_view.selectRow(list(MetadataField).index(MetadataField.ALBUM))
        dialog.accept()
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(ManualValueDialog, "exec", change_selection_during_dialog)
    window.review_controller.edit_manual()

    assert window.session_state is snapshot


def test_undo_remains_available_after_moving_or_clearing_file_selection(qtbot, tmp_path):
    window = review_window(qtbot, tmp_path, make_batch_session())
    first_id = window.session_state.groups[0].group.files[0].file_id
    qtbot.mouseClick(window.clear_value_button, Qt.MouseButton.LeftButton)
    window.file_table_view.selectRow(1)

    assert window.undo_review_button.isEnabled()
    assert "01.flac: Title" in window.undo_review_button.toolTip()
    assert "02.flac" not in window.undo_review_button.toolTip()

    qtbot.mouseClick(window.clear_file_selection_button, Qt.MouseButton.LeftButton)
    assert window.review_target_file_ids() == ()
    assert window.undo_review_button.isEnabled()
    assert tuple(item.source.file_id for item in review_undo_targets(window.session_state)) == (first_id,)
    qtbot.mouseClick(window.undo_review_button, Qt.MouseButton.LeftButton)

    assert window.session_state.groups[0].reviewed_files[0].change_set.final_metadata.title == "Local first"
    assert not window.session_state.review_undo
    assert not window.undo_review_button.isEnabled()
    assert window.processing_executor.pending == []


def test_undo_skips_newer_stale_evidence_and_describes_the_usable_action(qtbot, tmp_path):
    state = make_batch_session()
    first, second, _third = state.groups[0].group.files
    settings = RenameSettings()

    for source in (first, second):
        state = apply_field_decision(
            state, "album", source.file_id, MetadataField.TITLE, FieldDecisionKind.CLEAR, settings,
        )

    # Reproduce a later independent update whose evidence supersedes the second
    # action. Its old undo record remains, but it must not restore that field.
    history = state.review_undo
    state = apply_field_decision(
        state, "album", second.file_id, MetadataField.TITLE, FieldDecisionKind.USE_MANUAL, settings,
        manual_value="Independent review",
    )
    state = replace(state, review_undo=history)
    window = review_window(qtbot, tmp_path, state)

    assert tuple(item.source.file_id for item in review_undo_targets(state)) == (first.file_id,)
    assert window.undo_review_button.isEnabled()
    assert "01.flac: Title" in window.undo_review_button.toolTip()
    assert "02.flac" not in window.undo_review_button.toolTip()
    qtbot.mouseClick(window.undo_review_button, Qt.MouseButton.LeftButton)

    first_review, second_review, _third_review = window.session_state.groups[0].reviewed_files
    assert first_review.change_set.final_metadata.title == "Local first"
    assert second_review.change_set.final_metadata.title == "Independent review"
    assert not window.session_state.review_undo


# Undo follows the last valid session action, not the currently displayed album.
# The tooltip and restored state must describe the same historical targets.
def test_undo_identifies_and_restores_an_action_in_another_album(qtbot, tmp_path):
    state = make_batch_session()
    other = make_local_session(make_source("other.flac")).groups[0]
    other = replace(other, group=replace(other.group, group_id="other", album_title="Other album"))
    state = replace(state, groups=(*state.groups, other))
    first = state.groups[0].group.files[0]
    edited = apply_field_decision(
        state, "album", first.file_id, MetadataField.TITLE, FieldDecisionKind.CLEAR, RenameSettings(),
    )
    edited = replace(edited, selection=GroupSelection("other"))
    window = review_window(qtbot, tmp_path, edited)

    assert window.current_file_id() == other.group.files[0].file_id
    assert window.undo_review_button.isEnabled()
    assert "Album · 01.flac: Title" in window.undo_review_button.toolTip()
    assert "other.flac" not in window.undo_review_button.toolTip()
    qtbot.mouseClick(window.undo_review_button, Qt.MouseButton.LeftButton)

    assert window.session_state.groups[0].reviewed_files[0].change_set.final_metadata.title == "Local first"
    assert window.session_state.groups[1] is other
    assert window.session_state.selection == GroupSelection("other")


def test_undo_is_disabled_for_invalidated_history_and_during_operations(qtbot, tmp_path):
    window = review_window(qtbot, tmp_path, make_batch_session())
    qtbot.mouseClick(window.clear_value_button, Qt.MouseButton.LeftButton)
    edited = window.session_state
    active = begin_operation(edited, "LOOKUP-UNDO-TEST", OperationKind.LOOKUP, ("album",))
    window.set_session_state(active)

    assert not window.undo_review_button.isEnabled()
    assert "current operation" in window.undo_review_button.toolTip()
    window.review_controller.undo_review()
    assert window.session_state is active

    invalidated = mark_groups_requires_rescan(edited, ("album",))
    window.set_session_state(invalidated)

    assert invalidated.review_undo
    assert not review_undo_targets(invalidated)
    assert not window.undo_review_button.isEnabled()
    window.review_controller.undo_review()
    assert window.session_state is invalidated
