import pytest

from metadata_polisher.domain.metadata import MetadataSnapshot, Position
from metadata_polisher.rename.template import (
    FilenameRenderPolicy,
    TemplateError,
    parse_template,
    render_template,
)


def _render(
    template: str,
    *,
    disc: int | None = None,
    track: int | None = None,
    title: str | None = None,
) -> str:
    metadata = MetadataSnapshot(
        title=title,
        track=Position(number=track),
        disc=Position(number=disc),
    )

    return render_template(parse_template(template), metadata, FilenameRenderPolicy())


def test_default_template_renders_disc_when_present() -> None:
    assert (
        _render(
            "[%discnumber%.]%tracknumber%. %title%",
            disc=1,
            track=1,
            title="Emblem Engage!",
        )
        == "1.01. Emblem Engage!"
    )


def test_default_template_omits_whole_optional_group_when_disc_is_missing() -> None:
    assert (
        _render(
            "[%discnumber%.]%tracknumber%. %title%",
            track=1,
            title="Emblem Engage!",
        )
        == "01. Emblem Engage!"
    )


# Disc is present but artist is absent. The entire optional prefix,
# including punctuation, must disappear instead of yielding a partial label.
def test_optional_group_requires_every_referenced_field() -> None:
    template = parse_template("[%discnumber% - %artist%: ]%tracknumber%. %title%")
    metadata = MetadataSnapshot(
        title="Opening",
        track=Position(number=2),
        disc=Position(number=1),
    )

    assert render_template(template, metadata, FilenameRenderPolicy()) == "02. Opening"


def test_optional_group_renders_when_every_referenced_field_is_usable() -> None:
    template = parse_template("[%discnumber% - %artist%: ]%tracknumber%. %title%")
    metadata = MetadataSnapshot(
        title="Opening",
        artists=("Anna", "Bob"),
        track=Position(number=2),
        disc=Position(number=1),
    )

    assert render_template(template, metadata, FilenameRenderPolicy()) == "1 - Anna; Bob: 02. Opening"


def test_all_supported_tokens_render_semantic_metadata() -> None:
    template = parse_template(
        "%discnumber%|%tracknumber%|%title%|%artist%|%album%|"
        "%albumartist%|%composer%|%date%|%genre%"
    )
    metadata = MetadataSnapshot(
        title="Title",
        artists=("Artist One", "Artist Two"),
        album="Album",
        album_artists=("Album Artist",),
        composers=("Composer One", "Composer Two"),
        track=Position(number=4),
        disc=Position(number=2),
        date="2026-09-05",
        genres=("Game", "Soundtrack"),
    )

    assert render_template(template, metadata, FilenameRenderPolicy()) == (
        "2|04|Title|Artist One; Artist Two|Album|Album Artist|"
        "Composer One; Composer Two|2026-09-05|Game; Soundtrack"
    )


# Raw strings expose exactly the template escapes passed to the parser;
# escaped metacharacters must survive as literal output.
def test_escaping_produces_literal_brackets_percent_and_backslash() -> None:
    assert _render(r"\[literal\]\%\\", title="unused") == "[literal]%\\"


def test_escaping_works_inside_an_optional_group() -> None:
    assert _render(r"[\[%discnumber%\]\%] %title%", disc=2, title="Finale") == "[2]% Finale"


@pytest.mark.parametrize(
    "template",
    (
        "[%title%] ]",
        "[%title%",
        "[[%title%]]",
        "[%title%[]",
        "%title",
        "title%",
        "%%",
        "%unknown%",
        "ending\\",
        r"bad\q",
    ),
)
def test_malformed_nested_unmatched_and_unknown_syntax_is_rejected(template: str) -> None:
    with pytest.raises(TemplateError):
        parse_template(template)


def test_missing_required_token_is_a_render_error() -> None:
    ast = parse_template("%tracknumber%. %title%")
    metadata = MetadataSnapshot(track=Position(number=1))

    with pytest.raises(TemplateError, match="title"):
        render_template(ast, metadata, FilenameRenderPolicy())


def test_whitespace_only_required_value_is_missing() -> None:
    ast = parse_template("%title%")

    with pytest.raises(TemplateError, match="title"):
        render_template(ast, MetadataSnapshot(title=" \t "), FilenameRenderPolicy())


def test_multi_value_separator_is_configurable() -> None:
    ast = parse_template("%artist% / %composer% / %genre%")
    metadata = MetadataSnapshot(
        artists=("A", "B"),
        composers=("C", "D"),
        genres=("E", "F"),
    )
    policy = FilenameRenderPolicy(multi_value_separator=" + ")

    assert render_template(ast, metadata, policy) == "A + B / C + D / E + F"


# Padding widens disc 2 to 002, while a three-digit track remains 123.
# The requested minimum width must never truncate a valid source number.
def test_number_width_policy_sets_minimum_width_without_truncating() -> None:
    ast = parse_template("%discnumber%.%tracknumber%")
    metadata = MetadataSnapshot(
        track=Position(number=123),
        disc=Position(number=2),
    )
    policy = FilenameRenderPolicy(minimum_track_digits=2, minimum_disc_digits=3)

    assert render_template(ast, metadata, policy) == "002.123"


@pytest.mark.parametrize("field", ("minimum_track_digits", "minimum_disc_digits"))
def test_number_width_policy_requires_at_least_one_digit(field: str) -> None:
    kwargs = {field: 0}

    with pytest.raises(ValueError, match=field):
        FilenameRenderPolicy(**kwargs)
