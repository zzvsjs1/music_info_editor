"""Order-preserving local-to-provider track alignment with explainable evidence."""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import cast

from rapidfuzz.fuzz import ratio

from metadata_polisher.domain.matching import ProviderTrack, ReleaseCandidate
from metadata_polisher.domain.media import LocalMediaFile
from metadata_polisher.domain.metadata import FieldReadState, MetadataField, Position
from metadata_polisher.matching.evidence import effective_local_title
from metadata_polisher.matching.normalisation import normalise_for_matching
from metadata_polisher.matching.policy import DEFAULT_MATCHING_POLICY, SCORE_DECIMAL_PLACES, MatchingPolicy
from metadata_polisher.matching.release_scoring import (
    MatchClassification,
    MatchEvidence,
    MatchReasonCode,
    order_local_track_files,
)

# Floating-point path totals need a much smaller comparison tolerance than
# public score precision. This is arithmetic housekeeping, not the policy
# margin that decides whether identities are ambiguous.
_ALIGNMENT_ABSOLUTE_TOLERANCE = 1e-9


# Named tuple shapes keep the row/column helpers readable. Each pair always
# stores the local index first and the provider index second.
type _PairIndex = tuple[int, int]
type _PairIndexes = tuple[_PairIndex, ...]
type _PairGroups = tuple[_PairIndexes, ...]


# The mapper first selects numbered anchors, then aligns the remaining
# ordered sequences with gaps. Every chosen pair retains the evidence used
# to score it, so partial matches can be reviewed without rerunning a search.
class _NumberSource(StrEnum):
    TAG = "tag"
    FILENAME = "filename"


@dataclass(frozen=True)
class TrackMapping:
    """One accepted local/provider pair and the metadata positions it implies."""

    local_file_id: str
    provider_track_index: int
    track_position: Position
    disc_position: Position
    score: float
    classification: MatchClassification
    evidence: tuple[MatchEvidence, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.local_file_id, str) or not self.local_file_id:
            raise ValueError("local_file_id must be a non-empty string")

        if type(self.provider_track_index) is not int:
            raise TypeError("provider_track_index must be an integer")

        if self.provider_track_index < 0:
            raise ValueError("provider_track_index cannot be negative")

        if not isinstance(self.track_position, Position):
            raise TypeError("track_position must be Position")

        if not isinstance(self.disc_position, Position):
            raise TypeError("disc_position must be Position")

        if isinstance(self.score, bool) or not isinstance(self.score, (int, float)):
            raise TypeError("score must be a number")

        score = float(self.score)

        if not math.isfinite(score) or not 0.0 <= score <= 100.0:
            raise ValueError("score must be finite and between zero and 100")

        if not isinstance(self.classification, MatchClassification):
            raise TypeError("classification must be MatchClassification")

        object.__setattr__(self, "score", score)
        object.__setattr__(self, "evidence", _typed_tuple("evidence", self.evidence, MatchEvidence))

    @property
    def reason_codes(self) -> tuple[str, ...]:
        """Expose stable codes without requiring callers to parse explanation text."""
        return tuple(item.code for item in self.evidence)


@dataclass(frozen=True)
class TrackMappingResult:
    """Complete partition of local files and one selected provider medium."""

    mappings: tuple[TrackMapping, ...]
    unmatched_local_file_ids: tuple[str, ...]
    unmatched_provider_indexes: tuple[int, ...]
    selected_medium_index: int
    selected_medium_number: int | None
    classification: MatchClassification
    evidence: tuple[MatchEvidence, ...]

    def __post_init__(self) -> None:
        mappings = _typed_tuple("mappings", self.mappings, TrackMapping)
        unmatched_local = _typed_tuple(
            "unmatched_local_file_ids",
            self.unmatched_local_file_ids,
            str,
        )
        unmatched_provider = _typed_tuple(
            "unmatched_provider_indexes",
            self.unmatched_provider_indexes,
            int,
        )

        if any(type(index) is not int for index in unmatched_provider):
            raise TypeError("unmatched_provider_indexes must contain only integer values")

        if type(self.selected_medium_index) is not int:
            raise TypeError("selected_medium_index must be an integer")

        if self.selected_medium_index < 0:
            raise ValueError("selected_medium_index cannot be negative")

        if self.selected_medium_number is not None:
            if type(self.selected_medium_number) is not int:
                raise TypeError("selected_medium_number must be an integer or None")

            if self.selected_medium_number <= 0:
                raise ValueError("selected_medium_number must be greater than zero")

        if not isinstance(self.classification, MatchClassification):
            raise TypeError("classification must be MatchClassification")

        # Mapped and unmatched collections must be disjoint, with no repeated
        # file or track within either collection. Reject double assignments
        # before downstream review can reuse a provider track accidentally.
        mapped_local = tuple(item.local_file_id for item in mappings)
        mapped_provider = tuple(item.provider_track_index for item in mappings)

        if len(mapped_local) != len(set(mapped_local)):
            raise ValueError("mappings contain duplicate local file IDs")

        if len(mapped_provider) != len(set(mapped_provider)):
            raise ValueError("mappings contain duplicate provider track indexes")

        if len(unmatched_local) != len(set(unmatched_local)):
            raise ValueError("unmatched_local_file_ids contains duplicates")

        if len(unmatched_provider) != len(set(unmatched_provider)):
            raise ValueError("unmatched_provider_indexes contains duplicates")

        if set(mapped_local) & set(unmatched_local):
            raise ValueError("mapped and unmatched local files must be disjoint")

        if set(mapped_provider) & set(unmatched_provider):
            raise ValueError("mapped and unmatched provider tracks must be disjoint")

        if any(index < 0 for index in unmatched_provider):
            raise ValueError("unmatched provider indexes cannot be negative")

        object.__setattr__(self, "mappings", mappings)
        object.__setattr__(self, "unmatched_local_file_ids", unmatched_local)
        object.__setattr__(self, "unmatched_provider_indexes", unmatched_provider)
        object.__setattr__(self, "evidence", _typed_tuple("evidence", self.evidence, MatchEvidence))

    @property
    def reason_codes(self) -> tuple[str, ...]:
        """Expose stable overall mapping reason codes for logs and review UI."""
        return tuple(item.code for item in self.evidence)


