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
from metadata_polisher.matching.policy import (
    ClassificationThresholds,
    MatchingPolicy,
    ReleaseScoringWeights,
    TrackMappingPolicy,
)
from metadata_polisher.matching.release_scoring import (
    LocalReleaseEvidence,
    LocalTrackEvidence,
    MatchClassification,
    MatchReasonCode,
    ReleaseScore,
    build_local_release_evidence,
    rank_release_candidates,
    score_release_medium,
)
from metadata_polisher.scanner.grouping import AlbumGroup, GroupingReason


def make_local_file(
    number: int,
    *,
    title: str | None = None,
    title_state: FieldReadState | None = None,
    filename_title: str | None = None,
    album: str | None = "Example Album",
    album_state: FieldReadState | None = None,
    album_artists: tuple[str, ...] = ("Example Artist",),
    artist_state: FieldReadState | None = None,
    date: str | None = "2024",
    date_state: FieldReadState | None = None,
    disc_number: int | None = 1,
    disc_total: int | None = 1,
    disc_state: FieldReadState | None = None,
    filename_disc: int | None = None,
    duration_seconds: float | None = 180.0,
    track_number: int | None = None,
    track_state: FieldReadState | None = None,
) -> LocalMediaFile:
    # Allow fixtures to carry a raw value whose state is not PRESENT. These
    # cases prove that read quality, rather than an incidental stored value,
    # determines whether the scorer may trust a dimension.
    states = {field: FieldReadState.MISSING for field in MetadataField}
    states[MetadataField.TITLE] = title_state or (
        FieldReadState.PRESENT if title is not None else FieldReadState.MISSING
    )
    states[MetadataField.ALBUM] = album_state or (
        FieldReadState.PRESENT if album is not None else FieldReadState.MISSING
    )
    states[MetadataField.ALBUM_ARTISTS] = artist_state or (
        FieldReadState.PRESENT if album_artists else FieldReadState.MISSING
    )
    states[MetadataField.DATE] = date_state or (
        FieldReadState.PRESENT if date is not None else FieldReadState.MISSING
    )
    states[MetadataField.DISC] = disc_state or (
        FieldReadState.PRESENT if disc_number is not None or disc_total is not None else FieldReadState.MISSING
    )
    states[MetadataField.TRACK] = track_state or (
        FieldReadState.PRESENT if track_number is not None else FieldReadState.MISSING
    )

    return LocalMediaFile(
        path=Path("library") / "Example Album" / f"{number:02d}.flac",
        format_id="flac",
        read_result=MediaReadResult(
            metadata=MetadataSnapshot(
                title=title,
                album=album,
                album_artists=album_artists,
                track=Position(number=track_number),
                disc=Position(number=disc_number, total=disc_total),
                date=date,
            ),
            field_states=states,
            stream_info=StreamInfo(
                duration_seconds=duration_seconds,
                sample_rate=48_000,
                channels=2,
                bit_depth=24,
                codec="FLAC",
            ),
        ),
        filename_hints=FilenameHints(
            disc_number=filename_disc,
            track_number=number,
            probable_title=filename_title,
        ),
    )


def make_group(*files: LocalMediaFile) -> AlbumGroup:
    return AlbumGroup(
        group_id="group-release-score",
        files=files,
        album_title="Example Album",
        reason=GroupingReason.DIRECTORY_ALBUM_CONSISTENT,
    )


def make_provider_track(
    number: int,
    title: str | None,
    *,
    duration_seconds: float | None = 180.0,
    language: str | None = "en",
    script: str | None = "Latn",
) -> ProviderTrack:
    titles = () if title is None else (LocalisedText(value=title, language=language, script=script),)

    return ProviderTrack(
        track_number=number,
        titles=titles,
        artists=("Example Artist",),
        composers=(),
        duration_seconds=duration_seconds,
    )


def make_candidate(
    release_id: str,
    media: tuple[ReleaseMedium, ...],
    *,
    title: str = "Example Album",
    artist: str = "Example Artist",
    date: str | None = "2024-03-01",
    language: str | None = "en",
    script: str | None = "Latn",
    source_id: str = "catalogue",
) -> ReleaseCandidate:
    return ReleaseCandidate(
        engine_id="test-engine",
        source_id=source_id,
        release_id=release_id,
        titles=(LocalisedText(value=title, language=language, script=script),),
        album_artists=(artist,),
        date=date,
        media=media,
        source_url=None,
    )


