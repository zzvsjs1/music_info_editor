"""Parse and render deterministic filename templates."""

from dataclasses import dataclass
from enum import Enum

from metadata_polisher.domain.metadata import MetadataSnapshot


class TemplateError(ValueError):
    """A template cannot be parsed or rendered safely."""


class TemplateField(Enum):
    """Fields supported by the V1 filename-template grammar."""

    DISC_NUMBER = "discnumber"
    TRACK_NUMBER = "tracknumber"
    TITLE = "title"
    ARTIST = "artist"
    ALBUM = "album"
    ALBUM_ARTIST = "albumartist"
    COMPOSER = "composer"
    DATE = "date"
    GENRE = "genre"


@dataclass(frozen=True)
class LiteralNode:
    """Literal output whose template metacharacters have already been decoded."""

    text: str


@dataclass(frozen=True)
class FieldNode:
    """One semantic metadata-field reference."""

    field: TemplateField


@dataclass(frozen=True)
class OptionalGroupNode:
    """Non-nested content omitted if any referenced field is unavailable."""

    nodes: tuple[LiteralNode | FieldNode, ...]


type TemplateNode = LiteralNode | FieldNode | OptionalGroupNode


@dataclass(frozen=True)
class TemplateAst:
    """Validated filename-template syntax, ready for repeated rendering."""

    nodes: tuple[TemplateNode, ...]


@dataclass(frozen=True)
class FilenameRenderPolicy:
    """Formatting choices kept separate from template syntax."""

    minimum_track_digits: int = 2
    minimum_disc_digits: int = 1
    multi_value_separator: str = "; "

    def __post_init__(self) -> None:
        _validate_minimum_digits("minimum_track_digits", self.minimum_track_digits)
        _validate_minimum_digits("minimum_disc_digits", self.minimum_disc_digits)

        if not isinstance(self.multi_value_separator, str):
            raise TypeError("multi_value_separator must be a string")


def _validate_minimum_digits(name: str, value: int) -> None:
    if type(value) is not int:
        raise TypeError(f"{name} must be an integer")

    if value < 1:
        raise ValueError(f"{name} must be at least 1")


def _append_literal(nodes: list[TemplateNode], text: str) -> None:
    """Coalesce adjacent literal characters so the AST remains small."""
    if not text:
        return

    if nodes and isinstance(nodes[-1], LiteralNode):
        previous = nodes[-1]
        nodes[-1] = LiteralNode(previous.text + text)

        return

    nodes.append(LiteralNode(text))


def _parse_field(template: str, start: int) -> tuple[FieldNode, int]:
    closing_index = template.find("%", start + 1)

    if closing_index == -1:
        raise TemplateError(f"Unmatched '%' at position {start}")

    field_name = template[start + 1 : closing_index]

    try:
        field = TemplateField(field_name)
    except ValueError as error:
        shown_name = field_name or "<empty>"
        raise TemplateError(f"Unknown template field '{shown_name}' at position {start}") from error

    return FieldNode(field), closing_index + 1


def parse_template(template: str) -> TemplateAst:
    """Parse V1 syntax into a small immutable AST.

    The parser handles escaping before interpreting metacharacters. This makes an
    escaped percent unambiguously literal and prevents rendering behaviour from
    depending on a chain of regular-expression replacements.
    """
    # The parser has two states: root content and one open optional group.
    # None means no group is open; an empty list means a group has opened but
    # has no content yet. Keeping that distinction catches nested brackets.
    root_nodes: list[TemplateNode] = []
    optional_nodes: list[TemplateNode] | None = None
    index = 0

    while index < len(template):
        character = template[index]
        target_nodes = optional_nodes if optional_nodes is not None else root_nodes

        # Decode an escape before checking tokens or brackets, so an escaped
        # percent remains literal and never opens a field reference.
        if character == "\\":
            if index + 1 >= len(template):
                raise TemplateError(f"Malformed escape at position {index}")

            escaped_character = template[index + 1]

            if escaped_character not in "[]%\\":
                raise TemplateError(
                    f"Cannot escape '{escaped_character}' at position {index}; "
                    "only '[', ']', '%' and '\\' may be escaped"
                )

            _append_literal(target_nodes, escaped_character)
            index += 2

            continue

        if character == "%":
            field_node, index = _parse_field(template, index)
            target_nodes.append(field_node)

            continue

        if character == "[":
            if optional_nodes is not None:
                raise TemplateError(f"Nested optional group at position {index}")

            optional_nodes = []
            index += 1

            continue

        if character == "]":
            if optional_nodes is None:
                raise TemplateError(f"Unmatched ']' at position {index}")

            # Nested groups are rejected above, so an optional group can contain
            # only literal and field nodes. Keep that invariant visible to mypy.
            group_nodes = tuple(
                node for node in optional_nodes if isinstance(node, LiteralNode | FieldNode)
            )
            root_nodes.append(OptionalGroupNode(group_nodes))
            optional_nodes = None
            index += 1

            continue

        _append_literal(target_nodes, character)
        index += 1

    if optional_nodes is not None:
        raise TemplateError("Unmatched '[' in template")

    return TemplateAst(tuple(root_nodes))


