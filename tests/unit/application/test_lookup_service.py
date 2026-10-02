# Separate local evidence, provider calls and selected-release enrichment so tests
# can detect premature requests or rankings based on incomplete medium structure.

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

from metadata_polisher.application.changes import (
    ChangeIssueCode,
    ChangeValidationFacts,
    RenameDecision,
)
from metadata_polisher.application.lookup import (
    CandidateLookupResult,
    GroupLookupResult,
    LookupOperationStage,
    LookupService,
    build_release_search_queries,
    build_release_search_query,
)
from metadata_polisher.application.review import build_selected_file_results
from metadata_polisher.domain.errors import Issue, MatchingErrorCode, ProviderErrorCode
from metadata_polisher.domain.matching import (
    ComposerCredit,
    CreditScope,
    LocalisedText,
    MetadataProvenance,
    ProviderTrack,
    ReleaseCandidate,
    ReleaseMedium,
    ReleaseSearchQuery,
)
from metadata_polisher.domain.media import (
    FilenameHintConfidence,
    FilenameHintReason,
    FilenameHints,
    LocalMediaFile,
    MediaReadResult,
    StreamInfo,
)
from metadata_polisher.domain.metadata import FieldReadState, MetadataField, MetadataSnapshot, Position
from metadata_polisher.domain.review import (
    FieldConfidence,
    FieldDecisionKind,
    ReviewReasonCode,
)
from metadata_polisher.execution.cancellation import (
    MutableCancellationToken,
    OperationCancelledError,
)
from metadata_polisher.execution.events import (
    OperationEvent,
    OperationProgress,
    OperationStageChanged,
    ProviderCompleted,
    ProviderFailed,
    ProviderStarted,
)
from metadata_polisher.matching.release_scoring import MatchClassification, MatchReasonCode
from metadata_polisher.matching.track_mapping import TrackMapping, TrackMappingResult
from metadata_polisher.providers.base import ProviderCapabilities, RequestContext
from metadata_polisher.providers.coordinator import (
    CandidateHydrationNotice,
    CandidateHydrationReasonCode,
    CoordinatedCandidate,
    ProviderCoordinator,
)
from metadata_polisher.providers.transport import ProviderTransportError, ProviderTransportErrorContext
from metadata_polisher.rename.template import FilenameRenderPolicy
from metadata_polisher.scanner.grouping import AlbumGroup, GroupingReason


def make_media_file(
    name: str,
    *,
    title: str | None = None,
    title_state: FieldReadState | None = None,
    artists: tuple[str, ...] = (),
    artist_state: FieldReadState | None = None,
    album_artists: tuple[str, ...] = (),
    album_artist_state: FieldReadState | None = None,
    date: str | None = None,
    date_state: FieldReadState | None = None,
    disc_number: int | None = None,
    disc_state: FieldReadState | None = None,
    filename_disc: int | None = None,
    filename_title: str | None = None,
) -> LocalMediaFile:
    # Keep physical-looking values and read-state overrides independent so tests
    # can represent unreadable tags that still contain stale in-memory text.
    states = {field: FieldReadState.MISSING for field in MetadataField}
    states[MetadataField.TITLE] = title_state or (
        FieldReadState.PRESENT if title is not None else FieldReadState.MISSING
    )
    states[MetadataField.ARTISTS] = artist_state or (
        FieldReadState.PRESENT if artists else FieldReadState.MISSING
    )
    states[MetadataField.ALBUM_ARTISTS] = album_artist_state or (
        FieldReadState.PRESENT if album_artists else FieldReadState.MISSING
    )
    states[MetadataField.DATE] = date_state or (
        FieldReadState.PRESENT if date is not None else FieldReadState.MISSING
    )
    states[MetadataField.DISC] = disc_state or (
        FieldReadState.PRESENT if disc_number is not None else FieldReadState.MISSING
    )

    hints = FilenameHints()

    if filename_disc is not None or filename_title is not None:
        hints = FilenameHints(
            disc_number=filename_disc,
            track_number=1,
            probable_title=filename_title or f"Filename {name}",
            reason=(
                FilenameHintReason.DISC_TRACK_HYPHEN_PREFIX
                if filename_disc is not None
                else FilenameHintReason.TRACK_DASH_PREFIX
            ),
            confidence=FilenameHintConfidence.HIGH,
        )

    return LocalMediaFile(
        path=Path("library") / "Album" / name,
        format_id="flac",
        read_result=MediaReadResult(
            metadata=MetadataSnapshot(
                title=title,
                artists=artists,
                album="Folder must not become the query album",
                album_artists=album_artists,
                disc=Position(number=disc_number),
                date=date,
            ),
            field_states=states,
            stream_info=StreamInfo(
                duration_seconds=180.0,
                sample_rate=48_000,
                channels=2,
                bit_depth=24,
                codec="FLAC",
            ),
        ),
        filename_hints=hints,
    )


def make_group(*files: LocalMediaFile, album_title: str | None = "Album Evidence") -> AlbumGroup:
    return AlbumGroup(
        group_id="group-0001",
        files=files,
        album_title=album_title,
        reason=GroupingReason.DIRECTORY_ALBUM_CONSISTENT,
    )


def test_query_uses_present_group_evidence_with_deterministic_distinctive_titles() -> None:
    files = (
        make_media_file(
            "05.flac",
            title="Medium title",
            album_artists=("Beta Ensemble",),
            date="2024-11-30",
            disc_number=2,
        ),
        make_media_file(
            "01.flac",
            title="The exceptionally long finale",
            album_artists=("Alpha Ensemble",),
            date="2024",
            disc_number=2,
        ),
        make_media_file(
            "04.flac",
            title="A somewhat longer movement",
            album_artists=("Beta Ensemble",),
            date="2024-02",
            disc_number=2,
        ),
        make_media_file("02.flac", title="Short", date="2024", disc_number=2),
        make_media_file("03.flac", title="Another medium title", date="2024", disc_number=2),
    )

    forwards = build_release_search_query(make_group(*files))
    backwards = build_release_search_query(make_group(*reversed(files)))

    assert forwards == backwards
    assert forwards.album == "Album Evidence"
    assert forwards.artists == ("Alpha Ensemble", "Beta Ensemble")
    assert forwards.year == 2024
    assert forwards.disc_hint == 2
    assert forwards.local_track_count == 5
    assert forwards.distinctive_titles == (
        "The exceptionally long finale",
        "A somewhat longer movement",
        "Another medium title",
    )


def test_query_prefers_present_album_artists_then_falls_back_to_track_artists() -> None:
    with_album_artist = make_group(
        make_media_file(
            "01.flac",
            artists=("Track Artist",),
            album_artists=("Album Artist",),
        ),
        make_media_file("02.flac", artists=("Another Track Artist",)),
    )
    without_album_artist = make_group(
        make_media_file("01.flac", artists=("Zulu Artist",)),
        make_media_file("02.flac", artists=("Alpha Artist", "Zulu Artist")),
    )

    assert build_release_search_query(with_album_artist).artists == ("Album Artist",)
    assert build_release_search_query(without_album_artist).artists == (
        "Alpha Artist",
        "Zulu Artist",
    )


def test_query_requires_strict_consistent_year_and_does_not_use_unreadable_values() -> None:
    invalid_and_unreadable = make_group(
        make_media_file("01.flac", date="2024/01/02"),
        make_media_file(
            "02.flac",
            date="1999",
            date_state=FieldReadState.UNREADABLE,
        ),
    )
    conflicting = make_group(
        make_media_file("01.flac", date="2023-12-31"),
        make_media_file("02.flac", date="2024"),
    )
    non_ascii_digits = make_group(make_media_file("01.flac", date="２０２４"))

    assert build_release_search_query(invalid_and_unreadable).year is None
    assert build_release_search_query(conflicting).year is None
    assert build_release_search_query(non_ascii_digits).year is None


def test_query_prefers_consistent_present_disc_tags_and_uses_filename_only_as_fallback() -> None:
    tagged = make_group(
        make_media_file("01.flac", disc_number=3, filename_disc=8),
        make_media_file("02.flac", disc_number=3, filename_disc=8),
    )
    filename_only = make_group(
        make_media_file("01.flac", filename_disc=4),
        make_media_file("02.flac", filename_disc=4),
    )
    conflicting_tags = make_group(
        make_media_file("01.flac", disc_number=1, filename_disc=7),
        make_media_file("02.flac", disc_number=2, filename_disc=7),
    )

    assert build_release_search_query(tagged).disc_hint == 3
    assert build_release_search_query(filename_only).disc_hint == 4
    assert build_release_search_query(conflicting_tags).disc_hint is None


def test_query_excludes_non_present_and_blank_metadata_without_folder_inference() -> None:
    group = make_group(
        make_media_file(
            "An Informative Folder Name.flac",
            title="Hidden title",
            title_state=FieldReadState.UNREADABLE,
            artists=("Hidden artist",),
            artist_state=FieldReadState.UNREADABLE,
            album_artists=("Hidden album artist",),
            album_artist_state=FieldReadState.UNREADABLE,
            date="2024",
            date_state=FieldReadState.UNREADABLE,
            disc_number=2,
            disc_state=FieldReadState.UNREADABLE,
        ),
        album_title=None,
    )

    query = build_release_search_query(group)

    assert query.album is None
    assert query.artists == ()
    assert query.year is None
    assert query.disc_hint is None
    assert query.local_track_count == 1
    assert query.distinctive_titles == ()


