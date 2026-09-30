"""Immutable release evidence and ranking values shared by matching stages.

These records name the independent meanings previously packed into positional
results. Ordered collections remain tuples because their members share one role
and their order carries evidence or deterministic ranking precedence.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from metadata_polisher.domain.matching import ProviderTrack, ReleaseCandidate, ReleaseMedium


# Release ranking asks which release plus medium fits a local group. It uses
# ordered evidence for ranking; the separate track mapper performs the final
# gap-aware alignment. A score is a comparison scale, not a probability.
class MatchClassification(StrEnum):
    """Review class derived from score, coverage, contradictions, and ambiguity."""

    HIGH = "high"
    REVIEW = "review"
    LOW = "low"


class LocalEvidenceSource(StrEnum):
    """Authority tier retained when filename evidence fills a missing tag."""

    TAG = "tag"
    FILENAME = "filename"
    MANUAL_OVERRIDE = "manual_override"


class MatchReasonCode(StrEnum):
    """Stable machine-readable reasons retained beside human-readable details."""

    ALBUM_TITLE_EXACT = "ALBUM_TITLE_EXACT"
    ALBUM_TITLE_SIMILARITY = "ALBUM_TITLE_SIMILARITY"
    ALBUM_TITLE_UNAVAILABLE = "ALBUM_TITLE_UNAVAILABLE"
    TRACK_TITLE_ORDER_EXACT = "TRACK_TITLE_ORDER_EXACT"
    TRACK_TITLE_ORDER_AGREEMENT = "TRACK_TITLE_ORDER_AGREEMENT"
    TRACK_TITLE_ORDER_UNAVAILABLE = "TRACK_TITLE_ORDER_UNAVAILABLE"
    TRACK_TITLE_NUMBER_EXACT = "TRACK_TITLE_NUMBER_EXACT"
    TRACK_TITLE_NUMBER_AGREEMENT = "TRACK_TITLE_NUMBER_AGREEMENT"
    TRACK_COMPARISON_PARTIAL = "TRACK_COMPARISON_PARTIAL"
    TRACK_COUNT_EXACT = "TRACK_COUNT_EXACT"
    TRACK_COUNT_CONTRADICTION = "TRACK_COUNT_CONTRADICTION"
    TRACK_COUNT_UNAVAILABLE = "TRACK_COUNT_UNAVAILABLE"
    PROVIDER_LIST_INCOMPLETE = "PROVIDER_LIST_INCOMPLETE"
    DURATION_CLOSE = "DURATION_CLOSE"
    DURATION_AGREEMENT = "DURATION_AGREEMENT"
    DURATION_LARGE_MISMATCH = "DURATION_LARGE_MISMATCH"
    DURATION_UNAVAILABLE = "DURATION_UNAVAILABLE"
    DISC_EXACT = "DISC_EXACT"
    DISC_CONTRADICTION = "DISC_CONTRADICTION"
    DISC_UNAVAILABLE = "DISC_UNAVAILABLE"
    YEAR_EXACT = "YEAR_EXACT"
    YEAR_NEAR = "YEAR_NEAR"
    YEAR_CONTRADICTION = "YEAR_CONTRADICTION"
    YEAR_UNAVAILABLE = "YEAR_UNAVAILABLE"
    ARTIST_EXACT = "ARTIST_EXACT"
    ARTIST_SIMILARITY = "ARTIST_SIMILARITY"
    ARTIST_UNAVAILABLE = "ARTIST_UNAVAILABLE"
    LANGUAGE_SCRIPT_MATCH = "LANGUAGE_SCRIPT_MATCH"
    LANGUAGE_SCRIPT_MISMATCH = "LANGUAGE_SCRIPT_MISMATCH"
    LANGUAGE_SCRIPT_UNAVAILABLE = "LANGUAGE_SCRIPT_UNAVAILABLE"
    LOCAL_ALBUM_CONFLICT = "LOCAL_ALBUM_CONFLICT"
    LOCAL_ARTIST_CONFLICT = "LOCAL_ARTIST_CONFLICT"
    LOCAL_YEAR_CONFLICT = "LOCAL_YEAR_CONFLICT"
    LOCAL_YEAR_INVALID = "LOCAL_YEAR_INVALID"
    LOCAL_DISC_CONFLICT = "LOCAL_DISC_CONFLICT"
    LOCAL_DISC_TOTAL_CONFLICT = "LOCAL_DISC_TOTAL_CONFLICT"
    LOCAL_TRACK_ORDER_TAGGED = "LOCAL_TRACK_ORDER_TAGGED"
    LOCAL_TRACK_ORDER_FILENAME = "LOCAL_TRACK_ORDER_FILENAME"
    LOCAL_TRACK_ORDER_AMBIGUOUS = "LOCAL_TRACK_ORDER_AMBIGUOUS"
    TRACK_TITLE_COVERAGE_INSUFFICIENT = "TRACK_TITLE_COVERAGE_INSUFFICIENT"
    AMBIGUOUS_TOP_CANDIDATES = "AMBIGUOUS_TOP_CANDIDATES"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    TRACK_TITLE_EXACT = "TRACK_TITLE_EXACT"
    TRACK_TITLE_SIMILARITY = "TRACK_TITLE_SIMILARITY"
    TRACK_TITLE_UNAVAILABLE = "TRACK_TITLE_UNAVAILABLE"
    TRACK_DURATION_CLOSE = "TRACK_DURATION_CLOSE"
    TRACK_DURATION_SIMILARITY = "TRACK_DURATION_SIMILARITY"
    TRACK_DURATION_LARGE_MISMATCH = "TRACK_DURATION_LARGE_MISMATCH"
    TRACK_DURATION_UNAVAILABLE = "TRACK_DURATION_UNAVAILABLE"
    TRACK_NUMBER_EXACT_TAG = "TRACK_NUMBER_EXACT_TAG"
    TRACK_NUMBER_EXACT_FILENAME = "TRACK_NUMBER_EXACT_FILENAME"
    TRACK_NUMBER_CONFLICT = "TRACK_NUMBER_CONFLICT"
    TRACK_NUMBER_UNAVAILABLE = "TRACK_NUMBER_UNAVAILABLE"
    TRACK_SEQUENCE_POSITION = "TRACK_SEQUENCE_POSITION"
    TRACK_MAPPING_COMPLETE = "TRACK_MAPPING_COMPLETE"
    TRACK_MAPPING_PARTIAL = "TRACK_MAPPING_PARTIAL"
    TRACK_MAPPING_AMBIGUOUS = "TRACK_MAPPING_AMBIGUOUS"
    TRACK_MAPPING_INSUFFICIENT_EVIDENCE = "TRACK_MAPPING_INSUFFICIENT_EVIDENCE"
    TRACK_CONTENT_SUPPORT_INSUFFICIENT = "TRACK_CONTENT_SUPPORT_INSUFFICIENT"
    MANUAL_TRACK_ASSIGNMENT = "MANUAL_TRACK_ASSIGNMENT"
    MANUAL_TRACK_UNMAPPED = "MANUAL_TRACK_UNMAPPED"


def _typed_tuple[T](name: str, values: object, item_type: type[T]) -> tuple[T, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise TypeError(f"{name} must be an ordered sequence")

    copied = tuple(values)

    if any(not isinstance(value, item_type) for value in copied):
        raise TypeError(f"{name} must contain only {item_type.__name__} values")

    return copied


def _optional_positive_integer(name: str, value: object) -> None:
    if value is None:
        return

    if type(value) is not int:
        raise TypeError(f"{name} must be an integer or None")

    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")


def _optional_text(name: str, value: object) -> None:
    if value is not None and not isinstance(value, str):
        raise TypeError(f"{name} must be a string or None")


def _duration(value: object) -> float | None:
    if value is None:
        return None

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("duration_seconds must be a number or None")

    normalised = float(value)

    if not math.isfinite(normalised) or normalised < 0.0:
        raise ValueError("duration_seconds must be finite and cannot be negative")

    return normalised


@dataclass(frozen=True)
class MatchEvidence:
    """One weighted contribution or non-scoring diagnostic reason."""

    code: str
    contribution: float
    detail: str

    def __post_init__(self) -> None:
        if not isinstance(self.code, str):
            raise TypeError("code must be a string")

        if not self.code:
            raise ValueError("code must not be empty")

        if isinstance(self.contribution, bool) or not isinstance(self.contribution, (int, float)):
            raise TypeError("contribution must be a number")

        contribution = float(self.contribution)

        if not math.isfinite(contribution) or contribution < 0.0:
            raise ValueError("contribution must be finite and cannot be negative")

        if not isinstance(self.detail, str):
            raise TypeError("detail must be a string")

        if not self.detail.strip():
            raise ValueError("detail must not be empty")

        object.__setattr__(self, "contribution", contribution)


@dataclass(frozen=True)
class ReleaseScore:
    """A deterministic 0–100 ranking score and its complete explanation."""

    score: float
    classification: MatchClassification
    evidence: tuple[MatchEvidence, ...]

    def __post_init__(self) -> None:
        if isinstance(self.score, bool) or not isinstance(self.score, (int, float)):
            raise TypeError("score must be a number")

        score = float(self.score)

        if not math.isfinite(score) or not 0.0 <= score <= 100.0:
            raise ValueError("score must be finite and between zero and 100")

        if not isinstance(self.classification, MatchClassification):
            raise TypeError("classification must be MatchClassification")

        object.__setattr__(self, "score", score)
        object.__setattr__(self, "evidence", _typed_tuple("evidence", self.evidence, MatchEvidence))


@dataclass(frozen=True)
class LocalEvidenceNotice:
    """Non-scoring local conflict that made one evidence dimension unknown."""

    code: str
    detail: str

    def __post_init__(self) -> None:
        if not isinstance(self.code, str):
            raise TypeError("code must be a string")

        if not self.code:
            raise ValueError("code must not be empty")

        if not isinstance(self.detail, str):
            raise TypeError("detail must be a string")

        if not self.detail.strip():
            raise ValueError("detail must not be empty")


@dataclass(frozen=True)
class LocalTrackEvidence:
    """Comparison-only content and optional source numbering for one local file.

    Numbers retain their authority tier; they are not synthetic array positions.
    Existing callers may keep supplying only title and duration, which preserves
    the positional preliminary comparison until corroborated numbering exists.
    """

    title: str | None
    duration_seconds: float | None
    track_number: int | None = None
    track_number_source: LocalEvidenceSource | None = None

    def __post_init__(self) -> None:
        _optional_text("title", self.title)
        object.__setattr__(self, "duration_seconds", _duration(self.duration_seconds))
        _optional_positive_integer("track_number", self.track_number)

        if self.track_number_source is not None and not isinstance(self.track_number_source, LocalEvidenceSource):
            raise TypeError("track_number_source must be a LocalEvidenceSource or None")

        if self.track_number is None and self.track_number_source is not None:
            raise ValueError("track_number_source requires a track number")

        if self.track_number is not None and self.track_number_source is None:
            # Direct evidence follows the established disc-number constructor
            # convention. The scanner builder marks weaker filename hints.
            object.__setattr__(self, "track_number_source", LocalEvidenceSource.TAG)


@dataclass(frozen=True)
class LocalReleaseEvidence:
    """Conservative local group evidence with conflicts represented as unknown."""

    album_title: str | None
    artists: tuple[str, ...]
    year: int | None
    disc_number: int | None
    disc_total: int | None
    tracks: tuple[LocalTrackEvidence, ...]
    disc_number_source: LocalEvidenceSource | None = None
    notices: tuple[LocalEvidenceNotice, ...] = ()

    def __post_init__(self) -> None:
        _optional_text("album_title", self.album_title)
        artists = _typed_tuple("artists", self.artists, str)

        if any(not artist.strip() for artist in artists):
            raise ValueError("artists must not contain blank values")

        _optional_positive_integer("year", self.year)
        _optional_positive_integer("disc_number", self.disc_number)
        _optional_positive_integer("disc_total", self.disc_total)

        if self.disc_number_source is not None and not isinstance(
            self.disc_number_source,
            LocalEvidenceSource,
        ):
            raise TypeError("disc_number_source must be a LocalEvidenceSource or None")

        if self.disc_number is None and self.disc_number_source is not None:
            raise ValueError("disc_number_source requires a disc number")

        if self.disc_number is not None and self.disc_number_source is None:
            # Directly constructed evidence is authoritative by default. The
            # local-file builder explicitly marks weaker filename-derived values.
            object.__setattr__(self, "disc_number_source", LocalEvidenceSource.TAG)

        object.__setattr__(self, "artists", artists)
        object.__setattr__(self, "tracks", _typed_tuple("tracks", self.tracks, LocalTrackEvidence))
        object.__setattr__(self, "notices", _typed_tuple("notices", self.notices, LocalEvidenceNotice))


@dataclass(frozen=True)
class RankedReleaseMedium:
    """One independently scored release-medium combination."""

    release: ReleaseCandidate
    medium: ReleaseMedium
    medium_index: int
    result: ReleaseScore

    def __post_init__(self) -> None:
        if not isinstance(self.release, ReleaseCandidate):
            raise TypeError("release must be ReleaseCandidate")

        if not isinstance(self.medium, ReleaseMedium):
            raise TypeError("medium must be ReleaseMedium")

        if type(self.medium_index) is not int:
            raise TypeError("medium_index must be an integer")

        if self.medium_index < 0:
            raise ValueError("medium_index cannot be negative")

        if not isinstance(self.result, ReleaseScore):
            raise TypeError("result must be ReleaseScore")

    @property
    def identity(self) -> tuple[str, str, str, int]:
        """Return a compact stable identity suitable for logs and UI state."""
        return (
            self.release.engine_id,
            self.release.source_id,
            self.release.release_id,
            self.medium_index,
        )


@dataclass(frozen=True)
class ReleaseRanking:
    """Deterministically ordered release-medium results with ambiguity state."""

    entries: tuple[RankedReleaseMedium, ...]
    ambiguous: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "entries", _typed_tuple("entries", self.entries, RankedReleaseMedium))

        if not isinstance(self.ambiguous, bool):
            raise TypeError("ambiguous must be a bool")

    @property
    def identities(self) -> tuple[tuple[str, str, str, int], ...]:
        """Expose stable identities without discarding the full ranked entries."""
        return tuple(entry.identity for entry in self.entries)


@dataclass(frozen=True)
class TrackPair:
    """One local/provider content comparison, before final track assignment."""

    local: LocalTrackEvidence
    provider: ProviderTrack


@dataclass(frozen=True)
class DimensionResult:
    """Unit-weight agreement, availability and contradiction kept independent.

    An unavailable dimension leaves the score denominator. An available zero
    agreement remains in that denominator, so missing evidence cannot be confused
    with contradictory evidence. The bool records that decision directly.
    """

    evidence: MatchEvidence
    available: bool
    strong_contradiction: bool = False


@dataclass(frozen=True)
class TrackTitleResult:
    """Title agreement plus how much of the complete sequence was compared."""

    dimension: DimensionResult
    coverage: float


@dataclass(frozen=True)
class WeightedDimension:
    """The policy weight associated with one independently calculated result."""

    weight: float
    result: DimensionResult
