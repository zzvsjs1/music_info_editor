"""Explainable scoring of one local group against release-medium candidates.

The pipeline first extracts read-state-authorised local evidence, then chooses
one preliminary correspondence inside the selected medium. Each dimension
reports agreement separately from availability and contradiction. The final
weighted ratio ranks candidates; coverage and conflict rules decide whether
HIGH is justified. The track mapper still makes the final file-to-track choices.
"""

import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import date
from enum import StrEnum

from rapidfuzz.fuzz import ratio, token_set_ratio

from metadata_polisher.domain.matching import LocalisedText, ProviderTrack, ReleaseCandidate, ReleaseMedium
from metadata_polisher.domain.media import LocalMediaFile
from metadata_polisher.domain.metadata import FieldReadState, MetadataField
from metadata_polisher.matching.evidence import effective_local_title
from metadata_polisher.matching.language import Language, Script, build_language_profile
from metadata_polisher.matching.normalisation import normalise_for_matching
from metadata_polisher.matching.policy import DEFAULT_MATCHING_POLICY, SCORE_DECIMAL_PLACES, MatchingPolicy
from metadata_polisher.scanner.grouping import AlbumGroup

_ISO_LIKE_DATE = re.compile(
    r"^(?P<year>[0-9]{4})(?:-(?P<month>[0-9]{2})(?:-(?P<day>[0-9]{2}))?)?$"
)


# Release ranking asks which release plus medium fits a local group. It uses
# ordered evidence for ranking; the separate track mapper performs the final
# gap-aware alignment. A score is a comparison scale, not a probability.
class MatchClassification(StrEnum):
    """Review class derived from score, coverage, contradictions, and ambiguity."""

    HIGH = "high"
    REVIEW = "review"
    LOW = "low"


class LocalEvidenceSource(StrEnum):
    """Authority tier retained when filename evidence fills a missing tag."""

    TAG = "tag"
    FILENAME = "filename"
    MANUAL_OVERRIDE = "manual_override"


class MatchReasonCode(StrEnum):
    """Stable machine-readable reasons retained beside human-readable details."""

    ALBUM_TITLE_EXACT = "ALBUM_TITLE_EXACT"
    ALBUM_TITLE_SIMILARITY = "ALBUM_TITLE_SIMILARITY"
    ALBUM_TITLE_UNAVAILABLE = "ALBUM_TITLE_UNAVAILABLE"
    TRACK_TITLE_ORDER_EXACT = "TRACK_TITLE_ORDER_EXACT"
    TRACK_TITLE_ORDER_AGREEMENT = "TRACK_TITLE_ORDER_AGREEMENT"
    TRACK_TITLE_ORDER_UNAVAILABLE = "TRACK_TITLE_ORDER_UNAVAILABLE"
    TRACK_TITLE_NUMBER_EXACT = "TRACK_TITLE_NUMBER_EXACT"
    TRACK_TITLE_NUMBER_AGREEMENT = "TRACK_TITLE_NUMBER_AGREEMENT"
    TRACK_COMPARISON_PARTIAL = "TRACK_COMPARISON_PARTIAL"
    TRACK_COUNT_EXACT = "TRACK_COUNT_EXACT"
    TRACK_COUNT_CONTRADICTION = "TRACK_COUNT_CONTRADICTION"
    TRACK_COUNT_UNAVAILABLE = "TRACK_COUNT_UNAVAILABLE"
    PROVIDER_LIST_INCOMPLETE = "PROVIDER_LIST_INCOMPLETE"
    DURATION_CLOSE = "DURATION_CLOSE"
    DURATION_AGREEMENT = "DURATION_AGREEMENT"
    DURATION_LARGE_MISMATCH = "DURATION_LARGE_MISMATCH"
    DURATION_UNAVAILABLE = "DURATION_UNAVAILABLE"
    DISC_EXACT = "DISC_EXACT"
    DISC_CONTRADICTION = "DISC_CONTRADICTION"
    DISC_UNAVAILABLE = "DISC_UNAVAILABLE"
    YEAR_EXACT = "YEAR_EXACT"
    YEAR_NEAR = "YEAR_NEAR"
    YEAR_CONTRADICTION = "YEAR_CONTRADICTION"
    YEAR_UNAVAILABLE = "YEAR_UNAVAILABLE"
    ARTIST_EXACT = "ARTIST_EXACT"
    ARTIST_SIMILARITY = "ARTIST_SIMILARITY"
    ARTIST_UNAVAILABLE = "ARTIST_UNAVAILABLE"
    LANGUAGE_SCRIPT_MATCH = "LANGUAGE_SCRIPT_MATCH"
    LANGUAGE_SCRIPT_MISMATCH = "LANGUAGE_SCRIPT_MISMATCH"
    LANGUAGE_SCRIPT_UNAVAILABLE = "LANGUAGE_SCRIPT_UNAVAILABLE"
    LOCAL_ALBUM_CONFLICT = "LOCAL_ALBUM_CONFLICT"
    LOCAL_ARTIST_CONFLICT = "LOCAL_ARTIST_CONFLICT"
    LOCAL_YEAR_CONFLICT = "LOCAL_YEAR_CONFLICT"
    LOCAL_YEAR_INVALID = "LOCAL_YEAR_INVALID"
    LOCAL_DISC_CONFLICT = "LOCAL_DISC_CONFLICT"
    LOCAL_DISC_TOTAL_CONFLICT = "LOCAL_DISC_TOTAL_CONFLICT"
    LOCAL_TRACK_ORDER_TAGGED = "LOCAL_TRACK_ORDER_TAGGED"
    LOCAL_TRACK_ORDER_FILENAME = "LOCAL_TRACK_ORDER_FILENAME"
    LOCAL_TRACK_ORDER_AMBIGUOUS = "LOCAL_TRACK_ORDER_AMBIGUOUS"
    TRACK_TITLE_COVERAGE_INSUFFICIENT = "TRACK_TITLE_COVERAGE_INSUFFICIENT"
    AMBIGUOUS_TOP_CANDIDATES = "AMBIGUOUS_TOP_CANDIDATES"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    TRACK_TITLE_EXACT = "TRACK_TITLE_EXACT"
    TRACK_TITLE_SIMILARITY = "TRACK_TITLE_SIMILARITY"
    TRACK_TITLE_UNAVAILABLE = "TRACK_TITLE_UNAVAILABLE"
    TRACK_DURATION_CLOSE = "TRACK_DURATION_CLOSE"
    TRACK_DURATION_SIMILARITY = "TRACK_DURATION_SIMILARITY"
    TRACK_DURATION_LARGE_MISMATCH = "TRACK_DURATION_LARGE_MISMATCH"
    TRACK_DURATION_UNAVAILABLE = "TRACK_DURATION_UNAVAILABLE"
    TRACK_NUMBER_EXACT_TAG = "TRACK_NUMBER_EXACT_TAG"
    TRACK_NUMBER_EXACT_FILENAME = "TRACK_NUMBER_EXACT_FILENAME"
    TRACK_NUMBER_CONFLICT = "TRACK_NUMBER_CONFLICT"
    TRACK_NUMBER_UNAVAILABLE = "TRACK_NUMBER_UNAVAILABLE"
    TRACK_SEQUENCE_POSITION = "TRACK_SEQUENCE_POSITION"
    TRACK_MAPPING_COMPLETE = "TRACK_MAPPING_COMPLETE"
    TRACK_MAPPING_PARTIAL = "TRACK_MAPPING_PARTIAL"
    TRACK_MAPPING_AMBIGUOUS = "TRACK_MAPPING_AMBIGUOUS"
    TRACK_MAPPING_INSUFFICIENT_EVIDENCE = "TRACK_MAPPING_INSUFFICIENT_EVIDENCE"
    TRACK_CONTENT_SUPPORT_INSUFFICIENT = "TRACK_CONTENT_SUPPORT_INSUFFICIENT"
    MANUAL_TRACK_ASSIGNMENT = "MANUAL_TRACK_ASSIGNMENT"
    MANUAL_TRACK_UNMAPPED = "MANUAL_TRACK_UNMAPPED"


