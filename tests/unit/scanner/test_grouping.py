from pathlib import Path

import pytest

from metadata_polisher.domain.media import FilenameHints, LocalMediaFile, MediaReadResult, StreamInfo
from metadata_polisher.domain.metadata import FieldReadState, MetadataField, MetadataSnapshot, Position
from metadata_polisher.scanner.grouping import (
    AlbumGroup,
    GroupingReason,
    GroupingResult,
    GroupingWarning,
    GroupingWarningCode,
    GroupingWarningReason,
    group_scanned_files,
)


# Keep tag states independent of numeric values so tests can distinguish
# strong readable disc boundaries from stale or unreadable values.
def make_media_file(
    path: str,
    *,
    album: str | None = None,
    track_number: int | None = None,
    disc_number: int | None = None,
    track_state: FieldReadState | None = None,
    disc_state: FieldReadState | None = None,
    filename_hints: FilenameHints | None = None,
) -> LocalMediaFile:
    states = {field: FieldReadState.MISSING for field in MetadataField}

    if album is not None:
        states[MetadataField.ALBUM] = FieldReadState.PRESENT

    states[MetadataField.TRACK] = track_state or (
        FieldReadState.PRESENT if track_number is not None else FieldReadState.MISSING
    )
    states[MetadataField.DISC] = disc_state or (
        FieldReadState.PRESENT if disc_number is not None else FieldReadState.MISSING
    )

    return LocalMediaFile(
        path=Path(path),
        format_id="flac",
        read_result=MediaReadResult(
            metadata=MetadataSnapshot(
                album=album,
                track=Position(number=track_number),
                disc=Position(number=disc_number),
            ),
            field_states=states,
            stream_info=StreamInfo(
                duration_seconds=180.0,
                sample_rate=48_000,
                channels=2,
                bit_depth=24,
                codec="FLAC",
            ),
        ),
        filename_hints=filename_hints or FilenameHints(),
    )


def test_one_folder_with_a_consistent_album_tag_stays_one_group() -> None:
    second = make_media_file("library/Album/02 - Second.flac", album="Example Album", track_number=2)
    first = make_media_file("library/Album/01 - First.flac", album="Example Album", track_number=1)

    result = group_scanned_files((second, first))

    assert len(result.groups) == 1
    assert result.groups[0].group_id == "group-0001"
    assert result.groups[0].album_title == "Example Album"
    assert result.groups[0].reason is GroupingReason.DIRECTORY_ALBUM_CONSISTENT
    assert tuple(file.path for file in result.groups[0].files) == (first.path, second.path)
    assert result.warnings == ()


def test_two_clearly_different_album_tags_split_one_folder() -> None:
    album_b_second = make_media_file("library/Mixed/04.flac", album="Album B", track_number=2)
    album_a_first = make_media_file("library/Mixed/01.flac", album="Album A", track_number=1)
    album_b_first = make_media_file("library/Mixed/03.flac", album="Album B", track_number=1)
    album_a_second = make_media_file("library/Mixed/02.flac", album="Album A", track_number=2)

    result = group_scanned_files((album_b_second, album_a_first, album_b_first, album_a_second))

    assert tuple(group.group_id for group in result.groups) == ("group-0001", "group-0002")
    assert tuple(group.album_title for group in result.groups) == ("Album A", "Album B")
    assert all(group.reason is GroupingReason.DISTINCT_ALBUM_TAGS for group in result.groups)
    assert tuple(file.path for file in result.groups[0].files) == (
        album_a_first.path,
        album_a_second.path,
    )
    assert tuple(file.path for file in result.groups[1].files) == (
        album_b_first.path,
        album_b_second.path,
    )
    assert result.warnings == ()


