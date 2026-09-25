"""QtQuick lookup orchestration over the shared provider and session services."""

import json
import os
from collections import deque
from dataclasses import replace
from typing import TYPE_CHECKING, cast

from PySide6.QtCore import Property, QObject, Signal, Slot

from metadata_polisher.application.lookup import (
    CandidateLookupResult,
    GroupLookupResult,
    LookupService,
    ReleaseMediumIdentity,
    build_release_search_query,
)
from metadata_polisher.application.review import set_manual_track_assignment
from metadata_polisher.domain.errors import ProviderErrorCode
from metadata_polisher.domain.matching import ReleaseSearchQuery
from metadata_polisher.execution.cancellation import CancellationToken, OperationCancelledError
from metadata_polisher.execution.events import OperationEventSink
from metadata_polisher.infrastructure.logging_setup import redact_sensitive_text
from metadata_polisher.infrastructure.session_credentials import CredentialSnapshot
from metadata_polisher.infrastructure.settings import AppSettings, NetworkSettings, ProvidersSettings
from metadata_polisher.matching.release_scoring import RankedReleaseMedium
from metadata_polisher.matching.track_mapping import TrackMappingResult
from metadata_polisher.providers.base import RequestContext
from metadata_polisher.providers.catalogue import provider_label
from metadata_polisher.providers.runtime import ProviderRuntime, _normalise_musicbrainz_contact
from metadata_polisher.rename.template import FilenameRenderPolicy
from metadata_polisher.session.lookup_editing import (
    available_language_choices,
    change_language,
    incomplete_searchable_group_ids,
    is_group_searchable,
    set_search_query_override,
)
from metadata_polisher.session.mapping_editing import apply_manual_track_mapping
from metadata_polisher.session.state import (
    GroupState,
    OperationKind,
    SessionState,
    StateApplicationResult,
    apply_group_lookup_result,
)

if TYPE_CHECKING:
    from metadata_polisher.ui.quick.backend import QuickBackend


def _lookup_notices(result: CandidateLookupResult) -> str:
    """Retain empty-success, request-failure and hydration receipts separately."""
    messages: list[str] = []

    for summary in result.lookup_result.summaries:
        if summary.successful_queries:
            count = summary.candidate_count
            outcome = f"{count} matching releases" if count else "No matching releases"
            messages.append(f"{summary.engine_id}: {outcome} ({summary.successful_queries} searches completed).")

    recovery = {
        ProviderErrorCode.ACCESS_DENIED: "The provider blocked access. Retry later or choose a provider in Settings.",
        ProviderErrorCode.PROXY_AUTHENTICATION_REQUIRED: (
            "Check session proxy credentials in Settings, then test again."
        ),
        ProviderErrorCode.PROXY_CONNECTION_FAILED: "Check the proxy host, port and running proxy service in Settings.",
        ProviderErrorCode.TLS_VERIFICATION_FAILED: "Check the system clock and certificate trust; checks stay enabled.",
        ProviderErrorCode.AUTHENTICATION_REQUIRED: "Check the provider's access requirements in Settings.",
        ProviderErrorCode.RATE_LIMITED: "Wait before retrying this provider; its request limit was reached.",
    }

    for failure in result.lookup_result.failures:
        messages.append(f"{failure.engine_id}: {failure.issue.code.value} — {failure.issue.message}")
        advice = recovery.get(failure.issue.code) if isinstance(failure.issue.code, ProviderErrorCode) else None
        advice = advice or "Retry Find Metadata; check the connection and test it in Settings."
        messages.append(f"{failure.engine_id}: {advice}")

    for notice in result.hydration_notices:
        detail = f"{notice.issue.code.value} — {notice.issue.message}" if notice.issue else ""
        messages.append(f"{' / '.join(notice.candidate_identity)}: {notice.reason_code.value} — {detail}")

    messages.extend(f"{issue.code.value} — {issue.message}" for issue in result.matching_issues)

    if not result.lookup_result.candidates and any(
        summary.successful_queries for summary in result.lookup_result.summaries
    ):
        messages.append("Close this window and use Edit search terms to try another album title or artist.")

    return redact_sensitive_text("\n".join(dict.fromkeys(messages)))


