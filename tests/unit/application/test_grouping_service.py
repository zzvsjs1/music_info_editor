# Grouping transforms work on immutable file membership. Reordered selections
# should produce the same groups without moving or rewriting the source files.

from pathlib import Path

import pytest

from metadata_polisher.application.grouping import GroupManagementService
from metadata_polisher.domain.media import LocalMediaFile, MediaReadResult, StreamInfo
from metadata_polisher.domain.metadata import FieldReadState, MetadataField, MetadataSnapshot
from metadata_polisher.scanner.grouping import AlbumGroup, GroupingReason


def make_media_file(path: str, album: str) -> LocalMediaFile:
    states = {field: FieldReadState.MISSING for field in MetadataField}
    states[MetadataField.ALBUM] = FieldReadState.PRESENT

    return LocalMediaFile(
        path=Path(path),
        format_id="flac",
        read_result=MediaReadResult(
            metadata=MetadataSnapshot(album=album),
            field_states=states,
            stream_info=StreamInfo(
                duration_seconds=180.0,
                sample_rate=48_000,
                channels=2,
                bit_depth=24,
                codec="FLAC",
            ),
        ),
    )


def assert_unique_membership(groups: tuple[AlbumGroup, ...]) -> None:
    file_ids = [file.file_id for group in groups for file in group.files]

    assert len(file_ids) == len(set(file_ids))


def test_split_group_is_immutable_and_keeps_every_file_in_exactly_one_group() -> None:
    first = make_media_file("library/Album/01.flac", "Album")
    second = make_media_file("library/Album/02.flac", "Album")
    third = make_media_file("library/Album/03.flac", "Album")
    other = make_media_file("library/Other/01.flac", "Other")
    source_group = AlbumGroup(
        group_id="group-0001",
        files=(first, second, third),
        album_title="Album",
        reason=GroupingReason.DIRECTORY_ALBUM_CONSISTENT,
    )
    other_group = AlbumGroup(
        group_id="group-0002",
        files=(other,),
        album_title="Other",
        reason=GroupingReason.DIRECTORY_ALBUM_CONSISTENT,
    )
    original_groups = (source_group, other_group)
    original_source_files = source_group.files

    transformed = GroupManagementService().split_group(
        original_groups,
        source_group.group_id,
        (second.file_id,),
    )

    assert original_groups == (source_group, other_group)
    assert source_group.files == original_source_files
    assert transformed is not original_groups
    new_group_id = transformed[1].group_id

    assert tuple(group.group_id for group in transformed) == (
        "group-0001",
        new_group_id,
        "group-0002",
    )
    assert new_group_id.startswith("group-manual-")
    assert transformed[0].files == (first, third)
    assert transformed[1].files == (second,)
    assert transformed[0].reason is GroupingReason.MANUAL_SPLIT
    assert transformed[1].reason is GroupingReason.MANUAL_SPLIT
    assert_unique_membership(transformed)


def test_merge_groups_is_immutable_deterministic_and_supports_different_folders() -> None:
    later = make_media_file("library/Z/02.flac", "Later")
    first = make_media_file("library/A/01.flac", "First")
    middle = make_media_file("library/M/01.flac", "Middle")
    first_group = AlbumGroup(
        group_id="group-0001",
        files=(later,),
        album_title="Later",
        reason=GroupingReason.DIRECTORY_ALBUM_CONSISTENT,
    )
    middle_group = AlbumGroup(
        group_id="group-0002",
        files=(middle,),
        album_title="Middle",
        reason=GroupingReason.DIRECTORY_ALBUM_CONSISTENT,
    )
    last_group = AlbumGroup(
        group_id="group-0003",
        files=(first,),
        album_title="First",
        reason=GroupingReason.DIRECTORY_ALBUM_CONSISTENT,
    )
    original_groups = (first_group, middle_group, last_group)

    transformed = GroupManagementService().merge_groups(
        original_groups,
        (last_group.group_id, first_group.group_id),
    )

    assert original_groups == (first_group, middle_group, last_group)
    assert tuple(group.group_id for group in transformed) == ("group-0001", "group-0002")
    assert transformed[0].files == (first, later)
    assert transformed[0].album_title is None
    assert transformed[0].reason is GroupingReason.MANUAL_MERGE
    assert transformed[1] is middle_group
    assert_unique_membership(transformed)


