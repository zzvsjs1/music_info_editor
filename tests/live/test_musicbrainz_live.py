import os

import pytest

from metadata_polisher.domain.matching import ReleaseSearchQuery
from metadata_polisher.infrastructure.settings import NetworkSettings
from metadata_polisher.providers.base import RequestContext
from metadata_polisher.providers.cache import MemoryCache
from metadata_polisher.providers.musicbrainz.provider import MusicBrainzProvider
from metadata_polisher.providers.network import create_http_client
from metadata_polisher.providers.transport import ProviderTransport, TransportPolicy

# Live access is opt-in and requires an identifying contact. Ordinary offline
# runs must not turn missing configuration into an implicit public request.
_LIVE_ENABLED = os.environ.get("METADATA_POLISHER_MUSICBRAINZ_LIVE") == "1"
_CONTACT = os.environ.get("METADATA_POLISHER_MUSICBRAINZ_CONTACT", "").strip()


@pytest.mark.live
@pytest.mark.skipif(
    not _LIVE_ENABLED or not _CONTACT,
    reason=(
        "set METADATA_POLISHER_MUSICBRAINZ_LIVE=1 and "
        "METADATA_POLISHER_MUSICBRAINZ_CONTACT to opt in"
    ),
)
def test_musicbrainz_release_search_live_smoke() -> None:
    client = create_http_client(NetworkSettings())
    transport = ProviderTransport(
        client=client,
        policy=TransportPolicy(
            timeout_seconds=10,
            minimum_interval_seconds=1,
            max_attempts=2,
        ),
    )
    provider = MusicBrainzProvider(
        transport=transport,
        cache=MemoryCache(capacity=8),
        user_agent=f"MetadataPolisherLiveTest/0.1 ({_CONTACT})",
        search_limit=3,
    )

    try:
        candidates = provider.search_releases(
            ReleaseSearchQuery(
                album="Kind of Blue",
                artists=("Miles Davis",),
                year=1959,
                disc_hint=1,
                local_track_count=5,
                distinctive_titles=(),
            ),
            RequestContext(operation_id="live-smoke", preferred_language="eng"),
        )
    finally:
        client.close()

    assert candidates
