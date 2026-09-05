"""Pure in-memory split and merge operations for probable album groups."""

import hashlib
from collections.abc import Iterable
from dataclasses import replace

from metadata_polisher.domain.media import LocalMediaFile
from metadata_polisher.scanner.grouping import AlbumGroup, GroupingReason, derive_album_title


def _file_sort_key(file: LocalMediaFile) -> tuple[str, str, str]:
    portable_path = file.path.as_posix()

    return portable_path.casefold(), portable_path, file.file_id


def _validate_groups(groups: tuple[AlbumGroup, ...]) -> None:
    group_ids = tuple(group.group_id for group in groups)

    if len(group_ids) != len(set(group_ids)):
        raise ValueError("group IDs must be unique")

    file_ids = tuple(file.file_id for group in groups for file in group.files)

    if len(file_ids) != len(set(file_ids)):
        raise ValueError("each file ID must belong to exactly one group")


class GroupManagementService:
    """Apply manual grouping decisions without mutating scan or group objects."""

    @staticmethod
    def _new_group_id(files: tuple[LocalMediaFile, ...]) -> str:
        digest = hashlib.sha256()

        # Length-prefix each UTF-8 identifier so different identifier boundaries
        # cannot produce the same digest input. Sorting makes caller selection order
        # irrelevant while retaining deterministic session/debug output.
        for file_id in sorted(file.file_id for file in files):
            encoded_file_id = file_id.encode("utf-8")
            digest.update(len(encoded_file_id).to_bytes(8, byteorder="big"))
            digest.update(encoded_file_id)

        return f"group-manual-{digest.hexdigest()[:16]}"

    def split_group(
        self,
        groups: Iterable[AlbumGroup],
        group_id: str,
        selected_file_ids: Iterable[str],
    ) -> tuple[AlbumGroup, ...]:
        """Move a non-empty proper file selection into a new adjacent group."""
        source_groups = tuple(groups)
        _validate_groups(source_groups)
        requested_file_ids = tuple(selected_file_ids)

        if not requested_file_ids:
            raise ValueError("at least one file must be selected for a split")

        if len(requested_file_ids) != len(set(requested_file_ids)):
            raise ValueError("selected file IDs must be unique")

        source_index = next(
            (index for index, group in enumerate(source_groups) if group.group_id == group_id),
            None,
        )

        if source_index is None:
            raise ValueError(f"unknown group ID: {group_id}")

        source_group = source_groups[source_index]
        source_file_ids = {file.file_id for file in source_group.files}
        requested_set = set(requested_file_ids)

        if not requested_set <= source_file_ids:
            raise ValueError("every selected file must belong to the source group")

        if requested_set == source_file_ids:
            raise ValueError("a split must leave at least one file in the source group")

        # Iterate the immutable source tuple so both outputs retain the visible
        # file order, regardless of the caller's selection order.
        remaining_files = tuple(file for file in source_group.files if file.file_id not in requested_set)
        moved_files = tuple(file for file in source_group.files if file.file_id in requested_set)
        # Membership changes can remove the old album-title consensus, so derive
        # each new group's title from its own remaining evidence.
        remaining_group = replace(
            source_group,
            files=remaining_files,
            album_title=derive_album_title(remaining_files),
            reason=GroupingReason.MANUAL_SPLIT,
        )
        moved_group = AlbumGroup(
            group_id=self._new_group_id(moved_files),
            files=moved_files,
            album_title=derive_album_title(moved_files),
            reason=GroupingReason.MANUAL_SPLIT,
        )
        transformed = source_groups[:source_index] + (remaining_group, moved_group) + source_groups[source_index + 1 :]
        _validate_groups(transformed)

        return transformed

    def merge_groups(
        self,
        groups: Iterable[AlbumGroup],
        selected_group_ids: Iterable[str],
    ) -> tuple[AlbumGroup, ...]:
        """Merge two or more groups at the earliest selected group position."""
        source_groups = tuple(groups)
        _validate_groups(source_groups)
        requested_group_ids = tuple(selected_group_ids)

        if len(requested_group_ids) < 2:
            raise ValueError("at least two groups must be selected for a merge")

        if len(requested_group_ids) != len(set(requested_group_ids)):
            raise ValueError("selected group IDs must be unique")

        requested_set = set(requested_group_ids)
        known_group_ids = {group.group_id for group in source_groups}

        if not requested_set <= known_group_ids:
            raise ValueError("every selected group ID must exist")

        # Selection order comes from current session state, not from a UI
        # collection whose iteration order may vary.
        selected_groups = tuple(group for group in source_groups if group.group_id in requested_set)
        # Reuse the earliest group's identity and position to keep navigation
        # stable; the session layer separately invalidates its old lookup state.
        retained_group = selected_groups[0]
        merged_files = tuple(
            sorted(
                (file for group in selected_groups for file in group.files),
                key=_file_sort_key,
            )
        )
        merged_group = AlbumGroup(
            group_id=retained_group.group_id,
            files=merged_files,
            album_title=derive_album_title(merged_files),
            reason=GroupingReason.MANUAL_MERGE,
        )
        transformed_list: list[AlbumGroup] = []

        for group in source_groups:
            if group.group_id == retained_group.group_id:
                transformed_list.append(merged_group)
            elif group.group_id not in requested_set:
                transformed_list.append(group)

        transformed = tuple(transformed_list)
        _validate_groups(transformed)

        return transformed
