import json
from pathlib import Path

import pytest

from metadata_polisher.providers.musicbrainz.parser import (
    parse_recording_composers,
    parse_release_detail,
    parse_release_search,
)

FIXTURES = Path(__file__).parents[3] / "fixtures" / "providers" / "musicbrainz"


# Sanitised WS/2 documents make parser expectations independent of live
# catalogue edits, network availability and the provider transport implementation.
def load_fixture(name: str) -> object:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_search_parser_keeps_catalogue_identity_but_not_summary_media() -> None:
    candidates = parse_release_search(load_fixture("search_release.json"))

    selected, sparse = candidates

    assert selected.engine_id == "musicbrainz_direct"
    assert selected.source_id == "musicbrainz"
    assert selected.release_id == "11111111-1111-4111-8111-111111111111"
    assert [(title.value, title.language, title.script) for title in selected.titles] == [
        ("Adventure Soundtrack", "jpn", "Jpan")
    ]
    assert selected.album_artists == ("The Game Orchestra",)
    assert selected.date == "2003-03-19"
    assert selected.media == ()
    assert selected.source_url == (
        "https://musicbrainz.org/release/11111111-1111-4111-8111-111111111111"
    )

    assert sparse.titles[0].language is None
    assert sparse.titles[0].script is None
    assert sparse.album_artists == ()
    assert sparse.date is None
    assert sparse.media == ()


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"error": "Search service unavailable"},
        {"error": "Search service unavailable", "releases": []},
        {"releases": None},
        {"releases": "not an array"},
        {"releases": {}},
        {"releases": ()},
        {"releases": [None]},
    ],
)
def test_search_parser_rejects_invalid_envelopes_instead_of_reporting_no_matches(payload: object) -> None:
    # An invalid provider response must remain distinguishable from a successful
    # search whose catalogue contains no matches for the supplied query.
    with pytest.raises(ValueError, match="MusicBrainz"):
        parse_release_search(payload)


def test_search_parser_accepts_an_explicit_empty_release_array() -> None:
    assert parse_release_search({"count": 0, "offset": 0, "releases": []}) == ()


def test_detail_parser_preserves_media_positions_durations_and_credited_names() -> None:
    candidate = parse_release_detail(load_fixture("release_detail.json"))

    assert [(medium.medium_number, medium.title) for medium in candidate.media] == [
        (1, "Sea"),
        (2, None),
    ]
    assert [[track.track_number for track in medium.tracks] for medium in candidate.media] == [
        [1, 2],
        [1],
    ]

    first, fallback = candidate.media[0].tracks
    partial = candidate.media[1].tracks[0]

    assert first.titles[0].value == "Opening Theme"
    assert first.titles[0].language == "jpn"
    assert first.titles[0].script == "Jpan"
    assert first.duration_seconds == 181.234
    assert first.artists == ("The Game Orchestra",)
    assert first.composers == ("K. Kondo",)

    assert fallback.titles[0].value == "Ocean Voyage"
    assert fallback.titles[0].language == "jpn"
    assert fallback.titles[0].script == "Jpan"
    assert fallback.duration_seconds == 202.0
    assert fallback.artists == ("Soloist, as credited",)
    assert fallback.composers == ("Mitsuko Nagata",)

    assert partial.duration_seconds is None
    assert partial.composers == ()


# Repeated relationship appearances are not extra composers. Identity and
# relationship role, rather than display-name similarity, control these credits.
def test_recording_composer_parser_deduplicates_artist_ids_and_excludes_lyricists() -> None:
    payload = load_fixture("work_composers.json")
    assert isinstance(payload, dict)
    recordings = payload["recordings"]
    assert isinstance(recordings, list)

    assert parse_recording_composers(recordings[0]) == ("K. Kondo", "M. Nagata")
    assert parse_recording_composers(recordings[1]) == ()