_STRONG_LOCAL_CONFLICTS = frozenset(
    {
        MatchReasonCode.LOCAL_ALBUM_CONFLICT,
        MatchReasonCode.LOCAL_ARTIST_CONFLICT,
        MatchReasonCode.LOCAL_YEAR_CONFLICT,
        MatchReasonCode.LOCAL_DISC_CONFLICT,
        MatchReasonCode.LOCAL_DISC_TOTAL_CONFLICT,
    }
)


def _typed_tuple[T](name: str, values: object, item_type: type[T]) -> tuple[T, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise TypeError(f"{name} must be an ordered sequence")

    copied = tuple(values)

    if any(not isinstance(value, item_type) for value in copied):
        raise TypeError(f"{name} must contain only {item_type.__name__} values")

    return copied


def _optional_positive_integer(name: str, value: object) -> None:
    if value is None:
        return

    if type(value) is not int:
        raise TypeError(f"{name} must be an integer or None")

    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")


def _optional_text(name: str, value: object) -> None:
    if value is not None and not isinstance(value, str):
        raise TypeError(f"{name} must be a string or None")


def _duration(value: object) -> float | None:
    if value is None:
        return None

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("duration_seconds must be a number or None")

    normalised = float(value)

    if not math.isfinite(normalised) or normalised < 0.0:
        raise ValueError("duration_seconds must be finite and cannot be negative")

    return normalised


@dataclass(frozen=True)
class MatchEvidence:
    """One weighted contribution or non-scoring diagnostic reason."""

    code: str
    contribution: float
    detail: str

    def __post_init__(self) -> None:
        if not isinstance(self.code, str):
            raise TypeError("code must be a string")

        if not self.code:
            raise ValueError("code must not be empty")

        if isinstance(self.contribution, bool) or not isinstance(self.contribution, (int, float)):
            raise TypeError("contribution must be a number")

        contribution = float(self.contribution)

        if not math.isfinite(contribution) or contribution < 0.0:
            raise ValueError("contribution must be finite and cannot be negative")

        if not isinstance(self.detail, str):
            raise TypeError("detail must be a string")

        if not self.detail.strip():
            raise ValueError("detail must not be empty")

        object.__setattr__(self, "contribution", contribution)


@dataclass(frozen=True)
class ReleaseScore:
    """A deterministic 0–100 ranking score and its complete explanation."""

    score: float
    classification: MatchClassification
    evidence: tuple[MatchEvidence, ...]

    def __post_init__(self) -> None:
        if isinstance(self.score, bool) or not isinstance(self.score, (int, float)):
            raise TypeError("score must be a number")

        score = float(self.score)

        if not math.isfinite(score) or not 0.0 <= score <= 100.0:
            raise ValueError("score must be finite and between zero and 100")

        if not isinstance(self.classification, MatchClassification):
            raise TypeError("classification must be MatchClassification")

        object.__setattr__(self, "score", score)
        object.__setattr__(self, "evidence", _typed_tuple("evidence", self.evidence, MatchEvidence))


@dataclass(frozen=True)
class LocalEvidenceNotice:
    """Non-scoring local conflict that made one evidence dimension unknown."""

    code: str
    detail: str

    def __post_init__(self) -> None:
        if not isinstance(self.code, str):
            raise TypeError("code must be a string")

        if not self.code:
            raise ValueError("code must not be empty")

        if not isinstance(self.detail, str):
            raise TypeError("detail must be a string")

        if not self.detail.strip():
            raise ValueError("detail must not be empty")


@dataclass(frozen=True)
class LocalTrackEvidence:
    """Comparison-only content and optional source numbering for one local file.

    Numbers retain their authority tier; they are not synthetic array positions.
    Existing callers may keep supplying only title and duration, which preserves
    the positional preliminary comparison until corroborated numbering exists.
    """

    title: str | None
    duration_seconds: float | None
    track_number: int | None = None
    track_number_source: LocalEvidenceSource | None = None

    def __post_init__(self) -> None:
        _optional_text("title", self.title)
        object.__setattr__(self, "duration_seconds", _duration(self.duration_seconds))
        _optional_positive_integer("track_number", self.track_number)

        if self.track_number_source is not None and not isinstance(self.track_number_source, LocalEvidenceSource):
            raise TypeError("track_number_source must be a LocalEvidenceSource or None")

        if self.track_number is None and self.track_number_source is not None:
            raise ValueError("track_number_source requires a track number")

        if self.track_number is not None and self.track_number_source is None:
            # Direct evidence follows the established disc-number constructor
            # convention. The scanner builder marks weaker filename hints.
            object.__setattr__(self, "track_number_source", LocalEvidenceSource.TAG)


@dataclass(frozen=True)
class LocalReleaseEvidence:
    """Conservative local group evidence with conflicts represented as unknown."""

    album_title: str | None
    artists: tuple[str, ...]
    year: int | None
    disc_number: int | None
    disc_total: int | None
    tracks: tuple[LocalTrackEvidence, ...]
    disc_number_source: LocalEvidenceSource | None = None
    notices: tuple[LocalEvidenceNotice, ...] = ()

    def __post_init__(self) -> None:
        _optional_text("album_title", self.album_title)
        artists = _typed_tuple("artists", self.artists, str)

        if any(not artist.strip() for artist in artists):
            raise ValueError("artists must not contain blank values")

        _optional_positive_integer("year", self.year)
        _optional_positive_integer("disc_number", self.disc_number)
        _optional_positive_integer("disc_total", self.disc_total)

        if self.disc_number_source is not None and not isinstance(
            self.disc_number_source,
            LocalEvidenceSource,
        ):
            raise TypeError("disc_number_source must be a LocalEvidenceSource or None")

        if self.disc_number is None and self.disc_number_source is not None:
            raise ValueError("disc_number_source requires a disc number")

        if self.disc_number is not None and self.disc_number_source is None:
            # Directly constructed evidence is authoritative by default. The
            # local-file builder explicitly marks weaker filename-derived values.
            object.__setattr__(self, "disc_number_source", LocalEvidenceSource.TAG)

        object.__setattr__(self, "artists", artists)
        object.__setattr__(self, "tracks", _typed_tuple("tracks", self.tracks, LocalTrackEvidence))
        object.__setattr__(self, "notices", _typed_tuple("notices", self.notices, LocalEvidenceNotice))


@dataclass(frozen=True)
class RankedReleaseMedium:
    """One independently scored release-medium combination."""

    release: ReleaseCandidate
    medium: ReleaseMedium
    medium_index: int
    result: ReleaseScore

    def __post_init__(self) -> None:
        if not isinstance(self.release, ReleaseCandidate):
            raise TypeError("release must be ReleaseCandidate")

        if not isinstance(self.medium, ReleaseMedium):
            raise TypeError("medium must be ReleaseMedium")

        if type(self.medium_index) is not int:
            raise TypeError("medium_index must be an integer")

        if self.medium_index < 0:
            raise ValueError("medium_index cannot be negative")

        if not isinstance(self.result, ReleaseScore):
            raise TypeError("result must be ReleaseScore")

    @property
    def identity(self) -> tuple[str, str, str, int]:
        """Return a compact stable identity suitable for logs and UI state."""
        return (
            self.release.engine_id,
            self.release.source_id,
            self.release.release_id,
            self.medium_index,
        )


@dataclass(frozen=True)
class ReleaseRanking:
    """Deterministically ordered release-medium results with ambiguity state."""

    entries: tuple[RankedReleaseMedium, ...]
    ambiguous: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "entries", _typed_tuple("entries", self.entries, RankedReleaseMedium))

        if not isinstance(self.ambiguous, bool):
            raise TypeError("ambiguous must be a bool")

    @property
    def identities(self) -> tuple[tuple[str, str, str, int], ...]:
        """Expose stable identities without discarding the full ranked entries."""
        return tuple(entry.identity for entry in self.entries)


