"""Selected lookup follows highlighted group identities through a serial queue."""

from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QItemSelectionModel, Qt

from metadata_polisher.application.lookup import LookupService
from metadata_polisher.bootstrap import create_application
from metadata_polisher.domain.errors import ProviderErrorCode
from metadata_polisher.infrastructure.settings import ProvidersSettings
from metadata_polisher.providers.coordinator import ProviderCoordinator
from metadata_polisher.providers.runtime import ProviderRuntime
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


def selected_lookup_window(qtbot, tmp_path):
    candidate = make_candidate("vgmdb", "one", title="First album", media=(make_medium("Opening"),))
    provider = LookupFakeProvider("vgmdb", (candidate,))
    executor = ControlledExecutor()
    service = LookupService(ProviderCoordinator((provider,)))
    _, window = create_application(
        [], executor=executor, settings_file=tmp_path / "settings.json", lookup_service=service,
    )
    qtbot.addWidget(window)
    groups = tuple(
        GroupState(group=replace(
            make_group(make_media_file(f"{name}.flac", title="Opening"), album_title=f"{name} album"),
            group_id=name.casefold(),
        ))
        for name in ("First", "Second", "Third")
    )
    window.set_session_state(
        SessionState(root=Path("library"), groups=groups, selection=GroupSelection("first")),
    )
    return window, executor, provider


def select_groups(window, *rows):
    selection = window.group_view.selectionModel()
    selection.clearSelection()

    for row in rows:
        selection.setCurrentIndex(
            window.group_model.index(row, 0),
            QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows,
        )


def test_selected_lookup_searches_every_highlighted_group_and_leaves_others_alone(qtbot, tmp_path):
    window, executor, provider = selected_lookup_window(qtbot, tmp_path)
    select_groups(window, 0, 2)
    original_unselected = window.session_state.groups[1]
    qtbot.mouseClick(window.find_selected_button, Qt.MouseButton.LeftButton)

    assert len(executor.pending) == 1
    assert tuple(group.group_id for group in window.session_state.active_operation.target_groups) == ("first",)
    assert "Searching group 1 of 2" in window.workflow_message_label.text()

    with qtbot.waitSignal(window.operation_bridge.completed):
        executor.run_next()

    assert len(executor.pending) == 1
    assert tuple(group.group_id for group in window.session_state.active_operation.target_groups) == ("third",)
    assert "Searching group 2 of 2" in window.workflow_message_label.text()

    with qtbot.waitSignal(window.operation_bridge.completed):
        executor.run_next()

    assert executor.pending == []
    assert window.session_state.groups[1] is original_unselected
    assert window.session_state.groups[0].candidate_lookup is not None
    assert window.session_state.groups[2].candidate_lookup is not None
    assert {query.album for query, _ in provider.search_calls} == {"First album", "Third album"}
    assert provider.enrich_calls == []
    assert window.lookup_controller.candidate_dialog is None
    assert "Completed 2 of 2 groups" in window.workflow_message_label.text()
    assert "choose a candidate" in window.workflow_message_label.text()


def test_selected_lookup_skips_unsearchable_highlights_even_when_one_has_focus(qtbot, tmp_path):
    window, executor, provider = selected_lookup_window(qtbot, tmp_path)
    first, second, third = window.session_state.groups
    stale = replace(second, requires_rescan=True)
    blank = GroupState(group=replace(
        make_group(make_media_file("blank.flac"), album_title=None), group_id="blank",
    ))
    window.set_session_state(replace(window.session_state, groups=(first, stale, third, blank)))
    select_groups(window, 0, 2, 3, 1)

    assert window.session_state.selection == GroupSelection("second")
    assert window.find_selected_button.isEnabled()
    qtbot.mouseClick(window.find_selected_button, Qt.MouseButton.LeftButton)
    assert "Skipped 2 groups" in window.workflow_message_label.text()

    for _ in range(2):
        with qtbot.waitSignal(window.operation_bridge.completed):
            executor.run_next()

    assert executor.pending == []
    assert {query.album for query, _ in provider.search_calls} == {"First album", "Third album"}
    assert window.session_state.groups[1] is stale
    assert window.session_state.groups[3] is blank
    assert "Skipped 2 groups" in window.workflow_message_label.text()


# Qt focus can remain on a deselected album. Make focus disagree with highlights
# so a lookup driven only by the current index would query the wrong group.
def test_selected_lookup_does_not_use_an_unhighlighted_current_group(qtbot, tmp_path):
    window, executor, provider = selected_lookup_window(qtbot, tmp_path)
    select_groups(window, 0, 2)
    window.group_view.selectionModel().select(
        window.group_model.index(2, 0),
        QItemSelectionModel.SelectionFlag.Deselect | QItemSelectionModel.SelectionFlag.Rows,
    )

    assert window.session_state.selection == GroupSelection("third")
    assert window.selected_group_ids() == ("first",)
    window.lookup_controller.find_selected()

    with qtbot.waitSignal(window.operation_bridge.completed):
        executor.run_next()

    assert {query.album for query, _ in provider.search_calls} == {"First album"}
    assert window.session_state.groups[2].candidate_lookup is None
    assert window.lookup_controller.candidate_dialog.isVisible()


