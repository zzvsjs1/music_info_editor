"""Conservative, comparison-only text normalisation."""

import unicodedata

# These characters express the same separator in common metadata sources. The
# translation deliberately stops at dash-like punctuation: quotes and other
# punctuation can carry meaning in titles and must remain available to matching.
_DASH_TRANSLATION = str.maketrans(
    {
        "‐": "-",  # Hyphen.
        "‑": "-",  # Non-breaking hyphen.
        "‒": "-",  # Figure dash.
        "–": "-",  # En dash.
        "—": "-",  # Em dash.
        "―": "-",  # Horizontal bar.
        "−": "-",  # Minus sign.
        "﹘": "-",  # Small em dash.
        "－": "-",  # Full-width hyphen-minus.
    }
)


def normalise_for_matching(text: str) -> str:
    """Return conservative comparison text without changing stored metadata."""
    # NFKC reconciles compatibility forms such as full-width Latin characters.
    # Apply it before dash replacement, then collapse whitespace and case-fold.
    # Keep accents, words and scripts: this output is only a comparison key.
    compatibility_text = unicodedata.normalize("NFKC", text)
    dash_normalised_text = compatibility_text.translate(_DASH_TRANSLATION)

    return " ".join(dash_normalised_text.split()).casefold()
