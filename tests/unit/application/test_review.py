# Review policy combines read state, field confidence and language compatibility.
# Vary those dimensions independently so a strong release cannot hide field ambiguity.

from dataclasses import FrozenInstanceError
from itertools import permutations

import pytest

from metadata_polisher.application.review import (
    build_field_review_state,
    consolidate_proposals,
    rank_proposals,
    rerank_field_review_state,
    set_clear_decision,
    set_keep_existing_decision,
    set_manual_decision,
    set_proposal_decision,
)
from metadata_polisher.domain.matching import MetadataProvenance
from metadata_polisher.domain.metadata import FieldReadState, MetadataField, Position
from metadata_polisher.domain.review import (
    DecisionOrigin,
    FieldConfidence,
    FieldDecisionKind,
    FieldProposal,
    ReviewReasonCode,
)


def provenance(
    source_id: str = "catalogue-a",
    *,
    engine_id: str | None = None,
    language: str | None = None,
) -> MetadataProvenance:
    # Allow two engines to name one underlying source; duplicate routes to the
    # same catalogue must not be counted as independent provider agreement.
    return MetadataProvenance(
        engine_id=engine_id or source_id,
        source_id=source_id,
        record_id=f"record-{source_id}",
        source_url=f"https://catalogue.invalid/{source_id}",
        language=language,
        operation_id="LOOKUP-0001",
    )


def proposal(
    value: str,
    *,
    confidence: FieldConfidence = FieldConfidence.HIGH,
    source_id: str = "catalogue-a",
    language: str | None = "eng",
    script: str | None = "Latn",
) -> FieldProposal:
    return FieldProposal(
        field=MetadataField.TITLE,
        value=value,
        confidence=confidence,
        provenance=provenance(source_id, language=language),
        language=language,
        script=script,
        reason_codes=(ReviewReasonCode.FIELD_MATCH_HIGH,),
    )


def composer_proposal(
    value: tuple[str, ...],
    *,
    source_id: str,
    language: str,
    script: str,
) -> FieldProposal:
    return FieldProposal(
        field=MetadataField.COMPOSERS,
        value=value,
        confidence=FieldConfidence.HIGH,
        provenance=provenance(source_id, language=language),
        language=language,
        script=script,
        reason_codes=(ReviewReasonCode.FIELD_MATCH_HIGH,),
    )


@pytest.mark.parametrize(
    (
        "read_state",
        "existing_value",
        "field_proposals",
        "expected_decision",
        "expected_review",
        "expected_reasons",
    ),
    (
        (
            FieldReadState.MISSING,
            None,
            (proposal("Proposed"),),
            FieldDecisionKind.USE_PROPOSAL,
            False,
            {ReviewReasonCode.EXISTING_VALUE_MISSING, ReviewReasonCode.PROPOSAL_CONFIDENT},
        ),
        (
            FieldReadState.MISSING,
            None,
            (proposal("First"), proposal("Second", source_id="catalogue-b")),
            FieldDecisionKind.UNRESOLVED,
            True,
            {ReviewReasonCode.EXISTING_VALUE_MISSING, ReviewReasonCode.PROPOSAL_AMBIGUOUS},
        ),
        (
            FieldReadState.PRESENT,
            "  existing TITLE ",
            (proposal("Existing title"),),
            FieldDecisionKind.KEEP_EXISTING,
            False,
            {ReviewReasonCode.EXISTING_VALUE_EQUIVALENT},
        ),
        (
            FieldReadState.PRESENT,
            "Existing",
            (proposal("Different"),),
            FieldDecisionKind.KEEP_EXISTING,
            True,
            {ReviewReasonCode.EXISTING_VALUE_DIFFERENT, ReviewReasonCode.PROPOSAL_CONFIDENT},
        ),
        (
            FieldReadState.PRESENT,
            "Existing",
            (proposal("First"), proposal("Second", source_id="catalogue-b")),
            FieldDecisionKind.KEEP_EXISTING,
            True,
            {ReviewReasonCode.EXISTING_VALUE_DIFFERENT, ReviewReasonCode.PROPOSAL_AMBIGUOUS},
        ),
        (
            FieldReadState.UNREADABLE,
            "Preserve raw value",
            (proposal("Proposed"),),
            FieldDecisionKind.KEEP_EXISTING,
            True,
            {ReviewReasonCode.EXISTING_VALUE_UNREADABLE},
        ),
        (
            FieldReadState.UNSUPPORTED,
            None,
            (proposal("Proposed"),),
            FieldDecisionKind.KEEP_EXISTING,
            True,
            {ReviewReasonCode.EXISTING_VALUE_UNSUPPORTED},
        ),
        (
            FieldReadState.PRESENT,
            "Existing",
            (),
            FieldDecisionKind.KEEP_EXISTING,
            False,
            {ReviewReasonCode.NO_PROPOSAL},
        ),
        (
            FieldReadState.MISSING,
            None,
            (),
            FieldDecisionKind.KEEP_EXISTING,
            False,
            {ReviewReasonCode.EXISTING_VALUE_MISSING, ReviewReasonCode.NO_PROPOSAL},
        ),
    ),
    ids=(
        "missing-one-confident",
        "missing-ambiguous",
        "present-equivalent",
        "present-different-confident",
        "present-different-ambiguous",
        "unreadable-any",
        "unsupported-any",
        "present-no-proposal",
        "missing-no-proposal",
    ),
)
def test_default_hybrid_policy_covers_each_design_table_row(
    read_state: FieldReadState,
    existing_value: str | None,
    field_proposals: tuple[FieldProposal, ...],
    expected_decision: FieldDecisionKind,
    expected_review: bool,
    expected_reasons: set[ReviewReasonCode],
) -> None:
    state = build_field_review_state(
        field=MetadataField.TITLE,
        read_state=read_state,
        existing_value=existing_value,
        proposals=field_proposals,
    )

    assert state.decision is expected_decision
    assert state.decision_origin is DecisionOrigin.DEFAULT
    assert state.requires_review is expected_review
    assert expected_reasons <= set(state.reason_codes)
    assert (state.selected_proposal is not None) is (expected_decision is FieldDecisionKind.USE_PROPOSAL)


