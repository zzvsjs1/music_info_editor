from dataclasses import dataclass, field, replace
from typing import cast

import pytest

from metadata_polisher.domain.errors import ProviderErrorCode
from metadata_polisher.domain.matching import (
    LocalisedText,
    ProviderTrack,
    ReleaseCandidate,
    ReleaseMedium,
    ReleaseSearchQuery,
)
from metadata_polisher.providers.base import ProviderCapabilities, RequestContext
from metadata_polisher.providers.coordinator import ProviderCoordinator
from metadata_polisher.providers.transport import ProviderTransportError, ProviderTransportErrorContext


# Opaque source IDs and separate engine IDs let tests distinguish true
# duplicate retrievals from equally named releases in different catalogues.
def make_candidate(
    engine_id: str,
    release_id: str,
    *,
    source_id: str | None = None,
    title: str | None = None,
) -> ReleaseCandidate:
    return ReleaseCandidate(
        engine_id=engine_id,
        source_id=source_id or engine_id,
        release_id=release_id,
        titles=(LocalisedText(value=title or release_id, language="eng", script="Latn"),),
        album_artists=(),
        date=None,
        media=(),
        source_url=f"https://catalogue.invalid/{release_id}",
    )


def make_query() -> ReleaseSearchQuery:
    return ReleaseSearchQuery(
        album="Album",
        artists=(),
        year=None,
        disc_hint=None,
        local_track_count=2,
        distinctive_titles=(),
    )


def make_context() -> RequestContext:
    return RequestContext(operation_id="lookup-14", preferred_language="eng")


@pytest.mark.parametrize("code", [
    ProviderErrorCode.ACCESS_DENIED,
    ProviderErrorCode.AUTHENTICATION_REQUIRED,
    ProviderErrorCode.RATE_LIMITED,
])
def test_terminal_access_failure_stops_fallbacks_without_hiding_other_providers(code) -> None:
    blocked = FakeProvider("blocked", failure=make_transport_error(code))
    empty = FakeProvider("empty")
    found = FakeProvider("found", candidates=(make_candidate("found", "release"),))
    query = make_query()
    queries = (query, replace(query, local_track_count=0))

    result = ProviderCoordinator((blocked, empty, found)).search_release_queries(queries, make_context())

    assert blocked.call_log == ["blocked"]
    assert empty.call_log == ["empty", "empty"]
    assert found.call_log == ["found", "found"]
    assert len(result.failures) == 1
    assert len(result.candidates) == 1
    assert [
        (item.engine_id, item.attempted_queries, item.successful_queries, item.candidate_count)
        for item in result.summaries
    ] == [("blocked", 1, 0, 0), ("empty", 2, 2, 0), ("found", 2, 2, 1)]


def test_successful_empty_search_is_retained_in_provider_summary() -> None:
    result = ProviderCoordinator((FakeProvider("empty"),)).search_releases(make_query(), make_context())

    assert result.failures == ()
    assert result.candidates == ()
    assert len(result.summaries) == 1
    assert result.summaries[0].successful_queries == 1
    assert result.summaries[0].candidate_count == 0


def make_transport_error(code: ProviderErrorCode) -> ProviderTransportError:
    return ProviderTransportError(
        code=code,
        context=ProviderTransportErrorContext(attempt_count=1),
    )


@dataclass
class FakeProvider:
    engine_id: str
    candidates: tuple[ReleaseCandidate, ...] = ()
    failure: Exception | None = None
    raw_candidates: object | None = None
    enriched_candidate: ReleaseCandidate | None = None
    enrichment_failure: Exception | None = None
    media_candidate: ReleaseCandidate | None = None
    media_failure: Exception | None = None
    call_log: list[str] = field(default_factory=list)
    media_calls: list[str] = field(default_factory=list)

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            release_search=True,
            track_listing=True,
            composer_credits=False,
            multilingual_titles=False,
        )

    def search_releases(
        self,
        query: ReleaseSearchQuery,
        context: RequestContext,
    ) -> tuple[ReleaseCandidate, ...]:
        self.call_log.append(self.engine_id)

        if self.failure is not None:
            raise self.failure

        if self.raw_candidates is not None:
            return cast(tuple[ReleaseCandidate, ...], self.raw_candidates)

        return self.candidates

    def enrich_release(
        self,
        candidate: ReleaseCandidate,
        context: RequestContext,
    ) -> ReleaseCandidate:
        if self.enrichment_failure is not None:
            raise self.enrichment_failure

        return self.enriched_candidate or candidate

    def load_release_media(
        self,
        candidate: ReleaseCandidate,
        context: RequestContext,
    ) -> ReleaseCandidate:
        self.media_calls.append(candidate.release_id)

        if self.media_failure is not None:
            raise self.media_failure

        return self.media_candidate or candidate


