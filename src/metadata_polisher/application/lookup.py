"""Explicit metadata lookup orchestration built from local evidence."""

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date
from enum import StrEnum

from metadata_polisher.application.changes import (
    DEFAULT_RENAME_TEMPLATE,
    ChangeValidationFacts,
    RenameDecision,
)
from metadata_polisher.application.review import ReviewedFileResult, build_selected_file_results
from metadata_polisher.application.search_queries import clean_album_search_fallback
from metadata_polisher.domain.errors import Issue, MatchingErrorCode, ProviderErrorCode
from metadata_polisher.domain.matching import ReleaseCandidate, ReleaseSearchQuery
from metadata_polisher.domain.media import LocalMediaFile
from metadata_polisher.domain.metadata import FieldReadState, MetadataField
from metadata_polisher.domain.release_identity import ReleaseMediumIdentity as ReleaseMediumIdentity
from metadata_polisher.execution.cancellation import CancellationToken, NeverCancelledToken
from metadata_polisher.execution.events import (
    OperationEvent,
    OperationEventSink,
    OperationProgress,
    OperationStageChanged,
    ProviderCompleted,
    ProviderFailed,
    ProviderStarted,
)
from metadata_polisher.matching.normalisation import normalise_for_matching
from metadata_polisher.matching.policy import DEFAULT_MATCHING_POLICY, MatchingPolicy
from metadata_polisher.matching.release_scoring import (
    LocalEvidenceSource,
    MatchClassification,
    ReleaseRanking,
    build_local_release_evidence,
    rank_release_candidates,
)
from metadata_polisher.matching.track_mapping import TrackMappingResult, map_tracks
from metadata_polisher.providers.base import RequestContext
from metadata_polisher.providers.coordinator import (
    CandidateHydrationNotice,
    CoordinatedCandidate,
    ProviderCoordinator,
    ProviderFailure,
    ProviderSearchSummary,
    is_valid_basic_media,
)
from metadata_polisher.rename.template import FilenameRenderPolicy
from metadata_polisher.scanner.grouping import AlbumGroup

_ISO_LIKE_DATE = re.compile(
    r"^(?P<year>[0-9]{4})(?:-(?P<month>[0-9]{2})(?:-(?P<day>[0-9]{2}))?)?$"
)
_DEFAULT_FILENAME_RENDER_POLICY = FilenameRenderPolicy()


class LookupOperationStage(StrEnum):
    """Stable semantic stages for search and selected-candidate orchestration."""

    SEARCHING_PROVIDERS = "searching_providers"
    HYDRATING_MEDIA = "hydrating_media"
    RANKING_CANDIDATES = "ranking_candidates"
    ENRICHING_SELECTION = "enriching_selection"
    MAPPING_TRACKS = "mapping_tracks"
    BUILDING_PROPOSALS = "building_proposals"


class _DiscardEventSink:
    def emit(self, _event: OperationEvent) -> None:
        return


def _clean_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None

    cleaned = " ".join(value.split())

    return cleaned or None


def _present(file: LocalMediaFile, field: MetadataField) -> bool:
    return file.read_result.field_states[field] is FieldReadState.PRESENT


def _deterministic_unique(values: Iterable[str]) -> tuple[str, ...]:
    variants_by_comparison_text: dict[str, set[str]] = {}

    for value in values:
        cleaned = _clean_text(value)

        if cleaned is None:
            continue

        comparison_text = normalise_for_matching(cleaned)
        variants_by_comparison_text.setdefault(comparison_text, set()).add(cleaned)

    # Keep one original spelling per comparison key, then sort independently of
    # scan order so repeated searches generate the same query and cache identity.
    representatives = (
        min(variants, key=lambda value: (value.casefold(), value))
        for variants in variants_by_comparison_text.values()
    )

    return tuple(sorted(representatives, key=lambda value: (value.casefold(), value)))


def _query_artists(files: tuple[LocalMediaFile, ...]) -> tuple[str, ...]:
    # Album artists describe the release as a whole. Track artists are a fallback
    # because a compilation can contain many performers unrelated to its title.
    album_artists = _deterministic_unique(
        artist
        for file in files
        if _present(file, MetadataField.ALBUM_ARTISTS)
        for artist in file.read_result.metadata.album_artists
    )

    if album_artists:
        return album_artists

    return _deterministic_unique(
        artist
        for file in files
        if _present(file, MetadataField.ARTISTS)
        for artist in file.read_result.metadata.artists
    )


