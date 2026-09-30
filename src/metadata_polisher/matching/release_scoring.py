"""Explainable scoring of one local group against release-medium candidates.

Local evidence extraction and immutable result values live in separate modules.
This module chooses preliminary correspondences, calculates weighted dimensions
and ranks candidates. Coverage and contradiction rules remain separate from the
numerical ranking ratio; the track mapper makes the final file-to-track choices.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass, replace

from rapidfuzz.fuzz import ratio, token_set_ratio

from metadata_polisher.domain.matching import LocalisedText, ProviderTrack, ReleaseCandidate, ReleaseMedium
from metadata_polisher.matching.language import Language, Script, build_language_profile
from metadata_polisher.matching.local_release_evidence import (
    LocalEvidenceResult,
    OrderedLocalTracks,
    _clean_text,
    _deterministic_unique,
    _parse_year,
    _positive_integer,
    build_local_release_evidence,
    order_local_track_files,
)
from metadata_polisher.matching.normalisation import normalise_for_matching
from metadata_polisher.matching.policy import DEFAULT_MATCHING_POLICY, SCORE_DECIMAL_PLACES, MatchingPolicy
from metadata_polisher.matching.release_models import (
    DimensionResult,
    LocalEvidenceNotice,
    LocalEvidenceSource,
    LocalReleaseEvidence,
    LocalTrackEvidence,
    MatchClassification,
    MatchEvidence,
    MatchReasonCode,
    RankedReleaseMedium,
    ReleaseRanking,
    ReleaseScore,
    TrackPair,
    TrackTitleResult,
    WeightedDimension,
    _typed_tuple,
)

# Explicit re-exports preserve existing callers while the smaller modules keep
# extraction, data contracts and scoring separately readable. They refer to the
# same classes and functions; there are no duplicated compatibility wrappers.
__all__ = (
    "LocalEvidenceNotice",
    "LocalEvidenceResult",
    "LocalEvidenceSource",
    "LocalReleaseEvidence",
    "LocalTrackEvidence",
    "MatchClassification",
    "MatchEvidence",
    "MatchReasonCode",
    "OrderedLocalTracks",
    "RankedReleaseMedium",
    "ReleaseRanking",
    "ReleaseScore",
    "build_local_release_evidence",
    "order_local_track_files",
    "rank_release_candidates",
    "score_release_medium",
)

_STRONG_LOCAL_CONFLICTS = frozenset(
    {
        MatchReasonCode.LOCAL_ALBUM_CONFLICT,
        MatchReasonCode.LOCAL_ARTIST_CONFLICT,
        MatchReasonCode.LOCAL_YEAR_CONFLICT,
        MatchReasonCode.LOCAL_DISC_CONFLICT,
        MatchReasonCode.LOCAL_DISC_TOTAL_CONFLICT,
    }
)


@dataclass(frozen=True)
class _ScoringState:
    """Calculated ratio components plus separately prepared public evidence.

    Totals use a common normalised weight scale. Evidence contributions retain
    the raw policy's units for display; they must not be summed back into a score.
    """

    evidence: tuple[MatchEvidence, ...]
    weighted_total: float
    available_weight: float
    strong_contradiction: bool = False
    track_title_coverage: float = 0.0


def _evidence(code: str, contribution: float, detail: str) -> MatchEvidence:
    # Internal dimensions retain their unrounded agreement. The score state
    # prepares rounded raw-weight display contributions only after calculation.
    return MatchEvidence(code=code, contribution=contribution, detail=detail)


def _text_similarity(left: str, right: str) -> float:
    return ratio(normalise_for_matching(left), normalise_for_matching(right)) / 100.0


def _album_dimension(
    local: LocalReleaseEvidence,
    release: ReleaseCandidate,
    weight: float,
) -> DimensionResult:
    # A provider may offer several original title variants. The strongest
    # comparison supplies this one dimension; variants do not add extra weight.
    provider_titles = tuple(title.value for title in release.titles if _clean_text(title.value) is not None)

    if local.album_title is None or not provider_titles:
        return DimensionResult(
            evidence=_evidence(
                MatchReasonCode.ALBUM_TITLE_UNAVAILABLE,
                0.0,
                "Album-title evidence is missing or conflicted on one side.",
            ),
            available=False,
            strong_contradiction=False,
        )

    similarity = max(_text_similarity(local.album_title, title) for title in provider_titles)
    code = (
        MatchReasonCode.ALBUM_TITLE_EXACT
        if similarity == 1.0
        else MatchReasonCode.ALBUM_TITLE_SIMILARITY
    )

    return DimensionResult(
        evidence=_evidence(code, weight * similarity, f"Best album-title similarity is {similarity * 100:.1f}%."),
        available=bool(weight),
        strong_contradiction=False,
    )


def _best_provider_title_similarity(local_title: str, titles: tuple[LocalisedText, ...]) -> float | None:
    usable_titles = tuple(title.value for title in titles if _clean_text(title.value) is not None)

    if not usable_titles:
        return None

    return max(_text_similarity(local_title, title) for title in usable_titles)


@dataclass(frozen=True)
class _TrackComparison:
    """One reusable preliminary pairing, distinct from final track assignment."""

    pairs: tuple[TrackPair, ...]
    uses_numbers: bool = False
    has_unpaired_tracks: bool = False


def _has_complete_unique_numbers(numbers: tuple[int | None, ...]) -> bool:
    """Require positive, unique source numbers throughout one sequence."""
    if not numbers or any(number is None for number in numbers):
        return False

    # The caller obtains provider numbers through the source's supported
    # numbering contract, so pregaps and non-numeric printed labels are absent.
    return len(set(numbers)) == len(numbers)


def _number_pair_has_content_support(
    local_track: LocalTrackEvidence,
    provider_track: ProviderTrack,
    policy: MatchingPolicy,
) -> bool:
    """Check independent content before allowing numbers to repair an offset.

    A usable title comparison takes precedence: equal/common durations must not
    rescue numbering that associates visibly different titles. A strong title
    may still establish correspondence when duration conflicts; the separate
    duration dimension must then report that real contradiction for review.
    """
    if local_track.title is not None:
        similarity = _best_provider_title_similarity(local_track.title, provider_track.titles)

        if similarity is not None:
            return similarity >= policy.track_mapping.minimum_content_title_similarity

    if local_track.duration_seconds is None or provider_track.duration_seconds is None:
        return False

    delta = abs(local_track.duration_seconds - provider_track.duration_seconds)

    return delta <= policy.track_mapping.close_duration_seconds


def _preliminary_track_comparison(
    local: LocalReleaseEvidence,
    medium: ReleaseMedium,
    policy: MatchingPolicy,
) -> _TrackComparison:
    """Use corroborated source numbers for partial albums; otherwise keep order.

    This is deliberately narrower than gap-aware mapping. For example, local
    tracks 2..10 should compare with provider tracks 2..10, rather than 1..9.
    Every shared number must be content-supported and the pairing must preserve
    order. Unknown, mixed, duplicated or misleading numbering leaves the
    existing positional result recoverable for later mapping and human review.
    """
    positional = _TrackComparison(
        pairs=tuple(
            TrackPair(local=local_track, provider=provider_track)
            for local_track, provider_track in zip(local.tracks, medium.tracks, strict=False)
        ),
        has_unpaired_tracks=len(local.tracks) != len(medium.tracks),
    )
    local_numbers = tuple(track.track_number for track in local.tracks)
    provider_numbers = tuple(
        _positive_integer(track.track_number) if track.supports_automatic_numbering else None
        for track in medium.tracks
    )

    if not _has_complete_unique_numbers(local_numbers):
        return positional

    if not _has_complete_unique_numbers(provider_numbers):
        return positional

    source_tiers = {track.track_number_source for track in local.tracks}

    if None in source_tiers or len(source_tiers) != 1:
        return positional

    provider_by_number = {
        number: index
        for index, number in enumerate(provider_numbers)
        if number is not None
    }
    index_pairs = tuple(
        (local_index, provider_by_number[number])
        for local_index, number in enumerate(local_numbers)
        if number is not None and number in provider_by_number
    )

    if not index_pairs:
        return positional

    # A numerical match cannot authorise a crossing assignment. The selected
    # provider medium's documented sequence remains the order constraint.
    provider_indexes = tuple(provider_index for _local_index, provider_index in index_pairs)

    if provider_indexes != tuple(sorted(provider_indexes)):
        return positional

    pairs = tuple(
        TrackPair(local=local.tracks[local_index], provider=medium.tracks[provider_index])
        for local_index, provider_index in index_pairs
    )

    if not all(_number_pair_has_content_support(pair.local, pair.provider, policy) for pair in pairs):
        return positional

    positional_indexes = tuple((index, index) for index in range(len(positional.pairs)))

    if index_pairs == positional_indexes:
        # Keep the established ordered-title reason when numbering changes no
        # comparison. Number-specific reasons describe an actual repaired gap.
        return positional

    sequence_length = max(len(local.tracks), len(medium.tracks))

    return _TrackComparison(
        pairs=pairs,
        uses_numbers=True,
        has_unpaired_tracks=len(pairs) < sequence_length,
    )


def _track_title_dimension(
    local: LocalReleaseEvidence,
    medium: ReleaseMedium,
    weight: float,
    comparison: _TrackComparison,
) -> TrackTitleResult:
    similarities: list[float] = []

    # Title and duration inspect the same preliminary pairs. Otherwise one
    # dimension could reward a repaired gap while another invents a conflict.
    for pair in comparison.pairs:
        if pair.local.title is None:
            continue

        similarity = _best_provider_title_similarity(pair.local.title, pair.provider.titles)

        if similarity is not None:
            similarities.append(similarity)

    # Coverage measures how much of the larger sequence had usable title
    # pairs. Three exact pairs out of ten yield perfect title agreement but
    # only 0.3 coverage, which is too little to justify HIGH confidence.
    sequence_length = max(len(local.tracks), len(medium.tracks))
    coverage = len(similarities) / sequence_length if sequence_length else 0.0

    if not similarities:
        return TrackTitleResult(
            dimension=DimensionResult(
                evidence=_evidence(
                    MatchReasonCode.TRACK_TITLE_ORDER_UNAVAILABLE,
                    0.0,
                    "No local/provider title pairs are available in the preliminary correspondence.",
                ),
                available=False,
                strong_contradiction=False,
            ),
            coverage=coverage,
        )

    agreement = sum(similarities) / len(similarities)

    if comparison.uses_numbers:
        exact_code = MatchReasonCode.TRACK_TITLE_NUMBER_EXACT
        similar_code = MatchReasonCode.TRACK_TITLE_NUMBER_AGREEMENT
        method = "Content-supported source-number"
    else:
        exact_code = MatchReasonCode.TRACK_TITLE_ORDER_EXACT
        similar_code = MatchReasonCode.TRACK_TITLE_ORDER_AGREEMENT
        method = "Ordered"

    code = exact_code if agreement == 1.0 else similar_code

    return TrackTitleResult(
        dimension=DimensionResult(
            evidence=_evidence(
                code,
                weight * agreement,
                (
                    f"{method} title agreement is {agreement * 100:.1f}% across "
                    f"{len(similarities)}/{sequence_length} positions."
                ),
            ),
            available=bool(weight),
            strong_contradiction=False,
        ),
        coverage=coverage,
    )


def _track_count_dimension(
    local: LocalReleaseEvidence,
    medium: ReleaseMedium,
    weight: float,
) -> DimensionResult:
    local_count = len(local.tracks)
    # A visible row count cannot establish an exact match when the provider
    # supplied only part of its listing or has unsupported count semantics.
    provider_count = medium.track_total

    if local_count == 0 or provider_count is None:
        return DimensionResult(
            evidence=_evidence(
                MatchReasonCode.TRACK_COUNT_UNAVAILABLE,
                0.0,
                "A complete local/provider count pair is unavailable for selected-medium comparison.",
            ),
            available=False,
            strong_contradiction=False,
        )

    if local_count == provider_count:
        return DimensionResult(
            evidence=_evidence(
                MatchReasonCode.TRACK_COUNT_EXACT,
                weight,
                f"Track count agrees: {local_count} local and {provider_count} selected-medium tracks.",
            ),
            available=bool(weight),
            strong_contradiction=False,
        )

    # Use the smaller count divided by the larger so either missing or extra
    # tracks reduce agreement symmetrically. The explicit contradiction flag
    # also prevents a near count from yielding an unsupported HIGH result.
    largest_count = max(local_count, provider_count)
    similarity = min(local_count, provider_count) / largest_count if largest_count else 0.0

    return DimensionResult(
        evidence=_evidence(
            MatchReasonCode.TRACK_COUNT_CONTRADICTION,
            weight * similarity,
            f"Track count conflicts: {local_count} local and {provider_count} selected-medium tracks.",
        ),
        available=bool(weight),
        strong_contradiction=True,
    )


def _duration_similarity(delta: float, policy: MatchingPolicy) -> float:
    close = policy.track_mapping.close_duration_seconds
    large = policy.track_mapping.large_duration_mismatch_seconds

    if delta <= close:
        return 1.0

    if delta >= large:
        return 0.0

    # Scale the gap between the tolerant and contradictory bounds onto 1..0.
    # At the midpoint between the default 3 s and 10 s limits, similarity is 0.5.
    return 1.0 - ((delta - close) / (large - close))


def _duration_dimension(
    comparison: _TrackComparison,
    weight: float,
    policy: MatchingPolicy,
) -> DimensionResult:
    deltas = tuple(
        abs(pair.local.duration_seconds - pair.provider.duration_seconds)
        for pair in comparison.pairs
        if pair.local.duration_seconds is not None and pair.provider.duration_seconds is not None
    )

    if not deltas:
        return DimensionResult(
            evidence=_evidence(
                MatchReasonCode.DURATION_UNAVAILABLE,
                0.0,
                "No local/provider duration pairs are available in the preliminary correspondence.",
            ),
            available=False,
            strong_contradiction=False,
        )

    # Average only known duration pairs, but inspect the worst difference
    # separately. Many close tracks must not hide one strong duration conflict.
    similarity = sum(_duration_similarity(delta, policy) for delta in deltas) / len(deltas)
    largest_delta = max(deltas)
    has_large_mismatch = largest_delta >= policy.track_mapping.large_duration_mismatch_seconds

    if has_large_mismatch:
        code = MatchReasonCode.DURATION_LARGE_MISMATCH
    elif largest_delta <= policy.track_mapping.close_duration_seconds:
        code = MatchReasonCode.DURATION_CLOSE
    else:
        code = MatchReasonCode.DURATION_AGREEMENT

    return DimensionResult(
        evidence=_evidence(
            code,
            weight * similarity,
            f"Duration agreement uses {len(deltas)} pairs; largest difference is {largest_delta:.3f} seconds.",
        ),
        available=bool(weight),
        strong_contradiction=has_large_mismatch,
    )


def _disc_dimension(
    local: LocalReleaseEvidence,
    release: ReleaseCandidate,
    medium: ReleaseMedium,
    weight: float,
) -> DimensionResult:
    # Disc number and disc total are separate comparisons within one weight.
    # If both are known and only one agrees, this dimension earns half its
    # weight. Filename-only disc disagreement remains weaker than a real tag.
    comparisons: list[bool] = []
    descriptions: list[str] = []
    strong_contradiction = False

    if local.disc_number is not None and medium.medium_number is not None:
        disc_number_matches = local.disc_number == medium.medium_number
        disc_number_source = local.disc_number_source or LocalEvidenceSource.TAG
        comparisons.append(disc_number_matches)
        descriptions.append(
            f"disc {local.disc_number}/{medium.medium_number} ({disc_number_source.value})"
        )
        strong_contradiction = (
            not disc_number_matches
            and disc_number_source
            in {LocalEvidenceSource.TAG, LocalEvidenceSource.MANUAL_OVERRIDE}
        )

    provider_disc_total = release.disc_total

    if local.disc_total is not None and provider_disc_total is not None:
        disc_total_matches = local.disc_total == provider_disc_total
        comparisons.append(disc_total_matches)
        descriptions.append(f"disc total {local.disc_total}/{provider_disc_total}")
        strong_contradiction = strong_contradiction or not disc_total_matches

    if not comparisons:
        return DimensionResult(
            evidence=_evidence(
                MatchReasonCode.DISC_UNAVAILABLE,
                0.0,
                "Disc number/count evidence is unavailable on one side.",
            ),
            available=False,
            strong_contradiction=False,
        )

    agreement = sum(comparisons) / len(comparisons)
    exact = all(comparisons)

    return DimensionResult(
        evidence=_evidence(
            MatchReasonCode.DISC_EXACT if exact else MatchReasonCode.DISC_CONTRADICTION,
            weight * agreement,
            "Disc comparison: " + ", ".join(descriptions) + ".",
        ),
        available=bool(weight),
        strong_contradiction=strong_contradiction,
    )


def _year_dimension(
    local: LocalReleaseEvidence,
    release: ReleaseCandidate,
    weight: float,
) -> DimensionResult:
    provider_year = _parse_year(release.date)

    if local.year is None or provider_year is None:
        return DimensionResult(
            evidence=_evidence(
                MatchReasonCode.YEAR_UNAVAILABLE,
                0.0,
                "A valid local/provider year pair is unavailable.",
            ),
            available=False,
            strong_contradiction=False,
        )

    difference = abs(local.year - provider_year)

    if difference == 0:
        return DimensionResult(
            evidence=_evidence(
                MatchReasonCode.YEAR_EXACT,
                weight,
                f"Release years agree at {local.year}.",
            ),
            available=bool(weight),
            strong_contradiction=False,
        )

    # A one-year difference can occur between release editions, so retain half
    # the year weight. Wider differences contribute zero and cap confidence.
    if difference == 1:
        return DimensionResult(
            evidence=_evidence(
                MatchReasonCode.YEAR_NEAR,
                weight * 0.5,
                f"Release years differ by one ({local.year} versus {provider_year}).",
            ),
            available=bool(weight),
            strong_contradiction=False,
        )

    return DimensionResult(
        evidence=_evidence(
            MatchReasonCode.YEAR_CONTRADICTION,
            0.0,
            f"Release years strongly conflict ({local.year} versus {provider_year}).",
        ),
        available=bool(weight),
        strong_contradiction=True,
    )


def _artist_dimension(
    local: LocalReleaseEvidence,
    release: ReleaseCandidate,
    weight: float,
) -> DimensionResult:
    provider_artists = _deterministic_unique(release.album_artists)

    if not local.artists or not provider_artists:
        return DimensionResult(
            evidence=_evidence(
                MatchReasonCode.ARTIST_UNAVAILABLE,
                0.0,
                "Album-artist evidence is unavailable on one side.",
            ),
            available=False,
            strong_contradiction=False,
        )

    local_text = " ; ".join(normalise_for_matching(artist) for artist in local.artists)
    provider_text = " ; ".join(normalise_for_matching(artist) for artist in provider_artists)
    similarity = token_set_ratio(local_text, provider_text) / 100.0
    exact = set(normalise_for_matching(artist) for artist in local.artists) == set(
        normalise_for_matching(artist) for artist in provider_artists
    )

    # A fuzzy value helps ranking minor spelling variants; exact set agreement is
    # retained as its own reason so a reviewer can distinguish the two situations.
    return DimensionResult(
        evidence=_evidence(
            MatchReasonCode.ARTIST_EXACT if exact else MatchReasonCode.ARTIST_SIMILARITY,
            weight * similarity,
            f"Album-artist similarity is {similarity * 100:.1f}%.",
        ),
        available=bool(weight),
        strong_contradiction=False,
    )


def _normalised_language(value: str | None) -> Language | None:
    if value is None:
        return None

    primary = value.strip().casefold().split("-", maxsplit=1)[0]

    if primary in {"ja", "jpn"}:
        return Language.JAPANESE

    if primary in {"ko", "kor"}:
        return Language.KOREAN

    return None


def _observed_scripts(text: str) -> frozenset[Script]:
    """Read actual characters; provider labels never add observed script."""
    return frozenset(item.script for item in build_language_profile((text,)).script_evidence)


def _variant_preserves_language(title: LocalisedText, language: Language) -> bool:
    """Evaluate native script and its language association within one variant.

    Kana and Hangul are direct native-language evidence even when provider
    labels are wrong. Han is shared among languages: Japanese association must
    belong to this same Han variant, not a romanised alias elsewhere. A Japanese
    script label may provide that association, but never manufacture characters.
    Korean Han alone does not meet the current Hangul-preservation contract.
    """
    scripts = _observed_scripts(title.value)

    if language is Language.KOREAN:
        return Script.HANGUL in scripts

    if Script.KANA in scripts:
        return True

    if Script.HAN not in scripts:
        return False

    declared_language = _normalised_language(title.language)

    if declared_language is Language.JAPANESE:
        return True

    # Only an absent language may defer to the script association. Explicit
    # non-Japanese language metadata must not be reassigned through another label.
    if title.language is not None:
        return False

    script_label = title.script.strip().casefold() if title.script is not None else None

    return script_label in {"jpan", "japanese"}


def _language_dimension(
    local: LocalReleaseEvidence,
    release: ReleaseCandidate,
    medium: ReleaseMedium,
    weight: float,
) -> DimensionResult:
    # Infer local preference from its actual text, including artists, without
    # inferring Japanese or Chinese from Han alone. This is an offered-variant
    # preference for ranking; the proposal layer separately chooses written text.
    local_texts = (
        local.album_title,
        *local.artists,
        *(track.title for track in local.tracks),
    )
    local_profile = build_language_profile(local_texts)

    if local_profile.ambiguous or not local_profile.script_evidence:
        return DimensionResult(
            evidence=_evidence(
                MatchReasonCode.LANGUAGE_SCRIPT_UNAVAILABLE,
                0.0,
                "Local language/script evidence is absent or ambiguous.",
            ),
            available=False,
            strong_contradiction=False,
        )

    # Keep variants separate for the native-language decision. Combining the
    # Japanese label of a Latin alias with unrelated Han text would claim a
    # Japanese representation which no single offered variant actually supplies.
    provider_variants = (
        *release.titles,
        *(title for track in medium.tracks for title in track.titles),
        *(LocalisedText(artist, None, None) for artist in release.album_artists),
        *(LocalisedText(artist, None, None) for track in medium.tracks for artist in track.artists),
    )
    provider_scripts = frozenset(
        script
        for variant in provider_variants
        for script in _observed_scripts(variant.value)
    )

    if not provider_scripts:
        return DimensionResult(
            evidence=_evidence(
                MatchReasonCode.LANGUAGE_SCRIPT_UNAVAILABLE,
                0.0,
                "Provider text has no observed script evidence.",
            ),
            available=False,
            strong_contradiction=False,
        )

    preferred_language = local_profile.preferred_language

    if preferred_language is not None:
        matches = any(
            _variant_preserves_language(variant, preferred_language)
            for variant in provider_variants
        )
        language_label = "Japanese" if preferred_language is Language.JAPANESE else "Korean"
        relationship = "preserves" if matches else "does not preserve"
        detail = f"Observed provider text {relationship} the local {language_label} script preference."
    else:
        # With no strong native-language preference, ordinary observed-script
        # overlap remains useful (for example Latin text). Incidental Latin is
        # deliberately excluded from the Japanese/Korean branch above.
        local_scripts = {item.script for item in local_profile.script_evidence}
        matches = bool(local_scripts & provider_scripts)
        script_labels = ", ".join(sorted(script.value for script in local_scripts))
        relationship = "matches" if matches else "conflicts with"
        detail = f"Local script evidence ({script_labels}) {relationship} observed provider text."

    return DimensionResult(
        evidence=_evidence(
            MatchReasonCode.LANGUAGE_SCRIPT_MATCH if matches else MatchReasonCode.LANGUAGE_SCRIPT_MISMATCH,
            weight if matches else 0.0,
            detail,
        ),
        available=bool(weight),
        strong_contradiction=False,
    )


def _score_state(
    local: LocalReleaseEvidence,
    release: ReleaseCandidate,
    medium: ReleaseMedium,
    policy: MatchingPolicy,
) -> _ScoringState:
    weights = policy.release_weights
    comparison = _preliminary_track_comparison(local, medium, policy)
    titles = _track_title_dimension(
        local,
        medium,
        1.0,
        comparison,
    )

    # Ask dimensions for agreement on a unit weight. This keeps the exact
    # fractional agreement separate from user-supplied weight magnitudes and
    # from the rounded contributions shown in the explanation dialog.
    # Availability is a bool: unknown dimensions leave the denominator, whereas
    # an available zero agreement still carries its complete policy weight.
    dimensions = (
        WeightedDimension(weight=weights.album_title, result=_album_dimension(local, release, 1.0)),
        WeightedDimension(weight=weights.track_title_order, result=titles.dimension),
        WeightedDimension(
            weight=weights.selected_medium_track_count,
            result=_track_count_dimension(local, medium, 1.0),
        ),
        WeightedDimension(weight=weights.duration, result=_duration_dimension(comparison, 1.0, policy)),
        WeightedDimension(weight=weights.disc, result=_disc_dimension(local, release, medium, 1.0)),
        WeightedDimension(weight=weights.year, result=_year_dimension(local, release, 1.0)),
        WeightedDimension(weight=weights.artist, result=_artist_dimension(local, release, 1.0)),
        WeightedDimension(weight=weights.language_script, result=_language_dimension(local, release, medium, 1.0)),
    )

    # Dividing numerator and denominator by the same positive maximum leaves
    # their ratio unchanged. It avoids overflowing the sum of large weights,
    # and tiny common rescalings cannot disappear through display rounding.
    # Only available weights set the scale: a huge but unavailable dimension
    # must not underflow all the dimensions we can actually compare.
    available = tuple(
        dimension
        for dimension in dimensions
        if dimension.result.available
    )
    scale = max((dimension.weight for dimension in available), default=1.0)
    weighted_total = math.fsum(
        dimension.result.evidence.contribution * (dimension.weight / scale)
        for dimension in available
    )
    available_weight = math.fsum(dimension.weight / scale for dimension in available)

    # Ratios smaller than floating-point representation can still underflow;
    # that is a representational limit, not a new minimum accepted weight.
    # Public evidence remains in familiar raw policy-weight units, rounded to
    # six decimals. Consumers must not reconstruct the ratio from these values.
    evidence = [
        replace(
            dimension.result.evidence,
            contribution=round(dimension.result.evidence.contribution * dimension.weight, SCORE_DECIMAL_PLACES),
        )
        for dimension in dimensions
    ]
    strong_contradiction = any(dimension.result.strong_contradiction for dimension in dimensions)

    if comparison.has_unpaired_tracks:
        # Counts can match even if one file is missing and another is extra.
        # Supported common pairs remain useful, but that unresolved coverage
        # cannot become HIGH merely because the two totals happen to cancel.
        evidence.append(_evidence(
            MatchReasonCode.TRACK_COMPARISON_PARTIAL,
            0.0,
            "Preliminary track correspondence leaves local or provider tracks unpaired; review is required.",
        ))
        strong_contradiction = True

    evidence.extend(_evidence(notice.code, 0.0, notice.detail) for notice in local.notices)
    strong_contradiction = strong_contradiction or any(
        notice.code in _STRONG_LOCAL_CONFLICTS
        for notice in local.notices
    )

    return _ScoringState(
        evidence=tuple(evidence),
        weighted_total=weighted_total,
        available_weight=available_weight,
        strong_contradiction=strong_contradiction,
        track_title_coverage=titles.coverage,
    )


def score_release_medium(
    local: LocalReleaseEvidence,
    release: ReleaseCandidate,
    medium: ReleaseMedium,
    policy: MatchingPolicy = DEFAULT_MATCHING_POLICY,
) -> ReleaseScore:
    """Score one release and one selected medium without flattening its siblings."""
    if not isinstance(local, LocalReleaseEvidence):
        raise TypeError("local must be LocalReleaseEvidence")

    if not isinstance(release, ReleaseCandidate):
        raise TypeError("release must be ReleaseCandidate")

    if not isinstance(medium, ReleaseMedium):
        raise TypeError("medium must be ReleaseMedium")

    if not isinstance(policy, MatchingPolicy):
        raise TypeError("policy must be MatchingPolicy")

    state = _score_state(local, release, medium, policy)
    evidence = list(state.evidence)

    if state.available_weight == 0.0:
        evidence.append(
            _evidence(
                MatchReasonCode.INSUFFICIENT_EVIDENCE,
                0.0,
                "No comparable evidence is available for this release and medium.",
            )
        )

        return ReleaseScore(
            score=0.0,
            classification=MatchClassification.LOW,
            evidence=tuple(evidence),
        )

    # Divide earned points by only the weights we could compare, then scale
    # to 100. If a missing year removes its default weight 5 and everything
    # else agrees, 95 / 95 still gives 100. A conflicting year remains in the
    # denominator, so 95 / 100 gives 95 and also forces review.
    score = round((state.weighted_total / state.available_weight) * 100.0, SCORE_DECIMAL_PLACES)
    thresholds = policy.classification
    provider_listing_complete = medium.tracks_complete and release.media_complete

    # Perfect agreement with every visible row can still describe a truncated
    # result. Keep its useful evidence, but do not represent it as HIGH confidence.
    if not provider_listing_complete:
        evidence.append(
            _evidence(
                MatchReasonCode.PROVIDER_LIST_INCOMPLETE,
                0.0,
                "Provider track or medium listing completeness is unestablished; this result requires review.",
            )
        )

    # Raw score and permission to call a result HIGH are separate decisions.
    # Adequate title coverage, complete provider listings and no strong
    # contradiction are all required even when the numerical score is perfect.
    if score >= thresholds.high:
        coverage_is_adequate = state.track_title_coverage >= thresholds.minimum_high_track_title_coverage

        if not coverage_is_adequate:
            evidence.append(
                _evidence(
                    MatchReasonCode.TRACK_TITLE_COVERAGE_INSUFFICIENT,
                    0.0,
                    (
                        f"Track-title coverage is {state.track_title_coverage:.3f}; "
                        f"HIGH requires at least {thresholds.minimum_high_track_title_coverage:.3f}."
                    ),
                )
            )

        classification = (
            MatchClassification.HIGH
            if coverage_is_adequate and provider_listing_complete and not state.strong_contradiction
            else MatchClassification.REVIEW
        )
    elif score >= thresholds.review:
        classification = MatchClassification.REVIEW
    else:
        classification = MatchClassification.LOW

    return ReleaseScore(
        score=score,
        classification=classification,
        evidence=tuple(evidence),
    )


# Sort score descending, then stable source identities and medium position.
# Explicit secondary keys prevent network response order from deciding an
# equal-score winner; an absent medium number sorts after known numbers.
def _ranking_key(entry: RankedReleaseMedium) -> tuple[float, str, str, str, str, str, str, bool, int, int]:
    release = entry.release
    medium_number = entry.medium.medium_number

    return (
        -entry.result.score,
        release.source_id.casefold(),
        release.source_id,
        release.release_id.casefold(),
        release.release_id,
        release.engine_id.casefold(),
        release.engine_id,
        medium_number is None,
        medium_number if medium_number is not None else 0,
        entry.medium_index,
    )


def _mark_ambiguous(entry: RankedReleaseMedium, difference: float) -> RankedReleaseMedium:
    ambiguity = _evidence(
        MatchReasonCode.AMBIGUOUS_TOP_CANDIDATES,
        0.0,
        f"This result is within {difference:.3f} points of the top result.",
    )

    return replace(
        entry,
        result=replace(
            entry.result,
            classification=MatchClassification.REVIEW,
            evidence=(*entry.result.evidence, ambiguity),
        ),
    )


def _unique_releases(releases: tuple[ReleaseCandidate, ...]) -> tuple[ReleaseCandidate, ...]:
    """Collapse equal full payloads; reject competing meanings of one identity.

    Release identity precedes medium enumeration. Comparing only the selected
    medium would miss conflicting sibling media, credits or completeness. The
    provider boundary must resolve genuine revisions with its explicit source
    policy; accepting whichever arrives first here would make ranking unstable.
    """
    by_identity: dict[tuple[str, str, str], ReleaseCandidate] = {}

    for release in releases:
        identity = (release.engine_id, release.source_id, release.release_id)
        existing = by_identity.get(identity)

        if existing is not None and existing != release:
            raise ValueError(f"conflicting release payloads for candidate identity {identity!r}")

        by_identity[identity] = release

    return tuple(by_identity.values())


def _is_plausible_near_top(score: float, top_score: float, policy: MatchingPolicy) -> bool:
    """Keep the inclusive review and ambiguity boundaries in one predicate."""
    reaches_review = score >= policy.classification.review
    within_margin = top_score - score <= policy.classification.ambiguity_margin

    return reaches_review and within_margin


def rank_release_candidates(
    local: LocalReleaseEvidence,
    releases: Sequence[ReleaseCandidate],
    policy: MatchingPolicy = DEFAULT_MATCHING_POLICY,
) -> ReleaseRanking:
    """Score every distinct release-medium combination without a score shortlist.

    Equal complete payloads with one identity collapse before ranking. Different
    payloads with that identity raise ValueError so callers cannot accidentally
    choose network-arrival precedence. Low results remain selectable for later
    mapping; distinct near-equal plausible candidates still require review.
    """
    if not isinstance(local, LocalReleaseEvidence):
        raise TypeError("local must be LocalReleaseEvidence")

    if not isinstance(policy, MatchingPolicy):
        raise TypeError("policy must be MatchingPolicy")

    validated_releases = _unique_releases(_typed_tuple("releases", releases, ReleaseCandidate))
    entries = [
        RankedReleaseMedium(
            release=release,
            medium=medium,
            medium_index=medium_index,
            result=score_release_medium(local, release, medium, policy),
        )
        for release in validated_releases
        for medium_index, medium in enumerate(release.media)
    ]
    entries.sort(key=_ranking_key)
    ambiguous = False

    # Ambiguity applies only when both leaders reach the REVIEW threshold.
    # A close runner-up that is itself implausible must not weaken the leader.
    # When tied plausible results exist, mark every entry within the margin.
    if len(entries) >= 2:
        top_score = entries[0].result.score
        second_score = entries[1].result.score
        ambiguous = _is_plausible_near_top(second_score, top_score, policy)

        if ambiguous:
            entries = [
                _mark_ambiguous(entry, top_score - entry.result.score)
                if _is_plausible_near_top(entry.result.score, top_score, policy)
                else entry
                for entry in entries
            ]

    return ReleaseRanking(entries=tuple(entries), ambiguous=ambiguous)
