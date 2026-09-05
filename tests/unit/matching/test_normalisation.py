import pytest

from metadata_polisher.matching.normalisation import normalise_for_matching


# Full-width digits and a circled digit are compatibility forms. The expected
# comparison key demonstrates NFKC without changing original source values.
def test_normalisation_applies_unicode_nfkc() -> None:
    assert normalise_for_matching("１２３①") == "1231"


def test_normalisation_trims_and_collapses_unicode_whitespace() -> None:
    assert normalise_for_matching("  one\t two\n\u00a0three  ") == "one two three"


@pytest.mark.parametrize(
    "dash",
    (
        "‐",  # Hyphen.
        "‑",  # Non-breaking hyphen.
        "‒",  # Figure dash.
        "–",  # En dash.
        "—",  # Em dash.
        "―",  # Horizontal bar.
        "−",  # Minus sign.
        "﹘",  # Small em dash.
        "－",  # Full-width hyphen-minus.
    ),
)
def test_normalisation_maps_common_dash_variants_to_ascii_hyphen(dash: str) -> None:
    assert normalise_for_matching(f"part{dash}two") == "part-two"


# Matching uses linguistic case folding, including the sharp-s expansion.
# Filename collision policy intentionally has a different comparison rule.
def test_normalisation_uses_unicode_case_folding() -> None:
    assert normalise_for_matching("Straße") == "strasse"


# Similarity preparation must not invent a translation or romanisation;
# Japanese text survives apart from surrounding whitespace.
def test_normalisation_preserves_japanese_instead_of_romanising_or_translating() -> None:
    assert normalise_for_matching("  さくら 日本語  ") == "さくら 日本語"