def _clean_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None

    cleaned = " ".join(value.split())

    return cleaned or None


# Group equivalent spellings for comparison, then select an original spelling
# with explicit tie-breaks. Provider/input iteration order must not decide
# which text appears in evidence or which artist set is compared.
def _deterministic_unique(values: Iterable[str]) -> tuple[str, ...]:
    variants: dict[str, set[str]] = {}

    for value in values:
        cleaned = _clean_text(value)

        if cleaned is None:
            continue

        variants.setdefault(normalise_for_matching(cleaned), set()).add(cleaned)

    representatives = (
        min(originals, key=lambda item: (item.casefold(), item))
        for originals in variants.values()
    )

    return tuple(sorted(representatives, key=lambda item: (item.casefold(), item)))


def _parse_year(value: object) -> int | None:
    cleaned = _clean_text(value)

    if cleaned is None:
        return None

    # Validate the whole supported date shape before extracting its year.
    # Taking the first four characters of arbitrary text would turn malformed
    # metadata into apparently reliable release-year evidence.
    match = _ISO_LIKE_DATE.fullmatch(cleaned)

    if match is None:
        return None

    year = int(match.group("year"))
    month_text = match.group("month")
    day_text = match.group("day")

    if year == 0:
        return None

    if month_text is None:
        return year

    month = int(month_text)

    if not 1 <= month <= 12:
        return None

    if day_text is None:
        return year

    try:
        date(year, month, int(day_text))
    except ValueError:
        return None

    return year


def _present(file: LocalMediaFile, field: MetadataField) -> bool:
    return file.read_result.field_states[field] is FieldReadState.PRESENT


# Consensus means all usable values agree after conservative normalisation.
# Do not select the majority album from a mixed group: preserve a conflict
# notice and leave the value unknown so the reviewer can correct the group.
def _consensus_text(
    values: Iterable[str],
    *,
    conflict_code: MatchReasonCode,
    field_label: str,
) -> tuple[str | None, LocalEvidenceNotice | None]:
    variants: dict[str, set[str]] = {}

    for value in values:
        cleaned = _clean_text(value)

        if cleaned is not None:
            variants.setdefault(normalise_for_matching(cleaned), set()).add(cleaned)

    if not variants:
        return None, None

    if len(variants) > 1:
        return None, LocalEvidenceNotice(
            code=conflict_code,
            detail=f"Present local {field_label} values disagree, so this dimension is unknown.",
        )

    originals = next(iter(variants.values()))

    return min(originals, key=lambda item: (item.casefold(), item)), None


def _consensus_integer(
    values: Iterable[int],
    *,
    conflict_code: MatchReasonCode,
    field_label: str,
) -> tuple[int | None, LocalEvidenceNotice | None]:
    distinct = set(values)

    if not distinct:
        return None, None

    if len(distinct) > 1:
        return None, LocalEvidenceNotice(
            code=conflict_code,
            detail=f"Local {field_label} values disagree, so this dimension is unknown.",
        )

    return next(iter(distinct)), None


def _positive_integer(value: object) -> int | None:
    if type(value) is not int or value <= 0:
        return None

    return value


def _present_track_number(file: LocalMediaFile) -> int | None:
    if not _present(file, MetadataField.TRACK):
        return None

    return _positive_integer(file.read_result.metadata.track.number)


def order_local_track_files(
    files: tuple[LocalMediaFile, ...],
) -> tuple[tuple[LocalMediaFile, ...], LocalEvidenceNotice | None]:
    """Choose one evidence-based sequence shared by release and track matching.

    Complete unique track tags establish album order independently of filename
    spelling. Filename numbers are a fallback only when no usable tags exist;
    incomplete or duplicated tags keep the caller's order and record ambiguity.
    """
    tagged = tuple((file, _present_track_number(file)) for file in files)
    usable_tagged = tuple((file, number) for file, number in tagged if number is not None)

    # Choose one ordering tier for the entire group. Mixing a few real tags
    # with filename hints could silently assemble a sequence neither source
    # actually supports, so incomplete real numbering retains visible order.
    if usable_tagged:
        tagged_numbers = tuple(number for _file, number in usable_tagged)

        if len(usable_tagged) == len(files) and len(tagged_numbers) == len(set(tagged_numbers)):
            return (
                tuple(file for file, _number in sorted(usable_tagged, key=lambda item: item[1])),
                LocalEvidenceNotice(
                    code=MatchReasonCode.LOCAL_TRACK_ORDER_TAGGED,
                    detail="Local tracks were ordered by unique PRESENT track-number tags.",
                ),
            )

        return files, LocalEvidenceNotice(
            code=MatchReasonCode.LOCAL_TRACK_ORDER_AMBIGUOUS,
            detail="Present track-number tags are incomplete or duplicated; visible group order was retained.",
        )

    filename_numbered = tuple(
        (file, _positive_integer(file.filename_hints.track_number))
        for file in files
    )
    usable_filename = tuple(
        (file, number)
        for file, number in filename_numbered
        if number is not None
    )
    filename_numbers = tuple(number for _file, number in usable_filename)

    if len(usable_filename) == len(files) and len(filename_numbers) == len(set(filename_numbers)):
        return (
            tuple(file for file, _number in sorted(usable_filename, key=lambda item: item[1])),
            LocalEvidenceNotice(
                code=MatchReasonCode.LOCAL_TRACK_ORDER_FILENAME,
                detail="No usable track-number tags exist; local tracks were ordered by filename hints.",
            ),
        )

    return files, None