def test_missing_non_confident_proposal_remains_unresolved() -> None:
    state = build_field_review_state(
        field=MetadataField.TITLE,
        read_state=FieldReadState.MISSING,
        existing_value=None,
        proposals=(proposal("Tentative", confidence=FieldConfidence.REVIEW),),
    )

    assert state.decision is FieldDecisionKind.UNRESOLVED
    assert state.requires_review is True
    assert ReviewReasonCode.PROPOSAL_NOT_CONFIDENT in state.reason_codes


def test_review_models_are_frozen() -> None:
    value = proposal("Immutable")

    with pytest.raises(FrozenInstanceError):
        value.value = "Changed"  # type: ignore[misc]


def test_equivalent_japanese_values_consolidate_and_retain_both_provider_provenances() -> None:
    first = composer_proposal(
        ("久石 譲",),
        source_id="musicbrainz",
        language="jpn",
        script="Jpan",
    )
    second = composer_proposal(
        ("久石\u3000譲",),
        source_id="vgmdb",
        language="ja",
        script="Japanese",
    )

    forwards = consolidate_proposals((first, second))
    backwards = consolidate_proposals((second, first))

    assert forwards == backwards
    assert len(forwards) == 1
    assert forwards[0].language == "ja"
    assert forwards[0].script == "jpan"
    assert tuple(item.source_id for item in forwards[0].provenances) == (
        "musicbrainz",
        "vgmdb",
    )
    assert ReviewReasonCode.PROVIDER_AGREEMENT in forwards[0].reason_codes


def test_native_and_romanised_japanese_values_remain_separate_proposals() -> None:
    native = composer_proposal(
        ("久石譲",),
        source_id="catalogue-a",
        language="jpn",
        script="Jpan",
    )
    romanised = composer_proposal(
        ("Joe Hisaishi",),
        source_id="catalogue-a",
        language="ja",
        script="Latn",
    )

    consolidated = consolidate_proposals((romanised, native))

    assert len(consolidated) == 2
    assert {item.script for item in consolidated} == {"jpan", "latn"}
    assert all(ReviewReasonCode.PROVIDER_DISAGREEMENT not in item.reason_codes for item in consolidated)


