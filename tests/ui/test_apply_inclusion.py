"""Highlighting, write inclusion and final confirmation are separate actions."""

from dataclasses import replace

from PySide6.QtCore import QItemSelectionModel, Qt
from PySide6.QtWidgets import QDialog, QTableView

from metadata_polisher.application.changes import RenameDecision
from metadata_polisher.bootstrap import create_application
from metadata_polisher.domain.metadata import MetadataField
from metadata_polisher.domain.review import FieldConfidence, FieldDecisionKind
from metadata_polisher.execution.cancellation import OperationCancelledError
from metadata_polisher.infrastructure.settings import RenameSettings
from metadata_polisher.session.review_editing import apply_field_decision, set_rename_decision
from metadata_polisher.ui.dialogs.apply_summary_dialog import ApplySummaryDialog
from metadata_polisher.ui.models.file_table_model import FileTableModel
from tests.ui.test_apply_workflow import changed_local_session
from tests.ui.test_review_workflow import review_window
from tests.ui.test_scan_workflow import ControlledExecutor
from tests.unit.session.test_review_editing import make_local_session, make_selected_session, make_source, proposal


def included_window(qtbot, tmp_path, state=None, *, service=None):
    executor = ControlledExecutor()
    _, window = create_application(
        [], executor=executor, settings_file=tmp_path / "settings.json", apply_service=service,
    )
    qtbot.addWidget(window)
    window.set_session_state(state or changed_local_session())

    return window, executor


def file_ids(window):
    return tuple(source.file_id for group in window.session_state.groups for source in group.group.files)


