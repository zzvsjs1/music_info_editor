"""Exercise review parity through semantic commands and the actual QML windows."""

from dataclasses import replace
from pathlib import Path

import pytest
from PySide6.QtCore import QPointF, Qt
from PySide6.QtQuick import QQuickItem, QQuickWindow
from PySide6.QtTest import QTest

from metadata_polisher.application.changes import RenameDecision
from metadata_polisher.domain.metadata import MetadataField, MetadataSnapshot, Position
from metadata_polisher.execution.events import (
    FileStageChanged,
    FileTransactionStage,
    OperationProgress,
    OperationStageChanged,
    ProviderStarted,
)
from metadata_polisher.session.state import OperationKind
from tests.ui.test_quick_backend import backend as backend_fixture
from tests.ui.test_quick_parity import parity_scene as scene_fixture
from tests.ui.test_quick_window import click_item
from tests.unit.session.test_review_editing import field_review, make_selected_session, proposal

backend = pytest.fixture(backend_fixture.__wrapped__)
scene = pytest.fixture(scene_fixture.__wrapped__)


def test_review_starts_without_authorising_any_field_command(backend):
    backend.selectFile("first", False)

    assert backend.selectedFields == []
    assert not backend.canEdit
    assert not backend.beginEdit()

    original = backend.session_state
    backend.reviewAction("clear")
    assert backend.session_state is original
    assert "Select" in backend.fieldDetails


def test_full_values_includes_each_selected_field_in_table_order(backend):
    backend.selectFile("first", False)
    backend.selectField("title")
    backend.selectFieldExtended("album", True, False)

    details = backend.fieldDetails
    assert "Title\n" in details
    assert "Album\n" in details
    assert details.index("Title\n") < details.index("Album\n")
    assert "First title" in details

    backend.selectFieldExtended("title", True, False)
    backend.selectFieldExtended("album", True, False)
    assert "Select" in backend.fieldDetails
    assert "First title" not in backend.fieldDetails


@pytest.mark.parametrize("navigation", ["field", "file", "scope"])
def test_proposal_picker_restores_the_accepted_choice_after_navigation(backend, navigation):
    state = make_selected_session(
        (
            proposal(MetadataField.TITLE, "Choice A", record="a"),
            proposal(MetadataField.TITLE, "Choice B", record="b"),
        )
    )
    group = state.groups[0]
    other = backend.session_state.groups[0]
    second = other.group.files[1]
    other = replace(other, group=replace(other.group, group_id="other", files=(second,)))
    backend.set_state(replace(state, groups=(group, other)))
    selected_id = group.group.files[0].file_id
    backend.selectFile(selected_id, False)
    backend.selectField("title")
    backend.setProposal("1")
    backend.reviewAction("use_candidate")

    if navigation == "field":
        backend.selectField("album")
        backend.selectField("title")
    elif navigation == "file":
        backend.selectGroup("other")
        backend.selectFile("second", False)
        backend.setProposal("0")
        backend.selectGroup("album")
        backend.selectFile(selected_id, False)
    else:
        backend.setReviewScope("library")
        backend.selectField("album")
        backend.selectField("title")
        backend.setReviewScope("selected")

    assert backend.proposalKey == "1"
    backend.reviewAction("use_candidate")
    assert field_review(backend.session_state, MetadataField.TITLE).selected_proposal.value == "Choice B"


def test_disabling_renaming_disables_apply_but_allows_keep_filename(backend):
    backend.selectFile("first", False)
    backend.renameAction(True)
    assert backend.session_state.groups[0].reviewed_files[0].change_set.rename_decision is RenameDecision.APPLY_RENAME

    backend.app_settings = replace(backend.app_settings, rename=replace(backend.app_settings.rename, enabled=False))
    backend.set_state(backend.session_state)
    assert not backend.canRename
    backend.renameAction(False)
    assert backend.session_state.groups[0].reviewed_files[0].change_set.rename_decision is RenameDecision.KEEP_FILENAME


def test_filename_feedback_reports_each_decision_and_batch_count(backend):
    backend.selectFile("first", False)
    backend.renameAction(False)
    assert "Filename kept" in backend.renameValidation

    backend.renameAction(True)
    assert "Rename will apply" in backend.renameValidation

    backend.selectAllFiles()
    assert "Renames included: 1 of 2 files" in backend.renameValidation


def test_progress_keeps_exact_counts_and_provider_item_context(backend):
    assert backend.submit_operation(
        "LOOKUP-port",
        OperationKind.LOOKUP,
        ("album",),
        lambda _token, _events: None,
        lambda state, _result: state,
    )
    backend.bridge.operation_event.emit(ProviderStarted("LOOKUP-port", "musicbrainz"))
    backend.bridge.operation_event.emit(
        FileStageChanged(
            "LOOKUP-port",
            "first",
            Path("library/first.flac"),
            FileTransactionStage.VERIFYING_TEMPORARY,
        )
    )
    backend.bridge.operation_event.emit(OperationProgress("LOOKUP-port", "reviewing", 7, 19))

    assert getattr(backend, "progressCount", "") == "7 / 19 in this stage"
    assert "musicbrainz" in getattr(backend, "progressContext", "")
    assert "first.flac" in backend.progressContext
    assert backend.progress == pytest.approx(7 / 19)

    backend.bridge.operation_event.emit(OperationStageChanged("LOOKUP-port", "finishing"))
    assert backend.progress < 0
    assert backend.progressCount == ""
    assert "musicbrainz" in backend.progressContext
    assert "first.flac" in backend.progressContext