# Keep the evidence-only score separate from the score including position.
# Position helps choose an ordered path, but must not hide ambiguous titles.
@dataclass(frozen=True)
class _PairAssessment:
    score: float
    substantive_score: float
    substantive_dimension_count: int
    has_substantive_evidence: bool
    strong_contradiction: bool
    support_dimension_count: int
    number_source: _NumberSource | None
    number_agrees: bool | None
    evidence: tuple[MatchEvidence, ...]
    # Retain availability and agreement separately. Aggregate scores alone
    # cannot compare alternatives fairly when their denominators differ.
    # Defaults preserve the private constructor used by independent oracles.
    title_similarity: float | None = None
    duration_similarity: float | None = None
    number_similarity: float | None = None


# value is the accumulated alignment reward after gap penalties; pairs is
# the complete chosen path of zero-based (local, provider) indexes. Keeping
# the path here also makes equal-score decisions reproducible.
@dataclass(frozen=True)
class _AlignmentState:
    value: float
    pairs: tuple[tuple[int, int], ...]


def _typed_tuple[T](name: str, values: object, item_type: type[T]) -> tuple[T, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise TypeError(f"{name} must be an ordered sequence")

    copied = tuple(values)

    if any(not isinstance(value, item_type) for value in copied):
        raise TypeError(f"{name} must contain only {item_type.__name__} values")

    return cast(tuple[T, ...], copied)


def _evidence(code: MatchReasonCode, contribution: float, detail: str) -> MatchEvidence:
    return MatchEvidence(code=code, contribution=contribution, detail=detail)


def _clean_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None

    cleaned = " ".join(value.split())

    return cleaned or None


def _positive_integer(value: object) -> int | None:
    if type(value) is not int or value <= 0:
        return None

    return value


# A PRESENT track tag owns this evidence tier, even if its number is unusable.
# A filename must not silently replace an existing tag during pair assessment.
def _local_number(file: LocalMediaFile) -> tuple[int | None, _NumberSource | None]:
    if file.read_result.field_states[MetadataField.TRACK] is FieldReadState.PRESENT:
        return _positive_integer(file.read_result.metadata.track.number), _NumberSource.TAG

    number = _positive_integer(file.filename_hints.track_number)

    if number is None:
        return None, None

    return number, _NumberSource.FILENAME


def _title_dimension(
    local: LocalMediaFile,
    provider: ProviderTrack,
    policy: MatchingPolicy,
) -> tuple[MatchEvidence, float | None, bool]:
    local_title = effective_local_title(local)
    weight = policy.track_mapping.title_weight
    provider_titles = tuple(
        cleaned
        for title in provider.titles
        if (cleaned := _clean_text(title.value)) is not None
    )

    if local_title is None or not provider_titles:
        return (
            _evidence(
                MatchReasonCode.TRACK_TITLE_UNAVAILABLE,
                0.0,
                "A usable local or provider track title is unavailable.",
            ),
            None,
            False,
        )

    # Provider aliases are alternative spellings of this track. Use the best
    # comparison, without averaging a correct title with unrelated translations
    # or changing any original text that may later be proposed.
    local_comparison = normalise_for_matching(local_title)
    similarities = tuple(
        ratio(local_comparison, normalise_for_matching(title)) / 100.0
        for title in provider_titles
    )
    similarity = max(similarities, default=0.0)
    exact = similarity == 1.0
    code = MatchReasonCode.TRACK_TITLE_EXACT if exact else MatchReasonCode.TRACK_TITLE_SIMILARITY

    return (
        _evidence(
            code,
            weight * similarity,
            f"Best title similarity is {similarity * 100:.1f}% across {len(provider_titles)} provider variant(s).",
        ),
        similarity,
        similarity >= policy.track_mapping.minimum_content_title_similarity,
    )


def _duration_similarity(delta: float, policy: MatchingPolicy) -> float:
    close = policy.track_mapping.close_duration_seconds
    large = policy.track_mapping.large_duration_mismatch_seconds

    if delta <= close:
        return 1.0

    if delta >= large:
        return 0.0

    # Between the two tolerances, reduce similarity in a straight line. With
    # the default 3 s and 10 s bounds, a 6.5 s difference is halfway: 0.5.
    # Differences at or below 3 s receive 1; at or above 10 s receive 0.
    return 1.0 - ((delta - close) / (large - close))


def _duration_dimension(
    local: LocalMediaFile,
    provider: ProviderTrack,
    policy: MatchingPolicy,
) -> tuple[MatchEvidence, float | None, bool, bool]:
    local_duration = local.read_result.stream_info.duration_seconds
    provider_duration = provider.duration_seconds
    weight = policy.track_mapping.duration_weight

    if local_duration is None or provider_duration is None:
        return (
            _evidence(
                MatchReasonCode.TRACK_DURATION_UNAVAILABLE,
                0.0,
                "A local or provider duration is unavailable, so duration is unknown.",
            ),
            None,
            False,
            False,
        )

    delta = abs(local_duration - provider_duration)
    similarity = _duration_similarity(delta, policy)
    large_mismatch = delta >= policy.track_mapping.large_duration_mismatch_seconds

    if large_mismatch:
        code = MatchReasonCode.TRACK_DURATION_LARGE_MISMATCH
    elif delta <= policy.track_mapping.close_duration_seconds:
        code = MatchReasonCode.TRACK_DURATION_CLOSE
    else:
        code = MatchReasonCode.TRACK_DURATION_SIMILARITY

    return (
        _evidence(
            code,
            weight * similarity,
            f"Track durations differ by {delta:.3f} seconds.",
        ),
        similarity,
        similarity == 1.0,
        large_mismatch,
    )


def _number_dimension(
    local: LocalMediaFile,
    provider: ProviderTrack,
    policy: MatchingPolicy,
) -> tuple[MatchEvidence, float | None, _NumberSource | None, bool | None, bool]:
    local_number, source = _local_number(local)
    provider_number = _positive_integer(provider.track_number)
    weight = policy.track_mapping.track_number_weight

    if local_number is None or provider_number is None:
        return (
            _evidence(
                MatchReasonCode.TRACK_NUMBER_UNAVAILABLE,
                0.0,
                "A usable local or provider track number is unavailable.",
            ),
            None,
            source,
            None,
            False,
        )

    # A filename agreement is discounted because filenames are only hints.
    # A conflicting real tag is a strong contradiction; a conflicting filename
    # lowers the score but does not impose that same confidence restriction.
    agrees = local_number == provider_number

    if agrees and source is _NumberSource.TAG:
        code = MatchReasonCode.TRACK_NUMBER_EXACT_TAG
        similarity = 1.0
    elif agrees:
        code = MatchReasonCode.TRACK_NUMBER_EXACT_FILENAME
        similarity = policy.track_mapping.filename_number_factor
    else:
        code = MatchReasonCode.TRACK_NUMBER_CONFLICT
        similarity = 0.0

    return (
        _evidence(
            code,
            weight * similarity,
            f"Local {source.value if source is not None else 'unknown'} number {local_number} "
            f"was compared with provider number {provider_number}.",
        ),
        similarity,
        source,
        agrees,
        not agrees and source is _NumberSource.TAG,
    )


def _position_similarity(
    local_index: int,
    provider_index: int,
    local_count: int,
    provider_count: int,
) -> float:
    # Convert each index to a fraction of its own sequence: first = 0, last = 1.
    # This compares relative position even when a bonus track changes the count.
    # A one-track sequence uses 0 to avoid dividing by zero.
    local_position = local_index / (local_count - 1) if local_count > 1 else 0.0
    provider_position = provider_index / (provider_count - 1) if provider_count > 1 else 0.0

    return max(0.0, 1.0 - abs(local_position - provider_position))


def _assess_pair(
    local: LocalMediaFile,
    provider: ProviderTrack,
    *,
    local_index: int,
    provider_index: int,
    local_count: int,
    provider_count: int,
    policy: MatchingPolicy,
) -> _PairAssessment:
    mapping_policy = policy.track_mapping
    title_evidence, title_similarity, title_supports = _title_dimension(
        local,
        provider,
        policy,
    )
    duration_evidence, duration_similarity, duration_supports, duration_conflicts = (
        _duration_dimension(local, provider, policy)
    )
    number_evidence, number_similarity, number_source, number_agrees, number_conflicts = (
        _number_dimension(local, provider, policy)
    )
    position_similarity = _position_similarity(
        local_index,
        provider_index,
        local_count,
        provider_count,
    )
    position_evidence = _evidence(
        MatchReasonCode.TRACK_SEQUENCE_POSITION,
        mapping_policy.sequence_position_weight * position_similarity,
        f"Relative sequence-position agreement is {position_similarity * 100:.1f}%.",
    )

    # None means there is no comparison, not a disagreement. Exclude absent
    # dimensions from both the earned points and the possible points. A known
    # conflict has similarity 0 and still contributes its full possible weight.
    available: list[tuple[float, float]] = []

    if title_similarity is not None:
        available.append((mapping_policy.title_weight, title_similarity))

    if duration_similarity is not None:
        available.append((mapping_policy.duration_weight, duration_similarity))

    if number_similarity is not None:
        available.append((mapping_policy.track_number_weight, number_similarity))

    # Weighted average = 100 * earned points / available points. For example,
    # title similarity 0.8 at weight 55 and exact duration at weight 20 give
    # 100 * (44 + 20) / (55 + 20), about 85.33, before adding sequence position.
    substantive_weight = sum(weight for weight, _similarity in available)
    substantive_contribution = sum(weight * similarity for weight, similarity in available)
    substantive_score = (
        100.0 * substantive_contribution / substantive_weight
        if substantive_weight > 0.0
        else 0.0
    )

    # Position contributes only a small additional weight. The separate
    # has_substantive_evidence flag prevents this always-available dimension
    # from matching two tracks when nothing else can actually be compared.
    total_weight = substantive_weight + mapping_policy.sequence_position_weight
    total_contribution = (
        substantive_contribution
        + mapping_policy.sequence_position_weight * position_similarity
    )
    score = 100.0 * total_contribution / total_weight

    return _PairAssessment(
        score=round(score, SCORE_DECIMAL_PLACES),
        substantive_score=round(substantive_score, SCORE_DECIMAL_PLACES),
        substantive_dimension_count=len(available),
        has_substantive_evidence=bool(available),
        strong_contradiction=duration_conflicts or number_conflicts,
        support_dimension_count=int(title_supports) + int(duration_supports),
        number_source=number_source,
        number_agrees=number_agrees,
        evidence=(title_evidence, duration_evidence, number_evidence, position_evidence),
        title_similarity=title_similarity,
        duration_similarity=duration_similarity,
        number_similarity=number_similarity,
    )


# Precompute every local/provider pair once. Anchors, ambiguity checks and
# alignment then share identical evidence instead of recalculating scores
# with potentially different inputs along different paths.
def _build_assessment_matrix(
    local_files: tuple[LocalMediaFile, ...],
    provider_tracks: tuple[ProviderTrack, ...],
    policy: MatchingPolicy,
) -> tuple[tuple[_PairAssessment, ...], ...]:
    return tuple(
        tuple(
            _assess_pair(
                local,
                provider,
                local_index=local_index,
                provider_index=provider_index,
                local_count=len(local_files),
                provider_count=len(provider_tracks),
                policy=policy,
            )
            for provider_index, provider in enumerate(provider_tracks)
        )
        for local_index, local in enumerate(local_files)
    )


def _anchor_candidates(
    local_files: tuple[LocalMediaFile, ...],
    provider_tracks: tuple[ProviderTrack, ...],
    assessments: tuple[tuple[_PairAssessment, ...], ...],
    policy: MatchingPolicy,
) -> tuple[tuple[int, int], ...]:
    local_numbers = tuple(_local_number(file) for file in local_files)
    provider_numbers = tuple(_positive_integer(track.track_number) for track in provider_tracks)
    candidates: list[tuple[int, int]] = []

    for local_index, (local_number, source) in enumerate(local_numbers):
        if local_number is None or source is None:
            continue

        # Repeated numbers cannot identify an anchor uniquely on either side.
        # They remain available to the later sequence alignment instead.
        if sum(number == local_number for number, _item_source in local_numbers) != 1:
            continue

        if sum(number == local_number for number in provider_numbers) != 1:
            continue

        provider_index = provider_numbers.index(local_number)
        assessment = assessments[local_index][provider_index]

        # An anchor becomes a hard ordering boundary for every later pair.
        # Both tag and filename numbers therefore need independent content
        # support. A discount on a filename score alone does not make forcing
        # an uncorroborated hint safe; such hints stay reviewable suggestions.
        if not _has_high_content_confidence(assessment, policy):
            continue

        candidates.append((local_index, provider_index))

    return tuple(candidates)


def _select_anchor_chain(candidates: tuple[tuple[int, int], ...]) -> tuple[tuple[int, int], ...]:
    if not candidates:
        return ()

    # Choose a longest increasing chain of candidate anchors. Both indexes
    # must increase, so two anchors can never force the later alignment to cross.
    # For each candidate, retain the best chain ending at that candidate.
    ordered = tuple(sorted(candidates))
    best_ending: list[tuple[tuple[int, int], ...]] = []

    for candidate in ordered:
        earlier_chains = tuple(
            chain
            for chain in best_ending
            if chain[-1][0] < candidate[0] and chain[-1][1] < candidate[1]
        )
        prefix = max(earlier_chains, key=lambda chain: (len(chain), tuple(reversed(chain))), default=())
        best_ending.append((*prefix, candidate))

    # More anchors win. Equal lengths use the lexicographically largest
    # reversed chain: compare final pairs first, then work backwards. This
    # deterministic anchor tie-break differs from the alignment tie-break.
    return max(best_ending, key=lambda chain: (len(chain), tuple(reversed(chain))))


def _is_eligible_pair(assessment: _PairAssessment, policy: MatchingPolicy) -> bool:
    """Keep the existing score floor for useful, possibly reviewable pairs."""
    return (
        assessment.has_substantive_evidence
        and assessment.score >= policy.track_mapping.minimum_pair_score
    )


def _has_high_content_confidence(assessment: _PairAssessment, policy: MatchingPolicy) -> bool:
    """Check the shared content gate; ambiguity is resolved separately."""
    score_is_high = assessment.score >= policy.track_mapping.high_pair_score
    content_supports_identity = assessment.support_dimension_count > 0

    return score_is_high and content_supports_identity and not assessment.strong_contradiction


def _clearly_preferred(
    first: _PairAssessment,
    second: _PairAssessment,
    policy: MatchingPolicy,
) -> bool:
    """Require discriminating observations, rather than extra observations."""
    # A known large duration/tag contradiction is different from missing
    # metadata. Keep that distinction even if the conflicting dimension is
    # unavailable for the other candidate and so cannot be compared below.
    if first.strong_contradiction != second.strong_contradiction:
        return not first.strong_contradiction

    mapping_policy = policy.track_mapping
    comparisons = (
        (mapping_policy.title_weight, first.title_similarity, second.title_similarity),
        (mapping_policy.duration_weight, first.duration_similarity, second.duration_similarity),
        (mapping_policy.track_number_weight, first.number_similarity, second.number_similarity),
    )
    shared = tuple(
        (weight, first_value - second_value)
        for weight, first_value, second_value in comparisons
        if first_value is not None and second_value is not None
    )

    if not shared:
        # Disjoint evidence cannot establish which identity is better. The
        # solver may choose a reproducible path, but that is not corroboration.
        return False

    # Compare the same evidence denominator on both sides. Two equal titles
    # remain equal when one provider omits duration, even if that omission
    # raises its ordinary weighted score. Real shared duration differences
    # still separate repeated titles. Position never participates here.
    shared_weight = sum(weight for weight, _difference in shared)
    advantage = 100.0 * sum(weight * difference for weight, difference in shared) / shared_weight

    return advantage > mapping_policy.ambiguity_margin


def _preferred_pair(
    candidates: _PairIndexes,
    assessments: tuple[tuple[_PairAssessment, ...], ...],
    policy: MatchingPolicy,
) -> _PairIndex | None:
    # A winner must beat every alternative using shared evidence. Sorting
    # by ordinary scores would reintroduce differences caused by missing
    # dimensions, and comparing only two rows can overlook a third rival.
    for candidate in candidates:
        assessment = assessments[candidate[0]][candidate[1]]
        alternatives = (other for other in candidates if other != candidate)

        if all(
            _clearly_preferred(assessment, assessments[other[0]][other[1]], policy)
            for other in alternatives
        ):
            return candidate

    return None


def _order_compatible(first: _PairIndex, second: _PairIndex) -> bool:
    if first == second:
        return True

    if first[0] == second[0] or first[1] == second[1]:
        return False

    # A pair lies before its neighbour on both sides or after it on both.
    # Equal rows/columns above would reuse a track; opposite order crosses it.
    return (first[0] < second[0]) == (first[1] < second[1])


def _competition_groups(
    pairs: _PairIndexes,
) -> tuple[_PairGroups, _PairGroups]:
    """Index candidates once for the two directions of identity competition."""
    rows: dict[int, list[_PairIndex]] = {}
    columns: dict[int, list[_PairIndex]] = {}

    for pair in pairs:
        rows.setdefault(pair[0], []).append(pair)
        columns.setdefault(pair[1], []).append(pair)

    return (
        tuple(tuple(group) for group in rows.values()),
        tuple(tuple(group) for group in columns.values()),
    )


def _feasible_segment_pairs(
    assessments: tuple[tuple[_PairAssessment, ...], ...],
    *,
    local_start: int,
    local_end: int,
    provider_start: int,
    provider_end: int,
    policy: MatchingPolicy,
) -> _PairIndexes:
    plausible = tuple(
        (local_index, provider_index)
        for local_index in range(local_start, local_end)
        for provider_index in range(provider_start, provider_end)
        if _is_eligible_pair(assessments[local_index][provider_index], policy)
    )
    rows, columns = _competition_groups(plausible)
    row_preferences = {_preferred_pair(group, assessments, policy) for group in rows}
    column_preferences = {_preferred_pair(group, assessments, policy) for group in columns}
    neighbours = tuple(
        pair
        for pair in plausible
        if pair in row_preferences
        and pair in column_preferences
        and _has_high_content_confidence(assessments[pair[0]][pair[1]], policy)
    )

    # Only mutually unique, well-supported neighbours may exclude a rival.
    # Conflicting neighbours are not settled by a positional tie-break: omit
    # both as constraints and leave their competing paths to normal alignment.
    compatible_neighbours = tuple(
        pair
        for pair in neighbours
        if all(_order_compatible(pair, other) for other in neighbours)
    )

    return tuple(
        pair
        for pair in plausible
        if all(_order_compatible(pair, neighbour) for neighbour in compatible_neighbours)
    )


def _ambiguous_from_pairs(
    assessments: tuple[tuple[_PairAssessment, ...], ...],
    feasible: _PairIndexes,
    policy: MatchingPolicy,
) -> frozenset[int]:
    ambiguous: set[int] = set()

    # Check both directions. A local with two possible provider tracks and
    # two locals sharing one possible provider track are both identity ties.
    # Restricting this to the current anchor segment and supported neighbours
    # preserves valid partial mappings on either side of a known track.
    rows, columns = _competition_groups(feasible)

    for candidates in (*rows, *columns):
        if len(candidates) < 2:
            continue

        if _preferred_pair(candidates, assessments, policy) is None:
            ambiguous.update(local_index for local_index, _provider_index in candidates)

    return frozenset(ambiguous)


def _ambiguous_locals(
    assessments: tuple[tuple[_PairAssessment, ...], ...],
    *,
    local_start: int,
    local_end: int,
    provider_start: int,
    provider_end: int,
    policy: MatchingPolicy,
) -> frozenset[int]:
    """Expose ambiguity exclusions separately for diagnostics and the oracle."""
    feasible = _feasible_segment_pairs(
        assessments,
        local_start=local_start,
        local_end=local_end,
        provider_start=provider_start,
        provider_end=provider_end,
        policy=policy,
    )

    return _ambiguous_from_pairs(assessments, feasible, policy)


# Prefer the greatest accumulated reward. Treat nearly equal float totals
# as tied, then prefer more accepted pairs and finally the lexicographically
# smallest pair sequence. This final rule makes repeated runs choose alike.
def _better_state(*states: _AlignmentState) -> _AlignmentState:
    best_value = max(state.value for state in states)
    tied = tuple(
        state
        for state in states
        if math.isclose(state.value, best_value, abs_tol=_ALIGNMENT_ABSOLUTE_TOLERANCE)
    )
    greatest_count = max(len(state.pairs) for state in tied)
    finalists = tuple(state for state in tied if len(state.pairs) == greatest_count)

    return min(finalists, key=lambda state: state.pairs)


def _align_segment(
    assessments: tuple[tuple[_PairAssessment, ...], ...],
    *,
    local_start: int,
    local_end: int,
    provider_start: int,
    provider_end: int,
    policy: MatchingPolicy,
) -> tuple[tuple[tuple[int, int], ...], frozenset[int]]:
    local_count = local_end - local_start
    provider_count = provider_end - provider_start
    gap = policy.track_mapping.gap_penalty
    feasible = _feasible_segment_pairs(
        assessments,
        local_start=local_start,
        local_end=local_end,
        provider_start=provider_start,
        provider_end=provider_end,
        policy=policy,
    )
    ambiguous = _ambiguous_from_pairs(assessments, feasible, policy)
    permitted_pairs = frozenset(pair for pair in feasible if pair[0] not in ambiguous)

    # table[i][j] describes only the first i local and first j provider tracks
    # in this segment. Row/column zero represent empty prefixes; therefore the
    # table has one extra row and column beyond the number of tracks.
    table = [
        [_AlignmentState(value=0.0, pairs=()) for _provider in range(provider_count + 1)]
        for _local in range(local_count + 1)
    ]

    # Matching any non-empty prefix against an empty one requires a gap for
    # every track. For two skipped tracks at the default penalty 4, the initial
    # value is -8. These boundary values let the same recurrence handle edges.
    for local_offset in range(1, local_count + 1):
        table[local_offset][0] = _AlignmentState(value=-gap * local_offset, pairs=())

    for provider_offset in range(1, provider_count + 1):
        table[0][provider_offset] = _AlignmentState(value=-gap * provider_offset, pairs=())

    # D[i,j] is the best explanation for the first i local and j provider
    # tracks. It is the maximum of a local gap, a provider gap, or a permitted
    # pair added to D[i-1,j-1]. This recurrence intentionally forbids crossings:
    # every accepted pair extends prefixes in the same order on both sides.
    for local_offset in range(1, local_count + 1):
        for provider_offset in range(1, provider_count + 1):
            local_index = local_start + local_offset - 1
            provider_index = provider_start + provider_offset - 1
            local_gap_state = table[local_offset - 1][provider_offset]
            provider_gap_state = table[local_offset][provider_offset - 1]

            # An upward move skips one local file; a leftward move skips one
            # provider track. In both cases retain the previous accepted pairs
            # and subtract one gap penalty.
            candidates = (
                _AlignmentState(local_gap_state.value - gap, local_gap_state.pairs),
                _AlignmentState(provider_gap_state.value - gap, provider_gap_state.pairs),
            )
            assessment = assessments[local_index][provider_index]

            pair_is_allowed = (local_index, provider_index) in permitted_pairs

            if pair_is_allowed:
                previous = table[local_offset - 1][provider_offset - 1]

                # A diagonal move consumes one track on each side. Only evidence
                # above the minimum earns extra reward: score 85 at minimum 65
                # adds 20. Even a zero-reward permitted pair avoids two gaps.
                reward = assessment.score - policy.track_mapping.minimum_pair_score
                pair_state = _AlignmentState(
                    value=previous.value + reward,
                    pairs=(*previous.pairs, (local_index, provider_index)),
                )
                table[local_offset][provider_offset] = _better_state(*candidates, pair_state)
            else:
                table[local_offset][provider_offset] = _better_state(*candidates)

    return table[local_count][provider_count].pairs, ambiguous


def _align_around_anchors(
    assessments: tuple[tuple[_PairAssessment, ...], ...],
    anchors: tuple[tuple[int, int], ...],
    *,
    local_count: int,
    provider_count: int,
    policy: MatchingPolicy,
) -> tuple[tuple[tuple[int, int], ...], frozenset[int]]:
    pairs: list[tuple[int, int]] = []
    ambiguous: set[int] = set()
    previous_local = -1
    previous_provider = -1

    # Solve only the open intervals between trusted anchors, then append the
    # anchor itself. The final artificial endpoint flushes the trailing segment
    # without creating a fictitious match beyond either sequence.
    for anchor_local, anchor_provider in (*anchors, (local_count, provider_count)):
        segment_pairs, segment_ambiguous = _align_segment(
            assessments,
            local_start=previous_local + 1,
            local_end=anchor_local,
            provider_start=previous_provider + 1,
            provider_end=anchor_provider,
            policy=policy,
        )
        pairs.extend(segment_pairs)
        ambiguous.update(segment_ambiguous)

        if anchor_local < local_count:
            pairs.append((anchor_local, anchor_provider))

        previous_local = anchor_local
        previous_provider = anchor_provider

    return tuple(pairs), frozenset(ambiguous)


# Accepted pairs below HIGH, or with strong contradictions, stay reviewable.
# Contradictions cap confidence here rather than deleting useful partial
# results from the alignment.
def _mapping_classification(
    assessment: _PairAssessment,
    policy: MatchingPolicy,
) -> MatchClassification:
    if _has_high_content_confidence(assessment, policy):
        return MatchClassification.HIGH

    return MatchClassification.REVIEW


def _mapping_evidence(assessment: _PairAssessment) -> tuple[MatchEvidence, ...]:
    if assessment.support_dimension_count > 0:
        return assessment.evidence

    # Keep the numerical suggestion visible while explaining why even score
    # 100 cannot establish content identity from a number and a row position.
    return (
        *assessment.evidence,
        _evidence(
            MatchReasonCode.TRACK_CONTENT_SUPPORT_INSUFFICIENT,
            0.0,
            "No sufficiently similar title or close duration independently supports "
            "this track identity; numbering and position alone require review.",
        ),
    )


def classify_mapping_summary(
    mappings: Sequence[TrackMapping],
    *,
    complete: bool,
    listing_complete: bool,
    ambiguous: bool = False,
) -> MatchClassification:
    """Share visible-row and listing confidence rules with manual review.

    A manually approved pair may be HIGH independently of automatic evidence.
    Neither manual approval nor complete supplied-row coverage proves that an
    incomplete provider response contains every track or medium.
    """
    if not mappings:
        return MatchClassification.REVIEW if ambiguous else MatchClassification.LOW

    all_pairs_high = all(item.classification is MatchClassification.HIGH for item in mappings)

    if complete and listing_complete and all_pairs_high and not ambiguous:
        return MatchClassification.HIGH

    return MatchClassification.REVIEW


def _summary_evidence(
    mappings: tuple[TrackMapping, ...],
    unmatched_local: tuple[str, ...],
    unmatched_provider: tuple[int, ...],
    ambiguous: frozenset[int],
    *,
    listing_complete: bool,
) -> tuple[MatchEvidence, ...]:
    ambiguity_evidence: tuple[MatchEvidence, ...]

    if ambiguous:
        ambiguity_evidence = (
            _evidence(
                MatchReasonCode.TRACK_MAPPING_AMBIGUOUS,
                0.0,
                f"{len(ambiguous)} local track(s) had unresolved competition for track identity.",
            ),
        )
    else:
        ambiguity_evidence = ()

    if mappings and not unmatched_local and not unmatched_provider:
        status = _evidence(
            MatchReasonCode.TRACK_MAPPING_COMPLETE,
            0.0,
            f"All {len(mappings)} supplied local/provider track pair(s) were mapped.",
        )
    elif mappings:
        status = _evidence(
            MatchReasonCode.TRACK_MAPPING_PARTIAL,
            0.0,
            f"Mapped {len(mappings)} pair(s); {len(unmatched_local)} local and "
            f"{len(unmatched_provider)} provider track(s) remain unmatched.",
        )
    else:
        status = _evidence(
            MatchReasonCode.TRACK_MAPPING_INSUFFICIENT_EVIDENCE,
            0.0,
            "No local/provider pair had enough unambiguous substantive evidence.",
        )

    if listing_complete:
        return (*ambiguity_evidence, status)

    # Complete alignment of supplied rows is useful coverage information,
    # while listing completeness is a separate provider contract. Keep both
    # reasons and leave unknown totals to the domain's position projections.
    return (
        *ambiguity_evidence,
        status,
        _evidence(
            MatchReasonCode.PROVIDER_LIST_INCOMPLETE,
            0.0,
            "The selected track listing or release medium listing is incomplete; "
            "mapping supplied rows does not establish complete release identity.",
        ),
    )


def map_tracks(
    local_files: Sequence[LocalMediaFile],
    release: ReleaseCandidate,
    *,
    selected_medium_index: int,
    policy: MatchingPolicy = DEFAULT_MATCHING_POLICY,
) -> TrackMappingResult:
    """Map local files to one medium without flattening release disc boundaries."""
    locals_tuple = _typed_tuple("local_files", local_files, LocalMediaFile)

    if not isinstance(release, ReleaseCandidate):
        raise TypeError("release must be ReleaseCandidate")

    if type(selected_medium_index) is not int:
        raise TypeError("selected_medium_index must be an integer")

    if not 0 <= selected_medium_index < len(release.media):
        raise IndexError("selected_medium_index is outside release media")

    if not isinstance(policy, MatchingPolicy):
        raise TypeError("policy must be MatchingPolicy")

    local_ids = tuple(file.file_id for file in locals_tuple)

    if len(local_ids) != len(set(local_ids)):
        raise ValueError("duplicate local file IDs are not allowed")

    # Alignment preserves album sequence, which reliable numbering establishes
    # more accurately than path sorting such as 1.flac, 10.flac, 2.flac. Sharing
    # the scorer's order policy also keeps release and track explanations aligned.
    locals_tuple, order_notice = order_local_track_files(locals_tuple)
    medium = release.media[selected_medium_index]
    assessments = _build_assessment_matrix(locals_tuple, medium.tracks, policy)
    anchors = _select_anchor_chain(
        _anchor_candidates(locals_tuple, medium.tracks, assessments, policy)
    )
    pair_indexes, ambiguous = _align_around_anchors(
        assessments,
        anchors,
        local_count=len(locals_tuple),
        provider_count=len(medium.tracks),
        policy=policy,
    )

    # Translate alignment indexes back to stable file identities and source
    # positions. Medium/release helpers supply trustworthy numbering and totals;
    # a list index alone is never promoted into a tag value.
    mappings = tuple(
        TrackMapping(
            local_file_id=locals_tuple[local_index].file_id,
            provider_track_index=provider_index,
            track_position=medium.track_position(provider_index),
            disc_position=release.disc_position(selected_medium_index),
            score=assessments[local_index][provider_index].score,
            classification=_mapping_classification(
                assessments[local_index][provider_index],
                policy,
            ),
            evidence=_mapping_evidence(assessments[local_index][provider_index]),
        )
        for local_index, provider_index in pair_indexes
    )

    # Take each complement independently: a missing local file and an extra
    # provider bonus track are different unresolved items and both stay visible.
    mapped_local_indexes = {local_index for local_index, _provider_index in pair_indexes}
    mapped_provider_indexes = {provider_index for _local_index, provider_index in pair_indexes}
    unmatched_local = tuple(
        file.file_id
        for index, file in enumerate(locals_tuple)
        if index not in mapped_local_indexes
    )
    unmatched_provider = tuple(
        index
        for index in range(len(medium.tracks))
        if index not in mapped_provider_indexes
    )
    listing_complete = medium.tracks_complete and release.media_complete
    evidence = _summary_evidence(
        mappings,
        unmatched_local,
        unmatched_provider,
        ambiguous,
        listing_complete=listing_complete,
    )

    if order_notice is not None:
        evidence = (MatchEvidence(order_notice.code, 0.0, order_notice.detail), *evidence)

    classification = classify_mapping_summary(
        mappings,
        complete=not unmatched_local and not unmatched_provider,
        listing_complete=listing_complete,
        ambiguous=bool(ambiguous),
    )

    return TrackMappingResult(
        mappings=mappings,
        unmatched_local_file_ids=unmatched_local,
        unmatched_provider_indexes=unmatched_provider,
        selected_medium_index=selected_medium_index,
        selected_medium_number=medium.medium_number,
        classification=classification,
        evidence=evidence,
    )
