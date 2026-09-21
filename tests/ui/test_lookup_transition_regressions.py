"""Lookup transitions preserve usable evidence and expose the actual language policy."""

from dataclasses import replace

import pytest
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication

from metadata_polisher.application.lookup import LookupService, build_release_search_query
from metadata_polisher.bootstrap import create_application
from metadata_polisher.domain.errors import ProviderErrorCode
from metadata_polisher.domain.metadata import MetadataField
from metadata_polisher.domain.review import FieldDecisionKind
from metadata_polisher.infrastructure.settings import RenameSettings
from metadata_polisher.providers.coordinator import ProviderCoordinator
from metadata_polisher.providers.transport import ProviderTransportError, ProviderTransportErrorContext
from metadata_polisher.session.lookup_editing import change_language, set_search_query_override
from metadata_polisher.session.review_editing import apply_field_decision, undo_last_review_action
from tests.ui.test_scan_workflow import ControlledExecutor
from tests.unit.application.test_lookup_service import LookupFakeProvider
from tests.unit.session.test_lookup_editing import make_selected_session, title_review


def selected_lookup_window(qtbot, tmp_path, *, settings_language="auto"):
    state = make_selected_session()
    candidate = state.groups[0].selected_release.candidate.candidate
    provider = LookupFakeProvider("vgmdb", (candidate,))
    executor = ControlledExecutor()
    _, window = create_application(
        [], executor=executor, settings_file=tmp_path / "settings.json",
        lookup_service=LookupService(ProviderCoordinator((provider,))),
    )
    qtbot.addWidget(window)
    settings = window.library_controller.settings
    window.library_controller.settings = replace(
        settings, matching=replace(settings.matching, preferred_language=settings_language),
    )
    window.set_session_state(state)

    return window, executor, provider


def finish_search(qtbot, window, executor):
    qtbot.mouseClick(window.find_selected_button, Qt.MouseButton.LeftButton)

    with qtbot.waitSignal(window.operation_bridge.completed):
        executor.run_next()


def test_accepting_first_unchanged_search_dialogue_preserves_the_exact_session(qtbot, tmp_path):
    window, _, _ = selected_lookup_window(qtbot, tmp_path)
    original = window.session_state
    QTimer.singleShot(0, lambda: QApplication.activeModalWidget().accept())

    qtbot.mouseClick(window.edit_search_button, Qt.MouseButton.LeftButton)

    assert window.session_state is original
    assert original.groups[0].search_query_override is None


def test_accepting_effective_default_query_does_not_invalidate_release_evidence():
    state = make_selected_session()
    effective_query = build_release_search_query(state.groups[0].group)

    assert set_search_query_override(state, "album", effective_query) is state


@pytest.mark.parametrize("code", [
    ProviderErrorCode.NETWORK_TIMEOUT,
    ProviderErrorCode.ACCESS_DENIED,
    ProviderErrorCode.PROXY_CONNECTION_FAILED,
    ProviderErrorCode.TLS_VERIFICATION_FAILED,
])
def test_failed_repeat_search_keeps_review_mapping_and_undo_and_displays_failure(qtbot, tmp_path, code):
    window, executor, provider = selected_lookup_window(qtbot, tmp_path)
    initial = window.session_state
    file_id = initial.groups[0].group.files[0].file_id
    reviewed = apply_field_decision(
        initial, "album", file_id, MetadataField.TITLE, FieldDecisionKind.USE_PROPOSAL,
        RenameSettings(), proposal_index=1,
    )
    # Retain a human-confirmed mapping as well as the original automatic mapping.
    group = reviewed.groups[0]
    reviewed = replace(reviewed, groups=(replace(group, manual_track_mapping=group.automatic_track_mapping),))
    window.set_session_state(reviewed)
    before = window.session_state.groups[0]
    provider.search_failure = ProviderTransportError(
        code=code, context=ProviderTransportErrorContext(attempt_count=1),
    )

    finish_search(qtbot, window, executor)

    after = window.session_state.groups[0]
    assert after.selected_release is before.selected_release
    assert after.automatic_track_mapping is before.automatic_track_mapping
    assert after.manual_track_mapping is before.manual_track_mapping
    assert after.reviewed_files == before.reviewed_files
    assert after.candidate_lookup is before.candidate_lookup
    assert after.search_failure.lookup_result.failures[0].issue.code is code
    assert code.value in window.workflow_message_label.text()
    assert code.value in window.lookup_controller.candidate_dialog.issues_label.text()
    assert window.lookup_controller.candidate_dialog.table.model().rowCount() == 1
    assert provider.enrich_calls == []
    assert executor.pending == []

    # Closing the failed attempt still leaves the previous usable candidates
    # reachable. The failed request never silently selects a replacement.
    window.lookup_controller.candidate_dialog.close()
    qtbot.mouseClick(window.choose_candidate_button, Qt.MouseButton.LeftButton)
    assert window.lookup_controller.candidate_dialog.table.model().rowCount() == 1
    assert code.value in window.lookup_controller.candidate_dialog.issues_label.text()

    undone = undo_last_review_action(window.session_state, RenameSettings())
    assert title_review(undone).decision is title_review(initial).decision


