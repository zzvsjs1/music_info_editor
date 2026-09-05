from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from metadata_polisher import __version__
from metadata_polisher.application.lookup import LookupService
from metadata_polisher.infrastructure.settings import ProvidersSettings
from metadata_polisher.providers.base import RequestContext
from metadata_polisher.providers.runtime import ProviderRuntime
from metadata_polisher.session.state import SessionState
from tests.unit.application.test_lookup_service import make_group, make_media_file

FIXTURES = Path(__file__).parents[2] / "fixtures" / "providers"
CONTACT = "test@example.invalid"


# Advance virtual time instead of waiting. Shared clock values expose whether
# service reconfiguration accidentally resets the provider's request interval.
class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


# Real providers and composition share one injected offline client; recording
# host/path and virtual start time checks selection, caching and polite spacing.
def runtime_with_fixtures():
    requests: list[tuple[httpx.Request, float]] = []
    clock = FakeClock()

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append((request, clock.now))

        if request.url.host == "musicbrainz.org":
            assert request.url.path == "/ws/2/release"
            return httpx.Response(
                200,
                content=(FIXTURES / "musicbrainz" / "search_release.json").read_bytes(),
                headers={"Content-Type": "application/json"},
            )

        assert request.url.host == "vgmdb.net"
        assert request.url.path == "/search"
        return httpx.Response(
            200,
            text=(FIXTURES / "vgmdb" / "search_results.html").read_text(encoding="utf-8"),
            headers={"Content-Type": "text/html"},
        )

    cache = SessionState(root=None).provider_cache
    client = httpx.Client(transport=httpx.MockTransport(respond))
    runtime = ProviderRuntime(client=client, cache=cache, monotonic=clock.monotonic, sleeper=clock.sleep)
    return runtime, client, cache, requests


def test_runtime_creation_is_local_and_close_explicitly_releases_its_client() -> None:
    runtime, client, cache, requests = runtime_with_fixtures()

    try:
        service = runtime.service(ProvidersSettings(), musicbrainz_contact=CONTACT)
        assert isinstance(service, LookupService)
        assert runtime.cache is cache
        assert requests == []
        assert not client.is_closed
    finally:
        runtime.close()

    assert client.is_closed
    runtime.close()

    with pytest.raises(RuntimeError, match="closed"):
        runtime.service(ProvidersSettings(), musicbrainz_contact=CONTACT)


def test_provider_selection_and_shared_cache_survive_service_reconfiguration() -> None:
    runtime, _client, cache, requests = runtime_with_fixtures()
    group = make_group(make_media_file("01.flac"))
    settings = ProvidersSettings("vgmdb")

    try:
        initial = runtime.service(settings, musicbrainz_contact=CONTACT).search_group(
            group, RequestContext("LOOKUP-0001", "auto")
        )
        assert initial.failures == ()
        assert tuple(dict.fromkeys(item.candidate.engine_id for item in initial.candidates)) == (
            "vgmdb",
        )
        assert requests[0][0].url.host == "vgmdb.net"
        assert len(cache) > 0
        selected = replace(settings, selected_provider_id="musicbrainz_direct")
        next_result = runtime.service(selected, musicbrainz_contact=CONTACT).search_group(
            group, RequestContext("LOOKUP-0002", "auto")
        )
        assert next_result.failures == ()
        assert tuple(dict.fromkeys(item.candidate.engine_id for item in next_result.candidates)) == (
            "musicbrainz_direct",
        )
        request_count = len(requests)
        repeated = runtime.service(settings, musicbrainz_contact="").search_group(
            group, RequestContext("LOOKUP-0003", "auto"),
        )
        assert len(requests) == request_count
        assert all(
            member.operation_id == "LOOKUP-0003" for candidate in repeated.candidates for member in candidate.provenance
        )
    finally:
        runtime.close()


@pytest.mark.parametrize(
    ("selected", "expected_engines"),
    [
        ("vgmdb", {"vgmdb"}),
        ("musicbrainz_direct", {"musicbrainz_direct"}),
        (None, set()),
    ],
)
def test_disabled_providers_make_no_requests_and_disabled_musicbrainz_needs_no_contact(
    selected,
    expected_engines,
) -> None:
    runtime, _client, _cache, requests = runtime_with_fixtures()
    settings = ProvidersSettings(selected)

    try:
        service = runtime.service(settings, musicbrainz_contact=CONTACT if selected == "musicbrainz_direct" else "")
        assert requests == []
        result = service.search_group(make_group(make_media_file("01.flac")), RequestContext("LOOKUP-0001", "auto"))
        assert result.failures == ()
        assert {item.candidate.engine_id for item in result.candidates} == expected_engines
        assert ("musicbrainz.org" in {request.url.host for request, _ in requests}) is (
            selected == "musicbrainz_direct"
        )
        assert ("vgmdb.net" in {request.url.host for request, _ in requests}) is (selected == "vgmdb")
    finally:
        runtime.close()


@pytest.mark.parametrize("contact", ["", "   ", "not a contact", "https://", "a@b.invalid\r\nInjected: header"])
def test_enabled_musicbrainz_rejects_missing_or_invalid_contact_without_network(contact: str) -> None:
    runtime, _client, _cache, requests = runtime_with_fixtures()

    try:
        with pytest.raises(ValueError, match="contact"):
            runtime.service(ProvidersSettings(), musicbrainz_contact=contact)

        assert requests == []
    finally:
        runtime.close()


@pytest.mark.parametrize("contact", [CONTACT, "https://example.invalid/metadata-polisher"])
def test_musicbrainz_request_identifies_application_version_and_user_supplied_contact(contact: str) -> None:
    runtime, _client, _cache, requests = runtime_with_fixtures()
    settings = ProvidersSettings("musicbrainz_direct")

    try:
        service = runtime.service(settings, musicbrainz_contact=contact)
        service.search_group(make_group(make_media_file("01.flac")), RequestContext("LOOKUP-0001", "auto"))
        assert requests
        assert all(
            request.headers["User-Agent"] == f"MetadataPolisher/{__version__} ({contact})" for request, _ in requests
        )
    finally:
        runtime.close()


def test_musicbrainz_rate_limit_continues_across_service_reconfiguration() -> None:
    runtime, _client, _cache, requests = runtime_with_fixtures()
    settings = ProvidersSettings("musicbrainz_direct")
    group = make_group(make_media_file("01.flac"))

    try:
        runtime.service(settings, musicbrainz_contact=CONTACT).search_group(
            group, RequestContext("LOOKUP-0001", "auto")
        )
        runtime.service(settings, musicbrainz_contact=CONTACT).search_group(
            replace(group, album_title="Another album"), RequestContext("LOOKUP-0002", "auto")
        )
        times = [started for request, started in requests if request.url.host == "musicbrainz.org"]
        assert len(times) >= 2
        assert all(later - earlier >= 1.0 for earlier, later in zip(times, times[1:], strict=False))
    finally:
        runtime.close()