def test_provider_disagreement_remains_visible_as_two_explainable_proposals() -> None:
    first = composer_proposal(
        ("Alice Example",),
        source_id="catalogue-a",
        language="eng",
        script="Latn",
    )
    second = composer_proposal(
        ("Bob Example",),
        source_id="catalogue-b",
        language="en",
        script="latin",
    )

    consolidated = consolidate_proposals((first, second))

    assert tuple(item.value for item in consolidated) == (("Alice Example",), ("Bob Example",))
    assert all(ReviewReasonCode.PROVIDER_DISAGREEMENT in item.reason_codes for item in consolidated)


def test_auto_language_ranking_prefers_native_japanese_for_a_japanese_profile() -> None:
    japanese = proposal("星のカービィ", source_id="catalogue-a", language="jpn", script="Jpan")
    english = proposal("Kirby", source_id="catalogue-b", language="eng", script="Latn")

    ranking = rank_proposals(
        consolidate_proposals((english, japanese)),
        preferred_language="auto",
        local_texts=("星のカービィ",),
    )

    assert ranking.proposals[0].value == "星のカービィ"
    assert ranking.effective_language == "ja"
    assert ranking.ambiguous is False
    assert ReviewReasonCode.LANGUAGE_MATCH in ranking.proposals[0].reason_codes


def test_explicit_english_override_supersedes_an_auto_japanese_profile() -> None:
    japanese = proposal("星のカービィ", source_id="catalogue-a", language="jpn", script="Jpan")
    english = proposal("Kirby", source_id="catalogue-b", language="eng", script="Latn")

    ranking = rank_proposals(
        consolidate_proposals((japanese, english)),
        preferred_language="English",
        local_texts=("星のカービィ",),
    )

    assert ranking.proposals[0].value == "Kirby"
    assert ranking.effective_language == "en"
    assert ranking.ambiguous is False
    assert ReviewReasonCode.LANGUAGE_OVERRIDE in ranking.reason_codes
    assert ReviewReasonCode.LANGUAGE_MATCH in ranking.proposals[0].reason_codes


@pytest.mark.parametrize(
    ("native_album_text", "expected_language", "romanised_value", "english_value"),
    (
        ("星のカービィ", "ja", "Hoshi no Kirby", "Kirby"),
        ("별의 커비", "ko", "Byeorui Keobi", "Kirby"),
    ),
)
def test_romanised_override_uses_album_profile_to_distinguish_language(
    native_album_text: str,
    expected_language: str,
    romanised_value: str,
    english_value: str,
) -> None:
    romanised = proposal(
        romanised_value,
        source_id="catalogue-a",
        language=expected_language,
        script="Latn",
    )
    english = proposal(
        english_value,
        source_id="catalogue-b",
        language="en",
        script="Latn",
    )

    ranking = rank_proposals(
        consolidate_proposals((english, romanised)),
        preferred_language="romanised",
        # Same-field similarity deliberately favours English. The separate
        # album profile supplies the language evidence for the override.
        local_texts=(english_value,),
        language_profile_texts=(native_album_text,),
    )

    assert ranking.proposals[0].value == romanised_value
    assert ranking.effective_language == expected_language
    assert ranking.effective_script == "latn"
    assert ranking.ambiguous is False
    assert ReviewReasonCode.LANGUAGE_MATCH in ranking.proposals[0].reason_codes
    assert ReviewReasonCode.LANGUAGE_MATCH not in ranking.proposals[1].reason_codes


@pytest.mark.parametrize(
    "album_profile_texts",
    (
        ("Kirby",),
        ("星之卡比",),
        (),
        ("星のカービィ", "별의 커비"),
    ),
)
def test_romanised_override_stays_unavailable_without_one_strong_album_language(
    album_profile_texts: tuple[str, ...],
) -> None:
    romanised_japanese = proposal(
        "Hoshi no Kirby",
        source_id="catalogue-a",
        language="ja",
        script="Latn",
    )
    english = proposal(
        "Kirby",
        source_id="catalogue-b",
        language="en",
        script="Latn",
    )

    ranking = rank_proposals(
        consolidate_proposals((romanised_japanese, english)),
        preferred_language="romanised",
        language_profile_texts=album_profile_texts,
    )

    assert ranking.effective_language is None
    assert ranking.effective_script is None
    assert ranking.ambiguous is True
    assert ReviewReasonCode.LANGUAGE_UNAVAILABLE in ranking.reason_codes
    assert all(
        ReviewReasonCode.LANGUAGE_MATCH not in item.reason_codes
        for item in ranking.proposals
    )


