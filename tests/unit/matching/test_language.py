from metadata_polisher.matching.language import (
    Language,
    LanguageProfileReason,
    Script,
    ScriptEvidence,
    build_language_profile,
)


# The exact script counts make the evidence inspectable: Kana supplies the
# Japanese signal, while the shared Han character alone would be ambiguous.
def test_kana_and_han_are_strong_japanese_evidence() -> None:
    profile = build_language_profile(("星のカービィ",))

    assert profile.preferred_language is Language.JAPANESE
    assert profile.ambiguous is False
    assert profile.reason is LanguageProfileReason.KANA_PRESENT
    assert profile.script_evidence == (
        ScriptEvidence(script=Script.KANA, character_count=5),
        ScriptEvidence(script=Script.HAN, character_count=1),
    )


def test_hangul_is_strong_korean_evidence() -> None:
    profile = build_language_profile(("별의 커비",))

    assert profile.preferred_language is Language.KOREAN
    assert profile.ambiguous is False
    assert profile.reason is LanguageProfileReason.HANGUL_PRESENT
    assert profile.script_evidence == (ScriptEvidence(script=Script.HANGUL, character_count=4),)


# Latin script does not identify English; the same alphabet appears in many
# languages and in romanised metadata.
def test_latin_text_records_script_without_guessing_a_language() -> None:
    profile = build_language_profile(("The Album",))

    assert profile.preferred_language is None
    assert profile.ambiguous is False
    assert profile.reason is LanguageProfileReason.LATIN_ONLY
    assert profile.script_evidence == (ScriptEvidence(script=Script.LATIN, character_count=8),)


def test_han_only_text_remains_language_ambiguous() -> None:
    profile = build_language_profile(("星之卡比",))

    assert profile.preferred_language is None
    assert profile.ambiguous is True
    assert profile.reason is LanguageProfileReason.HAN_WITHOUT_LANGUAGE_SIGNAL
    assert profile.script_evidence == (ScriptEvidence(script=Script.HAN, character_count=4),)


def test_mixed_japanese_and_latin_titles_retain_all_script_evidence() -> None:
    texts = ("星のカービィ", "Kirby")

    profile = build_language_profile(texts)

    assert profile.preferred_language is Language.JAPANESE
    assert profile.ambiguous is False
    assert profile.reason is LanguageProfileReason.KANA_PRESENT
    assert profile.script_evidence == (
        ScriptEvidence(script=Script.KANA, character_count=5),
        ScriptEvidence(script=Script.HAN, character_count=1),
        ScriptEvidence(script=Script.LATIN, character_count=5),
    )
    assert build_language_profile(reversed(texts)) == profile


# Conflicting strong signals remain unresolved instead of choosing the
# language with more characters in this small fixture.
def test_conflicting_kana_and_hangul_signals_are_ambiguous() -> None:
    profile = build_language_profile(("ひらがな", "한글"))

    assert profile.preferred_language is None
    assert profile.ambiguous is True
    assert profile.reason is LanguageProfileReason.CONFLICTING_STRONG_SIGNALS


def test_empty_metadata_has_no_script_or_language_evidence() -> None:
    profile = build_language_profile((None, "", " \t "))

    assert profile.preferred_language is None
    assert profile.ambiguous is False
    assert profile.reason is LanguageProfileReason.NO_SCRIPT_EVIDENCE
    assert profile.script_evidence == ()
