"""Native numbered-partial, candidate identity and unchanged calibration regressions."""

from dataclasses import replace
from itertools import permutations
from pathlib import Path

import pytest

from metadata_polisher.domain.matching import LocalisedText, ProviderTrack, ReleaseCandidate, ReleaseMedium
from metadata_polisher.domain.media import FilenameHints, LocalMediaFile, MediaReadResult, StreamInfo
from metadata_polisher.domain.metadata import FieldReadState, MetadataField, MetadataSnapshot, Position
from metadata_polisher.matching.policy import MatchingPolicy, TrackMappingPolicy
from metadata_polisher.matching.release_scoring import (
    LocalEvidenceSource,
    LocalReleaseEvidence,
    LocalTrackEvidence,
    MatchClassification,
    MatchReasonCode,
    ReleaseScore,
    build_local_release_evidence,
    order_local_track_files,
    rank_release_candidates,
    score_release_medium,
)
from metadata_polisher.matching.track_mapping import map_tracks
from metadata_polisher.scanner.grouping import AlbumGroup, GroupingReason


def local_file(number: int, title: str, *, duration: float | None = None) -> LocalMediaFile:
    states = {field: FieldReadState.MISSING for field in MetadataField}

    for field in (
        MetadataField.TITLE,
        MetadataField.ALBUM,
        MetadataField.ALBUM_ARTISTS,
        MetadataField.TRACK,
        MetadataField.DISC,
        MetadataField.DATE,
    ):
        states[field] = FieldReadState.PRESENT

    return LocalMediaFile(
        path=Path("library") / "Example Album" / f"{number:02d}.flac",
        format_id="flac",
        read_result=MediaReadResult(
            metadata=MetadataSnapshot(
                title=title,
                album="Example Album",
                album_artists=("Example Artist",),
                track=Position(number=number),
                disc=Position(number=1, total=1),
                date="2024",
            ),
            field_states=states,
            stream_info=StreamInfo(
                duration_seconds=100.0 + 20.0 * number if duration is None else duration,
                sample_rate=48_000,
                channels=2,
                bit_depth=24,
                codec="FLAC",
            ),
        ),
        filename_hints=FilenameHints(),
        file_id=f"L{number}",
    )


def release(titles: tuple[str, ...] = tuple("ABCDEFGHIJ")) -> ReleaseCandidate:
    return ReleaseCandidate(
        engine_id="review-provider",
        source_id="review-catalogue",
        release_id="release-1",
        titles=(LocalisedText("Example Album", "en", "Latn"),),
        album_artists=("Example Artist",),
        date="2024",
        media=(ReleaseMedium(
            medium_number=1,
            title=None,
            tracks=tuple(
                ProviderTrack(
                    track_number=number,
                    titles=(LocalisedText(title, "en", "Latn"),),
                    artists=("Example Artist",),
                    composers=(),
                    duration_seconds=100.0 + 20.0 * number,
                )
                for number, title in enumerate(titles, 1)
            ),
        ),),
        source_url=None,
    )


def group(files: tuple[LocalMediaFile, ...]) -> AlbumGroup:
    return AlbumGroup(
        group_id="review-ranking",
        files=files,
        album_title="Example Album",
        reason=GroupingReason.DIRECTORY_ALBUM_CONSISTENT,
    )


def complete_files() -> tuple[LocalMediaFile, ...]:
    return tuple(local_file(number, title) for number, title in enumerate("ABCDEFGHIJ", 1))


def codes(result: ReleaseScore) -> set[str]:
    return {item.code for item in result.evidence}


def test_r5_exact_complete_baseline_and_trusted_number_permutation() -> None:
    candidate = release()
    files = complete_files()
    forward = build_local_release_evidence(group(files))
    backward = build_local_release_evidence(group(tuple(reversed(files))))

    assert forward == backward
    result = score_release_medium(forward, candidate, candidate.media[0])
    assert result.score == 100.0
    assert result.classification is MatchClassification.HIGH
    assert map_tracks(files, candidate, selected_medium_index=0).classification is MatchClassification.HIGH


