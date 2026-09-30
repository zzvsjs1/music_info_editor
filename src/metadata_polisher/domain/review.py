"""Immutable field proposals and user-review decisions."""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from metadata_polisher.domain.matching import ComposerCredit, MetadataProvenance
from metadata_polisher.domain.metadata import FieldReadState, MetadataField, Position
from metadata_polisher.domain.metadata import FieldValue as FieldValue


class FieldDecisionKind(StrEnum):
    """The explicit resolution chosen for one managed metadata field."""

    KEEP_EXISTING = "keep_existing"
    USE_PROPOSAL = "use_proposal"
    USE_MANUAL = "use_manual"
    CLEAR = "clear"
    UNRESOLVED = "unresolved"


class FieldConfidence(StrEnum):
    """Field-specific confidence independent of the release match class."""

    HIGH = "high"
    REVIEW = "review"
    LOW = "low"


class DecisionOrigin(StrEnum):
    """Whether a decision came from policy or an explicit user action."""

    DEFAULT = "default"
    USER = "user"


class ReviewReasonCode(StrEnum):
    """Stable evidence codes explaining proposal and review decisions."""

    FIELD_MATCH_HIGH = "FIELD_MATCH_HIGH"
    FIELD_MATCH_REVIEW = "FIELD_MATCH_REVIEW"
    FIELD_MATCH_LOW = "FIELD_MATCH_LOW"
    PROVIDER_AGREEMENT = "PROVIDER_AGREEMENT"
    PROVIDER_DISAGREEMENT = "PROVIDER_DISAGREEMENT"
    LANGUAGE_MATCH = "LANGUAGE_MATCH"
    LANGUAGE_OVERRIDE = "LANGUAGE_OVERRIDE"
    LANGUAGE_PROFILE_AMBIGUOUS = "LANGUAGE_PROFILE_AMBIGUOUS"
    LANGUAGE_UNAVAILABLE = "LANGUAGE_UNAVAILABLE"
    EXISTING_VALUE_MISSING = "EXISTING_VALUE_MISSING"
    EXISTING_VALUE_EQUIVALENT = "EXISTING_VALUE_EQUIVALENT"
    EXISTING_VALUE_DIFFERENT = "EXISTING_VALUE_DIFFERENT"
    EXISTING_VALUE_UNREADABLE = "EXISTING_VALUE_UNREADABLE"
    EXISTING_VALUE_UNSUPPORTED = "EXISTING_VALUE_UNSUPPORTED"
    PROPOSAL_CONFIDENT = "PROPOSAL_CONFIDENT"
    PROPOSAL_AMBIGUOUS = "PROPOSAL_AMBIGUOUS"
    PROPOSAL_NOT_CONFIDENT = "PROPOSAL_NOT_CONFIDENT"
    NO_PROPOSAL = "NO_PROPOSAL"
    KEEP_EXISTING_SELECTED = "KEEP_EXISTING_SELECTED"
    PROPOSAL_SELECTED = "PROPOSAL_SELECTED"
    MANUAL_VALUE_SELECTED = "MANUAL_VALUE_SELECTED"
    CLEAR_SELECTED = "CLEAR_SELECTED"
    USER_DECISION_PRESERVED = "USER_DECISION_PRESERVED"
    CANDIDATE_DEPENDENCY_CHANGED = "CANDIDATE_DEPENDENCY_CHANGED"


_STRING_FIELDS = frozenset((MetadataField.TITLE, MetadataField.ALBUM, MetadataField.DATE))
_SEQUENCE_FIELDS = frozenset(
    (
        MetadataField.ARTISTS,
        MetadataField.ALBUM_ARTISTS,
        MetadataField.COMPOSERS,
        MetadataField.GENRES,
    )
)
_POSITION_FIELDS = frozenset((MetadataField.TRACK, MetadataField.DISC))


def _validate_enum(name: str, value: object, enum_type: type[StrEnum]) -> None:
    if not isinstance(value, enum_type):
        raise TypeError(f"{name} must be a {enum_type.__name__}")


def _normalise_optional_label(name: str, value: object) -> str | None:
    if value is None:
        return None

    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string or None")

    if not value.strip():
        raise ValueError(f"{name} cannot be blank")

    return value