def _usable_text(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None

    return value


def _usable_multi_value(values: tuple[str, ...], separator: str) -> str | None:
    usable_values = tuple(value for value in values if value.strip())

    if not usable_values:
        return None

    # Joining is only for the filename projection. The underlying metadata
    # retains separate tuple values for each artist/composer/genre.
    return separator.join(usable_values)


def _field_value(
    field: TemplateField,
    metadata: MetadataSnapshot,
    policy: FilenameRenderPolicy,
) -> str | None:
    if field is TemplateField.DISC_NUMBER:
        number = metadata.disc.number

        if number is None:
            return None

        return f"{number:0{policy.minimum_disc_digits}d}"

    if field is TemplateField.TRACK_NUMBER:
        number = metadata.track.number

        if number is None:
            return None

        # Width is a minimum, not a limit: track 123 remains 123 when the
        # configured width is two digits.
        return f"{number:0{policy.minimum_track_digits}d}"

    if field is TemplateField.TITLE:
        return _usable_text(metadata.title)

    if field is TemplateField.ARTIST:
        return _usable_multi_value(metadata.artists, policy.multi_value_separator)

    if field is TemplateField.ALBUM:
        return _usable_text(metadata.album)

    if field is TemplateField.ALBUM_ARTIST:
        return _usable_multi_value(metadata.album_artists, policy.multi_value_separator)

    if field is TemplateField.COMPOSER:
        return _usable_multi_value(metadata.composers, policy.multi_value_separator)

    if field is TemplateField.DATE:
        return _usable_text(metadata.date)

    if field is TemplateField.GENRE:
        return _usable_multi_value(metadata.genres, policy.multi_value_separator)

    # The enum and exhaustive branches above should make this unreachable. Keep a
    # typed failure rather than silently producing a partial filename if the enum
    # is extended without updating the renderer.
    raise TemplateError(f"Unsupported template field '{field.value}'")


# Render a group atomically: if any field is unavailable, discard its
# literals as well. For [%discnumber%-], an unknown disc omits the hyphen
# along with the number instead of leaving a dangling separator.
def _render_optional_group(
    group: OptionalGroupNode,
    metadata: MetadataSnapshot,
    policy: FilenameRenderPolicy,
) -> str | None:
    pieces: list[str] = []

    for node in group.nodes:
        if isinstance(node, LiteralNode):
            pieces.append(node.text)

            continue

        value = _field_value(node.field, metadata, policy)

        if value is None:
            return None

        pieces.append(value)

    return "".join(pieces)


def render_template(
    ast: TemplateAst,
    metadata: MetadataSnapshot,
    policy: FilenameRenderPolicy,
) -> str:
    """Render validated syntax from final reviewed semantic metadata."""
    pieces: list[str] = []

    for node in ast.nodes:
        if isinstance(node, LiteralNode):
            pieces.append(node.text)

            continue

        if isinstance(node, OptionalGroupNode):
            optional_text = _render_optional_group(node, metadata, policy)

            if optional_text is not None:
                pieces.append(optional_text)

            continue

        value = _field_value(node.field, metadata, policy)

        # A required missing value blocks the preview instead of producing a
        # partial name that could misrepresent the final reviewed metadata.
        if value is None:
            raise TemplateError(f"Required field '%{node.field.value}%' has no usable value")

        pieces.append(value)

    return "".join(pieces)