@pytest.mark.parametrize("successful_empty", [False, True])
def test_successful_repeat_search_replaces_evidence_and_clears_previous_failure(qtbot, tmp_path, successful_empty):
    window, executor, provider = selected_lookup_window(qtbot, tmp_path)
    provider.search_failure = ProviderTransportError(
        code=ProviderErrorCode.NETWORK_TIMEOUT,
        context=ProviderTransportErrorContext(attempt_count=1),
    )
    finish_search(qtbot, window, executor)
    window.lookup_controller.candidate_dialog.close()
    provider.search_failure = None

    if successful_empty:
        provider.candidates = ()
    else:
        provider.candidates = (replace(provider.candidates[0], release_id="replacement"),)

    finish_search(qtbot, window, executor)

    group = window.session_state.groups[0]
    assert group.search_failure is None
    assert group.selected_release is None
    assert group.effective_track_mapping is None
    assert group.candidate_lookup.lookup_result.failures == ()
    identities = tuple(item.candidate.release_id for item in group.lookup_result.candidates)
    assert identities == (() if successful_empty else ("replacement",))


def test_editing_query_clears_the_failed_attempt_with_its_previous_evidence(qtbot, tmp_path):
    window, executor, provider = selected_lookup_window(qtbot, tmp_path)
    provider.search_failure = ProviderTransportError(
        code=ProviderErrorCode.NETWORK_TIMEOUT,
        context=ProviderTransportErrorContext(attempt_count=1),
    )
    finish_search(qtbot, window, executor)
    state = window.session_state
    query = replace(build_release_search_query(state.groups[0].group), album="Another title")

    changed = set_search_query_override(state, "album", query)

    assert changed.groups[0].search_failure is None
    assert changed.groups[0].candidate_lookup is None
    assert changed.groups[0].selected_release is None


def test_inherited_language_is_visible_and_auto_can_be_chosen_directly(qtbot, tmp_path):
    window, executor, provider = selected_lookup_window(qtbot, tmp_path, settings_language="en")
    assert window.language_combo.currentData() is None
    assert "Settings" in window.language_combo.currentText()
    assert "English" in window.language_combo.currentText()

    window.language_combo.setCurrentIndex(window.language_combo.findData("auto"))

    assert window.session_state.groups[0].language_override == "auto"
    finish_search(qtbot, window, executor)
    assert {context.preferred_language for _, context in provider.search_calls} == {"auto"}


def test_returning_to_settings_language_reranks_loaded_variants_and_next_request(qtbot, tmp_path):
    window, executor, provider = selected_lookup_window(qtbot, tmp_path, settings_language="en")
    window.language_combo.setCurrentIndex(window.language_combo.findData("auto"))
    assert title_review(window.session_state).proposals[0].value == "序曲"
    inherited = window.language_combo.findData(None)
    assert inherited >= 0

    window.language_combo.setCurrentIndex(inherited)

    assert window.session_state.groups[0].language_override is None
    assert title_review(window.session_state).proposals[0].value == "Overture"
    finish_search(qtbot, window, executor)
    assert {context.preferred_language for _, context in provider.search_calls} == {"en"}


@pytest.mark.parametrize("override, expected", [(None, "Overture"), ("auto", "序曲"), ("en", "Overture")])
def test_saved_language_changes_rerank_only_groups_inheriting_settings(qtbot, tmp_path, override, expected):
    window, _, provider = selected_lookup_window(qtbot, tmp_path)

    if override is not None:
        window.set_session_state(change_language(window.session_state, "album", override, RenameSettings()))

    def edit_settings():
        dialog = QApplication.activeModalWidget()
        dialog.preferred_language_edit.setText("en")
        dialog.accept()

    QTimer.singleShot(0, edit_settings)
    qtbot.mouseClick(window.settings_button, Qt.MouseButton.LeftButton)

    assert title_review(window.session_state).proposals[0].value == expected
    assert window.session_state.groups[0].language_override == override
    assert provider.search_calls == []

    if override is None:
        assert "English" in window.language_combo.currentText()


def test_failed_settings_save_keeps_inherited_language_and_review_unchanged(qtbot, tmp_path, monkeypatch):
    window, _, _ = selected_lookup_window(qtbot, tmp_path)
    before = window.session_state

    def deny_save(*_args):
        raise PermissionError("Read-only settings location")

    def edit_settings():
        dialog = QApplication.activeModalWidget()
        dialog.preferred_language_edit.setText("en")
        dialog.accept()

    monkeypatch.setattr("metadata_polisher.ui.settings_controller.save_settings", deny_save)
    QTimer.singleShot(0, edit_settings)
    qtbot.mouseClick(window.settings_button, Qt.MouseButton.LeftButton)

    assert window.session_state is before
    assert window.library_controller.settings.matching.preferred_language == "auto"
    assert title_review(window.session_state).proposals[0].value == "序曲"