@pytest.mark.parametrize("selected_file_ids", [(), ("missing-file",)])
def test_split_rejects_empty_or_unknown_selections(selected_file_ids: tuple[str, ...]) -> None:
    first = make_media_file("library/Album/01.flac", "Album")
    second = make_media_file("library/Album/02.flac", "Album")
    group = AlbumGroup(
        group_id="group-0001",
        files=(first, second),
        album_title="Album",
        reason=GroupingReason.DIRECTORY_ALBUM_CONSISTENT,
    )

    with pytest.raises(ValueError):
        GroupManagementService().split_group((group,), group.group_id, selected_file_ids)


def test_merge_rejects_duplicate_group_selection_without_changing_inputs() -> None:
    first = make_media_file("library/A/01.flac", "A")
    second = make_media_file("library/B/01.flac", "B")
    groups = (
        AlbumGroup(
            group_id="group-0001",
            files=(first,),
            album_title="A",
            reason=GroupingReason.DIRECTORY_ALBUM_CONSISTENT,
        ),
        AlbumGroup(
            group_id="group-0002",
            files=(second,),
            album_title="B",
            reason=GroupingReason.DIRECTORY_ALBUM_CONSISTENT,
        ),
    )

    with pytest.raises(ValueError):
        GroupManagementService().merge_groups(groups, ("group-0001", "group-0001"))

    assert groups[0].files == (first,)
    assert groups[1].files == (second,)


def test_split_recomputes_each_album_title_from_its_actual_present_tags() -> None:
    first = make_media_file("library/Mixed/01.flac", "Actual A")
    second = make_media_file("library/Mixed/02.flac", "Actual B")
    source = AlbumGroup(
        group_id="group-0001",
        files=(first, second),
        album_title="Stale summary",
        reason=GroupingReason.DIRECTORY_ALBUM_CONSISTENT,
    )

    transformed = GroupManagementService().split_group(
        (source,),
        source.group_id,
        (second.file_id,),
    )

    assert transformed[0].album_title == "Actual A"
    assert transformed[1].album_title == "Actual B"


def test_merge_recomputes_album_title_from_files_instead_of_group_summaries() -> None:
    first = make_media_file("library/A/01.flac", "Actual Album")
    second = make_media_file("library/B/01.flac", "Actual Album")
    groups = (
        AlbumGroup(
            group_id="group-0001",
            files=(first,),
            album_title="Stale A",
            reason=GroupingReason.DIRECTORY_ALBUM_CONSISTENT,
        ),
        AlbumGroup(
            group_id="group-0002",
            files=(second,),
            album_title="Stale B",
            reason=GroupingReason.DIRECTORY_ALBUM_CONSISTENT,
        ),
    )

    transformed = GroupManagementService().merge_groups(
        groups,
        ("group-0001", "group-0002"),
    )

    assert transformed[0].album_title == "Actual Album"


def test_split_id_is_membership_stable_and_does_not_reuse_a_retired_group_id() -> None:
    first = make_media_file("library/A/01.flac", "A")
    second = make_media_file("library/A/02.flac", "A")
    third = make_media_file("library/B/01.flac", "B")
    fourth = make_media_file("library/C/01.flac", "C")
    groups = (
        AlbumGroup(
            group_id="group-0001",
            files=(first, second),
            album_title="A",
            reason=GroupingReason.DIRECTORY_ALBUM_CONSISTENT,
        ),
        AlbumGroup(
            group_id="group-0002",
            files=(third,),
            album_title="B",
            reason=GroupingReason.DIRECTORY_ALBUM_CONSISTENT,
        ),
        AlbumGroup(
            group_id="group-0003",
            files=(fourth,),
            album_title="C",
            reason=GroupingReason.DIRECTORY_ALBUM_CONSISTENT,
        ),
    )
    service = GroupManagementService()

    after_merge = service.merge_groups(groups, ("group-0002", "group-0003"))
    after_split = service.split_group(after_merge, "group-0001", (second.file_id,))
    repeated = service.split_group(after_merge, "group-0001", (second.file_id,))
    from_fresh_service = GroupManagementService().split_group(
        after_merge,
        "group-0001",
        (second.file_id,),
    )

    assert after_split == repeated == from_fresh_service
    assert after_split[1].group_id.startswith("group-manual-")
    assert after_split[1].group_id != "group-0003"
