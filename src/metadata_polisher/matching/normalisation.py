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
    # It runs first so equivalent input forms reach the same later rules. This
    # new string is a comparison key; callers retain the original display/tag text.
    compatibility_text = unicodedata.normalize("NFKC", text)

    # Restrict punctuation equivalence to the documented separator variants above.
    # Removing all punctuation, accents or edition words could merge different
    # titles, so those distinctions remain available to the scoring algorithm.
    dash_normalised_text = compatibility_text.translate(_DASH_TRANSLATION)

    # split/join treats Unicode whitespace consistently and removes edge spacing.
    # casefold handles textual case equivalence more fully than lower(), while
    # preserving the script: this step neither translates nor invents romanisation.
    return " ".join(dash_normalised_text.split()).casefold()
