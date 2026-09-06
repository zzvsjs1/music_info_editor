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
    """Count of normalised script characters, including attached Kana marks.

    Counts describe NFKC characters rather than visual glyphs or confidence.
    Composed voiced Kana therefore counts once; a remaining combining mark or
    length/iteration mark counts separately only inside an existing Kana run.
    Punctuation and unattached Kana marks never contribute to a script count.
    """

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

# Unicode groups length and iteration marks as modifier letters (Lm), while
# combining accents use mark categories (Mn, Mc, Me). These are useful context
# after Kana letters, but are not independent evidence of Japanese text.
_KANA_CONTINUATION_CATEGORIES = frozenset(("Lm", "Mn", "Mc", "Me"))


def _classify_character(character: str, *, follows_kana: bool = False) -> Script | None:
    """Recognise script letters and marks continuing an established Kana run."""
    character_name = unicodedata.name(character, "")
    character_category = unicodedata.category(character)

    if "HIRAGANA" in character_name or "KATAKANA" in character_name:
        # Kana base letters, including small and historical forms, are Unicode
        # Other_Letter (Lo). A script name alone also admits the middle dot;
        # accepting every letter category additionally admits a lone length mark.
        if character_category == "Lo":
            return Script.KANA

        if follows_kana and character_category in _KANA_CONTINUATION_CATEGORIES:
            return Script.KANA

        return None

    # Han is shared by several writing systems. Record it here without choosing
    # a language; the profile decision below keeps Han-only metadata ambiguous.
    if character_name.startswith(("CJK UNIFIED IDEOGRAPH", "CJK COMPATIBILITY IDEOGRAPH")):
        return Script.HAN

    # Hangul and Latin retain the existing letter-only rule. Digits, symbols and
    # punctuation may occur in titles but cannot identify either script alone.
    if "HANGUL" in character_name and character_category.startswith("L"):
        return Script.HANGUL

    if "LATIN" in character_name and character_category.startswith("L"):
        return Script.LATIN

    return None


def _profile(
    preferred_language: Language | None,
    evidence: tuple[ScriptEvidence, ...],
    *,
    ambiguous: bool,
    reason: LanguageProfileReason,
) -> LanguageProfile:
    # Keep every decision branch on the same immutable result shape. Consumers
    # can inspect the reason and evidence without reproducing inference rules.
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

        # Attachment belongs to this text only. A final Kana letter in an album
        # title must not lend support to a standalone mark in an artist field.
        follows_kana = False

        # Compatibility normalisation lets half-width Kana and full-width Latin
        # contribute like their standard forms and composes voiced Kana where
        # possible. Analyse this copy without changing the original metadata.
        for character in unicodedata.normalize("NFKC", text):
            script = _classify_character(character, follows_kana=follows_kana)

            # Only adjacent Kana can continue the run. Whitespace, punctuation
            # and other scripts all break it, even if earlier text contains Kana.
            follows_kana = script is Script.KANA

            if script is not None:
                counts[script] += 1

    # Omit zero-count buckets so absence remains explicit. In particular, a
    # punctuation-only value has no script evidence instead of an empty language.
    evidence = tuple(
        ScriptEvidence(script=script, character_count=counts[script])
        for script in _SCRIPT_ORDER
        if counts[script] > 0
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

    # No recognised text is absence of evidence, not a conflict between scripts.
    return _profile(
        None,
        evidence,
        ambiguous=False,
        reason=LanguageProfileReason.NO_SCRIPT_EVIDENCE,
    )
