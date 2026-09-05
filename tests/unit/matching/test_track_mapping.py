from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from metadata_polisher.domain.matching import (
    LocalisedText,
    ProviderTrack,
    ReleaseCandidate,
    ReleaseMedium,
)
from metadata_polisher.domain.media import FilenameHints, LocalMediaFile, MediaReadResult, StreamInfo
from metadata_polisher.domain.metadata import FieldReadState, MetadataField, MetadataSnapshot, Position
from metadata_polisher.matching.release_scoring import MatchClassification, MatchReasonCode
from metadata_polisher.matching.track_mapping import TrackMappingResult, map_tracks


def make_local(
    file_id: str,
    *,
    title: str | None,
    tagged_number: int | None,
    filename_number: int | None = None,
    filename_title: str | None = None,
    duration: float | None = 180.0,
    track_state: FieldReadState | None = None,
) -> LocalMediaFile:
    # Separate stored values, read states and filename hints so fixtures can
    # represent contradictory or unreadable tags without silently fixing them.
    states = {field: FieldReadState.MISSING for field in MetadataField}
    states[MetadataField.TITLE] = (
        FieldReadState.PRESENT if title is not None else FieldReadState.MISSING
    )
    states[MetadataField.TRACK] = track_state or (
        FieldReadState.PRESENT if tagged_number is not None else FieldReadState.MISSING
    )

    return LocalMediaFile(
        path=Path("library") / f"{file_id}.flac",
        format_id="flac",
        read_result=MediaReadResult(
            metadata=MetadataSnapshot(
                title=title,
                artists=("Example Artist",),
                track=Position(number=tagged_number),
            ),
            field_states=states,
            stream_info=StreamInfo(
                duration_seconds=duration,
                sample_rate=48_000,
                channels=2,
                bit_depth=24,
                codec="FLAC",
            ),
        ),
        filename_hints=FilenameHints(
            track_number=filename_number,
            probable_title=filename_title,
        ),
        file_id=file_id,
    )


def make_provider(
    number: int | None,
    title: str | None,
    *,
    duration: float | None = 180.0,
) -> ProviderTrack:
    titles = () if title is None else (LocalisedText(title, "en", "Latn"),)

    return ProviderTrack(
        track_number=number,
        titles=titles,
        artists=("Example Artist",),
        composers=(),
        duration_seconds=duration,
    )


def make_release(
    *tracks: ProviderTrack,
    medium_number: int | None = 1,
    leading_medium: bool = False,
) -> ReleaseCandidate:
    media: tuple[ReleaseMedium, ...] = (
        ReleaseMedium(medium_number=medium_number, title=None, tracks=tracks),
    )

    if leading_medium:
        media = (
            ReleaseMedium(
                medium_number=1,
                title="Disc 1",
                tracks=(make_provider(1, "Other disc"),),
            ),
            ReleaseMedium(medium_number=medium_number, title="Disc 2", tracks=tracks),
        )

    return ReleaseCandidate(
        engine_id="test-engine",
        source_id="test-source",
        release_id="release-1",
        titles=(LocalisedText("Example Album", "en", "Latn"),),
        album_artists=("Example Artist",),
        date="2024",
        media=media,
        source_url=None,
    )


def mapping_pairs(result: TrackMappingResult) -> tuple[tuple[str, int], ...]:
    return tuple(
        (mapping.local_file_id, mapping.provider_track_index)
        for mapping in result.mappings
    )


def mapping_codes(result: TrackMappingResult) -> set[str]:
    return {
        evidence.code
        for mapping in result.mappings
        for evidence in mapping.evidence
    }


def result_codes(result: TrackMappingResult) -> set[str]:
    return {evidence.code for evidence in result.evidence}


def test_exact_three_track_mapping_is_high_and_derives_selected_medium_totals() -> None:
    local = tuple(
        make_local(str(number), title=title, tagged_number=number)
        for number, title in enumerate(("Opening", "Journey", "Finale"), start=1)
    )
    release = make_release(
        *(make_provider(number, title) for number, title in enumerate(("Opening", "Journey", "Finale"), start=1)),
        medium_number=2,
        leading_medium=True,
    )

    result = map_tracks(local, release, selected_medium_index=1)

    assert mapping_pairs(result) == (("1", 0), ("2", 1), ("3", 2))
    assert result.selected_medium_index == 1
    assert result.selected_medium_number == 2
    assert result.classification is MatchClassification.HIGH
    assert all(mapping.track_position.total == 3 for mapping in result.mappings)
    assert all(mapping.disc_position == Position(number=2, total=2) for mapping in result.mappings)
    assert MatchReasonCode.TRACK_NUMBER_EXACT_TAG in mapping_codes(result)


