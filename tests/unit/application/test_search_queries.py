# Fallback cleaning affects additional search terms only. The exact tagged title
# and annotations outside the narrowly recognised patterns must remain available.

from dataclasses import replace
from pathlib import Path

import pytest

from metadata_polisher.application.lookup import (
    build_release_search_queries,
    build_release_search_query,
)
from metadata_polisher.domain.media import LocalMediaFile, MediaReadResult, StreamInfo
from metadata_polisher.domain.metadata import FieldReadState, MetadataField, MetadataSnapshot, Position
from metadata_polisher.scanner.grouping import AlbumGroup, GroupingReason

_RELEASE_TITLE = "ファイアーエムブレム エンゲージ オリジナルサウンドトラック"


def make_group(album: str | None) -> AlbumGroup:
    states = {field: FieldReadState.MISSING for field in MetadataField}

    for field in (
        MetadataField.TITLE,
        MetadataField.ALBUM_ARTISTS,
        MetadataField.DISC,
        MetadataField.DATE,
    ):
        states[field] = FieldReadState.PRESENT

    if album is not None:
        states[MetadataField.ALBUM] = FieldReadState.PRESENT

    files = tuple(
        LocalMediaFile(
            path=Path("library") / "Disc 2" / f"{number:02}.flac",
            format_id="flac",
            read_result=MediaReadResult(
                metadata=MetadataSnapshot(
                    title=title,
                    album=album,
                    album_artists=("Album Artist",),
                    # The real collection has this contradictory tag. Search
                    # relaxation must not silently repair its source evidence.
                    disc=Position(number=1, total=1),
                    date="2024",
                ),
                field_states=states,
                stream_info=StreamInfo(
                    duration_seconds=180.0,
                    sample_rate=48_000,
                    channels=2,
                    bit_depth=24,
                    codec="FLAC",
                ),
            ),
        )
        for number, title in enumerate(("Opening", "Finale"), start=1)
    )

    return AlbumGroup(
        group_id="group-0001",
        files=files,
        album_title=album,
        reason=GroupingReason.DIRECTORY_ALBUM_CONSISTENT,
    )


@pytest.mark.parametrize(
    ("album", "clean_title"),
    (
        (f"[QWCI-00014-2] {_RELEASE_TITLE} (Disc 2)", _RELEASE_TITLE),
        (f"[QWCI-00014] {_RELEASE_TITLE}", _RELEASE_TITLE),
        (f"{_RELEASE_TITLE} (Disc 1)", _RELEASE_TITLE),
        (f"{_RELEASE_TITLE} (disc 07)", _RELEASE_TITLE),
        ("[QWCI-00014-2] Album BONUS DISC (Disc 2)", "Album BONUS DISC"),
        ("Album (Live) (Disc 2)", "Album (Live)"),
        ("[Live] Album (Disc 2)", "[Live] Album"),
    ),
)
def test_decorated_album_adds_one_clean_title_query_after_original_strategies(
    album: str,
    clean_title: str,
) -> None:
    group = make_group(album)
    primary = build_release_search_query(group)
    relaxed = replace(primary, year=None, disc_hint=None, local_track_count=0)
    album_only = replace(relaxed, artists=())

    queries = build_release_search_queries(group)

    # The original strict, relaxed and album-only searches keep their order.
    # Only the extra broad query drops decorations; no source tag is changed.
    assert queries == (
        primary,
        relaxed,
        album_only,
        replace(album_only, album=clean_title),
    )
    assert group.album_title == album
    assert all(file.read_result.metadata.album == album for file in group.files)
    assert all(file.read_result.metadata.disc == Position(1, 1) for file in group.files)
    assert primary.disc_hint == 1
    assert all(query.distinctive_titles == ("Opening", "Finale") for query in queries)


@pytest.mark.parametrize(
    "album",
    (
        "Album BONUS DISC",
        "Album (BONUS DISC)",
        "Album (Live)",
        "Album [Disc 2]",
        "Album (Disc II)",
        "Album (Disc 0)",
        "Album (Disc 2: Bonus)",
        "Album (Disc 2) Reprise",
        "[Live] Album",
        "[2024] Album",
        "[Studio-54] Album",
        "Album [QWCI-00014-2]",
        "[QWCI-00014-2]",
        "(Disc 2)",
        "[QWCI-00014-2] (Disc 2)",
    ),
)
def test_meaningful_or_ambiguous_album_text_does_not_add_a_cleaned_query(album: str) -> None:
    group = make_group(album)
    primary = build_release_search_query(group)
    relaxed = replace(primary, year=None, disc_hint=None, local_track_count=0)

    assert build_release_search_queries(group) == (
        primary,
        relaxed,
        replace(relaxed, artists=()),
    )


def test_clean_title_query_order_is_independent_of_input_file_order() -> None:
    group = make_group(f"[QWCI-00014-2] {_RELEASE_TITLE} (Disc 2)")
    reversed_group = replace(group, files=tuple(reversed(group.files)))

    forwards = build_release_search_queries(group)
    backwards = build_release_search_queries(reversed_group)

    assert forwards == backwards
    assert tuple(query.album for query in forwards) == (
        group.album_title,
        group.album_title,
        group.album_title,
        _RELEASE_TITLE,
    )


def test_missing_album_does_not_invent_a_title_from_disc_folder_or_track_titles() -> None:
    queries = build_release_search_queries(make_group(None))

    assert len(queries) == 2
    assert all(query.album is None for query in queries)
