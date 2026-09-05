"""Completing one write batch must leave the next album ready for review."""

import wave

import pytest
from mutagen.id3 import TALB, TIT2, TRCK
from mutagen.wave import WAVE
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog

from metadata_polisher.application.apply import ApplyFileOutcomeStatus, ApplyService, LocalApplyPreflightInspector
from metadata_polisher.application.changes import RenameDecision
from metadata_polisher.bootstrap import create_application
from metadata_polisher.domain.metadata import MetadataField
from metadata_polisher.domain.review import FieldDecisionKind
from metadata_polisher.formats.registry import FormatRegistry
from metadata_polisher.infrastructure.transaction import TransactionalFileWriter
from metadata_polisher.session.review_editing import apply_field_decision, set_rename_decision
from metadata_polisher.ui.dialogs.apply_summary_dialog import ApplySummaryDialog
from tests.ui.test_scan_workflow import ControlledExecutor


def scanned_window(qtbot, tmp_path, *, service=None):
    # Generate disposable tagged audio in two folders so group navigation and
    # the real transactional writer are exercised without touching user media.
    root = tmp_path / "library"

    for album in ("First album", "Second album"):
        directory = root / album
        directory.mkdir(parents=True)
        path = directory / "original.wav"

        with wave.open(str(path), "wb") as audio:
            audio.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
            audio.writeframes(b"\0\0" * 80)

        tags = WAVE(path)
        tags.add_tags()
        tags.tags.add(TALB(encoding=3, text=[album]))
        tags.tags.add(TIT2(encoding=3, text=["Original title"]))
        tags.tags.add(TRCK(encoding=3, text=["1"]))
        tags.save()

    executor = ControlledExecutor()
    _, window = create_application(
        [], executor=executor, settings_file=tmp_path / "settings.json", apply_service=service,
    )
    qtbot.addWidget(window)
    window.root_path_edit.setText(str(root))
    window.rescan_button.click()
    executor.run_next()
    qtbot.waitUntil(lambda: window.session_state.active_operation is None)

    assert len(window.session_state.groups) == 2

    return window, executor


def edit_file(window, group, field, value):
    source = group.group.files[0]
    state = apply_field_decision(
        window.session_state, group.group.group_id, source.file_id, field,
        FieldDecisionKind.USE_MANUAL, window.library_controller.settings.rename, manual_value=value,
    )
    window.set_session_state(state)


def complete_apply(qtbot, window, executor, monkeypatch):
    monkeypatch.setattr(ApplySummaryDialog, "exec", lambda _dialog: QDialog.DialogCode.Accepted)
    assert window.review_apply_button.isEnabled()
    window.review_apply_button.click()
    assert len(executor.pending) == 1
    executor.run_next()
    qtbot.waitUntil(lambda: window.session_state.active_operation is None)


@pytest.mark.parametrize("rename", [False, True])
def test_apply_then_switch_group_allows_next_batch_and_refreshes_album(qtbot, tmp_path, monkeypatch, rename):
    window, executor = scanned_window(qtbot, tmp_path)
    first, second = window.session_state.groups
    first_id = first.group.files[0].file_id
    second_id = second.group.files[0].file_id
    edit_file(window, first, MetadataField.ALBUM, "Corrected first album")
    edit_file(window, second, MetadataField.TITLE, "Pending second title")
    pending_review = window.session_state.groups[1].reviewed_files

    if rename:
        window.set_session_state(set_rename_decision(
            window.session_state, first.group.group_id, (first_id,), RenameDecision.APPLY_RENAME,
            window.library_controller.settings.rename,
        ))

    window.group_view.setCurrentIndex(window.group_model.index(0, 0))
    window.file_table_view.selectRow(0)
    window.include_selected_button.click()
    complete_apply(qtbot, window, executor, monkeypatch)
    outcome = window.apply_controller.last_result.groups[0].files[0]

    assert outcome.status is ApplyFileOutcomeStatus.APPLIED
    assert outcome.refreshed_source is not None
    assert (outcome.final_path != outcome.source_path) is rename
    assert window.session_state.groups[1].reviewed_files == pending_review

    # The completed file is no longer part of the next batch. Merely changing
    # the highlighted album must still leave inclusion under explicit control.
    window.group_view.setCurrentIndex(window.group_model.index(1, 0))
    window.file_table_view.selectRow(0)
    window.include_selected_button.click()

    assert window.review_apply_button.isEnabled()
    assert window.apply_selected_button.isEnabled()
    assert window.included_file_ids == frozenset((second_id,))
    assert window.group_model.index(0, 0).data() == "Corrected first album"
    assert window.group_model.index(0, 0).data(Qt.ItemDataRole.UserRole) == first.group.group_id
    assert WAVE(outcome.final_path).tags["TALB"].text == ["Corrected first album"]

    complete_apply(qtbot, window, executor, monkeypatch)
    next_result = window.apply_controller.last_result

    assert tuple(item.file_id for group in next_result.groups for item in group.files) == (second_id,)
    assert next_result.groups[0].files[0].status is ApplyFileOutcomeStatus.APPLIED
    assert WAVE(second.group.files[0].path).tags["TIT2"].text == ["Pending second title"]
    assert not window.included_file_ids
    assert "0 included" in window.selection_scope_label.text()
    assert not window.review_apply_button.isEnabled()


