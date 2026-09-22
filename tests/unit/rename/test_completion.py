"""Completion follows the template grammar without requiring a valid draft."""

import pytest

from metadata_polisher.rename.completion import template_completion


@pytest.mark.parametrize(
    ("marked_template", "prefix", "replacement"),
    [
        ("%|", "%", "%"),
        ("[%di|.]%tracknumber%. %title%", "%di", "%di"),
        ("%tracknumber%. %ti| - %album%", "%ti", "%ti"),
        ("%tracknumber%. %ti|tle% - %album%", "%ti", "%title%"),
        ("%|title%", "%", "%title%"),
        ("%title|%", "%title", "%title%"),
        ("%title%%ar|", "%ar", "%ar"),
        (r"\% literal %al|", "%al", "%al"),
        (r"\\%al|", "%al", "%al"),
        ("🎵 [%AL|bum%]", "%AL", "%ALbum%"),
    ],
)
def test_completion_identifies_only_the_field_at_the_cursor(marked_template, prefix, replacement):
    cursor = marked_template.index("|")
    template = marked_template.replace("|", "")

    completion = template_completion(template, cursor)

    assert completion is not None
    assert completion.prefix == prefix
    assert template[completion.start : completion.end] == replacement


@pytest.mark.parametrize(
    "marked_template",
    ["|", "Album |", "|%title%", "%title%|", r"\%|", r"\%ti|", r"\\\%ti|", "%bad name|"],
)
def test_literal_text_and_closed_or_escaped_fields_have_no_completion(marked_template):
    cursor = marked_template.index("|")

    assert template_completion(marked_template.replace("|", ""), cursor) is None