def with_media(candidate: ReleaseCandidate, *, composer: str | None = None) -> ReleaseCandidate:
    return replace(
        candidate,
        media=(
            ReleaseMedium(
                medium_number=1,
                title=None,
                tracks=(
                    ProviderTrack(
                        track_number=1,
                        titles=(LocalisedText(value="Track 1", language="eng", script="Latn"),),
                        artists=("Artist",),
                        composers=(composer,) if composer is not None else (),
                        duration_seconds=180.0,
                    ),
                ),
            ),
        ),
    )


def with_empty_medium(candidate: ReleaseCandidate) -> ReleaseCandidate:
    return replace(
        candidate,
        media=(ReleaseMedium(medium_number=1, title=None, tracks=()),),
    )


def test_load_release_media_preserves_identity_and_provenance_without_composers() -> None:
    candidate = make_candidate("provider", "release-1", source_id="catalogue")
    provider = FakeProvider(
        engine_id="provider",
        candidates=(candidate,),
        media_candidate=with_media(candidate),
    )
    coordinator = ProviderCoordinator((provider,))
    context = make_context()
    selected = coordinator.search_releases(make_query(), context).candidates[0]

    result = coordinator.load_release_media((selected,), context, per_engine_limit=1)

    assert provider.media_calls == ["release-1"]
    assert len(result.candidates) == 1
    hydrated = result.candidates[0]
    assert (
        hydrated.candidate.engine_id,
        hydrated.candidate.source_id,
        hydrated.candidate.release_id,
    ) == ("provider", "catalogue", "release-1")
    assert hydrated.provenance == selected.provenance
    assert hydrated.candidate.media[0].tracks[0].composers == ()
    assert result.notices == ()


def test_load_release_media_rejects_composer_leakage_as_candidate_specific_invalid_response() -> None:
    candidate = make_candidate("provider", "release-1", source_id="catalogue")
    provider = FakeProvider(
        engine_id="provider",
        candidates=(candidate,),
        media_candidate=with_media(candidate, composer="Too Early"),
    )
    coordinator = ProviderCoordinator((provider,))
    context = make_context()
    selected = coordinator.search_releases(make_query(), context).candidates[0]

    result = coordinator.load_release_media((selected,), context, per_engine_limit=1)

    assert result.candidates == (selected,)
    assert len(result.notices) == 1
    assert result.notices[0].candidate_identity == (
        "provider",
        "catalogue",
        "release-1",
    )
    assert result.notices[0].reason_code.value == "BASIC_MEDIA_INVALID"
    assert result.notices[0].issue is not None
    assert result.notices[0].issue.code is ProviderErrorCode.INVALID_RESPONSE


# Each configured engine gets its own attempt budget; one catalogue's many
# hits must not consume another catalogue's basic-media allowance.
def test_load_release_media_applies_the_bound_independently_per_engine() -> None:
    first_candidates = (
        make_candidate("first", "first-1"),
        make_candidate("first", "first-2"),
    )
    second_candidates = (
        make_candidate("second", "second-1"),
        make_candidate("second", "second-2"),
    )
    first = FakeProvider(
        engine_id="first",
        candidates=first_candidates,
        media_candidate=with_media(first_candidates[0]),
    )
    second = FakeProvider(
        engine_id="second",
        candidates=second_candidates,
        media_candidate=with_media(second_candidates[0]),
    )
    coordinator = ProviderCoordinator((first, second))
    context = make_context()
    searched = coordinator.search_releases(make_query(), context)

    result = coordinator.load_release_media(
        searched.candidates,
        context,
        per_engine_limit=1,
    )

    assert first.media_calls == ["first-1"]
    assert second.media_calls == ["second-1"]
    assert tuple(bool(item.candidate.media) for item in result.candidates) == (
        True,
        False,
        True,
        False,
    )
    assert tuple(notice.candidate_identity for notice in result.notices) == (
        ("first", "first", "first-2"),
        ("second", "second", "second-2"),
    )
    assert all(
        notice.reason_code.value == "PER_ENGINE_LIMIT_REACHED"
        and notice.issue is None
        for notice in result.notices
    )