def test_query_uses_filename_title_per_file_when_present_tag_evidence_is_unusable() -> None:
    group = make_group(
        make_media_file(
            "01.flac",
            title="Tagged Title",
            filename_title="Ignored Filename Title",
        ),
        make_media_file(
            "02.flac",
            filename_title="Filename Fallback",
        ),
        make_media_file(
            "03.flac",
            title="Stale title",
            title_state=FieldReadState.UNREADABLE,
            filename_title="Readable Filename",
        ),
    )

    assert build_release_search_query(group).distinctive_titles == (
        "Filename Fallback",
        "Readable Filename",
        "Tagged Title",
    )


def test_query_strategies_relax_constraints_without_changing_evidence_meaning() -> None:
    group = make_group(
        make_media_file(
            "01.flac",
            title="Opening",
            album_artists=("Album Artist",),
            date="2024-01-02",
            disc_number=2,
        ),
        make_media_file("02.flac", title="Finale", date="2024", disc_number=2),
    )

    queries = build_release_search_queries(group)

    assert len(queries) == 3
    assert queries[0] == build_release_search_query(group)
    assert (queries[0].artists, queries[0].year, queries[0].disc_hint, queries[0].local_track_count) == (
        ("Album Artist",),
        2024,
        2,
        2,
    )
    assert (queries[1].artists, queries[1].year, queries[1].disc_hint, queries[1].local_track_count) == (
        ("Album Artist",),
        None,
        None,
        0,
    )
    assert (queries[2].artists, queries[2].year, queries[2].disc_hint, queries[2].local_track_count) == (
        (),
        None,
        None,
        0,
    )
    assert all(query.album == "Album Evidence" for query in queries)
    assert all(query.distinctive_titles == ("Opening", "Finale") for query in queries)


def make_candidate(
    engine_id: str,
    release_id: str,
    *,
    media: tuple[ReleaseMedium, ...] = (),
    title: str | None = None,
) -> ReleaseCandidate:
    return ReleaseCandidate(
        engine_id=engine_id,
        source_id=engine_id,
        release_id=release_id,
        titles=(LocalisedText(value=title or f"Title {release_id}", language="eng", script="Latn"),),
        album_artists=(),
        date=None,
        media=media,
        source_url=f"https://catalogue.invalid/{release_id}",
    )


@dataclass
class LookupFakeProvider:
    engine_id: str
    candidates: tuple[ReleaseCandidate, ...]
    search_failure: ProviderTransportError | None = None
    enriched_candidate: ReleaseCandidate | None = None
    enrichment_failure: ProviderTransportError | None = None
    basic_candidates: dict[str, ReleaseCandidate] = field(default_factory=dict)
    after_media_load: Callable[[], None] | None = None
    after_enrichment: Callable[[], None] | None = None
    search_calls: list[tuple[ReleaseSearchQuery, RequestContext]] = field(default_factory=list)
    media_calls: list[tuple[ReleaseCandidate, RequestContext]] = field(default_factory=list)
    enrich_calls: list[tuple[ReleaseCandidate, RequestContext]] = field(default_factory=list)

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            release_search=True,
            track_listing=True,
            composer_credits=True,
            multilingual_titles=True,
        )

    def search_releases(
        self,
        query: ReleaseSearchQuery,
        context: RequestContext,
    ) -> tuple[ReleaseCandidate, ...]:
        self.search_calls.append((query, context))

        if self.search_failure is not None:
            raise self.search_failure

        return self.candidates

    def enrich_release(
        self,
        candidate: ReleaseCandidate,
        context: RequestContext,
    ) -> ReleaseCandidate:
        self.enrich_calls.append((candidate, context))

        if self.enrichment_failure is not None:
            raise self.enrichment_failure

        if self.after_enrichment is not None:
            self.after_enrichment()

        return self.enriched_candidate or candidate

    def load_release_media(
        self,
        candidate: ReleaseCandidate,
        context: RequestContext,
    ) -> ReleaseCandidate:
        self.media_calls.append((candidate, context))

        if self.after_media_load is not None:
            self.after_media_load()

        return self.basic_candidates.get(candidate.release_id, candidate)


@dataclass
class RecordingEventSink:
    events: list[OperationEvent] = field(default_factory=list)

    def emit(self, event: OperationEvent) -> None:
        self.events.append(event)


@dataclass
class CancellingProgressSink:
    cancellation: MutableCancellationToken
    stage: LookupOperationStage
    current: int
    events: list[OperationEvent] = field(default_factory=list)

    def emit(self, event: OperationEvent) -> None:
        self.events.append(event)

        if (
            isinstance(event, OperationProgress)
            and event.stage == self.stage
            and event.current == self.current
        ):
            self.cancellation.cancel()


def lookup_context() -> RequestContext:
    return RequestContext(operation_id="explicit-lookup", preferred_language="eng")


def make_medium(
    *titles: str,
    medium_number: int = 1,
    artists: tuple[str, ...] = (),
    composers: tuple[str, ...] = (),
) -> ReleaseMedium:
    return ReleaseMedium(
        medium_number=medium_number,
        title=None,
        tracks=tuple(
            ProviderTrack(
                track_number=index,
                titles=(LocalisedText(value=title, language="eng", script="Latn"),),
                artists=artists,
                composers=composers,
                duration_seconds=180.0,
                composer_credits=(ComposerCredit(composers, CreditScope.TRACK),) if composers else (),
            )
            for index, title in enumerate(titles, start=1)
        ),
    )


def test_lookup_construction_and_local_query_building_do_not_call_providers() -> None:
    provider = LookupFakeProvider(
        engine_id="provider",
        candidates=(make_candidate("provider", "release-1"),),
    )
    service = LookupService(ProviderCoordinator((provider,)))

    query = build_release_search_query(make_group(make_media_file("01.flac")))

    assert query.local_track_count == 1
    assert provider.search_calls == []
    assert provider.enrich_calls == []
    assert service is not None


def test_search_runs_only_on_explicit_request_and_does_not_eagerly_enrich() -> None:
    candidate = make_candidate("provider", "release-1")
    provider = LookupFakeProvider(engine_id="provider", candidates=(candidate,))
    service = LookupService(ProviderCoordinator((provider,)))
    group = make_group(
        make_media_file("01.flac", title="Opening"),
        make_media_file("02.flac", title="Finale"),
    )
    context = lookup_context()

    result = service.search_group(group, context)

    assert len(provider.search_calls) == len(result.queries)
    assert tuple(query for query, _context in provider.search_calls) == result.queries
    assert all(call_context is context for _query, call_context in provider.search_calls)
    assert provider.enrich_calls == []
    assert result.group_id == group.group_id
    assert result.query == result.queries[0]
    assert result.candidates[0].candidate is candidate
    assert result.failures == ()


def test_enrichment_is_deferred_to_the_selected_candidate_and_uses_its_provider() -> None:
    first_candidate = make_candidate("first", "release-1")
    selected_candidate = make_candidate("second", "release-2")
    enriched_candidate = make_candidate(
        "second",
        "release-2",
        media=(ReleaseMedium(medium_number=1, title=None, tracks=()),),
    )
    first = LookupFakeProvider(engine_id="first", candidates=(first_candidate,))
    second = LookupFakeProvider(
        engine_id="second",
        candidates=(selected_candidate,),
        enriched_candidate=enriched_candidate,
    )
    service = LookupService(ProviderCoordinator((first, second)))
    context = lookup_context()
    search_result = service.search_group(
        make_group(make_media_file("01.flac")),
        context,
    )

    enrichment = service.enrich_selected(search_result.candidates[1], context)

    assert first.enrich_calls == []
    assert second.enrich_calls == [(selected_candidate, context)]
    assert enrichment.candidate is not None
    assert enrichment.candidate.candidate is enriched_candidate
    assert enrichment.candidate.provenance == search_result.candidates[1].provenance
    assert enrichment.failures == ()


def test_selected_enrichment_failure_is_structured_and_secret_free() -> None:
    candidate = make_candidate("provider", "release-1")
    error = ProviderTransportError(
        code=ProviderErrorCode.NETWORK_TIMEOUT,
        context=ProviderTransportErrorContext(attempt_count=2),
    )
    provider = LookupFakeProvider(
        engine_id="provider",
        candidates=(candidate,),
        enrichment_failure=error,
    )
    service = LookupService(ProviderCoordinator((provider,)))
    context = lookup_context()
    selected = service.search_group(
        make_group(make_media_file("01.flac")),
        context,
    ).candidates[0]

    enrichment = service.enrich_selected(selected, context)

    assert enrichment.candidate is None
    assert len(enrichment.failures) == 1
    assert enrichment.failures[0].engine_id == "provider"
    assert enrichment.failures[0].issue.code is ProviderErrorCode.NETWORK_TIMEOUT
    assert enrichment.failures[0].issue.technical_detail == "attempts=2, status=None, retry_after=None"


