"""Explicit lookup commands, candidate selection, and immutable worker results."""

import os
from collections import deque

from PySide6.QtCore import QObject, Slot
from PySide6.QtWidgets import QDialog, QInputDialog

from metadata_polisher.application.lookup import (
    GroupLookupResult,
    LookupService,
    ReleaseMediumIdentity,
    build_release_search_query,
)
from metadata_polisher.execution.cancellation import CancellationToken, OperationCancelledError
from metadata_polisher.execution.events import OperationEventSink
from metadata_polisher.infrastructure.session_credentials import CredentialSnapshot, SessionCredentials
from metadata_polisher.infrastructure.settings import NetworkSettings, ProvidersSettings, RenameSettings
from metadata_polisher.providers.base import RequestContext
from metadata_polisher.providers.catalogue import provider_label
from metadata_polisher.providers.runtime import ProviderRuntime
from metadata_polisher.rename.template import FilenameRenderPolicy
from metadata_polisher.session.lookup_editing import (
    change_language,
    incomplete_searchable_group_ids,
    is_group_searchable,
    set_search_query_override,
)
from metadata_polisher.session.state import GroupSelection, GroupState, OperationKind
from metadata_polisher.ui.dialogs.candidate_dialog import CandidateDialog
from metadata_polisher.ui.dialogs.search_terms_dialog import SearchTermsDialog
from metadata_polisher.ui.main_window import MainWindow