def test_load_release_media_isolates_a_typed_failure_to_the_exact_candidate() -> None:
    candidate = make_candidate("provider", "release-1", source_id="catalogue")
    provider = FakeProvider(
        engine_id="provider",
        candidates=(candidate,),
        media_failure=make_transport_error(ProviderErrorCode.SERVICE_UNAVAILABLE),
    )
    coordinator = ProviderCoordinator((provider,))
    context = make_context()
    selected = coordinator.search_releases(make_query(), context).candidates[0]

    result = coordinator.load_release_media((selected,), context, per_engine_limit=1)

    assert result.candidates == (selected,)
    assert len(result.notices) == 1
    notice = result.notices[0]
    assert notice.candidate_identity == ("provider", "catalogue", "release-1")
    assert notice.reason_code.value == "BASIC_MEDIA_LOAD_FAILED"
    assert notice.issue is not None
    assert notice.issue.code is ProviderErrorCode.SERVICE_UNAVAILABLE


@pytest.mark.parametrize(
    "invalid_media",
    [
        pytest.param(None, id="empty-media"),
        pytest.param("empty-medium", id="empty-medium"),
        pytest.param("changed-identity", id="changed-identity"),
    ],
)
def test_load_release_media_rejects_invalid_candidate_specific_results(
    invalid_media: str | None,
) -> None:
    candidate = make_candidate("provider", "release-1", source_id="catalogue")
    if invalid_media is None:
        returned = candidate
    elif invalid_media == "empty-medium":
        returned = with_empty_medium(candidate)
    else:
        returned = with_media(make_candidate("provider", invalid_media, source_id="catalogue"))
    provider = FakeProvider(
        engine_id="provider",
        candidates=(candidate,),
        media_candidate=returned,
    )
    coordinator = ProviderCoordinator((provider,))
    context = make_context()
    selected = coordinator.search_releases(make_query(), context).candidates[0]

    result = coordinator.load_release_media((selected,), context, per_engine_limit=1)

    assert result.candidates == (selected,)
    assert len(result.notices) == 1
    notice = result.notices[0]
    assert notice.candidate_identity == ("provider", "catalogue", "release-1")
    assert notice.reason_code.value == "BASIC_MEDIA_INVALID"
    assert notice.issue is not None
    assert notice.issue.code is ProviderErrorCode.INVALID_RESPONSE


def test_prepopulated_empty_medium_is_hydrated_instead_of_treated_as_valid() -> None:
    sparse = with_empty_medium(make_candidate("provider", "release-1"))
    provider = FakeProvider(
        engine_id="provider",
        candidates=(sparse,),
        media_candidate=with_media(sparse),
    )
    coordinator = ProviderCoordinator((provider,))
    context = make_context()
    selected = coordinator.search_releases(make_query(), context).candidates[0]

    result = coordinator.load_release_media((selected,), context, per_engine_limit=1)

    assert provider.media_calls == ["release-1"]
    assert len(result.candidates[0].candidate.media[0].tracks) == 1
    assert result.notices == ()


def test_valid_prepopulated_media_does_not_call_or_consume_the_engine_bound() -> None:
    ready = with_media(make_candidate("provider", "ready"))
    sparse = make_candidate("provider", "sparse")
    provider = FakeProvider(
        engine_id="provider",
        candidates=(ready, sparse),
        media_candidate=with_media(sparse),
    )
    coordinator = ProviderCoordinator((provider,))
    context = make_context()
    searched = coordinator.search_releases(make_query(), context)

    result = coordinator.load_release_media(
        searched.candidates,
        context,
        per_engine_limit=1,
    )

    assert provider.media_calls == ["sparse"]
    assert all(candidate.candidate.media for candidate in result.candidates)
    assert result.notices == ()


