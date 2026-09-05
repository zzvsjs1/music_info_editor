"""Synchronous, fixture-isolated provider for public VGMdb album pages."""

from dataclasses import replace

from metadata_polisher.domain.errors import ProviderErrorCode
from metadata_polisher.domain.matching import ReleaseCandidate, ReleaseSearchQuery
from metadata_polisher.providers.base import ProviderCapabilities, RequestContext
from metadata_polisher.providers.cache import MemoryCache
from metadata_polisher.providers.transport import (
    ProviderTransport,
    ProviderTransportError,
    ProviderTransportErrorContext,
)
from metadata_polisher.providers.vgmdb.parser import (
    ENGINE_ID,
    is_access_challenge,
    parse_album_detail,
    parse_search_results,
)

_BASE_URL = "https://vgmdb.net"
_HTML_HEADERS = {"Accept": "text/html"}

type _CacheKey = tuple[str, ...]
type _CachedValue = ReleaseCandidate | tuple[ReleaseCandidate, ...]


def _search_text(query: ReleaseSearchQuery) -> str:
    if query.album is None or not query.album.strip():
        raise ValueError("VGMdb release search requires an album title")

    return query.album.strip()


# An HTTP 200 page can still be a challenge or malformed catalogue response.
# Keep that semantic failure typed instead of caching it as an empty search.
def _page_error(code: ProviderErrorCode) -> ProviderTransportError:
    return ProviderTransportError(
        code=code,
        context=ProviderTransportErrorContext(
            attempt_count=1,
            status_code=200,
        ),
    )


def _without_composers(candidate: ReleaseCandidate) -> ReleaseCandidate:
    """Return track-listing data without leaking selected-only credits."""
    return replace(
        candidate,
        media=tuple(
            replace(
                medium,
                tracks=tuple(replace(track, composers=(), composer_credits=()) for track in medium.tracks),
            )
            for medium in candidate.media
        ),
    )


class VgmdbProvider:
    """Search albums cheaply and fetch detail only for the selected result."""

    def __init__(
        self,
        *,
        transport: ProviderTransport,
        cache: MemoryCache[_CacheKey, _CachedValue],
    ) -> None:
        self._transport = transport
        self._cache = cache

    @property
    def engine_id(self) -> str:
        return ENGINE_ID

    def capabilities(self) -> ProviderCapabilities:
        """Describe only fields exposed by the parsed public album pages."""
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
        search_text = _search_text(query)
        cache_key = (ENGINE_ID, "album-search", search_text)
        cached = self._cache.get(cache_key)

        if isinstance(cached, tuple):
            return cached

        html = self._transport.get_text(
            f"{_BASE_URL}/search",
            params={"q": search_text, "type": "album"},
            headers=_HTML_HEADERS,
            context=context,
        )

        if is_access_challenge(html):
            raise _page_error(ProviderErrorCode.ACCESS_DENIED)

        try:
            candidates = parse_search_results(html)
        except (TypeError, ValueError):
            raise _page_error(ProviderErrorCode.INVALID_RESPONSE) from None

        self._cache.put(cache_key, candidates)

        return candidates

    def enrich_release(
        self,
        candidate: ReleaseCandidate,
        context: RequestContext,
    ) -> ReleaseCandidate:
        return self._load_album_detail(candidate, context)

    def load_release_media(
        self,
        candidate: ReleaseCandidate,
        context: RequestContext,
    ) -> ReleaseCandidate:
        """Reuse the album page while exposing only relationship-free media."""
        cache_key = (ENGINE_ID, "album-media", candidate.release_id)
        cached = self._cache.get(cache_key)

        if isinstance(cached, ReleaseCandidate):
            return cached

        # One album page serves both stages, but the exposed basic candidate
        # still strips track credits to honour the selected-detail boundary.
        basic = _without_composers(self._load_album_detail(candidate, context))
        self._cache.put(cache_key, basic)

        return basic

    def _load_album_detail(self, candidate: ReleaseCandidate, context: RequestContext) -> ReleaseCandidate:
        source_url = self._selected_source_url(candidate)
        cache_key = (ENGINE_ID, "album-detail", candidate.release_id)
        cached = self._cache.get(cache_key)

        if isinstance(cached, ReleaseCandidate):
            return cached

        html = self._transport.get_text(
            source_url,
            headers=_HTML_HEADERS,
            context=context,
        )

        if is_access_challenge(html):
            raise _page_error(ProviderErrorCode.ACCESS_DENIED)

        try:
            enriched = parse_album_detail(html, source_url=source_url)
        except (TypeError, ValueError):
            raise _page_error(ProviderErrorCode.INVALID_RESPONSE) from None

        self._cache.put(cache_key, enriched)

        return enriched

    @staticmethod
    def _selected_source_url(candidate: ReleaseCandidate) -> str:
        if candidate.engine_id != ENGINE_ID or candidate.source_id != ENGINE_ID:
            raise ValueError("VGMdb can enrich only its own candidates")

        if not candidate.release_id.isascii() or not candidate.release_id.isdecimal():
            raise ValueError("VGMdb release ID must be numeric")

        # Construct the approved URL from the numeric identity and require the
        # candidate to agree; enrichment must not follow an arbitrary source URL.
        expected_url = f"{_BASE_URL}/album/{candidate.release_id}"

        if candidate.source_url != expected_url:
            raise ValueError("VGMdb candidate source URL does not match its release ID")

        return expected_url