def test_search_and_rank_hydrates_one_sparse_candidate_without_enriching_it() -> None:
    sparse = make_candidate(
        "provider",
        "release-1",
        title="Folder must not become the query album",
    )
    provider = LookupFakeProvider(
        engine_id="provider",
        candidates=(sparse,),
        basic_candidates={
            "release-1": make_candidate(
                "provider",
                "release-1",
                media=(make_medium("Opening", "Finale"),),
                title="Folder must not become the query album",
            )
        },
    )
    service = LookupService(ProviderCoordinator((provider,)))
    group = make_group(
        make_media_file("01.flac", title="Opening"),
        make_media_file("02.flac", title="Finale"),
    )
    context = lookup_context()

    result = service.search_and_rank_group(group, context, per_engine_limit=1)

    assert [candidate.release_id for candidate, _context in provider.media_calls] == [
        "release-1",
    ]
    assert provider.enrich_calls == []
    assert result.lookup_result.group_id == group.group_id
    assert result.release_ranking.identities == (
        ("provider", "provider", "release-1", 0),
    )
    assert result.hydration_notices == ()
    assert result.matching_issues == ()


def test_search_and_rank_uses_only_the_explicit_query_override() -> None:
    sparse = make_candidate("provider", "release-1")
    provider = LookupFakeProvider(
        engine_id="provider",
        candidates=(sparse,),
        basic_candidates={
            "release-1": make_candidate(
                "provider",
                "release-1",
                media=(make_medium("Opening"),),
            )
        },
    )
    service = LookupService(ProviderCoordinator((provider,)))
    group = make_group(make_media_file("01.flac", title="Opening"))
    override = ReleaseSearchQuery(
        album="Exact manual search",
        artists=("Chosen Artist",),
        year=1999,
        disc_hint=2,
        local_track_count=0,
        distinctive_titles=(),
    )

    result = service.search_and_rank_group(
        group,
        lookup_context(),
        query_override=override,
        per_engine_limit=1,
    )

    assert tuple(query for query, _context in provider.search_calls) == (override,)
    assert result.lookup_result.queries == (override,)


def test_search_and_rank_labels_manual_disc_override_truthfully_and_uses_it_for_ranking() -> None:
    sparse = make_candidate("provider", "release-1")
    hydrated = make_candidate(
        "provider",
        "release-1",
        title="Folder must not become the query album",
        media=(
            make_medium("Opening", medium_number=1),
            make_medium("Opening", medium_number=2),
        ),
    )
    provider = LookupFakeProvider(
        engine_id="provider",
        candidates=(sparse,),
        basic_candidates={"release-1": hydrated},
    )
    service = LookupService(ProviderCoordinator((provider,)))
    group = make_group(
        make_media_file("01.flac", title="Opening", disc_number=1),
    )

    result = service.search_and_rank_group(
        group,
        lookup_context(),
        disc_number_override=2,
        per_engine_limit=1,
    )

    top = result.release_ranking.entries[0]
    disc_evidence = next(
        evidence
        for evidence in top.result.evidence
        if evidence.code == MatchReasonCode.DISC_EXACT
    )
    assert top.identity == ("provider", "provider", "release-1", 1)
    assert "(manual_override)" in disc_evidence.detail


def test_search_and_rank_hydrates_real_media_and_keeps_ambiguous_candidates_unselected() -> None:
    medium = make_medium("Opening", "Finale")
    sparse_candidates = (
        make_candidate("provider", "release-1", title="Folder must not become the query album"),
        make_candidate("provider", "release-2", title="Folder must not become the query album"),
    )
    provider = LookupFakeProvider(
        engine_id="provider",
        candidates=sparse_candidates,
        basic_candidates={
            candidate.release_id: make_candidate(
                "provider",
                candidate.release_id,
                media=(medium,),
                title="Folder must not become the query album",
            )
            for candidate in sparse_candidates
        },
    )
    service = LookupService(ProviderCoordinator((provider,)))
    group = make_group(
        make_media_file("01.flac", title="Opening"),
        make_media_file("02.flac", title="Finale"),
    )
    context = lookup_context()

    result = service.search_and_rank_group(
        group,
        context,
        per_engine_limit=2,
    )

    assert [candidate.release_id for candidate, _context in provider.media_calls] == [
        "release-1",
        "release-2",
    ]
    assert provider.enrich_calls == []
    assert result.lookup_result.group_id == group.group_id
    assert all(candidate.candidate.media for candidate in result.lookup_result.candidates)
    assert result.release_ranking.identities == (
        ("provider", "provider", "release-1", 0),
        ("provider", "provider", "release-2", 0),
    )
    assert result.release_ranking.ambiguous
    assert result.hydration_notices == ()
    assert tuple(issue.code.value for issue in result.matching_issues) == (
        "AMBIGUOUS_CANDIDATE",
    )


def make_candidate_lookup_result() -> CandidateLookupResult:
    sparse = make_candidate("provider", "release-1")
    provider = LookupFakeProvider(
        engine_id="provider",
        candidates=(sparse,),
        basic_candidates={
            "release-1": make_candidate(
                "provider",
                "release-1",
                media=(make_medium("Opening"),),
            )
        },
    )

    return LookupService(ProviderCoordinator((provider,))).search_and_rank_group(
        make_group(make_media_file("01.flac", title="Opening")),
        lookup_context(),
        per_engine_limit=1,
    )


def test_candidate_lookup_result_rejects_duplicate_lookup_identities() -> None:
    result = make_candidate_lookup_result()
    candidate = result.lookup_result.candidates[0]
    duplicate_lookup = replace(
        result.lookup_result,
        candidates=(candidate, candidate),
    )

    with pytest.raises(ValueError, match="lookup candidate identities"):
        replace(result, lookup_result=duplicate_lookup)


def test_candidate_lookup_result_rejects_a_foreign_or_mismatched_ranked_medium() -> None:
    result = make_candidate_lookup_result()
    entry = result.release_ranking.entries[0]
    foreign = make_candidate(
        "other",
        "foreign",
        media=(make_medium("Opening"),),
    )
    foreign_ranking = replace(
        result.release_ranking,
        entries=(replace(entry, release=foreign, medium=foreign.media[0]),),
    )

    with pytest.raises(ValueError, match="ranking entry release"):
        replace(result, release_ranking=foreign_ranking)

    mismatched_ranking = replace(
        result.release_ranking,
        entries=(replace(entry, medium=make_medium("Different")),),
    )

    with pytest.raises(ValueError, match="ranking entry medium"):
        replace(result, release_ranking=mismatched_ranking)


def test_candidate_lookup_result_requires_each_scoreable_medium_exactly_once() -> None:
    result = make_candidate_lookup_result()
    entry = result.release_ranking.entries[0]
    duplicate_ranking = replace(
        result.release_ranking,
        entries=(entry, entry),
    )

    with pytest.raises(ValueError, match="ranking identities"):
        replace(result, release_ranking=duplicate_ranking)

    empty_ranking = replace(result.release_ranking, entries=(), ambiguous=False)
    insufficient = Issue(
        code=MatchingErrorCode.INSUFFICIENT_EVIDENCE,
        message="No candidate has enough real media evidence for a reliable match.",
    )

    with pytest.raises(ValueError, match="ranking identities"):
        replace(
            result,
            release_ranking=empty_ranking,
            matching_issues=(insufficient,),
        )


def test_candidate_lookup_result_rejects_foreign_or_duplicate_hydration_notices() -> None:
    result = make_candidate_lookup_result()
    foreign_notice = CandidateHydrationNotice(
        candidate_identity=("other", "other", "foreign"),
        reason_code=CandidateHydrationReasonCode.PER_ENGINE_LIMIT_REACHED,
        issue=None,
    )

    with pytest.raises(ValueError, match="hydration notice"):
        replace(result, hydration_notices=(foreign_notice,))

    identity = (
        result.lookup_result.candidates[0].candidate.engine_id,
        result.lookup_result.candidates[0].candidate.source_id,
        result.lookup_result.candidates[0].candidate.release_id,
    )
    notice = replace(foreign_notice, candidate_identity=identity)

    with pytest.raises(ValueError, match="hydration notice identities"):
        replace(result, hydration_notices=(notice, notice))


def test_candidate_lookup_result_requires_notices_for_exactly_unscoreable_candidates() -> None:
    scoreable = make_candidate_lookup_result()
    valid_identity = (
        scoreable.lookup_result.candidates[0].candidate.engine_id,
        scoreable.lookup_result.candidates[0].candidate.source_id,
        scoreable.lookup_result.candidates[0].candidate.release_id,
    )
    false_notice = CandidateHydrationNotice(
        candidate_identity=valid_identity,
        reason_code=CandidateHydrationReasonCode.BASIC_MEDIA_INVALID,
        issue=Issue(
            code=ProviderErrorCode.INVALID_RESPONSE,
            message="Contradicts the valid basic media.",
        ),
    )

    with pytest.raises(ValueError, match="unscoreable lookup candidates"):
        replace(scoreable, hydration_notices=(false_notice,))

    sparse = make_candidate("provider", "release-1")
    provider = LookupFakeProvider(engine_id="provider", candidates=(sparse,))
    unscoreable = LookupService(ProviderCoordinator((provider,))).search_and_rank_group(
        make_group(make_media_file("01.flac", title="Opening")),
        lookup_context(),
        per_engine_limit=1,
    )

    assert len(unscoreable.hydration_notices) == 1

    with pytest.raises(ValueError, match="unscoreable lookup candidates"):
        replace(unscoreable, hydration_notices=())

    two_sparse = (
        make_candidate("provider", "release-1"),
        make_candidate("provider", "release-2"),
    )
    two_unscoreable = LookupService(
        ProviderCoordinator(
            (LookupFakeProvider(engine_id="provider", candidates=two_sparse),)
        )
    ).search_and_rank_group(
        make_group(make_media_file("01.flac", title="Opening")),
        lookup_context(),
        per_engine_limit=2,
    )

    assert len(two_unscoreable.hydration_notices) == 2

    with pytest.raises(ValueError, match="lookup order"):
        replace(
            two_unscoreable,
            hydration_notices=tuple(reversed(two_unscoreable.hydration_notices)),
        )


