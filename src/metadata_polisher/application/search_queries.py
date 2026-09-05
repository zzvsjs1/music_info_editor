"""Conservative album-title fallbacks used only for explicit provider searches."""

import re

# Anchor both patterns to the title edges: a catalogue-like string inside the
# actual title must remain searchable, even if it resembles an annotation.
_CATALOGUE_PREFIX = re.compile(r"\A\[[A-Z]{2,6}-[0-9]{3,8}(?:-[0-9]{1,3})?\]\s+")
_DISC_SUFFIX = re.compile(r"(?:\A|\s+)\((?i:disc)\s+0*[1-9][0-9]*\)\Z")


def clean_album_search_fallback(album: str) -> str | None:
    """Return an additional search title, or None when no safe fallback remains."""
    # Recognise only an initial bracketed catalogue-shaped identifier and a
    # terminal numeric disc annotation. Other brackets, parentheses and edition
    # words such as BONUS DISC retain their meaning in the additional search.
    cleaned = _CATALOGUE_PREFIX.sub("", album, count=1)
    cleaned = _DISC_SUFFIX.sub("", cleaned, count=1).strip()

    # A title consisting entirely of annotations has no usable base title.
    # Callers retain the original query and source evidence in either case.
    if not cleaned or cleaned == album:
        return None

    return cleaned