@pytest.mark.parametrize("missing", [1, 5, 10])
def test_r5_correct_alignment_must_not_create_duration_contradictions(missing: int) -> None:
    files = tuple(file for number, file in enumerate(complete_files(), 1) if number != missing)
    candidate = release()
    result = score_release_medium(build_local_release_evidence(group(files)), candidate, candidate.media[0])

    assert MatchReasonCode.DURATION_LARGE_MISMATCH not in codes(result)
    assert result.classification is MatchClassification.REVIEW


def test_r5_duplicate_tags_preserve_caller_order_with_explicit_notice() -> None:
    files = complete_files()[:3]
    duplicated = tuple(
        replace(file, read_result=replace(
            file.read_result,
            metadata=replace(file.read_result.metadata, track=Position(number=1)),
        ))
        for file in reversed(files)
    )
    ordered, notice = order_local_track_files(duplicated)

    assert ordered == duplicated
    assert notice is not None
    assert notice.code is MatchReasonCode.LOCAL_TRACK_ORDER_AMBIGUOUS


def test_r5_genuine_same_track_duration_conflict_requires_review() -> None:
    files = complete_files()
    changed = replace(files[4], read_result=replace(
        files[4].read_result,
        stream_info=replace(files[4].read_result.stream_info, duration_seconds=230.0),
    ))
    files = (*files[:4], changed, *files[5:])
    candidate = release()
    result = score_release_medium(build_local_release_evidence(group(files)), candidate, candidate.media[0])
    mapping = map_tracks(files, candidate, selected_medium_index=0)

    assert MatchReasonCode.DURATION_LARGE_MISMATCH in codes(result)
    assert result.classification is MatchClassification.REVIEW
    conflicting_pair = next(item for item in mapping.mappings if item.local_file_id == "L5")
    assert conflicting_pair.provider_track_index == 4
    assert conflicting_pair.classification is MatchClassification.REVIEW


@pytest.mark.parametrize("separate", [False, True])
@pytest.mark.parametrize("reverse", [False, True])
def test_r8_identical_repetitions_must_not_create_an_alternative(separate: bool, reverse: bool) -> None:
    candidate = release()
    other = release() if separate else candidate
    candidates = (other, candidate) if reverse else (candidate, other)
    ranking = rank_release_candidates(build_local_release_evidence(group(complete_files())), candidates)

    assert candidate == other
    assert len(ranking.entries) == 1
    assert not ranking.ambiguous
    assert ranking.entries[0].result.classification is MatchClassification.HIGH


def test_r8_distinct_equal_candidates_remain_ambiguous_under_all_permutations() -> None:
    candidate = release()
    candidates = (candidate, replace(candidate, release_id="release-2"), replace(candidate, release_id="release-3"))
    local = build_local_release_evidence(group(complete_files()))
    baseline = rank_release_candidates(local, candidates)

    for candidate_order in permutations(candidates):
        assert rank_release_candidates(local, candidate_order) == baseline

    assert baseline.ambiguous
    assert len(baseline.entries) == 3
    assert all(item.result.classification is MatchClassification.REVIEW for item in baseline.entries)


def test_r12_half_title_agreement_is_currently_high_with_full_coverage() -> None:
    files = tuple(local_file(number, title) for number, title in enumerate(("aaaa", "bbbb", "cccc"), 1))
    candidate = release(("aazz", "bbyy", "ccxx"))
    result = score_release_medium(build_local_release_evidence(group(files)), candidate, candidate.media[0])
    mapping = map_tracks(files, candidate, selected_medium_index=0)

    assert result.score == 85.0
    assert result.classification is MatchClassification.HIGH
    title_evidence = next(item for item in result.evidence if item.code == MatchReasonCode.TRACK_TITLE_ORDER_AGREEMENT)
    assert title_evidence.contribution == 15.0
    assert "50.0%" in title_evidence.detail and "3/3" in title_evidence.detail
    assert MatchReasonCode.TRACK_TITLE_COVERAGE_INSUFFICIENT not in codes(result)
    assert mapping.classification is MatchClassification.REVIEW
    assert all(item.classification is MatchClassification.REVIEW for item in mapping.mappings)


