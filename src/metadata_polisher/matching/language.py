"""Conservative script evidence and album-level language preference inference."""

import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum


class Language(StrEnum):
    """Languages inferred only from strong, unambiguous script evidence."""

    JAPANESE = "ja"
    KOREAN = "ko"


class Script(StrEnum):
    """Script families relevant to the V1 language-preservation policy."""

    KANA = "kana"
    HAN = "han"
    HANGUL = "hangul"
    LATIN = "latin"


class LanguageProfileReason(StrEnum):
    """Stable rule explaining the inferred profile decision."""

    KANA_PRESENT = "KANA_PRESENT"
    HANGUL_PRESENT = "HANGUL_PRESENT"
    CONFLICTING_STRONG_SIGNALS = "CONFLICTING_STRONG_SIGNALS"
    HAN_WITHOUT_LANGUAGE_SIGNAL = "HAN_WITHOUT_LANGUAGE_SIGNAL"
    LATIN_ONLY = "LATIN_ONLY"
    NO_SCRIPT_EVIDENCE = "NO_SCRIPT_EVIDENCE"


@dataclass(frozen=True)
class ScriptEvidence:
    """Count of comparison characters belonging to one recognised script."""

    script: Script
    character_count: int


@dataclass(frozen=True)
class LanguageProfile:
    """Deterministic language preference plus the evidence behind it."""

    preferred_language: Language | None
    script_evidence: tuple[ScriptEvidence, ...]
    ambiguous: bool
    reason: LanguageProfileReason


# An explicit script order keeps evidence stable even if input text arrives
# in a different order. Counts support explanations, not language percentages.
_SCRIPT_ORDER = (Script.KANA, Script.HAN, Script.HANGUL, Script.LATIN)


def _classify_character(character: str) -> Script | None:
    character_name = unicodedata.name(character, "")

    if "HIRAGANA" in character_name or "KATAKANA" in character_name:
        return Script.KANA

    if character_name.startswith(("CJK UNIFIED IDEOGRAPH", "CJK COMPATIBILITY IDEOGRAPH")):
        return Script.HAN

    if "HANGUL" in character_name and unicodedata.category(character).startswith("L"):
        return Script.HANGUL

    if "LATIN" in character_name and unicodedata.category(character).startswith("L"):
        return Script.LATIN

    return None


def _profile(
    preferred_language: Language | None,
    evidence: tuple[ScriptEvidence, ...],
    *,
    ambiguous: bool,
    reason: LanguageProfileReason,
) -> LanguageProfile:
    return LanguageProfile(
        preferred_language=preferred_language,
        script_evidence=evidence,
        ambiguous=ambiguous,
        reason=reason,
    )


def build_language_profile(texts: Iterable[str | None]) -> LanguageProfile:
    """Aggregate local text into a conservative, order-independent profile."""
    counts = {script: 0 for script in _SCRIPT_ORDER}

    for text in texts:
        if text is None:
            continue

        # Compatibility normalisation lets half-width Kana and full-width Latin
        # contribute to the same evidence buckets as their standard forms.
        for character in unicodedata.normalize("NFKC", text):
            script = _classify_character(character)

            if script is not None:
                counts[script] += 1

    evidence = tuple(
        ScriptEvidence(script=script, character_count=counts[script]) for script in _SCRIPT_ORDER if counts[script] > 0
    )
    has_kana = counts[Script.KANA] > 0
    has_hangul = counts[Script.HANGUL] > 0

    # Check conflicting strong signals before either single-language branch.
    # A large Kana count must not erase the presence of a Korean signal, or
    # vice versa; that mixed case needs a reviewable ambiguous profile.
    if has_kana and has_hangul:
        return _profile(
            None,
            evidence,
            ambiguous=True,
            reason=LanguageProfileReason.CONFLICTING_STRONG_SIGNALS,
        )

    if has_kana:
        return _profile(
            Language.JAPANESE,
            evidence,
            ambiguous=False,
            reason=LanguageProfileReason.KANA_PRESENT,
        )

    if has_hangul:
        return _profile(
            Language.KOREAN,
            evidence,
            ambiguous=False,
            reason=LanguageProfileReason.HANGUL_PRESENT,
        )

    if counts[Script.HAN] > 0:
        # Han characters alone cannot safely distinguish Chinese from Japanese.
        return _profile(
            None,
            evidence,
            ambiguous=True,
            reason=LanguageProfileReason.HAN_WITHOUT_LANGUAGE_SIGNAL,
        )

    if counts[Script.LATIN] > 0:
        # Latin identifies a script, not a particular language such as English.
        return _profile(
            None,
            evidence,
            ambiguous=False,
            reason=LanguageProfileReason.LATIN_ONLY,
        )

    return _profile(
        None,
        evidence,
        ambiguous=False,
        reason=LanguageProfileReason.NO_SCRIPT_EVIDENCE,
    )