class LookupController(QObject):
    def __init__(self, window: MainWindow, service: LookupService | None) -> None:
        super().__init__(window)
        self._window = window
        self._service = service
        self._runtime: ProviderRuntime | None = None
        self.session_credentials = SessionCredentials()
        self._credential_reset_pending = False
        self._musicbrainz_contact = os.environ.get("METADATA_POLISHER_MUSICBRAINZ_CONTACT", "").strip()
        self.candidate_dialog: CandidateDialog | None = None
        self._search_ids: set[str] = set()
        self._queued_groups: deque[str] = deque()
        self._bulk_lookup = False
        self._queue_total = 0
        self._queue_completed = 0
        self._queue_skipped = 0
        self._queue_issue_count = 0
        self._stopped_queue_operation_id: str | None = None
        window.find_selected_button.clicked.connect(self.find_selected)
        window.find_all_incomplete_button.clicked.connect(self.find_all)
        window.choose_candidate_button.clicked.connect(self.show_candidates)
        window.edit_search_button.clicked.connect(self.edit_search)
        window.language_combo.currentIndexChanged.connect(self._language_changed)
        assert window.operation_bridge is not None
        window.operation_bridge.completed.connect(self._completed)
        window.operation_bridge.cancelled.connect(self._stop_queue)
        window.operation_bridge.failed.connect(self._stop_queue)
        window.operation_bridge.completed.connect(self._finish_credential_reset)
        window.operation_bridge.cancelled.connect(self._finish_credential_reset)
        window.operation_bridge.failed.connect(self._finish_credential_reset)
        window.operation_bridge.completed.connect(self._finish_provider_operation)
        window.operation_bridge.cancelled.connect(self._finish_provider_operation)
        window.operation_bridge.failed.connect(self._finish_provider_operation)
        assert window.operation_controller is not None
        window.operation_controller.cancellation_requested.connect(self._stop_queue)
        self.refresh_provider_label()

    def refresh_provider_label(self) -> None:
        controller = self._window.library_controller

        if controller is not None:
            label = provider_label(controller.settings.providers.selected_provider_id)
            self._window.active_provider_label.setText(f"Lookup provider: {label}")

    def _selected_group(self) -> GroupState | None:
        state = self._window.session_state

        if not isinstance(state.selection, GroupSelection):
            return None

        return next(item for item in state.groups if item.group.group_id == state.selection.group_id)

    @Slot()
    def find_selected(self) -> None:
        state = self._window.session_state

        if state.active_operation is not None:
            return

        # The current row is only the keyboard focus. Capture all highlighted
        # group identities before worker results refresh the selection model.
        selected_ids = frozenset(self._window.selected_group_ids())
        selected = tuple(group for group in state.groups if group.group.group_id in selected_ids)
        searchable = tuple(group for group in selected if is_group_searchable(group))
        skipped = len(selected) - len(searchable)

        if not searchable:
            noun = "group" if skipped == 1 else "groups"
            self._window.workflow_message_label.setText(
                f"No selected groups can be searched. Skipped {skipped} {noun} without usable search evidence "
                "or needing a rescan."
                if selected else "Select one or more groups to find metadata."
            )
            return

        if len(selected) == 1:
            # A single explicit lookup keeps its immediate release chooser.
            self._submit(searchable[0])
            return

        self._start_queue(tuple(group.group.group_id for group in searchable), skipped=skipped)

    def _submit(self, group: GroupState, identity: ReleaseMediumIdentity | None = None) -> None:
        snapshot = self._window.session_state

        if snapshot.active_operation is not None or not is_group_searchable(group):
            return

        controller = self._window.library_controller

        if identity is not None and self._service is None and controller is not None:
            selected_provider = controller.settings.providers.selected_provider_id

            # Do this before constructing a service or asking for contact data.
            # Displaying old evidence is safe; fetching it needs its own adapter.
            if identity[0] != selected_provider:
                self._window.workflow_message_label.setText(
                    "Switch Settings back to this candidate's provider, or start a new lookup. "
                    "Existing reviewed changes remain available."
                )
                return

        service = self._lookup_service()

        if service is None:
            self._queued_groups.clear()
            self._bulk_lookup = False
            return

        operation_id = self._window.operation_ids.next_id("LOOKUP")
        settings = self._window.library_controller.settings if self._window.library_controller else None
        language = group.language_override or (settings.matching.preferred_language if settings else "auto")
        rename = settings.rename if settings else RenameSettings()
        credential_generation = self.session_credentials.snapshot().generation
        context = RequestContext(operation_id, language, credential_generation=credential_generation)

        def work(token: CancellationToken, events: OperationEventSink) -> GroupLookupResult:
            # Credentials can be explicitly replaced before queued work starts.
            # Do not send a request under a different login from the captured one.
            if self.session_credentials.snapshot().generation != credential_generation:
                raise OperationCancelledError("The operation's session credentials were changed.")

            if identity is None:
                lookup = service.search_and_rank_group(
                    group.group,
                    context,
                    query_override=group.search_query_override,
                    disc_number_override=group.disc_number_override,
                    cancellation=token,
                    events=events,
                )
                selected = None
            else:
                assert group.candidate_lookup is not None
                lookup = group.candidate_lookup
                selected = service.select_candidate_metadata(
                    group.group,
                    lookup,
                    identity,
                    context,
                    preferred_language=language,
                    rename_template=rename.template,
                    rename_policy=FilenameRenderPolicy(rename.minimum_track_digits, rename.minimum_disc_digits),
                    cancellation=token,
                    events=events,
                )

            # Carry session, library and group lineage with the plain result so
            # the UI-thread reducer can reject evidence from an obsolete snapshot.
            return GroupLookupResult(
                operation_id,
                snapshot.revision,
                snapshot.library_revision,
                group.group.group_id,
                group.revision,
                lookup,
                selected,
            )

        if identity is None:
            self._search_ids.add(operation_id)

        assert self._window.operation_controller is not None
        self._window.open_metadata_review(select_first=True)
        self._window.operation_controller.start(operation_id, OperationKind.LOOKUP, (group.group.group_id,), work)

    @Slot(str, object)
    def _completed(self, operation_id: str, result: object) -> None:
        if not isinstance(result, GroupLookupResult):
            return

        current = next(
            (item for item in self._window.session_state.groups if item.group.group_id == result.group_id),
            None,
        )

        if current is None or (
            current.candidate_lookup != result.candidate_lookup
            and current.search_failure != result.candidate_lookup
        ):
            return

        failures = result.candidate_lookup.lookup_result.failures

        if result.selected_metadata is not None:
            failures += result.selected_metadata.failures

        if operation_id == self._stopped_queue_operation_id:
            # Cancellation can arrive after the worker has returned but before
            # Qt delivers its result. Retain that completed result without
            # restarting the queue or unexpectedly opening a release chooser.
            self._stopped_queue_operation_id = None
            self._queue_completed += 1
            self._queue_issue_count += bool(failures)
            self._report_queue("Lookup queue stopped.")
            return

        if operation_id in self._search_ids:
            self._search_ids.remove(operation_id)

            if self._bulk_lookup:
                self._queue_completed += 1
                self._queue_issue_count += bool(failures)
                self._next_queued_group()
                return

            # Keep the usable candidates and mapping visible when a repeat
            # request fails; its separate receipt supplies the new failure note.
            self._show_candidates(current)

        self._window.workflow_message_label.setText(
            "\n".join(
                f"{failure.engine_id}: {failure.issue.code.value} — {failure.issue.message}" for failure in failures
            )
        )

    @Slot()
    def show_candidates(self) -> None:
        group = self._selected_group()

        if group is not None:
            self._show_candidates(group)

    def _show_candidates(self, group: GroupState) -> None:
        lookup = group.candidate_lookup

        if lookup is None:
            return

        if self.candidate_dialog is not None:
            self.candidate_dialog.close()

        dialog = CandidateDialog(
            lookup, self._window,
            mapped_identity=group.selected_release.identity if group.selected_release is not None else None,
            mapping=group.effective_track_mapping,
            search_failure=group.search_failure,
        )
        self.candidate_dialog = dialog

        def choose(identity: ReleaseMediumIdentity) -> None:
            current = next(
                (item for item in self._window.session_state.groups if item.group.group_id == group.group.group_id),
                None,
            )

            # A displayed dialog can outlive a rescan or regroup. Require its
            # captured evidence to remain current before starting enrichment.
            if current is not None and current.revision == group.revision and current.candidate_lookup == lookup:
                self._submit(current, identity)

        dialog.candidate_selected.connect(choose)
        dialog.open()

    @Slot()
    def find_all(self) -> None:
        if self._window.session_state.active_operation is not None:
            return

        self._start_queue(incomplete_searchable_group_ids(self._window.session_state))

    def _start_queue(self, group_ids: tuple[str, ...], *, skipped: int = 0) -> None:
        self._queued_groups = deque(group_ids)
        self._bulk_lookup = True
        self._queue_total = len(group_ids)
        self._queue_completed = 0
        self._queue_skipped = skipped
        self._queue_issue_count = 0
        self._stopped_queue_operation_id = None
        self._next_queued_group()

    def _next_queued_group(self) -> None:
        # Resolve each queued ID against the latest session and submit one group
        # only. Its terminal callback advances the serial queue after reduction.
        while self._queued_groups:
            group_id = self._queued_groups.popleft()
            group = next(
                (item for item in self._window.session_state.groups if item.group.group_id == group_id),
                None,
            )

            if group is None or not is_group_searchable(group):
                self._queue_skipped += 1
                self._queue_total -= 1
                continue

            self._report_queue(f"Searching group {self._queue_completed + 1} of {self._queue_total}.")
            self._submit(group)
            return

        self._bulk_lookup = False
        self._report_queue("Lookup finished. Select a group and choose a candidate to review its results.")

    def _report_queue(self, message: str) -> None:
        total_noun = "group" if self._queue_total == 1 else "groups"
        details = [message, f"Completed {self._queue_completed} of {self._queue_total} {total_noun}."]

        if self._queue_skipped:
            skipped_noun = "group" if self._queue_skipped == 1 else "groups"
            details.append(
                f"Skipped {self._queue_skipped} {skipped_noun} without usable search evidence or needing a rescan."
            )

        if self._queue_issue_count:
            issue_noun = "group" if self._queue_issue_count == 1 else "groups"
            details.append(
                f"{self._queue_issue_count} {issue_noun} reported provider issues; candidate results show details."
            )

        self._window.workflow_message_label.setText(" ".join(details))

    def _stop_queue(self, operation_id: str, _error: object = None) -> None:
        if operation_id in self._search_ids:
            self._search_ids.discard(operation_id)
            self._queued_groups.clear()

            if self._bulk_lookup:
                self._stopped_queue_operation_id = operation_id
                message = "Lookup queue stopped. Completed results remain available."

                if _error is not None:
                    # The operation controller has already displayed the
                    # worker error; retain that explanation in the batch report.
                    message = f"Lookup queue stopped. {self._window.workflow_message_label.text()}"

                self._report_queue(message)

            self._bulk_lookup = False

    def _lookup_service(self) -> LookupService | None:
        if self._service is not None:
            return self._service

        controller = self._window.library_controller
        assert controller is not None
        settings = controller.settings.providers
        return self._configured_service(settings)

    def connection_service(
        self, settings: ProvidersSettings, *, network: NetworkSettings | None = None,
        credentials: CredentialSnapshot | None = None,
    ) -> LookupService | None:
        """Capture staged selection for an explicit test, bypassing cached results."""
        # A cached catalogue result cannot demonstrate that the currently staged
        # proxy/login works; an explicit connection test needs fresh transport work.
        return self._configured_service(settings, fresh_cache=True, network=network, credentials=credentials)

    def _configured_service(
        self, settings: ProvidersSettings, *, fresh_cache: bool = False,
        network: NetworkSettings | None = None, credentials: CredentialSnapshot | None = None,
    ) -> LookupService | None:
        if settings.selected_provider_id is None:
            self._window.workflow_message_label.setText(
                "Online lookup is disabled in Settings; local editing remains available.",
            )
            return None

        if settings.selected_provider_id == "musicbrainz_direct" and not self._musicbrainz_contact:
            contact, accepted = QInputDialog.getText(
                self._window,
                "MusicBrainz contact",
                "MusicBrainz requires a public project URL or contact email with requests.\n"
                "Enter the contact to use for this session:",
            )

            if not accepted or not contact.strip():
                self._window.workflow_message_label.setText(
                    "MusicBrainz contact was not provided; no request was sent.",
                )
                return None

            self._musicbrainz_contact = contact.strip()

        if self._runtime is None:
            self._runtime = ProviderRuntime(
                cache=self._window.session_state.provider_cache,
            )

        controller = self._window.library_controller
        assert controller is not None
        route = network if network is not None else controller.settings.network
        credential_snapshot = credentials if credentials is not None else self.session_credentials.snapshot()

        try:
            return self._runtime.service(
                settings, musicbrainz_contact=self._musicbrainz_contact, fresh_cache=fresh_cache,
                network=route, credentials=credential_snapshot,
            )
        except ValueError as error:
            self._musicbrainz_contact = ""
            self._window.workflow_message_label.setText(str(error))
            return None

    def invalidate_session_credentials(self) -> None:
        """Discard client auth after explicit Save/Forget, once active work settles."""
        # Clear future work immediately, but keep the current transport alive
        # until cooperative cancellation reaches its bounded request boundary.
        self._queued_groups.clear()
        self._bulk_lookup = False

        if self._window.session_state.active_operation is not None:
            self._credential_reset_pending = True
            assert self._window.operation_controller is not None
            self._window.operation_controller.cancel_active()
            return

        self._discard_runtime()

    def finish_connection_test(self) -> None:
        if self._runtime is not None:
            self._runtime.finish_connection_test()

    def _finish_credential_reset(self, _operation_id: str, _result: object = None) -> None:
        if self._credential_reset_pending:
            self._discard_runtime()

    def _discard_runtime(self) -> None:
        self._credential_reset_pending = False

        if self._runtime is not None:
            self._runtime.invalidate_credentials()

    def _finish_provider_operation(self, _operation_id: str, _result: object = None) -> None:
        if self._runtime is not None:
            self._runtime.finish_operation()

    def close(self) -> None:
        """Release outbound connections after the serial executor has stopped."""
        if self._runtime is not None:
            self._runtime.close()

        self.session_credentials.forget()
        self._musicbrainz_contact = ""

    @Slot()
    def edit_search(self) -> None:
        group = self._selected_group()
        controller = self._window.library_controller

        if group is None or controller is None or self._window.session_state.active_operation is not None:
            return

        query = group.search_query_override or build_release_search_query(group.group)
        dialog = SearchTermsDialog(query, self._window)

        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._window.set_session_state(
                set_search_query_override(
                    self._window.session_state,
                    group.group.group_id,
                    dialog.query(),
                    controller.settings.rename,
                )
            )

    @Slot(int)
    def _language_changed(self, _index: int) -> None:
        group = self._selected_group()
        controller = self._window.library_controller

        if group is None or controller is None or self._window.session_state.active_operation is not None:
            return

        language = self._window.language_combo.currentData()

        if language is None or isinstance(language, str):
            self._window.set_session_state(
                change_language(
                    self._window.session_state,
                    group.group.group_id,
                    language,
                    controller.settings.rename,
                    settings_language=controller.settings.matching.preferred_language,
                )
            )
