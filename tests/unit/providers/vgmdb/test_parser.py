from pathlib import Path

import pytest

from metadata_polisher.providers.vgmdb.parser import (
    parse_album_detail,
    parse_search_results,
)

FIXTURES = Path(__file__).parents[3] / "fixtures" / "providers" / "vgmdb"


# Stored HTML fixes the page shape for reproducible parsing tests. Passing
# these fixtures does not establish that the public site is reachable today.
def load_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_search_parser_preserves_language_script_variants_and_entities() -> None:
    first = parse_search_results(load_fixture("search_results.html"))[0]

    assert [(title.value, title.language, title.script) for title in first.titles] == [
        ("Adventure & Sea <Suite>", "en", "Latn"),
        ("冒険と海 組曲", "ja", "Jpan"),
        ("Bouken to Umi Suite", "ja", "Latn"),
    ]


def test_search_parser_keeps_distinct_printings_with_identical_titles() -> None:
    candidates = parse_search_results(load_fixture("search_results.html"))

    assert [candidate.release_id for candidate in candidates] == ["4242", "4243", "4244", "4245"]
    assert candidates[0].titles == candidates[1].titles
    assert candidates[0] != candidates[1]


def test_search_parser_normalises_full_iso_year_and_missing_dates() -> None:
    candidates = parse_search_results(load_fixture("search_results.html"))

    assert [candidate.date for candidate in candidates] == [
        "2003-03-19",
        "2003-03-19",
        "2003",
        None,
    ]


def test_search_parser_returns_lightweight_vgmdb_candidates() -> None:
    candidates = parse_search_results(load_fixture("search_results.html"))

    assert all(candidate.engine_id == "vgmdb" for candidate in candidates)
    assert all(candidate.source_id == "vgmdb" for candidate in candidates)
    assert all(candidate.album_artists == () for candidate in candidates)
    assert all(candidate.media == () for candidate in candidates)
    assert candidates[0].source_url == "https://vgmdb.net/album/4242"


def test_detail_parser_preserves_album_title_variants_and_entities() -> None:
    candidate = parse_album_detail(
        load_fixture("album_detail.html"),
        source_url="https://vgmdb.net/album/4242",
    )

    assert [(title.value, title.language, title.script) for title in candidate.titles] == [
        ("Adventure & Sea <Suite>", "en", "Latn"),
        ("冒険と海 組曲", "ja", "Jpan"),
        ("Bouken to Umi Suite", "ja", "Latn"),
    ]
    assert candidate.date == "2003-03-19"


# Parallel language tabs describe one track sequence per disc; merging must
# add title variants without multiplying tracks or crossing disc boundaries.
def test_detail_parser_merges_language_tracklists_by_disc_and_position() -> None:
    candidate = parse_album_detail(
        load_fixture("album_detail.html"),
        source_url="https://vgmdb.net/album/4242",
    )

    assert len(candidate.media) == 2
    assert [len(medium.tracks) for medium in candidate.media] == [2, 2]
    assert [(title.value, title.language, title.script) for title in candidate.media[0].tracks[0].titles] == [
        ("Opening & Dawn", "en", "Latn"),
        ("始まりと夜明け", "ja", "Jpan"),
    ]
    assert [track.track_number for track in candidate.media[1].tracks] == [1, 2]


def test_detail_parser_preserves_media_titles_numbers_and_durations() -> None:
    candidate = parse_album_detail(
        load_fixture("album_detail.html"),
        source_url="https://vgmdb.net/album/4242",
    )

    assert [(medium.medium_number, medium.title) for medium in candidate.media] == [
        (1, "Sea & Sky"),
        (2, "Bonus Disc"),
    ]
    assert [[track.duration_seconds for track in medium.tracks] for medium in candidate.media] == [
        [181.0, 245.0],
        [120.0, 180.0],
    ]


def test_detail_parser_retains_album_credit_roles_without_assigning_tracks() -> None:
    candidate = parse_album_detail(
        load_fixture("album_detail.html"),
        source_url="https://vgmdb.net/album/4242",
    )

    composers = {track.composers for medium in candidate.media for track in medium.tracks}

    assert composers == {()}
    assert {credit.role: credit.names for credit in candidate.album_credits} == {
        "composer": ("Kei & Co.", "Mika Ono"),
        "arranger": ("An Arranger",),
        "lyricist": ("A Lyricist",),
    }


def test_detail_parser_keeps_missing_optional_values_missing() -> None:
    candidate = parse_album_detail(
        load_fixture("album_missing_fields.html"),
        source_url="https://vgmdb.net/album/4299",
    )

    track = candidate.media[0].tracks[0]

    assert candidate.date is None
    assert candidate.album_artists == ()
    assert candidate.media[0].title is None
    assert track.track_number is None
    assert track.titles == ()
    assert track.artists == ()
    assert track.composers == ()
    assert track.duration_seconds is None


def test_non_zero_search_heading_without_parseable_rows_is_invalid() -> None:
    html = "<html><body><h3>4 album results</h3><div>changed layout</div></body></html>"

    with pytest.raises(ValueError):
        parse_search_results(html)


def test_explicit_zero_result_heading_returns_an_empty_result() -> None:
    html = "<html><body><h3>0 album results</h3></body></html>"

    assert parse_search_results(html) == ()


def test_sparse_media_titles_stay_with_their_following_track_table() -> None:
    candidate = parse_album_detail(
        load_fixture("album_sparse_media.html"),
        source_url="https://vgmdb.net/album/4300",
    )

    assert [(medium.medium_number, medium.title) for medium in candidate.media] == [
        (1, None),
        (2, "Bonus Disc"),
    ]


def test_two_part_duration_allows_more_than_sixty_minutes() -> None:
    candidate = parse_album_detail(
        load_fixture("album_sparse_media.html"),
        source_url="https://vgmdb.net/album/4300",
    )

    assert candidate.media[0].tracks[0].duration_seconds == 4_500.0