def test_selected_lookup_with_no_highlights_does_not_search_the_current_group(qtbot, tmp_path):
    window, executor, provider = selected_lookup_window(qtbot, tmp_path)
    window.group_view.selectionModel().clearSelection()
    window.lookup_controller.find_selected()

    assert executor.pending == []
    assert provider.search_calls == []
    assert "Select one or more groups" in window.workflow_message_label.text()


def test_selected_lookup_with_only_stale_groups_reports_skips_without_submission(qtbot, tmp_path):
    window, executor, provider = selected_lookup_window(qtbot, tmp_path)
    window.set_session_state(replace(
        window.session_state,
        groups=tuple(replace(group, requires_rescan=True) for group in window.session_state.groups),
    ))
    select_groups(window, 0, 1)
    window.lookup_controller.find_selected()

    assert executor.pending == []
    assert provider.search_calls == []
    assert "No selected groups can be searched" in window.workflow_message_label.text()
    assert "Skipped 2 groups" in window.workflow_message_label.text()


def test_selected_queue_cancellation_keeps_finished_results_and_stops_later_groups(qtbot, tmp_path):
    window, executor, provider = selected_lookup_window(qtbot, tmp_path)
    select_groups(window, 0, 1, 2)
    window.lookup_controller.find_selected()

    with qtbot.waitSignal(window.operation_bridge.completed):
        executor.run_next()

    completed_calls = tuple(provider.search_calls)
    assert window.operation_controller.cancel_active()

    with qtbot.waitSignal(window.operation_bridge.cancelled):
        executor.run_next()

    assert tuple(provider.search_calls) == completed_calls
    assert executor.pending == []
    assert window.session_state.active_operation is None
    assert window.session_state.groups[0].candidate_lookup is not None
    assert all(group.candidate_lookup is None for group in window.session_state.groups[1:])
    assert "Lookup queue stopped" in window.workflow_message_label.text()
    assert "Completed 1 of 3 groups" in window.workflow_message_label.text()


# A completed Future may still have a queued Qt notification. Cancelling in
# that interval should keep its evidence while preventing queue/chooser activity.
def test_selected_queue_cancel_after_worker_return_retains_result_without_opening_chooser(qtbot, tmp_path):
    window, executor, _ = selected_lookup_window(qtbot, tmp_path)
    select_groups(window, 0, 1)
    window.lookup_controller.find_selected()
    executor.run_next()

    assert window.operation_controller.cancel_active()
    qtbot.waitUntil(lambda: window.session_state.groups[0].candidate_lookup is not None)

    assert executor.pending == []
    assert window.session_state.active_operation is None
    assert window.session_state.groups[1].candidate_lookup is None
    assert window.lookup_controller.candidate_dialog is None
    assert "Lookup queue stopped" in window.workflow_message_label.text()
    assert "Completed 1 of 2 groups" in window.workflow_message_label.text()


def test_selected_queue_uses_only_the_configured_provider_and_retains_issue_counts(qtbot, tmp_path, monkeypatch):
    window, executor, provider = selected_lookup_window(qtbot, tmp_path)
    provider.search_failure = ProviderTransportError(
        code=ProviderErrorCode.NETWORK_TIMEOUT,
        context=ProviderTransportErrorContext(attempt_count=1),
    )
    requested_providers = []
    unselected = LookupFakeProvider("musicbrainz_direct", ())

    def service(_runtime, settings, **_configuration):
        requested_providers.append(settings.selected_provider_id)
        selected = {"vgmdb": provider, "musicbrainz_direct": unselected}[settings.selected_provider_id]
        return LookupService(ProviderCoordinator((selected,)))

    # Exercise production provider selection while replacing its outbound
    # service boundary with offline fakes. A failure must never trigger a
    # request through the other catalogue during any later queued group.
    monkeypatch.setattr(ProviderRuntime, "service", service)
    window.lookup_controller._service = None
    window.library_controller.settings = replace(
        window.library_controller.settings, providers=ProvidersSettings(selected_provider_id="vgmdb"),
    )
    select_groups(window, 0, 1)
    window.lookup_controller.find_selected()

    for _ in range(2):
        with qtbot.waitSignal(window.operation_bridge.completed):
            executor.run_next()

    assert requested_providers == ["vgmdb", "vgmdb"]
    assert unselected.search_calls == []
    assert unselected.enrich_calls == []
    assert "2 groups reported provider issues" in window.workflow_message_label.text()
    window.lookup_controller.close()


def test_selected_queue_failure_preserves_the_worker_error_and_stops_later_groups(qtbot, tmp_path):
    window, executor, provider = selected_lookup_window(qtbot, tmp_path)
    select_groups(window, 0, 1)
    window.lookup_controller.find_selected()
    handle, _work, _events = executor.pending.pop(0)

    with qtbot.waitSignal(window.operation_bridge.failed):
        handle.future.set_exception(RuntimeError("The fixture worker failed."))

    assert executor.pending == []
    assert provider.search_calls == []
    assert window.session_state.active_operation is None
    assert "The fixture worker failed" in window.workflow_message_label.text()
    assert "Completed 0 of 2 groups" in window.workflow_message_label.text()
