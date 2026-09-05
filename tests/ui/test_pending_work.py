"""Session decisions survive cancelled replacement and exit operations."""

from dataclasses import replace
from pathlib import Path

import pytest
from PySide6.QtWidgets import QFileDialog, QMessageBox

from metadata_polisher.application.changes import RenameDecision, build_change_set
from metadata_polisher.application.scanning import ScanLibraryService
from metadata_polisher.bootstrap import create_application
from metadata_polisher.domain.metadata import MetadataField
from metadata_polisher.domain.review import FieldDecisionKind
from metadata_polisher.execution.cancellation import OperationCancelledError
from metadata_polisher.formats.registry import FormatRegistry
from metadata_polisher.infrastructure.settings import RenameSettings
from metadata_polisher.scanner.grouping import GroupingReason
from metadata_polisher.scanner.scanner import ScanResult
from metadata_polisher.session.review_editing import apply_field_decision, set_rename_decision, undo_last_review_action
from metadata_polisher.session.state import GroupSelection, ReviewedFileState, SessionState
from metadata_polisher.ui.main_window import MainWindow
from metadata_polisher.ui.pending_work import confirm_discard_pending, pending_work_summary
from tests.ui.test_models import make_group_state, make_reviews, make_source
from tests.ui.test_scan_workflow import ControlledExecutor
from tests.unit.session.test_lookup_editing import make_selected_session


def local_session(root: Path = Path("library")) -> SessionState:
    source = make_source(path=str(root / "Album" / "01.flac"))
    reviews = make_reviews(source)
    reviewed = ReviewedFileState(
        source.file_id, reviews=reviews,
        change_set=build_change_set(source, reviews, RenameDecision.KEEP_FILENAME),
    )

    return SessionState(
        root=root, groups=(make_group_state("album", source, reviewed),), selection=GroupSelection("album"),
    )


def edit_title(state: SessionState, decision=FieldDecisionKind.USE_MANUAL) -> SessionState:
    return apply_field_decision(
        state, "album", "file-1", MetadataField.TITLE, decision, RenameSettings(),
        manual_value="My correction" if decision is FieldDecisionKind.USE_MANUAL else None,
    )


def test_default_reviews_and_filename_previews_are_not_pending_user_work():
    state = local_session()
    summary = pending_work_summary(state)

    assert state.groups[0].reviewed_files
    assert not summary.has_pending_work
    assert summary.description() == ""


@pytest.mark.parametrize("decision", [
    FieldDecisionKind.KEEP_EXISTING, FieldDecisionKind.USE_MANUAL, FieldDecisionKind.CLEAR,
])
# A resolved Keep existing decision has value even when there is no tag delta.
# Discard protection therefore follows explicit intent, not just write counts.
def test_explicit_field_choices_are_counted_even_when_the_value_is_unchanged(decision):
    state = edit_title(local_session(), decision)
    summary = pending_work_summary(state)

    assert summary.has_pending_work
    assert summary.field_decision_count == 1
    assert summary.reviewed_file_count == 1
    assert summary.rename_file_count == 0
    assert summary.undo_action_count == 1
    assert "1 metadata decision in 1 file" in summary.description()


def test_rename_intent_is_protected_and_undo_can_return_to_an_unmodified_session():
    state = set_rename_decision(
        local_session(), "album", ("file-1",), RenameDecision.APPLY_RENAME, RenameSettings(),
    )
    summary = pending_work_summary(state)

    assert summary.field_decision_count == 0
    assert summary.rename_file_count == 1
    assert summary.undo_action_count == 1
    assert not pending_work_summary(undo_last_review_action(state, RenameSettings())).has_pending_work


@pytest.mark.parametrize("choice", ["split", "merge", "language", "disc"])
def test_session_only_grouping_and_lookup_hints_are_protected(choice):
    state = local_session()
    group = state.groups[0]

    if choice == "split":
        group = replace(group, group=replace(group.group, reason=GroupingReason.MANUAL_SPLIT))
    elif choice == "merge":
        group = replace(group, group=replace(group.group, reason=GroupingReason.MANUAL_MERGE))
    elif choice == "language":
        group = replace(group, language_override="en")
    else:
        group = replace(group, disc_number_override=2)

    summary = pending_work_summary(replace(state, groups=(group,)))

    assert summary.has_pending_work
    assert summary.edited_group_count == 1
    assert summary.field_decision_count == 0


def test_selected_release_is_a_choice_but_default_provider_fields_are_not_user_edits():
    summary = pending_work_summary(make_selected_session())

    assert summary.edited_group_count == 1
    assert summary.field_decision_count == 0
    assert summary.undo_action_count == 0


def test_write_inclusion_counts_only_files_still_in_the_library():
    summary = pending_work_summary(local_session(), frozenset({"file-1", "missing-file"}))

    assert summary.has_pending_work
    assert summary.included_file_count == 1
    assert summary.field_decision_count == 0


@pytest.mark.parametrize("response", [QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Discard])
def test_confirmation_shows_counts_and_defaults_enter_and_escape_to_cancel(qtbot, monkeypatch, response):
    window = MainWindow()
    qtbot.addWidget(window)
    window.set_session_state(edit_title(local_session()))
    window.set_included_file_ids(frozenset({"file-1"}))
    original = window.session_state
    observed = []

    def answer(dialog):
        observed.append(dialog)
        assert dialog.objectName() == "discardPendingWorkDialog"
        assert dialog.defaultButton() == dialog.button(QMessageBox.StandardButton.Cancel)
        assert dialog.escapeButton() == dialog.button(QMessageBox.StandardButton.Cancel)
        assert "1 metadata decision in 1 file" in dialog.informativeText()
        assert "1 file included for writing" in dialog.informativeText()
        assert "successful scan replaces" in dialog.informativeText()

        return response

    monkeypatch.setattr(QMessageBox, "exec", answer)

    assert confirm_discard_pending(window, "scan this folder") is (response == QMessageBox.StandardButton.Discard)
    assert len(observed) == 1
    assert window.session_state is original


