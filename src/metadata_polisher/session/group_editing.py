"""Pure session transforms for user-confirmed group membership and disc hints."""

from dataclasses import replace

from metadata_polisher.application.grouping import GroupManagementService
from metadata_polisher.domain.review import DecisionOrigin, FieldDecisionKind
from metadata_polisher.infrastructure.settings import RenameSettings
from metadata_polisher.scanner.grouping import AlbumGroup
from metadata_polisher.session.review_editing import (
    local_reviews_after_lookup_reset,
    rebuild_reviewed_files,
    regrouped_review_undo,
)
from metadata_polisher.session.state import GroupSelection, GroupState, SessionState

_DEFAULT_RENAME_SETTINGS = RenameSettings()


def regroup_candidate_decisions(state: SessionState, group_ids: tuple[str, ...]) -> int:
    """Count explicit approvals that cannot survive changed track membership."""
    return sum(
        review.decision_origin is DecisionOrigin.USER and review.decision is FieldDecisionKind.USE_PROPOSAL
        for group in state.groups if group.group.group_id in group_ids
        for reviewed in group.reviewed_files for review in reviewed.reviews
    )


def _require_idle(state: SessionState) -> None:
    if state.active_operation is not None:
        raise ValueError("Wait for the current operation before editing groups.")


def _install_groups(
    state: SessionState, groups: tuple[AlbumGroup, ...], selected: str, rename_settings: RenameSettings,
) -> SessionState:
    previous = {item.group.group_id: item for item in state.groups}
    updated: list[GroupState] = []
    affected_file_ids: set[str] = set()
    local_reviews = {}

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

        # Track alignment belongs to the old membership. Independent manual,
        # Clear, Keep and filename choices belong to stable files and survive.
        # Cache each contributor's projection because a split visits it twice.
        for contributor in contributors:
            if contributor.group.group_id not in local_reviews:
                local_reviews[contributor.group.group_id] = local_reviews_after_lookup_reset(
                    state, contributor, rename_settings,
                )

        retained = tuple(review for contributor in contributors
                         for review in local_reviews[contributor.group.group_id] if review.file_id in file_ids)
        affected_file_ids.update(file_ids)
        updated.append(
            GroupState(
                group=group,
                language_override=next(iter(languages)) if len(languages) == 1 else None,
                disc_number_override=next(iter(discs)) if len(discs) == 1 else None,
                requires_rescan=any(item.requires_rescan for item in contributors),
                reviewed_files=retained,
                revision=old.revision + 1 if old is not None else 1,
            )
        )

    staged = replace(
        state,
        groups=tuple(updated),
        selection=GroupSelection(selected),
        revision=state.revision + 1,
        library_revision=state.library_revision + 1,
    )

    # Resolve rename collisions against the complete new membership. A second
    # pass observes every refreshed destination, including cross-group siblings.
    for _pass in range(2):
        staged = replace(staged, groups=tuple(
            replace(group, reviewed_files=rebuild_reviewed_files(
                staged, group, group.reviewed_files, rename_settings,
            )) if any(source.file_id in affected_file_ids for source in group.group.files) else group
            for group in staged.groups
        ))

    return replace(staged, review_undo=regrouped_review_undo(staged, frozenset(affected_file_ids), rename_settings))


def split_session_group(
    state: SessionState, group_id: str, file_ids: tuple[str, ...],
    rename_settings: RenameSettings = _DEFAULT_RENAME_SETTINGS,
) -> SessionState:
    """Split a proper selection and invalidate only affected group evidence."""
    _require_idle(state)
    groups = GroupManagementService().split_group((item.group for item in state.groups), group_id, file_ids)
    return _install_groups(state, groups, group_id, rename_settings)


def merge_session_groups(
    state: SessionState, group_ids: tuple[str, ...], rename_settings: RenameSettings = _DEFAULT_RENAME_SETTINGS,
) -> SessionState:
    """Merge selected groups while keeping unrelated review state intact."""
    _require_idle(state)

    # An uncertain filesystem outcome makes the complete merged group stale.
    # Refuse that transition so healthy files do not lose their retained review
    # to the invariant that stale groups cannot carry writable derived state.
    if any(group.requires_rescan for group in state.groups if group.group.group_id in group_ids):
        raise ValueError("Rescan the affected groups before merging them. Existing review choices are retained.")

    groups = GroupManagementService().merge_groups((item.group for item in state.groups), group_ids)
    retained = next(group.group_id for group in groups if group.group_id in group_ids)
    return _install_groups(state, groups, retained, rename_settings)


def set_disc_override(
    state: SessionState, group_id: str, number: int | None,
    rename_settings: RenameSettings = _DEFAULT_RENAME_SETTINGS,
) -> SessionState:
    """Change lookup evidence, leaving original media tags untouched."""
    _require_idle(state)
    current = next((item for item in state.groups if item.group.group_id == group_id), None)

    if current is None:
        raise ValueError("The selected group no longer exists.")

    if current.disc_number_override == number:
        return state

    # A new disc hint invalidates the release and track evidence. Independent
    # local decisions and filename intent still belong to the same stable files.
    replacement = GroupState(
        group=current.group,
        warnings=current.warnings,
        language_override=current.language_override,
        disc_number_override=number,
        search_query_override=current.search_query_override,
        requires_rescan=current.requires_rescan,
        reviewed_files=local_reviews_after_lookup_reset(state, current, rename_settings),
        revision=current.revision + 1,
    )
    staged = replace(
        state,
        groups=tuple(replacement if item is current else item for item in state.groups),
        revision=state.revision + 1,
    )

    # Project Undo through the same local-only boundary so it can reverse manual
    # decisions without bringing an obsolete provider proposal or mapping back.
    affected_file_ids = frozenset(source.file_id for source in current.group.files)

    return replace(staged, review_undo=regrouped_review_undo(staged, affected_file_ids, rename_settings))
