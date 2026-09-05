"""Explicit proxy routing and RAM credentials use no real network or secrets."""

import json

import httpx
import pytest

from metadata_polisher.infrastructure import settings as settings_module
from metadata_polisher.infrastructure.diagnostics import sanitise_diagnostic_data
from metadata_polisher.infrastructure.settings import AppSettings, ProvidersSettings, load_settings, save_settings
from metadata_polisher.providers.base import RequestContext
from metadata_polisher.providers.runtime import ProviderRuntime
from metadata_polisher.session.state import SessionState
from tests.unit.application.test_lookup_service import make_group, make_media_file
from tests.unit.providers.test_runtime import CONTACT, FakeClock

# These conspicuous values are synthetic leak detectors, never credentials
# for a real account. Both username and password must stay out of saved output.
USERNAME = "SENTINEL_PROXY_USER_3F42"
PASSWORD = "SENTINEL_PROXY_PASSWORD_81A7"


def credentials_type():
    from metadata_polisher.infrastructure.session_credentials import SessionCredentials

    return SessionCredentials


def test_network_settings_persist_only_non_secret_routing_fields(tmp_path):
    network = settings_module.NetworkSettings(mode="manual_proxy", proxy_host="proxy.invalid", proxy_port=3128)
    settings = AppSettings(network=network)
    path = tmp_path / "settings.json"
    save_settings(path, settings)
    document = json.loads(path.read_text(encoding="utf-8"))

    assert document["network"] == {"mode": "manual_proxy", "proxy_host": "proxy.invalid", "proxy_port": 3128}
    assert load_settings(path).settings.network == network
    assert not any("password" in key or "username" in key for key in document["network"])


def test_credentials_are_redacted_immutable_snapshots_and_discarded_on_forget(tmp_path):
    credentials = credentials_type()()
    initial_generation = credentials.snapshot().generation
    credentials.set_proxy(USERNAME, PASSWORD)
    snapshot = credentials.snapshot()

    assert snapshot.proxy_auth == (USERNAME, PASSWORD)
    assert snapshot.generation > initial_generation

    with pytest.raises((AttributeError, TypeError)):
        snapshot.proxy_auth = ("replacement", "replacement")

    rendered = repr(credentials) + repr(snapshot) + json.dumps(sanitise_diagnostic_data(snapshot))
    assert USERNAME not in rendered
    assert PASSWORD not in rendered
    assert credentials_type()().snapshot().proxy_auth is None

    credentials.forget()
    assert credentials.snapshot().proxy_auth is None
    assert credentials.snapshot().generation > snapshot.generation
    assert list(tmp_path.iterdir()) == []


# Capture construction options and route every response through MockTransport
# so TLS/proxy configuration can be inspected without contacting a real host.
def capture_clients(monkeypatch, *, failure=False, clients_out=None, request_hook=None):
    created = []
    requests = []
    original_client = httpx.Client

    def factory(**kwargs):
        created.append(kwargs)

        def respond(request):
            requests.append(request)

            if request_hook is not None:
                request_hook(request)

            if failure:
                raise httpx.ProxyError(f"{USERNAME}:{PASSWORD} must never escape", request=request)

            return httpx.Response(200, json={"releases": [], "count": 0, "offset": 0})

        # Capture the production client configuration, then replace its socket
        # transport completely. This is configuration verification, not a claim
        # that a real proxy performed HTTP CONNECT or checked a certificate.
        client = original_client(transport=httpx.MockTransport(respond), trust_env=False)

        if clients_out is not None:
            clients_out.append(client)

        return client

    monkeypatch.setattr(httpx, "Client", factory)
    return created, requests


