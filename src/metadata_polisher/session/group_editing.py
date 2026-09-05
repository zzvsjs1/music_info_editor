"""Pure session transforms for user-confirmed group membership and disc hints."""

from dataclasses import replace

from metadata_polisher.application.grouping import GroupManagementService
from metadata_polisher.scanner.grouping import AlbumGroup
from metadata_polisher.session.state import GroupSelection, GroupState, SessionState


def _require_idle(state: SessionState) -> None:
    if state.active_operation is not None:
        raise ValueError("Wait for the current operation before editing groups.")


def _install_groups(state: SessionState, groups: tuple[AlbumGroup, ...], selected: str) -> SessionState:
    previous = {item.group.group_id: item for item in state.groups}
    updated: list[GroupState] = []

    for group in groups:
        old = previous.get(group.group_id)

        # Reuse untouched groups exactly, including their reviews and revisions;
        # a split elsewhere should not discard unrelated work.
        if old is not None and old.group == group:
            updated.append(old)
            continue

        file_ids = {file.file_id for file in group.files}
        contributors = tuple(
            item for item in state.groups if any(file.file_id in file_ids for file in item.group.files)
        )
        languages = {item.language_override for item in contributors}
        discs = {item.disc_number_override for item in contributors}

        # Membership changes invalidate track alignment and every derived review.
        # Preserve a common user hint, but never carry contradictory disc hints
        # across a merge or discard a rescan requirement caused by a prior Apply.
        updated.append(
            GroupState(
                group=group,
                language_override=next(iter(languages)) if len(languages) == 1 else None,
                disc_number_override=next(iter(discs)) if len(discs) == 1 else None,
                requires_rescan=any(item.requires_rescan for item in contributors),
                revision=old.revision + 1 if old is not None else 1,
            )
        )

    return replace(
        state,
        groups=tuple(updated),
        selection=GroupSelection(selected),
        revision=state.revision + 1,
        library_revision=state.library_revision + 1,
    )


def split_session_group(state: SessionState, group_id: str, file_ids: tuple[str, ...]) -> SessionState:
    """Split a proper selection and invalidate only affected group evidence."""
    _require_idle(state)
    groups = GroupManagementService().split_group((item.group for item in state.groups), group_id, file_ids)
    return _install_groups(state, groups, group_id)


def merge_session_groups(state: SessionState, group_ids: tuple[str, ...]) -> SessionState:
    """Merge selected groups while keeping unrelated review state intact."""
    _require_idle(state)
    groups = GroupManagementService().merge_groups((item.group for item in state.groups), group_ids)
    retained = next(group.group_id for group in groups if group.group_id in group_ids)
    return _install_groups(state, groups, retained)


def set_disc_override(state: SessionState, group_id: str, number: int | None) -> SessionState:
    """Change lookup evidence, leaving original media tags untouched."""
    _require_idle(state)
    current = next((item for item in state.groups if item.group.group_id == group_id), None)

    if current is None:
        raise ValueError("The selected group no longer exists.")

    if current.disc_number_override == number:
        return state

    # A new disc hint changes matching evidence. Construct fresh downstream
    # state rather than carrying a ranking calculated for the previous hint.
    replacement = GroupState(
        group=current.group,
        warnings=current.warnings,
        language_override=current.language_override,
        disc_number_override=number,
        search_query_override=current.search_query_override,
        requires_rescan=current.requires_rescan,
        revision=current.revision + 1,
    )
    return replace(
        state,
        groups=tuple(replacement if item is current else item for item in state.groups),
        revision=state.revision + 1,
    )
