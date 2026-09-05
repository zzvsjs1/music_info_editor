"""One explicitly included target must not become the highlighted file."""

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog

from metadata_polisher.domain.metadata import MetadataField
from metadata_polisher.domain.review import DecisionOrigin, FieldDecisionKind
from metadata_polisher.ui.dialogs.manual_value_dialog import ManualValueDialog
from tests.ui.test_review_workflow import review_window
from tests.unit.session.test_batch_review import make_batch_session


@pytest.mark.parametrize(("button_name", "decision", "expected"), [
    ("keep_existing_button", FieldDecisionKind.KEEP_EXISTING, "Local second"),
    ("use_proposed_button", FieldDecisionKind.USE_PROPOSAL, "Candidate 2"),
    ("clear_value_button", FieldDecisionKind.CLEAR, None),
    ("manual_value_button", FieldDecisionKind.USE_MANUAL, "Edited included title"),
])
# Disagreeing included/highlighted files expose hidden reliance on the table's
# current row: the visible review scope must determine the actual edit target.
def test_one_included_file_receives_review_while_another_stays_highlighted(
    qtbot, tmp_path, monkeypatch, button_name, decision, expected,
):
    window = review_window(qtbot, tmp_path, make_batch_session())
    before = window.session_state.groups[0].reviewed_files
    first, second, _third = before
    window.set_included_file_ids(frozenset((second.file_id,)))
    window.review_scope_combo.setCurrentIndex(window.review_scope_combo.findData("included"))
    window.diff_table_view.selectRow(0)
    initial_manual_values = []

    def edit_value(dialog):
        initial_manual_values.append(dialog.text_edit.toPlainText())
        dialog.text_edit.setPlainText("Edited included title")
        dialog.accept()
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(ManualValueDialog, "exec", edit_value)

    assert window.current_file_id() == first.file_id
    assert window.review_target_file_ids() == (second.file_id,)
    qtbot.mouseClick(getattr(window, button_name), Qt.MouseButton.LeftButton)
    after = window.session_state.groups[0].reviewed_files
    review = next(item for item in after[1].reviews if item.field is MetadataField.TITLE)

    assert after[0] == before[0]
    assert after[2] == before[2]
    assert review.decision is decision
    assert review.decision_origin is DecisionOrigin.USER
    assert after[1].change_set.final_metadata.title == expected
    assert window.included_file_ids == frozenset((second.file_id,))
    assert window.processing_executor.pending == []

    if decision is FieldDecisionKind.USE_MANUAL:
        assert initial_manual_values == ["Local second"]