def _local_artists(files: tuple[LocalMediaFile, ...]) -> tuple[tuple[str, ...], LocalEvidenceNotice | None]:
    album_artist_sets: list[tuple[str, ...]] = []

    for file in files:
        if not _present(file, MetadataField.ALBUM_ARTISTS):
            continue

        artists = _deterministic_unique(file.read_result.metadata.album_artists)

        if artists:
            album_artist_sets.append(artists)

    if album_artist_sets:
        # Canonical equality is independent of the display representatives'
        # ordering: full-width A can sort after B although canonical a precedes b.
        normalised_sets = {
            frozenset(normalise_for_matching(artist) for artist in artists)
            for artists in album_artist_sets
        }

        if len(normalised_sets) > 1:
            return (), LocalEvidenceNotice(
                code=MatchReasonCode.LOCAL_ARTIST_CONFLICT,
                detail="Present local album-artist values disagree, so artist evidence is unknown.",
            )

        representative = min(
            album_artist_sets,
            key=lambda artists: tuple((artist.casefold(), artist) for artist in artists),
        )

        return representative, None

    # Track artists can legitimately vary across an album. When no album artist
    # exists, retain their deterministic union instead of labelling variation a conflict.
    return (
        _deterministic_unique(
            artist
            for file in files
            if _present(file, MetadataField.ARTISTS)
            for artist in file.read_result.metadata.artists
        ),
        None,
    )


def build_local_release_evidence(group: AlbumGroup) -> LocalReleaseEvidence:
    """Extract only trustworthy local values, with per-file filename fallback."""
    files = tuple(group.files)
    notices: list[LocalEvidenceNotice] = []
    album_title, album_notice = _consensus_text(
        (
            file.read_result.metadata.album
            for file in files
            if _present(file, MetadataField.ALBUM)
            if isinstance(file.read_result.metadata.album, str)
        ),
        conflict_code=MatchReasonCode.LOCAL_ALBUM_CONFLICT,
        field_label="album",
    )

    if album_notice is not None:
        notices.append(album_notice)

    artists, artist_notice = _local_artists(files)

    if artist_notice is not None:
        notices.append(artist_notice)

    # Read-state flags are part of the evidence contract. A stale value stored
    # beside an UNREADABLE or UNSUPPORTED state must not contribute a year.
    present_date_values = tuple(
        file.read_result.metadata.date
        for file in files
        if _present(file, MetadataField.DATE)
        if isinstance(file.read_result.metadata.date, str)
    )
    parsed_years = tuple(year for value in present_date_values if (year := _parse_year(value)) is not None)

    if len(parsed_years) != len(present_date_values):
        notices.append(
            LocalEvidenceNotice(
                code=MatchReasonCode.LOCAL_YEAR_INVALID,
                detail="At least one present local date is invalid, so it cannot contribute year evidence.",
            )
        )

    year, year_notice = _consensus_integer(
        parsed_years,
        conflict_code=MatchReasonCode.LOCAL_YEAR_CONFLICT,
        field_label="year",
    )

    if year_notice is not None:
        notices.append(year_notice)

    ordered_track_files, order_notice = order_local_track_files(files)

    if order_notice is not None:
        notices.append(order_notice)

    tagged_disc_numbers: list[int] = []
    filename_disc_numbers: list[int] = []
    disc_number_sources: list[LocalEvidenceSource] = []
    disc_totals: list[int] = []
    tracks: list[LocalTrackEvidence] = []

    # Select the numbering source globally, just as ordering does. A missing
    # tag among otherwise tagged files cannot be patched with a filename hint
    # to manufacture apparently complete, single-source numbering.
    has_tagged_track_numbers = any(_present_track_number(file) is not None for file in files)

    for file in ordered_track_files:
        if has_tagged_track_numbers:
            track_number = _present_track_number(file)
            track_number_source = LocalEvidenceSource.TAG
        else:
            track_number = _positive_integer(file.filename_hints.track_number)
            track_number_source = LocalEvidenceSource.FILENAME

        tracks.append(
            LocalTrackEvidence(
                title=effective_local_title(file),
                duration_seconds=file.read_result.stream_info.duration_seconds,
                track_number=track_number,
                track_number_source=track_number_source if track_number is not None else None,
            )
        )

    for file in files:
        if _present(file, MetadataField.DISC):
            tagged_disc_number = _positive_integer(file.read_result.metadata.disc.number)
            tagged_disc_total = _positive_integer(file.read_result.metadata.disc.total)

            if tagged_disc_number is not None:
                tagged_disc_numbers.append(tagged_disc_number)

            if tagged_disc_total is not None:
                disc_totals.append(tagged_disc_total)

        filename_disc_number = _positive_integer(file.filename_hints.disc_number)

        if filename_disc_number is not None:
            filename_disc_numbers.append(filename_disc_number)

    # The evidence tiers are global for the group: any usable real disc tag
    # outranks every filename hint. Filename consensus is considered only when
    # the group contains no usable tagged disc number at all.
    if tagged_disc_numbers:
        selected_disc_numbers = tagged_disc_numbers
        disc_number_sources.append(LocalEvidenceSource.TAG)
    else:
        selected_disc_numbers = filename_disc_numbers

        if filename_disc_numbers:
            disc_number_sources.append(LocalEvidenceSource.FILENAME)

    disc_number, disc_notice = _consensus_integer(
        selected_disc_numbers,
        conflict_code=MatchReasonCode.LOCAL_DISC_CONFLICT,
        field_label="disc-number",
    )
    disc_total, disc_total_notice = _consensus_integer(
        disc_totals,
        conflict_code=MatchReasonCode.LOCAL_DISC_TOTAL_CONFLICT,
        field_label="disc-total",
    )

    if disc_notice is not None:
        notices.append(disc_notice)

    if disc_total_notice is not None:
        notices.append(disc_total_notice)

    disc_number_source = None

    if disc_number is not None:
        disc_number_source = (
            LocalEvidenceSource.TAG
            if LocalEvidenceSource.TAG in disc_number_sources
            else LocalEvidenceSource.FILENAME
        )

    return LocalReleaseEvidence(
        album_title=album_title,
        artists=artists,
        year=year,
        disc_number=disc_number,
        disc_total=disc_total,
        tracks=tuple(tracks),
        disc_number_source=disc_number_source,
        notices=tuple(notices),
    )


@dataclass(frozen=True)
class _ScoringState:
    """Calculated ratio components plus separately prepared public evidence.

    Totals use a common normalised weight scale. Evidence contributions retain
    the raw policy's units for display; they must not be summed back into a score.
    """

    evidence: tuple[MatchEvidence, ...]
    weighted_total: float
    available_weight: float
    strong_contradiction: bool = False
    track_title_coverage: float = 0.0


def _evidence(code: str, contribution: float, detail: str) -> MatchEvidence:
    # Internal dimensions retain their unrounded agreement. The score state
    # prepares rounded raw-weight display contributions only after calculation.
    return MatchEvidence(code=code, contribution=contribution, detail=detail)


def _text_similarity(left: str, right: str) -> float:
    return ratio(normalise_for_matching(left), normalise_for_matching(right)) / 100.0


def _album_dimension(
    local: LocalReleaseEvidence,
    release: ReleaseCandidate,
    weight: float,
) -> tuple[MatchEvidence, float, bool]:
    # A provider may offer several original title variants. The strongest
    # comparison supplies this one dimension; variants do not add extra weight.
    provider_titles = tuple(title.value for title in release.titles if _clean_text(title.value) is not None)

    if local.album_title is None or not provider_titles:
        return (
            _evidence(
                MatchReasonCode.ALBUM_TITLE_UNAVAILABLE,
                0.0,
                "Album-title evidence is missing or conflicted on one side.",
            ),
            0.0,
            False,
        )

    similarity = max(_text_similarity(local.album_title, title) for title in provider_titles)
    code = (
        MatchReasonCode.ALBUM_TITLE_EXACT
        if similarity == 1.0
        else MatchReasonCode.ALBUM_TITLE_SIMILARITY
    )

    return (
        _evidence(code, weight * similarity, f"Best album-title similarity is {similarity * 100:.1f}%."),
        weight,
        False,
    )


