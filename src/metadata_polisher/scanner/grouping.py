"""Pure, conservative grouping of scanned files into probable albums."""

import unicodedata
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from metadata_polisher.domain.media import LocalMediaFile
from metadata_polisher.domain.metadata import FieldReadState, MetadataField


class GroupingReason(StrEnum):
    """Stable reason explaining how a probable group was formed."""

    DIRECTORY_ALBUM_CONSISTENT = "DIRECTORY_ALBUM_CONSISTENT"
    DIRECTORY_WITHOUT_ALBUM = "DIRECTORY_WITHOUT_ALBUM"
    DISTINCT_ALBUM_TAGS = "DISTINCT_ALBUM_TAGS"
    TRACK_RESTART_WITH_DISC_CHANGE = "TRACK_RESTART_WITH_DISC_CHANGE"
    UNTAGGED_FILES_BESIDE_DISTINCT_ALBUMS = "UNTAGGED_FILES_BESIDE_DISTINCT_ALBUMS"
    MANUAL_SPLIT = "MANUAL_SPLIT"
    MANUAL_MERGE = "MANUAL_MERGE"


class GroupingWarningCode(StrEnum):
    """Stable user-facing grouping warning categories."""

    POSSIBLE_MULTIPLE_ALBUMS = "POSSIBLE_MULTIPLE_ALBUMS"


class GroupingWarningReason(StrEnum):
    """Specific evidence behind a conservative grouping warning."""

    TRACK_NUMBER_RESTART_WITHOUT_STRONG_TAG_EVIDENCE = "TRACK_NUMBER_RESTART_WITHOUT_STRONG_TAG_EVIDENCE"
    MISSING_ALBUM_BESIDE_DISTINCT_ALBUM_TAGS = "MISSING_ALBUM_BESIDE_DISTINCT_ALBUM_TAGS"


@dataclass(frozen=True)
class AlbumGroup:
    """An immutable probable album/disc group containing each file at most once."""

    group_id: str
    files: tuple[LocalMediaFile, ...]
    album_title: str | None
    reason: GroupingReason

    def __post_init__(self) -> None:
        files = tuple(self.files)

        if not self.group_id:
            raise ValueError("group_id must not be empty")

        if not files:
            raise ValueError("an album group must contain at least one file")

        file_ids = tuple(file.file_id for file in files)

        if len(file_ids) != len(set(file_ids)):
            raise ValueError("an album group cannot contain duplicate file IDs")

        object.__setattr__(self, "files", files)


@dataclass(frozen=True)
class GroupingWarning:
    """Structured warning for evidence that is too weak to split automatically."""

    code: GroupingWarningCode
    reason: GroupingWarningReason
    group_id: str
    affected_file_ids: tuple[str, ...]
    message: str

    def __post_init__(self) -> None:
        affected_file_ids = tuple(self.affected_file_ids)

        if not self.group_id:
            raise ValueError("a grouping warning must refer to a group ID")

        if not affected_file_ids:
            raise ValueError("a grouping warning must identify at least one affected file")

        if len(affected_file_ids) != len(set(affected_file_ids)):
            raise ValueError("grouping warning affected file IDs must be unique")

        if not self.message.strip():
            raise ValueError("a grouping warning message must not be empty")

        object.__setattr__(self, "affected_file_ids", affected_file_ids)


@dataclass(frozen=True)
class GroupingResult:
    """Immutable groups and conservative warnings produced from one scan."""

    groups: tuple[AlbumGroup, ...]
    warnings: tuple[GroupingWarning, ...]

    def __post_init__(self) -> None:
        groups = tuple(self.groups)
        warnings = tuple(self.warnings)
        group_ids = tuple(group.group_id for group in groups)

        if len(group_ids) != len(set(group_ids)):
            raise ValueError("group IDs must be unique")

        file_ids = tuple(file.file_id for group in groups for file in group.files)

        if len(file_ids) != len(set(file_ids)):
            raise ValueError("each file ID must belong to exactly one group")

        # Warnings must name actual members of their group. This keeps a
        # review warning from pointing at unrelated files after regrouping.
        files_by_group_id = {group.group_id: {file.file_id for file in group.files} for group in groups}

        for warning in warnings:
            group_file_ids = files_by_group_id.get(warning.group_id)

            if group_file_ids is None:
                raise ValueError("every grouping warning must refer to an existing group")

            if not set(warning.affected_file_ids) <= group_file_ids:
                raise ValueError("grouping warning affected file IDs must belong to its group")

        object.__setattr__(self, "groups", groups)
        object.__setattr__(self, "warnings", warnings)


def _text_sort_key(value: str) -> tuple[str, str]:
    return value.casefold(), value


def _path_sort_key(path: Path) -> tuple[str, str]:
    portable_path = path.as_posix()

    return portable_path.casefold(), portable_path