def test_han_only_auto_profile_remains_ambiguous_and_does_not_guess_japanese() -> None:
    chinese = proposal("星之卡比", source_id="catalogue-a", language="zho", script="Hans")
    japanese = proposal("星のカービィ", source_id="catalogue-b", language="jpn", script="Jpan")

    ranking = rank_proposals(
        consolidate_proposals((japanese, chinese)),
        preferred_language="auto",
        local_texts=("星之卡比",),
    )

    assert ranking.proposals[0].value == "星之卡比"
    assert ranking.effective_language is None
    assert ranking.ambiguous is True
    assert ReviewReasonCode.LANGUAGE_PROFILE_AMBIGUOUS in ranking.reason_codes


def test_latin_script_does_not_implicitly_select_english() -> None:
    english = proposal("Kirby", source_id="catalogue-a", language="eng", script="Latn")
    romanised_japanese = proposal(
        "Hoshi no Kirby",
        source_id="catalogue-b",
        language="jpn",
        script="Latn",
    )

    ranking = rank_proposals(
        consolidate_proposals((romanised_japanese, english)),
        preferred_language="auto",
        local_texts=("Kirby soundtrack",),
    )

    assert ranking.effective_language is None
    assert ranking.ambiguous is True
    assert ReviewReasonCode.LANGUAGE_UNAVAILABLE in ranking.reason_codes


def test_han_only_language_ambiguity_keeps_a_single_missing_proposal_reviewable() -> None:
    state = build_field_review_state(
        field=MetadataField.TITLE,
        read_state=FieldReadState.MISSING,
        existing_value=None,
        proposals=(proposal("星之卡比", language="zho", script="Hans"),),
        preferred_language="auto",
        local_texts=("星之卡比",),
    )

    assert state.decision is FieldDecisionKind.UNRESOLVED
    assert state.requires_review is True
    assert ReviewReasonCode.LANGUAGE_PROFILE_AMBIGUOUS in state.reason_codes


def test_reranking_rebuilds_default_proposal_order_without_mutating_old_state() -> None:
    japanese = proposal("星のカービィ", source_id="catalogue-a", language="jpn", script="Jpan")
    english = proposal("Kirby", source_id="catalogue-b", language="eng", script="Latn")
    auto_state = build_field_review_state(
        field=MetadataField.TITLE,
        read_state=FieldReadState.MISSING,
        existing_value=None,
        proposals=(english, japanese),
        preferred_language="auto",
        local_texts=("星のカービィ",),
    )

    english_state = rerank_field_review_state(
        auto_state,
        preferred_language="eng",
        local_texts=("星のカービィ",),
    )

    assert auto_state.proposals[0].value == "星のカービィ"
    assert english_state.proposals[0].value == "Kirby"
    assert english_state is not auto_state
    assert english_state.decision is FieldDecisionKind.UNRESOLVED


def test_manual_decision_and_later_ranking_preserve_value_and_provider_proposals() -> None:
    japanese = proposal("星のカービィ", source_id="catalogue-a", language="jpn", script="Jpan")
    english = proposal("Kirby", source_id="catalogue-b", language="eng", script="Latn")
    original = build_field_review_state(
        field=MetadataField.TITLE,
        read_state=FieldReadState.PRESENT,
        existing_value="Old title",
        proposals=(english, japanese),
        preferred_language="auto",
        local_texts=("星のカービィ",),
    )

    manual = set_manual_decision(original, "My reviewed title")
    reranked = rerank_field_review_state(
        manual,
        preferred_language="English",
        local_texts=("星のカービィ",),
    )

    assert original.decision is FieldDecisionKind.KEEP_EXISTING
    assert manual.proposals is original.proposals
    assert manual.decision is FieldDecisionKind.USE_MANUAL
    assert manual.decision_origin is DecisionOrigin.USER
    assert manual.manual_value == "My reviewed title"
    assert manual.requires_review is False
    assert reranked.decision is FieldDecisionKind.USE_MANUAL
    assert reranked.manual_value == "My reviewed title"
    assert reranked.proposals[0].value == "Kirby"
    assert tuple(member for item in reranked.proposals for member in item.members) == (english, japanese)
    assert ReviewReasonCode.USER_DECISION_PRESERVED in reranked.reason_codes


