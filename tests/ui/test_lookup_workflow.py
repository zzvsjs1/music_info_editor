from dataclasses import replace
from pathlib import Path

import pytest
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication, QInputDialog

from metadata_polisher.application.lookup import LookupService
from metadata_polisher.bootstrap import create_application
from metadata_polisher.domain.errors import ProviderErrorCode
from metadata_polisher.domain.metadata import FieldReadState, MetadataField
from metadata_polisher.providers.coordinator import ProviderCoordinator
from metadata_polisher.providers.transport import ProviderTransportError, ProviderTransportErrorContext
from metadata_polisher.session.state import GroupSelection, GroupState, SessionState
from tests.ui.test_scan_workflow import ControlledExecutor
from tests.unit.application.test_lookup_service import (
    LookupFakeProvider,
    make_candidate,
    make_group,
    make_media_file,
    make_medium,
)


def lookup_window(qtbot, tmp_path):
    group = GroupState(group=make_group(make_media_file("01.flac", title="Opening")))
    candidate = make_candidate("catalogue", "one", title="Album Evidence", media=(make_medium("Opening"),))
    # Equal candidates force an explicit chooser action. The injected failing
    # provider separately checks that partial evidence retains its error context.
    provider = LookupFakeProvider("catalogue", (candidate, replace(candidate, release_id="two")))
    failed = LookupFakeProvider(
        "offline",
        (),
        search_failure=ProviderTransportError(
            code=ProviderErrorCode.NETWORK_TIMEOUT,
            context=ProviderTransportErrorContext(attempt_count=1),
        ),
    )
    executor = ControlledExecutor()
    service = LookupService(ProviderCoordinator((provider, failed)))
    _, window = create_application(
        [],
        executor=executor,
        settings_file=tmp_path / "settings.json",
        lookup_service=service,
    )
    qtbot.addWidget(window)
    window.set_session_state(
        SessionState(root=Path("library"), groups=(group,), selection=GroupSelection(group.group.group_id))
    )
    return window, executor, provider


def test_explicit_lookup_requires_candidate_selection_and_shows_partial_failure(qtbot, tmp_path):
    window, executor, provider = lookup_window(qtbot, tmp_path)
    assert provider.search_calls == []
    qtbot.mouseClick(window.find_selected_button, Qt.MouseButton.LeftButton)
    assert len(executor.pending) == 1
    assert not window.find_selected_button.isEnabled()

    with qtbot.waitSignal(window.operation_bridge.completed):
        executor.run_next()

    group = window.session_state.groups[0]
    assert group.release_ranking.ambiguous
    assert group.selected_release is None
    assert provider.enrich_calls == []
    dialog = window.lookup_controller.candidate_dialog
    assert dialog.isVisible()
    assert dialog.table.model().rowCount() == 2
    assert "offline" in dialog.issues_label.text()
    assert "NETWORK_TIMEOUT" in dialog.issues_label.text()

    dialog.table.selectRow(0)
    qtbot.mouseClick(dialog.choose_button, Qt.MouseButton.LeftButton)
    assert len(executor.pending) == 1

    with qtbot.waitSignal(window.operation_bridge.completed):
        executor.run_next()

    assert len(provider.enrich_calls) == 1
    assert window.session_state.groups[0].selected_release is not None
    assert window.session_state.groups[0].reviewed_files
    qtbot.mouseClick(window.choose_candidate_button, Qt.MouseButton.LeftButton)
    model = window.lookup_controller.candidate_dialog.table.model()
    assert model.index(0, 7).data() == "1 / 1 local tracks"


def test_match_explanation_shows_reason_and_contribution(qtbot, tmp_path):
    window, executor, _ = lookup_window(qtbot, tmp_path)
    qtbot.mouseClick(window.find_selected_button, Qt.MouseButton.LeftButton)

    with qtbot.waitSignal(window.operation_bridge.completed):
        executor.run_next()

    dialog = window.lookup_controller.candidate_dialog
    dialog.table.selectRow(0)
    qtbot.mouseClick(dialog.why_button, Qt.MouseButton.LeftButton)
    explanation = dialog.explanation_dialog
    model = explanation.table.model()
    codes = [model.index(row, 0).data() for row in range(model.rowCount())]
    expected = window.session_state.groups[0].release_ranking.entries[0].result.evidence
    assert codes == [item.code for item in expected]
    assert model.index(0, 2).data() == expected[0].detail


