import os

import pytest

from metadata_polisher.domain.errors import ProviderErrorCode
from metadata_polisher.domain.matching import ReleaseSearchQuery
from metadata_polisher.infrastructure.settings import NetworkSettings
from metadata_polisher.providers.base import RequestContext
from metadata_polisher.providers.cache import MemoryCache
from metadata_polisher.providers.network import create_http_client
from metadata_polisher.providers.transport import (
    ProviderTransport,
    ProviderTransportError,
    TransportPolicy,
)
from metadata_polisher.providers.vgmdb.provider import VgmdbProvider


@pytest.mark.live
def test_vgmdb_live_search_smoke() -> None:
    if os.environ.get("METADATA_POLISHER_VGMDB_LIVE") != "1":
        pytest.skip("Set METADATA_POLISHER_VGMDB_LIVE=1 to run the public VGMdb smoke test")

    client = create_http_client(NetworkSettings())
    provider = VgmdbProvider(
        transport=ProviderTransport(
            client=client,
            policy=TransportPolicy(
                timeout_seconds=10,
                minimum_interval_seconds=1,
                max_attempts=2,
            ),
        ),
        cache=MemoryCache(capacity=8),
    )
    query = ReleaseSearchQuery(
        album="Final Fantasy V Original Sound Version",
        artists=(),
        year=None,
        disc_hint=None,
        local_track_count=0,
        distinctive_titles=(),
    )

    try:
        try:
            candidates = provider.search_releases(
                query,
                RequestContext(operation_id="live-vgmdb", preferred_language="en"),
            )
        except ProviderTransportError as error:
            # Record a declined live boundary as skipped, not successful
            # validation. Offline fixture coverage cannot establish current access.
            if error.code in {
                ProviderErrorCode.AUTHENTICATION_REQUIRED,
                ProviderErrorCode.ACCESS_DENIED,
                ProviderErrorCode.SERVICE_UNAVAILABLE,
            }:
                pytest.skip(f"VGMdb declined automated access: {error.code.value}")

            raise
    finally:
        client.close()

    assert candidates
    assert all(candidate.engine_id == "vgmdb" for candidate in candidates)
