"""Pure conversion of MusicBrainz WS/2 JSON into provider-neutral models."""

import math
from collections.abc import Mapping, Sequence
from typing import cast

from metadata_polisher.domain.matching import (
    ComposerCredit,
    CreditScope,
    LocalisedText,
    ProviderTrack,
    ReleaseCandidate,
    ReleaseMedium,
)

ENGINE_ID = "musicbrainz_direct"
SOURCE_ID = "musicbrainz"

# Stable relationship UUIDs avoid relying on translated or display-oriented
# relationship names returned by MusicBrainz.
_PERFORMANCE_RELATION_TYPE_ID = "a3005666-a872-32c3-ad06-98af558e99b0"
_COMPOSER_RELATION_TYPE_ID = "d59d99ea-23d4-4a80-b066-edca32ee158f"


def _as_mapping(value: object) -> Mapping[str, object] | None:
    if not isinstance(value, Mapping):
        return None

    return value


def _mapping_items(container: Mapping[str, object], key: str) -> tuple[Mapping[str, object], ...]:
    raw_items = container.get(key)

    if isinstance(raw_items, (str, bytes)) or not isinstance(raw_items, Sequence):
        return ()

    items: list[Mapping[str, object]] = []

    for raw_item in raw_items:
        item = _as_mapping(raw_item)

        if item is not None:
            items.append(item)

    return tuple(items)


