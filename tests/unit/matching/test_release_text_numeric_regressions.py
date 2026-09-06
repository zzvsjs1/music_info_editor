"""Corrected native regressions for observed script, artist, numeric and title evidence."""

import math
import wave
from dataclasses import fields, replace
from pathlib import Path

import pytest
from mutagen.id3 import TIT2
from mutagen.wave import WAVE

from metadata_polisher.domain.matching import LocalisedText, ProviderTrack, ReleaseCandidate, ReleaseMedium
from metadata_polisher.domain.media import FilenameHints, LocalMediaFile, MediaReadResult, StreamInfo
from metadata_polisher.domain.metadata import FieldReadState, MetadataField, MetadataSnapshot, Position
from metadata_polisher.formats.registry import FormatRegistry
from metadata_polisher.formats.wave import WaveAdapter
from metadata_polisher.matching.normalisation import normalise_for_matching
from metadata_polisher.matching.policy import ClassificationThresholds, MatchingPolicy, ReleaseScoringWeights
from metadata_polisher.matching.release_scoring import (
    LocalReleaseEvidence,
    LocalTrackEvidence,
    MatchClassification,
    MatchReasonCode,
    build_local_release_evidence,
    score_release_medium,
)
from metadata_polisher.matching.track_mapping import map_tracks
from metadata_polisher.providers.musicbrainz.parser import parse_release_detail
from metadata_polisher.scanner.grouping import AlbumGroup, GroupingReason
from metadata_polisher.scanner.scanner import scan_media


def local_file(
    number: int,
    *,
    title: str | None = "Glacier",
    state: FieldReadState = FieldReadState.PRESENT,
    artists: tuple[str, ...] = ("Artist",),
    filename_title: str | None = "Glacier",
    duration: float | None = 180.0,
) -> LocalMediaFile:
    states = {item: FieldReadState.MISSING for item in MetadataField}
    states.update(
        {
            MetadataField.TITLE: state,
            MetadataField.ALBUM: FieldReadState.PRESENT,
            MetadataField.ALBUM_ARTISTS: FieldReadState.PRESENT,
            MetadataField.TRACK: FieldReadState.PRESENT,
            MetadataField.DISC: FieldReadState.PRESENT,
            MetadataField.DATE: FieldReadState.PRESENT,
        }
    )

    return LocalMediaFile(
        path=Path("scratch-library") / f"{number:02d}. Glacier.wav",
        format_id="wave",
        read_result=MediaReadResult(
            metadata=MetadataSnapshot(
                title=title,
                album="Album",
                album_artists=artists,
                track=Position(number),
                disc=Position(1, 1),
                date="2024",
            ),
            field_states=states,
            stream_info=StreamInfo(duration, 48_000, 2, 16, "wave:1"),
        ),
        filename_hints=FilenameHints(probable_title=filename_title),
    )


def group(*files: LocalMediaFile) -> AlbumGroup:
    return AlbumGroup("scratch-text", files, "Album", GroupingReason.DIRECTORY_ALBUM_CONSISTENT)


def candidate(
    titles: tuple[LocalisedText, ...] = (LocalisedText("Album", "en", "Latn"),),
    track_titles: tuple[LocalisedText, ...] = (LocalisedText("Glacier", "en", "Latn"),),
    *,
    artists: tuple[str, ...] = ("Artist",),
    duration: float | None = 180.0,
) -> ReleaseCandidate:
    track = ProviderTrack(1, track_titles, artists, (), duration)
    medium = ReleaseMedium(1, None, (track,))

    return ReleaseCandidate("scratch", "native", "text", titles, artists, "2024", (medium,), None)


def language_code(local: LocalReleaseEvidence, release: ReleaseCandidate) -> str:
    result = score_release_medium(local, release, release.media[0])
    codes = {
        MatchReasonCode.LANGUAGE_SCRIPT_MATCH,
        MatchReasonCode.LANGUAGE_SCRIPT_MISMATCH,
        MatchReasonCode.LANGUAGE_SCRIPT_UNAVAILABLE,
    }

    return next(item.code for item in result.evidence if item.code in codes)


def language_local(text: str, artists: tuple[str, ...] = ()) -> LocalReleaseEvidence:
    return LocalReleaseEvidence(text, artists, None, None, None, (LocalTrackEvidence(text, None),))