def test_provider_priority_determines_call_and_candidate_display_order() -> None:
    calls: list[str] = []
    second = FakeProvider(
        engine_id="second",
        candidates=(make_candidate("second", "release-2"),),
        call_log=calls,
    )
    first = FakeProvider(
        engine_id="first",
        candidates=(make_candidate("first", "release-1"),),
        call_log=calls,
    )
    coordinator = ProviderCoordinator((first, second))

    result = coordinator.search_releases(make_query(), make_context())

    assert calls == ["first", "second"]
    assert tuple(hit.candidate.release_id for hit in result.candidates) == (
        "release-1",
        "release-2",
    )
    assert result.failures == ()


def test_one_typed_provider_failure_is_isolated_from_another_success() -> None:
    unavailable = FakeProvider(
        engine_id="unavailable",
        failure=make_transport_error(ProviderErrorCode.SERVICE_UNAVAILABLE),
    )
    working = FakeProvider(
        engine_id="working",
        candidates=(make_candidate("working", "release-ok"),),
    )

    result = ProviderCoordinator((unavailable, working)).search_releases(
        make_query(),
        make_context(),
    )

    assert tuple(hit.candidate.release_id for hit in result.candidates) == ("release-ok",)
    assert len(result.failures) == 1
    assert result.failures[0].engine_id == "unavailable"
    assert result.failures[0].issue.code is ProviderErrorCode.SERVICE_UNAVAILABLE
    assert result.failures[0].issue.technical_detail == "attempts=1, status=None, retry_after=None"


def test_all_provider_failures_are_returned_as_structured_ordered_results() -> None:
    first = FakeProvider(
        engine_id="first",
        failure=make_transport_error(ProviderErrorCode.NETWORK_TIMEOUT),
    )
    second = FakeProvider(
        engine_id="second",
        failure=make_transport_error(ProviderErrorCode.AUTHENTICATION_REQUIRED),
    )

    result = ProviderCoordinator((first, second)).search_releases(
        make_query(),
        make_context(),
    )

    assert result.candidates == ()
    assert tuple(failure.engine_id for failure in result.failures) == ("first", "second")
    assert tuple(failure.issue.code for failure in result.failures) == (
        ProviderErrorCode.NETWORK_TIMEOUT,
        ProviderErrorCode.AUTHENTICATION_REQUIRED,
    )


def test_exact_opaque_source_identity_deduplicates_without_erasing_provenance() -> None:
    primary_candidate = make_candidate(
        "primary",
        "Release-ID",
        source_id="shared-catalogue",
        title="Primary display value",
    )
    secondary_duplicate = make_candidate(
        "secondary",
        "Release-ID",
        source_id="shared-catalogue",
        title="Lower-priority display value",
    )
    opaque_case_variant = make_candidate(
        "secondary",
        "release-id",
        source_id="shared-catalogue",
        title="Distinct opaque ID",
    )
    coordinator = ProviderCoordinator(
        (
            FakeProvider(engine_id="primary", candidates=(primary_candidate,)),
            FakeProvider(
                engine_id="secondary",
                candidates=(secondary_duplicate, opaque_case_variant),
            ),
        )
    )

    result = coordinator.search_releases(make_query(), make_context())

    assert len(result.candidates) == 2
    merged = result.candidates[0]
    assert merged.candidate is primary_candidate
    assert tuple(provenance.engine_id for provenance in merged.provenance) == (
        "primary",
        "secondary",
    )
    assert all(provenance.source_id == "shared-catalogue" for provenance in merged.provenance)
    assert all(provenance.record_id == "Release-ID" for provenance in merged.provenance)
    assert all(provenance.operation_id == "lookup-14" for provenance in merged.provenance)
    assert result.candidates[1].candidate is opaque_case_variant


def test_duplicate_configured_engine_ids_are_rejected_before_any_lookup() -> None:
    first = FakeProvider(engine_id="duplicate")
    second = FakeProvider(engine_id="duplicate")

    with pytest.raises(ValueError, match="duplicate provider engine ID"):
        ProviderCoordinator((first, second))

    assert first.call_log == []
    assert second.call_log == []


def test_provider_priority_requires_an_ordered_sequence() -> None:
    provider = FakeProvider(engine_id="provider")

    with pytest.raises(TypeError, match="ordered sequence"):
        ProviderCoordinator(iter((provider,)))


