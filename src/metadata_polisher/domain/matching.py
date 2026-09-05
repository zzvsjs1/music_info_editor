"""Immutable, provider-neutral release and track matching values."""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import cast

from metadata_polisher.domain.metadata import Position


def _normalise_typed_tuple[T](
    name: str,
    values: object,
    item_type: type[T],
) -> tuple[T, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise TypeError(f"{name} must be an ordered sequence")

    # Copy at the boundary: freezing a dataclass does not freeze a caller-owned
    # list. Preserve ordering because release media and track sequence matter.
    copied_values = tuple(values)

    if any(not isinstance(value, item_type) for value in copied_values):
        raise TypeError(f"{name} must contain only {item_type.__name__} values")

    return cast(tuple[T, ...], copied_values)


def _normalise_string_tuple(name: str, values: object) -> tuple[str, ...]:
    return _normalise_typed_tuple(name, values, str)


def _validate_string(name: str, value: object) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")


def _validate_optional_string(name: str, value: object) -> None:
    if value is not None:
        _validate_string(name, value)


def _validate_positive_optional_integer(name: str, value: object) -> None:
    if value is None:
        return

    if type(value) is not int:
        raise TypeError(f"{name} must be an integer or None")

    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")


def _validate_non_negative_optional_integer(name: str, value: object) -> None:
    if value is None:
        return

    if type(value) is not int:
        raise TypeError(f"{name} must be an integer or None")

    if value < 0:
        raise ValueError(f"{name} cannot be negative")


@dataclass(frozen=True)
class LocalisedText:
    """One original provider text variant and its optional language metadata."""

    value: str
    language: str | None
    script: str | None

    def __post_init__(self) -> None:
        _validate_string("value", self.value)
        _validate_optional_string("language", self.language)
        _validate_optional_string("script", self.script)


@dataclass(frozen=True)
class MetadataProvenance:
    """Origin of one proposed metadata value across engine and source boundaries."""

    engine_id: str
    source_id: str
    record_id: str | None
    source_url: str | None
    language: str | None
    operation_id: str

    def __post_init__(self) -> None:
        _validate_string("engine_id", self.engine_id)
        _validate_string("source_id", self.source_id)
        _validate_optional_string("record_id", self.record_id)
        _validate_optional_string("source_url", self.source_url)
        _validate_optional_string("language", self.language)
        _validate_string("operation_id", self.operation_id)


class CreditScope(StrEnum):
    """The subject explicitly covered by source attribution, never inferred from count."""

    ALBUM = "album"
    TRACK = "track"
    WORK = "work"
    ALL_TRACKS = "all_tracks"


class CreditConfidence(StrEnum):
    """Certainty of the attribution itself, separate from a release or track match."""

    HIGH = "high"
    REVIEW = "review"
    LOW = "low"


@dataclass(frozen=True)
class ComposerCredit:
    """Source credit retained with its role, applicable scope and original identity.

    A credit may retain another role for explanation, but only a composer role
    explicitly covering a track/work/all tracks can support a Composer proposal.
    Album credits remain useful evidence without claiming precise assignments.
    """

    names: tuple[str, ...]
    scope: CreditScope
    confidence: CreditConfidence = CreditConfidence.HIGH
    role: str = "composer"
    record_id: str | None = None
    source_url: str | None = None

    def __post_init__(self) -> None:
        names = _normalise_string_tuple("names", self.names)

        if not names or any(not name.strip() for name in names):
            raise ValueError("credit names must contain non-blank names")

        if not isinstance(self.scope, CreditScope):
            raise TypeError("scope must be a CreditScope")

        if not isinstance(self.confidence, CreditConfidence):
            raise TypeError("confidence must be a CreditConfidence")

        _validate_string("role", self.role)
        _validate_optional_string("record_id", self.record_id)
        _validate_optional_string("source_url", self.source_url)
        object.__setattr__(self, "names", names)

    # Scope must explicitly cover a track/work/all tracks. The number of
    # credited people never turns an album-level credit into track evidence.
    @property
    def supports_track_composer(self) -> bool:
        """Report explicit applicability; one album-credited name is still album-only."""
        return self.role == "composer" and self.scope in {
            CreditScope.TRACK, CreditScope.WORK, CreditScope.ALL_TRACKS,
        }


@dataclass(frozen=True)
class ProviderTrack:
    """Provider-neutral track data retained within its release medium."""

    track_number: int | None
    titles: tuple[LocalisedText, ...]
    artists: tuple[str, ...]
    composers: tuple[str, ...]
    duration_seconds: float | None
    composer_credits: tuple[ComposerCredit, ...] = ()
    printed_number: str | None = None

    def __post_init__(self) -> None:
        # A documented provider pregap position is zero. Retain it as evidence;
        # the position projection below decides whether automatic writing is supported.
        _validate_non_negative_optional_integer("track_number", self.track_number)
        _validate_optional_string("printed_number", self.printed_number)
        object.__setattr__(self, "titles", _normalise_typed_tuple("titles", self.titles, LocalisedText))
        object.__setattr__(self, "artists", _normalise_string_tuple("artists", self.artists))
        object.__setattr__(self, "composers", _normalise_string_tuple("composers", self.composers))
        object.__setattr__(self, "composer_credits", _normalise_typed_tuple(
            "composer_credits", self.composer_credits, ComposerCredit,
        ))

        if self.duration_seconds is None:
            return

        if isinstance(self.duration_seconds, bool) or not isinstance(self.duration_seconds, (int, float)):
            raise TypeError("duration_seconds must be a number or None")

        normalised_duration = float(self.duration_seconds)

        if not math.isfinite(normalised_duration) or normalised_duration < 0:
            raise ValueError("duration_seconds must be finite and cannot be negative")

        object.__setattr__(self, "duration_seconds", normalised_duration)

    @property
    def supports_automatic_numbering(self) -> bool:
        """Keep pregaps and ambiguous printed labels out of numeric write proposals."""
        if self.track_number == 0:
            return False

        if self.printed_number is None:
            return True

        # Only an ASCII decimal printed label agreeing with the numeric field
        # is safe to project. Labels such as A1 or 1a retain their source meaning
        # and must not be converted into invented consecutive track numbers.
        printed = self.printed_number.strip()
        normalised = printed.lstrip("0") or "0"
        return printed.isascii() and printed.isdecimal() and normalised == str(self.track_number)


@dataclass(frozen=True)
class ReleaseMedium:
    """One provider release medium with its own ordered track listing."""

    medium_number: int | None
    title: str | None
    tracks: tuple[ProviderTrack, ...]
    declared_track_count: int | None = None
    tracks_complete: bool = True
    has_pregap: bool = False
    has_data_tracks: bool = False

    def __post_init__(self) -> None:
        _validate_positive_optional_integer("medium_number", self.medium_number)
        _validate_optional_string("title", self.title)
        object.__setattr__(self, "tracks", _normalise_typed_tuple("tracks", self.tracks, ProviderTrack))
        _validate_non_negative_optional_integer("declared_track_count", self.declared_track_count)

        for name in ("tracks_complete", "has_pregap", "has_data_tracks"):
            if type(getattr(self, name)) is not bool:
                raise TypeError(f"{name} must be a bool")

    @property
    def track_total(self) -> int | None:
        """Return only a supported complete-list total, independently of local files.

        Typed callers supplying a complete listing retain their original contract.
        External parsers must establish completeness explicitly from their source.
        Pregap/data totals have incompatible conventions, so they stay unknown.
        """
        if not self.tracks_complete or self.has_pregap or self.has_data_tracks or not self.tracks:
            return None

        # A partial page can contain a perfectly coherent visible sequence.
        # Confirm its declared count agrees before exposing a total to scoring
        # and later tag proposals.
        count = self.declared_track_count

        if count is not None and count != len(self.tracks):
            return None

        return len(self.tracks)

    def track_position(self, track_index: int) -> Position:
        """Use the same source position policy for mapping, review and validation."""
        # The array index selects a track; its provider number supplies the tag.
        # This distinction preserves missing numbers and non-standard listings.
        track = self.tracks[track_index]

        if not track.supports_automatic_numbering:
            return Position()

        return Position(track.track_number, self.track_total)


@dataclass(frozen=True)
class ReleaseCandidate:
    """Normalised release retaining source identity and medium boundaries."""

    engine_id: str
    source_id: str
    release_id: str
    titles: tuple[LocalisedText, ...]
    album_artists: tuple[str, ...]
    date: str | None
    media: tuple[ReleaseMedium, ...]
    source_url: str | None
    album_credits: tuple[ComposerCredit, ...] = ()
    media_complete: bool = True

    def __post_init__(self) -> None:
        _validate_string("engine_id", self.engine_id)
        _validate_string("source_id", self.source_id)
        _validate_string("release_id", self.release_id)
        object.__setattr__(self, "titles", _normalise_typed_tuple("titles", self.titles, LocalisedText))
        object.__setattr__(self, "album_artists", _normalise_string_tuple("album_artists", self.album_artists))
        _validate_optional_string("date", self.date)
        object.__setattr__(self, "media", _normalise_typed_tuple("media", self.media, ReleaseMedium))
        _validate_optional_string("source_url", self.source_url)

        if type(self.media_complete) is not bool:
            raise TypeError("media_complete must be a bool")

        # Keep album credits scoped to the release boundary. Track/work credits
        # belong with their own track evidence and cannot be attached here.
        credits = _normalise_typed_tuple("album_credits", self.album_credits, ComposerCredit)

        if any(credit.scope not in {CreditScope.ALBUM, CreditScope.ALL_TRACKS} for credit in credits):
            raise ValueError("album credits must have album or explicit all-tracks scope")

        object.__setattr__(self, "album_credits", credits)

    @property
    def disc_total(self) -> int | None:
        """Count all source release media only when their full listing is established."""
        return len(self.media) if self.media_complete and self.media else None

    def disc_position(self, medium_index: int) -> Position:
        """Project independent source medium number and release-wide total."""
        return Position(self.media[medium_index].medium_number, self.disc_total)


@dataclass(frozen=True)
class ReleaseSearchQuery:
    """Provider-independent search evidence assembled from a local group."""

    album: str | None
    artists: tuple[str, ...]
    year: int | None
    disc_hint: int | None
    local_track_count: int
    distinctive_titles: tuple[str, ...]

    def __post_init__(self) -> None:
        _validate_optional_string("album", self.album)
        object.__setattr__(self, "artists", _normalise_string_tuple("artists", self.artists))
        _validate_positive_optional_integer("year", self.year)
        _validate_positive_optional_integer("disc_hint", self.disc_hint)

        if type(self.local_track_count) is not int:
            raise TypeError("local_track_count must be an integer")

        if self.local_track_count < 0:
            raise ValueError("local_track_count cannot be negative")

        object.__setattr__(
            self,
            "distinctive_titles",
            _normalise_string_tuple("distinctive_titles", self.distinctive_titles),
        )