def test_filename_numbers_are_used_only_when_track_tags_are_not_present() -> None:
    local = tuple(
        make_local(
            str(number),
            title=None,
            tagged_number=None,
            filename_number=number,
            filename_title=title,
        )
        for number, title in enumerate(("Opening", "Journey", "Finale"), start=1)
    )
    release = make_release(
        *(make_provider(number, title) for number, title in enumerate(("Opening", "Journey", "Finale"), start=1))
    )

    result = map_tracks(local, release, selected_medium_index=0)

    assert mapping_pairs(result) == (("1", 0), ("2", 1), ("3", 2))
    assert MatchReasonCode.TRACK_NUMBER_EXACT_FILENAME in mapping_codes(result)


# A bonus row shifts provider numbers after the opening track. The expected
# pairs follow title/duration evidence across the gap while retaining the
# number conflict as a reason for review.
def test_remote_bonus_track_becomes_a_gap_without_shifting_later_matches() -> None:
    local = (
        make_local("a", title="Opening", tagged_number=1),
        make_local("b", title="Journey", tagged_number=2),
        make_local("c", title="Finale", tagged_number=3),
    )
    release = make_release(
        make_provider(1, "Opening"),
        make_provider(2, "Bonus"),
        make_provider(3, "Journey"),
        make_provider(4, "Finale"),
    )

    result = map_tracks(local, release, selected_medium_index=0)

    assert mapping_pairs(result) == (("a", 0), ("b", 2), ("c", 3))
    assert result.unmatched_local_file_ids == ()
    assert result.unmatched_provider_indexes == (1,)
    assert result.classification is MatchClassification.REVIEW


def test_missing_local_track_leaves_one_provider_index_unmatched() -> None:
    local = (
        make_local("a", title="Opening", tagged_number=1),
        make_local("c", title="Finale", tagged_number=3),
    )
    release = make_release(
        make_provider(1, "Opening"),
        make_provider(2, "Journey"),
        make_provider(3, "Finale"),
    )

    result = map_tracks(local, release, selected_medium_index=0)

    assert mapping_pairs(result) == (("a", 0), ("c", 2))
    assert result.unmatched_provider_indexes == (1,)


def test_extra_local_track_is_left_unmatched_without_displacing_exact_pairs() -> None:
    local = (
        make_local("a", title="Opening", tagged_number=1),
        make_local("x", title="Local interlude", tagged_number=None, duration=None),
        make_local("b", title="Finale", tagged_number=2),
    )
    release = make_release(make_provider(1, "Opening"), make_provider(2, "Finale"))

    result = map_tracks(local, release, selected_medium_index=0)

    assert mapping_pairs(result) == (("a", 0), ("b", 1))
    assert result.unmatched_local_file_ids == ("x",)


# A wrong tag does not erase an otherwise useful title/duration match.
# The match remains visible, but its contradiction prevents HIGH confidence.
def test_wrong_real_track_number_can_map_by_exact_title_and_duration_but_requires_review() -> None:
    local = (make_local("wrong-number", title="Opening", tagged_number=9),)
    release = make_release(make_provider(1, "Opening"))

    result = map_tracks(local, release, selected_medium_index=0)

    assert mapping_pairs(result) == (("wrong-number", 0),)
    assert result.mappings[0].classification is MatchClassification.REVIEW
    assert result.classification is MatchClassification.REVIEW
    assert MatchReasonCode.TRACK_NUMBER_CONFLICT in mapping_codes(result)


def test_exact_title_with_huge_duration_mismatch_is_downgraded_to_review() -> None:
    local = (make_local("long", title="Opening", tagged_number=1, duration=100.0),)
    release = make_release(make_provider(1, "Opening", duration=300.0))

    result = map_tracks(local, release, selected_medium_index=0)

    assert mapping_pairs(result) == (("long", 0),)
    assert result.mappings[0].classification is MatchClassification.REVIEW
    assert MatchReasonCode.TRACK_DURATION_LARGE_MISMATCH in mapping_codes(result)


# Both provider rows have the same only usable evidence. Sequence position
# must not break this evidential tie and invent a confident identification.
def test_equal_substantive_remote_candidates_remain_unresolved() -> None:
    local = (
        make_local(
            "ambiguous",
            title="Same title",
            tagged_number=None,
            filename_number=None,
            duration=None,
        ),
    )
    release = make_release(
        make_provider(None, "Same title", duration=None),
        make_provider(None, "Same title", duration=None),
    )

    result = map_tracks(local, release, selected_medium_index=0)

    assert result.mappings == ()
    assert result.unmatched_local_file_ids == ("ambiguous",)
    assert result.unmatched_provider_indexes == (0, 1)
    assert result.classification is MatchClassification.REVIEW
    assert MatchReasonCode.TRACK_MAPPING_AMBIGUOUS in result_codes(result)


