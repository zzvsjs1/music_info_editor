"""Own provider HTTP lifetime, session cache and concrete lookup composition."""

import re
import time
from collections.abc import Callable
from urllib.parse import urlsplit

import httpx

from metadata_polisher import __version__
from metadata_polisher.application.lookup import LookupService
from metadata_polisher.domain.matching import ReleaseCandidate
from metadata_polisher.infrastructure.session_credentials import CredentialSnapshot
from metadata_polisher.infrastructure.settings import NetworkSettings, ProvidersSettings
from metadata_polisher.providers.base import MetadataProvider
from metadata_polisher.providers.cache import MemoryCache
from metadata_polisher.providers.coordinator import ProviderCoordinator
from metadata_polisher.providers.musicbrainz.provider import MusicBrainzProvider
from metadata_polisher.providers.network import create_http_client, validate_network_settings
from metadata_polisher.providers.transport import ProviderRateLimit, ProviderTransport, TransportPolicy
from metadata_polisher.providers.vgmdb.provider import VgmdbProvider

type ProviderCache = MemoryCache[tuple[str, ...], ReleaseCandidate | tuple[ReleaseCandidate, ...]]

# Keep each provider's policy independent even while their initial limits match.
# MusicBrainz allows at most one request per second; the VGMdb adapter uses the
# same conservative interval and bounded retries as its opt-in live smoke test.
_MUSICBRAINZ_POLICY = TransportPolicy(timeout_seconds=10, minimum_interval_seconds=1, max_attempts=2)
_VGMDB_POLICY = TransportPolicy(timeout_seconds=10, minimum_interval_seconds=1, max_attempts=2)
_CONTACT_EMAIL = re.compile(r"[^@\s<>()]+@[^@\s<>()]+\.[^@\s<>()]+")


def _normalise_musicbrainz_contact(contact: str) -> str:
    message = "MusicBrainz contact must be an email address or an HTTP(S) URL."
    candidate = contact.strip()

    # Contact is sent in one User-Agent header. Reject delimiters and control
    # characters rather than letting a malformed value alter that header.
    if (
        not candidate
        or not candidate.isascii()
        or any(ord(character) < 32 or ord(character) == 127 for character in contact)
        or any(character in candidate for character in "()")
        or any(character.isspace() for character in candidate)
    ):
        raise ValueError(message)

    if _CONTACT_EMAIL.fullmatch(candidate):
        return candidate

    try:
        parsed = urlsplit(candidate)
        valid_url = (
            parsed.scheme in {"http", "https"}
            and parsed.hostname is not None
            and parsed.username is None
            and parsed.password is None
        )
    except ValueError:
        raise ValueError(message) from None

    if not valid_url:
        raise ValueError(message)

    return candidate