@pytest.mark.parametrize(
    "date,classification", [(None, MatchClassification.HIGH), ("2021", MatchClassification.REVIEW)],
)
def test_r12_missing_year_and_wrong_edition_year_have_different_contracts(
    date: str | None, classification: MatchClassification,
) -> None:
    candidate = replace(release(), date=date)
    result = score_release_medium(build_local_release_evidence(group(complete_files())), candidate, candidate.media[0])

    assert result.classification is classification
    assert (MatchReasonCode.YEAR_CONTRADICTION in codes(result)) == (date == "2021")


def test_r12_similar_album_name_can_remain_high_when_track_content_agrees() -> None:
    candidate = replace(release(), titles=(LocalisedText("Example Album II", "en", "Latn"),))
    result = score_release_medium(build_local_release_evidence(group(complete_files())), candidate, candidate.media[0])

    # A potentially different album cannot be rejected using its name alone
    # when the supplied content agrees exactly. This fixture is calibration
    # evidence only; it does not establish that any live release is misidentified.
    assert MatchReasonCode.ALBUM_TITLE_SIMILARITY in codes(result)
    assert result.classification is MatchClassification.HIGH


def test_r12_multilingual_alias_retains_exact_matching_variant() -> None:
    candidate = release()
    medium = replace(candidate.media[0], tracks=tuple(
        replace(track, titles=(LocalisedText("海", "ja", "Jpan"), *track.titles))
        for track in candidate.media[0].tracks
    ))
    candidate = replace(candidate, media=(medium,))
    result = score_release_medium(build_local_release_evidence(group(complete_files())), candidate, medium)

    assert MatchReasonCode.TRACK_TITLE_ORDER_EXACT in codes(result)
    assert result.classification is MatchClassification.HIGH


@pytest.mark.parametrize("missing", [1, 5, 10])
def test_numbered_partial_content_scores_98_but_keeps_count_review(missing: int) -> None:
    files = tuple(file for index, file in enumerate(complete_files(), 1) if index != missing)
    candidate = release()
    result = score_release_medium(build_local_release_evidence(group(files)), candidate, candidate.media[0])

    # Nine known titles and durations agree. Only the count earns 18/20;
    # the absent track remains visible as a contradiction and requires review.
    assert result.score == 98.0
    assert result.classification is MatchClassification.REVIEW
    assert MatchReasonCode.TRACK_COUNT_CONTRADICTION in codes(result)
    assert MatchReasonCode.DURATION_LARGE_MISMATCH not in codes(result)

    title = next(item for item in result.evidence if "9/10" in item.detail)
    assert title.contribution == 30.0

    if missing != 10:
        assert title.code == "TRACK_TITLE_NUMBER_EXACT"
        assert "number" in title.detail.casefold()


@pytest.mark.parametrize("extra_number", [1, 6, 12])
def test_extra_numbered_track_preserves_supported_content_and_requires_review(extra_number: int) -> None:
    # Leave a genuine hole in both corresponding lists, then fill only the
    # local side. This exercises an extra at the start, middle or end.
    numbers = tuple(number for number in range(1, 13) if number != extra_number)
    tracks = tuple(local_file(number, f"Movement {number}") for number in numbers)
    candidate = replace(release(), media=(ReleaseMedium(1, None, tuple(
        ProviderTrack(number, (LocalisedText(f"Movement {number}", None, None),), (), (), 100.0 + 20.0 * number)
        for number in numbers
    )),))
    files = (*tracks, local_file(extra_number, "Unlisted bonus"))
    result = score_release_medium(build_local_release_evidence(group(files)), candidate, candidate.media[0])

    assert result.classification is MatchClassification.REVIEW
    assert MatchReasonCode.TRACK_COUNT_CONTRADICTION in codes(result)
    assert MatchReasonCode.DURATION_LARGE_MISMATCH not in codes(result)