@pytest.mark.parametrize(("text", "language"), [("ひかり", "ja"), ("빛나는", "ko")])
def test_r6_latin_artist_must_not_make_romanised_provider_preserve_script(text: str, language: str) -> None:
    offered = (LocalisedText("Romanised", language, "Latn"),)
    release = candidate(offered, offered, artists=())

    assert language_code(language_local(text), release) == MatchReasonCode.LANGUAGE_SCRIPT_MISMATCH
    assert language_code(language_local(text, ("Artist",)), release) == MatchReasonCode.LANGUAGE_SCRIPT_MISMATCH


@pytest.mark.parametrize(("text", "language", "script"), [("ひかり", "ja", "Jpan"), ("빛나는", "ko", "Kore")])
def test_r6_misleading_script_label_must_not_establish_observed_script(text: str, language: str, script: str) -> None:
    offered = (LocalisedText("Romanised", language, script),)
    release = candidate(offered, offered, artists=())

    assert language_code(language_local(text), release) == MatchReasonCode.LANGUAGE_SCRIPT_MISMATCH


def test_r6_misleading_script_label_reaches_scoring_through_real_provider_parser() -> None:
    release = parse_release_detail(
        {
            "id": "scratch-script-label",
            "title": "Hikari",
            "text-representation": {"language": "jpn", "script": "Jpan"},
            "media": [
                {
                    "position": 1,
                    "track-count": 1,
                    "tracks": [{"position": 1, "number": "1", "title": "Hikari"}],
                }
            ],
        }
    )

    assert release.titles[0] == LocalisedText("Hikari", "jpn", "Jpan")
    assert language_code(language_local("ひかり"), release) == MatchReasonCode.LANGUAGE_SCRIPT_MISMATCH


@pytest.mark.parametrize(("text", "language", "script"), [("ひかり", "ja", "Jpan"), ("빛나는", "ko", "Kore")])
def test_r6_observed_genuine_script_variants_are_available_even_among_mixed_aliases(
    text: str, language: str, script: str,
) -> None:
    offered = (
        LocalisedText("Romanised", language, "Latn"),
        LocalisedText(text, language, script),
        LocalisedText("한글" if language == "ja" else "かな", None, None),
    )
    release = candidate(offered, offered, artists=())

    assert language_code(language_local(text), release) == MatchReasonCode.LANGUAGE_SCRIPT_MATCH
    assert release.titles == offered


def test_r6_observed_kana_still_supports_script_when_provider_labels_are_wrong() -> None:
    offered = (LocalisedText("ひかり", "en", "Latn"),)

    assert language_code(language_local("ひかり"), candidate(offered, offered)) == MatchReasonCode.LANGUAGE_SCRIPT_MATCH


def test_r6_han_only_local_remains_language_ambiguous_and_unscored() -> None:
    offered = (LocalisedText("光", "ja", "Hani"),)

    assert (
        language_code(language_local("光"), candidate(offered, offered))
        == MatchReasonCode.LANGUAGE_SCRIPT_UNAVAILABLE
    )


def test_r6_japanese_associated_han_preserves_japanese_without_requiring_local_han() -> None:
    # Japanese titles may consist entirely of Han. The offered variant itself
    # must associate the observed Han with Japanese, matching proposal policy.
    offered = (LocalisedText("光", "ja", "Hani"),)
    release = candidate(offered, offered, artists=())

    assert language_code(language_local("ひかり"), release) == MatchReasonCode.LANGUAGE_SCRIPT_MATCH
    assert language_code(language_local("光の歌"), release) == MatchReasonCode.LANGUAGE_SCRIPT_MATCH


@pytest.mark.parametrize("language,expected", [
    (None, MatchReasonCode.LANGUAGE_SCRIPT_MATCH),
    ("zh", MatchReasonCode.LANGUAGE_SCRIPT_MISMATCH),
])
def test_r6_script_label_can_associate_observed_han_but_cannot_override_explicit_language(language, expected):
    offered = (LocalisedText("光", language, "Jpan"),)
    release = candidate(offered, offered, artists=())

    # Jpan can interpret already-observed Han when language is unknown. It
    # cannot silently relabel an explicitly Chinese variant as Japanese text.
    assert language_code(language_local("ひかり"), release) == expected


@pytest.mark.parametrize("language", [None, "zh", "ko"])
def test_r6_unassociated_or_other_language_han_does_not_prove_japanese_preservation(language: str | None) -> None:
    offered = (LocalisedText("光", language, "Hani"),)
    romanised = LocalisedText("Hikari", "ja", "Latn")
    release = candidate((*offered, romanised), (*offered, romanised), artists=())

    # The Japanese label on the Latin alias cannot be combined with the Han
    # characters of an unrelated Chinese/Korean/unlabelled variant.
    assert language_code(language_local("ひかり"), release) == MatchReasonCode.LANGUAGE_SCRIPT_MISMATCH