def test_group_lookup_result_carries_explicit_lineage_with_zero_candidates() -> None:
    provider = LookupFakeProvider(engine_id="provider", candidates=())
    candidate_lookup = LookupService(ProviderCoordinator((provider,))).search_and_rank_group(
        make_group(make_media_file("01.flac", title="Opening")),
        lookup_context(),
    )

    result = GroupLookupResult(
        operation_id="explicit-lookup",
        base_session_revision=7,
        base_library_revision=3,
        group_id="group-0001",
        base_group_revision=2,
        candidate_lookup=candidate_lookup,
    )

    assert result.candidate_lookup.lookup_result.candidates == ()
    assert result.candidate_lookup.matching_issues[0].code is MatchingErrorCode.NO_CANDIDATE
    assert result.operation_id == "explicit-lookup"
    assert result.base_session_revision == 7
    assert result.base_library_revision == 3
    assert result.group_id == "group-0001"
    assert result.base_group_revision == 2
    assert result.selected_metadata is None


def test_group_lookup_result_rejects_mismatched_group_or_provenance_lineage() -> None:
    candidate_lookup = make_candidate_lookup_result()
    result = GroupLookupResult(
        operation_id="explicit-lookup",
        base_session_revision=4,
        base_library_revision=1,
        group_id="group-0001",
        base_group_revision=2,
        candidate_lookup=candidate_lookup,
    )

    with pytest.raises(ValueError, match="group_id"):
        replace(result, group_id="foreign-group")

    with pytest.raises(ValueError, match="provenance operation"):
        replace(result, operation_id="FOREIGN-LOOKUP")

    with pytest.raises(ValueError, match="base_group_revision"):
        replace(result, base_session_revision=1, base_group_revision=2)


def test_candidate_lookup_result_rejects_non_matching_or_contradictory_issue_codes() -> None:
    result = make_candidate_lookup_result()

    with pytest.raises(ValueError, match="matching_issues"):
        replace(
            result,
            matching_issues=(
                Issue(
                    code=ProviderErrorCode.NETWORK_ERROR,
                    message="This is not a matching outcome.",
                ),
            ),
        )

    with pytest.raises(ValueError, match="matching_issues"):
        replace(
            result,
            matching_issues=(
                Issue(
                    code=MatchingErrorCode.NO_CANDIDATE,
                    message="Contradicts the populated ranking.",
                ),
            ),
        )


def test_search_and_rank_preserves_partial_failure_limit_and_sparse_candidate() -> None:
    unavailable = LookupFakeProvider(
        engine_id="unavailable",
        candidates=(),
        search_failure=ProviderTransportError(
            code=ProviderErrorCode.SERVICE_UNAVAILABLE,
            context=ProviderTransportErrorContext(attempt_count=1),
        ),
    )
    sparse_candidates = (
        make_candidate("working", "release-1"),
        make_candidate("working", "release-2"),
    )
    working = LookupFakeProvider(
        engine_id="working",
        candidates=sparse_candidates,
        basic_candidates={
            "release-1": make_candidate(
                "working",
                "release-1",
                media=(make_medium("Opening"),),
            )
        },
    )
    result = LookupService(
        ProviderCoordinator((unavailable, working))
    ).search_and_rank_group(
        make_group(make_media_file("01.flac", title="Opening")),
        lookup_context(),
        per_engine_limit=1,
    )

    assert len(result.lookup_result.failures) == len(unavailable.search_calls)
    assert all(
        failure.engine_id == "unavailable"
        and failure.issue.code is ProviderErrorCode.SERVICE_UNAVAILABLE
        for failure in result.lookup_result.failures
    )
    assert tuple(item.candidate.release_id for item in result.lookup_result.candidates) == (
        "release-1",
        "release-2",
    )
    assert result.release_ranking.identities == (("working", "working", "release-1", 0),)
    assert tuple(notice.candidate_identity for notice in result.hydration_notices) == (
        ("working", "working", "release-2"),
    )
    assert result.hydration_notices[0].reason_code is (
        CandidateHydrationReasonCode.PER_ENGINE_LIMIT_REACHED
    )


def test_search_and_rank_distinguishes_no_candidate_from_no_scoreable_media() -> None:
    empty_provider = LookupFakeProvider(engine_id="empty", candidates=())
    no_candidates = LookupService(
        ProviderCoordinator((empty_provider,))
    ).search_and_rank_group(
        make_group(make_media_file("01.flac", title="Opening")),
        lookup_context(),
    )

    assert no_candidates.release_ranking.entries == ()
    assert tuple(issue.code for issue in no_candidates.matching_issues) == (
        MatchingErrorCode.NO_CANDIDATE,
    )

    sparse = make_candidate("sparse", "release-1")
    no_media = LookupService(
        ProviderCoordinator((LookupFakeProvider(engine_id="sparse", candidates=(sparse,)),))
    ).search_and_rank_group(
        make_group(make_media_file("01.flac", title="Opening")),
        lookup_context(),
        per_engine_limit=1,
    )

    assert no_media.lookup_result.candidates[0].candidate is sparse
    assert no_media.release_ranking.entries == ()
    assert no_media.hydration_notices[0].candidate_identity == (
        "sparse",
        "sparse",
        "release-1",
    )
    assert tuple(issue.code for issue in no_media.matching_issues) == (
        MatchingErrorCode.INSUFFICIENT_EVIDENCE,
    )


def test_search_and_rank_marks_a_low_top_candidate_as_insufficient_evidence() -> None:
    sparse = make_candidate("provider", "release-1")
    low_candidate = make_candidate(
        "provider",
        "release-1",
        title="Entirely unrelated release",
        media=(make_medium("Unrelated track", medium_number=2),),
    )
    result = LookupService(
        ProviderCoordinator(
            (
                LookupFakeProvider(
                    engine_id="provider",
                    candidates=(sparse,),
                    basic_candidates={"release-1": low_candidate},
                ),
            )
        )
    ).search_and_rank_group(
        make_group(
            make_media_file("01.flac", title="Opening", disc_number=1),
        ),
        lookup_context(),
        per_engine_limit=1,
    )

    assert result.release_ranking.entries[0].result.classification.value == "low"
    assert tuple(issue.code for issue in result.matching_issues) == (
        MatchingErrorCode.INSUFFICIENT_EVIDENCE,
    )


def test_search_and_rank_emits_semantic_provider_stage_and_progress_events() -> None:
    sparse = make_candidate("provider", "release-1")
    provider = LookupFakeProvider(
        engine_id="provider",
        candidates=(sparse,),
        basic_candidates={
            "release-1": make_candidate(
                "provider",
                "release-1",
                media=(make_medium("Opening"),),
            )
        },
    )
    events = RecordingEventSink()
    context = lookup_context()
    query_override = ReleaseSearchQuery(
        album="Album",
        artists=(),
        year=None,
        disc_hint=None,
        local_track_count=1,
        distinctive_titles=("Opening",),
    )

    LookupService(ProviderCoordinator((provider,))).search_and_rank_group(
        make_group(make_media_file("01.flac", title="Opening")),
        context,
        query_override=query_override,
        per_engine_limit=1,
        events=events,
    )

    assert events.events == [
        OperationStageChanged(context.operation_id, LookupOperationStage.SEARCHING_PROVIDERS),
        OperationProgress(context.operation_id, LookupOperationStage.SEARCHING_PROVIDERS, 0, 1),
        ProviderStarted(context.operation_id, "provider"),
        ProviderCompleted(context.operation_id, "provider"),
        OperationProgress(context.operation_id, LookupOperationStage.SEARCHING_PROVIDERS, 1, 1),
        OperationStageChanged(context.operation_id, LookupOperationStage.HYDRATING_MEDIA),
        OperationProgress(context.operation_id, LookupOperationStage.HYDRATING_MEDIA, 0, 1),
        ProviderStarted(context.operation_id, "provider"),
        ProviderCompleted(context.operation_id, "provider"),
        OperationProgress(context.operation_id, LookupOperationStage.HYDRATING_MEDIA, 1, 1),
        OperationStageChanged(context.operation_id, LookupOperationStage.RANKING_CANDIDATES),
        OperationProgress(context.operation_id, LookupOperationStage.RANKING_CANDIDATES, 0, 1),
        OperationProgress(context.operation_id, LookupOperationStage.RANKING_CANDIDATES, 1, 1),
    ]