def evidence_codes(result: ReleaseScore) -> set[str]:
    return {item.code for item in result.evidence}


def test_policy_centralises_exact_release_weights_thresholds_and_mapping_tolerances() -> None:
    policy = MatchingPolicy()

    assert policy.release_weights == ReleaseScoringWeights(
        album_title=25.0,
        track_title_order=30.0,
        selected_medium_track_count=20.0,
        duration=10.0,
        disc=5.0,
        year=5.0,
        artist=3.0,
        language_script=2.0,
    )
    assert policy.classification == ClassificationThresholds(
        high=85.0,
        review=65.0,
        ambiguity_margin=5.0,
        minimum_high_track_title_coverage=0.60,
    )
    assert policy.track_mapping == TrackMappingPolicy(
        close_duration_seconds=3.0,
        large_duration_mismatch_seconds=10.0,
    )


# With every available dimension exact, earned and possible weights match.
# The explicit contributions document how the default 100 points are built.
def test_exact_release_and_selected_medium_score_one_hundred_with_weighted_evidence() -> None:
    local_files = tuple(
        make_local_file(number, title=title)
        for number, title in enumerate(("Opening", "Journey", "Finale"), start=1)
    )
    medium = ReleaseMedium(
        medium_number=1,
        title=None,
        tracks=tuple(
            make_provider_track(number, title)
            for number, title in enumerate(("Opening", "Journey", "Finale"), start=1)
        ),
    )
    candidate = make_candidate("exact", (medium,))

    result = score_release_medium(build_local_release_evidence(make_group(*local_files)), candidate, medium)

    contributions = {item.code: item.contribution for item in result.evidence}
    assert result.score == 100.0
    assert result.classification is MatchClassification.HIGH
    assert contributions[MatchReasonCode.ALBUM_TITLE_EXACT] == 25.0
    assert contributions[MatchReasonCode.TRACK_TITLE_ORDER_EXACT] == 30.0
    assert contributions[MatchReasonCode.TRACK_COUNT_EXACT] == 20.0
    assert contributions[MatchReasonCode.DURATION_CLOSE] == 10.0
    assert contributions[MatchReasonCode.DISC_EXACT] == 5.0
    assert contributions[MatchReasonCode.YEAR_EXACT] == 5.0
    assert contributions[MatchReasonCode.ARTIST_EXACT] == 3.0
    assert contributions[MatchReasonCode.LANGUAGE_SCRIPT_MATCH] == 2.0


# Disc five has 29 tracks inside a seven-disc release. Comparing against the
# whole 68-track box would incorrectly punish this correctly grouped folder.
def test_twenty_nine_track_disc_is_compared_with_one_medium_not_the_release_total() -> None:
    local = LocalReleaseEvidence(
        album_title=None,
        artists=(),
        year=None,
        disc_number=5,
        disc_total=7,
        tracks=tuple(LocalTrackEvidence(title=None, duration_seconds=None) for _ in range(29)),
    )
    media = tuple(
        ReleaseMedium(
            medium_number=number,
            title=None,
            tracks=tuple(make_provider_track(track, None, duration_seconds=None) for track in range(1, count + 1)),
        )
        for number, count in enumerate((4, 5, 6, 7, 29, 8, 9), start=1)
    )
    candidate = make_candidate("seven-disc", media, date=None)

    ranking = rank_release_candidates(local, (candidate,))

    assert ranking.entries[0].medium.medium_number == 5
    assert MatchReasonCode.TRACK_COUNT_EXACT in evidence_codes(ranking.entries[0].result)
    count_evidence = next(
        item
        for item in ranking.entries[0].result.evidence
        if item.code == MatchReasonCode.TRACK_COUNT_EXACT
    )
    assert "29 local and 29 selected-medium tracks" in count_evidence.detail
    assert "68" not in count_evidence.detail
    assert ranking.entries[0].result.score == 100.0


