"""Synchronous MusicBrainz Direct provider using the shared transport policy."""

from dataclasses import replace
from urllib.parse import quote

from metadata_polisher.domain.matching import ReleaseCandidate, ReleaseSearchQuery
from metadata_polisher.providers.base import ProviderCapabilities, RequestContext
from metadata_polisher.providers.cache import MemoryCache
from metadata_polisher.providers.musicbrainz.parser import (
    ENGINE_ID,
    parse_release_detail,
    parse_release_search,
)
from metadata_polisher.providers.transport import ProviderTransport

_API_BASE_URL = "https://musicbrainz.org/ws/2"
RELEASE_DETAIL_INCLUDES = (
    "artist-credits+recordings+recording-level-rels+work-rels+work-level-rels+artist-rels"
)
BASIC_MEDIA_INCLUDES = "artist-credits+recordings"

type _CacheKey = tuple[str, ...]
type _CachedValue = ReleaseCandidate | tuple[ReleaseCandidate, ...]

# Lucene query parser metacharacters are escaped even inside quoted values so
# user metadata cannot alter the intended field/value structure.
_LUCENE_SPECIAL_CHARACTERS = frozenset(r'+-&|!(){}[]^"~*?:\/')


def _escape_lucene_value(value: str) -> str:
    return "".join(f"\\{character}" if character in _LUCENE_SPECIAL_CHARACTERS else character for character in value)


def _quoted_term(field: str, value: str) -> str | None:
    normalised = value.strip()

    if not normalised:
        return None

    return f'{field}:"{_escape_lucene_value(normalised)}"'


def build_release_query(query: ReleaseSearchQuery) -> str:
    """Build a deterministic MusicBrainz release-search Lucene expression."""
    terms: list[str] = []

    if query.album is not None:
        album_term = _quoted_term("release", query.album)
        alias_term = _quoted_term("alias", query.album)

        if album_term is not None and alias_term is not None:
            # MusicBrainz indexes release aliases separately from the canonical
            # title. Search both using the same local text: a Japanese album
            # alias can identify a release whose printed title is in English.
            # Parentheses keep artist/date/count constraints on both alternatives.
            terms.append(f"({album_term} OR {alias_term})")

    for artist in query.artists:
        artist_term = _quoted_term("artist", artist)

        if artist_term is not None:
            terms.append(artist_term)

    if query.year is not None:
        terms.append(f"date:{query.year}")

    if query.local_track_count > 0:
        # The local folder is matched against one release medium, so constraining
        # that medium's size does not wrongly exclude a larger multi-disc release.
        terms.append(f"tracksmedium:{query.local_track_count}")

    # A local disc number is not a release medium count, and distinctive track
    # titles are not supported release-search fields. Omitting both avoids
    # converting useful evidence into misleading MusicBrainz constraints.
    if not terms:
        raise ValueError("MusicBrainz release search requires album, artist, year, or track-count evidence")

    return " AND ".join(terms)


def _normalise_user_agent(value: str) -> str:
    candidate = value.strip()

    if "\r" in candidate or "\n" in candidate:
        raise ValueError("MusicBrainz User-Agent must fit on one line")

    try:
        product, parenthesised_contact = candidate.rsplit(" (", 1)
        application, version = product.split("/", 1)
    except ValueError:
        raise ValueError(
            "MusicBrainz User-Agent must identify an application/version and contact"
        ) from None

    contact = parenthesised_contact[:-1] if parenthesised_contact.endswith(")") else ""

    if not application.strip() or not version.strip() or not contact.strip():
        raise ValueError("MusicBrainz User-Agent must identify an application/version and contact")

    return candidate


def _without_composers(candidate: ReleaseCandidate) -> ReleaseCandidate:
    """Keep fetched media while enforcing the relationship-free search boundary."""
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


class MusicBrainzProvider:
    """Provide lightweight search and selected-release detail from MusicBrainz."""

    def __init__(
        self,
        *,
        transport: ProviderTransport,
        cache: MemoryCache[_CacheKey, _CachedValue],
        user_agent: str,
        search_limit: int = 25,
    ) -> None:
        self._transport = transport
        self._cache = cache
        self._user_agent = _normalise_user_agent(user_agent)

        if type(search_limit) is not int:
            raise TypeError("search_limit must be an integer")

        if not 1 <= search_limit <= 100:
            raise ValueError("search_limit must be between 1 and 100")

        self._search_limit = search_limit

    @property
    def engine_id(self) -> str:
        return ENGINE_ID

    def capabilities(self) -> ProviderCapabilities:
        """Return only capabilities supplied by the configured WS/2 requests."""
        return ProviderCapabilities(
            release_search=True,
            track_listing=True,
            composer_credits=True,
            # These endpoints identify title language/script but do not return
            # independently selectable translations or romanisations.
            multilingual_titles=False,
        )

    def search_releases(
        self,
        query: ReleaseSearchQuery,
        context: RequestContext,
    ) -> tuple[ReleaseCandidate, ...]:
        lucene_query = build_release_query(query)
        cache_key = (
            ENGINE_ID,
            "release-search",
            lucene_query,
            str(self._search_limit),
        )
        cached = self._cache.get(cache_key)

        if isinstance(cached, tuple):
            return cached

        payload = self._transport.get_json(
            f"{_API_BASE_URL}/release",
            params={
                "query": lucene_query,
                "fmt": "json",
                "limit": str(self._search_limit),
            },
            headers=self._headers(),
            context=context,
        )
        candidates = parse_release_search(payload)
        self._cache.put(cache_key, candidates)

        return candidates

    def enrich_release(
        self,
        candidate: ReleaseCandidate,
        context: RequestContext,
    ) -> ReleaseCandidate:
        # Detail and basic-media caches have different keys: composer-bearing
        # responses must not leak back into the lightweight candidate boundary.
        cache_key = (
            ENGINE_ID,
            "release-detail",
            candidate.release_id,
        )
        cached = self._cache.get(cache_key)

        if isinstance(cached, ReleaseCandidate):
            return cached

        release_id = quote(candidate.release_id, safe="")
        payload = self._transport.get_json(
            f"{_API_BASE_URL}/release/{release_id}",
            params={
                "fmt": "json",
                "inc": RELEASE_DETAIL_INCLUDES,
            },
            headers=self._headers(),
            context=context,
        )
        enriched = parse_release_detail(payload)
        self._cache.put(cache_key, enriched)

        return enriched

    def load_release_media(
        self,
        candidate: ReleaseCandidate,
        context: RequestContext,
    ) -> ReleaseCandidate:
        """Fetch scoreable media without work or composer relationships."""
        cache_key = (
            ENGINE_ID,
            "release-media",
            candidate.release_id,
        )
        cached = self._cache.get(cache_key)

        if isinstance(cached, ReleaseCandidate):
            return cached

        # Treat the opaque record ID as one URL path component, even if a
        # malformed source ID contains characters meaningful to URL routing.
        release_id = quote(candidate.release_id, safe="")
        payload = self._transport.get_json(
            f"{_API_BASE_URL}/release/{release_id}",
            params={
                "fmt": "json",
                "inc": BASIC_MEDIA_INCLUDES,
            },
            headers=self._headers(),
            context=context,
        )
        basic = _without_composers(parse_release_detail(payload))
        self._cache.put(cache_key, basic)

        return basic

    def _headers(self) -> dict[str, str]:
        # MusicBrainz does not require credentials for these public endpoints.
        # Supplying only the configured identifying header also makes accidental
        # credential propagation impossible at this boundary.
        return {"User-Agent": self._user_agent}