def _normalise_typed_tuple[T](name: str, values: object, item_type: type[T]) -> tuple[T, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise TypeError(f"{name} must be an ordered sequence")

    copied = tuple(values)

    if any(not isinstance(value, item_type) for value in copied):
        raise TypeError(f"{name} must contain only {item_type.__name__} values")

    return copied


def _normalise_reason_codes(values: object) -> tuple[ReviewReasonCode, ...]:
    codes = _normalise_typed_tuple("reason_codes", values, ReviewReasonCode)

    if len(codes) != len(set(codes)):
        raise ValueError("reason_codes must not contain duplicates")

    return codes


def _normalise_field_value(
    field: MetadataField,
    value: object,
    *,
    name: str,
    allow_none: bool,
) -> FieldValue | None:
    if value is None:
        if allow_none:
            return None

        raise ValueError(f"{name} cannot be None")

    if field in _STRING_FIELDS:
        if not isinstance(value, str):
            raise TypeError(f"{name} for {field.value} must be a string")

        if not value.strip():
            raise ValueError(f"{name} for {field.value} cannot be blank")

        return value

    # Multi-value fields keep complete, ordered names. Empty or blank values
    # are not replacement proposals; clearing requires its own decision kind.
    if field in _SEQUENCE_FIELDS:
        values = _normalise_typed_tuple(name, value, str)

        if not values:
            raise ValueError(f"{name} for {field.value} cannot be empty")

        if any(not item.strip() for item in values):
            raise ValueError(f"{name} for {field.value} cannot contain blank values")

        return values

    if field in _POSITION_FIELDS:
        if not isinstance(value, Position):
            raise TypeError(f"{name} for {field.value} must be a Position")

        if value.number is None and value.total is None:
            raise ValueError(f"{name} for {field.value} cannot be an empty Position")

        return value

    raise TypeError("field must be a MetadataField")


@dataclass(frozen=True)
class FieldProposal:
    """One provider's candidate value for one managed metadata field."""

    field: MetadataField
    value: FieldValue
    confidence: FieldConfidence
    provenance: MetadataProvenance
    language: str | None = None
    script: str | None = None
    reason_codes: tuple[ReviewReasonCode, ...] = ()
    credit_evidence: tuple[ComposerCredit, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.field, MetadataField):
            raise TypeError("field must be a MetadataField")

        value = _normalise_field_value(self.field, self.value, name="value", allow_none=False)
        _validate_enum("confidence", self.confidence, FieldConfidence)

        if not isinstance(self.provenance, MetadataProvenance):
            raise TypeError("provenance must be MetadataProvenance")

        object.__setattr__(self, "value", value)
        object.__setattr__(self, "language", _normalise_optional_label("language", self.language))
        object.__setattr__(self, "script", _normalise_optional_label("script", self.script))
        object.__setattr__(self, "reason_codes", _normalise_reason_codes(self.reason_codes))
        object.__setattr__(self, "credit_evidence", _normalise_typed_tuple(
            "credit_evidence", self.credit_evidence, ComposerCredit,
        ))


@dataclass(frozen=True)
class ConsolidatedProposal:
    """One visible equivalent value retaining every contributing proposal."""

    field: MetadataField
    value: FieldValue
    language: str | None
    script: str | None
    confidence: FieldConfidence
    members: tuple[FieldProposal, ...]
    reason_codes: tuple[ReviewReasonCode, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.field, MetadataField):
            raise TypeError("field must be a MetadataField")

        value = _normalise_field_value(self.field, self.value, name="value", allow_none=False)
        _validate_enum("confidence", self.confidence, FieldConfidence)
        members = _normalise_typed_tuple("members", self.members, FieldProposal)

        # Consolidation combines equivalent proposals for display while keeping
        # every contributor. A visible value without any member would have no
        # provenance for the reviewer to inspect.
        if not members:
            raise ValueError("members cannot be empty")

        if any(member.field is not self.field for member in members):
            raise ValueError("every member must propose the consolidated field")

        object.__setattr__(self, "value", value)
        object.__setattr__(self, "language", _normalise_optional_label("language", self.language))
        object.__setattr__(self, "script", _normalise_optional_label("script", self.script))
        object.__setattr__(self, "members", members)
        object.__setattr__(self, "reason_codes", _normalise_reason_codes(self.reason_codes))

    @property
    def provenances(self) -> tuple[MetadataProvenance, ...]:
        """Return all retained provenance records in deterministic member order."""
        return tuple(member.provenance for member in self.members)


@dataclass(frozen=True)
class ProposalRanking:
    """Deterministic proposal order plus the effective language decision."""

    proposals: tuple[ConsolidatedProposal, ...]
    effective_language: str | None
    effective_script: str | None
    ambiguous: bool
    reason_codes: tuple[ReviewReasonCode, ...]

    def __post_init__(self) -> None:
        proposals = _normalise_typed_tuple("proposals", self.proposals, ConsolidatedProposal)
        fields = {proposal.field for proposal in proposals}

        if len(fields) > 1:
            raise ValueError("ranked proposals must all belong to one field")

        if type(self.ambiguous) is not bool:
            raise TypeError("ambiguous must be a bool")

        object.__setattr__(self, "proposals", proposals)
        object.__setattr__(
            self,
            "effective_language",
            _normalise_optional_label("effective_language", self.effective_language),
        )
        object.__setattr__(
            self,
            "effective_script",
            _normalise_optional_label("effective_script", self.effective_script),
        )
        object.__setattr__(self, "reason_codes", _normalise_reason_codes(self.reason_codes))


@dataclass(frozen=True)
class FieldReviewState:
    """Complete immutable review state for one managed field."""

    field: MetadataField
    read_state: FieldReadState
    existing_value: FieldValue | None
    proposals: tuple[ConsolidatedProposal, ...]
    decision: FieldDecisionKind
    selected_proposal: ConsolidatedProposal | None
    manual_value: FieldValue | None
    decision_origin: DecisionOrigin
    requires_review: bool
    reason_codes: tuple[ReviewReasonCode, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.field, MetadataField):
            raise TypeError("field must be a MetadataField")

        if not isinstance(self.read_state, FieldReadState):
            raise TypeError("read_state must be a FieldReadState")

        existing_value = _normalise_field_value(
            self.field,
            self.existing_value,
            name="existing_value",
            allow_none=True,
        )
        proposals = _normalise_typed_tuple("proposals", self.proposals, ConsolidatedProposal)

        if any(proposal.field is not self.field for proposal in proposals):
            raise ValueError("every proposal must belong to the reviewed field")

        # Validate the read-state/value pair so widgets and services can use
        # the same facts without reconstructing what missing means.
        if self.read_state is FieldReadState.MISSING and existing_value is not None:
            raise ValueError("a missing field cannot have an existing value")

        if self.read_state is FieldReadState.PRESENT and existing_value is None:
            raise ValueError("a present field must have an existing value")

        _validate_enum("decision", self.decision, FieldDecisionKind)
        _validate_enum("decision_origin", self.decision_origin, DecisionOrigin)

        if type(self.requires_review) is not bool:
            raise TypeError("requires_review must be a bool")

        if self.selected_proposal is not None:
            if not isinstance(self.selected_proposal, ConsolidatedProposal):
                raise TypeError("selected_proposal must be ConsolidatedProposal or None")

            if self.selected_proposal not in proposals:
                raise ValueError("selected_proposal must belong to proposals")

        manual_value = _normalise_field_value(
            self.field,
            self.manual_value,
            name="manual_value",
            allow_none=True,
        )

        # A decision must carry exactly its own payload. Reject leftover manual
        # values or stale selected proposals instead of allowing them to leak
        # into a later derived ChangeSet.
        if self.decision is FieldDecisionKind.USE_PROPOSAL:
            if self.selected_proposal is None:
                raise ValueError("USE_PROPOSAL requires selected_proposal")
        elif self.selected_proposal is not None:
            raise ValueError("selected_proposal is valid only for USE_PROPOSAL")

        if self.decision is FieldDecisionKind.USE_MANUAL:
            if manual_value is None:
                raise ValueError("USE_MANUAL requires manual_value")
        elif manual_value is not None:
            raise ValueError("manual_value is valid only for USE_MANUAL")

        # Manual replacement and deletion require a recorded user decision;
        # a default policy cannot manufacture that authorisation.
        if (
            self.decision in {FieldDecisionKind.USE_MANUAL, FieldDecisionKind.CLEAR}
            and self.decision_origin is not DecisionOrigin.USER
        ):
            raise ValueError("manual and clear decisions must have USER origin")

        object.__setattr__(self, "existing_value", existing_value)
        object.__setattr__(self, "proposals", proposals)
        object.__setattr__(self, "manual_value", manual_value)
        object.__setattr__(self, "reason_codes", _normalise_reason_codes(self.reason_codes))
