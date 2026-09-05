"""Deterministic, non-authoritative metadata hints from local filenames."""

import re
from dataclasses import dataclass
from pathlib import Path
from re import Pattern

from metadata_polisher.domain.media import (
    FilenameHintConfidence,
    FilenameHintReason,
    FilenameHints,
)


@dataclass(frozen=True)
class _FilenameRule:
    pattern: Pattern[str]
    reason: FilenameHintReason
    confidence: FilenameHintConfidence


def _compile(expression: str) -> Pattern[str]:
    return re.compile(expression, flags=re.IGNORECASE)


# Specific disc-and-track forms must precede track-only forms. The deliberately
# narrow separators avoid treating a leading year or an ordinary numeric title
# as a track number merely because it begins with digits.
_RULES = (
    _FilenameRule(
        pattern=_compile(
            r"^Disc\s+(?P<disc>\d{1,3})\s*-\s*(?P<track>\d{1,3})\s*-\s*(?P<title>\S.*)$"
        ),
        reason=FilenameHintReason.LABELLED_DISC_TRACK_PREFIX,
        confidence=FilenameHintConfidence.HIGH,
    ),
    _FilenameRule(
        pattern=_compile(r"^CD\s*(?P<disc>\d{1,3})\s+(?P<track>\d{1,3})\s+(?P<title>\S.*)$"),
        reason=FilenameHintReason.CD_TRACK_PREFIX,
        confidence=FilenameHintConfidence.HIGH,
    ),
    _FilenameRule(
        pattern=_compile(r"^(?P<disc>\d{1,3})\.(?P<track>\d{1,3})\.\s+(?P<title>\S.*)$"),
        reason=FilenameHintReason.DISC_TRACK_DOTTED_PREFIX,
        confidence=FilenameHintConfidence.HIGH,
    ),
    _FilenameRule(
        pattern=_compile(r"^(?P<disc>\d{1,3})-(?P<track>\d{1,3})\s+(?P<title>\S.*)$"),
        reason=FilenameHintReason.DISC_TRACK_HYPHEN_PREFIX,
        confidence=FilenameHintConfidence.HIGH,
    ),
    _FilenameRule(
        pattern=_compile(r"^(?P<track>\d{1,3})\s+-\s+(?P<title>\S.*)$"),
        reason=FilenameHintReason.TRACK_DASH_PREFIX,
        confidence=FilenameHintConfidence.MEDIUM,
    ),
    _FilenameRule(
        pattern=_compile(r"^(?P<track>\d{1,3})\.\s+(?P<title>\S.*)$"),
        reason=FilenameHintReason.TRACK_DOTTED_PREFIX,
        confidence=FilenameHintConfidence.MEDIUM,
    ),
    _FilenameRule(
        pattern=_compile(r"^(?P<track>\d{1,3})_(?P<title>\S.*)$"),
        reason=FilenameHintReason.TRACK_UNDERSCORE_PREFIX,
        confidence=FilenameHintConfidence.MEDIUM,
    ),
)


def _positive_number(match: re.Match[str], group: str) -> int | None:
    raw_value = match.groupdict().get(group)

    if raw_value is None:
        return None

    value = int(raw_value)

    return value if value > 0 else None


def extract_filename_hints(path: Path) -> FilenameHints:
    """Return structured evidence without promoting filename text into metadata."""
    # Strip the actual filename extension only; dots inside the remaining stem
    # may be part of a disc/track prefix or the title itself.
    stem = path.stem.strip()

    if not stem:
        return FilenameHints()

    # First successful rule wins. Each result carries its rule and confidence
    # so later matching can distinguish filename evidence from real tags.
    for rule in _RULES:
        match = rule.pattern.fullmatch(stem)

        if match is None:
            continue

        disc_number = _positive_number(match, "disc")
        track_number = _positive_number(match, "track")

        # Every numbered rule requires a positive track, and disc-bearing rules
        # also require a positive disc. Invalid zero prefixes remain title text.
        if track_number is None:
            continue

        if "disc" in match.groupdict() and disc_number is None:
            continue

        return FilenameHints(
            disc_number=disc_number,
            track_number=track_number,
            probable_title=match.group("title").strip(),
            reason=rule.reason,
            confidence=rule.confidence,
        )

    # If no narrow numbering rule applies, preserve the whole stem as weak
    # title evidence. A numeric-looking title is safer than an invented number.
    return FilenameHints(
        probable_title=stem,
        reason=FilenameHintReason.STEM_ONLY,
        confidence=FilenameHintConfidence.LOW,
    )
