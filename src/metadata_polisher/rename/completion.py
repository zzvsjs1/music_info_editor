"""Locate the editable field in an incomplete filename template."""

from dataclasses import dataclass


@dataclass(frozen=True)
class TemplateCompletion:
    """Python character offsets for replacing one field, plus its typed prefix."""

    start: int
    end: int
    prefix: str


def template_completion(template: str, cursor: int) -> TemplateCompletion | None:
    """Follow percent delimiters and literal escapes up to the editing position.

    Unlike parsing for Save, completion must tolerate unfinished brackets and
    field names. Only the field containing the cursor determines suggestions.
    """
    start: int | None = None
    index = 0

    while index < cursor:
        # Outside a field, consume an escape together with its literal character.
        # This also handles backslash parity: two backslashes leave a following
        # percent free to open a field, while one backslash escapes that percent.
        if start is None and template[index] == "\\":
            index += 2
            continue

        if template[index] == "%":
            start = index if start is None else None

        index += 1

    if start is None:
        return None

    prefix = template[start:cursor]

    if any(not (character.isalnum() or character == "_") for character in prefix[1:]):
        return None

    # When editing an existing field, replace the name's remaining letters and
    # closing delimiter too. Stop at literal separators or brackets in an
    # incomplete draft, so later fields and optional groups remain untouched.
    end = cursor

    while end < len(template) and (template[end].isalnum() or template[end] == "_"):
        end += 1

    if end < len(template) and template[end] == "%":
        end += 1

    return TemplateCompletion(start, end, prefix)
