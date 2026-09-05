"""Real HTTP CONNECT routing using only loopback sockets and explicit test trust."""

import base64
import select
import socket
import ssl
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from time import monotonic
from urllib.parse import urlsplit

import httpx
import pytest

from metadata_polisher.domain.matching import ReleaseSearchQuery
from metadata_polisher.providers.base import RequestContext
from metadata_polisher.providers.cache import MemoryCache
from metadata_polisher.providers.musicbrainz import provider as musicbrainz
from metadata_polisher.providers.transport import ProviderTransport, ProviderTransportError, TransportPolicy

FIXTURES = Path(__file__).parents[2] / "fixtures"
CERTIFICATE = FIXTURES / "network" / "loopback-test-cert.pem"
PRIVATE_KEY = FIXTURES / "network" / "loopback-test-key.pem"
USERNAME = "LOOPBACK_PROXY_USER_SENTINEL"
PASSWORD = "LOOPBACK_PROXY_PASSWORD_SENTINEL"


class FixtureOrigin(ThreadingHTTPServer):
    """Serve sanitised provider fixtures; retain observed requests only in RAM."""

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), FixtureHandler)
        self.requests: list[tuple[str, dict[str, str]]] = []
        # A fixture certificate keeps this integration entirely on loopback.
        # The client must explicitly trust it; production TLS remains unchanged.
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(CERTIFICATE, PRIVATE_KEY)
        self.socket = context.wrap_socket(self.socket, server_side=True)


class FixtureHandler(BaseHTTPRequestHandler):
    server: FixtureOrigin

    def do_GET(self) -> None:
        self.server.requests.append((self.path, dict(self.headers)))
        filename = "search_release.json" if urlsplit(self.path).path.endswith("/release") else "release_detail.json"
        payload = (FIXTURES / "providers" / "musicbrainz" / filename).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, _format: str, *args: object) -> None:
        # Request observations are asserted directly; test traffic is never
        # copied into terminal logs or application diagnostics.
        pass


class ConnectProxy(ThreadingHTTPServer):
    """A bounded fixture tunnel, restricted to one known loopback TLS origin."""

    def __init__(self, origin: FixtureOrigin) -> None:
        super().__init__(("127.0.0.1", 0), ConnectHandler)
        self.origin_address = ("127.0.0.1", origin.server_port)
        self.requests: list[tuple[str, str | None]] = []
        self.require_authentication = False