# Batch inclusion represents remaining work. A cancelled run may consume its
# verified successes while untouched groups keep their choices for the next run.
def test_cancelled_batch_consumes_only_successes_and_keeps_unstarted_album(qtbot, tmp_path, monkeypatch):
    class CancelAfterFirstWrite:
        def apply_file(self, source, changes, adapter, backup, cancellation, events):
            result = TransactionalFileWriter().apply_file(source, changes, adapter, backup, cancellation, events)
            cancellation.cancel()

            return result

    service = ApplyService(preflight=LocalApplyPreflightInspector(FormatRegistry()), writer=CancelAfterFirstWrite())
    window, executor = scanned_window(qtbot, tmp_path, service=service)
    first, second = window.session_state.groups

    for group in (first, second):
        edit_file(window, group, MetadataField.TITLE, "Corrected title")

    pending_review = window.session_state.groups[1].reviewed_files
    window.set_included_file_ids(frozenset(group.group.files[0].file_id for group in (first, second)))
    complete_apply(qtbot, window, executor, monkeypatch)
    outcomes = tuple(item for group in window.apply_controller.last_result.groups for item in group.files)

    assert outcomes[0].status is ApplyFileOutcomeStatus.APPLIED
    assert outcomes[0].refreshed_source is not None
    assert outcomes[1].status is ApplyFileOutcomeStatus.SKIPPED
    assert window.included_file_ids == frozenset((second.group.files[0].file_id,))
    assert window.session_state.groups[1].reviewed_files == pending_review
    assert window.review_apply_button.isEnabled()

    complete_apply(qtbot, window, executor, monkeypatch)
    assert window.apply_controller.last_result.groups[0].files[0].status is ApplyFileOutcomeStatus.APPLIED
    assert not window.included_file_ids


# No-op files have no write receipt. Their unchanged captured review is the
# evidence that completed inclusion can be removed without losing a newer edit.
def test_completed_batch_also_consumes_unchanged_included_files(qtbot, tmp_path, monkeypatch):
    window, executor = scanned_window(qtbot, tmp_path)
    first, second = window.session_state.groups
    edit_file(window, first, MetadataField.TITLE, "Corrected title")
    second_id = second.group.files[0].file_id
    window.set_session_state(apply_field_decision(
        window.session_state, second.group.group_id, second_id, MetadataField.TITLE,
        FieldDecisionKind.KEEP_EXISTING, window.library_controller.settings.rename,
    ))
    unchanged_review = window.session_state.groups[1].reviewed_files
    window.set_included_file_ids(frozenset((first.group.files[0].file_id, second_id)))
    complete_apply(qtbot, window, executor, monkeypatch)
    outcomes = tuple(item for group in window.apply_controller.last_result.groups for item in group.files)

    assert tuple(item.status for item in outcomes) == (
        ApplyFileOutcomeStatus.APPLIED, ApplyFileOutcomeStatus.NO_CHANGES,
    )
    assert not window.included_file_ids
    assert window.session_state.groups[1].reviewed_files == unchanged_review
    assert not window.apply_selected_button.isEnabled()