# The raw 1999 values are marked missing/unreadable, so they cannot conflict
# with 2035. Removing the year weight from both sides gives 95 / 95 = 100.
def test_missing_local_year_is_unknown_and_does_not_reduce_the_weighted_average() -> None:
    local_files = (
        make_local_file(1, title="Opening", date="1999", date_state=FieldReadState.MISSING),
        make_local_file(2, title="Finale", date="1999", date_state=FieldReadState.UNREADABLE),
    )
    medium = ReleaseMedium(
        medium_number=1,
        title=None,
        tracks=(make_provider_track(1, "Opening"), make_provider_track(2, "Finale")),
    )
    candidate = make_candidate("unknown-local-year", (medium,), date="2035-01-01")

    result = score_release_medium(build_local_release_evidence(make_group(*local_files)), candidate, medium)

    assert result.score == 100.0
    assert result.classification is MatchClassification.HIGH
    assert MatchReasonCode.YEAR_CONTRADICTION not in evidence_codes(result)
    assert MatchReasonCode.YEAR_UNAVAILABLE in evidence_codes(result)


# Here the year is readable and really conflicts: the score is 95 / 100.
# That high number still requires review because contradiction is a separate
# confidence guard, rather than merely a five-point deduction.
def test_strong_year_contradiction_keeps_high_raw_score_in_review() -> None:
    local_files = (
        make_local_file(1, title="Opening", date="2024"),
        make_local_file(2, title="Finale", date="2024"),
    )
    medium = ReleaseMedium(
        medium_number=1,
        title=None,
        tracks=(make_provider_track(1, "Opening"), make_provider_track(2, "Finale")),
    )
    candidate = make_candidate("wrong-year", (medium,), date="1994-01-01")

    result = score_release_medium(build_local_release_evidence(make_group(*local_files)), candidate, medium)

    assert result.score == 95.0
    assert result.classification is MatchClassification.REVIEW
    assert MatchReasonCode.YEAR_CONTRADICTION in evidence_codes(result)


# Three local tracks versus four provider tracks earns 20 * 3/4 = 15
# count points. The resulting score 95 remains REVIEW because the known
# count mismatch could mean a different edition or an incomplete folder.
def test_wrong_selected_medium_track_count_is_a_strong_contradiction_and_review_cap() -> None:
    local_files = tuple(
        make_local_file(number, title=title)
        for number, title in enumerate(("Opening", "Journey", "Finale"), start=1)
    )
    medium = ReleaseMedium(
        medium_number=1,
        title=None,
        tracks=(
            make_provider_track(1, "Opening"),
            make_provider_track(2, "Journey"),
            make_provider_track(3, "Finale"),
            make_provider_track(4, "Bonus"),
        ),
    )
    candidate = make_candidate("bonus-track", (medium,))

    result = score_release_medium(build_local_release_evidence(make_group(*local_files)), candidate, medium)

    assert result.score == 95.0
    assert result.classification is MatchClassification.REVIEW
    assert MatchReasonCode.TRACK_COUNT_CONTRADICTION in evidence_codes(result)


def test_japanese_script_metadata_is_positive_language_evidence() -> None:
    local = LocalReleaseEvidence(
        album_title="風のタクト サウンドトラック",
        artists=(),
        year=None,
        disc_number=None,
        disc_total=None,
        tracks=(LocalTrackEvidence(title="大海原のテーマ", duration_seconds=None),),
    )
    medium = ReleaseMedium(
        medium_number=1,
        title=None,
        tracks=(
            make_provider_track(
                1,
                "大海原のテーマ",
                duration_seconds=None,
                language="ja",
                script="Jpan",
            ),
        ),
    )
    candidate = make_candidate(
        "japanese",
        (medium,),
        title="風のタクト サウンドトラック",
        artist="",
        date=None,
        language="ja",
        script="Jpan",
    )

    result = score_release_medium(local, candidate, medium)

    language = next(
        item for item in result.evidence if item.code == MatchReasonCode.LANGUAGE_SCRIPT_MATCH
    )
    assert language.contribution == 2.0
    assert "Japanese" in language.detail


# The default ambiguity margin includes exactly five points. Both 100 and
# 95 are plausible, so stable ranking must still leave their choice open.
def test_two_plausible_releases_at_inclusive_five_point_margin_are_ambiguous() -> None:
    local_files = (
        make_local_file(1, title="Opening"),
        make_local_file(2, title="Finale"),
    )
    medium = ReleaseMedium(
        medium_number=1,
        title=None,
        tracks=(make_provider_track(1, "Opening"), make_provider_track(2, "Finale")),
    )
    exact = make_candidate("release-a", (medium,))
    year_variant = make_candidate("release-b", (medium,), date="1994")

    ranking = rank_release_candidates(
        build_local_release_evidence(make_group(*local_files)),
        (year_variant, exact),
    )

    assert ranking.ambiguous
    assert [entry.release.release_id for entry in ranking.entries] == ["release-a", "release-b"]
    assert ranking.entries[0].result.score == 100.0
    assert ranking.entries[1].result.score == 95.0
    assert all(entry.result.classification is MatchClassification.REVIEW for entry in ranking.entries[:2])
    assert all(
        MatchReasonCode.AMBIGUOUS_TOP_CANDIDATES in evidence_codes(entry.result)
        for entry in ranking.entries[:2]
    )