def test_search_and_rank_checks_cancellation_after_terminal_ranking_progress() -> None:
    cancellation = MutableCancellationToken()
    sparse = make_candidate("provider", "release-1")
    provider = LookupFakeProvider(
        engine_id="provider",
        candidates=(sparse,),
        basic_candidates={
            "release-1": make_candidate(
                "provider",
                "release-1",
                media=(make_medium("Opening"),),
            )
        },
    )
    events = CancellingProgressSink(
        cancellation,
        LookupOperationStage.RANKING_CANDIDATES,
        1,
    )

    with pytest.raises(OperationCancelledError, match="operation was cancelled"):
        LookupService(ProviderCoordinator((provider,))).search_and_rank_group(
            make_group(make_media_file("01.flac", title="Opening")),
            lookup_context(),
            per_engine_limit=1,
            cancellation=cancellation,
            events=events,
        )


def test_search_and_rank_checks_cancellation_after_reused_media_progress() -> None:
    cancellation = MutableCancellationToken()
    ready = make_candidate(
        "provider",
        "release-1",
        media=(make_medium("Opening"),),
    )
    provider = LookupFakeProvider(engine_id="provider", candidates=(ready,))
    events = CancellingProgressSink(
        cancellation,
        LookupOperationStage.HYDRATING_MEDIA,
        1,
    )

    with pytest.raises(OperationCancelledError, match="operation was cancelled"):
        LookupService(ProviderCoordinator((provider,))).search_and_rank_group(
            make_group(make_media_file("01.flac", title="Opening")),
            lookup_context(),
            per_engine_limit=1,
            cancellation=cancellation,
            events=events,
        )

    assert provider.media_calls == []
    assert not any(
        isinstance(event, OperationStageChanged)
        and event.stage == LookupOperationStage.RANKING_CANDIDATES
        for event in events.events
    )


def test_search_provider_failure_emits_failure_and_advances_call_progress() -> None:
    issue_error = ProviderTransportError(
        code=ProviderErrorCode.SERVICE_UNAVAILABLE,
        context=ProviderTransportErrorContext(attempt_count=1),
    )
    provider = LookupFakeProvider(
        engine_id="provider",
        candidates=(),
        search_failure=issue_error,
    )
    events = RecordingEventSink()
    context = lookup_context()
    query_override = ReleaseSearchQuery(
        album="Album",
        artists=(),
        year=None,
        disc_hint=None,
        local_track_count=1,
        distinctive_titles=(),
    )

    result = LookupService(ProviderCoordinator((provider,))).search_and_rank_group(
        make_group(make_media_file("01.flac", title="Opening")),
        context,
        query_override=query_override,
        events=events,
    )

    assert result.lookup_result.failures[0].issue.code is ProviderErrorCode.SERVICE_UNAVAILABLE
    assert events.events[:5] == [
        OperationStageChanged(context.operation_id, LookupOperationStage.SEARCHING_PROVIDERS),
        OperationProgress(context.operation_id, LookupOperationStage.SEARCHING_PROVIDERS, 0, 1),
        ProviderStarted(context.operation_id, "provider"),
        ProviderFailed(
            context.operation_id,
            "provider",
            result.lookup_result.failures[0].issue,
        ),
        OperationProgress(context.operation_id, LookupOperationStage.SEARCHING_PROVIDERS, 1, 1),
    ]


def test_search_and_rank_checks_cancellation_between_basic_media_calls() -> None:
    cancellation = MutableCancellationToken()
    sparse_candidates = (
        make_candidate("provider", "release-1"),
        make_candidate("provider", "release-2"),
    )
    provider = LookupFakeProvider(
        engine_id="provider",
        candidates=sparse_candidates,
        basic_candidates={
            candidate.release_id: make_candidate(
                "provider",
                candidate.release_id,
                media=(make_medium("Opening"),),
            )
            for candidate in sparse_candidates
        },
        after_media_load=cancellation.cancel,
    )
    query_override = ReleaseSearchQuery(
        album="Album",
        artists=(),
        year=None,
        disc_hint=None,
        local_track_count=1,
        distinctive_titles=("Opening",),
    )

    with pytest.raises(OperationCancelledError, match="operation was cancelled"):
        LookupService(ProviderCoordinator((provider,))).search_and_rank_group(
            make_group(make_media_file("01.flac", title="Opening")),
            lookup_context(),
            query_override=query_override,
            per_engine_limit=2,
            cancellation=cancellation,
        )

    assert [candidate.release_id for candidate, _context in provider.media_calls] == [
        "release-1",
    ]


def test_explicit_selection_enriches_only_that_candidate_and_maps_its_medium() -> None:
    sparse_candidates = (
        make_candidate("provider", "release-1"),
        make_candidate("provider", "release-2"),
    )
    basic_candidates = {
        candidate.release_id: make_candidate(
            "provider",
            candidate.release_id,
            media=(make_medium("Opening"),),
        )
        for candidate in sparse_candidates
    }
    enriched_selected = make_candidate(
        "provider",
        "release-2",
        media=(make_medium("Opening", composers=("Composer",)),),
    )
    provider = LookupFakeProvider(
        engine_id="provider",
        candidates=sparse_candidates,
        basic_candidates=basic_candidates,
        enriched_candidate=enriched_selected,
    )
    service = LookupService(ProviderCoordinator((provider,)))
    group = make_group(make_media_file("01.flac", title="Opening"))
    context = lookup_context()
    lookup = service.search_and_rank_group(group, context, per_engine_limit=2)

    result = service.select_candidate_metadata(
        group,
        lookup,
        ("provider", "provider", "release-2", 0),
        context,
    )

    assert provider.enrich_calls == [(basic_candidates["release-2"], context)]
    assert result.candidate_lookup is lookup
    assert result.selected_identity == ("provider", "provider", "release-2", 0)
    assert result.selected_identity.release_id == "release-2"
    assert result.selected_identity.medium_index == 0
    assert result.selected_candidate is not None
    assert result.selected_candidate.candidate is enriched_selected
    assert result.selected_candidate.candidate.media[0].tracks[0].composers == ("Composer",)
    assert result.track_mapping is not None
    assert tuple(mapping.local_file_id for mapping in result.track_mapping.mappings) == (
        group.files[0].file_id,
    )
    assert result.failures == ()

    # Legacy callers can reconstruct a result with the original public tuple.
    # Its named projection must not change equality or relax index validation.
    legacy_result = replace(result, selected_identity=("provider", "provider", "release-2", 0))

    assert legacy_result == result
    assert legacy_result.selected_identity.release_identity == ("provider", "provider", "release-2")

    with pytest.raises(TypeError, match="selected_identity"):
        replace(result, selected_identity=("provider", "provider", "release-2", False))

    mismatched_mapping = replace(result.track_mapping, selected_medium_number=99)

    with pytest.raises(ValueError, match="selected medium number"):
        replace(result, track_mapping=mismatched_mapping)


@pytest.mark.parametrize("number_source", ["tag", "filename"])
def test_explicit_selection_orders_unpadded_filenames_by_reliable_track_evidence(
    number_source: str,
) -> None:
    titles = (
        "Opening", "Mountain", "Rainfall", "Journey", "Moonlight",
        "Celebration", "Quiet Waters", "Homecoming", "Nightfall", "Finale",
    )
    files: list[LocalMediaFile] = []

    for number, title in enumerate(titles, start=1):
        file = make_media_file(f"{number}.flac", title=title)

        if number_source == "tag":
            read_result = replace(
                file.read_result,
                metadata=replace(file.read_result.metadata, track=Position(number=number)),
                field_states={**file.read_result.field_states, MetadataField.TRACK: FieldReadState.PRESENT},
            )
            file = replace(file, read_result=read_result)
        else:
            file = replace(file, filename_hints=FilenameHints(track_number=number, probable_title=title))

        files.append(file)

    # Scanner/grouping retain deterministic path order. Unpadded filenames put
    # track 10 before track 2; that incidental order must not defeat clear tags.
    group = make_group(*sorted(files, key=lambda file: file.path.name))
    candidate = make_candidate("provider", "release-1", media=(make_medium(*titles),))
    provider = LookupFakeProvider(engine_id="provider", candidates=(candidate,))
    service = LookupService(ProviderCoordinator((provider,)))
    context = lookup_context()
    lookup = service.search_and_rank_group(group, context)

    result = service.select_candidate_metadata(
        group,
        lookup,
        ("provider", "provider", "release-1", 0),
        context,
    )

    assert tuple(file.path.name for file in group.files[:3]) == ("1.flac", "10.flac", "2.flac")
    assert result.track_mapping is not None
    assert result.track_mapping.unmatched_local_file_ids == ()
    assert result.track_mapping.unmatched_provider_indexes == ()
    assert {
        mapping.local_file_id: mapping.provider_track_index
        for mapping in result.track_mapping.mappings
    } == {file.file_id: index for index, file in enumerate(files)}