class ConnectHandler(BaseHTTPRequestHandler):
    server: ConnectProxy

    def do_CONNECT(self) -> None:
        self.server.requests.append((self.path, self.headers.get("Proxy-Authorization")))
        expected = f"127.0.0.1:{self.server.origin_address[1]}"

        if self.server.require_authentication:
            self.send_response(407, "Proxy Authentication Required")
            self.send_header("Proxy-Authenticate", 'Basic realm="loopback fixture"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        # Restrict CONNECT to this test origin, making the fixture incapable
        # of tunnelling arbitrary external addresses supplied by a client.
        if self.path != expected:
            self.send_error(403)
            return

        with socket.create_connection(self.server.origin_address, timeout=2) as upstream:
            self.send_response(200, "Connection established")
            self.end_headers()
            self.wfile.flush()
            peers = (self.connection, upstream)
            deadline = monotonic() + 10

            # Tunnel encrypted bytes in either direction. Closing the origin or
            # client ends the handler; the deadline also bounds a broken client.
            while monotonic() < deadline:
                readable, _, _ = select.select(peers, (), (), 0.1)

                try:
                    for source in readable:
                        payload = source.recv(65536)

                        if not payload:
                            return

                        destination = upstream if source is self.connection else self.connection
                        destination.sendall(payload)
                except (ConnectionResetError, BrokenPipeError):
                    # TLS rejection can close either side without a graceful
                    # shutdown. The main test asserts the observed failure.
                    return

    def log_message(self, _format: str, *args: object) -> None:
        pass


@contextmanager
def serving(server: ThreadingHTTPServer) -> Iterator[ThreadingHTTPServer]:
    worker = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    worker.start()

    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
        assert not worker.is_alive(), "Loopback test listener did not stop"


def provider_client(monkeypatch, origin: FixtureOrigin, proxy_port: int, *, trust_fixture: bool = True):
    from metadata_polisher.infrastructure.session_credentials import SessionCredentials
    from metadata_polisher.infrastructure.settings import NetworkSettings
    from metadata_polisher.providers import network

    original_client = httpx.Client
    context = ssl.create_default_context(cafile=str(CERTIFICATE))

    def verified_client(**kwargs):
        # Assert the real factory requested normal verification before injecting
        # trust for this synthetic certificate. The verification switch is never
        # disabled, including in the deliberately untrusted-certificate test.
        assert kwargs["verify"] is True
        assert kwargs["trust_env"] is False

        if trust_fixture:
            kwargs["verify"] = context

        return original_client(**kwargs)

    monkeypatch.setattr(network.httpx, "Client", verified_client)
    monkeypatch.setattr(musicbrainz, "_API_BASE_URL", f"https://127.0.0.1:{origin.server_port}/ws/2")
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setenv("HTTPS_PROXY", "http://unused.invalid:1")
    credentials = SessionCredentials()
    credentials.set_proxy(USERNAME, PASSWORD)
    client = network.create_http_client(
        NetworkSettings("manual_proxy", "127.0.0.1", proxy_port), credentials.snapshot(),
    )
    transport = ProviderTransport(client=client, proxy_mode=True, policy=TransportPolicy(
        timeout_seconds=2, minimum_interval_seconds=0, max_attempts=1,
    ))
    provider = musicbrainz.MusicBrainzProvider(
        transport=transport, cache=MemoryCache(16), user_agent="MetadataPolisherTests/1.0 (test@example.invalid)",
    )

    return provider, client


def search(provider):
    return provider.search_releases(
        ReleaseSearchQuery(album="Adventure Soundtrack", artists=(), year=None, disc_hint=None,
                           local_track_count=3, distinctive_titles=()),
        RequestContext("LOOPBACK-001", "auto"),
    )


def test_selected_provider_search_media_and_enrichment_use_real_authenticated_connect(monkeypatch):
    origin = FixtureOrigin()
    proxy = ConnectProxy(origin)

    with serving(origin), serving(proxy):
        provider, client = provider_client(monkeypatch, origin, proxy.server_port)

        with client:
            candidates = search(provider)
            media = provider.load_release_media(candidates[0], RequestContext("LOOPBACK-002", "auto"))
            enriched = provider.enrich_release(candidates[0], RequestContext("LOOPBACK-003", "auto"))

        assert len(candidates) == 2
        assert candidates[0].release_id == "11111111-1111-4111-8111-111111111111"
        assert media.media[0].tracks
        assert enriched.media[0].tracks
        assert len(origin.requests) == len(proxy.requests) == 3
        expected_auth = "Basic " + base64.b64encode(f"{USERNAME}:{PASSWORD}".encode()).decode()
        assert all(auth == expected_auth for _, auth in proxy.requests)
        assert all(authority == f"127.0.0.1:{origin.server_port}" for authority, _ in proxy.requests)
        assert all("Proxy-Authorization" not in headers for _, headers in origin.requests)
        assert all("Authorization" not in headers for _, headers in origin.requests)


def test_stopped_manual_proxy_never_falls_back_to_reachable_https_origin(monkeypatch):
    origin = FixtureOrigin()
    stopped_proxy = ConnectProxy(origin)
    stopped_port = stopped_proxy.server_port
    stopped_proxy.server_close()

    with serving(origin):
        provider, client = provider_client(monkeypatch, origin, stopped_port)

        with client, pytest.raises(ProviderTransportError) as caught:
            search(provider)

        assert caught.value.code.value == "PROXY_CONNECTION_FAILED"
        assert origin.requests == []
        assert USERNAME not in str(caught.value)
        assert PASSWORD not in str(caught.value)


def test_https_through_proxy_rejects_untrusted_certificate(monkeypatch):
    origin = FixtureOrigin()
    proxy = ConnectProxy(origin)

    with serving(origin), serving(proxy):
        provider, client = provider_client(monkeypatch, origin, proxy.server_port, trust_fixture=False)

        with client, pytest.raises(ProviderTransportError) as caught:
            search(provider)

        assert caught.value.code.value == "TLS_VERIFICATION_FAILED"
        assert len(proxy.requests) == 1
        assert origin.requests == []


def test_connect_407_is_proxy_authentication_required_without_origin_access(monkeypatch):
    origin = FixtureOrigin()
    proxy = ConnectProxy(origin)
    proxy.require_authentication = True

    with serving(origin), serving(proxy):
        provider, client = provider_client(monkeypatch, origin, proxy.server_port)

        with client, pytest.raises(ProviderTransportError) as caught:
            search(provider)

        assert caught.value.code.value == "PROXY_AUTHENTICATION_REQUIRED"
        assert len(proxy.requests) == 1
        assert origin.requests == []
        assert USERNAME not in repr(caught.value)
        assert PASSWORD not in repr(caught.value)
