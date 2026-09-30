"""Extract conservative release evidence from authorised local read states.

Consensus and ordering retain their notices alongside their named values. This
stage never compares a provider candidate or edits the supplied media files.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date

from metadata_polisher.domain.media import LocalMediaFile
from metadata_polisher.domain.metadata import FieldReadState, MetadataField
from metadata_polisher.matching.evidence import effective_local_title
from metadata_polisher.matching.normalisation import normalise_for_matching
from metadata_polisher.matching.release_models import (
    LocalEvidenceNotice,
    LocalEvidenceSource,
    LocalReleaseEvidence,
    LocalTrackEvidence,
    MatchReasonCode,
)
from metadata_polisher.scanner.grouping import AlbumGroup

_ISO_LIKE_DATE = re.compile(
    r"^(?P<year>[0-9]{4})(?:-(?P<month>[0-9]{2})(?:-(?P<day>[0-9]{2}))?)?$"
)


@dataclass(frozen=True)
class LocalEvidenceResult[T]:
    """A consensus value and the independent reason it became unknown, if any."""

    value: T
    notice: LocalEvidenceNotice | None = None


@dataclass(frozen=True)
class OrderedLocalTracks:
    """An evidence-authorised file sequence with its ordering explanation."""

    files: tuple[LocalMediaFile, ...]
    notice: LocalEvidenceNotice | None = None


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
) -> LocalEvidenceResult[str | None]:
    variants: dict[str, set[str]] = {}

    for value in values:
        cleaned = _clean_text(value)

        if cleaned is not None:
            variants.setdefault(normalise_for_matching(cleaned), set()).add(cleaned)

    if not variants:
        return LocalEvidenceResult(value=None)

    if len(variants) > 1:
        return LocalEvidenceResult(
            value=None,
            notice=LocalEvidenceNotice(
                code=conflict_code,
                detail=f"Present local {field_label} values disagree, so this dimension is unknown.",
            ),
        )

    originals = next(iter(variants.values()))

    return LocalEvidenceResult(value=min(originals, key=lambda item: (item.casefold(), item)))


def _consensus_integer(
    values: Iterable[int],
    *,
    conflict_code: MatchReasonCode,
    field_label: str,
) -> LocalEvidenceResult[int | None]:
    distinct = set(values)

    if not distinct:
        return LocalEvidenceResult(value=None)

    if len(distinct) > 1:
        return LocalEvidenceResult(
            value=None,
            notice=LocalEvidenceNotice(
                code=conflict_code,
                detail=f"Local {field_label} values disagree, so this dimension is unknown.",
            ),
        )

    return LocalEvidenceResult(value=next(iter(distinct)))


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
) -> OrderedLocalTracks:
    """Choose one evidence-based sequence shared by release and track matching.

    Complete unique track tags establish album order independently of filename
    spelling. Filename numbers are a fallback only when no usable tags exist;
    incomplete or duplicated tags keep the caller's order and record ambiguity.
    """
    tagged_files: dict[int, LocalMediaFile] = {}

    for file in files:
        number = _present_track_number(file)

        if number is not None:
            tagged_files[number] = file

    # Choose one ordering tier for the entire group. Mixing a few real tags
    # with filename hints could silently assemble a sequence neither source
    # actually supports, so incomplete real numbering retains visible order.
    if tagged_files:
        # A dictionary has one entry per unique usable number. Its size matches
        # the input only when every file supplied a different positive number;
        # missing and duplicated numbers both retain the original visible order.
        if len(tagged_files) == len(files):
            return OrderedLocalTracks(
                files=tuple(tagged_files[number] for number in sorted(tagged_files)),
                notice=LocalEvidenceNotice(
                    code=MatchReasonCode.LOCAL_TRACK_ORDER_TAGGED,
                    detail="Local tracks were ordered by unique PRESENT track-number tags.",
                ),
            )

        return OrderedLocalTracks(
            files=files,
            notice=LocalEvidenceNotice(
                code=MatchReasonCode.LOCAL_TRACK_ORDER_AMBIGUOUS,
                detail="Present track-number tags are incomplete or duplicated; visible group order was retained.",
            ),
        )

    filename_files: dict[int, LocalMediaFile] = {}

    for file in files:
        number = _positive_integer(file.filename_hints.track_number)

        if number is not None:
            filename_files[number] = file

    if len(filename_files) == len(files):
        return OrderedLocalTracks(
            files=tuple(filename_files[number] for number in sorted(filename_files)),
            notice=LocalEvidenceNotice(
                code=MatchReasonCode.LOCAL_TRACK_ORDER_FILENAME,
                detail="No usable track-number tags exist; local tracks were ordered by filename hints.",
            ),
        )

    return OrderedLocalTracks(files=files)


def _local_artists(files: tuple[LocalMediaFile, ...]) -> LocalEvidenceResult[tuple[str, ...]]:
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
            return LocalEvidenceResult(
                value=(),
                notice=LocalEvidenceNotice(
                    code=MatchReasonCode.LOCAL_ARTIST_CONFLICT,
                    detail="Present local album-artist values disagree, so artist evidence is unknown.",
                ),
            )

        representative = min(
            album_artist_sets,
            key=lambda artists: tuple((artist.casefold(), artist) for artist in artists),
        )

        return LocalEvidenceResult(value=representative)

    # Track artists can legitimately vary across an album. When no album artist
    # exists, retain their deterministic union instead of labelling variation a conflict.
    return LocalEvidenceResult(
        value=_deterministic_unique(
            artist
            for file in files
            if _present(file, MetadataField.ARTISTS)
            for artist in file.read_result.metadata.artists
        ),
    )


def build_local_release_evidence(group: AlbumGroup) -> LocalReleaseEvidence:
    """Extract only trustworthy local values, with per-file filename fallback."""
    files = tuple(group.files)
    notices: list[LocalEvidenceNotice] = []
    album = _consensus_text(
        (
            file.read_result.metadata.album
            for file in files
            if _present(file, MetadataField.ALBUM)
            if isinstance(file.read_result.metadata.album, str)
        ),
        conflict_code=MatchReasonCode.LOCAL_ALBUM_CONFLICT,
        field_label="album",
    )

    if album.notice is not None:
        notices.append(album.notice)

    artists = _local_artists(files)

    if artists.notice is not None:
        notices.append(artists.notice)

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

    year_consensus = _consensus_integer(
        parsed_years,
        conflict_code=MatchReasonCode.LOCAL_YEAR_CONFLICT,
        field_label="year",
    )

    if year_consensus.notice is not None:
        notices.append(year_consensus.notice)

    ordered_tracks = order_local_track_files(files)

    if ordered_tracks.notice is not None:
        notices.append(ordered_tracks.notice)

    tagged_disc_numbers: list[int] = []
    filename_disc_numbers: list[int] = []
    disc_number_sources: list[LocalEvidenceSource] = []
    disc_totals: list[int] = []
    tracks: list[LocalTrackEvidence] = []

    # Select the numbering source globally, just as ordering does. A missing
    # tag among otherwise tagged files cannot be patched with a filename hint
    # to manufacture apparently complete, single-source numbering.
    has_tagged_track_numbers = any(_present_track_number(file) is not None for file in files)

    for file in ordered_tracks.files:
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

    disc_number = _consensus_integer(
        selected_disc_numbers,
        conflict_code=MatchReasonCode.LOCAL_DISC_CONFLICT,
        field_label="disc-number",
    )
    disc_total = _consensus_integer(
        disc_totals,
        conflict_code=MatchReasonCode.LOCAL_DISC_TOTAL_CONFLICT,
        field_label="disc-total",
    )

    if disc_number.notice is not None:
        notices.append(disc_number.notice)

    if disc_total.notice is not None:
        notices.append(disc_total.notice)

    disc_number_source = None

    if disc_number.value is not None:
        disc_number_source = (
            LocalEvidenceSource.TAG
            if LocalEvidenceSource.TAG in disc_number_sources
            else LocalEvidenceSource.FILENAME
        )

    return LocalReleaseEvidence(
        album_title=album.value,
        artists=artists.value,
        year=year_consensus.value,
        disc_number=disc_number.value,
        disc_total=disc_total.value,
        tracks=tuple(tracks),
        disc_number_source=disc_number_source,
        notices=tuple(notices),
    )