def _best_provider_title_similarity(local_title: str, titles: tuple[LocalisedText, ...]) -> float | None:
    usable_titles = tuple(title.value for title in titles if _clean_text(title.value) is not None)

    if not usable_titles:
        return None

    return max(_text_similarity(local_title, title) for title in usable_titles)


@dataclass(frozen=True)
class _TrackComparison:
    """One reusable preliminary pairing, distinct from final track assignment."""

    pairs: tuple[tuple[LocalTrackEvidence, ProviderTrack], ...]
    uses_numbers: bool = False
    has_unpaired_tracks: bool = False


def _has_complete_unique_numbers(numbers: tuple[int | None, ...]) -> bool:
    """Require positive, unique source numbers throughout one sequence."""
    if not numbers or any(number is None for number in numbers):
        return False

    # The caller obtains provider numbers through the source's supported
    # numbering contract, so pregaps and non-numeric printed labels are absent.
    return len(set(numbers)) == len(numbers)


def _number_pair_has_content_support(
    local_track: LocalTrackEvidence,
    provider_track: ProviderTrack,
    policy: MatchingPolicy,
) -> bool:
    """Check independent content before allowing numbers to repair an offset.

    A usable title comparison takes precedence: equal/common durations must not
    rescue numbering that associates visibly different titles. A strong title
    may still establish correspondence when duration conflicts; the separate
    duration dimension must then report that real contradiction for review.
    """
    if local_track.title is not None:
        similarity = _best_provider_title_similarity(local_track.title, provider_track.titles)

        if similarity is not None:
            return similarity >= policy.track_mapping.minimum_content_title_similarity

    if local_track.duration_seconds is None or provider_track.duration_seconds is None:
        return False

    delta = abs(local_track.duration_seconds - provider_track.duration_seconds)

    return delta <= policy.track_mapping.close_duration_seconds


def _preliminary_track_comparison(
    local: LocalReleaseEvidence,
    medium: ReleaseMedium,
    policy: MatchingPolicy,
) -> _TrackComparison:
    """Use corroborated source numbers for partial albums; otherwise keep order.

    This is deliberately narrower than gap-aware mapping. For example, local
    tracks 2..10 should compare with provider tracks 2..10, rather than 1..9.
    Every shared number must be content-supported and the pairing must preserve
    order. Unknown, mixed, duplicated or misleading numbering leaves the
    existing positional result recoverable for later mapping and human review.
    """
    positional = _TrackComparison(
        pairs=tuple(zip(local.tracks, medium.tracks, strict=False)),
        has_unpaired_tracks=len(local.tracks) != len(medium.tracks),
    )
    local_numbers = tuple(track.track_number for track in local.tracks)
    provider_numbers = tuple(
        _positive_integer(track.track_number) if track.supports_automatic_numbering else None
        for track in medium.tracks
    )

    if not _has_complete_unique_numbers(local_numbers):
        return positional

    if not _has_complete_unique_numbers(provider_numbers):
        return positional

    source_tiers = {track.track_number_source for track in local.tracks}

    if None in source_tiers or len(source_tiers) != 1:
        return positional

    provider_by_number = {
        number: index
        for index, number in enumerate(provider_numbers)
        if number is not None
    }
    index_pairs = tuple(
        (local_index, provider_by_number[number])
        for local_index, number in enumerate(local_numbers)
        if number is not None and number in provider_by_number
    )

    if not index_pairs:
        return positional

    # A numerical match cannot authorise a crossing assignment. The selected
    # provider medium's documented sequence remains the order constraint.
    provider_indexes = tuple(provider_index for _local_index, provider_index in index_pairs)

    if provider_indexes != tuple(sorted(provider_indexes)):
        return positional

    pairs = tuple(
        (local.tracks[local_index], medium.tracks[provider_index])
        for local_index, provider_index in index_pairs
    )

    if not all(_number_pair_has_content_support(left, right, policy) for left, right in pairs):
        return positional

    positional_indexes = tuple((index, index) for index in range(len(positional.pairs)))

    if index_pairs == positional_indexes:
        # Keep the established ordered-title reason when numbering changes no
        # comparison. Number-specific reasons describe an actual repaired gap.
        return positional

    sequence_length = max(len(local.tracks), len(medium.tracks))

    return _TrackComparison(
        pairs=pairs,
        uses_numbers=True,
        has_unpaired_tracks=len(pairs) < sequence_length,
    )


def _track_title_dimension(
    local: LocalReleaseEvidence,
    medium: ReleaseMedium,
    weight: float,
    comparison: _TrackComparison,
) -> tuple[MatchEvidence, float, bool, float]:
    similarities: list[float] = []

    # Title and duration inspect the same preliminary pairs. Otherwise one
    # dimension could reward a repaired gap while another invents a conflict.
    for local_track, provider_track in comparison.pairs:
        if local_track.title is None:
            continue

        similarity = _best_provider_title_similarity(local_track.title, provider_track.titles)

        if similarity is not None:
            similarities.append(similarity)

    # Coverage measures how much of the larger sequence had usable title
    # pairs. Three exact pairs out of ten yield perfect title agreement but
    # only 0.3 coverage, which is too little to justify HIGH confidence.
    sequence_length = max(len(local.tracks), len(medium.tracks))
    coverage = len(similarities) / sequence_length if sequence_length else 0.0

    if not similarities:
        return (
            _evidence(
                MatchReasonCode.TRACK_TITLE_ORDER_UNAVAILABLE,
                0.0,
                "No local/provider title pairs are available in the preliminary correspondence.",
            ),
            0.0,
            False,
            coverage,
        )

    agreement = sum(similarities) / len(similarities)

    if comparison.uses_numbers:
        exact_code = MatchReasonCode.TRACK_TITLE_NUMBER_EXACT
        similar_code = MatchReasonCode.TRACK_TITLE_NUMBER_AGREEMENT
        method = "Content-supported source-number"
    else:
        exact_code = MatchReasonCode.TRACK_TITLE_ORDER_EXACT
        similar_code = MatchReasonCode.TRACK_TITLE_ORDER_AGREEMENT
        method = "Ordered"

    code = exact_code if agreement == 1.0 else similar_code

    return (
        _evidence(
            code,
            weight * agreement,
            (
                f"{method} title agreement is {agreement * 100:.1f}% across "
                f"{len(similarities)}/{sequence_length} positions."
            ),
        ),
        weight,
        False,
        coverage,
    )


def _track_count_dimension(
    local: LocalReleaseEvidence,
    medium: ReleaseMedium,
    weight: float,
) -> tuple[MatchEvidence, float, bool]:
    local_count = len(local.tracks)
    # A visible row count cannot establish an exact match when the provider
    # supplied only part of its listing or has unsupported count semantics.
    provider_count = medium.track_total

    if local_count == 0 or provider_count is None:
        return (
            _evidence(
                MatchReasonCode.TRACK_COUNT_UNAVAILABLE,
                0.0,
                "A complete local/provider count pair is unavailable for selected-medium comparison.",
            ),
            0.0,
            False,
        )

    if local_count == provider_count:
        return (
            _evidence(
                MatchReasonCode.TRACK_COUNT_EXACT,
                weight,
                f"Track count agrees: {local_count} local and {provider_count} selected-medium tracks.",
            ),
            weight,
            False,
        )

    # Use the smaller count divided by the larger so either missing or extra
    # tracks reduce agreement symmetrically. The explicit contradiction flag
    # also prevents a near count from yielding an unsupported HIGH result.
    largest_count = max(local_count, provider_count)
    similarity = min(local_count, provider_count) / largest_count if largest_count else 0.0

    return (
        _evidence(
            MatchReasonCode.TRACK_COUNT_CONTRADICTION,
            weight * similarity,
            f"Track count conflicts: {local_count} local and {provider_count} selected-medium tracks.",
        ),
        weight,
        True,
    )