class ProviderRuntime:
    """Share real provider boundaries across successive explicit lookups."""

    def __init__(
        self,
        *,
        cache: ProviderCache,
        client: httpx.Client | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.cache = cache
        self._client = client if client is not None else create_http_client(NetworkSettings())
        self._injected_client = client is not None
        self._closed = False
        self._monotonic = monotonic
        self._sleeper = sleeper
        self._musicbrainz_rate_limit = ProviderRateLimit()
        self._vgmdb_rate_limit = ProviderRateLimit()
        self._musicbrainz_transport = ProviderTransport(
            client=self._client,
            policy=_MUSICBRAINZ_POLICY,
            monotonic=monotonic,
            sleeper=sleeper,
            rate_limit=self._musicbrainz_rate_limit,
        )
        self._vgmdb_transport = ProviderTransport(
            client=self._client,
            policy=_VGMDB_POLICY,
            monotonic=monotonic,
            sleeper=sleeper,
            rate_limit=self._vgmdb_rate_limit,
        )
        # Credentials and client objects remain private RAM state. A service
        # keeps its captured transport even if the next operation changes route.
        self._routes: dict[
            tuple[NetworkSettings, tuple[str, str] | None],
            tuple[httpx.Client, ProviderTransport, ProviderTransport],
        ] = {(NetworkSettings(), None): (self._client, self._musicbrainz_transport, self._vgmdb_transport)}
        self._test_clients: list[httpx.Client] = []
        self._active_route_key: tuple[NetworkSettings, tuple[str, str] | None] | None = (NetworkSettings(), None)

    def service(
        self, settings: ProvidersSettings, musicbrainz_contact: str, *, fresh_cache: bool = False,
        network: NetworkSettings | None = None, credentials: CredentialSnapshot | None = None,
    ) -> LookupService:
        """Capture one configured adapter without starting network work."""
        if self._closed or (self._injected_client and self._client.is_closed):
            raise RuntimeError("The provider runtime is closed.")

        providers: tuple[MetadataProvider, ...] = ()
        selected = settings.selected_provider_id
        # Explicit connection tests must reach the selected boundary again;
        # ordinary lookup retains its shared session cache.
        cache: ProviderCache = MemoryCache(16) if fresh_cache else self.cache

        if selected not in {None, "musicbrainz_direct", "vgmdb"}:
            raise ValueError("The selected provider is unavailable. Choose a provider in Settings.")

        route = network if network is not None else NetworkSettings()
        validate_network_settings(route)
        auth = credentials.proxy_auth if credentials is not None and route.mode == "manual_proxy" else None
        route_key = (route, auth)

        # Explicitly injected clients are the caller's transport boundary (for
        # example an offline fixture). A cache-bypassing test must not escape it.
        fresh_client = fresh_cache and not (self._injected_client and route.mode == "direct")

        # A route captures both non-secret settings and session authentication.
        # New clients share provider rate-limit clocks with older captured routes.
        if fresh_client or route_key not in self._routes:
            client = create_http_client(route, credentials)
            transports = (
                client,
                ProviderTransport(
                    client=client, policy=_MUSICBRAINZ_POLICY,
                    monotonic=self._monotonic, sleeper=self._sleeper,
                    rate_limit=self._musicbrainz_rate_limit, proxy_mode=route.mode == "manual_proxy",
                ),
                ProviderTransport(
                    client=client, policy=_VGMDB_POLICY,
                    monotonic=self._monotonic, sleeper=self._sleeper,
                    rate_limit=self._vgmdb_rate_limit, proxy_mode=route.mode == "manual_proxy",
                ),
            )

            if fresh_client:
                self._test_clients.append(client)
            else:
                self._routes[route_key] = transports
        else:
            transports = self._routes[route_key]

        _client, musicbrainz_transport, vgmdb_transport = transports

        # A connection test is temporary: it must not replace the active route
        # used by ordinary lookups or retain its credentials as session settings.
        if not fresh_cache:
            self._active_route_key = route_key

        if selected == "musicbrainz_direct":
            contact = _normalise_musicbrainz_contact(musicbrainz_contact)
            providers = (
                MusicBrainzProvider(
                    transport=musicbrainz_transport,
                    cache=cache,
                    user_agent=f"MetadataPolisher/{__version__} ({contact})",
                ),
            )
        elif selected == "vgmdb":
            providers = (VgmdbProvider(transport=vgmdb_transport, cache=cache),)

        # A submitted service retains its adapter. Reconfiguration affects the
        # next service while transport rate history and session cache survive.
        coordinator = ProviderCoordinator(providers)

        return LookupService(coordinator)

    def finish_connection_test(self) -> None:
        """Release test-only proxy authentication after its worker has settled."""
        for client in self._test_clients:
            self._retire_client(client)

        self._test_clients.clear()

    # Call after the worker settles: services already submitted may still own
    # a transport from an older route until their current operation completes.
    def finish_operation(self) -> None:
        """Prune obsolete pools only after captured operations have settled."""
        for key, (client, _musicbrainz, _vgmdb) in tuple(self._routes.items()):
            if key == self._active_route_key or (self._injected_client and client is self._client):
                continue

            self._retire_client(client)
            del self._routes[key]

    def invalidate_credentials(self) -> None:
        """Remove old authentication/cookies without resetting provider timing."""
        self.finish_connection_test()

        for key, (client, _musicbrainz, _vgmdb) in tuple(self._routes.items()):
            if self._injected_client and client is self._client:
                # Preserve an explicitly injected transport boundary for offline
                # callers; clear its HTTP session state without escaping it.
                client.cookies.clear()
                continue

            self._retire_client(client)
            del self._routes[key]

        self._active_route_key = None

    @staticmethod
    def _retire_client(client: httpx.Client) -> None:
        # Closing HTTPX's pool does not empty its cookie jar. Clear both kinds
        # of session state while the retired client may still have references.
        client.cookies.clear()
        client.close()

    def close(self) -> None:
        """Release the owned HTTP client after background work has finished."""
        if self._closed:
            return

        self._closed = True
        self.finish_connection_test()

        for client, _musicbrainz, _vgmdb in self._routes.values():
            self._retire_client(client)

        self._routes.clear()
        self._active_route_key = None
