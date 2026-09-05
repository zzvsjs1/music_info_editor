"""Pure proposal consolidation and default Hybrid review policy."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace

from rapidfuzz import fuzz

from metadata_polisher.application.changes import (
    DEFAULT_RENAME_TEMPLATE,
    ChangeValidationFacts,
    FileChangeSet,
    RenameDecision,
    build_change_set,
)
from metadata_polisher.domain.matching import ComposerCredit, CreditScope, MetadataProvenance, ReleaseCandidate
from metadata_polisher.domain.media import LocalMediaFile
from metadata_polisher.domain.metadata import FieldReadState, MetadataField, Position
from metadata_polisher.domain.review import (
    ConsolidatedProposal,
    DecisionOrigin,
    FieldConfidence,
    FieldDecisionKind,
    FieldProposal,
    FieldReviewState,
    FieldValue,
    ProposalRanking,
    ReviewReasonCode,
)
from metadata_polisher.matching.language import Language, LanguageProfileReason, build_language_profile
from metadata_polisher.matching.normalisation import normalise_for_matching
from metadata_polisher.matching.release_scoring import (
    MatchClassification,
    MatchEvidence,
    MatchReasonCode,
    order_local_track_files,
)
from metadata_polisher.matching.track_mapping import TrackMapping, TrackMappingResult
from metadata_polisher.providers.coordinator import CoordinatedCandidate
from metadata_polisher.rename.template import FilenameRenderPolicy

_CONFIDENCE_PRIORITY = {
    FieldConfidence.HIGH: 0,
    FieldConfidence.REVIEW: 1,
    FieldConfidence.LOW: 2,
}
_LANGUAGE_ALIASES = {
    "en": "en",
    "eng": "en",
    "english": "en",
    "ja": "ja",
    "jpn": "ja",
    "japanese": "ja",
    "ko": "ko",
    "kor": "ko",
    "korean": "ko",
}
_SCRIPT_ALIASES = {
    "han": "han",
    "hang": "hang",
    "hangul": "hang",
    "hani": "han",
    "hans": "han",
    "hant": "han",
    "hira": "jpan",
    "hrkt": "jpan",
    "kana": "jpan",
    "katakana": "jpan",
    "latin": "latn",
    "latn": "latn",
    "japanese": "jpan",
    "jpan": "jpan",
}
_ROMANISED_ALIASES = frozenset(("latin", "latn", "romanised", "romanized", "romaji"))
_LANGUAGE_NEUTRAL_FIELDS = frozenset((MetadataField.TRACK, MetadataField.DISC, MetadataField.DATE))

_CLASSIFICATION_CONFIDENCE = {
    MatchClassification.HIGH: FieldConfidence.HIGH,
    MatchClassification.REVIEW: FieldConfidence.REVIEW,
    MatchClassification.LOW: FieldConfidence.LOW,
}
_CONFIDENCE_REASON = {
    FieldConfidence.HIGH: ReviewReasonCode.FIELD_MATCH_HIGH,
    FieldConfidence.REVIEW: ReviewReasonCode.FIELD_MATCH_REVIEW,
    FieldConfidence.LOW: ReviewReasonCode.FIELD_MATCH_LOW,
}
_DEFAULT_FILENAME_RENDER_POLICY = FilenameRenderPolicy()


def _canonical_language(language: str | None) -> str | None:
    if language is None:
        return None

    cleaned = language.strip().casefold()

    return _LANGUAGE_ALIASES.get(cleaned, cleaned)


def _canonical_script(script: str | None) -> str | None:
    if script is None:
        return None

    cleaned = script.strip().casefold()

    return _SCRIPT_ALIASES.get(cleaned, cleaned)


def _value_text(value: FieldValue) -> str:
    if isinstance(value, str):
        return value

    if isinstance(value, Position):
        return ""

    return " ".join(value)


def _effective_script(value: FieldValue, declared_script: str | None) -> str | None:
    canonical_script = _canonical_script(declared_script)

    if canonical_script is not None:
        return canonical_script

    value_text = _value_text(value)

    if not value_text:
        return None

    # Script inference is deliberately narrower than language inference. Latin
    # can identify Latn, for example, but must never be interpreted as English.
    profile = build_language_profile((value_text,))

    if profile.preferred_language is Language.JAPANESE:
        return "jpan"

    if profile.preferred_language is Language.KOREAN:
        return "hang"

    if profile.reason is LanguageProfileReason.HAN_WITHOUT_LANGUAGE_SIGNAL:
        return "han"

    if profile.reason is LanguageProfileReason.LATIN_ONLY:
        return "latn"

    return None


def _value_key(value: FieldValue) -> tuple[object, ...]:
    if isinstance(value, str):
        return ("string", normalise_for_matching(value))

    if isinstance(value, Position):
        # Optional numeric components need an explicit type-stable order. Raw
        # ``None`` and ``int`` values cannot be compared by Python's tuple sort.
        number = (0, 0) if value.number is None else (1, value.number)
        total = (0, 0) if value.total is None else (1, value.total)

        return ("position", number, total)

    return ("sequence", *(normalise_for_matching(item) for item in value))


def _raw_value_key(value: FieldValue) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value.casefold(), value)

    if isinstance(value, Position):
        return (str(value.number), str(value.total))

    return tuple(part for item in value for part in (item.casefold(), item))


def _optional_string_sort_key(value: str | None) -> tuple[str, str]:
    if value is None:
        return "", ""

    return value.casefold(), value


def _proposal_sort_key(proposal: FieldProposal) -> tuple[object, ...]:
    provenance = proposal.provenance

    # This is a total key over every immutable member field. Consolidation must
    # never inherit caller order merely because two aliases share canonical keys.
    return (
        _CONFIDENCE_PRIORITY[proposal.confidence],
        proposal.field.value,
        _value_key(proposal.value),
        _canonical_language(proposal.language) or "",
        _effective_script(proposal.value, proposal.script) or "",
        _raw_value_key(proposal.value),
        _optional_string_sort_key(provenance.source_id),
        _optional_string_sort_key(provenance.engine_id),
        _optional_string_sort_key(provenance.record_id),
        _optional_string_sort_key(provenance.source_url),
        _optional_string_sort_key(provenance.language),
        _optional_string_sort_key(provenance.operation_id),
        _optional_string_sort_key(proposal.language),
        _optional_string_sort_key(proposal.script),
        tuple(code.value for code in proposal.reason_codes),
        tuple((credit.names, credit.scope.value, credit.confidence.value, credit.role,
               credit.record_id or "", credit.source_url or "") for credit in proposal.credit_evidence),
    )


def _ordered_reason_union(*groups: Sequence[ReviewReasonCode]) -> tuple[ReviewReasonCode, ...]:
    return tuple(dict.fromkeys(code for group in groups for code in group))


def _copy_field_proposals(values: object) -> tuple[FieldProposal, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise TypeError("proposals must be an ordered sequence")

    copied = tuple(values)

    if any(not isinstance(proposal, FieldProposal) for proposal in copied):
        raise TypeError("proposals must contain only FieldProposal values")

    return tuple(proposal for proposal in copied if isinstance(proposal, FieldProposal))


def consolidate_proposals(proposals: Sequence[FieldProposal]) -> tuple[ConsolidatedProposal, ...]:
    """Collapse equivalent same-language/script proposals without losing provenance."""
    copied = _copy_field_proposals(proposals)

    fields = {proposal.field for proposal in copied}

    if len(fields) > 1:
        raise ValueError("proposals for different fields cannot be consolidated together")

    # Compare normalised values only within the same language and script. Two
    # spellings can name the same work yet remain useful, separate user choices.
    groups: dict[tuple[object, ...], list[FieldProposal]] = {}

    for proposal in copied:
        key = (
            proposal.field,
            _value_key(proposal.value),
            _canonical_language(proposal.language),
            _effective_script(proposal.value, proposal.script),
        )
        groups.setdefault(key, []).append(proposal)

    consolidated: list[ConsolidatedProposal] = []

    for members_list in groups.values():
        members = tuple(sorted(members_list, key=_proposal_sort_key))
        # Pick an existing spelling deterministically; normalisation is for
        # comparison and must never manufacture the text eventually written.
        representative = min(members, key=lambda item: (_raw_value_key(item.value), _proposal_sort_key(item)))
        confidence = min((member.confidence for member in members), key=_CONFIDENCE_PRIORITY.__getitem__)
        reasons = _ordered_reason_union(*(member.reason_codes for member in members))
        # Several responses from one catalogue are still only one source of
        # evidence, even when they arrived through different lookup engines.
        distinct_sources = {member.provenance.source_id for member in members}

        if len(distinct_sources) > 1:
            reasons = _ordered_reason_union(reasons, (ReviewReasonCode.PROVIDER_AGREEMENT,))

        consolidated.append(
            ConsolidatedProposal(
                field=representative.field,
                value=representative.value,
                language=_canonical_language(representative.language),
                script=_effective_script(representative.value, representative.script),
                confidence=confidence,
                members=members,
                reason_codes=reasons,
            )
        )

    consolidated.sort(
        key=lambda proposal: (
            _CONFIDENCE_PRIORITY[proposal.confidence],
            proposal.field.value,
            _value_key(proposal.value),
            proposal.language or "",
            proposal.script or "",
            _raw_value_key(proposal.value),
        )
    )

    if len(consolidated) > 1:
        marked: list[ConsolidatedProposal] = []

        for consolidated_proposal in consolidated:
            comparable = tuple(
                other
                for other in consolidated
                if other is not consolidated_proposal
                and other.language == consolidated_proposal.language
                and other.script == consolidated_proposal.script
            )
            sources = {
                member.provenance.source_id
                for item in (consolidated_proposal, *comparable)
                for member in item.members
            }

            # Different translations are not disagreement. Flag competing
            # values only after restricting the comparison to matching variants.
            if comparable and len(sources) > 1:
                consolidated_proposal = replace(
                    consolidated_proposal,
                    reason_codes=_ordered_reason_union(
                        consolidated_proposal.reason_codes,
                        (ReviewReasonCode.PROVIDER_DISAGREEMENT,),
                    ),
                )

            marked.append(consolidated_proposal)

        consolidated = marked

    return tuple(consolidated)


def _proposal_text(proposal: ConsolidatedProposal) -> str:
    return _value_text(proposal.value)


def _best_local_similarity(proposal: ConsolidatedProposal, local_texts: tuple[str, ...]) -> float:
    proposal_text = normalise_for_matching(_proposal_text(proposal))

    if not proposal_text or not local_texts:
        return 0.0

    return max(
        fuzz.ratio(proposal_text, normalise_for_matching(local_text))
        for local_text in local_texts
    )


def _comparison_language(proposal: ConsolidatedProposal) -> str | None:
    declared = _canonical_language(proposal.language)

    if declared is not None:
        return declared

    # Provider people/credits often have no language label. Kana or Hangul can
    # still establish compatibility with the album's script; Han-only and Latin
    # remain unknown. This inference is comparison evidence only and never alters
    # the original proposal or provenance language supplied by the provider.
    profile = build_language_profile((_proposal_text(proposal),))

    return profile.preferred_language


def _language_sort_rank(proposal: ConsolidatedProposal, effective_language: str | None) -> int:
    if effective_language is None:
        return 0

    proposal_language = _comparison_language(proposal)

    if proposal_language == effective_language:
        return 0

    if proposal_language is None:
        return 1

    return 2


def _scripts_compatible(proposal_script: str | None, effective_script: str) -> bool:
    """Return whether a provider script can satisfy the requested script style."""
    return proposal_script == effective_script or (
        effective_script == "jpan" and proposal_script == "han"
    )


def _script_sort_rank(proposal: ConsolidatedProposal, effective_script: str | None) -> int:
    if effective_script is None:
        return 0

    proposal_script = _effective_script(proposal.value, proposal.script)

    if proposal_script == effective_script:
        return 0

    if _scripts_compatible(proposal_script, effective_script):
        return 1

    if proposal_script is None:
        return 2

    return 3


def _matches_effective_dimensions(
    proposal: ConsolidatedProposal,
    effective_language: str | None,
    effective_script: str | None,
) -> bool:
    if effective_language is None and effective_script is None:
        return False

    proposal_language = _comparison_language(proposal)
    proposal_script = _effective_script(proposal.value, proposal.script)

    if effective_language is not None and proposal_language != effective_language:
        return False

    return effective_script is None or _scripts_compatible(proposal_script, effective_script)


def _ranking_tie_breaker(proposal: ConsolidatedProposal) -> tuple[object, ...]:
    return (
        _CONFIDENCE_PRIORITY[proposal.confidence],
        -len({member.provenance.source_id for member in proposal.members}),
        _value_key(proposal.value),
        proposal.language or "",
        proposal.script or "",
        _raw_value_key(proposal.value),
        tuple(_proposal_sort_key(member) for member in proposal.members),
    )


def _copy_consolidated_proposals(values: object) -> tuple[ConsolidatedProposal, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise TypeError("proposals must be an ordered sequence")

    copied = tuple(values)

    if any(not isinstance(proposal, ConsolidatedProposal) for proposal in copied):
        raise TypeError("proposals must contain only ConsolidatedProposal values")

    return tuple(proposal for proposal in copied if isinstance(proposal, ConsolidatedProposal))


def _copy_optional_texts(
    values: object,
    *,
    parameter_name: str,
) -> tuple[str | None, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise TypeError(f"{parameter_name} must be an ordered sequence")

    copied = tuple(values)

    if any(text is not None and not isinstance(text, str) for text in copied):
        raise TypeError(f"{parameter_name} must contain only strings or None")

    return tuple(text for text in copied if text is None or isinstance(text, str))


def rank_proposals(
    proposals: Sequence[ConsolidatedProposal],
    *,
    preferred_language: str = "auto",
    local_texts: Sequence[str | None] = (),
    language_profile_texts: Sequence[str | None] | None = None,
) -> ProposalRanking:
    """Rank values using field similarity and separate album language evidence."""
    copied = _copy_consolidated_proposals(proposals)

    fields = {proposal.field for proposal in copied}

    if len(fields) > 1:
        raise ValueError("proposals for different fields cannot be ranked together")

    if not isinstance(preferred_language, str):
        raise TypeError("preferred_language must be a string")

    requested = preferred_language.strip().casefold()

    if not requested:
        raise ValueError("preferred_language cannot be blank")

    copied_local_texts = _copy_optional_texts(local_texts, parameter_name="local_texts")
    # The album-wide profile chooses a language; this field's local text ranks
    # similar spellings. Keeping these inputs separate avoids comparing, for
    # example, a proposed composer name against the album title.
    copied_profile_texts = (
        copied_local_texts
        if language_profile_texts is None
        else _copy_optional_texts(
            language_profile_texts,
            parameter_name="language_profile_texts",
        )
    )

    if copied and copied[0].field in _LANGUAGE_NEUTRAL_FIELDS:
        # Positions and dates have no translated spelling to select. The Hybrid
        # layer still checks confidence and competing values, but the album's
        # language must not make one confident disc number or date ambiguous.
        return ProposalRanking(
            proposals=tuple(sorted(copied, key=_ranking_tie_breaker)),
            effective_language=None,
            effective_script=None,
            ambiguous=False,
            reason_codes=(),
        )

    useful_local_texts = tuple(text for text in copied_local_texts if text is not None and text.strip())
    reasons: tuple[ReviewReasonCode, ...] = ()
    effective_language: str | None = None
    effective_script: str | None = None
    ambiguous = False

    if requested == "auto":
        profile = build_language_profile(copied_profile_texts)

        if profile.preferred_language is Language.JAPANESE:
            effective_language = "ja"
            effective_script = "jpan"
        elif profile.preferred_language is Language.KOREAN:
            effective_language = "ko"
            effective_script = "hang"

        if profile.ambiguous:
            ambiguous = True
            reasons = _ordered_reason_union(reasons, (ReviewReasonCode.LANGUAGE_PROFILE_AMBIGUOUS,))
        elif effective_language is None:
            distinct_languages = {
                language
                for proposal in copied
                if (language := _canonical_language(proposal.language)) is not None
            }
            distinct_scripts = {
                script
                for proposal in copied
                if (script := _effective_script(proposal.value, proposal.script)) is not None
            }
            ambiguous = len(distinct_languages) > 1 or len(distinct_scripts) > 1
            reasons = _ordered_reason_union(reasons, (ReviewReasonCode.LANGUAGE_UNAVAILABLE,))
    else:
        reasons = _ordered_reason_union(reasons, (ReviewReasonCode.LANGUAGE_OVERRIDE,))

        if requested in _ROMANISED_ALIASES:
            profile = build_language_profile(copied_profile_texts)

            if profile.preferred_language is Language.JAPANESE:
                effective_language = "ja"
                effective_script = "latn"
            elif profile.preferred_language is Language.KOREAN:
                effective_language = "ko"
                effective_script = "latn"
            else:
                # Latin names alone do not reveal whether a user wants English,
                # Japanese romanisation, Korean romanisation, or another language.
                ambiguous = True

                if profile.ambiguous:
                    reasons = _ordered_reason_union(
                        reasons,
                        (ReviewReasonCode.LANGUAGE_PROFILE_AMBIGUOUS,),
                    )

                reasons = _ordered_reason_union(
                    reasons,
                    (ReviewReasonCode.LANGUAGE_UNAVAILABLE,),
                )
        else:
            effective_language = _canonical_language(requested)

            if effective_language == "ja":
                effective_script = "jpan"
            elif effective_language == "ko":
                effective_script = "hang"
            elif effective_language == "en":
                effective_script = "latn"

    has_effective_preference = effective_language is not None or effective_script is not None
    has_compatible_proposal = any(
        _matches_effective_dimensions(proposal, effective_language, effective_script)
        for proposal in copied
    )

    if copied and has_effective_preference and not has_compatible_proposal:
        ambiguous = True
        reasons = _ordered_reason_union(reasons, (ReviewReasonCode.LANGUAGE_UNAVAILABLE,))

    # Sort keys are applied left to right: language, script, then similarity.
    # Negating similarity puts the largest match first in an ascending sort;
    # the final stable key makes equal matches independent of provider order.
    ranked = tuple(
        sorted(
            copied,
            key=lambda proposal: (
                _language_sort_rank(proposal, effective_language),
                _script_sort_rank(proposal, effective_script),
                -_best_local_similarity(proposal, useful_local_texts),
                _ranking_tie_breaker(proposal),
            ),
        )
    )
    matched: list[ConsolidatedProposal] = []

    for proposal in ranked:
        if _matches_effective_dimensions(proposal, effective_language, effective_script):
            proposal = replace(
                proposal,
                reason_codes=_ordered_reason_union(
                    proposal.reason_codes,
                    (ReviewReasonCode.LANGUAGE_MATCH,),
                ),
            )

        matched.append(proposal)

    return ProposalRanking(
        proposals=tuple(matched),
        effective_language=effective_language,
        effective_script=effective_script,
        ambiguous=ambiguous,
        reason_codes=reasons,
    )


def _hybrid_state(
    *,
    field: MetadataField,
    read_state: FieldReadState,
    existing_value: FieldValue | None,
    proposals: tuple[ConsolidatedProposal, ...],
    extra_reason_codes: tuple[ReviewReasonCode, ...] = (),
    proposal_choice_ambiguous: bool = False,
) -> FieldReviewState:
    reasons = extra_reason_codes
    selected_proposal: ConsolidatedProposal | None = None

    # A read failure is not an empty field. Preserve uncertain local data before
    # considering any automatic addition, regardless of provider confidence.
    if read_state is FieldReadState.UNREADABLE:
        decision = FieldDecisionKind.KEEP_EXISTING
        requires_review = True
        reasons = _ordered_reason_union(reasons, (ReviewReasonCode.EXISTING_VALUE_UNREADABLE,))
    elif read_state is FieldReadState.UNSUPPORTED:
        decision = FieldDecisionKind.KEEP_EXISTING
        requires_review = True
        reasons = _ordered_reason_union(reasons, (ReviewReasonCode.EXISTING_VALUE_UNSUPPORTED,))
    elif not proposals:
        decision = FieldDecisionKind.KEEP_EXISTING
        requires_review = False

        if read_state is FieldReadState.MISSING:
            reasons = _ordered_reason_union(reasons, (ReviewReasonCode.EXISTING_VALUE_MISSING,))

        reasons = _ordered_reason_union(reasons, (ReviewReasonCode.NO_PROPOSAL,))
    elif read_state is FieldReadState.MISSING:
        reasons = _ordered_reason_union(reasons, (ReviewReasonCode.EXISTING_VALUE_MISSING,))

        # Automatic filling needs all three safeguards: an absent local value,
        # exactly one high-confidence proposal, and no unresolved language choice.
        if (
            len(proposals) == 1
            and proposals[0].confidence is FieldConfidence.HIGH
            and not proposal_choice_ambiguous
        ):
            decision = FieldDecisionKind.USE_PROPOSAL
            selected_proposal = proposals[0]
            requires_review = False
            reasons = _ordered_reason_union(reasons, (ReviewReasonCode.PROPOSAL_CONFIDENT,))
        else:
            decision = FieldDecisionKind.UNRESOLVED
            requires_review = True
            proposal_reason = (
                ReviewReasonCode.PROPOSAL_AMBIGUOUS
                if len(proposals) > 1 or proposal_choice_ambiguous
                else ReviewReasonCode.PROPOSAL_NOT_CONFIDENT
            )
            reasons = _ordered_reason_union(reasons, (proposal_reason,))
    else:
        decision = FieldDecisionKind.KEEP_EXISTING

        if existing_value is None:
            raise ValueError("a present field must have an existing value")

        equivalent = tuple(
            proposal
            for proposal in proposals
            if _value_key(proposal.value) == _value_key(existing_value)
        )

        if len(equivalent) == len(proposals):
            requires_review = False
            reasons = _ordered_reason_union(reasons, (ReviewReasonCode.EXISTING_VALUE_EQUIVALENT,))
        else:
            requires_review = True
            proposal_reason = (
                ReviewReasonCode.PROPOSAL_AMBIGUOUS
                if len(proposals) > 1
                else (
                    ReviewReasonCode.PROPOSAL_CONFIDENT
                    if proposals[0].confidence is FieldConfidence.HIGH
                    else ReviewReasonCode.PROPOSAL_NOT_CONFIDENT
                )
            )
            reasons = _ordered_reason_union(
                reasons,
                (ReviewReasonCode.EXISTING_VALUE_DIFFERENT, proposal_reason),
            )

    return FieldReviewState(
        field=field,
        read_state=read_state,
        existing_value=existing_value,
        proposals=proposals,
        decision=decision,
        selected_proposal=selected_proposal,
        manual_value=None,
        decision_origin=DecisionOrigin.DEFAULT,
        requires_review=requires_review,
        reason_codes=reasons,
    )


def build_field_review_state(
    *,
    field: MetadataField,
    read_state: FieldReadState,
    existing_value: FieldValue | None,
    proposals: Sequence[FieldProposal],
    preferred_language: str = "auto",
    local_texts: Sequence[str | None] = (),
    language_profile_texts: Sequence[str | None] | None = None,
) -> FieldReviewState:
    """Consolidate proposals and apply the exact default Hybrid table."""
    if not isinstance(field, MetadataField):
        raise TypeError("field must be a MetadataField")

    if not isinstance(read_state, FieldReadState):
        raise TypeError("read_state must be a FieldReadState")

    consolidated = consolidate_proposals(proposals)

    if any(proposal.field is not field for proposal in consolidated):
        raise ValueError("every proposal must belong to field")

    ranking = rank_proposals(
        consolidated,
        preferred_language=preferred_language,
        local_texts=local_texts,
        language_profile_texts=language_profile_texts,
    )

    return _hybrid_state(
        field=field,
        read_state=read_state,
        existing_value=existing_value,
        proposals=ranking.proposals,
        extra_reason_codes=ranking.reason_codes,
        proposal_choice_ambiguous=ranking.ambiguous,
    )


@dataclass(frozen=True)
class ReviewedFileResult:
    """State-independent proposals, reviews, and derived changes for one file."""

    file_id: str
    proposals: tuple[FieldProposal, ...]
    reviews: tuple[FieldReviewState, ...]
    track_mapping_resolved: bool
    change_set: FileChangeSet

    def __post_init__(self) -> None:
        if not isinstance(self.file_id, str) or not self.file_id:
            raise ValueError("file_id must be a non-empty string")

        proposals = _copy_field_proposals(self.proposals)
        reviews = tuple(self.reviews)

        if len(proposals) != len(set(proposals)):
            raise ValueError("proposals must be unique")

        if any(not isinstance(review, FieldReviewState) for review in reviews):
            raise TypeError("reviews must contain only FieldReviewState values")

        if len(reviews) != len(MetadataField) or tuple(review.field for review in reviews) != tuple(
            MetadataField
        ):
            raise ValueError("reviews must contain every MetadataField in declaration order")

        review_members = tuple(
            member
            for review in reviews
            for consolidated in review.proposals
            for member in consolidated.members
        )

        if len(review_members) != len(set(review_members)) or set(review_members) != set(proposals):
            raise ValueError("proposals must equal the unique members retained by reviews")

        if type(self.track_mapping_resolved) is not bool:
            raise TypeError("track_mapping_resolved must be a bool")

        if not isinstance(self.change_set, FileChangeSet):
            raise TypeError("change_set must be a FileChangeSet")

        if self.change_set.file_id != self.file_id:
            raise ValueError("change_set must refer to file_id")

        object.__setattr__(self, "proposals", proposals)
        object.__setattr__(self, "reviews", reviews)


def _confidence_for(
    release_classification: MatchClassification,
    mapping: TrackMapping | None = None,
) -> FieldConfidence:
    classifications = [release_classification]

    if mapping is not None:
        classifications.append(mapping.classification)

    return max(
        (_CLASSIFICATION_CONFIDENCE[item] for item in classifications),
        key=_CONFIDENCE_PRIORITY.__getitem__,
    )


def _proposal(
    *,
    field: MetadataField,
    value: FieldValue,
    confidence: FieldConfidence,
    provenance: MetadataProvenance,
    language: str | None = None,
    script: str | None = None,
    credit_evidence: tuple[ComposerCredit, ...] = (),
) -> FieldProposal:
    return FieldProposal(
        field=field,
        value=value,
        confidence=confidence,
        provenance=replace(provenance, language=language),
        language=language,
        script=script,
        reason_codes=(_CONFIDENCE_REASON[confidence],),
        credit_evidence=credit_evidence,
    )


def _representative_provenance(selected: CoordinatedCandidate) -> MetadataProvenance:
    candidate = selected.candidate

    try:
        return next(
            provenance
            for provenance in selected.provenance
            if provenance.engine_id == candidate.engine_id
            and provenance.source_id == candidate.source_id
            and provenance.record_id == candidate.release_id
        )
    except StopIteration:
        raise ValueError("selected candidate lacks representative provenance") from None


def _metadata_value(file: LocalMediaFile, field: MetadataField) -> FieldValue | None:
    metadata = file.read_result.metadata
    values: dict[MetadataField, FieldValue | None] = {
        MetadataField.TITLE: metadata.title,
        MetadataField.ARTISTS: metadata.artists,
        MetadataField.ALBUM: metadata.album,
        MetadataField.ALBUM_ARTISTS: metadata.album_artists,
        MetadataField.COMPOSERS: metadata.composers,
        MetadataField.TRACK: metadata.track,
        MetadataField.DISC: metadata.disc,
        MetadataField.DATE: metadata.date,
        MetadataField.GENRES: metadata.genres,
    }

    return values[field]


def _existing_value(file: LocalMediaFile, field: MetadataField) -> FieldValue | None:
    read_state = file.read_result.field_states[field]

    if read_state is FieldReadState.MISSING:
        return None

    value = _metadata_value(file, field)
    has_semantic_value = (
        (isinstance(value, str) and bool(value.strip()))
        or (isinstance(value, tuple) and bool(value))
        or (
            isinstance(value, Position)
            and (value.number is not None or value.total is not None)
        )
    )

    if has_semantic_value:
        return value

    if read_state is FieldReadState.PRESENT:
        raise ValueError(f"present field {field.value} has no semantic value")

    return None


def _position_with_existing_components(
    file: LocalMediaFile,
    field: MetadataField,
    proposed: Position,
) -> Position:
    """Absent source components preserve supported local values rather than clearing them."""
    if file.read_result.field_states[field] is not FieldReadState.PRESENT:
        return proposed

    existing = _existing_value(file, field)

    if not isinstance(existing, Position):
        return proposed

    return Position(
        proposed.number if proposed.number is not None else existing.number,
        proposed.total if proposed.total is not None else existing.total,
    )


def build_local_review_texts(
    files: tuple[LocalMediaFile, ...],
) -> dict[MetadataField, tuple[str, ...]]:
    """Project readable local text for initial review and later language edits."""
    text_fields = (
        MetadataField.TITLE,
        MetadataField.ARTISTS,
        MetadataField.ALBUM,
        MetadataField.ALBUM_ARTISTS,
        MetadataField.COMPOSERS,
    )
    result: dict[MetadataField, tuple[str, ...]] = {}

    for field in text_fields:
        texts: list[str] = []

        for file in files:
            if file.read_result.field_states[field] is not FieldReadState.PRESENT:
                continue

            value = _metadata_value(file, field)

            if isinstance(value, str):
                texts.append(value)
            elif isinstance(value, tuple):
                texts.extend(value)

        result[field] = tuple(texts)

    return result


def build_selected_file_results(
    files: Sequence[LocalMediaFile],
    selected: CoordinatedCandidate,
    *,
    medium_index: int,
    mapping_result: TrackMappingResult,
    release_classification: MatchClassification,
    preferred_language: str,
    validation_facts_by_file: Mapping[str, ChangeValidationFacts] | None = None,
    rename_decisions_by_file: Mapping[str, RenameDecision] | None = None,
    rename_template: str = DEFAULT_RENAME_TEMPLATE,
    rename_policy: FilenameRenderPolicy = _DEFAULT_FILENAME_RENDER_POLICY,
    check_cancelled: Callable[[], None] | None = None,
    on_file_built: Callable[[str], None] | None = None,
) -> tuple[ReviewedFileResult, ...]:
    """Build proposals, all field reviews, and ChangeSets from immutable inputs."""
    copied_files = tuple(files)

    if any(not isinstance(file, LocalMediaFile) for file in copied_files):
        raise TypeError("files must contain only LocalMediaFile values")

    file_ids = tuple(file.file_id for file in copied_files)

    if len(file_ids) != len(set(file_ids)):
        raise ValueError("files must have unique file IDs")

    if not isinstance(selected, CoordinatedCandidate):
        raise TypeError("selected must be a CoordinatedCandidate")

    if not isinstance(mapping_result, TrackMappingResult):
        raise TypeError("mapping_result must be a TrackMappingResult")

    if not isinstance(release_classification, MatchClassification):
        raise TypeError("release_classification must be a MatchClassification")

    if not isinstance(preferred_language, str) or not preferred_language.strip():
        raise ValueError("preferred_language must be a non-blank string")

    candidate = selected.candidate

    if not 0 <= medium_index < len(candidate.media):
        raise IndexError("medium_index is outside selected candidate media")

    if mapping_result.selected_medium_index != medium_index:
        raise ValueError("mapping_result must refer to medium_index")

    if mapping_result.selected_medium_number != candidate.media[medium_index].medium_number:
        raise ValueError("mapping_result must refer to the selected medium number")

    if validation_facts_by_file is not None and not isinstance(
        validation_facts_by_file,
        Mapping,
    ):
        raise TypeError("validation_facts_by_file must be a mapping or None")

    if rename_decisions_by_file is not None and not isinstance(
        rename_decisions_by_file,
        Mapping,
    ):
        raise TypeError("rename_decisions_by_file must be a mapping or None")

    facts_by_file = validation_facts_by_file or {}
    rename_by_file = rename_decisions_by_file or {}
    unknown_fact_ids = set(facts_by_file) - {file.file_id for file in copied_files}

    if unknown_fact_ids:
        raise ValueError("validation facts must refer only to supplied files")

    if any(not isinstance(value, ChangeValidationFacts) for value in facts_by_file.values()):
        raise TypeError("validation facts must contain ChangeValidationFacts values")

    if set(rename_by_file) - {file.file_id for file in copied_files}:
        raise ValueError("rename decisions must refer only to supplied files")

    if any(not isinstance(value, RenameDecision) for value in rename_by_file.values()):
        raise TypeError("rename decisions must contain RenameDecision values")

    if not isinstance(rename_template, str):
        raise TypeError("rename_template must be a string")

    if not isinstance(rename_policy, FilenameRenderPolicy):
        raise TypeError("rename_policy must be a FilenameRenderPolicy")

    base_provenance = _representative_provenance(selected)
    medium = candidate.media[medium_index]
    release_confidence = _confidence_for(release_classification)
    # Mapping indexes belong to this particular medium, not the whole release.
    # Check both partitions before using an index to propose track-specific tags.
    mappings_by_file = {item.local_file_id: item for item in mapping_result.mappings}
    mapped_local_ids = set(mappings_by_file)
    all_mapping_local_ids = mapped_local_ids | set(mapping_result.unmatched_local_file_ids)
    mapped_provider_indexes = {item.provider_track_index for item in mapping_result.mappings}
    all_mapping_provider_indexes = mapped_provider_indexes | set(
        mapping_result.unmatched_provider_indexes
    )

    if all_mapping_local_ids != set(file_ids):
        raise ValueError("mapping_result must partition every supplied local file")

    if all_mapping_provider_indexes != set(range(len(medium.tracks))):
        raise ValueError("mapping_result must partition every selected provider track")

    disc_position = candidate.disc_position(medium_index)

    for source_mapping in mapping_result.mappings:
        if (
            source_mapping.track_position != medium.track_position(source_mapping.provider_track_index)
            or source_mapping.disc_position != disc_position
        ):
            raise ValueError("Mapped positions must match the current selected provider medium.")

    group_local_texts = build_local_review_texts(copied_files)
    language_profile_texts = tuple(
        text
        for field in (
            MetadataField.TITLE,
            MetadataField.ARTISTS,
            MetadataField.ALBUM,
            MetadataField.ALBUM_ARTISTS,
        )
        for text in group_local_texts.get(field, ())
    )
    results: list[ReviewedFileResult] = []

    for file in copied_files:
        if check_cancelled is not None:
            check_cancelled()

        proposals: list[FieldProposal] = []

        # Release-level values can be offered even when this file has no track
        # assignment. Track titles, artists and composers require a mapped pair.
        for title in candidate.titles:
            if title.value.strip():
                proposals.append(
                    _proposal(
                        field=MetadataField.ALBUM,
                        value=title.value,
                        confidence=release_confidence,
                        provenance=base_provenance,
                        language=title.language,
                        script=title.script,
                    )
                )

        if candidate.album_artists and all(value.strip() for value in candidate.album_artists):
            proposals.append(
                _proposal(
                    field=MetadataField.ALBUM_ARTISTS,
                    value=candidate.album_artists,
                    confidence=release_confidence,
                    provenance=base_provenance,
                )
            )

        if candidate.date is not None and candidate.date.strip():
            proposals.append(
                _proposal(
                    field=MetadataField.DATE,
                    value=candidate.date,
                    confidence=release_confidence,
                    provenance=base_provenance,
                )
            )

        if disc_position != Position():
            proposals.append(
                _proposal(
                    field=MetadataField.DISC,
                    value=_position_with_existing_components(file, MetadataField.DISC, disc_position),
                    confidence=release_confidence,
                    provenance=base_provenance,
                )
            )

        mapping = mappings_by_file.get(file.file_id)

        if mapping is not None:
            track = medium.tracks[mapping.provider_track_index]
            track_confidence = _confidence_for(release_classification, mapping)

            for title in track.titles:
                if title.value.strip():
                    proposals.append(
                        _proposal(
                            field=MetadataField.TITLE,
                            value=title.value,
                            confidence=track_confidence,
                            provenance=base_provenance,
                            language=title.language,
                            script=title.script,
                        )
                    )

            if track.artists and all(item.strip() for item in track.artists):
                proposals.append(
                    _proposal(
                        field=MetadataField.ARTISTS,
                        value=track.artists,
                        confidence=track_confidence,
                        provenance=base_provenance,
                    )
                )

            # Only source-assigned track/work credits or an explicit all-tracks
            # statement apply here. A legacy names tuple is not attribution.
            credits = tuple(dict.fromkeys(
                credit for credit in (*track.composer_credits, *candidate.album_credits)
                if credit.supports_track_composer
                and (credit in track.composer_credits or credit.scope is CreditScope.ALL_TRACKS)
            ))

            if credits:
                # Larger priority numbers mean weaker confidence. Taking the
                # weakest input prevents precise-looking credits from outranking
                # either their attribution evidence or the track match itself.
                composer_confidence = max(
                    (track_confidence, *(FieldConfidence(credit.confidence.value) for credit in credits)),
                    key=_CONFIDENCE_PRIORITY.__getitem__,
                )
                names = tuple(dict.fromkeys(name for credit in credits for name in credit.names))
                proposals.append(_proposal(
                    field=MetadataField.COMPOSERS, value=names, confidence=composer_confidence,
                    provenance=base_provenance, credit_evidence=credits,
                ))

            if mapping.track_position != Position():
                proposals.append(
                    _proposal(
                        field=MetadataField.TRACK,
                        value=_position_with_existing_components(file, MetadataField.TRACK, mapping.track_position),
                        confidence=track_confidence,
                        provenance=base_provenance,
                    )
                )

        # Remove exact duplicates without reordering the provider's variants,
        # then build every managed field, including fields with no proposal.
        proposals = list(dict.fromkeys(proposals))
        proposals_by_field = {
            field: tuple(proposal for proposal in proposals if proposal.field is field)
            for field in MetadataField
        }
        reviews = tuple(
            build_field_review_state(
                field=field,
                read_state=file.read_result.field_states[field],
                existing_value=_existing_value(file, field),
                proposals=proposals_by_field[field],
                preferred_language=preferred_language,
                local_texts=group_local_texts.get(field, ()),
                language_profile_texts=language_profile_texts,
            )
            for field in MetadataField
        )
        supplied_facts = facts_by_file.get(file.file_id, ChangeValidationFacts())
        validation = replace(
            supplied_facts,
            track_mapping_resolved=mapping is not None,
        )
        change_set = build_change_set(
            file,
            reviews,
            rename_by_file.get(file.file_id, RenameDecision.KEEP_FILENAME),
            rename_template,
            rename_policy,
            validation=validation,
        )
        results.append(
            ReviewedFileResult(
                file_id=file.file_id,
                proposals=tuple(proposals),
                reviews=reviews,
                track_mapping_resolved=mapping is not None,
                change_set=change_set,
            )
        )

        if on_file_built is not None:
            on_file_built(file.file_id)

    return tuple(results)


_MAPPING_SUMMARY_CODES = frozenset(
    (
        MatchReasonCode.TRACK_MAPPING_COMPLETE,
        MatchReasonCode.TRACK_MAPPING_PARTIAL,
        MatchReasonCode.TRACK_MAPPING_AMBIGUOUS,
        MatchReasonCode.TRACK_MAPPING_INSUFFICIENT_EVIDENCE,
        MatchReasonCode.MANUAL_TRACK_ASSIGNMENT,
        MatchReasonCode.MANUAL_TRACK_UNMAPPED,
    )
)


def set_manual_track_assignment(
    files: Sequence[LocalMediaFile],
    candidate: ReleaseCandidate,
    mapping: TrackMappingResult,
    *,
    local_file_id: str,
    provider_track_index: int | None,
) -> TrackMappingResult:
    """Assign one local file to an unused selected-medium track, or leave it unmapped.

    Human confirmation is separate from similarity scoring. It establishes a
    usable pairing without inventing a numerical match score or improving the
    release confidence. Untouched automatic pairs retain their original evidence.
    """
    if not isinstance(files, Sequence):
        raise TypeError("files must be an ordered sequence of LocalMediaFile values")

    copied_files = tuple(files)

    if any(not isinstance(file, LocalMediaFile) for file in copied_files):
        raise TypeError("files must contain only LocalMediaFile values")

    if not isinstance(candidate, ReleaseCandidate):
        raise TypeError("candidate must be a ReleaseCandidate")

    if not isinstance(mapping, TrackMappingResult):
        raise TypeError("mapping must be a TrackMappingResult")

    if not isinstance(local_file_id, str):
        raise TypeError("local_file_id must be a string")

    if provider_track_index is not None and type(provider_track_index) is not int:
        raise TypeError("provider_track_index must be an integer or None")

    local_ids = tuple(file.file_id for file in copied_files)

    if len(local_ids) != len(set(local_ids)):
        raise ValueError("files must have unique local file IDs")

    if local_file_id not in local_ids:
        raise ValueError("The selected local file must belong to the supplied files.")

    if not 0 <= mapping.selected_medium_index < len(candidate.media):
        raise ValueError("The selected medium is outside the supplied release.")

    medium = candidate.media[mapping.selected_medium_index]

    if mapping.selected_medium_number != medium.medium_number:
        raise ValueError("The mapping must refer to the selected medium number.")

    mapped_by_id = {item.local_file_id: item for item in mapping.mappings}
    mapped_provider = {item.provider_track_index for item in mapping.mappings}

    if set(mapped_by_id) | set(mapping.unmatched_local_file_ids) != set(local_ids):
        raise ValueError("The mapping must partition every supplied local file.")

    if mapped_provider | set(mapping.unmatched_provider_indexes) != set(range(len(medium.tracks))):
        raise ValueError("The mapping must partition every selected provider track.")

    disc_position = candidate.disc_position(mapping.selected_medium_index)

    for item in mapping.mappings:
        expected_track = medium.track_position(item.provider_track_index)

        if item.track_position != expected_track or item.disc_position != disc_position:
            raise ValueError("Mapped positions must match the current selected provider medium.")

    if provider_track_index is not None:
        if not 0 <= provider_track_index < len(medium.tracks):
            raise ValueError("Choose a provider track from the selected medium.")

        if any(
            item.provider_track_index == provider_track_index and item.local_file_id != local_file_id
            for item in mapping.mappings
        ):
            raise ValueError("That provider track is already assigned; clear its current assignment first.")

        # HIGH records an accepted human assignment here. The zero contribution
        # and explicit reason keep it distinguishable from a strong fuzzy match.
        action = MatchEvidence(
            MatchReasonCode.MANUAL_TRACK_ASSIGNMENT,
            0.0,
            f"User assigned local file {local_file_id} to provider track index {provider_track_index} "
            f"in selected medium {mapping.selected_medium_index}. No automated score was attributed.",
        )
        mapped_by_id[local_file_id] = TrackMapping(
            local_file_id=local_file_id,
            provider_track_index=provider_track_index,
            track_position=medium.track_position(provider_track_index),
            disc_position=disc_position,
            score=0.0,
            classification=MatchClassification.HIGH,
            evidence=(action,),
        )
    else:
        mapped_by_id.pop(local_file_id, None)
        action = MatchEvidence(
            MatchReasonCode.MANUAL_TRACK_UNMAPPED,
            0.0,
            f"User left local file {local_file_id} unmapped; it receives no track-specific provider proposals.",
        )

    ordered_files, order_notice = order_local_track_files(copied_files)
    updated_mappings = tuple(mapped_by_id[file.file_id] for file in ordered_files if file.file_id in mapped_by_id)
    unmatched_local = tuple(file.file_id for file in ordered_files if file.file_id not in mapped_by_id)
    used_provider = {item.provider_track_index for item in updated_mappings}
    unmatched_provider = tuple(index for index in range(len(medium.tracks)) if index not in used_provider)
    complete = bool(updated_mappings) and not unmatched_local and not unmatched_provider

    if complete and all(item.classification is MatchClassification.HIGH for item in updated_mappings):
        classification = MatchClassification.HIGH
    elif updated_mappings:
        classification = MatchClassification.REVIEW
    else:
        classification = MatchClassification.LOW

    # Automatic completeness and ambiguity summaries describe the old partition.
    # Recalculate them from the current assignments while retaining pair evidence
    # and any independent diagnostic facts already attached to the result.
    evidence = tuple(item for item in mapping.evidence if item.code not in _MAPPING_SUMMARY_CODES)

    if order_notice is not None and order_notice.code not in {item.code for item in evidence}:
        evidence = (*evidence, MatchEvidence(order_notice.code, 0.0, order_notice.detail))

    summary = MatchEvidence(
        MatchReasonCode.TRACK_MAPPING_COMPLETE if complete else MatchReasonCode.TRACK_MAPPING_PARTIAL,
        0.0,
        f"Mapped {len(updated_mappings)} pair(s); {len(unmatched_local)} local and "
        f"{len(unmatched_provider)} provider track(s) remain unmapped after manual review.",
    )

    return TrackMappingResult(
        mappings=updated_mappings,
        unmatched_local_file_ids=unmatched_local,
        unmatched_provider_indexes=unmatched_provider,
        selected_medium_index=mapping.selected_medium_index,
        selected_medium_number=medium.medium_number,
        classification=classification,
        evidence=(*evidence, action, summary),
    )


_USER_ACTION_CODES = frozenset(
    (
        ReviewReasonCode.KEEP_EXISTING_SELECTED,
        ReviewReasonCode.PROPOSAL_SELECTED,
        ReviewReasonCode.MANUAL_VALUE_SELECTED,
        ReviewReasonCode.CLEAR_SELECTED,
        ReviewReasonCode.USER_DECISION_PRESERVED,
    )
)
_LANGUAGE_CODES = frozenset(
    (
        ReviewReasonCode.LANGUAGE_MATCH,
        ReviewReasonCode.LANGUAGE_OVERRIDE,
        ReviewReasonCode.LANGUAGE_PROFILE_AMBIGUOUS,
        ReviewReasonCode.LANGUAGE_UNAVAILABLE,
    )
)


def _user_reasons(state: FieldReviewState, action: ReviewReasonCode) -> tuple[ReviewReasonCode, ...]:
    retained = tuple(code for code in state.reason_codes if code not in _USER_ACTION_CODES)

    return _ordered_reason_union(retained, (action,))


def set_keep_existing_decision(state: FieldReviewState) -> FieldReviewState:
    """Record an explicit choice to retain the local value or its missing state."""
    if not isinstance(state, FieldReviewState):
        raise TypeError("state must be a FieldReviewState")

    return FieldReviewState(
        field=state.field,
        read_state=state.read_state,
        existing_value=state.existing_value,
        proposals=state.proposals,
        decision=FieldDecisionKind.KEEP_EXISTING,
        selected_proposal=None,
        manual_value=None,
        decision_origin=DecisionOrigin.USER,
        requires_review=False,
        reason_codes=_user_reasons(state, ReviewReasonCode.KEEP_EXISTING_SELECTED),
    )


def set_proposal_decision(
    state: FieldReviewState,
    proposal: ConsolidatedProposal,
) -> FieldReviewState:
    """Record an explicit selection from the currently visible proposals."""
    if not isinstance(state, FieldReviewState):
        raise TypeError("state must be a FieldReviewState")

    if not isinstance(proposal, ConsolidatedProposal):
        raise TypeError("proposal must be a ConsolidatedProposal")

    if proposal not in state.proposals:
        raise ValueError("proposal must belong to state.proposals")

    return FieldReviewState(
        field=state.field,
        read_state=state.read_state,
        existing_value=state.existing_value,
        proposals=state.proposals,
        decision=FieldDecisionKind.USE_PROPOSAL,
        selected_proposal=proposal,
        manual_value=None,
        decision_origin=DecisionOrigin.USER,
        requires_review=False,
        reason_codes=_user_reasons(state, ReviewReasonCode.PROPOSAL_SELECTED),
    )


def set_manual_decision(state: FieldReviewState, value: FieldValue) -> FieldReviewState:
    """Record an explicit non-empty manual value while preserving proposals."""
    if not isinstance(state, FieldReviewState):
        raise TypeError("state must be a FieldReviewState")

    return FieldReviewState(
        field=state.field,
        read_state=state.read_state,
        existing_value=state.existing_value,
        proposals=state.proposals,
        decision=FieldDecisionKind.USE_MANUAL,
        selected_proposal=None,
        manual_value=value,
        decision_origin=DecisionOrigin.USER,
        requires_review=False,
        reason_codes=_user_reasons(state, ReviewReasonCode.MANUAL_VALUE_SELECTED),
    )


def set_clear_decision(state: FieldReviewState) -> FieldReviewState:
    """Record an explicit clear action without encoding it as an empty value."""
    if not isinstance(state, FieldReviewState):
        raise TypeError("state must be a FieldReviewState")

    return FieldReviewState(
        field=state.field,
        read_state=state.read_state,
        existing_value=state.existing_value,
        proposals=state.proposals,
        decision=FieldDecisionKind.CLEAR,
        selected_proposal=None,
        manual_value=None,
        decision_origin=DecisionOrigin.USER,
        requires_review=False,
        reason_codes=_user_reasons(state, ReviewReasonCode.CLEAR_SELECTED),
    )


def _same_consolidated_value(
    left: ConsolidatedProposal,
    right: ConsolidatedProposal,
) -> bool:
    return (
        left.field is right.field
        and _value_key(left.value) == _value_key(right.value)
        and _canonical_language(left.language) == _canonical_language(right.language)
        and _canonical_script(left.script) == _canonical_script(right.script)
        and left.members == right.members
    )


def rerank_field_review_state(
    state: FieldReviewState,
    *,
    preferred_language: str,
    local_texts: Sequence[str | None] = (),
    language_profile_texts: Sequence[str | None] | None = None,
) -> FieldReviewState:
    """Rebuild proposal order while preserving explicit user decisions."""
    if not isinstance(state, FieldReviewState):
        raise TypeError("state must be a FieldReviewState")

    source_proposals = tuple(
        member
        for proposal in state.proposals
        for member in proposal.members
    )
    ranking = rank_proposals(
        consolidate_proposals(source_proposals),
        preferred_language=preferred_language,
        local_texts=local_texts,
        language_profile_texts=language_profile_texts,
    )

    # A language edit can recompute defaults, but an explicit human decision
    # survives. Reattach its selected value to the newly ranked proposal object.
    if state.decision_origin is DecisionOrigin.DEFAULT:
        return _hybrid_state(
            field=state.field,
            read_state=state.read_state,
            existing_value=state.existing_value,
            proposals=ranking.proposals,
            extra_reason_codes=ranking.reason_codes,
            proposal_choice_ambiguous=ranking.ambiguous,
        )

    selected_proposal = None

    if state.selected_proposal is not None:
        selected_proposal = next(
            (
                proposal
                for proposal in ranking.proposals
                if _same_consolidated_value(proposal, state.selected_proposal)
            ),
            None,
        )

        if selected_proposal is None:
            raise ValueError("the selected proposal disappeared during deterministic reranking")

    retained_reasons = tuple(
        code
        for code in state.reason_codes
        if code not in _LANGUAGE_CODES and code is not ReviewReasonCode.USER_DECISION_PRESERVED
    )
    reasons = _ordered_reason_union(
        retained_reasons,
        ranking.reason_codes,
        (ReviewReasonCode.USER_DECISION_PRESERVED,),
    )

    return FieldReviewState(
        field=state.field,
        read_state=state.read_state,
        existing_value=state.existing_value,
        proposals=ranking.proposals,
        decision=state.decision,
        selected_proposal=selected_proposal,
        manual_value=state.manual_value,
        decision_origin=DecisionOrigin.USER,
        requires_review=state.requires_review,
        reason_codes=reasons,
    )
