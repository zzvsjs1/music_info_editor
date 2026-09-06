"""Central, immutable defaults for deterministic metadata matching."""

import math
from dataclasses import dataclass, field

# Scores and displayed contributions share a stable presentation precision.
# Calculations must keep full precision until this output boundary; changing
# display rounding must never change which evidence is available or trusted.
SCORE_DECIMAL_PLACES = 6


def _finite_number(name: str, value: object, *, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number")

    # Reject bool separately above because Python treats it as an integer.
    # NaN/infinity below would make comparisons and tie-breaking unreliable.
    normalised = float(value)

    if not math.isfinite(normalised) or normalised < minimum:
        raise ValueError(f"{name} must be finite and at least {minimum:g}")

    return normalised


@dataclass(frozen=True)
class ReleaseScoringWeights:
    """Weights for each independently available release-matching dimension."""

    # These defaults sum to 100, but scoring reweights over available evidence.
    # Their ratios express relative importance. Missing dimensions are left
    # out; actual disagreements retain their available weight.
    album_title: float = 25.0
    track_title_order: float = 30.0
    selected_medium_track_count: float = 20.0
    duration: float = 10.0
    disc: float = 5.0
    year: float = 5.0
    artist: float = 3.0
    language_script: float = 2.0

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            value = _finite_number(name, getattr(self, name))

            if value == 0.0:
                raise ValueError(f"{name} must be greater than zero")

            object.__setattr__(self, name, value)


@dataclass(frozen=True)
class ClassificationThresholds:
    """Human-review boundaries applied after the raw weighted score."""

    high: float = 85.0
    review: float = 65.0
    ambiguity_margin: float = 5.0

    # Coverage is a fraction, unlike the 0..100 score thresholds above.
    # For example, six usable ordered title pairs out of ten meet 0.60.
    minimum_high_track_title_coverage: float = 0.60

    def __post_init__(self) -> None:
        high = _finite_number("high", self.high)
        review = _finite_number("review", self.review)
        ambiguity_margin = _finite_number("ambiguity_margin", self.ambiguity_margin)
        coverage = _finite_number(
            "minimum_high_track_title_coverage",
            self.minimum_high_track_title_coverage,
        )

        if high > 100.0 or review > high:
            raise ValueError("classification thresholds must satisfy 0 <= review <= high <= 100")

        if coverage > 1.0:
            raise ValueError("minimum_high_track_title_coverage must be between zero and one")

        object.__setattr__(self, "high", high)
        object.__setattr__(self, "review", review)
        object.__setattr__(self, "ambiguity_margin", ambiguity_margin)
        object.__setattr__(self, "minimum_high_track_title_coverage", coverage)


@dataclass(frozen=True)
class TrackMappingPolicy:
    """Weights and boundaries for suggestions, independent identity and gaps.

    A score is an evidence-weighted ranking value, not a probability. Reaching
    ``high_pair_score`` is necessary for HIGH but cannot replace content
    support, resolve competing identities or cancel a strong contradiction.
    """

    close_duration_seconds: float = 3.0
    large_duration_mismatch_seconds: float = 10.0

    title_weight: float = 55.0
    duration_weight: float = 20.0
    track_number_weight: float = 20.0
    sequence_position_weight: float = 5.0

    # Discount an agreeing filename number while leaving a real tag at full
    # weight. Gap penalty prices a skipped track during sequence alignment;
    # it is separate from the minimum score required to allow a pair.
    filename_number_factor: float = 0.85

    minimum_pair_score: float = 65.0
    high_pair_score: float = 85.0
    ambiguity_margin: float = 5.0
    gap_penalty: float = 4.0

    # The same title gate corroborates numbered anchors and pair-level HIGH.
    # An exact/near-exact title or a duration within close_duration_seconds
    # supplies content support; numbers and sequence position do not. Keep
    # this separate from the looser score floor for reviewable suggestions.
    minimum_content_title_similarity: float = 0.90

    def __post_init__(self) -> None:
        close = _finite_number("close_duration_seconds", self.close_duration_seconds)
        large = _finite_number(
            "large_duration_mismatch_seconds",
            self.large_duration_mismatch_seconds,
        )

        # The interpolation divides by large - close, so these bounds must be
        # strictly ordered rather than merely non-negative.
        if large <= close:
            raise ValueError("large duration mismatch must be greater than the close tolerance")

        for name in (
            "title_weight",
            "duration_weight",
            "track_number_weight",
            "sequence_position_weight",
            "gap_penalty",
        ):
            value = _finite_number(name, getattr(self, name))

            if value == 0.0:
                raise ValueError(f"{name} must be greater than zero")

            object.__setattr__(self, name, value)

        filename_factor = _finite_number("filename_number_factor", self.filename_number_factor)
        minimum_score = _finite_number("minimum_pair_score", self.minimum_pair_score)
        high_score = _finite_number("high_pair_score", self.high_pair_score)
        ambiguity_margin = _finite_number("ambiguity_margin", self.ambiguity_margin)
        content_similarity = _finite_number(
            "minimum_content_title_similarity",
            self.minimum_content_title_similarity,
        )

        if filename_factor > 1.0:
            raise ValueError("filename_number_factor must be between zero and one")

        if content_similarity > 1.0:
            raise ValueError("minimum_content_title_similarity must be between zero and one")

        if minimum_score > high_score or high_score > 100.0:
            raise ValueError("track mapping scores must satisfy minimum <= high <= 100")

        object.__setattr__(self, "filename_number_factor", filename_factor)
        object.__setattr__(self, "minimum_pair_score", minimum_score)
        object.__setattr__(self, "high_pair_score", high_score)
        object.__setattr__(self, "ambiguity_margin", ambiguity_margin)
        object.__setattr__(self, "minimum_content_title_similarity", content_similarity)

        object.__setattr__(self, "close_duration_seconds", close)
        object.__setattr__(self, "large_duration_mismatch_seconds", large)


@dataclass(frozen=True)
class MatchingPolicy:
    """One tested policy object containing every V1 matching default."""

    release_weights: ReleaseScoringWeights = field(default_factory=ReleaseScoringWeights)
    classification: ClassificationThresholds = field(default_factory=ClassificationThresholds)
    track_mapping: TrackMappingPolicy = field(default_factory=TrackMappingPolicy)

    def __post_init__(self) -> None:
        if not isinstance(self.release_weights, ReleaseScoringWeights):
            raise TypeError("release_weights must be ReleaseScoringWeights")

        if not isinstance(self.classification, ClassificationThresholds):
            raise TypeError("classification must be ClassificationThresholds")

        if not isinstance(self.track_mapping, TrackMappingPolicy):
            raise TypeError("track_mapping must be TrackMappingPolicy")


DEFAULT_MATCHING_POLICY = MatchingPolicy()