def test_include_column_emits_stable_identity_without_mutating_metadata(qapp):
    del qapp
    state = changed_local_session()
    group = state.groups[0]
    first, second = (source.file_id for source in group.group.files)
    model = FileTableModel(group, included_file_ids=frozenset((second,)))
    requests = []
    model.inclusion_requested.connect(lambda file_id, included: requests.append((file_id, included)))
    before = group.reviewed_files

    assert model.headerData(0, Qt.Orientation.Horizontal) == "Include"
    assert model.flags(model.index(0, 0)) & Qt.ItemFlag.ItemIsUserCheckable
    assert model.data(model.index(0, 0), Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Unchecked
    assert model.data(model.index(1, 0), Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Checked
    assert model.setData(model.index(0, 0), Qt.CheckState.Checked, Qt.ItemDataRole.CheckStateRole)
    assert requests == [(first, True)]
    assert group.reviewed_files == before
    assert not model.setData(model.index(0, 5), "Unreviewed title", Qt.ItemDataRole.EditRole)


# Reordering recreates visible rows but must not transfer checkbox membership;
# the write scope belongs to stable file identities, not their table positions.
def test_inclusion_tracks_ids_when_the_file_model_is_reordered(qapp):
    del qapp
    state = changed_local_session()
    group = state.groups[0]
    first, second = (source.file_id for source in group.group.files)
    model = FileTableModel(group, included_file_ids=frozenset((first,)))
    reordered = replace(group, group=replace(group.group, files=tuple(reversed(group.group.files))))
    model.set_group_state(reordered)

    assert model.index(0, 0).data(Qt.ItemDataRole.UserRole) == second
    assert model.index(0, 0).data(Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Unchecked
    assert model.index(1, 0).data(Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Checked


def test_highlighting_never_changes_write_inclusion(qtbot, tmp_path):
    window, executor = included_window(qtbot, tmp_path)
    first, second = file_ids(window)
    window.set_included_file_ids(frozenset((first,)))
    window.file_table_view.selectRow(1)

    assert window.selected_file_ids() == (second,)
    assert window.included_file_ids == frozenset((first,))
    assert not executor.pending


def test_include_and_exclude_selected_have_explicit_separate_counts(qtbot, tmp_path):
    window, _ = included_window(qtbot, tmp_path)
    first, second = file_ids(window)
    window.set_included_file_ids(frozenset())
    window.file_table_view.selectRow(0)
    window.include_selected_button.click()
    window.file_table_view.selectRow(1)

    assert window.included_file_ids == frozenset((first,))
    window.include_selected_button.click()
    assert window.included_file_ids == frozenset((first, second))
    assert "1 highlighted" in window.selection_scope_label.text().lower()
    assert "2 included" in window.selection_scope_label.text().lower()

    window.exclude_selected_button.click()
    assert window.included_file_ids == frozenset((first,))


# Deliberately disagreeing scopes expose accidental use of the current review
# target when the controller should instead freeze the explicit write batch.
def test_confirmation_submits_only_included_files_even_when_another_is_highlighted(qtbot, tmp_path, monkeypatch):
    received = []

    class RecordingService:
        def apply(self, request, *, cancellation, events):
            received.append(request)
            raise OperationCancelledError("Test capture stops before any file work")

    window, executor = included_window(qtbot, tmp_path, service=RecordingService())
    first, second = file_ids(window)
    window.set_included_file_ids(frozenset((first,)))
    window.file_table_view.selectRow(1)
    summaries = []

    def confirm(dialog):
        summaries.append(dialog._summary)
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(ApplySummaryDialog, "exec", confirm)
    window.apply_selected_button.click()

    assert len(summaries) == len(executor.pending) == 1
    assert summaries[0].file_count == summaries[0].write_file_count == 1
    executor.run_next()
    qtbot.waitUntil(lambda: window.session_state.active_operation is None)
    assert len(received) == 1
    assert tuple(item.source.file_id for group in received[0].groups for item in group.files) == (first,)
    assert second not in {item.source.file_id for group in received[0].groups for item in group.files}


def test_inclusion_change_while_confirmation_is_open_requires_a_new_summary(qtbot, tmp_path, monkeypatch):
    window, executor = included_window(qtbot, tmp_path)
    first, second = file_ids(window)
    window.set_included_file_ids(frozenset((first,)))

    def change_scope_and_confirm(_dialog):
        # Modal dialogues continue processing events. The confirmation must
        # retain its captured inclusion membership as well as its review state.
        window.set_included_file_ids(frozenset((second,)))
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(ApplySummaryDialog, "exec", change_scope_and_confirm)
    window.apply_selected_button.click()

    assert not executor.pending
    assert "changed" in window.workflow_message_label.text().lower()


def test_review_decisions_and_include_rename_do_not_submit_a_write(qtbot, tmp_path):
    window, executor = included_window(qtbot, tmp_path)
    first, _ = file_ids(window)
    window.set_included_file_ids(frozenset((first,)))
    window.file_table_view.selectRow(0)
    window.diff_table_view.selectRow(list(MetadataField).index(MetadataField.TITLE))
    window.keep_existing_button.click()
    window.apply_rename_button.click()

    assert window.apply_rename_button.text() == "Include rename"
    assert window.session_state.active_operation is None
    assert not executor.pending


def test_rename_only_confirmation_allows_unresolved_optional_composer(qtbot, tmp_path, monkeypatch):
    state = make_selected_session((proposal(MetadataField.COMPOSERS, ("Uncertain credit",), FieldConfidence.LOW),))
    first = state.groups[0].group.files[0].file_id
    state = set_rename_decision(state, "album", (first,), RenameDecision.APPLY_RENAME, RenameSettings())
    window, executor = included_window(qtbot, tmp_path, state)
    window.set_included_file_ids(frozenset((first,)))
    summaries = []

    def cancel_after_review(dialog):
        summaries.append(dialog._summary)
        return QDialog.DialogCode.Rejected

    monkeypatch.setattr(ApplySummaryDialog, "exec", cancel_after_review)
    window.apply_selected_button.click()

    assert len(summaries) == 1
    assert summaries[0].can_apply
    assert summaries[0].rename_count == 1
    assert summaries[0].addition_count == summaries[0].replacement_count == summaries[0].removal_count == 0
    assert not executor.pending


def test_noop_included_file_is_not_counted_as_changed(qtbot, tmp_path, monkeypatch):
    state = make_local_session(make_source("changed.flac"), make_source("unchanged.flac"))
    first, second = (source.file_id for source in state.groups[0].group.files)
    state = apply_field_decision(
        state, "album", first, MetadataField.TITLE, FieldDecisionKind.USE_MANUAL,
        RenameSettings(), manual_value="Reviewed title",
    )
    state = apply_field_decision(
        state, "album", second, MetadataField.TITLE, FieldDecisionKind.KEEP_EXISTING, RenameSettings(),
    )
    window, executor = included_window(qtbot, tmp_path, state)
    window.set_included_file_ids(frozenset((first, second)))
    summaries = []

    def inspect_and_cancel(dialog):
        summaries.append(dialog._summary)
        assert dialog.file_table.model().rowCount() == 2
        assert dialog.file_table.model().index(0, 1).data() == "1"
        assert dialog.file_table.model().index(1, 1).data() == "0"
        return QDialog.DialogCode.Rejected

    monkeypatch.setattr(ApplySummaryDialog, "exec", inspect_and_cancel)
    window.apply_selected_button.click()

    assert len(summaries) == 1
    assert summaries[0].file_count == 2
    assert summaries[0].write_file_count == summaries[0].replacement_count == 1
    assert not executor.pending


def test_multi_file_rename_preview_shows_each_files_own_destination(qtbot, tmp_path):
    window = review_window(qtbot, tmp_path, changed_local_session())
    selection = window.file_table_view.selectionModel()
    selection.select(
        window.file_model.index(1, 0),
        QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows,
    )
    window.rename_previews_button.click()
    table = window.findChild(QTableView, "renamePreviewTable")

    assert table is not None and table.model().rowCount() == 2
    displayed = "\n".join(
        str(table.model().index(row, column).data())
        for row in range(table.model().rowCount()) for column in range(table.model().columnCount())
    )
    assert "first.flac" in displayed and "second.flac" in displayed
    assert "Corrected 0.flac" in displayed and "Corrected 1.flac" in displayed


def test_empty_inclusion_cannot_submit_highlighted_files(qtbot, tmp_path):
    window, executor = included_window(qtbot, tmp_path)
    window.set_included_file_ids(frozenset())
    window.file_table_view.selectRow(0)

    assert not window.apply_selected_button.isEnabled()
    assert not executor.pending