def test_clear_decision_is_explicit_and_survives_language_ranking() -> None:
    japanese = proposal("星のカービィ", source_id="catalogue-a", language="jpn", script="Jpan")
    english = proposal("Kirby", source_id="catalogue-b", language="eng", script="Latn")
    original = build_field_review_state(
        field=MetadataField.TITLE,
        read_state=FieldReadState.PRESENT,
        existing_value="Old title",
        proposals=(japanese, english),
    )

    cleared = set_clear_decision(original)
    reranked = rerank_field_review_state(
        cleared,
        preferred_language="English",
        local_texts=("星のカービィ",),
    )

    assert cleared.decision is FieldDecisionKind.CLEAR
    assert cleared.decision_origin is DecisionOrigin.USER
    assert cleared.manual_value is None
    assert cleared.selected_proposal is None
    assert cleared.proposals is original.proposals
    assert reranked.decision is FieldDecisionKind.CLEAR
    assert reranked.proposals[0].value == "Kirby"


def test_user_can_explicitly_keep_existing_or_select_a_visible_proposal() -> None:
    state = build_field_review_state(
        field=MetadataField.TITLE,
        read_state=FieldReadState.PRESENT,
        existing_value="Old title",
        proposals=(proposal("New title"),),
    )

    kept = set_keep_existing_decision(state)
    selected = set_proposal_decision(state, state.proposals[0])

    assert kept.decision is FieldDecisionKind.KEEP_EXISTING
    assert kept.decision_origin is DecisionOrigin.USER
    assert kept.requires_review is False
    assert selected.decision is FieldDecisionKind.USE_PROPOSAL
    assert selected.selected_proposal == state.proposals[0]
    assert selected.decision_origin is DecisionOrigin.USER
    assert selected.requires_review is False


def test_review_boundaries_reject_unordered_or_field_incompatible_values() -> None:
    item = proposal("Title")

    with pytest.raises(TypeError, match="ordered sequence"):
        consolidate_proposals({item})  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="title must be a string"):
        FieldProposal(
            field=MetadataField.TITLE,
            value=("Not a scalar title",),
            confidence=FieldConfidence.HIGH,
            provenance=provenance(),
        )

    with pytest.raises(TypeError, match="local_texts must be an ordered sequence"):
        rank_proposals(consolidate_proposals((item,)), local_texts={"Title"})  # type: ignore[arg-type]

    artists = FieldProposal(
        field=MetadataField.ARTISTS,
        value=("First", "Second"),
        confidence=FieldConfidence.HIGH,
        provenance=provenance(),
    )
    mutable_value = ["First", "Second"]
    copied = FieldProposal(
        field=MetadataField.ARTISTS,
        value=mutable_value,  # type: ignore[arg-type]
        confidence=FieldConfidence.HIGH,
        provenance=provenance(),
    )
    mutable_value.append("Third")

    assert copied.value == artists.value


def test_review_boundaries_reject_invalid_state_and_accidental_empty_manual_values() -> None:
    with pytest.raises(ValueError, match="missing field cannot have an existing value"):
        build_field_review_state(
            field=MetadataField.TITLE,
            read_state=FieldReadState.MISSING,
            existing_value="Contradiction",
            proposals=(),
        )

    state = build_field_review_state(
        field=MetadataField.TRACK,
        read_state=FieldReadState.PRESENT,
        existing_value=Position(number=1, total=2),
        proposals=(),
    )

    with pytest.raises(TypeError, match="must be a Position"):
        set_manual_decision(state, "1/2")

    title_state = build_field_review_state(
        field=MetadataField.TITLE,
        read_state=FieldReadState.PRESENT,
        existing_value="Title",
        proposals=(),
    )

    with pytest.raises(ValueError, match="cannot be blank"):
        set_manual_decision(title_state, "   ")


def test_agreement_requires_distinct_underlying_sources_not_two_engines() -> None:
    first = FieldProposal(
        field=MetadataField.TITLE,
        value="Same title",
        confidence=FieldConfidence.HIGH,
        provenance=provenance("shared-source", engine_id="direct"),
    )
    second = FieldProposal(
        field=MetadataField.TITLE,
        value="Same title",
        confidence=FieldConfidence.HIGH,
        provenance=provenance("shared-source", engine_id="aggregator"),
    )

    consolidated = consolidate_proposals((first, second))

    assert len(consolidated) == 1
    assert len(consolidated[0].provenances) == 2
    assert ReviewReasonCode.PROVIDER_AGREEMENT not in consolidated[0].reason_codes