def test_candidate_dialog_distinguishes_empty_results_from_blocked_access(qtbot, tmp_path):
    window, executor, provider = lookup_window(qtbot, tmp_path)
    provider.candidates = ()
    qtbot.mouseClick(window.find_selected_button, Qt.MouseButton.LeftButton)

    with qtbot.waitSignal(window.operation_bridge.completed):
        executor.run_next()

    message = window.lookup_controller.candidate_dialog.issues_label.text()
    assert "catalogue: No matching releases" in message
    assert message.count("offline: NETWORK_TIMEOUT") == 1
    assert "Edit search terms" in message


def test_candidate_dialog_initially_shows_the_disc_column(qtbot, tmp_path):
    window, executor, provider = lookup_window(qtbot, tmp_path)
    provider.candidates = tuple(replace(
        candidate,
        titles=(replace(candidate.titles[0], value="FIRE EMBLEM ENGAGE ORIGINAL SOUNDTRACK"),),
    ) for candidate in provider.candidates)
    qtbot.mouseClick(window.find_selected_button, Qt.MouseButton.LeftButton)

    with qtbot.waitSignal(window.operation_bridge.completed):
        executor.run_next()

    dialog = window.lookup_controller.candidate_dialog
    disc_cell = dialog.table.model().index(0, 4)
    assert dialog.table.visualRect(disc_cell).right() < dialog.table.viewport().width()


def test_new_lookup_uses_current_filename_settings(qtbot, tmp_path):
    from metadata_polisher.infrastructure.settings import RenameSettings

    window, executor, _ = lookup_window(qtbot, tmp_path)
    window.library_controller.settings = replace(
        window.library_controller.settings, rename=RenameSettings(template="%title%", minimum_track_digits=3),
    )
    qtbot.mouseClick(window.find_selected_button, Qt.MouseButton.LeftButton)

    with qtbot.waitSignal(window.operation_bridge.completed):
        executor.run_next()

    dialog = window.lookup_controller.candidate_dialog
    dialog.table.selectRow(0)
    qtbot.mouseClick(dialog.choose_button, Qt.MouseButton.LeftButton)

    with qtbot.waitSignal(window.operation_bridge.completed):
        executor.run_next()

    preview = window.session_state.groups[0].reviewed_files[0].change_set.rename_preview
    assert preview.new_path.name == "Opening.flac"


def test_find_all_only_submits_incomplete_searchable_groups(qtbot, tmp_path):
    window, executor, provider = lookup_window(qtbot, tmp_path)
    first = window.session_state.groups[0]
    source = first.group.files[0]
    complete_source = replace(
        source,
        file_id="complete",
        path=source.path.with_name("complete.flac"),
        read_result=replace(
            source.read_result, field_states={field: FieldReadState.PRESENT for field in MetadataField}
        ),
    )
    complete = GroupState(group=replace(first.group, group_id="complete", files=(complete_source,)))
    stale_source = replace(source, file_id="stale", path=source.path.with_name("stale.flac"))
    stale = GroupState(group=replace(first.group, group_id="stale", files=(stale_source,)), requires_rescan=True)
    window.set_session_state(replace(window.session_state, groups=(first, complete, stale)))
    qtbot.mouseClick(window.find_all_incomplete_button, Qt.MouseButton.LeftButton)
    assert len(executor.pending) == 1

    with qtbot.waitSignal(window.operation_bridge.completed):
        executor.run_next()

    assert executor.pending == []
    assert {query.album for query, _ in provider.search_calls} == {first.group.album_title}
    assert window.session_state.groups[1] is complete
    assert window.session_state.groups[2] is stale


def test_search_terms_override_is_used_only_in_this_session(qtbot, tmp_path):
    window, executor, provider = lookup_window(qtbot, tmp_path)

    def edit_terms():
        dialog = QApplication.activeModalWidget()
        dialog.album_edit.setText("Custom search")
        dialog.accept()

    # Resolve the control before arming the callback so a missing feature fails
    # cleanly without leaving a timer that fires during another test.
    button = window.edit_search_button
    QTimer.singleShot(0, edit_terms)
    qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
    qtbot.mouseClick(window.find_selected_button, Qt.MouseButton.LeftButton)

    with qtbot.waitSignal(window.operation_bridge.completed):
        executor.run_next()

    assert len(provider.search_calls) == 1
    assert provider.search_calls[0][0].album == "Custom search"
    assert not (tmp_path / "settings.json").exists()


