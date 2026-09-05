import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from metadata_polisher.domain.matching import ReleaseSearchQuery
from metadata_polisher.providers.base import RequestContext
from metadata_polisher.providers.cache import MemoryCache
from metadata_polisher.providers.musicbrainz.provider import (
    RELEASE_DETAIL_INCLUDES,
    MusicBrainzProvider,
    build_release_query,
)
from metadata_polisher.providers.transport import ProviderTransport, TransportPolicy

FIXTURES = Path(__file__).parents[2] / "fixtures" / "providers" / "musicbrainz"
USER_AGENT = "MetadataPolisherTests/1.0 (test@example.invalid)"


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def load_fixture(name: str) -> object:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# Compose real request building, transport and parsing over MockTransport.
# Synthetic time keeps rate-limit checks deterministic and entirely offline.
def make_provider(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    minimum_interval_seconds: float = 0.0,
    search_limit: int = 25,
) -> tuple[MusicBrainzProvider, httpx.Client, FakeClock]:
    clock = FakeClock()
    client = httpx.Client(transport=httpx.MockTransport(handler))
    transport = ProviderTransport(
        client=client,
        policy=TransportPolicy(
            timeout_seconds=5,
            minimum_interval_seconds=minimum_interval_seconds,
            max_attempts=1,
        ),
        monotonic=clock.monotonic,
        sleeper=clock.sleep,
    )
    provider = MusicBrainzProvider(
        transport=transport,
        cache=MemoryCache(capacity=16),
        user_agent=USER_AGENT,
        search_limit=search_limit,
    )

    return provider, client, clock


def search_query() -> ReleaseSearchQuery:
    return ReleaseSearchQuery(
        album="Adventure Soundtrack",
        artists=("The Game Orchestra",),
        year=2003,
        disc_hint=1,
        local_track_count=3,
        distinctive_titles=("Opening Theme",),
    )


def test_release_query_escapes_lucene_values_and_uses_supported_evidence() -> None:
    query = ReleaseSearchQuery(
        album='The "Sea" + [Deluxe]',
        artists=("A/B", "C && D"),
        year=2003,
        disc_hint=2,
        local_track_count=3,
        distinctive_titles=("Not a release-search field",),
    )

    assert build_release_query(query) == (
        r'(release:"The \"Sea\" \+ \[Deluxe\]" OR alias:"The \"Sea\" \+ \[Deluxe\]") '
        r'AND artist:"A\/B" '
        r'AND artist:"C \&\& D" AND date:2003 AND tracksmedium:3'
    )


def test_release_query_searches_the_same_japanese_album_in_title_and_alias_fields() -> None:
    query = ReleaseSearchQuery(
        album="ファイアーエムブレム エンゲージ オリジナルサウンドトラック",
        artists=("INTELLIGENT SYSTEMS",),
        year=2024,
        disc_hint=2,
        local_track_count=20,
        distinctive_titles=(),
    )

    assert build_release_query(query) == (
        '(release:"ファイアーエムブレム エンゲージ オリジナルサウンドトラック" '
        'OR alias:"ファイアーエムブレム エンゲージ オリジナルサウンドトラック") '
        'AND artist:"INTELLIGENT SYSTEMS" AND date:2024 AND tracksmedium:20'
    )


def test_search_uses_ws2_contract_capabilities_and_memory_cache_without_auth() -> None:
    requests: list[httpx.Request] = []
    payload = load_fixture("search_release.json")

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=payload)

    provider, client, _ = make_provider(handler, search_limit=12)
    context = RequestContext(operation_id="lookup-1", preferred_language="jpn")

    try:
        first = provider.search_releases(search_query(), context)
        second = provider.search_releases(search_query(), context)
    finally:
        client.close()

    capabilities = provider.capabilities()

    assert provider.engine_id == "musicbrainz_direct"
    assert capabilities.release_search
    assert capabilities.track_listing
    assert capabilities.composer_credits
    assert not capabilities.multilingual_titles
    assert first == second
    assert len(first) == 2
    assert all(candidate.source_id == "musicbrainz" for candidate in first)

    assert len(requests) == 1
    request = requests[0]
    assert request.method == "GET"
    assert request.url.path == "/ws/2/release"
    assert dict(request.url.params) == {
        "query": (
            '(release:"Adventure Soundtrack" OR alias:"Adventure Soundtrack") '
            'AND artist:"The Game Orchestra" '
            "AND date:2003 AND tracksmedium:3"
        ),
        "fmt": "json",
        "limit": "12",
    }
    assert "inc" not in request.url.params
    assert request.headers["User-Agent"] == USER_AGENT
    assert "Authorization" not in request.headers


def test_search_cache_is_shared_across_lookup_operation_ids() -> None:
    requests: list[httpx.Request] = []
    payload = load_fixture("search_release.json")

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=payload)

    provider, client, _ = make_provider(handler)

    try:
        first = provider.search_releases(
            search_query(),
            RequestContext(operation_id="lookup-1", preferred_language="jpn"),
        )
        second = provider.search_releases(
            search_query(),
            RequestContext(operation_id="lookup-2", preferred_language="jpn"),
        )
    finally:
        client.close()

    assert first == second
    assert len(requests) == 1