def test_r6_korean_han_only_variant_is_insufficient_for_native_script_match() -> None:
    offered = (LocalisedText("光", "ko", "Kore"),)

    assert language_code(language_local("빛나는"), candidate(offered, offered, artists=())) == (
        MatchReasonCode.LANGUAGE_SCRIPT_MISMATCH
    )


@pytest.mark.parametrize("artist", ["かな", "한글"])
def test_r6_genuine_artist_script_can_preserve_corresponding_native_style(artist: str) -> None:
    local = language_local(artist)
    offered = (LocalisedText("Romanised", None, "Latn"),)

    assert language_code(local, candidate(offered, offered, artists=(artist,))) == MatchReasonCode.LANGUAGE_SCRIPT_MATCH


@pytest.mark.parametrize("second", [("A", "B"), ("B", "A")])
def test_r7_equivalent_artist_sets_must_not_conflict(second: tuple[str, ...]) -> None:
    files = (local_file(1, artists=("Ａ", "B")), local_file(2, artists=second))
    before = tuple(file.read_result.metadata for file in files)
    local = build_local_release_evidence(group(*files))

    assert tuple(file.read_result.metadata for file in files) == before
    assert MatchReasonCode.LOCAL_ARTIST_CONFLICT not in {notice.code for notice in local.notices}
    assert frozenset(normalise_for_matching(value) for value in local.artists) == {"a", "b"}


@pytest.mark.parametrize("second", [("B", "A"), ("a", "b"), ("B", "A", "A")])
def test_r7_plain_order_case_and_duplicate_changes_are_already_equivalent(second: tuple[str, ...]) -> None:
    local = build_local_release_evidence(group(local_file(1, artists=("A", "B")), local_file(2, artists=second)))

    assert MatchReasonCode.LOCAL_ARTIST_CONFLICT not in {notice.code for notice in local.notices}
    assert frozenset(normalise_for_matching(value) for value in local.artists) == {"a", "b"}


def test_r7_genuinely_different_artist_sets_remain_conflicting() -> None:
    local = build_local_release_evidence(group(local_file(1, artists=("A", "B")), local_file(2, artists=("A", "C"))))

    assert local.artists == ()
    assert MatchReasonCode.LOCAL_ARTIST_CONFLICT in {notice.code for notice in local.notices}


def scaled_policy(scale: float) -> MatchingPolicy:
    defaults = ReleaseScoringWeights()
    weights = ReleaseScoringWeights(**{item.name: getattr(defaults, item.name) * scale for item in fields(defaults)})

    return MatchingPolicy(release_weights=weights)


@pytest.mark.parametrize("scale", [1.0, 1e-6, 1e3, 1e100, 1e306, 1e-9, 5e-324, 2e306])
def test_r9_perfect_match_preserves_score_for_finite_common_weight_scale(scale: float) -> None:
    policy = scaled_policy(scale)
    assert all(math.isfinite(getattr(policy.release_weights, item.name)) for item in fields(policy.release_weights))
    release = candidate()
    result = score_release_medium(build_local_release_evidence(group(local_file(1))), release, release.media[0], policy)

    assert math.isfinite(result.score)
    assert result.score == pytest.approx(100.0, abs=1e-6)
    assert result.classification is MatchClassification.HIGH


def test_r9_equal_small_weights_must_return_valid_perfect_score() -> None:
    weights = ReleaseScoringWeights(**{item.name: 0.0000006 for item in fields(ReleaseScoringWeights)})
    release = candidate()
    result = score_release_medium(
        build_local_release_evidence(group(local_file(1))),
        release,
        release.media[0],
        MatchingPolicy(release_weights=weights),
    )

    assert result.score == pytest.approx(100.0, abs=1e-6)
    assert result.classification is MatchClassification.HIGH


@pytest.mark.parametrize("scale", [1e3, 1e100, 1e-6, 1e-9, 2e306])
def test_r9_nonperfect_score_is_scale_invariant_away_from_thresholds(scale: float) -> None:
    release = candidate((LocalisedText("Almanac", "en", "Latn"),))
    local = build_local_release_evidence(group(local_file(1)))
    baseline = score_release_medium(local, release, release.media[0])
    result = score_release_medium(local, release, release.media[0], scaled_policy(scale))

    assert result.score == pytest.approx(baseline.score, abs=1e-6)
    assert result.classification is baseline.classification