def _manual_window(scene, qtbot, field):
    window, interface = scene
    interface.selectFile(interface.session_state.groups[0].group.files[0].file_id, False)
    interface.selectField(field)
    interface.openReview()
    assert interface.beginEdit()
    editor = window.findChild(QQuickWindow, "manualEditWindow")
    assert editor is not None, "Manual editing must use a separate native window"
    qtbot.waitUntil(editor.isVisible)
    editor.requestActivate()
    assert QTest.qWaitForWindowActive(editor, 2000)
    # Native activation precedes QML's deferred input focus on the next frame.
    # Send keys only after the actual editor control is ready to receive them.
    qtbot.waitUntil(lambda: editor.activeFocusItem() is not None)
    if field not in {"track", "disc"}:
        qtbot.waitUntil(lambda: editor.activeFocusItem().objectName() == "manualEditValue")

    return editor, interface


def test_final_activation_targets_the_hit_field_without_a_prior_click(scene, qtbot):
    main, interface = scene
    interface.selectFile("first", False)
    interface.selectField("title")
    interface.openReview()
    review = main.findChild(QQuickWindow, "metadataReviewWindow")
    table = review.findChild(QQuickItem, "reviewTable")
    original = interface.session_state

    table.cellActivated.emit("missing-field", "", 4)
    assert not interface.editing
    table.cellActivated.emit("date", "", 4)
    qtbot.waitUntil(lambda: interface.editing)
    assert interface.selectedFields == ["date"]
    assert interface.editLabel == "Date"
    assert interface.session_state is original
    assert not interface.executor.pending


@pytest.mark.parametrize("activation", ["pointer", "keyboard"])
def test_read_only_field_activation_opens_copyable_details(scene, qtbot, activation):
    main, interface = scene
    interface.selectFile("first", False)
    interface.selectField("title")
    interface.openReview()
    review = main.findChild(QQuickWindow, "metadataReviewWindow")
    table = review.findChild(QQuickItem, "reviewTable")
    review.requestActivate()
    assert QTest.qWaitForWindowActive(review, 2000)
    original = interface.session_state

    if activation == "pointer":
        table.cellActivated.emit("title", "First title", 2)
    else:
        table.forceActiveFocus()
        qtbot.waitUntil(table.hasActiveFocus)
        QTest.keyClick(review, Qt.Key.Key_Return)

    details = review.findChild(QQuickItem, "fullReviewValue")
    assert details is not None
    qtbot.waitUntil(details.hasActiveFocus)
    assert "First title" in details.property("text")
    assert details.property("readOnly") and details.property("selectByMouse")
    assert not interface.editing and interface.session_state is original
    assert not interface.includedFileIds and not interface.executor.pending
    QTest.keyClick(review, Qt.Key.Key_Escape)
    qtbot.waitUntil(table.hasActiveFocus)
    assert not details.isVisible()
    assert review.isVisible()


def test_manual_text_editor_is_compact_with_standard_cancel_semantics(scene, qtbot):
    editor, interface = _manual_window(scene, qtbot, "date")
    assert editor.title() == "Edit Date"
    assert editor.modality() == Qt.WindowModality.WindowModal
    assert editor.width() <= 520
    assert editor.height() <= 340
    assert editor.findChild(QQuickItem, "saveEditButton").property("text") == "Use value"

    original = interface.session_state
    QTest.keyClick(editor, Qt.Key.Key_Escape)
    qtbot.waitUntil(lambda: not interface.editing)
    assert interface.session_state is original
    assert not editor.isVisible()


def test_position_editor_uses_independent_number_and_total_controls(scene, qtbot):
    _window, interface = scene
    interface.set_state(make_selected_session((), metadata=MetadataSnapshot(track=Position(2, 12))))
    editor, interface = _manual_window(scene, qtbot, "track")
    number = editor.findChild(QQuickItem, "manualEditNumber")
    total = editor.findChild(QQuickItem, "manualEditTotal")
    assert number is not None and total is not None
    assert number.property("value") == 2
    assert total.property("value") == 12

    # A zero component leaves that part of the source position intact.
    number.setProperty("value", 0)
    assert number.property("displayText") == "Keep existing"
    total.setProperty("value", 15)
    click_item(editor, "saveEditButton")
    qtbot.waitUntil(lambda: not interface.editing)
    assert field_review(interface.session_state, MetadataField.TRACK).manual_value == Position(None, 15)
    changes = interface.session_state.groups[0].reviewed_files[0].change_set
    assert changes.final_metadata.track == Position(2, 15)