def test_multiple_queries_keep_provider_priority_and_deduplicate_repeated_hits() -> None:
    calls: list[str] = []
    first_candidate = make_candidate("first", "release-1")
    second_candidate = make_candidate("second", "release-2")
    first = FakeProvider(engine_id="first", candidates=(first_candidate,), call_log=calls)
    second = FakeProvider(engine_id="second", candidates=(second_candidate,), call_log=calls)
    coordinator = ProviderCoordinator((first, second))
    queries = (
        make_query(),
        ReleaseSearchQuery(
            album="Album",
            artists=(),
            year=None,
            disc_hint=None,
            local_track_count=0,
            distinctive_titles=(),
        ),
    )

    result = coordinator.search_release_queries(queries, make_context())

    assert calls == ["first", "first", "second", "second"]
    assert tuple(hit.candidate for hit in result.candidates) == (first_candidate, second_candidate)
    assert all(len(hit.provenance) == 1 for hit in result.candidates)


def test_wrong_returned_engine_attribution_becomes_safe_invalid_response_failure() -> None:
    dishonest = FakeProvider(
        engine_id="configured-engine",
        candidates=(make_candidate("different-engine", "release-1"),),
    )

    result = ProviderCoordinator((dishonest,)).search_releases(make_query(), make_context())

    assert result.candidates == ()
    assert len(result.failures) == 1
    assert result.failures[0].engine_id == "configured-engine"
    assert result.failures[0].issue.code is ProviderErrorCode.INVALID_RESPONSE
    assert result.failures[0].issue.technical_detail is None


# Selected-detail loading is an update to one record. A different returned
# identity must be rejected before existing review provenance can be reused.
def test_enrichment_cannot_change_the_selected_candidate_identity() -> None:
    candidate = make_candidate("provider", "release-1", source_id="catalogue")
    provider = FakeProvider(
        engine_id="provider",
        candidates=(candidate,),
        enriched_candidate=make_candidate("provider", "different-release", source_id="catalogue"),
    )
    coordinator = ProviderCoordinator((provider,))
    context = make_context()
    selected = coordinator.search_releases(make_query(), context).candidates[0]

    result = coordinator.enrich_release(selected, context)

    assert result.candidate is None
    assert len(result.failures) == 1
    assert result.failures[0].engine_id == "provider"
    assert result.failures[0].issue.code is ProviderErrorCode.INVALID_RESPONSE


def test_provider_exception_is_isolated_without_leaking_its_message() -> None:
    broken = FakeProvider(
        engine_id="broken",
        failure=ValueError("secret query text must not escape"),
    )
    working = FakeProvider(
        engine_id="working",
        candidates=(make_candidate("working", "release-ok"),),
    )

    result = ProviderCoordinator((broken, working)).search_releases(make_query(), make_context())

    assert tuple(hit.candidate.release_id for hit in result.candidates) == ("release-ok",)
    assert len(result.failures) == 1
    assert result.failures[0].issue.code is ProviderErrorCode.INVALID_RESPONSE
    assert "secret query text" not in repr(result.failures[0])


@pytest.mark.parametrize(
    "invalid_result",
    [
        pytest.param(iter((make_candidate("broken", "release-1"),)), id="generator"),
        pytest.param((object(),), id="wrong-item-type"),
    ],
)
def test_invalid_provider_result_shape_is_isolated(invalid_result: object) -> None:
    broken = FakeProvider(engine_id="broken", raw_candidates=invalid_result)

    result = ProviderCoordinator((broken,)).search_releases(make_query(), make_context())

    assert result.candidates == ()
    assert len(result.failures) == 1
    assert result.failures[0].issue.code is ProviderErrorCode.INVALID_RESPONSE


def test_enrichment_exception_is_returned_as_a_safe_invalid_response() -> None:
    candidate = make_candidate("provider", "release-1")
    provider = FakeProvider(
        engine_id="provider",
        candidates=(candidate,),
        enrichment_failure=RuntimeError("secret response fragment"),
    )
    coordinator = ProviderCoordinator((provider,))
    selected = coordinator.search_releases(make_query(), make_context()).candidates[0]

    result = coordinator.enrich_release(selected, make_context())

    assert result.candidate is None
    assert len(result.failures) == 1
    assert result.failures[0].issue.code is ProviderErrorCode.INVALID_RESPONSE
    assert "secret response fragment" not in repr(result.failures[0])