def _duration_similarity(delta: float, policy: MatchingPolicy) -> float:
    close = policy.track_mapping.close_duration_seconds
    large = policy.track_mapping.large_duration_mismatch_seconds

    if delta <= close:
        return 1.0

    if delta >= large:
        return 0.0

    # Scale the gap between the tolerant and contradictory bounds onto 1..0.
    # At the midpoint between the default 3 s and 10 s limits, similarity is 0.5.
    return 1.0 - ((delta - close) / (large - close))


def _duration_dimension(
    comparison: _TrackComparison,
    weight: float,
    policy: MatchingPolicy,
) -> tuple[MatchEvidence, float, bool]:
    deltas = tuple(
        abs(local_track.duration_seconds - provider_track.duration_seconds)
        for local_track, provider_track in comparison.pairs
        if local_track.duration_seconds is not None and provider_track.duration_seconds is not None
    )

    if not deltas:
        return (
            _evidence(
                MatchReasonCode.DURATION_UNAVAILABLE,
                0.0,
                "No local/provider duration pairs are available in the preliminary correspondence.",
            ),
            0.0,
            False,
        )

    # Average only known duration pairs, but inspect the worst difference
    # separately. Many close tracks must not hide one strong duration conflict.
    similarity = sum(_duration_similarity(delta, policy) for delta in deltas) / len(deltas)
    largest_delta = max(deltas)
    has_large_mismatch = largest_delta >= policy.track_mapping.large_duration_mismatch_seconds

    if has_large_mismatch:
        code = MatchReasonCode.DURATION_LARGE_MISMATCH
    elif largest_delta <= policy.track_mapping.close_duration_seconds:
        code = MatchReasonCode.DURATION_CLOSE
    else:
        code = MatchReasonCode.DURATION_AGREEMENT

    return (
        _evidence(
            code,
            weight * similarity,
            f"Duration agreement uses {len(deltas)} pairs; largest difference is {largest_delta:.3f} seconds.",
        ),
        weight,
        has_large_mismatch,
    )


def _disc_dimension(
    local: LocalReleaseEvidence,
    release: ReleaseCandidate,
    medium: ReleaseMedium,
    weight: float,
) -> tuple[MatchEvidence, float, bool]:
    # Disc number and disc total are separate comparisons within one weight.
    # If both are known and only one agrees, this dimension earns half its
    # weight. Filename-only disc disagreement remains weaker than a real tag.
    comparisons: list[bool] = []
    descriptions: list[str] = []
    strong_contradiction = False

    if local.disc_number is not None and medium.medium_number is not None:
        disc_number_matches = local.disc_number == medium.medium_number
        disc_number_source = local.disc_number_source or LocalEvidenceSource.TAG
        comparisons.append(disc_number_matches)
        descriptions.append(
            f"disc {local.disc_number}/{medium.medium_number} ({disc_number_source.value})"
        )
        strong_contradiction = (
            not disc_number_matches
            and disc_number_source
            in {LocalEvidenceSource.TAG, LocalEvidenceSource.MANUAL_OVERRIDE}
        )

    provider_disc_total = release.disc_total

    if local.disc_total is not None and provider_disc_total is not None:
        disc_total_matches = local.disc_total == provider_disc_total
        comparisons.append(disc_total_matches)
        descriptions.append(f"disc total {local.disc_total}/{provider_disc_total}")
        strong_contradiction = strong_contradiction or not disc_total_matches

    if not comparisons:
        return (
            _evidence(
                MatchReasonCode.DISC_UNAVAILABLE,
                0.0,
                "Disc number/count evidence is unavailable on one side.",
            ),
            0.0,
            False,
        )

    agreement = sum(comparisons) / len(comparisons)
    exact = all(comparisons)

    return (
        _evidence(
            MatchReasonCode.DISC_EXACT if exact else MatchReasonCode.DISC_CONTRADICTION,
            weight * agreement,
            "Disc comparison: " + ", ".join(descriptions) + ".",
        ),
        weight,
        strong_contradiction,
    )


def _year_dimension(
    local: LocalReleaseEvidence,
    release: ReleaseCandidate,
    weight: float,
) -> tuple[MatchEvidence, float, bool]:
    provider_year = _parse_year(release.date)

    if local.year is None or provider_year is None:
        return (
            _evidence(
                MatchReasonCode.YEAR_UNAVAILABLE,
                0.0,
                "A valid local/provider year pair is unavailable.",
            ),
            0.0,
            False,
        )

    difference = abs(local.year - provider_year)

    if difference == 0:
        return (
            _evidence(
                MatchReasonCode.YEAR_EXACT,
                weight,
                f"Release years agree at {local.year}.",
            ),
            weight,
            False,
        )

    # A one-year difference can occur between release editions, so retain half
    # the year weight. Wider differences contribute zero and cap confidence.
    if difference == 1:
        return (
            _evidence(
                MatchReasonCode.YEAR_NEAR,
                weight * 0.5,
                f"Release years differ by one ({local.year} versus {provider_year}).",
            ),
            weight,
            False,
        )

    return (
        _evidence(
            MatchReasonCode.YEAR_CONTRADICTION,
            0.0,
            f"Release years strongly conflict ({local.year} versus {provider_year}).",
        ),
        weight,
        True,
    )


def _artist_dimension(
    local: LocalReleaseEvidence,
    release: ReleaseCandidate,
    weight: float,
) -> tuple[MatchEvidence, float, bool]:
    provider_artists = _deterministic_unique(release.album_artists)

    if not local.artists or not provider_artists:
        return (
            _evidence(
                MatchReasonCode.ARTIST_UNAVAILABLE,
                0.0,
                "Album-artist evidence is unavailable on one side.",
            ),
            0.0,
            False,
        )

    local_text = " ; ".join(normalise_for_matching(artist) for artist in local.artists)
    provider_text = " ; ".join(normalise_for_matching(artist) for artist in provider_artists)
    similarity = token_set_ratio(local_text, provider_text) / 100.0
    exact = set(normalise_for_matching(artist) for artist in local.artists) == set(
        normalise_for_matching(artist) for artist in provider_artists
    )

    # A fuzzy value helps ranking minor spelling variants; exact set agreement is
    # retained as its own reason so a reviewer can distinguish the two situations.
    return (
        _evidence(
            MatchReasonCode.ARTIST_EXACT if exact else MatchReasonCode.ARTIST_SIMILARITY,
            weight * similarity,
            f"Album-artist similarity is {similarity * 100:.1f}%.",
        ),
        weight,
        False,
    )


