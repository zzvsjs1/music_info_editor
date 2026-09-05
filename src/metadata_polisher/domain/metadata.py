"""Immutable semantic metadata values independent of physical tag formats."""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from typing import cast


class MetadataField(Enum):
    """Managed V1 metadata fields."""

    TITLE = "title"
    ARTISTS = "artists"
    ALBUM = "album"
    ALBUM_ARTISTS = "album_artists"
    COMPOSERS = "composers"
    TRACK = "track"
    DISC = "disc"
    DATE = "date"
    GENRES = "genres"


# Keep absence distinct from a failed read: a missing field may be filled,
# whereas unreadable or unsupported data needs a separate review decision.
class FieldReadState(Enum):
    """Quality/state of a managed field read from a local file."""

    PRESENT = "present"
    MISSING = "missing"
    UNREADABLE = "unreadable"
    UNSUPPORTED = "unsupported"


def _validate_position_component(name: str, value: int | None) -> None:
    # None preserves an unknown number/total. Exact int validation rejects
    # bools, which otherwise pass Python integer checks as 0 or 1.
    if value is not None and type(value) is not int:
        raise TypeError(f"Position {name} must be an integer or None")

    if value is not None and value < 0:
        raise ValueError(f"Position {name} cannot be negative")


def _normalise_string_tuple(name: str, values: object) -> tuple[str, ...]:
    if isinstance(values, str):
        raise TypeError(f"{name} must be a sequence of strings, not a string")

    if not isinstance(values, Sequence):
        raise TypeError(f"{name} must be an ordered sequence of strings")

    # Preserve source order and each complete name; splitting on punctuation
    # would corrupt names that legitimately contain semicolons or slashes.
    normalised = tuple(values)

    if any(not isinstance(value, str) for value in normalised):
        raise TypeError(f"{name} must contain only strings")

    return cast(tuple[str, ...], normalised)


@dataclass(frozen=True)
class Position:
    """Semantic track or disc number and optional total."""

    # Number and total are independent: a source may know one without the
    # other. Keep zero representable at this layer; write and matching policy
    # decide whether a particular source position is safe to propose.
    number: int | None = None
    total: int | None = None

    def __post_init__(self) -> None:
        _validate_position_component("number", self.number)
        _validate_position_component("total", self.total)


@dataclass(frozen=True)
class MetadataSnapshot:
    """Format-neutral values for every metadata field managed in V1."""

    title: str | None = None
    artists: tuple[str, ...] = ()
    album: str | None = None
    album_artists: tuple[str, ...] = ()
    composers: tuple[str, ...] = ()
    track: Position = Position()
    disc: Position = Position()
    date: str | None = None
    genres: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # Boundary adapters may naturally build lists. Copy them to tuples here so
        # frozen snapshots cannot still be mutated through an external list alias.
        object.__setattr__(self, "artists", _normalise_string_tuple("artists", self.artists))
        object.__setattr__(
            self,
            "album_artists",
            _normalise_string_tuple("album_artists", self.album_artists),
        )
        object.__setattr__(self, "composers", _normalise_string_tuple("composers", self.composers))
        object.__setattr__(self, "genres", _normalise_string_tuple("genres", self.genres))


@dataclass(frozen=True)
class MetadataChange:
    """One semantic old/new field change after review."""

    field: MetadataField
    old_value: object
    new_value: object