@pytest.mark.parametrize("source", ["missing", "partial", "duplicate", "wrong", "mixed"])
def test_unreliable_local_numbers_do_not_repair_correspondence(source: str) -> None:
    files = complete_files()[1:]
    changed = []

    for index, file in enumerate(files):
        number = file.read_result.metadata.track.number
        state = FieldReadState.PRESENT
        hints = FilenameHints()

        if source == "missing" or (source in {"partial", "mixed"} and index == 0):
            state = FieldReadState.MISSING
            number = None

        if source == "mixed" and index == 0:
            hints = FilenameHints(track_number=2)

        if source == "duplicate":
            number = 2

        if source == "wrong":
            number = index + 1

        states = dict(file.read_result.field_states)
        states[MetadataField.TRACK] = state
        changed.append(replace(file, read_result=replace(
            file.read_result,
            field_states=states,
            metadata=replace(file.read_result.metadata, track=Position(number)),
        ), filename_hints=hints))

    candidate = release()
    result = score_release_medium(build_local_release_evidence(group(tuple(changed))), candidate, candidate.media[0])

    assert result.classification is not MatchClassification.HIGH
    assert MatchReasonCode.DURATION_LARGE_MISMATCH in codes(result)
    assert not any(item.code == "TRACK_TITLE_NUMBER_EXACT" for item in result.evidence)


@pytest.mark.parametrize("bad_number", [None, 0, 2])
def test_invalid_missing_or_duplicate_provider_number_keeps_positional_fallback(bad_number: int | None) -> None:
    candidate = release()
    medium = candidate.media[0]
    medium = replace(medium, tracks=(replace(medium.tracks[0], track_number=bad_number), *medium.tracks[1:]))
    candidate = replace(candidate, media=(medium,))
    result = score_release_medium(build_local_release_evidence(group(complete_files()[1:])), candidate, medium)

    assert MatchReasonCode.DURATION_LARGE_MISMATCH in codes(result)
    assert result.classification is not MatchClassification.HIGH


def test_filename_numbering_uses_one_lower_authority_tier_for_partial_comparison() -> None:
    files = []

    for file in complete_files()[1:]:
        states = dict(file.read_result.field_states)
        states[MetadataField.TRACK] = FieldReadState.UNREADABLE
        files.append(replace(file, read_result=replace(file.read_result, field_states=states),
                             filename_hints=FilenameHints(track_number=file.read_result.metadata.track.number)))

    local = build_local_release_evidence(group(tuple(files)))
    candidate = release()
    result = score_release_medium(local, candidate, candidate.media[0])

    assert all(track.track_number_source is LocalEvidenceSource.FILENAME for track in local.tracks)
    assert result.score == 98.0
    assert result.classification is MatchClassification.REVIEW


def test_numbered_partial_retains_real_duration_conflict_and_selected_medium_boundary() -> None:
    files = complete_files()[1:]
    changed = replace(files[3], read_result=replace(
        files[3].read_result,
        stream_info=replace(files[3].read_result.stream_info, duration_seconds=230.0),
    ))
    files = (*files[:3], changed, *files[4:])
    candidate = release()
    selected = candidate.media[0]
    other_medium = replace(selected, medium_number=2)
    candidate = replace(candidate, media=(selected, other_medium))
    result = score_release_medium(build_local_release_evidence(group(files)), candidate, selected)

    assert result.classification is MatchClassification.REVIEW
    assert MatchReasonCode.DURATION_LARGE_MISMATCH in codes(result)
    assert any(item.code == "TRACK_TITLE_NUMBER_EXACT" for item in result.evidence)
    assert "9/10" in next(item.detail for item in result.evidence if item.code == "TRACK_TITLE_NUMBER_EXACT")


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("conflict_field", ["title", "source_url", "sibling_medium", "completeness"])
def test_conflicting_full_release_payload_with_same_identity_is_rejected(reverse: bool, conflict_field: str) -> None:
    candidate = release()
    changes = {
        "title": {"titles": (LocalisedText("Different album", None, None),)},
        "source_url": {"source_url": "https://example.invalid/release"},
        "sibling_medium": {"media": (*candidate.media, replace(candidate.media[0], medium_number=2))},
        "completeness": {"media_complete": False},
    }
    conflict = replace(candidate, **changes[conflict_field])
    candidates = (candidate, conflict) if reverse else (conflict, candidate)

    with pytest.raises(ValueError, match="conflicting.*identity"):
        rank_release_candidates(build_local_release_evidence(group(complete_files())), candidates)