def _duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"

    minutes, remainder = divmod(max(0, round(seconds)), 60)
    return f"{minutes}:{remainder:02d}"


class QuickLookup(QObject):
    """Keep provider ownership and immutable dialogue drafts outside QML views."""

    changed = Signal()

    def __init__(self, host: QuickBackend, service: LookupService | None = None) -> None:
        super().__init__(host)
        self._host = host
        self._service = service
        self._runtime: ProviderRuntime | None = None
        self._closed = False
        self._credential_reset_pending = False
        self._contact = os.environ.get("METADATA_POLISHER_MUSICBRAINZ_CONTACT", "").strip()
        self._contact_visible = False
        self._contact_error = ""
        self._contact_target: tuple[GroupState, ReleaseMediumIdentity | None] | None = None
        self._candidate_group: GroupState | None = None
        self._candidate_visible = False
        self._candidate_key = ""
        self._candidate_sort = ""
        self._candidate_descending = False
        self._why_visible = False
        self._why_text = ""
        self._why_rows: list[dict[str, str]] = []
        self._search_state: SessionState | None = None
        self._search_group_id = ""
        self._search_query: ReleaseSearchQuery | None = None
        self._search_error = ""
        self._mapping_state: SessionState | None = None
        self._mapping_settings: AppSettings | None = None
        self._mapping_group: GroupState | None = None
        self._mapping: TrackMappingResult | None = None
        self._mapping_error = ""
        self._search_ids: set[str] = set()
        self._queue: deque[str] = deque()
        self._bulk = False
        self._queue_total = 0
        self._queue_completed = 0
        self._queue_skipped = 0
        self._queue_issues = 0
        self._stopped_id: str | None = None
        host.changed.connect(self.changed)
        host.bridge.completed.connect(self._completed)
        host.bridge.failed.connect(self._failed)
        host.bridge.cancelled.connect(self._cancelled)
        host.cancellation_requested.connect(self._stop_queue)

    def _idle(self) -> bool:
        return not self._closed and self._host.session_state.active_operation is None

    def _candidate_entry(self) -> RankedReleaseMedium | None:
        group = self._candidate_group
        if group is None or group.candidate_lookup is None:
            return None

        # One release can contain several media. The full identity is retained
        # independently of visual ordering, including after column sorting.
        return next(
            (
                entry
                for entry in group.candidate_lookup.release_ranking.entries
                if json.dumps(entry.identity) == self._candidate_key and entry.medium.tracks
            ),
            None,
        )

    def _candidates(self) -> list[dict[str, object]]:
        group = self._candidate_group
        if group is None or group.candidate_lookup is None:
            return []

        rows: list[dict[str, object]] = []

        for entry in group.candidate_lookup.release_ranking.entries:
            release = entry.release
            mapping = group.effective_track_mapping
            coverage = "Not mapped yet"

            if (
                mapping is not None
                and group.selected_release is not None
                and entry.identity == group.selected_release.identity
            ):
                matched = len(mapping.mappings)
                coverage = f"{matched} / {matched + len(mapping.unmatched_local_file_ids)} local tracks"

            identity = json.dumps(entry.identity)
            rows.append(
                {
                    "id": identity,
                    "key": identity,
                    "engine": release.engine_id,
                    "source": release.source_id,
                    "album": " / ".join(dict.fromkeys(title.value for title in release.titles)) or "Untitled release",
                    "date": release.date or "—",
                    "disc": f"Disc {entry.medium.medium_number or '?'} / {release.disc_total or '?'} · "
                    f"{len(entry.medium.tracks)} loaded tracks",
                    "languages": ", ".join(
                        dict.fromkeys(title.language or title.script or "Unspecified" for title in release.titles)
                    ),
                    "score": f"{entry.result.score:.1f} / 100 · {entry.result.classification.value.capitalize()}",
                    "numericScore": entry.result.score,
                    "coverage": coverage,
                }
            )

        if self._candidate_sort:
            key = self._candidate_sort

            # Scores sort numerically; every other column follows its displayed
            # text. Python's stable sort preserves ranking order for equal values.
            rows.sort(
                key=lambda row: float(cast(float, row["numericScore"])) if key == "score" else str(row[key]).casefold(),
                reverse=self._candidate_descending,
            )

        return rows

    def _notices(self) -> str:
        group = self._candidate_group
        if group is None or group.candidate_lookup is None:
            return ""

        texts = [_lookup_notices(group.candidate_lookup)]
        if group.search_failure is not None and group.search_failure != group.candidate_lookup:
            texts[:0] = [
                "The latest search failed. Previous results and review decisions are retained.",
                _lookup_notices(group.search_failure),
            ]

        return "\n".join(texts)

    def _language_choices(self) -> list[dict[str, str]]:
        return [
            {"value": value or "", "label": label}
            for value, label in available_language_choices(
                self._host._group(),
                self._host.app_settings.matching.preferred_language,
            )
        ]

    def _mapping_rows(self) -> list[dict[str, object]]:
        group, mapping = self._mapping_group, self._mapping
        if group is None or mapping is None:
            return []

        assignments = {item.local_file_id: item for item in mapping.mappings}
        provider_labels = {item["value"]: item["label"] for item in self._mapping_tracks()}
        rows: list[dict[str, object]] = []

        for source in group.group.files:
            assignment = assignments.get(source.file_id)
            evidence = "Unmapped"

            if assignment is not None:
                # Explicit human choices have no automatic matching score. Keep
                # that distinction visible in the dedicated evidence column.
                heading = (
                    "Manual assignment"
                    if "MANUAL_TRACK_ASSIGNMENT" in assignment.reason_codes
                    else (f"{assignment.score:g} / 100 · {assignment.classification.value.capitalize()}")
                )
                evidence = "\n".join((heading, *(f"{item.code} — {item.detail}" for item in assignment.evidence)))

            provider_index = assignment.provider_track_index if assignment is not None else -1
            rows.append(
                {
                    "id": source.file_id,
                    "file": source.path.name,
                    "title": source.read_result.metadata.title or "—",
                    "duration": _duration(source.read_result.stream_info.duration_seconds),
                    "track": provider_index,
                    "assignment": provider_labels[provider_index],
                    "evidence": evidence,
                }
            )

        return rows

    def _mapping_instructions(self) -> str:
        group, mapping = self._mapping_group, self._mapping

        if group is None or mapping is None or group.selected_release is None:
            return ""

        medium = group.selected_release.candidate.candidate.media[mapping.selected_medium_index]
        return (
            f"Disc {medium.medium_number or '?'}: assign a provider track to each local file, "
            "or leave it unmapped. Clear an existing assignment before reusing its track."
        )

    def _mapping_tracks(self) -> list[dict[str, object]]:
        group, mapping = self._mapping_group, self._mapping
        if group is None or mapping is None or group.selected_release is None:
            return []

        medium = group.selected_release.candidate.candidate.media[mapping.selected_medium_index]
        return [
            {"value": -1, "label": "Unmapped"},
            *(
                {
                    "value": index,
                    "label": f"{track.track_number if track.track_number is not None else '?'}. "
                    f"{' / '.join(dict.fromkeys(title.value for title in track.titles)) or 'Untitled track'} · "
                    f"{_duration(track.duration_seconds)}",
                }
                for index, track in enumerate(medium.tracks)
            ),
        ]

    def _can_map(self) -> bool:
        group = self._host._group()
        return bool(
            self._idle()
            and group is not None
            and not group.requires_rescan
            and group.selected_release is not None
            and group.effective_track_mapping is not None
        )

    def _can_choose(self) -> bool:
        group = self._host._group()
        return bool(self._idle() and group is not None and group.candidate_lookup is not None)

    def _language(self) -> str:
        group = self._host._group()
        return (group.language_override or "") if group is not None else ""

    # Lookup windows share the application's preference owner. Small embedded
    # hosts may omit preferences, so their read-only dialogues can still open.
    layoutUi = Property(QObject, lambda self: getattr(self._host, "layoutUi", None), constant=True)

    canFind = Property(
        bool,
        lambda self: (
            self._idle()
            and any(
                group.group.group_id in self._host.selected_group_ids and is_group_searchable(group)
                for group in self._host.session_state.groups
            )
        ),
        notify=changed,
    )
    canFindAll = Property(
        bool,
        lambda self: self._idle() and bool(incomplete_searchable_group_ids(self._host.session_state)),
        notify=changed,
    )
    canChoose = Property(bool, _can_choose, notify=changed)
    canMap = Property(bool, _can_map, notify=changed)
    providerLabel = Property(
        str,
        lambda self: "Lookup provider: " + provider_label(self._host.app_settings.providers.selected_provider_id),
        notify=changed,
    )
    languageChoices = Property(list, _language_choices, notify=changed)
    language = Property(str, _language, notify=changed)
    candidateVisible = Property(bool, lambda self: self._candidate_visible, notify=changed)
    candidates = Property(list, _candidates, notify=changed)
    candidateNotices = Property(str, _notices, notify=changed)
    candidateKey = Property(str, lambda self: self._candidate_key, notify=changed)
    canAcceptCandidate = Property(
        bool,
        lambda self: self._idle() and self._candidate_visible and self._candidate_entry() is not None,
        notify=changed,
    )
    whyVisible = Property(bool, lambda self: self._why_visible, notify=changed)
    whyText = Property(str, lambda self: self._why_text, notify=changed)
    whyRows = Property(list, lambda self: list(self._why_rows), notify=changed)
    searchVisible = Property(bool, lambda self: self._search_state is not None, notify=changed)
    searchAlbum = Property(
        str, lambda self: (self._search_query.album or "") if self._search_query else "", notify=changed
    )
    searchArtists = Property(
        str, lambda self: "; ".join(self._search_query.artists) if self._search_query else "", notify=changed
    )
    searchYear = Property(int, lambda self: (self._search_query.year or 0) if self._search_query else 0, notify=changed)
    searchError = Property(str, lambda self: self._search_error, notify=changed)
    mappingVisible = Property(bool, lambda self: self._mapping_state is not None, notify=changed)
    mappingInstructions = Property(str, _mapping_instructions, notify=changed)
    mappingRows = Property(list, _mapping_rows, notify=changed)
    mappingTracks = Property(list, _mapping_tracks, notify=changed)
    mappingEvidence = Property(
        str,
        lambda self: (
            "\n".join(f"{item.code} — {item.detail}" for item in self._mapping.evidence) if self._mapping else ""
        ),
        notify=changed,
    )
    mappingError = Property(str, lambda self: self._mapping_error, notify=changed)
    contactVisible = Property(bool, lambda self: self._contact_visible, notify=changed)
    contactError = Property(str, lambda self: self._contact_error, notify=changed)

    @Slot()
    def findSelected(self) -> None:
        if not self._idle():
            return

        selected = tuple(
            group for group in self._host.session_state.groups if group.group.group_id in self._host.selected_group_ids
        )
        searchable = tuple(group for group in selected if is_group_searchable(group))
        if not searchable:
            self._host.set_status("Select searchable groups with usable local evidence that do not need a rescan.")
            return

        if len(selected) == 1:
            self._submit(searchable[0])
        else:
            self._start_queue(tuple(group.group.group_id for group in searchable), len(selected) - len(searchable))

    @Slot()
    def findAll(self) -> None:
        if self._idle():
            self._start_queue(incomplete_searchable_group_ids(self._host.session_state), 0)

    def _start_queue(self, group_ids: tuple[str, ...], skipped: int) -> None:
        self._queue = deque(group_ids)
        self._bulk = True
        self._queue_total, self._queue_completed = len(group_ids), 0
        self._queue_skipped, self._queue_issues = skipped, 0
        self._stopped_id = None
        self._next_group()

    def _next_group(self) -> None:
        while self._queue:
            group_id = self._queue.popleft()
            group = next((item for item in self._host.session_state.groups if item.group.group_id == group_id), None)
            if group is None or not is_group_searchable(group):
                self._queue_skipped += 1
                self._queue_total -= 1
                continue

            self._queue_message(f"Searching group {self._queue_completed + 1} of {self._queue_total}.")
            self._submit(group)
            return

        self._bulk = False
        self._queue_message("Lookup finished. Select a group and choose a candidate to review its results.")

    def _queue_message(self, heading: str) -> None:
        self._host.set_status(
            f"{heading} Completed {self._queue_completed} of {self._queue_total} groups. "
            f"{self._queue_skipped} skipped; {self._queue_issues} with provider issues."
        )

    def _submit(self, group: GroupState, identity: ReleaseMediumIdentity | None = None) -> bool:
        if not self._idle() or not is_group_searchable(group):
            return False

        host = self._host
        settings = host.app_settings
        if identity is not None and self._service is None and identity[0] != settings.providers.selected_provider_id:
            host.set_status("Switch Settings back to this candidate's provider, or start a new lookup.")
            return False

        service = self._service or self._configured_service(settings.providers)
        if service is None:
            if self._contact_visible:
                self._contact_target = (group, identity)
            else:
                self._queue.clear()
                self._bulk = False
            return False

        snapshot = host.session_state
        operation_id = host._ids.next_id("LOOKUP")
        language = group.language_override or settings.matching.preferred_language
        rename = settings.rename
        credentials = host.credentials
        generation = credentials.snapshot().generation
        context = RequestContext(operation_id, language, credential_generation=generation)

        def work(token: CancellationToken, events: OperationEventSink) -> GroupLookupResult:
            # Capture provider route, immutable group and settings on the UI thread.
            # The only live owner checked here is the opaque credential generation.
            if credentials.snapshot().generation != generation:
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

            return GroupLookupResult(
                operation_id,
                snapshot.revision,
                snapshot.library_revision,
                group.group.group_id,
                group.revision,
                lookup,
                selected,
            )

        def reduce(state: SessionState, result: object) -> StateApplicationResult:
            if not isinstance(result, GroupLookupResult):
                raise TypeError("Lookup returned an unexpected result.")
            return apply_group_lookup_result(state, result, rename)

        if identity is None:
            self._search_ids.add(operation_id)

        host.openReview()
        submitted = host.submit_operation(operation_id, OperationKind.LOOKUP, (group.group.group_id,), work, reduce)
        if not submitted:
            self._search_ids.discard(operation_id)
            self._queue.clear()
            self._bulk = False
        return submitted

    @Slot(str, object)
    def _completed(self, operation_id: str, result: object) -> None:
        self._settled_runtime()
        if not isinstance(result, GroupLookupResult):
            return

        searching = operation_id in self._search_ids
        self._search_ids.discard(operation_id)
        group = next((item for item in self._host.session_state.groups if item.group.group_id == result.group_id), None)
        if group is None or (
            group.candidate_lookup != result.candidate_lookup and group.search_failure != result.candidate_lookup
        ):
            return

        failures = result.candidate_lookup.lookup_result.failures
        if result.selected_metadata is not None:
            failures += result.selected_metadata.failures

        if operation_id == self._stopped_id:
            self._stopped_id = None
            self._queue_completed += 1
            self._queue_issues += bool(failures)
            self._queue_message("Lookup queue stopped. Completed results remain available.")
            return

        if searching and self._bulk:
            self._queue_completed += 1
            self._queue_issues += bool(failures)
            self._next_group()
            return

        if searching:
            self._open_candidates(group)

        self._host.set_status(
            redact_sensitive_text(
                "\n".join(
                    f"{failure.engine_id}: {failure.issue.code.value} — {failure.issue.message}" for failure in failures
                )
            )
        )

    @Slot(str)
    def _stop_queue(self, operation_id: str, error: object = None) -> None:
        if operation_id not in self._search_ids:
            return

        self._queue.clear()
        if self._bulk:
            self._stopped_id = operation_id

            if error is not None:
                # A worker exception stops the queue before a typed provider
                # receipt exists. Count that failed group and preserve its
                # redacted cause in the primary feedback, as well as the log.
                self._queue_issues += 1
                self._queue_message(
                    "Lookup queue failed. " + redact_sensitive_text(str(error))
                    + " Completed results remain available."
                )
            else:
                self._queue_message("Lookup queue stopped. Completed results remain available.")

        self._bulk = False

    @Slot(str)
    def _cancelled(self, operation_id: str) -> None:
        self._stop_queue(operation_id)
        self._search_ids.discard(operation_id)
        self._settled_runtime()

    @Slot(str, object)
    def _failed(self, operation_id: str, error: object) -> None:
        self._stop_queue(operation_id, error)
        self._search_ids.discard(operation_id)
        self._settled_runtime()

    def _open_candidates(self, group: GroupState) -> None:
        if group.candidate_lookup is not None:
            self._candidate_group = group
            self._candidate_key = ""
            self._candidate_visible = True
            self.changed.emit()

    @Slot()
    def showCandidates(self) -> None:
        group = self._host._group()
        if self._idle() and group is not None:
            self._open_candidates(group)

    @Slot()
    def closeCandidates(self) -> None:
        self._candidate_visible = False
        self.changed.emit()

    @Slot(str)
    def selectCandidate(self, key: str) -> None:
        self._candidate_key = key
        self.changed.emit()

    @Slot(str, bool)
    def sortCandidates(self, column: str, descending: bool) -> None:
        if column in {"engine", "source", "album", "date", "disc", "languages", "score", "coverage"}:
            self._candidate_sort, self._candidate_descending = column, descending
            self.changed.emit()

    @Slot(result=bool)
    def chooseCandidate(self) -> bool:
        captured, entry = self._candidate_group, self._candidate_entry()
        if not self._idle() or not self._candidate_visible or captured is None or entry is None:
            return False

        current = next(
            (group for group in self._host.session_state.groups if group.group.group_id == captured.group.group_id),
            None,
        )
        if (
            current is None
            or current.revision != captured.revision
            or current.candidate_lookup != captured.candidate_lookup
        ):
            self._host.set_status("The group changed. Open the candidate results again.")
            return False

        # Closing first also latches duplicate double-click/activation signals.
        self._candidate_visible = False
        self.changed.emit()
        return self._submit(current, entry.identity)

    @Slot()
    def showWhy(self) -> None:
        entry = self._candidate_entry()
        if entry is not None:
            # Capture the chosen identity's evidence when opening the dialogue;
            # changing a highlighted candidate must not silently retarget it.
            self._why_rows = [
                {
                    "reason": redact_sensitive_text(item.code),
                    "contribution": f"{item.contribution:g}",
                    "detail": redact_sensitive_text(item.detail),
                }
                for item in entry.result.evidence
            ]
            self._why_text = "The score is a deterministic ranking, not a probability.\n\n" + "\n\n".join(
                f"{row['reason']} · {row['contribution']}\n{row['detail']}" for row in self._why_rows
            )
            self._why_visible = True
            self.changed.emit()

    @Slot()
    def closeWhy(self) -> None:
        self._why_visible = False
        self.changed.emit()

    @Slot(result=bool)
    def beginSearch(self) -> bool:
        group = self._host._group()
        if not self._idle() or group is None or group.requires_rescan:
            return False

        # The form edits a captured query, not live tags. Acceptance is valid
        # only while this exact session snapshot remains current.
        self._search_state = self._host.session_state
        self._search_group_id = group.group.group_id
        self._search_query = group.search_query_override or build_release_search_query(group.group)
        self._search_error = ""
        self.changed.emit()
        return True

    @Slot(str, str, int, result=bool)
    def commitSearch(self, album: str, artists: str, year: int) -> bool:
        try:
            if self._search_state is None or self._search_query is None:
                return False

            if not self._idle() or self._host.session_state is not self._search_state:
                raise ValueError("The session changed. Cancel and edit search terms again.")

            if not 0 <= year <= 9999:
                raise ValueError("Choose Any year or a year between 1 and 9999.")

            # Replace only the editable hints, retaining distinctive local titles
            # and other captured evidence used by the shared lookup service.
            query = replace(
                self._search_query,
                album=album.strip() or None,
                artists=tuple(value.strip() for value in artists.split(";") if value.strip()),
                year=year or None,
            )
            state = set_search_query_override(
                self._search_state,
                self._search_group_id,
                query,
                self._host.app_settings.rename,
            )
        except (TypeError, ValueError) as error:
            self._search_error = str(error)
            self.changed.emit()
            return False

        self._search_state = None
        self._host.set_state(state)
        self.changed.emit()
        return True

    @Slot()
    def cancelSearch(self) -> None:
        self._search_state, self._search_query = None, None
        self.changed.emit()

    @Slot(str)
    def setLanguage(self, language: str) -> None:
        group = self._host._group()
        if not self._idle() or group is None or group.requires_rescan:
            return

        try:
            self._host.set_state(
                change_language(
                    self._host.session_state,
                    group.group.group_id,
                    language or None,
                    self._host.app_settings.rename,
                    settings_language=self._host.app_settings.matching.preferred_language,
                )
            )
        except (TypeError, ValueError) as error:
            self._host.set_status(str(error))

    @Slot(result=bool)
    def beginMapping(self) -> bool:
        if not self._can_map():
            return False

        # Keep a private immutable working mapping. Cancel discards it; only OK
        # may publish it against the original session and settings snapshots.
        self._mapping_state = self._host.session_state
        self._mapping_settings = self._host.app_settings
        self._mapping_group = self._host._group()
        assert self._mapping_group is not None
        self._mapping = self._mapping_group.effective_track_mapping
        self._mapping_error = ""
        self.changed.emit()
        return True

    @Slot(str, int, result=bool)
    def assignTrack(self, file_id: str, provider_index: int) -> bool:
        group, mapping = self._mapping_group, self._mapping
        if group is None or mapping is None or group.selected_release is None:
            return False

        try:
            if not self._idle() or self._host.session_state is not self._mapping_state:
                raise ValueError("The session changed. Cancel and review the track mapping again.")

            # The shared service rejects duplicate provider assignments before
            # returning a replacement. A failed edit retains the previous draft.
            self._mapping = set_manual_track_assignment(
                group.group.files,
                group.selected_release.candidate.candidate,
                mapping,
                local_file_id=file_id,
                provider_track_index=None if provider_index == -1 else provider_index,
            )
        except (TypeError, ValueError) as error:
            self._mapping_error = str(error)
            self.changed.emit()
            return False

        self._mapping_error = ""
        self.changed.emit()
        return True

    @Slot(result=bool)
    def commitMapping(self) -> bool:
        state, group, mapping = self._mapping_state, self._mapping_group, self._mapping
        if state is None or group is None or mapping is None:
            return False

        try:
            if (
                not self._idle()
                or self._host.session_state is not state
                or self._host.app_settings != self._mapping_settings
            ):
                raise ValueError("The session or settings changed. Cancel and review the track mapping again.")

            updated = apply_manual_track_mapping(
                state,
                group.group.group_id,
                mapping,
                self._host.app_settings.rename,
                preferred_language=self._host.app_settings.matching.preferred_language,
            )
        except (TypeError, ValueError) as error:
            self._mapping_error = str(error)
            self.changed.emit()
            return False

        self._mapping_state = None
        self._host.set_state(updated)
        self.changed.emit()
        return True

    @Slot()
    def cancelMapping(self) -> None:
        self._mapping_state, self._mapping_group, self._mapping = None, None, None
        self.changed.emit()

    def connection_service(
        self,
        settings: ProvidersSettings,
        *,
        network: NetworkSettings | None = None,
        credentials: CredentialSnapshot | None = None,
    ) -> LookupService | None:
        # Connection tests use the same injected boundary as searches. In
        # fixtures and embedded hosts this must never construct live clients.
        if self._service is not None:
            return self._service

        return self._configured_service(settings, fresh_cache=True, network=network, credentials=credentials)

    def _configured_service(
        self,
        settings: ProvidersSettings,
        *,
        fresh_cache: bool = False,
        network: NetworkSettings | None = None,
        credentials: CredentialSnapshot | None = None,
    ) -> LookupService | None:
        if settings.selected_provider_id is None:
            self._host.set_status("Online lookup is disabled in Settings; local editing remains available.")
            return None

        if settings.selected_provider_id == "musicbrainz_direct" and not self._contact:
            self._contact_visible = True
            self._contact_error = ""
            self._host.set_status("MusicBrainz needs a public project URL or contact email for this session.")
            self.changed.emit()
            return None

        if self._runtime is None:
            self._runtime = ProviderRuntime(cache=self._host.session_state.provider_cache)

        try:
            return self._runtime.service(
                settings,
                musicbrainz_contact=self._contact,
                fresh_cache=fresh_cache,
                network=network if network is not None else self._host.app_settings.network,
                credentials=credentials if credentials is not None else self._host.credentials.snapshot(),
            )
        except ValueError as error:
            self._host.set_status(str(error))
            if settings.selected_provider_id == "musicbrainz_direct":
                self._contact = ""
            return None

    @Slot(str, result=bool)
    def setContact(self, contact: str) -> bool:
        try:
            self._contact = _normalise_musicbrainz_contact(contact)
        except ValueError as error:
            self._contact_error = str(error)
            self.changed.emit()
            return False

        self._contact_error = ""
        self._contact_visible = False
        target, self._contact_target = self._contact_target, None
        self.changed.emit()
        if target is not None:
            group, identity = target
            current = next(
                (item for item in self._host.session_state.groups if item.group.group_id == group.group.group_id), None
            )
            if current == group:
                self._submit(current, identity)
            else:
                self._host.set_status("The group changed. Start Find Metadata again.")
                self._queue.clear()
                self._bulk = False
        else:
            self._host.set_status("Contact saved for this session. Test the selected provider again.")
        return True

    @Slot()
    def clearContactError(self) -> None:
        # The message describes the last submitted value. Once the user edits
        # that value, retire the stale error while keeping the draft intact.
        if not self._contact_error:
            return

        self._contact_error = ""
        self.changed.emit()

    @Slot()
    def cancelContact(self) -> None:
        self._contact_visible, self._contact_target = False, None
        self._queue.clear()
        self._bulk = False
        self._host.set_status("MusicBrainz contact was not provided; no request was sent.")
        self.changed.emit()

    def invalidate_session_credentials(self) -> None:
        self._queue.clear()
        self._bulk = False
        if self._host.session_state.active_operation is not None:
            self._credential_reset_pending = True
            self._host.cancelScan()
        elif self._runtime is not None:
            self._runtime.invalidate_credentials()

    def finish_connection_test(self) -> None:
        if self._runtime is not None:
            self._runtime.finish_connection_test()

    def _settled_runtime(self) -> None:
        if self._runtime is not None:
            if self._credential_reset_pending:
                self._runtime.invalidate_credentials()
                self._credential_reset_pending = False
            self._runtime.finish_operation()

    def close(self) -> None:
        """Call only after the executor settles, so captured HTTP clients stay valid."""
        self._closed = True
        if self._runtime is not None:
            self._runtime.close()
        self._host.credentials.forget()
        self._contact = ""