def test_position_proposals_have_a_total_order_when_optional_components_differ() -> None:
    total_only = FieldProposal(
        field=MetadataField.TRACK,
        value=Position(number=None, total=12),
        confidence=FieldConfidence.HIGH,
        provenance=provenance("catalogue-a"),
    )
    numbered = FieldProposal(
        field=MetadataField.TRACK,
        value=Position(number=1, total=12),
        confidence=FieldConfidence.HIGH,
        provenance=provenance("catalogue-b"),
    )

    forwards = consolidate_proposals((total_only, numbered))
    backwards = consolidate_proposals((numbered, total_only))

    assert forwards == backwards
    assert tuple(item.value for item in forwards) == (
        Position(number=None, total=12),
        Position(number=1, total=12),
    )


def test_consolidation_is_permutation_deterministic_across_every_raw_member_detail() -> None:
    first = FieldProposal(
        field=MetadataField.TITLE,
        value="星のカービィ",
        confidence=FieldConfidence.HIGH,
        provenance=MetadataProvenance(
            engine_id="shared-engine",
            source_id="shared-source",
            record_id="shared-record",
            source_url="https://catalogue.invalid/b",
            language="jpn",
            operation_id="LOOKUP-0001",
        ),
        language="jpn",
        script="Jpan",
        reason_codes=(ReviewReasonCode.FIELD_MATCH_REVIEW,),
    )
    second = FieldProposal(
        field=MetadataField.TITLE,
        value="星のカービィ",
        confidence=FieldConfidence.HIGH,
        provenance=MetadataProvenance(
            engine_id="shared-engine",
            source_id="shared-source",
            record_id="shared-record",
            source_url="https://catalogue.invalid/a",
            language="ja",
            operation_id="LOOKUP-0001",
        ),
        language="ja",
        script="Japanese",
        reason_codes=(ReviewReasonCode.FIELD_MATCH_HIGH,),
    )
    expected = consolidate_proposals((first, second))

    assert all(consolidate_proposals(order) == expected for order in permutations((first, second)))


@pytest.mark.parametrize(
    ("value", "expected_script"),
    (
        ("星のカービィ", "jpan"),
        ("별의 커비", "hang"),
        ("星之卡比", "han"),
        ("Kirby", "latn"),
    ),
)
def test_missing_script_is_inferred_conservatively_without_inventing_language(
    value: str,
    expected_script: str,
) -> None:
    item = proposal(value, language=None, script=None)

    consolidated = consolidate_proposals((item,))

    assert consolidated[0].script == expected_script
    assert consolidated[0].language is None


def test_inferred_and_explicit_equivalent_scripts_consolidate_without_wildcards() -> None:
    inferred = proposal(
        "星のカービィ",
        source_id="catalogue-a",
        language="jpn",
        script=None,
    )
    explicit = proposal(
        "星のカービィ",
        source_id="catalogue-b",
        language="ja",
        script="Jpan",
    )

    consolidated = consolidate_proposals((inferred, explicit))

    assert len(consolidated) == 1
    assert consolidated[0].script == "jpan"
    assert len(consolidated[0].provenances) == 2


def test_inferred_script_preserves_native_and_romanised_japanese_separation() -> None:
    native = proposal(
        "星のカービィ",
        source_id="catalogue-a",
        language="jpn",
        script=None,
    )
    romanised = proposal(
        "Hoshi no Kirby",
        source_id="catalogue-a",
        language="ja",
        script=None,
    )

    consolidated = consolidate_proposals((native, romanised))

    assert len(consolidated) == 2
    assert {item.script for item in consolidated} == {"jpan", "latn"}


def test_auto_japanese_does_not_auto_use_an_incompatible_english_proposal() -> None:
    state = build_field_review_state(
        field=MetadataField.TITLE,
        read_state=FieldReadState.MISSING,
        existing_value=None,
        proposals=(proposal("Kirby", language="eng", script="Latn"),),
        preferred_language="auto",
        local_texts=("星のカービィ",),
    )

    assert state.decision is FieldDecisionKind.UNRESOLVED
    assert state.selected_proposal is None
    assert state.requires_review is True
    assert ReviewReasonCode.LANGUAGE_UNAVAILABLE in state.reason_codes