def test_only_selected_release_is_detailed_and_nested_composers_need_no_followups() -> None:
    search_payload = load_fixture("search_release.json")
    detail_payload = load_fixture("release_detail.json")
    requests: list[httpx.Request] = []
    request_starts: list[float] = []
    clock_holder: list[FakeClock] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        request_starts.append(clock_holder[0].monotonic())

        if request.url.path == "/ws/2/release":
            return httpx.Response(200, json=search_payload)

        if request.url.path == "/ws/2/release/11111111-1111-4111-8111-111111111111":
            return httpx.Response(200, json=detail_payload)

        raise AssertionError(f"Unexpected MusicBrainz request path: {request.url.path}")

    provider, client, clock = make_provider(handler, minimum_interval_seconds=1.0)
    clock_holder.append(clock)
    context = RequestContext(operation_id="lookup-2", preferred_language="jpn")

    try:
        selected, unselected = provider.search_releases(search_query(), context)
        enriched = provider.enrich_release(selected, context)
        cached_enriched = provider.enrich_release(selected, context)
    finally:
        client.close()

    assert selected.media == ()
    assert unselected.media == ()
    assert len(enriched.media) == 2
    assert enriched.media[0].tracks[0].composers == ("K. Kondo",)
    assert enriched.media[1].tracks[0].composers == ()
    assert cached_enriched == enriched

    # One search and one selected-release detail request proves there is no
    # per-track work/composer N+1 when the detail payload contains nested relations.
    assert len(requests) == 2
    assert request_starts == [0.0, 1.0]
    assert clock.sleeps == [1.0]

    detail_request = requests[1]
    assert detail_request.url.path == "/ws/2/release/11111111-1111-4111-8111-111111111111"
    assert dict(detail_request.url.params) == {
        "fmt": "json",
        "inc": RELEASE_DETAIL_INCLUDES,
    }
    assert detail_request.headers["User-Agent"] == USER_AGENT
    assert "Authorization" not in detail_request.headers


# Fetching detailed credits first must not contaminate later basic-media
# results; cache separation is part of the lightweight search contract.
def test_basic_media_uses_a_distinct_relationship_free_cache_and_strips_composers() -> None:
    search_payload = load_fixture("search_release.json")
    detail_payload = load_fixture("release_detail.json")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)

        if request.url.path == "/ws/2/release":
            return httpx.Response(200, json=search_payload)

        if request.url.path == "/ws/2/release/11111111-1111-4111-8111-111111111111":
            return httpx.Response(200, json=detail_payload)

        raise AssertionError(f"Unexpected MusicBrainz request path: {request.url.path}")

    provider, client, _ = make_provider(handler)
    context = RequestContext(operation_id="lookup-media", preferred_language="jpn")

    try:
        selected, _unselected = provider.search_releases(search_query(), context)
        basic = provider.load_release_media(selected, context)
        cached_basic = provider.load_release_media(selected, context)
        enriched = provider.enrich_release(selected, context)
        basic_after_enrichment = provider.load_release_media(selected, context)
    finally:
        client.close()

    assert basic == cached_basic == basic_after_enrichment
    assert len(basic.media) == 2
    assert all(
        track.composers == ()
        for medium in basic.media
        for track in medium.tracks
    )
    assert enriched.media[0].tracks[0].composers == ("K. Kondo",)

    assert len(requests) == 3
    basic_request = requests[1]
    enrichment_request = requests[2]
    assert dict(basic_request.url.params) == {
        "fmt": "json",
        "inc": "artist-credits+recordings",
    }
    assert dict(enrichment_request.url.params) == {
        "fmt": "json",
        "inc": RELEASE_DETAIL_INCLUDES,
    }


@pytest.mark.parametrize(
    "user_agent",
    ["", "python-httpx", "MetadataPolisher/0.1"],
)
def test_provider_rejects_user_agents_without_application_version_and_contact(user_agent: str) -> None:
    client = httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={})))
    transport = ProviderTransport(
        client=client,
        policy=TransportPolicy(timeout_seconds=5, minimum_interval_seconds=1),
    )

    try:
        with pytest.raises(ValueError, match="User-Agent"):
            MusicBrainzProvider(
                transport=transport,
                cache=MemoryCache(capacity=1),
                user_agent=user_agent,
            )
    finally:
        client.close()


@pytest.mark.parametrize("search_limit", [0, 101, True])
def test_provider_rejects_search_limits_outside_musicbrainz_range(search_limit: object) -> None:
    client = httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={})))
    transport = ProviderTransport(
        client=client,
        policy=TransportPolicy(timeout_seconds=5, minimum_interval_seconds=1),
    )

    try:
        with pytest.raises((TypeError, ValueError), match="search_limit"):
            MusicBrainzProvider(
                transport=transport,
                cache=MemoryCache(capacity=1),
                user_agent=USER_AGENT,
                search_limit=search_limit,  # type: ignore[arg-type]
            )
    finally:
        client.close()