def test_explicit_selection_emits_semantic_progress_around_real_phases() -> None:
    sparse = make_candidate("provider", "release-1")
    basic = make_candidate(
        "provider",
        "release-1",
        media=(make_medium("Opening"),),
    )
    enriched = replace(
        basic,
        media=(make_medium("Opening", composers=("Composer",)),),
    )
    provider = LookupFakeProvider(
        engine_id="provider",
        candidates=(sparse,),
        basic_candidates={"release-1": basic},
        enriched_candidate=enriched,
    )
    service = LookupService(ProviderCoordinator((provider,)))
    group = make_group(make_media_file("01.flac", title="Opening"))
    context = lookup_context()
    lookup = service.search_and_rank_group(group, context, per_engine_limit=1)
    events = RecordingEventSink()

    selected = service.select_candidate_metadata(
        group,
        lookup,
        ("provider", "provider", "release-1", 0),
        context,
        events=events,
    )

    assert selected.selected_candidate is not None
    assert events.events == [
        OperationStageChanged(context.operation_id, LookupOperationStage.ENRICHING_SELECTION),
        OperationProgress(
            context.operation_id,
            LookupOperationStage.ENRICHING_SELECTION,
            0,
            1,
        ),
        ProviderStarted(context.operation_id, "provider"),
        ProviderCompleted(context.operation_id, "provider"),
        OperationProgress(
            context.operation_id,
            LookupOperationStage.ENRICHING_SELECTION,
            1,
            1,
        ),
        OperationStageChanged(context.operation_id, LookupOperationStage.MAPPING_TRACKS),
        OperationProgress(context.operation_id, LookupOperationStage.MAPPING_TRACKS, 0, 1),
        OperationProgress(context.operation_id, LookupOperationStage.MAPPING_TRACKS, 1, 1),
        OperationStageChanged(context.operation_id, LookupOperationStage.BUILDING_PROPOSALS),
        OperationProgress(
            context.operation_id,
            LookupOperationStage.BUILDING_PROPOSALS,
            0,
            1,
        ),
        OperationProgress(
            context.operation_id,
            LookupOperationStage.BUILDING_PROPOSALS,
            1,
            1,
        ),
    ]


def test_explicit_selection_emits_provider_failure_and_stops_before_mapping() -> None:
    sparse = make_candidate("provider", "release-1")
    basic = make_candidate(
        "provider",
        "release-1",
        media=(make_medium("Opening"),),
    )
    provider = LookupFakeProvider(
        engine_id="provider",
        candidates=(sparse,),
        basic_candidates={"release-1": basic},
        enrichment_failure=ProviderTransportError(
            code=ProviderErrorCode.SERVICE_UNAVAILABLE,
            context=ProviderTransportErrorContext(attempt_count=1),
        ),
    )
    service = LookupService(ProviderCoordinator((provider,)))
    group = make_group(make_media_file("01.flac", title="Opening"))
    context = lookup_context()
    lookup = service.search_and_rank_group(group, context, per_engine_limit=1)
    events = RecordingEventSink()

    selected = service.select_candidate_metadata(
        group,
        lookup,
        ("provider", "provider", "release-1", 0),
        context,
        events=events,
    )

    assert selected.selected_candidate is None
    assert selected.candidate_lookup is lookup
    assert selected.failures[0].issue.code is ProviderErrorCode.SERVICE_UNAVAILABLE
    assert events.events == [
        OperationStageChanged(context.operation_id, LookupOperationStage.ENRICHING_SELECTION),
        OperationProgress(
            context.operation_id,
            LookupOperationStage.ENRICHING_SELECTION,
            0,
            1,
        ),
        ProviderStarted(context.operation_id, "provider"),
        ProviderFailed(context.operation_id, "provider", selected.failures[0].issue),
        OperationProgress(
            context.operation_id,
            LookupOperationStage.ENRICHING_SELECTION,
            1,
            1,
        ),
    ]


def test_failed_selection_checks_cancellation_after_terminal_enrichment_progress() -> None:
    cancellation = MutableCancellationToken()
    sparse = make_candidate("provider", "release-1")
    basic = make_candidate(
        "provider",
        "release-1",
        media=(make_medium("Opening"),),
    )
    provider = LookupFakeProvider(
        engine_id="provider",
        candidates=(sparse,),
        basic_candidates={"release-1": basic},
        enrichment_failure=ProviderTransportError(
            code=ProviderErrorCode.SERVICE_UNAVAILABLE,
            context=ProviderTransportErrorContext(attempt_count=1),
        ),
    )
    service = LookupService(ProviderCoordinator((provider,)))
    group = make_group(make_media_file("01.flac", title="Opening"))
    context = lookup_context()
    lookup = service.search_and_rank_group(group, context, per_engine_limit=1)
    events = CancellingProgressSink(
        cancellation,
        LookupOperationStage.ENRICHING_SELECTION,
        1,
    )

    with pytest.raises(OperationCancelledError, match="operation was cancelled"):
        service.select_candidate_metadata(
            group,
            lookup,
            ("provider", "provider", "release-1", 0),
            context,
            cancellation=cancellation,
            events=events,
        )


def test_explicit_selection_observes_cancellation_after_the_provider_call() -> None:
    cancellation = MutableCancellationToken()
    sparse = make_candidate("provider", "release-1")
    basic = make_candidate(
        "provider",
        "release-1",
        media=(make_medium("Opening"),),
    )
    provider = LookupFakeProvider(
        engine_id="provider",
        candidates=(sparse,),
        basic_candidates={"release-1": basic},
        enriched_candidate=replace(
            basic,
            media=(make_medium("Opening", composers=("Composer",)),),
        ),
        after_enrichment=cancellation.cancel,
    )
    service = LookupService(ProviderCoordinator((provider,)))
    group = make_group(make_media_file("01.flac", title="Opening"))
    context = lookup_context()
    lookup = service.search_and_rank_group(group, context, per_engine_limit=1)
    events = RecordingEventSink()

    with pytest.raises(OperationCancelledError, match="operation was cancelled"):
        service.select_candidate_metadata(
            group,
            lookup,
            ("provider", "provider", "release-1", 0),
            context,
            cancellation=cancellation,
            events=events,
        )

    assert ProviderStarted(context.operation_id, "provider") in events.events
    assert ProviderCompleted(context.operation_id, "provider") not in events.events
    assert not any(
        isinstance(event, OperationStageChanged)
        and event.stage == LookupOperationStage.MAPPING_TRACKS
        for event in events.events
    )


def test_explicit_selection_checks_cancellation_between_proposal_files() -> None:
    cancellation = MutableCancellationToken()
    sparse = make_candidate("provider", "release-1")
    basic = make_candidate(
        "provider",
        "release-1",
        media=(make_medium("Opening", "Finale"),),
    )
    enriched = replace(
        basic,
        media=(make_medium("Opening", "Finale", composers=("Composer",)),),
    )
    provider = LookupFakeProvider(
        engine_id="provider",
        candidates=(sparse,),
        basic_candidates={"release-1": basic},
        enriched_candidate=enriched,
    )
    service = LookupService(ProviderCoordinator((provider,)))
    group = make_group(
        make_media_file("01.flac", title="Opening"),
        make_media_file("02.flac", title="Finale"),
    )
    context = lookup_context()
    lookup = service.search_and_rank_group(group, context, per_engine_limit=1)
    events = CancellingProgressSink(
        cancellation,
        LookupOperationStage.BUILDING_PROPOSALS,
        1,
    )

    with pytest.raises(OperationCancelledError, match="operation was cancelled"):
        service.select_candidate_metadata(
            group,
            lookup,
            ("provider", "provider", "release-1", 0),
            context,
            cancellation=cancellation,
            events=events,
        )

    proposal_progress = [
        event
        for event in events.events
        if isinstance(event, OperationProgress)
        and event.stage == LookupOperationStage.BUILDING_PROPOSALS
    ]
    assert proposal_progress == [
        OperationProgress(
            context.operation_id,
            LookupOperationStage.BUILDING_PROPOSALS,
            0,
            2,
        ),
        OperationProgress(
            context.operation_id,
            LookupOperationStage.BUILDING_PROPOSALS,
            1,
            2,
        ),
    ]


def test_explicit_selection_checks_cancellation_between_mapping_and_proposals() -> None:
    cancellation = MutableCancellationToken()
    sparse = make_candidate("provider", "release-1")
    basic = make_candidate(
        "provider",
        "release-1",
        media=(make_medium("Opening"),),
    )
    provider = LookupFakeProvider(
        engine_id="provider",
        candidates=(sparse,),
        basic_candidates={"release-1": basic},
        enriched_candidate=replace(
            basic,
            media=(make_medium("Opening", composers=("Composer",)),),
        ),
    )
    service = LookupService(ProviderCoordinator((provider,)))
    group = make_group(make_media_file("01.flac", title="Opening"))
    context = lookup_context()
    lookup = service.search_and_rank_group(group, context, per_engine_limit=1)
    events = CancellingProgressSink(
        cancellation,
        LookupOperationStage.MAPPING_TRACKS,
        1,
    )

    with pytest.raises(OperationCancelledError, match="operation was cancelled"):
        service.select_candidate_metadata(
            group,
            lookup,
            ("provider", "provider", "release-1", 0),
            context,
            cancellation=cancellation,
            events=events,
        )

    assert not any(
        isinstance(event, OperationStageChanged)
        and event.stage == LookupOperationStage.BUILDING_PROPOSALS
        for event in events.events
    )