# Track 2 followed by track 1 suggests a boundary, but no album/disc evidence
# proves it. The warning names the two files that triggered the suspicion.
def test_track_restart_without_album_evidence_warns_but_does_not_split() -> None:
    files = (
        make_media_file("library/Unknown/01.flac", track_number=1),
        make_media_file("library/Unknown/02.flac", track_number=2),
        make_media_file("library/Unknown/03.flac", track_number=1),
        make_media_file("library/Unknown/04.flac", track_number=2),
    )

    result = group_scanned_files(files)

    assert len(result.groups) == 1
    assert result.groups[0].files == files
    assert len(result.warnings) == 1
    warning = result.warnings[0]
    assert warning.code is GroupingWarningCode.POSSIBLE_MULTIPLE_ALBUMS
    assert warning.reason is GroupingWarningReason.TRACK_NUMBER_RESTART_WITHOUT_STRONG_TAG_EVIDENCE
    assert warning.group_id == result.groups[0].group_id
    assert warning.affected_file_ids == (files[1].file_id, files[2].file_id)


def test_nested_directories_remain_separate_initial_groups() -> None:
    nested = make_media_file("library/Album/Disc 2/01.flac", album="Album")
    parent = make_media_file("library/Album/01.flac", album="Album")

    result = group_scanned_files((nested, parent))

    assert len(result.groups) == 2
    assert tuple(group.files for group in result.groups) == ((parent,), (nested,))


# Reversing input must preserve IDs as well as membership, since UI review
# state later refers to stable group identities.
def test_group_ids_order_and_membership_are_deterministic_for_input_order() -> None:
    files = (
        make_media_file("library/z-folder/02.flac", album="Beta"),
        make_media_file("library/A-folder/03.flac", album="Zulu"),
        make_media_file("library/A-folder/01.flac", album="Alpha"),
        make_media_file("library/z-folder/01.flac", album="Beta"),
    )

    forwards = group_scanned_files(files)
    backwards = group_scanned_files(tuple(reversed(files)))

    assert forwards == backwards
    assert tuple(group.group_id for group in forwards.groups) == (
        "group-0001",
        "group-0002",
        "group-0003",
    )
    assert tuple(group.album_title for group in forwards.groups) == (
        "Alpha",
        "Zulu",
        "Beta",
    )


def test_real_track_restart_with_real_disc_change_is_strong_split_evidence() -> None:
    files = (
        make_media_file("library/Set/01.flac", album="Set", disc_number=1, track_number=1),
        make_media_file("library/Set/02.flac", album="Set", disc_number=1, track_number=2),
        make_media_file("library/Set/03.flac", album="Set", disc_number=2, track_number=1),
        make_media_file("library/Set/04.flac", album="Set", disc_number=2, track_number=2),
    )

    result = group_scanned_files(files)

    assert tuple(group.files for group in result.groups) == (files[:2], files[2:])
    assert all(group.reason is GroupingReason.TRACK_RESTART_WITH_DISC_CHANGE for group in result.groups)
    assert result.warnings == ()


# Even matching disc and track filename prefixes remain weak evidence;
# auto-splitting requires the corresponding readable real tags.
def test_filename_hint_restart_is_warning_only_and_never_strong_split_evidence() -> None:
    files = (
        make_media_file(
            "library/Set/a.flac",
            filename_hints=FilenameHints(disc_number=1, track_number=1),
        ),
        make_media_file(
            "library/Set/b.flac",
            filename_hints=FilenameHints(disc_number=1, track_number=2),
        ),
        make_media_file(
            "library/Set/c.flac",
            filename_hints=FilenameHints(disc_number=2, track_number=1),
        ),
    )

    result = group_scanned_files(files)

    assert len(result.groups) == 1
    assert result.groups[0].files == files
    assert result.warnings[0].code is GroupingWarningCode.POSSIBLE_MULTIPLE_ALBUMS


def test_unreadable_disc_state_keeps_a_real_track_restart_as_weak_evidence() -> None:
    files = (
        make_media_file(
            "library/Set/01.flac",
            disc_number=1,
            track_number=2,
            disc_state=FieldReadState.UNREADABLE,
        ),
        make_media_file(
            "library/Set/02.flac",
            disc_number=2,
            track_number=1,
            disc_state=FieldReadState.UNREADABLE,
        ),
    )

    result = group_scanned_files(files)

    assert len(result.groups) == 1
    assert result.warnings[0].reason is (GroupingWarningReason.TRACK_NUMBER_RESTART_WITHOUT_STRONG_TAG_EVIDENCE)