def test_unedited_library_does_not_ask_for_discard_confirmation(qtbot, monkeypatch):
    window = MainWindow()
    qtbot.addWidget(window)
    window.set_session_state(local_session())

    def unexpected_confirmation(_dialog):
        pytest.fail("Automatic local review defaults must not trigger a discard warning")

    monkeypatch.setattr(QMessageBox, "exec", unexpected_confirmation)

    assert confirm_discard_pending(window, "exit")


@pytest.mark.parametrize("action", ["rescan", "browse"])
def test_cancelled_scan_preserves_review_undo_inclusion_and_the_current_folder(qtbot, monkeypatch, tmp_path, action):
    root = tmp_path / "library"
    root.mkdir()
    other = tmp_path / "another-library"
    other.mkdir()
    executor = ControlledExecutor()
    _, window = create_application([], executor=executor, settings_file=tmp_path / "settings.json")
    qtbot.addWidget(window)
    window.set_session_state(edit_title(local_session(root)))
    window.set_included_file_ids(frozenset({"file-1"}))
    original = window.session_state
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *args: str(other))
    monkeypatch.setattr(QMessageBox, "exec", lambda _dialog: QMessageBox.StandardButton.Cancel)

    getattr(window.library_controller, action)()

    assert executor.pending == []
    assert window.session_state is original
    assert window.session_state.review_undo
    assert window.included_file_ids == frozenset({"file-1"})
    assert window.root_path_edit.text() == str(root)


@pytest.mark.parametrize("outcome", ["completed", "failed", "cancelled"])
def test_confirmed_rescan_discards_work_only_after_a_successful_result(qtbot, monkeypatch, tmp_path, outcome):
    root = tmp_path / "library"
    root.mkdir()
    executor = ControlledExecutor()
    _, window = create_application([], executor=executor, settings_file=tmp_path / "settings.json")
    qtbot.addWidget(window)
    original = edit_title(local_session(root))
    window.set_session_state(original)
    window.set_included_file_ids(frozenset({"file-1"}))
    monkeypatch.setattr(QMessageBox, "exec", lambda _dialog: QMessageBox.StandardButton.Discard)
    window.library_controller.rescan()

    assert len(executor.pending) == 1
    assert window.session_state.groups == original.groups
    assert window.session_state.review_undo == original.review_undo

    if outcome == "completed":
        with qtbot.waitSignal(window.operation_bridge.completed):
            executor.run_next()

        assert window.session_state.groups == ()
        assert window.session_state.review_undo == ()
        assert window.included_file_ids == frozenset()
    else:
        handle, _, _ = executor.pending.pop()
        signal = window.operation_bridge.failed if outcome == "failed" else window.operation_bridge.cancelled
        error = OSError("Folder is no longer accessible") if outcome == "failed" else OperationCancelledError()

        with qtbot.waitSignal(signal):
            handle.future.set_exception(error)

        assert window.session_state.groups == original.groups
        assert window.session_state.review_undo == original.review_undo
        assert window.included_file_ids == frozenset({"file-1"})


# Stable IDs may survive scanning, but a replacement review session must not
# inherit authorisation to write from checkboxes set before that replacement.
def test_successful_rescan_clears_write_inclusion_even_when_file_ids_are_unchanged(qtbot, monkeypatch, tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    initial = local_session(root)
    source = initial.groups[0].group.files[0]
    scanner = ScanLibraryService(
        FormatRegistry(), scanner=lambda *args, **kwargs: ScanResult((source,), (), ()),
    )
    executor = ControlledExecutor()
    _, window = create_application(
        [], executor=executor, scanner=scanner, settings_file=tmp_path / "settings.json",
    )
    qtbot.addWidget(window)
    window.set_session_state(initial)
    window.set_included_file_ids(frozenset({source.file_id}))
    monkeypatch.setattr(QMessageBox, "exec", lambda _dialog: QMessageBox.StandardButton.Discard)
    window.library_controller.rescan()

    with qtbot.waitSignal(window.operation_bridge.completed):
        executor.run_next()

    assert window.session_state.groups[0].group.files[0].file_id == source.file_id
    assert window.included_file_ids == frozenset()


def test_invalid_folder_is_reported_before_asking_to_discard(qtbot, monkeypatch, tmp_path):
    executor = ControlledExecutor()
    _, window = create_application([], executor=executor, settings_file=tmp_path / "settings.json")
    qtbot.addWidget(window)
    window.set_session_state(edit_title(local_session()))
    window.root_path_edit.setText(str(tmp_path / "missing"))

    def unexpected_confirmation(_dialog):
        pytest.fail("An invalid folder cannot replace the session")

    monkeypatch.setattr(QMessageBox, "exec", unexpected_confirmation)
    window.library_controller.rescan()

    assert executor.pending == []
    assert "existing library folder" in window.workflow_message_label.text()


def test_cancelled_close_keeps_the_window_and_work_available(qtbot, monkeypatch):
    window = MainWindow()
    qtbot.addWidget(window)
    original = edit_title(local_session())
    window.set_session_state(original)
    window.show()
    monkeypatch.setattr(QMessageBox, "exec", lambda _dialog: QMessageBox.StandardButton.Cancel)

    assert not window.close()
    assert window.isVisible()
    assert window.session_state is original

    monkeypatch.setattr(QMessageBox, "exec", lambda _dialog: QMessageBox.StandardButton.Discard)

    assert window.close()
    assert not window.isVisible()