def _normalised_language(value: str | None) -> Language | None:
    if value is None:
        return None

    primary = value.strip().casefold().split("-", maxsplit=1)[0]

    if primary in {"ja", "jpn"}:
        return Language.JAPANESE

    if primary in {"ko", "kor"}:
        return Language.KOREAN

    return None


def _observed_scripts(text: str) -> frozenset[Script]:
    """Read actual characters; provider labels never add observed script."""
    return frozenset(item.script for item in build_language_profile((text,)).script_evidence)


def _variant_preserves_language(title: LocalisedText, language: Language) -> bool:
    """Evaluate native script and its language association within one variant.

    Kana and Hangul are direct native-language evidence even when provider
    labels are wrong. Han is shared among languages: Japanese association must
    belong to this same Han variant, not a romanised alias elsewhere. A Japanese
    script label may provide that association, but never manufacture characters.
    Korean Han alone does not meet the current Hangul-preservation contract.
    """
    scripts = _observed_scripts(title.value)

    if language is Language.KOREAN:
        return Script.HANGUL in scripts

    if Script.KANA in scripts:
        return True

    if Script.HAN not in scripts:
        return False

    declared_language = _normalised_language(title.language)

    if declared_language is Language.JAPANESE:
        return True

    # Only an absent language may defer to the script association. Explicit
    # non-Japanese language metadata must not be reassigned through another label.
    if title.language is not None:
        return False

    script_label = title.script.strip().casefold() if title.script is not None else None

    return script_label in {"jpan", "japanese"}


def _language_dimension(
    local: LocalReleaseEvidence,
    release: ReleaseCandidate,
    medium: ReleaseMedium,
    weight: float,
) -> tuple[MatchEvidence, float, bool]:
    # Infer local preference from its actual text, including artists, without
    # inferring Japanese or Chinese from Han alone. This is an offered-variant
    # preference for ranking; the proposal layer separately chooses written text.
    local_texts = (
        local.album_title,
        *local.artists,
        *(track.title for track in local.tracks),
    )
    local_profile = build_language_profile(local_texts)

    if local_profile.ambiguous or not local_profile.script_evidence:
        return (
            _evidence(
                MatchReasonCode.LANGUAGE_SCRIPT_UNAVAILABLE,
                0.0,
                "Local language/script evidence is absent or ambiguous.",
            ),
            0.0,
            False,
        )

    # Keep variants separate for the native-language decision. Combining the
    # Japanese label of a Latin alias with unrelated Han text would claim a
    # Japanese representation which no single offered variant actually supplies.
    provider_variants = (
        *release.titles,
        *(title for track in medium.tracks for title in track.titles),
        *(LocalisedText(artist, None, None) for artist in release.album_artists),
        *(LocalisedText(artist, None, None) for track in medium.tracks for artist in track.artists),
    )
    provider_scripts = frozenset(
        script
        for variant in provider_variants
        for script in _observed_scripts(variant.value)
    )

    if not provider_scripts:
        return (
            _evidence(
                MatchReasonCode.LANGUAGE_SCRIPT_UNAVAILABLE,
                0.0,
                "Provider text has no observed script evidence.",
            ),
            0.0,
            False,
        )

    preferred_language = local_profile.preferred_language

    if preferred_language is not None:
        matches = any(
            _variant_preserves_language(variant, preferred_language)
            for variant in provider_variants
        )
        language_label = "Japanese" if preferred_language is Language.JAPANESE else "Korean"
        relationship = "preserves" if matches else "does not preserve"
        detail = f"Observed provider text {relationship} the local {language_label} script preference."
    else:
        # With no strong native-language preference, ordinary observed-script
        # overlap remains useful (for example Latin text). Incidental Latin is
        # deliberately excluded from the Japanese/Korean branch above.
        local_scripts = {item.script for item in local_profile.script_evidence}
        matches = bool(local_scripts & provider_scripts)
        script_labels = ", ".join(sorted(script.value for script in local_scripts))
        relationship = "matches" if matches else "conflicts with"
        detail = f"Local script evidence ({script_labels}) {relationship} observed provider text."

    return (
        _evidence(
            MatchReasonCode.LANGUAGE_SCRIPT_MATCH if matches else MatchReasonCode.LANGUAGE_SCRIPT_MISMATCH,
            weight if matches else 0.0,
            detail,
        ),
        weight,
        False,
    )


def _score_state(
    local: LocalReleaseEvidence,
    release: ReleaseCandidate,
    medium: ReleaseMedium,
    policy: MatchingPolicy,
) -> _ScoringState:
    weights = policy.release_weights
    comparison = _preliminary_track_comparison(local, medium, policy)
    title_evidence, title_available, title_strong, coverage = _track_title_dimension(
        local,
        medium,
        1.0,
        comparison,
    )

    # Ask dimensions for agreement on a unit weight. This keeps the exact
    # fractional agreement separate from user-supplied weight magnitudes and
    # from the rounded contributions shown in the explanation dialog.
    # The returned availability is one for a comparison, zero for unknown.
    dimensions = (
        (weights.album_title, _album_dimension(local, release, 1.0)),
        (weights.track_title_order, (title_evidence, title_available, title_strong)),
        (weights.selected_medium_track_count, _track_count_dimension(local, medium, 1.0)),
        (weights.duration, _duration_dimension(comparison, 1.0, policy)),
        (weights.disc, _disc_dimension(local, release, medium, 1.0)),
        (weights.year, _year_dimension(local, release, 1.0)),
        (weights.artist, _artist_dimension(local, release, 1.0)),
        (weights.language_script, _language_dimension(local, release, medium, 1.0)),
    )

    # Dividing numerator and denominator by the same positive maximum leaves
    # their ratio unchanged. It avoids overflowing the sum of large weights,
    # and tiny common rescalings cannot disappear through display rounding.
    # Only available weights set the scale: a huge but unavailable dimension
    # must not underflow all the dimensions we can actually compare.
    available = tuple(
        (weight, item)
        for weight, (item, is_available, _is_strong) in dimensions
        if is_available
    )
    scale = max((weight for weight, _item in available), default=1.0)
    weighted_total = math.fsum(item.contribution * (weight / scale) for weight, item in available)
    available_weight = math.fsum(weight / scale for weight, _item in available)

    # Ratios smaller than floating-point representation can still underflow;
    # that is a representational limit, not a new minimum accepted weight.
    # Public evidence remains in familiar raw policy-weight units, rounded to
    # six decimals. Consumers must not reconstruct the ratio from these values.
    evidence = [
        replace(item, contribution=round(item.contribution * weight, SCORE_DECIMAL_PLACES))
        for weight, (item, _is_available, _is_strong) in dimensions
    ]
    strong_contradiction = any(is_strong for _weight, (_item, _available, is_strong) in dimensions)

    if comparison.has_unpaired_tracks:
        # Counts can match even if one file is missing and another is extra.
        # Supported common pairs remain useful, but that unresolved coverage
        # cannot become HIGH merely because the two totals happen to cancel.
        evidence.append(_evidence(
            MatchReasonCode.TRACK_COMPARISON_PARTIAL,
            0.0,
            "Preliminary track correspondence leaves local or provider tracks unpaired; review is required.",
        ))
        strong_contradiction = True

    evidence.extend(_evidence(notice.code, 0.0, notice.detail) for notice in local.notices)
    strong_contradiction = strong_contradiction or any(
        notice.code in _STRONG_LOCAL_CONFLICTS
        for notice in local.notices
    )

    return _ScoringState(
        evidence=tuple(evidence),
        weighted_total=weighted_total,
        available_weight=available_weight,
        strong_contradiction=strong_contradiction,
        track_title_coverage=coverage,
    )