def test_no_available_evidence_is_insufficient_and_low() -> None:
    local = LocalReleaseEvidence(
        album_title=None,
        artists=(),
        year=None,
        disc_number=None,
        disc_total=None,
        tracks=(),
    )
    medium = ReleaseMedium(medium_number=None, title=None, tracks=())
    candidate = make_candidate("empty", (medium,), title="", artist="", date=None, language=None, script=None)

    result = score_release_medium(local, candidate, medium)

    assert result.score == 0.0
    assert result.classification is MatchClassification.LOW
    assert MatchReasonCode.INSUFFICIENT_EVIDENCE in evidence_codes(result)


# Reverse provider arrival order while preserving the same candidates.
# Source identity and medium position must determine equal-score order.
def test_equal_scores_have_deterministic_identity_and_medium_order() -> None:
    local = LocalReleaseEvidence(
        album_title=None,
        artists=(),
        year=None,
        disc_number=None,
        disc_total=None,
        tracks=(LocalTrackEvidence(title=None, duration_seconds=None),),
    )
    medium_two = ReleaseMedium(medium_number=2, title=None, tracks=(make_provider_track(1, None),))
    medium_one = ReleaseMedium(medium_number=1, title=None, tracks=(make_provider_track(1, None),))
    release_z = make_candidate("z-release", (medium_two, medium_one), source_id="source-z", date=None)
    release_a = make_candidate("a-release", (medium_one,), source_id="source-a", date=None)

    forwards = rank_release_candidates(local, (release_z, release_a))
    backwards = rank_release_candidates(local, (release_a, release_z))

    expected = (
        ("test-engine", "source-a", "a-release", 0),
        ("test-engine", "source-z", "z-release", 1),
        ("test-engine", "source-z", "z-release", 0),
    )
    assert forwards.identities == expected
    assert backwards.identities == expected


def test_local_builder_uses_present_tags_then_per_file_filename_fallback_and_exposes_conflicts() -> None:
    tagged = make_local_file(
        1,
        title="Tagged title",
        filename_title="Ignored filename title",
        album="Album A",
        date="2024",
        disc_number=2,
        filename_disc=7,
    )
    fallback = make_local_file(
        2,
        title="Stale title",
        title_state=FieldReadState.UNREADABLE,
        filename_title="Filename fallback",
        album="Album B",
        date="2025",
        disc_number=8,
        disc_state=FieldReadState.UNREADABLE,
        filename_disc=2,
    )

    evidence = build_local_release_evidence(make_group(tagged, fallback))

    assert evidence.album_title is None
    assert evidence.year is None
    assert evidence.disc_number == 2
    assert tuple(track.title for track in evidence.tracks) == ("Tagged title", "Filename fallback")
    assert {notice.code for notice in evidence.notices} >= {
        MatchReasonCode.LOCAL_ALBUM_CONFLICT,
        MatchReasonCode.LOCAL_YEAR_CONFLICT,
    }


# A high average over a few title pairs cannot establish the whole album.
# This guards the coverage requirement independently from raw agreement.
def test_low_track_title_coverage_prevents_an_unsupported_high_classification() -> None:
    local = LocalReleaseEvidence(
        album_title="Example Album",
        artists=("Example Artist",),
        year=2024,
        disc_number=1,
        disc_total=1,
        tracks=(
            LocalTrackEvidence(title="Opening", duration_seconds=180.0),
            LocalTrackEvidence(title=None, duration_seconds=180.0),
            LocalTrackEvidence(title=None, duration_seconds=180.0),
        ),
    )
    medium = ReleaseMedium(
        medium_number=1,
        title=None,
        tracks=(
            make_provider_track(1, "Opening"),
            make_provider_track(2, None),
            make_provider_track(3, None),
        ),
    )
    candidate = make_candidate("thin-title-evidence", (medium,))

    result = score_release_medium(local, candidate, medium)

    assert result.score == 100.0
    assert result.classification is MatchClassification.REVIEW
    assert MatchReasonCode.TRACK_TITLE_COVERAGE_INSUFFICIENT in evidence_codes(result)