def test_old_local_track_constructor_remains_supported_without_numbering() -> None:
    track = LocalTrackEvidence("Title", 180.0)
    local = LocalReleaseEvidence(None, (), None, None, None, (track,))

    assert local.tracks[0].track_number is None
    assert local.tracks[0].track_number_source is None


def test_missing_and_extra_numbered_tracks_cannot_cancel_the_review_requirement() -> None:
    files = (*complete_files()[1:], local_file(11, "Unlisted bonus"))
    candidate = release()
    result = score_release_medium(build_local_release_evidence(group(files)), candidate, candidate.media[0])

    assert MatchReasonCode.TRACK_COUNT_EXACT in codes(result)
    assert "TRACK_COMPARISON_PARTIAL" in codes(result)
    assert result.score == 100.0
    assert result.classification is MatchClassification.REVIEW


def test_visible_unpaired_tail_requires_review_without_inventing_provider_total() -> None:
    candidate = release()
    medium = replace(candidate.media[0], has_data_tracks=True)
    candidate = replace(candidate, media=(medium,))
    result = score_release_medium(build_local_release_evidence(group(complete_files()[:-1])), candidate, medium)

    assert medium.track_total is None
    assert MatchReasonCode.TRACK_COUNT_UNAVAILABLE in codes(result)
    assert "TRACK_COMPARISON_PARTIAL" in codes(result)
    assert result.classification is MatchClassification.REVIEW


@pytest.mark.parametrize("title_threshold,uses_numbers", [(0.90, True), (0.900001, False)])
def test_number_repair_uses_the_shared_inclusive_content_title_policy(
    title_threshold: float,
    uses_numbers: bool,
) -> None:
    # One substitution in ten characters gives 90% title agreement. All other
    # numbered pairs match exactly, so only this policy boundary decides repair.
    files = complete_files()[1:]
    changed = replace(files[0], read_result=replace(
        files[0].read_result,
        metadata=replace(files[0].read_result.metadata, title="abcdefghix"),
    ))
    local = build_local_release_evidence(group((changed, *files[1:])))
    candidate = release()
    tracks = candidate.media[0].tracks
    medium = replace(candidate.media[0], tracks=(tracks[0], replace(
        tracks[1], titles=(LocalisedText("abcdefghij", None, None),),
    ), *tracks[2:]))
    candidate = replace(candidate, media=(medium,))
    policy = MatchingPolicy(track_mapping=TrackMappingPolicy(minimum_content_title_similarity=title_threshold))
    result = score_release_medium(local, candidate, medium, policy)

    assert ("TRACK_TITLE_NUMBER_AGREEMENT" in codes(result)) is uses_numbers
    assert result.classification is not MatchClassification.HIGH


def test_equal_common_durations_do_not_rescue_wrong_numbered_titles() -> None:
    files = tuple(replace(file, read_result=replace(
        file.read_result,
        stream_info=replace(file.read_result.stream_info, duration_seconds=180.0),
    )) for file in complete_files()[1:])
    candidate = release()
    medium = replace(candidate.media[0], tracks=tuple(
        replace(track, duration_seconds=180.0, titles=(LocalisedText("Unrelated", None, None),))
        for track in candidate.media[0].tracks
    ))
    candidate = replace(candidate, media=(medium,))
    result = score_release_medium(build_local_release_evidence(group(files)), candidate, medium)

    assert not any(item.code.startswith("TRACK_TITLE_NUMBER_") for item in result.evidence)
    assert result.classification is not MatchClassification.HIGH