def _parse_iso_like_year(value: object) -> int | None:
    cleaned = _clean_text(value)

    if cleaned is None:
        return None

    match = _ISO_LIKE_DATE.fullmatch(cleaned)

    if match is None:
        return None

    year = int(match.group("year"))
    month_text = match.group("month")
    day_text = match.group("day")

    if year == 0:
        return None

    if month_text is None:
        return year

    month = int(month_text)

    if not 1 <= month <= 12:
        return None

    if day_text is None:
        return year

    try:
        date(year, month, int(day_text))
    except ValueError:
        return None

    return year


def _query_year(files: tuple[LocalMediaFile, ...]) -> int | None:
    years = {
        year
        for file in files
        if _present(file, MetadataField.DATE)
        if (year := _parse_iso_like_year(file.read_result.metadata.date)) is not None
    }

    if len(years) != 1:
        return None

    return next(iter(years))


def _positive_integer(value: object) -> int | None:
    if type(value) is not int or value <= 0:
        return None

    return value


def _consensus(values: Iterable[int]) -> int | None:
    # Consensus means exactly one distinct known value, not a majority vote.
    # Conflicting disc numbers remain unknown instead of choosing one arbitrarily.
    distinct_values = set(values)

    if len(distinct_values) != 1:
        return None

    return next(iter(distinct_values))


def _query_disc_hint(files: tuple[LocalMediaFile, ...]) -> int | None:
    tagged_disc_numbers = tuple(
        disc_number
        for file in files
        if _present(file, MetadataField.DISC)
        if (disc_number := _positive_integer(file.read_result.metadata.disc.number)) is not None
    )

    # A real tag tier is authoritative enough that conflicting values must stay
    # unknown; weaker filename evidence must not hide the contradiction.
    if tagged_disc_numbers:
        return _consensus(tagged_disc_numbers)

    filename_disc_numbers = tuple(
        disc_number
        for file in files
        if (disc_number := _positive_integer(file.filename_hints.disc_number)) is not None
    )

    return _consensus(filename_disc_numbers)


def _query_distinctive_titles(files: tuple[LocalMediaFile, ...]) -> tuple[str, ...]:
    title_evidence: list[str] = []

    for file in files:
        title = None

        if _present(file, MetadataField.TITLE):
            title = _clean_text(file.read_result.metadata.title)

        # Filename evidence is secondary on each file. It fills an absent or
        # unreadable tag but never displaces a usable PRESENT title.
        if title is None:
            title = _clean_text(file.filename_hints.probable_title)

        if title is not None:
            title_evidence.append(title)

    titles = _deterministic_unique(title_evidence)

    # Longer titles usually carry more distinguishing words. The secondary text
    # ordering makes equal-length selection independent of input or scan order.
    return tuple(
        sorted(
            titles,
            key=lambda value: (
                -len(normalise_for_matching(value)),
                normalise_for_matching(value),
                value.casefold(),
                value,
            ),
        )[:3]
    )


def build_release_search_query(group: AlbumGroup) -> ReleaseSearchQuery:
    """Build provider-neutral search evidence without consulting folder names."""
    album = _clean_text(group.album_title)

    return ReleaseSearchQuery(
        album=album,
        artists=_query_artists(group.files),
        year=_query_year(group.files),
        disc_hint=_query_disc_hint(group.files),
        local_track_count=len(group.files),
        distinctive_titles=_query_distinctive_titles(group.files),
    )


def build_release_search_queries(group: AlbumGroup) -> tuple[ReleaseSearchQuery, ...]:
    """Build deterministic strict-to-broad strategies from the same evidence."""
    primary = build_release_search_query(group)
    queries = [primary]
    # Relax request filters without editing the evidence later used for scoring.
    # A broad search can discover a candidate without making it a stronger match.
    relaxed = replace(
        primary,
        year=None,
        disc_hint=None,
        local_track_count=0,
    )

    if (relaxed.album is not None or relaxed.artists) and relaxed not in queries:
        queries.append(relaxed)

    # Album-only is a conservative fallback. Distinctive titles remain
    # attached as ranking evidence but are never relabelled as an album title.
    if primary.album is not None:
        album_only = replace(relaxed, artists=())

        if album_only not in queries:
            queries.append(album_only)

        # Keep the exact tagged title in all existing searches. A final clean
        # title can find the release when local catalogue/disc annotations are
        # absent from the provider, without changing metadata or ranking facts.
        clean_album = clean_album_search_fallback(primary.album)

        if clean_album is not None:
            queries.append(replace(album_only, album=clean_album))

    return tuple(queries)