def test_large_duration_mismatch_is_structured_strong_contradiction() -> None:
    local_files = (
        make_local_file(1, title="Opening", duration_seconds=180.0),
        make_local_file(2, title="Finale", duration_seconds=180.0),
    )
    medium = ReleaseMedium(
        medium_number=1,
        title=None,
        tracks=(
            make_provider_track(1, "Opening", duration_seconds=180.0),
            make_provider_track(2, "Finale", duration_seconds=220.0),
        ),
    )
    candidate = make_candidate("duration-conflict", (medium,))

    result = score_release_medium(build_local_release_evidence(make_group(*local_files)), candidate, medium)

    assert result.score == 95.0
    assert result.classification is MatchClassification.REVIEW
    assert MatchReasonCode.DURATION_LARGE_MISMATCH in evidence_codes(result)


def test_scoring_results_defensively_copy_sequences_and_are_frozen() -> None:
    tracks = [LocalTrackEvidence(title="Opening", duration_seconds=180.0)]
    local = LocalReleaseEvidence(
        album_title="Album",
        artists=["Artist"],  # type: ignore[arg-type]
        year=2024,
        disc_number=1,
        disc_total=1,
        tracks=tracks,  # type: ignore[arg-type]
    )
    score = ReleaseScore(score=100.0, classification=MatchClassification.HIGH, evidence=[])

    tracks.clear()

    assert local.artists == ("Artist",)
    assert len(local.tracks) == 1
    assert score.evidence == ()

    with pytest.raises(FrozenInstanceError):
        score.score = 0.0


def test_conflicting_strong_local_release_evidence_caps_high_score_to_review() -> None:
    local_files = (
        make_local_file(1, title="Opening", album="Album A"),
        make_local_file(2, title="Finale", album="Album B"),
    )
    medium = ReleaseMedium(
        medium_number=1,
        title=None,
        tracks=(make_provider_track(1, "Opening"), make_provider_track(2, "Finale")),
    )
    candidate = make_candidate("local-conflict", (medium,))

    result = score_release_medium(build_local_release_evidence(make_group(*local_files)), candidate, medium)

    assert result.score == 100.0
    assert result.classification is MatchClassification.REVIEW
    assert MatchReasonCode.LOCAL_ALBUM_CONFLICT in evidence_codes(result)


def test_ambiguity_requires_the_second_result_to_be_plausible() -> None:
    local_files = (
        make_local_file(1, title="Opening"),
        make_local_file(2, title="Finale"),
    )
    medium = ReleaseMedium(
        medium_number=1,
        title=None,
        tracks=(make_provider_track(1, "Opening"), make_provider_track(2, "Finale")),
    )
    policy = MatchingPolicy(
        classification=ClassificationThresholds(
            high=100.0,
            review=96.0,
            ambiguity_margin=5.0,
            minimum_high_track_title_coverage=0.60,
        )
    )
    exact = make_candidate("release-a", (medium,))
    below_plausible = make_candidate("release-b", (medium,), date="1994")

    ranking = rank_release_candidates(
        build_local_release_evidence(make_group(*local_files)),
        (below_plausible, exact),
        policy,
    )

    assert not ranking.ambiguous
    assert ranking.entries[0].result.classification is MatchClassification.HIGH
    assert ranking.entries[1].result.score == 95.0
    assert ranking.entries[1].result.classification is MatchClassification.LOW


# A filename hint is weaker than an existing disc tag. Its disagreement
# should remain explainable without imposing the strong-tag review cap.
def test_filename_only_disc_mismatch_is_visible_but_does_not_cap_high_score() -> None:
    local_files = (
        make_local_file(
            1,
            title="Opening",
            disc_number=None,
            disc_total=None,
            disc_state=FieldReadState.MISSING,
            filename_disc=2,
        ),
        make_local_file(
            2,
            title="Finale",
            disc_number=None,
            disc_total=None,
            disc_state=FieldReadState.MISSING,
            filename_disc=2,
        ),
    )
    medium = ReleaseMedium(
        medium_number=1,
        title=None,
        tracks=(make_provider_track(1, "Opening"), make_provider_track(2, "Finale")),
    )
    candidate = make_candidate("weak-disc-hint", (medium,))

    result = score_release_medium(build_local_release_evidence(make_group(*local_files)), candidate, medium)

    assert result.score == 95.0
    assert result.classification is MatchClassification.HIGH
    assert MatchReasonCode.DISC_CONTRADICTION in evidence_codes(result)