def test_direct_route_explicitly_ignores_environment_proxies_and_keeps_tls(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://unexpected.invalid:9999")
    created, requests = capture_clients(monkeypatch)
    runtime = ProviderRuntime(cache=SessionState(root=None).provider_cache)

    try:
        service = runtime.service(
            ProvidersSettings(), CONTACT, network=settings_module.NetworkSettings(mode="direct"),
        )
        assert requests == []
        service.search_group(make_group(make_media_file("01.flac")), RequestContext("LOOKUP-0201", "auto"))

        assert created
        assert all(config.get("trust_env") is False for config in created)
        assert all(config.get("verify", True) is True for config in created)
        assert all(config.get("proxy") is None for config in created)
        assert {request.url.host for request in requests} == {"musicbrainz.org"}
    finally:
        runtime.close()


@pytest.mark.parametrize("failure", [False, True])
def test_manual_proxy_captures_ram_auth_and_never_sends_credentials_to_origin(
    monkeypatch, caplog, failure,
):
    created, requests = capture_clients(monkeypatch, failure=failure)
    credentials = credentials_type()()
    credentials.set_proxy(USERNAME, PASSWORD)
    clock = FakeClock()
    runtime = ProviderRuntime(
        cache=SessionState(root=None).provider_cache, monotonic=clock.monotonic, sleeper=clock.sleep,
    )

    try:
        service = runtime.service(
            ProvidersSettings(), CONTACT,
            network=settings_module.NetworkSettings("manual_proxy", "proxy.invalid", 3128),
            credentials=credentials.snapshot(),
        )
        assert requests == []
        result = service.search_group(make_group(make_media_file("01.flac")), RequestContext("LOOKUP-0202", "auto"))
        routed = [config for config in created if config.get("proxy") is not None]

        assert len(routed) == 1
        proxy = routed[0]["proxy"]
        assert isinstance(proxy, httpx.Proxy)
        assert proxy.url == httpx.URL("http://proxy.invalid:3128")
        assert proxy.auth == (USERNAME, PASSWORD)
        assert routed[0]["trust_env"] is False
        assert routed[0].get("verify", True) is True
        assert requests
        assert all("Proxy-Authorization" not in request.headers for request in requests)
        assert all("Authorization" not in request.headers for request in requests)
        assert bool(result.failures) is failure

        if failure:
            assert {item.issue.code.value for item in result.failures} == {"PROXY_CONNECTION_FAILED"}

        rendered = repr(result) + caplog.text
        assert USERNAME not in rendered
        assert PASSWORD not in rendered
    finally:
        runtime.close()


@pytest.mark.parametrize("host", ["", "http://proxy.invalid", "user:pass@proxy.invalid", "proxy.invalid/path"])
def test_invalid_manual_proxy_is_rejected_without_a_direct_request(monkeypatch, host):
    _created, requests = capture_clients(monkeypatch)
    runtime = ProviderRuntime(cache=SessionState(root=None).provider_cache)

    try:
        with pytest.raises(ValueError, match="proxy"):
            network = settings_module.NetworkSettings("manual_proxy", host, 3128)
            runtime.service(ProvidersSettings(), CONTACT, network=network)

        assert requests == []
    finally:
        runtime.close()


def test_obsolete_route_pools_close_only_after_an_operation_settles(monkeypatch):
    clients = []
    capture_clients(monkeypatch, clients_out=clients)
    runtime = ProviderRuntime(cache=SessionState(root=None).provider_cache)

    try:
        runtime.service(ProvidersSettings(), CONTACT, network=settings_module.NetworkSettings())
        proxy = settings_module.NetworkSettings("manual_proxy", "proxy.invalid", 3128)
        runtime.service(ProvidersSettings(), CONTACT, network=proxy)

        # Captured work may still use the earlier pool until the UI reports a
        # safe terminal boundary. Configuration alone must not close it early.
        assert len(clients) == 2
        assert all(not client.is_closed for client in clients)

        runtime.finish_operation()

        assert clients[0].is_closed
        assert not clients[1].is_closed
        runtime.service(ProvidersSettings(), CONTACT, network=proxy)
        assert len(clients) == 2
    finally:
        runtime.close()


def test_changing_credentials_closes_old_auth_but_preserves_provider_rate_history(monkeypatch):
    clients = []
    starts = []
    clock = FakeClock()
    created, _requests = capture_clients(
        monkeypatch, clients_out=clients, request_hook=lambda request: starts.append(clock.monotonic()),
    )
    credentials = credentials_type()()
    credentials.set_proxy(USERNAME, PASSWORD)
    runtime = ProviderRuntime(
        cache=SessionState(root=None).provider_cache, monotonic=clock.monotonic, sleeper=clock.sleep,
    )
    proxy = settings_module.NetworkSettings("manual_proxy", "proxy.invalid", 3128)

    try:
        service = runtime.service(ProvidersSettings(), CONTACT, network=proxy, credentials=credentials.snapshot())
        service.search_group(make_group(make_media_file("01.flac")), RequestContext("LOOKUP-0401", "auto"))
        previous_start = starts[-1]
        old_client = clients[-1]
        old_generation = credentials.snapshot().generation
        credentials.set_proxy("replacement-user", "replacement-password")
        runtime.invalidate_credentials()

        assert old_client.is_closed
        assert credentials.snapshot().generation > old_generation
        service = runtime.service(ProvidersSettings(), CONTACT, network=proxy, credentials=credentials.snapshot())
        next_request_index = len(starts)
        service.search_group(
            make_group(make_media_file("01.flac"), album_title="Different album"),
            RequestContext("LOOKUP-0402", "auto"),
        )

        assert starts[next_request_index] - previous_start >= 1.0
        assert created[-1]["proxy"].auth == ("replacement-user", "replacement-password")
    finally:
        runtime.close()


def test_forget_discards_cookie_jars_even_when_retired_clients_still_have_references(monkeypatch):
    clients = []
    capture_clients(monkeypatch, clients_out=clients)
    credentials = credentials_type()()
    credentials.set_proxy(USERNAME, PASSWORD)
    runtime = ProviderRuntime(cache=SessionState(root=None).provider_cache)

    try:
        runtime.service(
            ProvidersSettings(), CONTACT,
            network=settings_module.NetworkSettings("manual_proxy", "proxy.invalid", 3128),
            credentials=credentials.snapshot(),
        )

        for client in clients:
            client.cookies.set("session", "SENTINEL_RETIRED_COOKIE_A29E", domain="musicbrainz.org")

        credentials.forget()
        runtime.invalidate_credentials()

        assert credentials.snapshot().proxy_auth is None
        assert all(client.is_closed for client in clients)
        assert all(len(client.cookies) == 0 for client in clients)
    finally:
        runtime.close()