def _optional_string(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None

    return value


def _required_string(container: Mapping[str, object], key: str) -> str:
    value = _optional_string(container.get(key))

    if value is None:
        raise ValueError(f"MusicBrainz {key} must be a non-empty string")

    return value


def _positive_integer(value: object) -> int | None:
    if type(value) is not int or value <= 0:
        return None

    return value


def _non_negative_integer(value: object) -> int | None:
    if type(value) is not int or value < 0:
        return None

    return value


def _listing_items(container: Mapping[str, object], key: str) -> tuple[Mapping[str, object], ...]:
    """Do not turn damaged source list members into a smaller apparently full list."""
    if key not in container:
        return ()

    raw_items = container[key]

    if not isinstance(raw_items, list) or any(not isinstance(item, Mapping) for item in raw_items):
        raise ValueError(f"MusicBrainz {key} must be an array of objects")

    return tuple(cast(Mapping[str, object], item) for item in raw_items)


def _duration_seconds(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None

    milliseconds = float(value)

    if not math.isfinite(milliseconds) or milliseconds < 0:
        return None

    # The source measures milliseconds; domain durations use seconds. For
    # example, 180000 becomes 180.0 without rounding away subsecond evidence.
    return milliseconds / 1000.0


def _language_and_script(container: Mapping[str, object]) -> tuple[str | None, str | None]:
    representation = _as_mapping(container.get("text-representation"))

    if representation is None:
        return None, None

    return _optional_string(representation.get("language")), _optional_string(representation.get("script"))


def _artist_credit(container: Mapping[str, object]) -> tuple[str, ...]:
    names: list[str] = []

    for credit in _mapping_items(container, "artist-credit"):
        credited_name = _optional_string(credit.get("name"))

        if credited_name is None:
            artist = _as_mapping(credit.get("artist"))
            credited_name = _optional_string(artist.get("name")) if artist is not None else None

        if credited_name is not None:
            names.append(credited_name)

    return tuple(names)


def _candidate_identity(release: Mapping[str, object]) -> tuple[str, str, str | None, tuple[str, ...]]:
    release_id = _required_string(release, "id")
    title = _required_string(release, "title")
    date = _optional_string(release.get("date"))

    return release_id, title, date, _artist_credit(release)


def _source_url(release_id: str) -> str:
    return f"https://musicbrainz.org/release/{release_id}"


def parse_release_search(payload: object) -> tuple[ReleaseCandidate, ...]:
    """Parse lightweight release search results without treating media summaries as tracks."""
    root = _as_mapping(payload)

    if root is None:
        raise ValueError("MusicBrainz release search payload must be an object")

    if "error" in root:
        raise ValueError("MusicBrainz release search returned an error response")

    releases = root.get("releases")

    # The top-level array is required even when a valid search has no matches.
    # Optional nested metadata remains tolerant, but a damaged response envelope
    # must not be cached and displayed as a successful empty catalogue result.
    if not isinstance(releases, list):
        raise ValueError("MusicBrainz release search must contain a releases array")

    candidates: list[ReleaseCandidate] = []

    for raw_release in releases:
        release = _as_mapping(raw_release)

        if release is None:
            raise ValueError("MusicBrainz release search entries must be objects")

        release_id, title, date, album_artists = _candidate_identity(release)
        language, script = _language_and_script(release)

        candidates.append(
            ReleaseCandidate(
                engine_id=ENGINE_ID,
                source_id=SOURCE_ID,
                release_id=release_id,
                titles=(LocalisedText(value=title, language=language, script=script),),
                album_artists=album_artists,
                date=date,
                # Search media only reports counts and formats. An empty tuple
                # prevents those summaries being mistaken for a detailed listing.
                media=(),
                source_url=_source_url(release_id),
            )
        )

    return tuple(candidates)


def parse_recording_composer_credits(payload: object) -> tuple[ComposerCredit, ...]:
    """Retain recording-to-work composer links, without conflating other roles.

    MusicBrainz defines the performance relationship as the recording/work link
    and the composer relationship as authorship of that work's music. Keeping
    the work identity lets review explain why a credit applies to this track.
    """
    recording = _as_mapping(payload)

    if recording is None:
        return ()

    seen_assignments: set[tuple[str | None, str]] = set()
    credits: list[ComposerCredit] = []

    # Traverse recording -> performed work -> composing artist. Album credits
    # and other artist roles cannot establish authorship of this track.
    for recording_relation in _mapping_items(recording, "relations"):
        if recording_relation.get("type-id") != _PERFORMANCE_RELATION_TYPE_ID:
            continue

        if recording_relation.get("target-type") != "work":
            continue

        work = _as_mapping(recording_relation.get("work"))

        if work is None:
            continue

        work_id = _optional_string(work.get("id"))

        for work_relation in _mapping_items(work, "relations"):
            if work_relation.get("type-id") != _COMPOSER_RELATION_TYPE_ID:
                continue

            if work_relation.get("target-type") != "artist":
                continue

            artist = _as_mapping(work_relation.get("artist"))

            if artist is None:
                continue

            artist_id = _optional_string(artist.get("id"))

            if artist_id is None or (work_id, artist_id) in seen_assignments:
                continue

            name = _optional_string(work_relation.get("target-credit"))

            if name is None:
                name = _optional_string(artist.get("name"))

            if name is None:
                continue

            seen_assignments.add((work_id, artist_id))
            credits.append(ComposerCredit(
                names=(name,), scope=CreditScope.WORK, record_id=work_id,
                source_url=f"https://musicbrainz.org/work/{work_id}" if work_id is not None else None,
            ))

    return tuple(credits)


def parse_recording_composers(payload: object) -> tuple[str, ...]:
    """Keep the existing names projection while preserving evidence in the parser."""
    return tuple(dict.fromkeys(name for credit in parse_recording_composer_credits(payload) for name in credit.names))


def _parse_track(
    track: Mapping[str, object],
    language: str | None,
    script: str | None,
) -> ProviderTrack:
    recording = _as_mapping(track.get("recording"))
    title = _optional_string(track.get("title"))

    # Release-track text is the most specific evidence. Recording defaults are
    # used only where this particular release omits its own value.
    if title is None and recording is not None:
        title = _optional_string(recording.get("title"))

    artists = _artist_credit(track)

    if not artists and recording is not None:
        artists = _artist_credit(recording)

    duration = _duration_seconds(track.get("length"))

    if duration is None and recording is not None:
        duration = _duration_seconds(recording.get("length"))

    composer_credits = parse_recording_composer_credits(recording)
    composers = tuple(dict.fromkeys(name for credit in composer_credits for name in credit.names))
    titles = (
        (LocalisedText(value=title, language=language, script=script),)
        if title is not None
        else ()
    )

    return ProviderTrack(
        track_number=_non_negative_integer(track.get("position")),
        titles=titles,
        artists=artists,
        composers=composers,
        duration_seconds=duration,
        composer_credits=composer_credits,
        printed_number=_optional_string(track.get("number")),
    )


def _parse_medium(
    medium: Mapping[str, object],
    language: str | None,
    script: str | None,
) -> ReleaseMedium:
    tracks = tuple(_parse_track(track, language, script) for track in _listing_items(medium, "tracks"))
    declared_count = _non_negative_integer(medium.get("track-count"))
    offset = _non_negative_integer(medium.get("track-offset", 0))
    data_tracks = _listing_items(medium, "data-tracks")
    pregap = _as_mapping(medium.get("pregap"))

    if "pregap" in medium and medium["pregap"] is not None and pregap is None:
        raise ValueError("MusicBrainz pregap must be an object")

    # WS/2's track-count and tracks exclude pregap/data tracks. Its track-offset
    # can expose a partial listing. A coincidentally short local folder must not
    # turn that response into an authoritative smaller total.
    complete = (
        declared_count is not None
        and declared_count == len(tracks)
        and offset == 0
        and tuple(track.track_number for track in tracks) == tuple(range(1, len(tracks) + 1))
    )

    if pregap is not None:
        tracks = (_parse_track(pregap, language, script), *tracks)

    return ReleaseMedium(
        medium_number=_positive_integer(medium.get("position")),
        title=_optional_string(medium.get("title")),
        tracks=tracks,
        declared_track_count=declared_count,
        tracks_complete=complete,
        has_pregap=pregap is not None,
        has_data_tracks=bool(data_tracks),
    )


def parse_release_detail(payload: object) -> ReleaseCandidate:
    """Parse one selected release, retaining its medium boundaries and nested credits."""
    release = _as_mapping(payload)

    if release is None:
        raise ValueError("MusicBrainz release detail payload must be an object")

    release_id, title, date, album_artists = _candidate_identity(release)
    language, script = _language_and_script(release)
    media = tuple(_parse_medium(medium, language, script) for medium in _listing_items(release, "media"))

    return ReleaseCandidate(
        engine_id=ENGINE_ID,
        source_id=SOURCE_ID,
        release_id=release_id,
        titles=(LocalisedText(value=title, language=language, script=script),),
        album_artists=album_artists,
        date=date,
        media=media,
        source_url=_source_url(release_id),
        # Release-detail media comes from all_mediums in the source serializer.
        # Missing/duplicate/gapped positions cannot establish a full disc count.
        media_complete=bool(media) and tuple(item.medium_number for item in media) == tuple(range(1, len(media) + 1)),
    )
