from pathlib import Path

import pytest

from metadata_polisher.domain.media import (
    FilenameHintConfidence,
    FilenameHintReason,
    FilenameHints,
)
from metadata_polisher.scanner.filename_hints import extract_filename_hints


# These examples mirror the design grammar and assert the reason/confidence
# as well as parsed values. Similar-looking prefixes carry different evidence
# strength, so producing just the right title is not sufficient.
@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        (
            "01 - Title.flac",
            FilenameHints(
                track_number=1,
                probable_title="Title",
                reason=FilenameHintReason.TRACK_DASH_PREFIX,
                confidence=FilenameHintConfidence.MEDIUM,
            ),
        ),
        (
            "01. Title.flac",
            FilenameHints(
                track_number=1,
                probable_title="Title",
                reason=FilenameHintReason.TRACK_DOTTED_PREFIX,
                confidence=FilenameHintConfidence.MEDIUM,
            ),
        ),
        (
            "1-01 Title.flac",
            FilenameHints(
                disc_number=1,
                track_number=1,
                probable_title="Title",
                reason=FilenameHintReason.DISC_TRACK_HYPHEN_PREFIX,
                confidence=FilenameHintConfidence.HIGH,
            ),
        ),
        (
            "1.01. Title.flac",
            FilenameHints(
                disc_number=1,
                track_number=1,
                probable_title="Title",
                reason=FilenameHintReason.DISC_TRACK_DOTTED_PREFIX,
                confidence=FilenameHintConfidence.HIGH,
            ),
        ),
        (
            "CD1 01 Title.flac",
            FilenameHints(
                disc_number=1,
                track_number=1,
                probable_title="Title",
                reason=FilenameHintReason.CD_TRACK_PREFIX,
                confidence=FilenameHintConfidence.HIGH,
            ),
        ),
        (
            "Disc 2 - 03 - Title.flac",
            FilenameHints(
                disc_number=2,
                track_number=3,
                probable_title="Title",
                reason=FilenameHintReason.LABELLED_DISC_TRACK_PREFIX,
                confidence=FilenameHintConfidence.HIGH,
            ),
        ),
        (
            "03_Title.flac",
            FilenameHints(
                track_number=3,
                probable_title="Title",
                reason=FilenameHintReason.TRACK_UNDERSCORE_PREFIX,
                confidence=FilenameHintConfidence.MEDIUM,
            ),
        ),
        (
            "Title.flac",
            FilenameHints(
                probable_title="Title",
                reason=FilenameHintReason.STEM_ONLY,
                confidence=FilenameHintConfidence.LOW,
            ),
        ),
    ],
)
def test_extract_filename_hints_uses_ordered_design_patterns(
    filename: str,
    expected: FilenameHints,
) -> None:
    assert extract_filename_hints(Path(filename)) == expected


# Numbers can be part of a real title, year or telephone-style phrase.
# These negative cases protect the conservative fallback to the whole stem.
@pytest.mark.parametrize(
    "filename",
    [
        "1984.flac",
        "1984 - Main Theme.flac",
        "1-800-GHOST.flac",
        "007 Theme.flac",
        "2.0.flac",
        "10,000 Days.flac",
        "00 - Prelude.flac",
    ],
)
def test_extract_filename_hints_does_not_invent_numbers_for_numeric_titles(
    filename: str,
) -> None:
    hints = extract_filename_hints(Path(filename))

    assert hints.disc_number is None
    assert hints.track_number is None
    assert hints.probable_title == Path(filename).stem
    assert hints.reason is FilenameHintReason.STEM_ONLY
    assert hints.confidence is FilenameHintConfidence.LOW


def test_extract_filename_hints_handles_blank_stem_without_claiming_evidence() -> None:
    hints = extract_filename_hints(Path())

    assert hints == FilenameHints()