def _result_tuple[T](name: str, values: object, item_type: type[T]) -> tuple[T, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise TypeError(f"{name} must be an ordered sequence")

    copied = tuple(values)

    if any(not isinstance(value, item_type) for value in copied):
        raise TypeError(f"{name} must contain only {item_type.__name__} values")

    return copied


@dataclass(frozen=True)
class LookupSearchResult:
    """Group-labelled result of one explicit provider search."""

    group_id: str
    queries: tuple[ReleaseSearchQuery, ...]
    candidates: tuple[CoordinatedCandidate, ...]
    failures: tuple[ProviderFailure, ...]
    summaries: tuple[ProviderSearchSummary, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.group_id, str):
            raise TypeError("group_id must be a string")

        if not self.group_id:
            raise ValueError("group_id must not be empty")

        queries = _result_tuple("queries", self.queries, ReleaseSearchQuery)

        if not queries:
            raise ValueError("queries must contain at least one search strategy")

        object.__setattr__(self, "queries", queries)
        object.__setattr__(
            self,
            "candidates",
            _result_tuple("candidates", self.candidates, CoordinatedCandidate),
        )
        object.__setattr__(
            self,
            "failures",
            _result_tuple("failures", self.failures, ProviderFailure),
        )
        object.__setattr__(
            self, "summaries", _result_tuple("summaries", self.summaries, ProviderSearchSummary),
        )

    @property
    def query(self) -> ReleaseSearchQuery:
        """Return the primary strategy for compact displays and compatibility."""
        return self.queries[0]


@dataclass(frozen=True)
class LookupEnrichmentResult:
    """Result of explicitly enriching one selected coordinated candidate."""

    candidate: CoordinatedCandidate | None
    failures: tuple[ProviderFailure, ...]

    def __post_init__(self) -> None:
        if self.candidate is not None and not isinstance(self.candidate, CoordinatedCandidate):
            raise TypeError("candidate must be a CoordinatedCandidate or None")

        failures = _result_tuple("failures", self.failures, ProviderFailure)

        if (self.candidate is None) == (not failures):
            raise ValueError("enrichment must contain either one candidate or at least one failure")

        object.__setattr__(self, "failures", failures)


class LookupRankingIssue(StrEnum):
    """Shared lineage failures, independent of a caller's diagnostic wording."""

    DUPLICATE_CANDIDATES = "lookup candidate identities must be unique"
    FOREIGN_RELEASE = "every ranking entry release must equal its lookup candidate"
    FOREIGN_MEDIUM = "every ranking entry medium must equal its indexed lookup medium"
    INCOMPLETE_RANKING = "ranking identities must contain every scoreable lookup medium exactly once"


def lookup_ranking_issue(
    lookup_result: LookupSearchResult,
    release_ranking: ReleaseRanking,
) -> LookupRankingIssue | None:
    """Check the same candidate/medium lineage at lookup and session boundaries.

    Catalogue IDs alone do not establish ownership: ranked releases and media
    must equal the retained candidate values, and every scoreable medium must
    appear exactly once. Keep the checks ordered so the first failure is stable.
    """
    candidates_by_identity: dict[tuple[str, str, str], ReleaseCandidate] = {}

    for item in lookup_result.candidates:
        retained_candidate = item.candidate
        identity = (retained_candidate.engine_id, retained_candidate.source_id, retained_candidate.release_id)

        if identity in candidates_by_identity:
            return LookupRankingIssue.DUPLICATE_CANDIDATES

        candidates_by_identity[identity] = retained_candidate

    for entry in release_ranking.entries:
        candidate = candidates_by_identity.get(entry.identity.release_identity)

        if candidate is None or entry.release != candidate:
            return LookupRankingIssue.FOREIGN_RELEASE

        if entry.medium_index >= len(candidate.media) or entry.medium != candidate.media[entry.medium_index]:
            return LookupRankingIssue.FOREIGN_MEDIUM

    ranking_identities = release_ranking.identities
    expected_ranking_identities = {
        ReleaseMediumIdentity(candidate.engine_id, candidate.source_id, candidate.release_id, medium_index)
        for candidate in candidates_by_identity.values()
        if is_valid_basic_media(candidate)
        for medium_index, _medium in enumerate(candidate.media)
    }

    if (
        len(ranking_identities) != len(set(ranking_identities))
        or set(ranking_identities) != expected_ranking_identities
    ):
        return LookupRankingIssue.INCOMPLETE_RANKING

    return None


@dataclass(frozen=True)
class CandidateLookupResult:
    """State-independent hydrated search and deterministic release ranking."""

    lookup_result: LookupSearchResult
    release_ranking: ReleaseRanking
    hydration_notices: tuple[CandidateHydrationNotice, ...]
    matching_issues: tuple[Issue, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.lookup_result, LookupSearchResult):
            raise TypeError("lookup_result must be a LookupSearchResult")

        if not isinstance(self.release_ranking, ReleaseRanking):
            raise TypeError("release_ranking must be a ReleaseRanking")

        hydration_notices = _result_tuple(
            "hydration_notices",
            self.hydration_notices,
            CandidateHydrationNotice,
        )
        matching_issues = _result_tuple("matching_issues", self.matching_issues, Issue)
        candidate_identities = tuple(
            (
                item.candidate.engine_id,
                item.candidate.source_id,
                item.candidate.release_id,
            )
            for item in self.lookup_result.candidates
        )

        ranking_issue = lookup_ranking_issue(self.lookup_result, self.release_ranking)

        if ranking_issue is not None:
            raise ValueError(ranking_issue.value)

        notice_identities = tuple(
            notice.candidate_identity for notice in hydration_notices
        )

        if len(notice_identities) != len(set(notice_identities)):
            raise ValueError("hydration notice identities must be unique")

        expected_notice_identities = tuple(
            identity
            for identity, candidate in zip(
                candidate_identities,
                self.lookup_result.candidates,
                strict=True,
            )
            if not is_valid_basic_media(candidate.candidate)
        )

        if notice_identities != expected_notice_identities:
            raise ValueError(
                "hydration notices must follow all unscoreable lookup candidates in lookup order"
            )

        if any(not isinstance(issue.code, MatchingErrorCode) for issue in matching_issues):
            raise ValueError("matching_issues must contain only MatchingErrorCode issues")

        if matching_issues != _matching_issues(self.lookup_result, self.release_ranking):
            raise ValueError("matching_issues must match the deterministic lookup outcome")

        object.__setattr__(
            self,
            "hydration_notices",
            hydration_notices,
        )
        object.__setattr__(
            self,
            "matching_issues",
            matching_issues,
        )


@dataclass(frozen=True)
class SelectedMetadataResult:
    """State-free result of one explicit selected-release enrichment and mapping."""

    candidate_lookup: CandidateLookupResult
    selected_identity: ReleaseMediumIdentity
    selected_candidate: CoordinatedCandidate | None
    track_mapping: TrackMappingResult | None
    reviewed_files: tuple[ReviewedFileResult, ...]
    failures: tuple[ProviderFailure, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.candidate_lookup, CandidateLookupResult):
            raise TypeError("candidate_lookup must be a CandidateLookupResult")

        identity = self.selected_identity

        if (
            not isinstance(identity, tuple)
            or len(identity) != 4
            or any(not isinstance(value, str) or not value for value in identity[:3])
            or type(identity[3]) is not int
            or identity[3] < 0
        ):
            raise TypeError("selected_identity must contain three strings and a non-negative index")

        # Public callers may still supply the original four-tuple. Normalise it
        # only after the existing shape checks, preserving their error boundary.
        identity = ReleaseMediumIdentity(*identity)

        if identity not in self.candidate_lookup.release_ranking.identities:
            raise ValueError("selected_identity must belong to the candidate ranking")

        failures = _result_tuple("failures", self.failures, ProviderFailure)
        reviewed_files = _result_tuple(
            "reviewed_files",
            self.reviewed_files,
            ReviewedFileResult,
        )

        if self.selected_candidate is None:
            if self.track_mapping is not None:
                raise ValueError("a failed selection cannot contain a track mapping")

            if not failures:
                raise ValueError("a failed selection must contain at least one provider failure")

            if reviewed_files:
                raise ValueError("a failed selection cannot contain reviewed files")
        else:
            if not isinstance(self.selected_candidate, CoordinatedCandidate):
                raise TypeError("selected_candidate must be a CoordinatedCandidate or None")

            if failures:
                raise ValueError("a successful selection cannot contain provider failures")

            if not isinstance(self.track_mapping, TrackMappingResult):
                raise TypeError("a successful selection must contain a TrackMappingResult")

            candidate = self.selected_candidate.candidate

            if (
                (candidate.engine_id, candidate.source_id, candidate.release_id)
                != identity.release_identity
            ):
                raise ValueError("selected candidate identity must match selected_identity")

            if identity.medium_index >= len(candidate.media):
                raise ValueError("selected medium index is outside the enriched candidate")

            if self.track_mapping.selected_medium_index != identity.medium_index:
                raise ValueError("track mapping must use the selected medium index")

            if (
                self.track_mapping.selected_medium_number
                != candidate.media[identity.medium_index].medium_number
            ):
                raise ValueError("track mapping must use the selected medium number")

            expected_file_ids = {
                *(item.local_file_id for item in self.track_mapping.mappings),
                *self.track_mapping.unmatched_local_file_ids,
            }
            reviewed_file_ids = tuple(item.file_id for item in reviewed_files)

            if (
                len(reviewed_file_ids) != len(set(reviewed_file_ids))
                or set(reviewed_file_ids) != expected_file_ids
            ):
                raise ValueError("reviewed_files must contain every mapped local file exactly once")

        object.__setattr__(self, "failures", failures)
        object.__setattr__(self, "reviewed_files", reviewed_files)
        object.__setattr__(self, "selected_identity", identity)


@dataclass(frozen=True)
class GroupLookupResult:
    """State-free lookup worker result with an explicit captured lineage."""

    operation_id: str
    base_session_revision: int
    base_library_revision: int
    group_id: str
    base_group_revision: int
    candidate_lookup: CandidateLookupResult
    selected_metadata: SelectedMetadataResult | None = None

    def __post_init__(self) -> None:
        for name in ("operation_id", "group_id"):
            value = getattr(self, name)

            if not isinstance(value, str):
                raise TypeError(f"{name} must be a string")

            if not value.strip():
                raise ValueError(f"{name} must be non-blank")

        for name in (
            "base_session_revision",
            "base_library_revision",
            "base_group_revision",
        ):
            value = getattr(self, name)

            if type(value) is not int:
                raise TypeError(f"{name} must be an integer")

            if value < 0:
                raise ValueError(f"{name} cannot be negative")

        if self.base_library_revision > self.base_session_revision:
            raise ValueError("base_library_revision cannot exceed base_session_revision")

        if self.base_group_revision > self.base_session_revision:
            raise ValueError("base_group_revision cannot exceed base_session_revision")

        if not isinstance(self.candidate_lookup, CandidateLookupResult):
            raise TypeError("candidate_lookup must be a CandidateLookupResult")

        if self.candidate_lookup.lookup_result.group_id != self.group_id:
            raise ValueError("candidate lookup group_id must match group_id")

        if self.selected_metadata is None:
            if any(
                provenance.operation_id != self.operation_id
                for candidate in self.candidate_lookup.lookup_result.candidates
                for provenance in candidate.provenance
            ):
                raise ValueError("candidate provenance operation must match a search result")
        else:
            if not isinstance(self.selected_metadata, SelectedMetadataResult):
                raise TypeError("selected_metadata must be a SelectedMetadataResult or None")

            if self.selected_metadata.candidate_lookup != self.candidate_lookup:
                raise ValueError("selected metadata must retain the exact candidate lookup")


def _matching_issues(
    lookup_result: LookupSearchResult,
    release_ranking: ReleaseRanking,
) -> tuple[Issue, ...]:
    if not lookup_result.candidates:
        return (
            Issue(
                code=MatchingErrorCode.NO_CANDIDATE,
                message="No metadata provider returned a release candidate.",
            ),
        )

    if (
        not release_ranking.entries
        or release_ranking.entries[0].result.classification is MatchClassification.LOW
    ):
        return (
            Issue(
                code=MatchingErrorCode.INSUFFICIENT_EVIDENCE,
                message="No candidate has enough real media evidence for a reliable match.",
            ),
        )

    if release_ranking.ambiguous:
        return (
            Issue(
                code=MatchingErrorCode.AMBIGUOUS_CANDIDATE,
                message="More than one candidate remains plausible; choose one explicitly.",
            ),
        )

    return ()


def _selected_medium_structure_matches(
    basic: ReleaseCandidate,
    enriched: ReleaseCandidate,
    medium_index: int,
) -> bool:
    """Allow added detail while preserving the exact release/track structure scored."""
    if not 0 <= medium_index < len(basic.media):
        return False

    composer_free_enriched = replace(
        enriched,
        media=tuple(
            replace(
                medium,
                tracks=tuple(replace(track, composers=(), composer_credits=()) for track in medium.tracks),
            )
            for medium in enriched.media
        ),
    )

    return composer_free_enriched == basic


class LookupService:
    """Expose provider work only through explicit synchronous use-case methods."""

    def __init__(self, coordinator: ProviderCoordinator) -> None:
        self._coordinator = coordinator

    def search_group(
        self,
        group: AlbumGroup,
        context: RequestContext,
    ) -> LookupSearchResult:
        """Search providers for one group only when the caller requests it."""
        queries = build_release_search_queries(group)
        provider_result = self._coordinator.search_release_queries(queries, context)

        return LookupSearchResult(
            group_id=group.group_id,
            queries=queries,
            candidates=provider_result.candidates,
            failures=provider_result.failures,
            summaries=provider_result.summaries,
        )

    def enrich_selected(
        self,
        selected: CoordinatedCandidate,
        context: RequestContext,
    ) -> LookupEnrichmentResult:
        """Fetch expensive detail for the selected candidate and no others."""
        provider_result = self._coordinator.enrich_release(selected, context)

        return LookupEnrichmentResult(
            candidate=provider_result.candidate,
            failures=provider_result.failures,
        )

    def test_connection(self, context: RequestContext) -> tuple[ProviderFailure, ...]:
        """Explicitly test a real catalogue response, accepting a valid empty list."""
        result = self._coordinator.search_releases(
            ReleaseSearchQuery("Metadata Polisher connection test", (), None, None, 0, ()), context,
        )
        return result.failures

    def search_and_rank_group(
        self,
        group: AlbumGroup,
        context: RequestContext,
        *,
        query_override: ReleaseSearchQuery | None = None,
        disc_number_override: int | None = None,
        per_engine_limit: int = 5,
        policy: MatchingPolicy = DEFAULT_MATCHING_POLICY,
        cancellation: CancellationToken | None = None,
        events: OperationEventSink | None = None,
    ) -> CandidateLookupResult:
        """Search, media-hydrate, and rank without selecting a candidate."""
        if not isinstance(policy, MatchingPolicy):
            raise TypeError("policy must be a MatchingPolicy")

        if query_override is not None and not isinstance(query_override, ReleaseSearchQuery):
            raise TypeError("query_override must be a ReleaseSearchQuery or None")

        if disc_number_override is not None:
            if type(disc_number_override) is not int:
                raise TypeError("disc_number_override must be an integer or None")

            if disc_number_override <= 0:
                raise ValueError("disc_number_override must be greater than zero")

        active_cancellation = cancellation if cancellation is not None else NeverCancelledToken()
        active_events = events if events is not None else _DiscardEventSink()
        context = replace(context, cancellation=active_cancellation, events=active_events)
        active_cancellation.raise_if_cancelled()

        queries = (
            (query_override,)
            if query_override is not None
            else build_release_search_queries(group)
        )
        search_total = self._coordinator.provider_count * len(queries)
        search_current = 0
        active_events.emit(
            OperationStageChanged(
                context.operation_id,
                LookupOperationStage.SEARCHING_PROVIDERS,
            )
        )
        active_events.emit(
            OperationProgress(
                context.operation_id,
                LookupOperationStage.SEARCHING_PROVIDERS,
                0,
                search_total,
            )
        )

        def provider_started(engine_id: str) -> None:
            active_events.emit(ProviderStarted(context.operation_id, engine_id))

        def search_call_finished(engine_id: str, issue: Issue | None) -> None:
            nonlocal search_current

            if issue is None:
                active_events.emit(ProviderCompleted(context.operation_id, engine_id))
            else:
                active_events.emit(ProviderFailed(context.operation_id, engine_id, issue))

            search_current += 1
            active_events.emit(
                OperationProgress(
                    context.operation_id,
                    LookupOperationStage.SEARCHING_PROVIDERS,
                    search_current,
                    search_total,
                )
            )

        provider_result = self._coordinator.search_release_queries(
            queries,
            context,
            check_cancelled=active_cancellation.raise_if_cancelled,
            on_call_started=provider_started,
            on_call_finished=search_call_finished,
        )
        active_cancellation.raise_if_cancelled()
        search_result = LookupSearchResult(
            group_id=group.group_id,
            queries=queries,
            candidates=provider_result.candidates,
            failures=provider_result.failures,
            summaries=provider_result.summaries,
        )

        if search_current < search_total:
            # Skipped fallbacks still complete the bounded search stage. The
            # summaries retain the actual number of requests for diagnostics.
            active_events.emit(OperationProgress(
                context.operation_id, LookupOperationStage.SEARCHING_PROVIDERS,
                search_total, search_total,
            ))

        hydration_total = len(search_result.candidates)
        hydration_current = 0
        active_events.emit(
            OperationStageChanged(
                context.operation_id,
                LookupOperationStage.HYDRATING_MEDIA,
            )
        )
        active_events.emit(
            OperationProgress(
                context.operation_id,
                LookupOperationStage.HYDRATING_MEDIA,
                0,
                hydration_total,
            )
        )

        def hydration_call_finished(engine_id: str, issue: Issue | None) -> None:
            nonlocal hydration_current

            if issue is None:
                active_events.emit(ProviderCompleted(context.operation_id, engine_id))
            else:
                active_events.emit(ProviderFailed(context.operation_id, engine_id, issue))

            hydration_current += 1
            active_events.emit(
                OperationProgress(
                    context.operation_id,
                    LookupOperationStage.HYDRATING_MEDIA,
                    hydration_current,
                    hydration_total,
                )
            )

        # Search summaries may omit tracks. Load basic medium structure before
        # scoring counts and durations; expensive selected-release detail follows
        # only after the user chooses a candidate.
        media_result = self._coordinator.load_release_media(
            search_result.candidates,
            context,
            per_engine_limit=per_engine_limit,
            check_cancelled=active_cancellation.raise_if_cancelled,
            on_call_started=provider_started,
            on_call_finished=hydration_call_finished,
        )
        active_cancellation.raise_if_cancelled()

        if hydration_current < hydration_total:
            active_events.emit(
                OperationProgress(
                    context.operation_id,
                    LookupOperationStage.HYDRATING_MEDIA,
                    hydration_total,
                    hydration_total,
                )
            )

        active_cancellation.raise_if_cancelled()

        hydrated_lookup = LookupSearchResult(
            group_id=search_result.group_id,
            queries=search_result.queries,
            candidates=media_result.candidates,
            failures=search_result.failures,
            summaries=search_result.summaries,
        )
        # Retain incomplete candidates for diagnostics, but never turn an absent
        # track list into apparently valid evidence for the release scorer.
        scoreable = tuple(
            item.candidate
            for item in hydrated_lookup.candidates
            if is_valid_basic_media(item.candidate)
        )
        active_events.emit(
            OperationStageChanged(
                context.operation_id,
                LookupOperationStage.RANKING_CANDIDATES,
            )
        )
        active_events.emit(
            OperationProgress(
                context.operation_id,
                LookupOperationStage.RANKING_CANDIDATES,
                0,
                1,
            )
        )
        active_cancellation.raise_if_cancelled()
        local_evidence = build_local_release_evidence(group)

        if disc_number_override is not None:
            local_evidence = replace(
                local_evidence,
                disc_number=disc_number_override,
                disc_number_source=LocalEvidenceSource.MANUAL_OVERRIDE,
            )

        release_ranking = rank_release_candidates(
            local_evidence,
            scoreable,
            policy,
        )
        active_cancellation.raise_if_cancelled()
        active_events.emit(
            OperationProgress(
                context.operation_id,
                LookupOperationStage.RANKING_CANDIDATES,
                1,
                1,
            )
        )
        active_cancellation.raise_if_cancelled()

        return CandidateLookupResult(
            lookup_result=hydrated_lookup,
            release_ranking=release_ranking,
            hydration_notices=media_result.notices,
            matching_issues=_matching_issues(hydrated_lookup, release_ranking),
        )

    def select_candidate_metadata(
        self,
        group: AlbumGroup,
        candidate_lookup: CandidateLookupResult,
        selected_identity: tuple[str, str, str, int],
        context: RequestContext,
        *,
        policy: MatchingPolicy = DEFAULT_MATCHING_POLICY,
        preferred_language: str | None = None,
        validation_facts_by_file: Mapping[str, ChangeValidationFacts] | None = None,
        rename_decisions_by_file: Mapping[str, RenameDecision] | None = None,
        rename_template: str = DEFAULT_RENAME_TEMPLATE,
        rename_policy: FilenameRenderPolicy = _DEFAULT_FILENAME_RENDER_POLICY,
        cancellation: CancellationToken | None = None,
        events: OperationEventSink | None = None,
    ) -> SelectedMetadataResult:
        """Enrich and map exactly one explicitly selected ranked medium."""
        if not isinstance(group, AlbumGroup):
            raise TypeError("group must be an AlbumGroup")

        if not isinstance(candidate_lookup, CandidateLookupResult):
            raise TypeError("candidate_lookup must be a CandidateLookupResult")

        if candidate_lookup.lookup_result.group_id != group.group_id:
            raise ValueError("candidate lookup must refer to the selected group")

        if not isinstance(policy, MatchingPolicy):
            raise TypeError("policy must be a MatchingPolicy")

        # Resolve the captured release/medium identity, not its displayed row:
        # sorting the candidate table must not redirect a pending selection.
        ranked_entry = next(
            (
                entry
                for entry in candidate_lookup.release_ranking.entries
                if entry.identity == selected_identity
            ),
            None,
        )

        if ranked_entry is None:
            raise ValueError("selected_identity must belong to the candidate ranking")

        # Preserve the supplied components when naming a legacy tuple. In
        # particular, tuple equality must not turn an invalid bool index into
        # the ranking's valid integer before the result validates its shape.
        identity = ReleaseMediumIdentity(*selected_identity)
        selected = next(
            item
            for item in candidate_lookup.lookup_result.candidates
            if (
                item.candidate.engine_id,
                item.candidate.source_id,
                item.candidate.release_id,
            )
            == identity.release_identity
        )
        active_cancellation = cancellation if cancellation is not None else NeverCancelledToken()
        active_events = events if events is not None else _DiscardEventSink()
        context = replace(context, cancellation=active_cancellation, events=active_events)
        active_cancellation.raise_if_cancelled()
        active_events.emit(
            OperationStageChanged(
                context.operation_id,
                LookupOperationStage.ENRICHING_SELECTION,
            )
        )
        active_events.emit(
            OperationProgress(
                context.operation_id,
                LookupOperationStage.ENRICHING_SELECTION,
                0,
                1,
            )
        )

        def enrichment_started(engine_id: str) -> None:
            active_events.emit(ProviderStarted(context.operation_id, engine_id))

        def finish_enrichment(engine_id: str, issue: Issue | None) -> None:
            if issue is None:
                active_events.emit(ProviderCompleted(context.operation_id, engine_id))
            else:
                active_events.emit(ProviderFailed(context.operation_id, engine_id, issue))

            active_events.emit(
                OperationProgress(
                    context.operation_id,
                    LookupOperationStage.ENRICHING_SELECTION,
                    1,
                    1,
                )
            )

        enrichment = self._coordinator.enrich_release(
            selected,
            context,
            check_cancelled=active_cancellation.raise_if_cancelled,
            on_call_started=enrichment_started,
        )
        active_cancellation.raise_if_cancelled()

        if enrichment.candidate is None:
            finish_enrichment(selected.candidate.engine_id, enrichment.failures[0].issue)
            active_cancellation.raise_if_cancelled()

            return SelectedMetadataResult(
                candidate_lookup=candidate_lookup,
                selected_identity=identity,
                selected_candidate=None,
                track_mapping=None,
                reviewed_files=(),
                failures=enrichment.failures,
            )

        enriched = enrichment.candidate
        medium_index = identity.medium_index

        # Enrichment may add credits, but changing the selected track structure
        # would invalidate the earlier score and the meaning of every track index.
        if (
            medium_index >= len(enriched.candidate.media)
            or not _selected_medium_structure_matches(
                ranked_entry.release,
                enriched.candidate,
                medium_index,
            )
        ):
            failure = ProviderFailure(
                engine_id=enriched.candidate.engine_id,
                issue=Issue(
                    code=ProviderErrorCode.INVALID_RESPONSE,
                    message="The selected release medium changed during enrichment.",
                ),
            )
            finish_enrichment(failure.engine_id, failure.issue)
            active_cancellation.raise_if_cancelled()

            return SelectedMetadataResult(
                candidate_lookup=candidate_lookup,
                selected_identity=identity,
                selected_candidate=None,
                track_mapping=None,
                reviewed_files=(),
                failures=(failure,),
            )

        finish_enrichment(enriched.candidate.engine_id, None)
        active_cancellation.raise_if_cancelled()
        active_events.emit(
            OperationStageChanged(
                context.operation_id,
                LookupOperationStage.MAPPING_TRACKS,
            )
        )
        active_events.emit(
            OperationProgress(
                context.operation_id,
                LookupOperationStage.MAPPING_TRACKS,
                0,
                1,
            )
        )
        active_cancellation.raise_if_cancelled()
        mapping = map_tracks(
            group.files,
            enriched.candidate,
            selected_medium_index=medium_index,
            policy=policy,
        )
        active_cancellation.raise_if_cancelled()
        active_events.emit(
            OperationProgress(
                context.operation_id,
                LookupOperationStage.MAPPING_TRACKS,
                1,
                1,
            )
        )
        active_cancellation.raise_if_cancelled()
        effective_language = (
            preferred_language
            if preferred_language is not None
            else context.preferred_language
        )
        proposal_total = len(group.files)
        proposal_current = 0
        active_events.emit(
            OperationStageChanged(
                context.operation_id,
                LookupOperationStage.BUILDING_PROPOSALS,
            )
        )
        active_events.emit(
            OperationProgress(
                context.operation_id,
                LookupOperationStage.BUILDING_PROPOSALS,
                0,
                proposal_total,
            )
        )
        active_cancellation.raise_if_cancelled()

        def proposal_built(_file_id: str) -> None:
            nonlocal proposal_current

            proposal_current += 1
            active_events.emit(
                OperationProgress(
                    context.operation_id,
                    LookupOperationStage.BUILDING_PROPOSALS,
                    proposal_current,
                    proposal_total,
                )
            )

        reviewed_files = build_selected_file_results(
            group.files,
            enriched,
            medium_index=medium_index,
            mapping_result=mapping,
            release_classification=ranked_entry.result.classification,
            preferred_language=effective_language,
            validation_facts_by_file=validation_facts_by_file,
            rename_decisions_by_file=rename_decisions_by_file,
            rename_template=rename_template,
            rename_policy=rename_policy,
            check_cancelled=active_cancellation.raise_if_cancelled,
            on_file_built=proposal_built,
        )
        active_cancellation.raise_if_cancelled()

        return SelectedMetadataResult(
            candidate_lookup=candidate_lookup,
            selected_identity=identity,
            selected_candidate=enriched,
            track_mapping=mapping,
            reviewed_files=reviewed_files,
            failures=(),
        )