def test_explicit_english_does_not_auto_use_an_only_japanese_proposal() -> None:
    state = build_field_review_state(
        field=MetadataField.TITLE,
        read_state=FieldReadState.MISSING,
        existing_value=None,
        proposals=(proposal("星のカービィ", language="jpn", script="Jpan"),),
        preferred_language="English",
    )

    assert state.decision is FieldDecisionKind.UNRESOLVED
    assert state.selected_proposal is None
    assert state.requires_review is True
    assert ReviewReasonCode.LANGUAGE_OVERRIDE in state.reason_codes
    assert ReviewReasonCode.LANGUAGE_UNAVAILABLE in state.reason_codes


def test_language_match_requires_both_native_japanese_language_and_script() -> None:
    romanised = proposal(
        "Hoshi no Kirby",
        language="jpn",
        script="Latn",
    )

    ranking = rank_proposals(
        consolidate_proposals((romanised,)),
        preferred_language="auto",
        local_texts=("星のカービィ",),
    )

    assert ranking.ambiguous is True
    assert ReviewReasonCode.LANGUAGE_UNAVAILABLE in ranking.reason_codes
    assert ReviewReasonCode.LANGUAGE_MATCH not in ranking.proposals[0].reason_codes


def test_japanese_language_with_han_script_is_compatible_with_native_japanese() -> None:
    kanji_only = proposal(
        "久石譲",
        language="ja",
        script="Hani",
    )

    state = build_field_review_state(
        field=MetadataField.TITLE,
        read_state=FieldReadState.MISSING,
        existing_value=None,
        proposals=(kanji_only,),
        preferred_language="ja",
    )

    assert state.decision is FieldDecisionKind.USE_PROPOSAL
    assert state.selected_proposal is not None
    assert ReviewReasonCode.LANGUAGE_UNAVAILABLE not in state.reason_codes
    assert ReviewReasonCode.LANGUAGE_MATCH in state.selected_proposal.reason_codes


@pytest.mark.parametrize("preferred_language", ["auto", "en", "ja"])
@pytest.mark.parametrize(
    ("field", "value"),
    [
        (MetadataField.TRACK, Position(number=1, total=12)),
        (MetadataField.DISC, Position(number=2, total=3)),
        (MetadataField.DATE, "2024-03-01"),
    ],
)
def test_language_neutral_confident_additions_survive_language_reranking(
    preferred_language: str,
    field: MetadataField,
    value: Position | str,
) -> None:
    item = FieldProposal(field=field, value=value, confidence=FieldConfidence.HIGH, provenance=provenance())
    state = build_field_review_state(
        field=field,
        read_state=FieldReadState.MISSING,
        existing_value=None,
        proposals=(item,),
        preferred_language=preferred_language,
        language_profile_texts=("冒険のアルバム",),
    )

    assert state.decision is FieldDecisionKind.USE_PROPOSAL
    assert state.selected_proposal is not None
    assert state.selected_proposal.value == value
    assert state.requires_review is False
    assert ReviewReasonCode.PROPOSAL_AMBIGUOUS not in state.reason_codes
    assert ReviewReasonCode.LANGUAGE_UNAVAILABLE not in state.reason_codes

    # A language change cannot turn the same verified numeric/date evidence
    # into a missing value or alter the provenance recorded for a later Apply.
    for target_language in ("en", "ja", "auto"):
        reranked = rerank_field_review_state(
            state,
            preferred_language=target_language,
            language_profile_texts=("冒険のアルバム",),
        )
        assert reranked.decision is FieldDecisionKind.USE_PROPOSAL
        assert reranked.selected_proposal is not None
        assert reranked.selected_proposal.value == value
        assert reranked.selected_proposal.provenances == (item.provenance,)