def score_release_medium(
    local: LocalReleaseEvidence,
    release: ReleaseCandidate,
    medium: ReleaseMedium,
    policy: MatchingPolicy = DEFAULT_MATCHING_POLICY,
) -> ReleaseScore:
    """Score one release and one selected medium without flattening its siblings."""
    if not isinstance(local, LocalReleaseEvidence):
        raise TypeError("local must be LocalReleaseEvidence")

    if not isinstance(release, ReleaseCandidate):
        raise TypeError("release must be ReleaseCandidate")

    if not isinstance(medium, ReleaseMedium):
        raise TypeError("medium must be ReleaseMedium")

    if not isinstance(policy, MatchingPolicy):
        raise TypeError("policy must be MatchingPolicy")

    state = _score_state(local, release, medium, policy)
    evidence = list(state.evidence)

    if state.available_weight == 0.0:
        evidence.append(
            _evidence(
                MatchReasonCode.INSUFFICIENT_EVIDENCE,
                0.0,
                "No comparable evidence is available for this release and medium.",
            )
        )

        return ReleaseScore(
            score=0.0,
            classification=MatchClassification.LOW,
            evidence=tuple(evidence),
        )

    # Divide earned points by only the weights we could compare, then scale
    # to 100. If a missing year removes its default weight 5 and everything
    # else agrees, 95 / 95 still gives 100. A conflicting year remains in the
    # denominator, so 95 / 100 gives 95 and also forces review.
    score = round((state.weighted_total / state.available_weight) * 100.0, SCORE_DECIMAL_PLACES)
    thresholds = policy.classification
    provider_listing_complete = medium.tracks_complete and release.media_complete

    # Perfect agreement with every visible row can still describe a truncated
    # result. Keep its useful evidence, but do not represent it as HIGH confidence.
    if not provider_listing_complete:
        evidence.append(
            _evidence(
                MatchReasonCode.PROVIDER_LIST_INCOMPLETE,
                0.0,
                "Provider track or medium listing completeness is unestablished; this result requires review.",
            )
        )

    # Raw score and permission to call a result HIGH are separate decisions.
    # Adequate title coverage, complete provider listings and no strong
    # contradiction are all required even when the numerical score is perfect.
    if score >= thresholds.high:
        coverage_is_adequate = state.track_title_coverage >= thresholds.minimum_high_track_title_coverage

        if not coverage_is_adequate:
            evidence.append(
                _evidence(
                    MatchReasonCode.TRACK_TITLE_COVERAGE_INSUFFICIENT,
                    0.0,
                    (
                        f"Track-title coverage is {state.track_title_coverage:.3f}; "
                        f"HIGH requires at least {thresholds.minimum_high_track_title_coverage:.3f}."
                    ),
                )
            )

        classification = (
            MatchClassification.HIGH
            if coverage_is_adequate and provider_listing_complete and not state.strong_contradiction
            else MatchClassification.REVIEW
        )
    elif score >= thresholds.review:
        classification = MatchClassification.REVIEW
    else:
        classification = MatchClassification.LOW

    return ReleaseScore(
        score=score,
        classification=classification,
        evidence=tuple(evidence),
    )


# Sort score descending, then stable source identities and medium position.
# Explicit secondary keys prevent network response order from deciding an
# equal-score winner; an absent medium number sorts after known numbers.
def _ranking_key(entry: RankedReleaseMedium) -> tuple[object, ...]:
    release = entry.release
    medium_number = entry.medium.medium_number

    return (
        -entry.result.score,
        release.source_id.casefold(),
        release.source_id,
        release.release_id.casefold(),
        release.release_id,
        release.engine_id.casefold(),
        release.engine_id,
        medium_number is None,
        medium_number if medium_number is not None else 0,
        entry.medium_index,
    )


def _mark_ambiguous(entry: RankedReleaseMedium, difference: float) -> RankedReleaseMedium:
    ambiguity = _evidence(
        MatchReasonCode.AMBIGUOUS_TOP_CANDIDATES,
        0.0,
        f"This result is within {difference:.3f} points of the top result.",
    )

    return replace(
        entry,
        result=replace(
            entry.result,
            classification=MatchClassification.REVIEW,
            evidence=(*entry.result.evidence, ambiguity),
        ),
    )


def _unique_releases(releases: tuple[ReleaseCandidate, ...]) -> tuple[ReleaseCandidate, ...]:
    """Collapse equal full payloads; reject competing meanings of one identity.

    Release identity precedes medium enumeration. Comparing only the selected
    medium would miss conflicting sibling media, credits or completeness. The
    provider boundary must resolve genuine revisions with its explicit source
    policy; accepting whichever arrives first here would make ranking unstable.
    """
    by_identity: dict[tuple[str, str, str], ReleaseCandidate] = {}

    for release in releases:
        identity = (release.engine_id, release.source_id, release.release_id)
        existing = by_identity.get(identity)

        if existing is not None and existing != release:
            raise ValueError(f"conflicting release payloads for candidate identity {identity!r}")

        by_identity[identity] = release

    return tuple(by_identity.values())


def _is_plausible_near_top(score: float, top_score: float, policy: MatchingPolicy) -> bool:
    """Keep the inclusive review and ambiguity boundaries in one predicate."""
    reaches_review = score >= policy.classification.review
    within_margin = top_score - score <= policy.classification.ambiguity_margin

    return reaches_review and within_margin


def rank_release_candidates(
    local: LocalReleaseEvidence,
    releases: Sequence[ReleaseCandidate],
    policy: MatchingPolicy = DEFAULT_MATCHING_POLICY,
) -> ReleaseRanking:
    """Score every distinct release-medium combination without a score shortlist.

    Equal complete payloads with one identity collapse before ranking. Different
    payloads with that identity raise ValueError so callers cannot accidentally
    choose network-arrival precedence. Low results remain selectable for later
    mapping; distinct near-equal plausible candidates still require review.
    """
    if not isinstance(local, LocalReleaseEvidence):
        raise TypeError("local must be LocalReleaseEvidence")

    if not isinstance(policy, MatchingPolicy):
        raise TypeError("policy must be MatchingPolicy")

    validated_releases = _unique_releases(_typed_tuple("releases", releases, ReleaseCandidate))
    entries = [
        RankedReleaseMedium(
            release=release,
            medium=medium,
            medium_index=medium_index,
            result=score_release_medium(local, release, medium, policy),
        )
        for release in validated_releases
        for medium_index, medium in enumerate(release.media)
    ]
    entries.sort(key=_ranking_key)
    ambiguous = False

    # Ambiguity applies only when both leaders reach the REVIEW threshold.
    # A close runner-up that is itself implausible must not weaken the leader.
    # When tied plausible results exist, mark every entry within the margin.
    if len(entries) >= 2:
        top_score = entries[0].result.score
        second_score = entries[1].result.score
        ambiguous = _is_plausible_near_top(second_score, top_score, policy)

        if ambiguous:
            entries = [
                _mark_ambiguous(entry, top_score - entry.result.score)
                if _is_plausible_near_top(entry.result.score, top_score, policy)
                else entry
                for entry in entries
            ]

    return ReleaseRanking(entries=tuple(entries), ambiguous=ambiguous)