def test_present_track_numbers_define_release_scoring_order_before_paths() -> None:
    opening = make_local_file(1, title="Opening", track_number=1, duration_seconds=180.0)
    finale = make_local_file(2, title="Finale", track_number=2, duration_seconds=240.0)
    medium = ReleaseMedium(
        medium_number=1,
        title=None,
        tracks=(
            make_provider_track(1, "Opening", duration_seconds=180.0),
            make_provider_track(2, "Finale", duration_seconds=240.0),
        ),
    )
    candidate = make_candidate("tag-ordered", (medium,))

    local = build_local_release_evidence(make_group(finale, opening))
    result = score_release_medium(local, candidate, medium)

    assert tuple(track.title for track in local.tracks) == ("Opening", "Finale")
    assert result.score == 100.0
    assert result.classification is MatchClassification.HIGH


def test_any_usable_tagged_disc_tier_outranks_filename_only_values() -> None:
    tagged = make_local_file(1, title="Opening", disc_number=1, filename_disc=9)
    filename_only = make_local_file(
        2,
        title="Finale",
        disc_number=None,
        disc_total=None,
        disc_state=FieldReadState.MISSING,
        filename_disc=2,
    )

    local = build_local_release_evidence(make_group(tagged, filename_only))

    assert local.disc_number == 1
    assert MatchReasonCode.LOCAL_DISC_CONFLICT not in {notice.code for notice in local.notices}


def test_japanese_language_label_with_only_romanised_script_is_not_a_script_match() -> None:
    local = LocalReleaseEvidence(
        album_title="風の歌",
        artists=(),
        year=None,
        disc_number=None,
        disc_total=None,
        tracks=(LocalTrackEvidence(title="海", duration_seconds=None),),
    )
    medium = ReleaseMedium(
        medium_number=1,
        title=None,
        tracks=(
            make_provider_track(
                1,
                "Umi",
                duration_seconds=None,
                language="ja",
                script="Latn",
            ),
        ),
    )
    candidate = make_candidate(
        "romanised-only",
        (medium,),
        title="Kaze no Uta",
        artist="",
        date=None,
        language="ja",
        script="Latn",
    )

    result = score_release_medium(local, candidate, medium)
    language_evidence = next(
        item
        for item in result.evidence
        if item.code in {MatchReasonCode.LANGUAGE_SCRIPT_MATCH, MatchReasonCode.LANGUAGE_SCRIPT_MISMATCH}
    )

    assert language_evidence.code == MatchReasonCode.LANGUAGE_SCRIPT_MISMATCH
    assert language_evidence.contribution == 0.0


def test_artist_text_participates_in_language_script_evidence() -> None:
    local = LocalReleaseEvidence(
        album_title=None,
        artists=("さくら楽団",),
        year=None,
        disc_number=None,
        disc_total=None,
        tracks=(LocalTrackEvidence(title=None, duration_seconds=None),),
    )
    medium = ReleaseMedium(
        medium_number=1,
        title=None,
        tracks=(make_provider_track(1, None, duration_seconds=None),),
    )
    candidate = make_candidate(
        "artist-script",
        (medium,),
        title="",
        artist="さくら楽団",
        date=None,
        language=None,
        script=None,
    )

    result = score_release_medium(local, candidate, medium)

    assert MatchReasonCode.LANGUAGE_SCRIPT_MATCH in evidence_codes(result)


def test_unnumbered_media_have_distinct_stable_ranking_identities() -> None:
    local = LocalReleaseEvidence(
        album_title=None,
        artists=(),
        year=None,
        disc_number=None,
        disc_total=None,
        tracks=(),
    )
    media = (
        ReleaseMedium(medium_number=None, title="First", tracks=()),
        ReleaseMedium(medium_number=None, title="Second", tracks=()),
    )
    ranking = rank_release_candidates(local, (make_candidate("unnumbered", media, date=None),))

    assert len(set(ranking.identities)) == 2


def test_empty_ranking_still_validates_local_evidence() -> None:
    with pytest.raises(TypeError, match="local"):
        rank_release_candidates(None, ())  # type: ignore[arg-type]