def test_explicit_selection_checks_cancellation_between_enrichment_and_mapping() -> None:
    cancellation = MutableCancellationToken()
    sparse = make_candidate("provider", "release-1")
    basic = make_candidate(
        "provider",
        "release-1",
        media=(make_medium("Opening"),),
    )
    provider = LookupFakeProvider(
        engine_id="provider",
        candidates=(sparse,),
        basic_candidates={"release-1": basic},
        enriched_candidate=replace(
            basic,
            media=(make_medium("Opening", composers=("Composer",)),),
        ),
    )
    service = LookupService(ProviderCoordinator((provider,)))
    group = make_group(make_media_file("01.flac", title="Opening"))
    context = lookup_context()
    lookup = service.search_and_rank_group(group, context, per_engine_limit=1)
    events = CancellingProgressSink(
        cancellation,
        LookupOperationStage.ENRICHING_SELECTION,
        1,
    )

    with pytest.raises(OperationCancelledError, match="operation was cancelled"):
        service.select_candidate_metadata(
            group,
            lookup,
            ("provider", "provider", "release-1", 0),
            context,
            cancellation=cancellation,
            events=events,
        )

    assert not any(
        isinstance(event, OperationStageChanged)
        and event.stage == LookupOperationStage.MAPPING_TRACKS
        for event in events.events
    )


def test_explicit_selection_rejects_foreign_identity_before_enrichment() -> None:
    sparse = make_candidate("provider", "release-1")
    provider = LookupFakeProvider(
        engine_id="provider",
        candidates=(sparse,),
        basic_candidates={
            "release-1": make_candidate(
                "provider",
                "release-1",
                media=(make_medium("Opening"),),
            )
        },
    )
    service = LookupService(ProviderCoordinator((provider,)))
    group = make_group(make_media_file("01.flac", title="Opening"))
    lookup = service.search_and_rank_group(group, lookup_context(), per_engine_limit=1)

    with pytest.raises(ValueError, match="candidate ranking"):
        service.select_candidate_metadata(
            group,
            lookup,
            ("provider", "provider", "foreign", 0),
            lookup_context(),
        )

    assert provider.enrich_calls == []


def test_explicit_selection_returns_structured_failure_when_medium_tracks_drift() -> None:
    sparse = make_candidate("provider", "release-1")
    basic = make_candidate(
        "provider",
        "release-1",
        media=(make_medium("Opening", "Finale"),),
    )
    drifted = make_candidate(
        "provider",
        "release-1",
        media=(make_medium("Opening"),),
    )
    provider = LookupFakeProvider(
        engine_id="provider",
        candidates=(sparse,),
        basic_candidates={"release-1": basic},
        enriched_candidate=drifted,
    )
    service = LookupService(ProviderCoordinator((provider,)))
    group = make_group(
        make_media_file("01.flac", title="Opening"),
        make_media_file("02.flac", title="Finale"),
    )
    lookup = service.search_and_rank_group(group, lookup_context(), per_engine_limit=1)
    events = RecordingEventSink()

    result = service.select_candidate_metadata(
        group,
        lookup,
        ("provider", "provider", "release-1", 0),
        lookup_context(),
        events=events,
    )

    assert result.candidate_lookup is lookup
    assert result.selected_candidate is None
    assert result.track_mapping is None
    assert len(result.failures) == 1
    assert result.failures[0].issue.code is ProviderErrorCode.INVALID_RESPONSE
    assert ProviderFailed(
        "explicit-lookup",
        "provider",
        result.failures[0].issue,
    ) in events.events
    assert ProviderCompleted("explicit-lookup", "provider") not in events.events


def test_explicit_selection_rejects_same_number_tracks_whose_core_order_changed() -> None:
    sparse = make_candidate("provider", "release-1")
    basic = make_candidate(
        "provider",
        "release-1",
        media=(make_medium("Opening", "Finale"),),
    )
    reordered = make_candidate(
        "provider",
        "release-1",
        media=(make_medium("Finale", "Opening", composers=("Composer",)),),
    )
    provider = LookupFakeProvider(
        engine_id="provider",
        candidates=(sparse,),
        basic_candidates={"release-1": basic},
        enriched_candidate=reordered,
    )
    service = LookupService(ProviderCoordinator((provider,)))
    group = make_group(
        make_media_file("01.flac", title="Opening"),
        make_media_file("02.flac", title="Finale"),
    )
    lookup = service.search_and_rank_group(group, lookup_context(), per_engine_limit=1)

    result = service.select_candidate_metadata(
        group,
        lookup,
        ("provider", "provider", "release-1", 0),
        lookup_context(),
    )

    assert result.selected_candidate is None
    assert result.track_mapping is None
    assert result.candidate_lookup is lookup
    assert result.failures[0].issue.code is ProviderErrorCode.INVALID_RESPONSE


def test_selected_metadata_builds_proposals_all_reviews_and_a_fresh_change_set() -> None:
    sparse = make_candidate("provider", "release-1")
    basic = replace(
        make_candidate(
            "provider",
            "release-1",
            title="Folder must not become the query album",
            media=(
                make_medium(
                    "Opening",
                    artists=("Track Artist",),
                ),
            ),
        ),
        album_artists=("Album Artist",),
        date="2024-02-03",
    )
    enriched = replace(
        basic,
        media=(
            make_medium(
                "Opening",
                artists=("Track Artist",),
                composers=("Composer",),
            ),
        ),
    )
    provider = LookupFakeProvider(
        engine_id="provider",
        candidates=(sparse,),
        basic_candidates={"release-1": basic},
        enriched_candidate=enriched,
    )
    service = LookupService(ProviderCoordinator((provider,)))
    group = make_group(make_media_file("01.flac", title="Opening"))
    lookup = service.search_and_rank_group(group, lookup_context(), per_engine_limit=1)

    result = service.select_candidate_metadata(
        group,
        lookup,
        ("provider", "provider", "release-1", 0),
        lookup_context(),
        preferred_language="auto",
    )

    assert len(result.reviewed_files) == 1
    reviewed = result.reviewed_files[0]
    assert reviewed.file_id == group.files[0].file_id
    assert {proposal.field for proposal in reviewed.proposals} == {
        MetadataField.TITLE,
        MetadataField.ARTISTS,
        MetadataField.ALBUM,
        MetadataField.ALBUM_ARTISTS,
        MetadataField.COMPOSERS,
        MetadataField.TRACK,
        MetadataField.DISC,
        MetadataField.DATE,
    }
    assert all(proposal.confidence is FieldConfidence.HIGH for proposal in reviewed.proposals)
    assert all(
        proposal.reason_codes == (ReviewReasonCode.FIELD_MATCH_HIGH,)
        for proposal in reviewed.proposals
    )
    assert all(proposal.provenance.engine_id == "provider" for proposal in reviewed.proposals)
    assert tuple(review.field for review in reviewed.reviews) == tuple(MetadataField)
    assert reviewed.change_set.file_id == group.files[0].file_id
    assert reviewed.change_set.final_metadata.composers == ("Composer",)
    assert reviewed.change_set.rename_decision.value == "keep_filename"
    assert reviewed.change_set.rename_preview is not None


def test_unmatched_file_gets_only_release_proposals_and_complete_review_state() -> None:
    sparse = make_candidate("provider", "release-1")
    basic = replace(
        make_candidate(
            "provider",
            "release-1",
            title="Folder must not become the query album",
            media=(make_medium("Opening", artists=("Track Artist",)),),
        ),
        album_artists=("Album Artist",),
        date="2024",
    )
    enriched = replace(
        basic,
        media=(
            make_medium(
                "Opening",
                artists=("Track Artist",),
                composers=("Composer",),
            ),
        ),
    )
    provider = LookupFakeProvider(
        engine_id="provider",
        candidates=(sparse,),
        basic_candidates={"release-1": basic},
        enriched_candidate=enriched,
    )
    service = LookupService(ProviderCoordinator((provider,)))
    group = make_group(
        make_media_file("01.flac", title="Opening"),
        make_media_file("02.flac", title="Unmatched local track"),
    )
    lookup = service.search_and_rank_group(group, lookup_context(), per_engine_limit=1)
    selected = service.select_candidate_metadata(
        group,
        lookup,
        ("provider", "provider", "release-1", 0),
        lookup_context(),
    )
    assert selected.track_mapping is not None
    assert len(selected.track_mapping.unmatched_local_file_ids) == 1
    unmatched_id = selected.track_mapping.unmatched_local_file_ids[0]
    unmatched = next(item for item in selected.reviewed_files if item.file_id == unmatched_id)

    assert {proposal.field for proposal in unmatched.proposals} == {
        MetadataField.ALBUM,
        MetadataField.ALBUM_ARTISTS,
        MetadataField.DISC,
        MetadataField.DATE,
    }
    assert MetadataField.GENRES not in {proposal.field for proposal in unmatched.proposals}
    assert unmatched.track_mapping_resolved is False
    assert tuple(review.field for review in unmatched.reviews) == tuple(MetadataField)
    assert unmatched.change_set.file_id == unmatched_id
    assert unmatched.change_set.rename_decision.value == "keep_filename"