def test_r9_default_contributions_and_final_rounding_threshold_contract() -> None:
    release = candidate((LocalisedText("Almanac", "en", "Latn"),))
    local = build_local_release_evidence(group(local_file(1)))
    baseline = score_release_medium(local, release, release.media[0])

    for offset, expected in [(0.0, MatchClassification.HIGH), (0.000001, MatchClassification.REVIEW)]:
        thresholds = ClassificationThresholds(high=baseline.score + offset, review=0.0)
        result = score_release_medium(local, release, release.media[0], MatchingPolicy(classification=thresholds))

        assert result.classification is expected

    # Existing evidence is in raw policy-weight units, rounded to six decimals.
    # A fix must preserve these familiar default explanations, or document a new
    # presentation contract; the calculation cannot depend on that presentation.
    assert sum(item.contribution for item in baseline.evidence) == pytest.approx(baseline.score, abs=1e-6)


def test_r9_unavailable_huge_weight_cannot_erase_available_tiny_evidence() -> None:
    defaults = ReleaseScoringWeights()
    weights = ReleaseScoringWeights(**{item.name: 5e-324 for item in fields(defaults)})
    weights = replace(weights, album_title=1e308)
    local = replace(build_local_release_evidence(group(local_file(1))), album_title=None)
    release = candidate()
    result = score_release_medium(local, release, release.media[0], MatchingPolicy(release_weights=weights))

    assert result.score == 100.0
    assert result.classification is MatchClassification.HIGH

    # Display values legitimately round to zero in tiny raw-weight units;
    # the unrounded ratio is independently well-defined and remains perfect.
    assert all(item.contribution == 0.0 for item in result.evidence)


@pytest.fixture
def scanned_blank_title(tmp_path: Path) -> LocalMediaFile:
    path = tmp_path / "01. Glacier.wav"

    # Generate a tiny valid PCM container and persist a real ID3 whitespace title.
    # The production registry, WAVE adapter and scanner then establish reachability.
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8_000)
        output.writeframes(b"\0\0" * 800)

    audio = WAVE(path)
    audio.add_tags()
    assert audio.tags is not None
    audio.tags.add(TIT2(encoding=3, text=[" \t "]))
    audio.save()
    result = scan_media(tmp_path, FormatRegistry((WaveAdapter(),)))
    assert result.issues == ()
    assert len(result.supported_files) == 1

    return result.supported_files[0]


def test_r10_real_wave_scanner_preserves_blank_present_title(scanned_blank_title: LocalMediaFile) -> None:
    file = scanned_blank_title

    assert file.read_result.metadata.title == " \t "
    assert file.read_result.field_states[MetadataField.TITLE] is FieldReadState.PRESENT
    assert file.filename_hints.probable_title == "Glacier"
    assert build_local_release_evidence(group(file)).tracks[0].title == "Glacier"


def test_r10_mapping_must_use_same_blank_title_fallback_as_ranking(scanned_blank_title: LocalMediaFile) -> None:
    file = scanned_blank_title
    release = candidate(duration=file.read_result.stream_info.duration_seconds)
    result = map_tracks((file,), release, selected_medium_index=0)

    assert len(result.mappings) == 1
    assert MatchReasonCode.TRACK_TITLE_EXACT in result.mappings[0].reason_codes


@pytest.mark.parametrize("state", [FieldReadState.MISSING, FieldReadState.UNREADABLE, FieldReadState.UNSUPPORTED])
@pytest.mark.parametrize("filename_title", [None, "Glacier"])
def test_r10_stale_nonpresent_titles_never_supply_evidence(state: FieldReadState, filename_title: str | None) -> None:
    file = local_file(1, title="Stale incorrect tag", state=state, filename_title=filename_title)
    release = candidate()
    local = build_local_release_evidence(group(file))
    result = map_tracks((file,), release, selected_medium_index=0)
    expected = MatchReasonCode.TRACK_TITLE_EXACT if filename_title else MatchReasonCode.TRACK_TITLE_UNAVAILABLE

    assert local.tracks[0].title == filename_title
    assert len(result.mappings) == 1
    assert expected in result.mappings[0].reason_codes
    assert file.read_result.metadata.title == "Stale incorrect tag"