def test_harmless_album_unicode_and_whitespace_variants_do_not_split() -> None:
    full_width = make_media_file("library/Album/01.flac", album="Ａｌｂｕｍ   One")
    tabbed = make_media_file("library/Album/02.flac", album="Album\tOne")

    result = group_scanned_files((full_width, tabbed))

    assert len(result.groups) == 1
    assert result.groups[0].reason is GroupingReason.DIRECTORY_ALBUM_CONSISTENT


def test_album_split_still_reports_a_weak_restart_inside_a_result_group() -> None:
    album_a_files = (
        make_media_file("library/Mixed/01.flac", album="Album A", track_number=1),
        make_media_file("library/Mixed/02.flac", album="Album A", track_number=2),
        make_media_file("library/Mixed/03.flac", album="Album A", track_number=1),
    )
    album_b = make_media_file("library/Mixed/04.flac", album="Album B", track_number=1)

    result = group_scanned_files((*album_a_files, album_b))

    assert len(result.groups) == 2
    assert len(result.warnings) == 1
    assert result.warnings[0].group_id == result.groups[0].group_id
    assert result.warnings[0].affected_file_ids == (
        album_a_files[1].file_id,
        album_a_files[2].file_id,
    )


def test_album_partition_also_applies_strong_disc_boundaries_within_each_album() -> None:
    album_a_disc_one = (
        make_media_file("library/Mixed/01.flac", album="Album A", disc_number=1, track_number=1),
        make_media_file("library/Mixed/02.flac", album="Album A", disc_number=1, track_number=2),
    )
    album_a_disc_two = make_media_file(
        "library/Mixed/03.flac",
        album="Album A",
        disc_number=2,
        track_number=1,
    )
    album_b = make_media_file(
        "library/Mixed/04.flac",
        album="Album B",
        disc_number=1,
        track_number=1,
    )

    result = group_scanned_files((*album_a_disc_one, album_a_disc_two, album_b))

    assert tuple(group.files for group in result.groups) == (
        album_a_disc_one,
        (album_a_disc_two,),
        (album_b,),
    )
    assert tuple(group.reason for group in result.groups) == (
        GroupingReason.TRACK_RESTART_WITH_DISC_CHANGE,
        GroupingReason.TRACK_RESTART_WITH_DISC_CHANGE,
        GroupingReason.DISTINCT_ALBUM_TAGS,
    )
    assert result.warnings == ()


def test_zero_disc_number_is_not_strong_split_evidence() -> None:
    files = (
        make_media_file("library/Set/01.flac", disc_number=0, track_number=2),
        make_media_file("library/Set/02.flac", disc_number=1, track_number=1),
    )

    result = group_scanned_files(files)

    assert len(result.groups) == 1
    assert result.warnings[0].reason is (GroupingWarningReason.TRACK_NUMBER_RESTART_WITHOUT_STRONG_TAG_EVIDENCE)


def test_warning_affected_files_must_belong_to_the_referenced_group() -> None:
    first = make_media_file("library/A/01.flac")
    second = make_media_file("library/B/01.flac")
    groups = (
        AlbumGroup(
            group_id="group-0001",
            files=(first,),
            album_title=None,
            reason=GroupingReason.DIRECTORY_WITHOUT_ALBUM,
        ),
        AlbumGroup(
            group_id="group-0002",
            files=(second,),
            album_title=None,
            reason=GroupingReason.DIRECTORY_WITHOUT_ALBUM,
        ),
    )
    warning = GroupingWarning(
        code=GroupingWarningCode.POSSIBLE_MULTIPLE_ALBUMS,
        reason=GroupingWarningReason.TRACK_NUMBER_RESTART_WITHOUT_STRONG_TAG_EVIDENCE,
        group_id=groups[0].group_id,
        affected_file_ids=(second.file_id,),
        message="Possible multiple albums.",
    )

    with pytest.raises(ValueError, match="affected file"):
        GroupingResult(groups=groups, warnings=(warning,))