@pytest.mark.parametrize("accept_with_key", [True, False])
def test_position_editor_accepts_the_number_still_being_typed(scene, qtbot, accept_with_key):
    _window, interface = scene
    interface.set_state(make_selected_session((), metadata=MetadataSnapshot(track=Position(2, 12))))
    editor, interface = _manual_window(scene, qtbot, "track")
    number = editor.findChild(QQuickItem, "manualEditNumber")
    text_input = number.property("contentItem")
    text_input.forceActiveFocus()
    QTest.keyClick(editor, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    QTest.keyClick(editor, Qt.Key.Key_7)

    if accept_with_key:
        QTest.keyClick(editor, Qt.Key.Key_Return)
    else:
        click_item(editor, "saveEditButton")

    qtbot.waitUntil(lambda: not interface.editing)
    assert field_review(interface.session_state, MetadataField.TRACK).manual_value == Position(7, 12)


def test_manual_editor_keeps_invalid_values_editable_without_writes(scene, qtbot):
    editor, interface = _manual_window(scene, qtbot, "date")
    value = editor.findChild(QQuickItem, "manualEditValue")
    original = interface.session_state
    value.setProperty("text", "")
    click_item(editor, "saveEditButton")

    assert interface.editing
    assert interface.editError
    assert interface.session_state is original
    assert editor.isVisible()

    value.setProperty("text", "2026-09-23")
    click_item(editor, "saveEditButton")
    qtbot.waitUntil(lambda: not interface.editing)
    assert field_review(interface.session_state, MetadataField.DATE).manual_value == "2026-09-23"


def test_common_value_editor_names_all_captured_fields_and_files(backend):
    backend.selectAllFiles()
    backend.selectField("title")
    backend.selectFieldExtended("album", True, False)
    assert backend.beginEdit()

    assert "Title" in backend.editTitle
    assert "Album" in backend.editTitle
    assert "2 files" in backend.editTitle


@pytest.mark.parametrize("album", ["First title", "Other album"])
def test_common_value_editor_seeds_only_a_value_shared_by_all_fields(backend, album):
    backend.selectFile("first", False)
    backend.selectField("album")
    assert backend.beginEdit() and backend.commitEdit(album)
    backend.selectFieldExtended("title", True, False)
    snapshot = backend.session_state

    assert backend.beginEdit()
    assert backend.editValue == ("First title" if album == "First title" else "")
    assert backend.session_state is snapshot
    assert not backend.includedFileIds and not backend.executor.pending


def test_manual_editor_focuses_and_selects_the_current_value(scene, qtbot):
    editor, interface = _manual_window(scene, qtbot, "title")
    value = editor.findChild(QQuickItem, "manualEditValue")
    assert value.property("selectedText") == "First title"
    QTest.keyClick(editor, Qt.Key.Key_Z)
    assert value.property("text") == "z"
    QTest.keyClick(editor, Qt.Key.Key_Enter, Qt.KeyboardModifier.ControlModifier)
    qtbot.waitUntil(lambda: not interface.editing)
    assert field_review(interface.session_state, MetadataField.TITLE).manual_value == "z"
    assert not interface.includedFileIds and not interface.executor.pending


def test_long_progress_filename_keeps_cancellation_visible(scene, qtbot):
    root, interface = scene
    assert interface.submit_operation(
        "APPLY-overflow",
        OperationKind.APPLY,
        ("album",),
        lambda _token, _events: None,
        lambda state, _result: state,
    )
    interface.bridge.operation_event.emit(
        FileStageChanged(
            "APPLY-overflow",
            "first",
            Path("W" * 235 + ".flac"),
            FileTransactionStage.VERIFYING_TEMPORARY,
        )
    )
    interface.bridge.operation_event.emit(OperationProgress("APPLY-overflow", "verifying", 1, 20))
    window = root.findChild(QQuickWindow, "operationProgressWindow")
    window.resize(460, 300)
    qtbot.wait(80)
    button = window.findChild(QQuickItem, "progressCancelButton")
    bottom = button.mapToScene(QPointF(0, button.height())).y()
    assert bottom <= window.height(), (bottom, window.height())


def test_position_editor_accepts_locale_grouped_input(scene, qtbot):
    _window, interface = scene
    interface.set_state(make_selected_session((), metadata=MetadataSnapshot(track=Position(2, 1200))))
    editor, interface = _manual_window(scene, qtbot, "track")
    number = editor.findChild(QQuickItem, "manualEditNumber")
    text_input = number.property("contentItem")
    text_input.forceActiveFocus()
    text_input.setProperty("text", "1,000")
    QTest.keyClick(editor, Qt.Key.Key_Return)
    qtbot.waitUntil(lambda: not interface.editing)
    assert field_review(interface.session_state, MetadataField.TRACK).manual_value == Position(1000, 1200)
