from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from metadata_polisher.domain.errors import ProviderErrorCode
from metadata_polisher.domain.matching import ReleaseSearchQuery
from metadata_polisher.providers.base import RequestContext
from metadata_polisher.providers.cache import MemoryCache
from metadata_polisher.providers.transport import (
    ProviderTransport,
    ProviderTransportError,
    TransportPolicy,
)
from metadata_polisher.providers.vgmdb.provider import VgmdbProvider

FIXTURES = Path(__file__).parents[2] / "fixtures" / "providers" / "vgmdb"


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def load_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# Keep the concrete VGMdb adapter and shared transport in the test, replacing
# only HTTP responses and time so parser/caching behaviour remains integrated.
def make_provider(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    minimum_interval_seconds: float = 0.0,
) -> tuple[VgmdbProvider, httpx.Client, FakeClock]:
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
    provider = VgmdbProvider(
        transport=transport,
        cache=MemoryCache(capacity=16),
    )

    return provider, client, clock


def search_query() -> ReleaseSearchQuery:
    return ReleaseSearchQuery(
        album="Adventure & Sea",
        artists=("Kei & Co.",),
        year=2003,
        disc_hint=1,
        local_track_count=2,
        distinctive_titles=("Opening & Dawn",),
    )


def test_search_uses_one_album_request_and_returns_no_eager_detail() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, text=load_fixture("search_results.html"))

    provider, client, _ = make_provider(handler)

    try:
        candidates = provider.search_releases(
            search_query(),
            RequestContext(operation_id="lookup-1", preferred_language="ja"),
        )
    finally:
        client.close()

    capabilities = provider.capabilities()

    assert provider.engine_id == "vgmdb"
    assert capabilities.release_search
    assert capabilities.track_listing
    assert capabilities.composer_credits
    assert capabilities.multilingual_titles
    assert len(candidates) == 4
    assert all(candidate.source_id == "vgmdb" for candidate in candidates)
    assert all(candidate.media == () for candidate in candidates)

    assert len(requests) == 1
    request = requests[0]
    assert request.method == "GET"
    assert request.url.path == "/search"
    assert dict(request.url.params) == {"q": "Adventure & Sea", "type": "album"}
    assert request.headers["Accept"] == "text/html"
    assert "Cookie" not in request.headers


def test_search_cache_is_shared_across_lookup_operation_ids() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, text=load_fixture("search_results.html"))

    provider, client, _ = make_provider(handler)

    try:
        first = provider.search_releases(
            search_query(),
            RequestContext(operation_id="lookup-1", preferred_language="en"),
        )
        second = provider.search_releases(
            search_query(),
            RequestContext(operation_id="lookup-2", preferred_language="ja"),
        )
    finally:
        client.close()

    assert first == second
    assert len(requests) == 1


def test_only_selected_url_is_enriched_through_the_shared_polite_transport() -> None:
    requests: list[httpx.Request] = []
    request_starts: list[float] = []
    clock_holder: list[FakeClock] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        request_starts.append(clock_holder[0].monotonic())

        if request.url.path == "/search":
            return httpx.Response(200, text=load_fixture("search_results.html"))

        if request.url.path == "/album/4242":
            return httpx.Response(200, text=load_fixture("album_detail.html"))

        raise AssertionError(f"Unexpected VGMdb request path: {request.url.path}")

    provider, client, clock = make_provider(handler, minimum_interval_seconds=1.0)
    clock_holder.append(clock)
    first_context = RequestContext(operation_id="lookup-1", preferred_language="en")
    second_context = RequestContext(operation_id="lookup-2", preferred_language="ja")

    try:
        selected, unselected, *_ = provider.search_releases(search_query(), first_context)
        enriched = provider.enrich_release(selected, first_context)
        cached_enriched = provider.enrich_release(selected, second_context)
    finally:
        client.close()

    assert unselected.media == ()
    assert len(enriched.media) == 2
    assert cached_enriched == enriched
    assert [request.url.path for request in requests] == ["/search", "/album/4242"]
    assert request_starts == [0.0, 1.0]
    assert clock.sleeps == [1.0]
    assert all("Cookie" not in request.headers for request in requests)


def test_basic_media_shares_one_page_fetch_but_never_exposes_cached_composers() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)

        if request.url.path == "/search":
            return httpx.Response(200, text=load_fixture("search_results.html"))

        if request.url.path == "/album/4242":
            return httpx.Response(200, text=load_fixture("album_detail.html"))

        raise AssertionError(f"Unexpected VGMdb request path: {request.url.path}")

    provider, client, _ = make_provider(handler)
    context = RequestContext(operation_id="lookup-media", preferred_language="ja")

    try:
        selected, _unselected, *_ = provider.search_releases(search_query(), context)
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
    assert enriched.media[0].tracks[0].composers == ()
    assert next(credit.names for credit in enriched.album_credits if credit.role == "composer") == (
        "Kei & Co.", "Mika Ono",
    )
    assert [request.url.path for request in requests] == ["/search", "/album/4242"]


def test_malformed_search_page_becomes_a_typed_invalid_response() -> None:
    provider, client, _ = make_provider(
        lambda _request: httpx.Response(200, text="<html><title>VGMdb</title><p>changed layout</p></html>")
    )

    try:
        with pytest.raises(ProviderTransportError) as caught:
            provider.search_releases(
                search_query(),
                RequestContext(operation_id="lookup-1", preferred_language="en"),
            )
    finally:
        client.close()

    assert caught.value.code is ProviderErrorCode.INVALID_RESPONSE
    assert caught.value.context.status_code == 200
    assert "changed layout" not in repr(caught.value)
    assert caught.value.__cause__ is None


def test_non_zero_result_heading_with_changed_layout_is_invalid() -> None:
    provider, client, _ = make_provider(
        lambda _request: httpx.Response(200, text="<html><h3>4 album results</h3><p>changed layout</p></html>")
    )

    try:
        with pytest.raises(ProviderTransportError) as caught:
            provider.search_releases(
                search_query(),
                RequestContext(operation_id="lookup-1", preferred_language="en"),
            )
    finally:
        client.close()

    assert caught.value.code is ProviderErrorCode.INVALID_RESPONSE


@pytest.mark.parametrize("status_code", [200, 403])
# A browser challenge can arrive with HTTP 200 as well as 403. Neither is a
# successful empty catalogue search and neither should enter the result cache.
def test_access_challenge_becomes_typed_access_denied(status_code: int) -> None:
    provider, client, _ = make_provider(
        lambda _request: httpx.Response(status_code, text=load_fixture("cloudflare_challenge.html"))
    )

    try:
        with pytest.raises(ProviderTransportError) as caught:
            provider.search_releases(
                search_query(),
                RequestContext(operation_id="lookup-1", preferred_language="en"),
            )
    finally:
        client.close()

    assert caught.value.code.value == "ACCESS_DENIED"
    assert caught.value.context.status_code == status_code


def test_service_unavailability_remains_typed_for_partial_provider_results() -> None:
    provider, client, _ = make_provider(lambda _request: httpx.Response(503))

    try:
        with pytest.raises(ProviderTransportError) as caught:
            provider.search_releases(
                search_query(),
                RequestContext(operation_id="lookup-1", preferred_language="en"),
            )
    finally:
        client.close()

    assert caught.value.code is ProviderErrorCode.SERVICE_UNAVAILABLE