@pytest.mark.parametrize(
    ("field", "first", "second"),
    [
        (MetadataField.TRACK, Position(1, 12), Position(2, 12)),
        (MetadataField.DISC, Position(1, 3), Position(2, 3)),
        (MetadataField.DATE, "2024", "2025"),
    ],
)
def test_language_neutral_conflicting_additions_still_require_review(
    field: MetadataField,
    first: Position | str,
    second: Position | str,
) -> None:
    proposals = tuple(
        FieldProposal(field=field, value=value, confidence=FieldConfidence.HIGH, provenance=provenance(source))
        for source, value in (("first", first), ("second", second))
    )
    state = build_field_review_state(
        field=field,
        read_state=FieldReadState.MISSING,
        existing_value=None,
        proposals=proposals,
        preferred_language="ja",
        language_profile_texts=("冒険のアルバム",),
    )

    assert state.decision is FieldDecisionKind.UNRESOLVED
    assert state.requires_review is True
    assert len(state.proposals) == 2
    assert ReviewReasonCode.PROPOSAL_AMBIGUOUS in state.reason_codes


def test_language_neutral_low_confidence_addition_still_requires_review() -> None:
    state = build_field_review_state(
        field=MetadataField.DISC,
        read_state=FieldReadState.MISSING,
        existing_value=None,
        proposals=(FieldProposal(
            field=MetadataField.DISC, value=Position(1, 2), confidence=FieldConfidence.REVIEW, provenance=provenance(),
        ),),
        preferred_language="ja",
    )

    assert state.decision is FieldDecisionKind.UNRESOLVED
    assert ReviewReasonCode.PROPOSAL_NOT_CONFIDENT in state.reason_codes


@pytest.mark.parametrize(
    ("value", "local_profile", "preferred_language"),
    [
        (("ひかり音楽",), "冒険のアルバム", "auto"),
        (("ひかり音楽",), "冒険のアルバム", "ja"),
        (("김음악",), "모험 음악", "auto"),
        (("김음악",), "모험 음악", "ko"),
    ],
)
def test_unlabelled_native_composer_uses_unambiguous_script_without_inventing_provenance(
    value: tuple[str, ...],
    local_profile: str,
    preferred_language: str,
) -> None:
    item = FieldProposal(
        field=MetadataField.COMPOSERS, value=value, confidence=FieldConfidence.HIGH, provenance=provenance(),
    )
    state = build_field_review_state(
        field=MetadataField.COMPOSERS,
        read_state=FieldReadState.MISSING,
        existing_value=None,
        proposals=(item,),
        preferred_language=preferred_language,
        language_profile_texts=(local_profile,),
    )

    assert state.decision is FieldDecisionKind.USE_PROPOSAL
    assert state.requires_review is False
    assert state.selected_proposal is not None
    assert state.selected_proposal.value == value
    assert state.selected_proposal.language is None
    assert state.selected_proposal.members == (item,)
    assert state.selected_proposal.provenances == (item.provenance,)
    assert state.selected_proposal.provenances[0].language is None
    assert ReviewReasonCode.LANGUAGE_MATCH in state.selected_proposal.reason_codes


@pytest.mark.parametrize(
    ("value", "local_profile", "preferred_language", "language", "script"),
    [
        (("Hikari Ongaku",), "冒険のアルバム", "auto", None, None),
        (("Hikari Ongaku",), "English album", "en", None, None),
        (("久石譲",), "冒険のアルバム", "auto", None, None),
        (("久石譲",), "星空音楽", "auto", None, None),
        (("ひかり音楽",), "冒険のアルバム", "auto", "en", "Jpan"),
        (("ひかり音楽",), "冒険のアルバム", "auto", None, "Latn"),
    ],
)
def test_unlabelled_composer_ambiguity_and_declared_contradictions_remain_reviewable(
    value: tuple[str, ...],
    local_profile: str,
    preferred_language: str,
    language: str | None,
    script: str | None,
) -> None:
    item = FieldProposal(
        field=MetadataField.COMPOSERS,
        value=value,
        confidence=FieldConfidence.HIGH,
        provenance=provenance(language=language),
        language=language,
        script=script,
    )
    state = build_field_review_state(
        field=MetadataField.COMPOSERS,
        read_state=FieldReadState.MISSING,
        existing_value=None,
        proposals=(item,),
        preferred_language=preferred_language,
        language_profile_texts=(local_profile,),
    )

    assert state.decision is FieldDecisionKind.UNRESOLVED
    assert state.requires_review is True
    assert state.selected_proposal is None
    assert state.proposals[0].members == (item,)
