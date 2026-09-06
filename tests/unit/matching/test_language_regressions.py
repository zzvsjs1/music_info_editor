"""Native regressions for substantive, contextual Kana language evidence."""

import pytest

from metadata_polisher.application.review import consolidate_proposals, rank_proposals
from metadata_polisher.domain.matching import MetadataProvenance
from metadata_polisher.domain.metadata import MetadataField
from metadata_polisher.domain.review import FieldConfidence, FieldProposal, ReviewReasonCode
from metadata_polisher.matching.language import (
    Language,
    LanguageProfileReason,
    Script,
    ScriptEvidence,
    build_language_profile,
)
from metadata_polisher.matching.normalisation import normalise_for_matching


@pytest.mark.parametrize(
    "text",
    ("・", "･", "゠", "\u3099", "\u309a", "゛", "゜", "ﾞ", "ﾟ", "ー", "ｰ", "ゝ", "ゞ", "ヽ", "ヾ"),
)
def test_punctuation_and_unattached_kana_marks_supply_no_script_evidence(text: str) -> None:
    # Several marks have Unicode letter categories, so excluding punctuation
    # alone would still let isolated prolongation or iteration imply Japanese.
    profile = build_language_profile((text,))

    assert profile.preferred_language is None
    assert profile.script_evidence == ()
    assert profile.reason is LanguageProfileReason.NO_SCRIPT_EVIDENCE
    assert profile.ambiguous is False


@pytest.mark.parametrize("text", ("AC・DC", "AC･DC", "ACーDC", "\u3099ACDC", "ACDCゝ"))
def test_kana_punctuation_and_marks_do_not_change_latin_only_profile(text: str) -> None:
    profile = build_language_profile((text,))

    assert profile.preferred_language is None
    assert profile.reason is LanguageProfileReason.LATIN_ONLY
    assert profile.script_evidence == (ScriptEvidence(Script.LATIN, 4),)


@pytest.mark.parametrize(
    ("text", "kana_count"),
    (
        ("カタカナ", 4),
        ("ひらがな", 4),
        ("ｶﾀｶﾅ", 4),
        ("ｶﾞ", 1),
        ("か\u3099", 1),
        ("か\u309a", 2),
        ("カー", 2),
        ("カーー", 3),
        ("カ・ナ", 2),
        ("カﾞ", 1),
        ("くゝ", 2),
        ("くゞ", 2),
        ("クヽ", 2),
        ("クヾ", 2),
        ("ァㇿ", 2),
    ),
)
def test_genuine_kana_and_attached_marks_preserve_normalised_character_counts(text: str, kana_count: int) -> None:
    # NFKC composes voiced forms where possible. A mark that remains a separate
    # character counts only when it continues actual Kana in the same text.
    profile = build_language_profile((text,))

    assert profile.preferred_language is Language.JAPANESE
    assert profile.reason is LanguageProfileReason.KANA_PRESENT
    assert profile.script_evidence == (ScriptEvidence(Script.KANA, kana_count),)
    assert profile.ambiguous is False


@pytest.mark.parametrize("texts", (("ーか",), ("か・ー",), ("か ー",), ("か", "ー"), ("ー", "か")))
def test_unattached_marks_do_not_borrow_kana_context_across_boundaries(texts: tuple[str, ...]) -> None:
    profile = build_language_profile(texts)

    assert profile.preferred_language is Language.JAPANESE
    assert profile.script_evidence == (ScriptEvidence(Script.KANA, 1),)
    assert build_language_profile(reversed(texts)) == profile


def test_standalone_kana_marks_do_not_resolve_han_uncertainty() -> None:
    profile = build_language_profile(("漢字ー", "ゝ"))

    assert profile.preferred_language is None
    assert profile.reason is LanguageProfileReason.HAN_WITHOUT_LANGUAGE_SIGNAL
    assert profile.script_evidence == (ScriptEvidence(Script.HAN, 2),)
    assert profile.ambiguous is True


def test_standalone_kana_marks_do_not_conflict_with_korean_letters() -> None:
    profile = build_language_profile(("한글・ー",))

    assert profile.preferred_language is Language.KOREAN
    assert profile.reason is LanguageProfileReason.HANGUL_PRESENT
    assert profile.script_evidence == (ScriptEvidence(Script.HANGUL, 2),)
    assert profile.ambiguous is False


def test_real_kana_and_hangul_remain_ambiguous_regardless_of_counts_or_input_order() -> None:
    texts = ("かなかなかなー", "한글")
    profile = build_language_profile(texts)

    assert profile.preferred_language is None
    assert profile.reason is LanguageProfileReason.CONFLICTING_STRONG_SIGNALS
    assert profile.ambiguous is True
    assert build_language_profile(reversed(texts)) == profile


def _title_proposal(value: str, language: str, script: str) -> FieldProposal:
    return FieldProposal(
        field=MetadataField.TITLE,
        value=value,
        confidence=FieldConfidence.HIGH,
        provenance=MetadataProvenance(
            engine_id="catalogue",
            source_id="catalogue",
            record_id=language,
            source_url=f"https://catalogue.invalid/{language}",
            language=language,
            operation_id="language-regression",
        ),
        language=language,
        script=script,
        reason_codes=(ReviewReasonCode.FIELD_MATCH_HIGH,),
    )


def test_punctuation_does_not_set_japanese_preference_in_real_proposal_ranking() -> None:
    japanese = _title_proposal("エーシーディーシー", "ja", "Jpan")
    latin = _title_proposal("AC・DC", "en", "Latn")

    ranking = rank_proposals(
        consolidate_proposals((japanese, latin)),
        preferred_language="auto",
        local_texts=("AC・DC",),
    )

    # With no language evidence, the existing exact spelling ranks first and
    # competing language variants remain reviewable instead of favouring Japanese.
    assert ranking.effective_language is None
    assert ranking.effective_script is None
    assert ranking.proposals[0].value == "AC・DC"
    assert ranking.ambiguous is True
    assert ReviewReasonCode.LANGUAGE_UNAVAILABLE in ranking.reason_codes
    assert japanese.value == "エーシーディーシー"
    assert latin.value == "AC・DC"


def test_profile_analysis_preserves_original_text_and_comparison_normalisation() -> None:
    original = "Ａ—Ｂ　か\u3099ｶﾞ・"

    build_language_profile((original,))

    assert original == "Ａ—Ｂ　か\u3099ｶﾞ・"
    assert normalise_for_matching(original) == "a-b がガ・"