def _file_sort_key(file: LocalMediaFile) -> tuple[str, str, str]:
    folded_path, exact_path = _path_sort_key(file.path)

    return folded_path, exact_path, file.file_id


def _normalise_album_for_grouping(value: str) -> str:
    # Compatibility normalisation and whitespace collapse prevent formatting
    # differences from being treated as strong evidence for different albums.
    compatibility_text = unicodedata.normalize("NFKC", value)

    return " ".join(compatibility_text.split()).casefold()


def _present_album(file: LocalMediaFile) -> tuple[str, str] | None:
    if file.read_result.field_states[MetadataField.ALBUM] is not FieldReadState.PRESENT:
        return None

    album = file.read_result.metadata.album

    if not isinstance(album, str):
        return None

    display_value = " ".join(album.split())

    if not display_value:
        return None

    return _normalise_album_for_grouping(display_value), display_value


def derive_album_title(files: Iterable[LocalMediaFile]) -> str | None:
    """Return one deterministic title only when present album evidence agrees."""
    display_values: dict[str, set[str]] = defaultdict(set)

    for file in files:
        album = _present_album(file)

        if album is not None:
            comparison_value, display_value = album
            display_values[comparison_value].add(display_value)

    # No evidence and conflicting evidence both lack a single safe title.
    # Equivalent spellings still share one comparison key and retain an
    # original display spelling chosen deterministically below.
    if len(display_values) != 1:
        return None

    comparison_value = next(iter(display_values))

    return min(display_values[comparison_value], key=_text_sort_key)


def _track_number(file: LocalMediaFile) -> int | None:
    metadata_track = file.read_result.metadata.track.number

    if file.read_result.field_states[MetadataField.TRACK] is FieldReadState.PRESENT and metadata_track is not None:
        return metadata_track

    # Filename hints are deliberately only weak evidence. They can trigger a
    # review warning here, but they never become metadata or cause an auto-split.
    return file.filename_hints.track_number


def _first_track_restart(files: tuple[LocalMediaFile, ...]) -> tuple[str, str] | None:
    # Look for a return to track 1 after a known larger number. Unknown
    # numbers are skipped, because they neither prove nor disprove a restart.
    # This helper only supplies a warning; stronger evidence controls splits.
    previous_file: LocalMediaFile | None = None
    previous_number: int | None = None

    for file in files:
        current_number = _track_number(file)

        if previous_file is not None and previous_number is not None and previous_number > 1 and current_number == 1:
            return previous_file.file_id, file.file_id

        if current_number is not None:
            previous_file = file
            previous_number = current_number

    return None


# A split requires two adjacent files with readable track AND disc tags,
# a restart at track 1 and a change of positive disc number. A filename
# hint or an isolated changed disc value cannot prove that boundary.
def _is_strong_disc_boundary(previous: LocalMediaFile, current: LocalMediaFile) -> bool:
    previous_states = previous.read_result.field_states
    current_states = current.read_result.field_states

    if (
        previous_states[MetadataField.TRACK] is not FieldReadState.PRESENT
        or current_states[MetadataField.TRACK] is not FieldReadState.PRESENT
        or previous_states[MetadataField.DISC] is not FieldReadState.PRESENT
        or current_states[MetadataField.DISC] is not FieldReadState.PRESENT
    ):
        return False

    previous_track = previous.read_result.metadata.track.number
    current_track = current.read_result.metadata.track.number
    previous_disc = previous.read_result.metadata.disc.number
    current_disc = current.read_result.metadata.disc.number

    return (
        previous_track is not None
        and current_track is not None
        and previous_disc is not None
        and current_disc is not None
        and previous_disc > 0
        and current_disc > 0
        and previous_track > 1
        and current_track == 1
        and previous_disc != current_disc
    )


def _split_at_strong_disc_boundaries(
    files: tuple[LocalMediaFile, ...],
) -> tuple[tuple[LocalMediaFile, ...], ...]:
    boundary_indexes = tuple(
        index for index in range(1, len(files)) if _is_strong_disc_boundary(files[index - 1], files[index])
    )

    if not boundary_indexes:
        return (files,)

    segments: list[tuple[LocalMediaFile, ...]] = []
    start = 0

    for boundary_index in boundary_indexes:
        segments.append(files[start:boundary_index])
        start = boundary_index

    segments.append(files[start:])

    return tuple(segments)


@dataclass(frozen=True)
class _GroupDraft:
    files: tuple[LocalMediaFile, ...]
    album_title: str | None
    reason: GroupingReason
    warning_reason: GroupingWarningReason | None = None


def _drafts_with_strong_disc_boundaries(
    files: tuple[LocalMediaFile, ...],
    album_title: str | None,
    fallback_reason: GroupingReason,
    warning_reason: GroupingWarningReason | None = None,
) -> tuple[_GroupDraft, ...]:
    segments = _split_at_strong_disc_boundaries(files)
    reason = (
        GroupingReason.TRACK_RESTART_WITH_DISC_CHANGE
        if len(segments) >= 2
        else fallback_reason
    )

    return tuple(
        _GroupDraft(
            files=segment,
            album_title=album_title,
            reason=reason,
            warning_reason=warning_reason,
        )
        for segment in segments
    )