def coordinated_candidate(
    candidate: ReleaseCandidate,
    *,
    include_secondary_provenance: bool = False,
) -> CoordinatedCandidate:
    primary = MetadataProvenance(
        engine_id=candidate.engine_id,
        source_id=candidate.source_id,
        record_id=candidate.release_id,
        source_url=candidate.source_url,
        language=None,
        operation_id="explicit-lookup",
    )
    provenance = (primary,)

    if include_secondary_provenance:
        provenance = (
            primary,
            replace(primary, engine_id="secondary-engine"),
        )

    return CoordinatedCandidate(candidate=candidate, provenance=provenance)


def direct_mapping(
    file: LocalMediaFile,
    *,
    classification: MatchClassification,
    medium_number: int = 1,
) -> TrackMappingResult:
    mapping = TrackMapping(
        local_file_id=file.file_id,
        provider_track_index=0,
        track_position=Position(number=1, total=1),
        disc_position=Position(number=medium_number, total=1),
        score=75.0,
        classification=classification,
        evidence=(),
    )

    return TrackMappingResult(
        mappings=(mapping,),
        unmatched_local_file_ids=(),
        unmatched_provider_indexes=(),
        selected_medium_index=0,
        selected_medium_number=medium_number,
        classification=classification,
        evidence=(),
    )


def test_pure_builder_uses_worse_track_confidence_and_injected_change_settings() -> None:
    file = make_media_file("01.flac", title="Opening")
    candidate = replace(
        make_candidate(
            "provider",
            "release-1",
            title="Album",
            media=(
                make_medium(
                    "Opening",
                    artists=("Track Artist",),
                    composers=("Composer",),
                ),
            ),
        ),
        album_artists=("Album Artist",),
        date="2024",
    )
    reviewed = build_selected_file_results(
        (file,),
        coordinated_candidate(candidate),
        medium_index=0,
        mapping_result=direct_mapping(file, classification=MatchClassification.REVIEW),
        release_classification=MatchClassification.HIGH,
        preferred_language="auto",
        validation_facts_by_file={
            file.file_id: ChangeValidationFacts(adapter_available=False),
        },
        rename_decisions_by_file={file.file_id: RenameDecision.APPLY_RENAME},
        rename_template="%title%",
        rename_policy=FilenameRenderPolicy(),
    )[0]

    release_fields = {
        MetadataField.ALBUM,
        MetadataField.ALBUM_ARTISTS,
        MetadataField.DATE,
        MetadataField.DISC,
    }
    track_fields = {
        MetadataField.TITLE,
        MetadataField.ARTISTS,
        MetadataField.COMPOSERS,
        MetadataField.TRACK,
    }
    assert all(
        proposal.confidence is FieldConfidence.HIGH
        and proposal.reason_codes == (ReviewReasonCode.FIELD_MATCH_HIGH,)
        for proposal in reviewed.proposals
        if proposal.field in release_fields
    )
    assert all(
        proposal.confidence is FieldConfidence.REVIEW
        and proposal.reason_codes == (ReviewReasonCode.FIELD_MATCH_REVIEW,)
        for proposal in reviewed.proposals
        if proposal.field in track_fields
    )
    assert reviewed.change_set.rename_decision is RenameDecision.APPLY_RENAME
    assert reviewed.change_set.rename_preview is not None
    assert any(
        issue.code is ChangeIssueCode.ADAPTER_UNAVAILABLE
        for issue in reviewed.change_set.validation.issues
    )


@pytest.mark.parametrize(
    "read_state",
    (FieldReadState.UNREADABLE, FieldReadState.UNSUPPORTED),
)
def test_pure_builder_preserves_semantic_unreadable_or_unsupported_values(
    read_state: FieldReadState,
) -> None:
    file = make_media_file(
        "01.flac",
        title="Captured raw title",
        title_state=read_state,
    )
    candidate = make_candidate(
        "provider",
        "release-1",
        media=(make_medium("Provider title", composers=("Composer",)),),
    )
    reviewed = build_selected_file_results(
        (file,),
        coordinated_candidate(candidate),
        medium_index=0,
        mapping_result=direct_mapping(file, classification=MatchClassification.HIGH),
        release_classification=MatchClassification.HIGH,
        preferred_language="auto",
    )[0]
    title_review = next(
        review for review in reviewed.reviews if review.field is MetadataField.TITLE
    )

    assert title_review.existing_value == "Captured raw title"
    assert title_review.decision is FieldDecisionKind.KEEP_EXISTING
    assert reviewed.change_set.final_metadata.title == "Captured raw title"


def test_pure_builder_uses_album_profile_when_target_field_has_no_local_text() -> None:
    file = make_media_file("01.flac", title="星のカービィ")
    candidate = replace(
        make_candidate(
            "provider",
            "release-1",
            media=(make_medium("Provider track", composers=("Composer",)),),
        ),
        titles=(
            LocalisedText(value="Kirby", language="en", script="Latn"),
            LocalisedText(value="Hoshi no Kirby", language="ja", script="Latn"),
        ),
    )
    reviewed = build_selected_file_results(
        (file,),
        coordinated_candidate(candidate),
        medium_index=0,
        mapping_result=direct_mapping(file, classification=MatchClassification.HIGH),
        release_classification=MatchClassification.HIGH,
        preferred_language="romanised",
    )[0]
    album_review = next(
        review for review in reviewed.reviews if review.field is MetadataField.ALBUM
    )

    assert album_review.existing_value is None
    assert album_review.proposals[0].value == "Hoshi no Kirby"
    assert ReviewReasonCode.LANGUAGE_MATCH in album_review.proposals[0].reason_codes
    assert ReviewReasonCode.LANGUAGE_MATCH not in album_review.proposals[1].reason_codes


def test_pure_builder_rejects_a_mapping_for_a_different_medium_number() -> None:
    file = make_media_file("01.flac", title="Opening")
    candidate = make_candidate(
        "provider",
        "release-1",
        media=(make_medium("Opening", medium_number=1, composers=("Composer",)),),
    )

    with pytest.raises(ValueError, match="selected medium number"):
        build_selected_file_results(
            (file,),
            coordinated_candidate(candidate),
            medium_index=0,
            mapping_result=direct_mapping(
                file,
                classification=MatchClassification.HIGH,
                medium_number=2,
            ),
            release_classification=MatchClassification.HIGH,
            preferred_language="auto",
        )


def test_pure_builder_deduplicates_provider_variants_and_uses_only_representative_source() -> None:
    file = make_media_file("01.flac", title="Opening")
    duplicate_album_title = LocalisedText(value="Album", language="ja", script="Latn")
    duplicate_track_title = LocalisedText(value="Opening", language="ja", script="Latn")
    candidate = replace(
        make_candidate(
            "provider",
            "release-1",
            media=(make_medium("Opening", composers=("Composer",)),),
        ),
        titles=(duplicate_album_title, duplicate_album_title),
        media=(
            replace(
                make_medium("Opening", composers=("Composer",)),
                tracks=(
                    replace(
                        make_medium("Opening", composers=("Composer",)).tracks[0],
                        titles=(duplicate_track_title, duplicate_track_title),
                    ),
                ),
            ),
        ),
    )
    selected = coordinated_candidate(candidate, include_secondary_provenance=True)
    reviewed = build_selected_file_results(
        (file,),
        selected,
        medium_index=0,
        mapping_result=direct_mapping(file, classification=MatchClassification.HIGH),
        release_classification=MatchClassification.HIGH,
        preferred_language="ja",
    )[0]

    assert len(reviewed.proposals) == len(set(reviewed.proposals))
    assert sum(item.field is MetadataField.ALBUM for item in reviewed.proposals) == 1
    assert sum(item.field is MetadataField.TITLE for item in reviewed.proposals) == 1
    assert all(item.provenance.engine_id == "provider" for item in reviewed.proposals)
    assert all(item.provenance.record_id == "release-1" for item in reviewed.proposals)
    assert all(
        item.provenance.source_url == "https://catalogue.invalid/release-1"
        for item in reviewed.proposals
    )
    assert all(item.provenance.operation_id == "explicit-lookup" for item in reviewed.proposals)
    assert all(
        item.provenance.language == item.language
        for item in reviewed.proposals
        if item.field in (MetadataField.ALBUM, MetadataField.TITLE)
    )


def test_pure_builder_omits_blank_provider_text_values_and_never_proposes_genres() -> None:
    file = make_media_file("01.flac", title="Opening")
    blank_track = ProviderTrack(
        track_number=1,
        titles=(LocalisedText(value="  ", language="en", script="Latn"),),
        artists=(" ",),
        composers=("",),
        duration_seconds=180.0,
    )
    candidate = replace(
        make_candidate(
            "provider",
            "release-1",
            media=(ReleaseMedium(medium_number=1, title=None, tracks=(blank_track,)),),
        ),
        titles=(LocalisedText(value=" ", language="en", script="Latn"),),
        album_artists=("",),
        date=" ",
    )
    reviewed = build_selected_file_results(
        (file,),
        coordinated_candidate(candidate),
        medium_index=0,
        mapping_result=direct_mapping(file, classification=MatchClassification.HIGH),
        release_classification=MatchClassification.HIGH,
        preferred_language="auto",
    )[0]

    assert {item.field for item in reviewed.proposals} == {
        MetadataField.TRACK,
        MetadataField.DISC,
    }
    assert MetadataField.GENRES not in {item.field for item in reviewed.proposals}