# The sequences each have one row, so position agrees perfectly. This is
# insufficient by itself: there is no title, number or duration comparison.
def test_no_substantive_pair_evidence_is_unmapped_and_low() -> None:
    local = (make_local("unknown", title=None, tagged_number=None, duration=None),)
    release = make_release(make_provider(None, None, duration=None), medium_number=None)

    result = map_tracks(local, release, selected_medium_index=0)

    assert result.mappings == ()
    assert result.unmatched_local_file_ids == ("unknown",)
    assert result.unmatched_provider_indexes == (0,)
    assert result.classification is MatchClassification.LOW
    assert MatchReasonCode.TRACK_MAPPING_INSUFFICIENT_EVIDENCE in result_codes(result)


# Known title and position agree completely. Missing duration and number
# are excluded from the possible-weight denominator, preserving score 100.
def test_missing_pair_dimensions_are_unknown_instead_of_zero_score() -> None:
    local = (make_local("title-only", title="Opening", tagged_number=None, duration=None),)
    release = make_release(make_provider(None, "Opening", duration=None), medium_number=None)

    result = map_tracks(local, release, selected_medium_index=0)

    assert mapping_pairs(result) == (("title-only", 0),)
    assert result.mappings[0].score == 100.0
    assert {
        evidence.code: evidence.contribution
        for evidence in result.mappings[0].evidence
    }[MatchReasonCode.TRACK_DURATION_UNAVAILABLE] == 0.0


# Swapping Alpha and Beta would require crossing the two exact-title pairs.
# The order-preserving mapper must retain at most one instead of reordering
# the album to collect both attractive individual matches.
def test_sequence_alignment_never_crosses_mappings() -> None:
    local = (
        make_local("a", title="Alpha", tagged_number=None, duration=None),
        make_local("b", title="Beta", tagged_number=None, duration=None),
    )
    release = make_release(
        make_provider(None, "Beta", duration=None),
        make_provider(None, "Alpha", duration=None),
    )

    result = map_tracks(local, release, selected_medium_index=0)

    provider_indexes = tuple(mapping.provider_track_index for mapping in result.mappings)
    assert provider_indexes == tuple(sorted(provider_indexes))
    assert len(result.mappings) == 1


# A single visible row establishes a total of one, but does not establish
# the source track or disc number. Index zero is an address, not a tag.
def test_absent_provider_numbers_are_not_inferred_from_indexes() -> None:
    local = (make_local("a", title="Alpha", tagged_number=None, duration=None),)
    release = make_release(make_provider(None, "Alpha", duration=None), medium_number=None)

    result = map_tracks(local, release, selected_medium_index=0)

    assert result.mappings[0].track_position == Position(number=None, total=1)
    assert result.mappings[0].disc_position == Position(number=None, total=1)


@pytest.mark.parametrize("selected_medium_index", [-1, 1, True])
def test_selected_medium_index_must_identify_an_existing_medium(
    selected_medium_index: object,
) -> None:
    release = make_release(make_provider(1, "Alpha"))

    with pytest.raises((TypeError, IndexError)):
        map_tracks((), release, selected_medium_index=selected_medium_index)  # type: ignore[arg-type]


def test_duplicate_local_file_ids_are_rejected_before_mapping() -> None:
    local = (
        make_local("duplicate", title="Alpha", tagged_number=1),
        make_local("duplicate", title="Beta", tagged_number=2),
    )
    release = make_release(make_provider(1, "Alpha"), make_provider(2, "Beta"))

    with pytest.raises(ValueError, match="duplicate local file IDs"):
        map_tracks(local, release, selected_medium_index=0)


def test_mapping_results_are_frozen_and_partition_each_side_once() -> None:
    local = (
        make_local("a", title="Alpha", tagged_number=1),
        make_local("extra", title="Extra", tagged_number=None, duration=None),
    )
    release = make_release(make_provider(1, "Alpha"), make_provider(2, "Remote extra"))

    result = map_tracks(local, release, selected_medium_index=0)

    # Each side must be covered exactly once by matched plus unmatched items.
    # Check both completeness and disjointness: either check alone could hide
    # a dropped file or duplicate assignment.
    mapped_local = {mapping.local_file_id for mapping in result.mappings}
    mapped_provider = {mapping.provider_track_index for mapping in result.mappings}
    assert mapped_local | set(result.unmatched_local_file_ids) == {file.file_id for file in local}
    assert mapped_provider | set(result.unmatched_provider_indexes) == {0, 1}
    assert mapped_local.isdisjoint(result.unmatched_local_file_ids)
    assert mapped_provider.isdisjoint(result.unmatched_provider_indexes)

    with pytest.raises(FrozenInstanceError):
        result.classification = MatchClassification.HIGH  # type: ignore[misc]


def test_mapping_result_rejects_boolean_provider_indexes() -> None:
    with pytest.raises(TypeError, match="unmatched_provider_indexes"):
        TrackMappingResult(
            mappings=(),
            unmatched_local_file_ids=(),
            unmatched_provider_indexes=(True,),  # type: ignore[arg-type]
            selected_medium_index=0,
            selected_medium_number=1,
            classification=MatchClassification.LOW,
            evidence=(),
        )