@pytest.mark.parametrize("accepted", [False, True])
def test_musicbrainz_setup_uses_an_explicit_contact_before_provider_creation(qtbot, tmp_path, monkeypatch, accepted):
    from metadata_polisher.providers.runtime import ProviderRuntime

    monkeypatch.delenv("METADATA_POLISHER_MUSICBRAINZ_CONTACT", raising=False)
    contacts = []
    fake_service = LookupService(ProviderCoordinator(()))

    def service(_runtime, _settings, musicbrainz_contact, **_configuration):
        contacts.append(musicbrainz_contact)
        return fake_service

    monkeypatch.setattr(ProviderRuntime, "service", service)
    monkeypatch.setattr(QInputDialog, "getText", lambda *args: ("developer@example.test", accepted))
    executor = ControlledExecutor()
    _, window = create_application([], executor=executor, settings_file=tmp_path / "settings.json")
    qtbot.addWidget(window)
    group = GroupState(group=make_group(make_media_file("01.flac", title="Opening")))
    window.set_session_state(
        SessionState(root=Path("library"), groups=(group,), selection=GroupSelection(group.group.group_id))
    )
    qtbot.mouseClick(window.find_selected_button, Qt.MouseButton.LeftButton)

    assert contacts == (["developer@example.test"] if accepted else [])
    assert len(executor.pending) == int(accepted)
    window.lookup_controller.close()


def test_language_override_reranks_provider_variants_without_another_lookup(qtbot, tmp_path):
    from tests.unit.session.test_lookup_editing import make_selected_session, title_review

    window, executor, provider = lookup_window(qtbot, tmp_path)
    initial = make_selected_session()
    window.set_session_state(initial)
    assert title_review(window.session_state).proposals[0].value == "序曲"
    window.language_combo.setCurrentIndex(window.language_combo.findData("en"))

    assert title_review(window.session_state).proposals[0].value == "Overture"
    assert window.session_state.groups[0].language_override == "en"
    assert window.session_state.groups[0].group.files == initial.groups[0].group.files
    assert provider.search_calls == []
    assert executor.pending == []
    assert not (tmp_path / "settings.json").exists()


def test_find_all_cancellation_preserves_finished_group_and_stops_later_work(qtbot, tmp_path):
    window, executor, provider = lookup_window(qtbot, tmp_path)
    first = window.session_state.groups[0]
    additional = []

    for number in (2, 3):
        source = replace(first.group.files[0], file_id=str(number), path=Path("library") / str(number) / "01.flac")
        additional.append(GroupState(group=replace(first.group, group_id=str(number), files=(source,))))

    window.set_session_state(replace(window.session_state, groups=(first, *additional)))
    qtbot.mouseClick(window.find_all_incomplete_button, Qt.MouseButton.LeftButton)

    with qtbot.waitSignal(window.operation_bridge.completed):
        executor.run_next()

    assert window.session_state.groups[0].candidate_lookup is not None
    completed_queries = len(provider.search_calls)
    assert len(executor.pending) == 1
    assert window.operation_controller.cancel_active()

    with qtbot.waitSignal(window.operation_bridge.cancelled):
        executor.run_next()

    assert executor.pending == []
    assert len(provider.search_calls) == completed_queries
    assert window.session_state.groups[0].candidate_lookup is not None
    assert all(group.candidate_lookup is None for group in window.session_state.groups[1:])
    assert window.session_state.active_operation is None
    assert window.find_all_incomplete_button.isEnabled()


# Worker return and UI delivery are distinct boundaries: cancellation between
# them must retain finished evidence without launching another queued lookup.
def test_cancel_after_worker_return_stops_the_next_group_before_queued_completion(qtbot, tmp_path):
    window, executor, _ = lookup_window(qtbot, tmp_path)
    first = window.session_state.groups[0]
    other_source = replace(first.group.files[0], file_id="other", path=Path("library/other/01.flac"))
    other = GroupState(group=replace(first.group, group_id="other", files=(other_source,)))
    window.set_session_state(replace(window.session_state, groups=(first, other)))
    qtbot.mouseClick(window.find_all_incomplete_button, Qt.MouseButton.LeftButton)
    executor.run_next()
    assert window.operation_controller.cancel_active()
    qtbot.waitUntil(lambda: window.session_state.groups[0].candidate_lookup is not None)

    assert executor.pending == []
    assert window.session_state.active_operation is None
    assert window.session_state.groups[1] is other


def test_romanised_option_uses_supplied_japanese_latin_variants(qtbot, tmp_path):
    from metadata_polisher.domain.matching import LocalisedText

    window, executor, provider = lookup_window(qtbot, tmp_path)
    candidate = provider.candidates[0]
    track = candidate.media[0].tracks[0]
    track = replace(track, titles=(LocalisedText("始まり", "ja", "Jpan"), LocalisedText("Hajimari", "ja", "Latn")))
    provider.candidates = (replace(candidate, media=(replace(candidate.media[0], tracks=(track,)),)),)
    qtbot.mouseClick(window.find_selected_button, Qt.MouseButton.LeftButton)

    with qtbot.waitSignal(window.operation_bridge.completed):
        executor.run_next()

    assert window.language_combo.findData("romanised") >= 0
