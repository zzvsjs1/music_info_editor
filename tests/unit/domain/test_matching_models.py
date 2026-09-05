from collections.abc import Callable
from dataclasses import FrozenInstanceError
from typing import cast

import pytest

from metadata_polisher.domain.matching import (
    LocalisedText,
    MetadataProvenance,
    ProviderTrack,
    ReleaseCandidate,
    ReleaseMedium,
    ReleaseSearchQuery,
)
from metadata_polisher.providers.base import ProviderCapabilities, RequestContext


# Keep translated/original title variants inside their source medium.
# Flattening either structure would lose language choice or disc boundaries.
def test_provider_models_preserve_language_variants_and_release_media() -> None:
    english_title = LocalisedText(value="The Wind Waker", language="en", script="Latn")
    japanese_title = LocalisedText(value="風のタクト", language="ja", script="Jpan")
    track = ProviderTrack(
        track_number=1,
        titles=[english_title, japanese_title],  # type: ignore[arg-type]
        artists=["Koji Kondo"],  # type: ignore[arg-type]
        composers=["Koji Kondo"],  # type: ignore[arg-type]
        duration_seconds=182.5,
    )
    medium = ReleaseMedium(
        medium_number=2,
        title="The Great Sea",
        tracks=[track],  # type: ignore[arg-type]
    )
    candidate = ReleaseCandidate(
        engine_id="musicbrainz_direct",
        source_id="musicbrainz",
        release_id="release-1",
        titles=[english_title, japanese_title],  # type: ignore[arg-type]
        album_artists=["Koji Kondo"],  # type: ignore[arg-type]
        date="2003-03-19",
        media=[medium],  # type: ignore[arg-type]
        source_url="https://musicbrainz.org/release/release-1",
    )

    assert candidate.titles == (english_title, japanese_title)
    assert candidate.media == (medium,)
    assert candidate.media[0].tracks == (track,)
    assert candidate.media[0].tracks[0].titles[1].language == "ja"


def test_provider_models_defensively_copy_every_sequence() -> None:
    title = LocalisedText(value="Title", language=None, script=None)
    titles = [title]
    artists = ["Artist"]
    composers = ["Composer"]
    tracks = [
        ProviderTrack(
            track_number=1,
            titles=titles,  # type: ignore[arg-type]
            artists=artists,  # type: ignore[arg-type]
            composers=composers,  # type: ignore[arg-type]
            duration_seconds=None,
        )
    ]
    media = [ReleaseMedium(medium_number=1, title=None, tracks=tracks)]  # type: ignore[arg-type]
    album_artists = ["Album Artist"]
    distinctive_titles = ["Title"]
    candidate = ReleaseCandidate(
        engine_id="engine",
        source_id="source",
        release_id="release",
        titles=titles,  # type: ignore[arg-type]
        album_artists=album_artists,  # type: ignore[arg-type]
        date=None,
        media=media,  # type: ignore[arg-type]
        source_url=None,
    )
    query = ReleaseSearchQuery(
        album="Album",
        artists=artists,  # type: ignore[arg-type]
        year=None,
        disc_hint=None,
        local_track_count=1,
        distinctive_titles=distinctive_titles,  # type: ignore[arg-type]
    )

    # Mutate every input container after the models have been built. This
    # checks that nested release/track values own immutable sequence copies.
    titles.clear()
    artists.clear()
    composers.clear()
    tracks.clear()
    media.clear()
    album_artists.clear()
    distinctive_titles.clear()

    assert candidate.titles == (title,)
    assert candidate.album_artists == ("Album Artist",)
    assert candidate.media[0].tracks[0].artists == ("Artist",)
    assert candidate.media[0].tracks[0].composers == ("Composer",)
    assert query.artists == ("Artist",)
    assert query.distinctive_titles == ("Title",)


# An aggregator is the engine, while the original catalogue is the source.
# Retaining both prevents mirrored records from appearing independent.
def test_provenance_is_frozen_and_distinguishes_engine_from_source() -> None:
    provenance = MetadataProvenance(
        engine_id="aggregator_engine",
        source_id="musicbrainz",
        record_id="release-1",
        source_url="https://example.invalid/release-1",
        language="en",
        operation_id="lookup-42",
    )

    assert provenance.engine_id == "aggregator_engine"
    assert provenance.source_id == "musicbrainz"

    with pytest.raises(FrozenInstanceError):
        provenance.source_id = "hidden-source"


@pytest.mark.parametrize(
    ("factory", "message"),
    [
        (
            lambda: ProviderTrack(
                track_number=1,
                titles=("not-localised-text",),  # type: ignore[arg-type]
                artists=(),
                composers=(),
                duration_seconds=None,
            ),
            "titles must contain only LocalisedText",
        ),
        (
            lambda: ReleaseSearchQuery(
                album=None,
                artists="one artist",  # type: ignore[arg-type]
                year=None,
                disc_hint=None,
                local_track_count=1,
                distinctive_titles=(),
            ),
            "artists must be an ordered sequence",
        ),
        (
            lambda: ReleaseSearchQuery(
                album=None,
                artists=(),
                year=None,
                disc_hint=None,
                local_track_count=-1,
                distinctive_titles=(),
            ),
            "local_track_count cannot be negative",
        ),
    ],
)
def test_provider_models_reject_values_that_break_the_typed_contract(
    factory: object,
    message: str,
) -> None:
    with pytest.raises((TypeError, ValueError), match=message):
        cast(Callable[[], object], factory)()


def test_request_context_and_capabilities_are_frozen_typed_values() -> None:
    context = RequestContext(operation_id="lookup-42", preferred_language="ja")
    capabilities = ProviderCapabilities(
        release_search=True,
        track_listing=True,
        composer_credits=False,
        multilingual_titles=True,
    )

    assert context.preferred_language == "ja"
    assert capabilities.multilingual_titles

    with pytest.raises(FrozenInstanceError):
        context.operation_id = "other"

    with pytest.raises(TypeError, match="release_search must be a bool"):
        ProviderCapabilities(
            release_search=1,  # type: ignore[arg-type]
            track_listing=True,
            composer_credits=True,
            multilingual_titles=True,
        )