def _next_group_id(number: int) -> str:
    return f"group-{number:04d}"


def group_scanned_files(files: Iterable[LocalMediaFile]) -> GroupingResult:
    """Group files by directory, splitting only on unambiguous real album tags."""
    # Start with physical directories, then refine only where strong tags
    # justify it. Files in different folders are never merged automatically.
    directory_buckets: dict[Path, list[LocalMediaFile]] = defaultdict(list)

    for file in files:
        directory_buckets[file.path.parent].append(file)

    groups: list[AlbumGroup] = []
    warnings: list[GroupingWarning] = []

    for directory in sorted(directory_buckets, key=_path_sort_key):
        directory_files = tuple(sorted(directory_buckets[directory], key=_file_sort_key))
        album_buckets: dict[str, list[LocalMediaFile]] = defaultdict(list)
        display_values: dict[str, set[str]] = defaultdict(set)
        untagged_files: list[LocalMediaFile] = []

        for file in directory_files:
            album = _present_album(file)

            if album is None:
                untagged_files.append(file)
                continue

            comparison_value, display_value = album
            album_buckets[comparison_value].append(file)
            display_values[comparison_value].add(display_value)

        drafts: list[_GroupDraft] = []

        # Distinct non-empty album tags justify separate groups. Untagged files
        # cannot be assigned to either album safely, so retain a residual group
        # with a warning rather than guessing from neighbouring filenames.
        if len(album_buckets) >= 2:
            for comparison_value in sorted(album_buckets, key=_text_sort_key):
                tagged_files = tuple(
                    sorted(album_buckets[comparison_value], key=_file_sort_key)
                )
                drafts.extend(
                    _drafts_with_strong_disc_boundaries(
                        files=tagged_files,
                        album_title=min(
                            display_values[comparison_value], key=_text_sort_key
                        ),
                        fallback_reason=GroupingReason.DISTINCT_ALBUM_TAGS,
                    )
                )

            if untagged_files:
                residual_files = tuple(sorted(untagged_files, key=_file_sort_key))
                drafts.extend(
                    _drafts_with_strong_disc_boundaries(
                        files=residual_files,
                        album_title=None,
                        fallback_reason=(
                            GroupingReason.UNTAGGED_FILES_BESIDE_DISTINCT_ALBUMS
                        ),
                        warning_reason=(GroupingWarningReason.MISSING_ALBUM_BESIDE_DISTINCT_ALBUM_TAGS),
                    )
                )
        else:
            fallback_reason = (
                GroupingReason.DIRECTORY_ALBUM_CONSISTENT
                if album_buckets
                else GroupingReason.DIRECTORY_WITHOUT_ALBUM
            )
            strong_segments = _split_at_strong_disc_boundaries(directory_files)

            for segment in strong_segments:
                segment_reason = (
                    GroupingReason.TRACK_RESTART_WITH_DISC_CHANGE
                    if len(strong_segments) >= 2
                    else fallback_reason
                )
                drafts.append(
                    _GroupDraft(
                        files=segment,
                        album_title=derive_album_title(segment),
                        reason=segment_reason,
                    )
                )

        # Assign IDs only after deterministic directory/album ordering. Drafts
        # let split decisions settle before warnings refer to the final group IDs.
        for draft in drafts:
            group = AlbumGroup(
                group_id=_next_group_id(len(groups) + 1),
                files=draft.files,
                album_title=draft.album_title,
                reason=draft.reason,
            )
            groups.append(group)

            if draft.warning_reason is not None:
                warnings.append(
                    GroupingWarning(
                        code=GroupingWarningCode.POSSIBLE_MULTIPLE_ALBUMS,
                        reason=draft.warning_reason,
                        group_id=group.group_id,
                        affected_file_ids=tuple(file.file_id for file in group.files),
                        message=(
                            "Files without readable album tags could not be assigned to one "
                            "of the distinct tagged albums."
                        ),
                    )
                )

            restart = _first_track_restart(group.files)

            if restart is not None:
                warnings.append(
                    GroupingWarning(
                        code=GroupingWarningCode.POSSIBLE_MULTIPLE_ALBUMS,
                        reason=(GroupingWarningReason.TRACK_NUMBER_RESTART_WITHOUT_STRONG_TAG_EVIDENCE),
                        group_id=group.group_id,
                        affected_file_ids=restart,
                        message=(
                            "Track numbering restarts, but the tags do not provide enough "
                            "evidence to split this directory automatically."
                        ),
                    )
                )

    return GroupingResult(groups=tuple(groups), warnings=tuple(warnings))
