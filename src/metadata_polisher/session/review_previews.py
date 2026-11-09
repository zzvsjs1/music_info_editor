"""Prepare local review snapshots and validate the complete rename destination set."""

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from metadata_polisher.application.changes import (
    ChangeValidationFacts,
    FileChangeSet,
    RenameDecision,
    build_change_set,
    with_destination_collision,
)
from metadata_polisher.application.review import build_field_review_state
from metadata_polisher.domain.media import LocalMediaFile
from metadata_polisher.domain.metadata import MetadataField
from metadata_polisher.domain.review import FieldReviewState
from metadata_polisher.infrastructure.settings import RenameSettings
from metadata_polisher.rename.template import FilenameRenderPolicy
from metadata_polisher.rename.windows import windows_collision_key
from metadata_polisher.session.state import GroupState, ReviewedFileState, SessionState


@dataclass(frozen=True)
class ReviewFileSource:
    """Locate one stable file identity without relying on positional unpacking."""

    group: GroupState
    source: LocalMediaFile


def review_sources_by_id(state: SessionState) -> dict[str, ReviewFileSource]:
    sources: dict[str, ReviewFileSource] = {}

    for group in state.groups:
        for source in group.group.files:
            sources[source.file_id] = ReviewFileSource(group=group, source=source)

    return sources


def reviewed_files_by_id(state: SessionState) -> dict[str, ReviewedFileState]:
    """Index only published reviews, preserving the distinction from local drafts."""
    reviews: dict[str, ReviewedFileState] = {}

    for group in state.groups:
        for reviewed in group.reviewed_files:
            reviews[reviewed.file_id] = reviewed

    return reviews


def complete_reviewed_files_by_id(
    state: SessionState, file_ids: frozenset[str] | None = None,
) -> dict[str, ReviewedFileState]:
    """Include local-only files when a human command needs a complete review scope."""
    reviews: dict[str, ReviewedFileState] = {}

    for group in state.groups:
        for reviewed in complete_reviewed_files(group, file_ids):
            reviews[reviewed.file_id] = reviewed

    return reviews


def local_reviews(source: LocalMediaFile) -> ReviewedFileState:
    reviews: list[FieldReviewState] = []

    for field in MetadataField:
        reviews.append(build_field_review_state(
            field=field,
            read_state=source.read_result.field_states[field],
                existing_value=source.read_result.existing_value(field),
            proposals=(),
        ))

    return ReviewedFileState(source.file_id, reviews=tuple(reviews))


def complete_reviewed_files(
    group: GroupState, file_ids: frozenset[str] | None = None,
) -> tuple[ReviewedFileState, ...]:
    existing = {reviewed.file_id: reviewed for reviewed in group.reviewed_files}
    completed: list[ReviewedFileState] = []

    for source in group.group.files:
        if file_ids is not None and source.file_id not in file_ids:
            continue

        reviewed = existing.get(source.file_id)

        if reviewed is None or not reviewed.reviews:
            reviewed = local_reviews(source)

        completed.append(reviewed)

    return tuple(completed)


def rename_intent(reviewed: ReviewedFileState) -> RenameDecision:
    if reviewed.change_set is None:
        return RenameDecision.KEEP_FILENAME

    return reviewed.change_set.rename_decision


def scoped_change_sets(state: SessionState, file_ids: frozenset[str] | None = None) -> tuple[FileChangeSet, ...]:
    """Reserve all known physical names and only the requested scope's queued destinations."""
    changes = tuple(file.change_set for group in state.groups for file in group.reviewed_files
                    if file.change_set is not None and (file_ids is None or file.file_id in file_ids))
    return _checked_destinations(state, changes, changes)


def _checked_destinations(
    state: SessionState, changes: tuple[FileChangeSet, ...], reservations: Sequence[FileChangeSet],
) -> tuple[FileChangeSet, ...]:
    if not any(change.rename_preview is not None for change in changes):
        return changes

    def name_key(path: Path) -> tuple[Path, str]:
        return path.parent, windows_collision_key(path.name)

    occupied = Counter(name_key(file.path) for group in state.groups for file in group.group.files)
    occupied.update(name_key(file.path) for file in state.unsupported_files)
    occupied.update(name_key(change.rename_preview.new_path) for change in reservations
                    if change.rename_decision is RenameDecision.APPLY_RENAME and change.rename_preview is not None)

    checked = []
    for change in changes:
        preview = change.rename_preview
        if preview is not None:
            target = name_key(preview.new_path)
            # A case-only preview may occupy its own source slot. Its own queued
            # destination never counts against it, but every other owner counts.
            own_entries = int(name_key(preview.old_path) == target) + int(
                change.rename_decision is RenameDecision.APPLY_RENAME
            )
            change = with_destination_collision(change, occupied[target] > own_entries)
        checked.append(change)

    return tuple(checked)


def revalidate_review_destinations(state: SessionState) -> SessionState:
    """Refresh cross-group reservations while preserving unrelated snapshots and decisions."""
    checked = {change.file_id: change for change in scoped_change_sets(state)}
    groups = []
    for group in state.groups:
        if not any(item.change_set is not checked.get(item.file_id) for item in group.reviewed_files):
            groups.append(group)
            continue

        reviews = tuple(replace(item, change_set=checked[item.file_id]) if item.change_set is not None else item
                        for item in group.reviewed_files)
        groups.append(replace(group, reviewed_files=reviews, selected_metadata=None))

    if all(group is original for group, original in zip(groups, state.groups, strict=True)):
        return state
    return replace(state, groups=tuple(groups))


def rebuild_reviewed_files(
    state: SessionState,
    group: GroupState,
    reviewed_files: Sequence[ReviewedFileState],
    rename_settings: RenameSettings,
    *,
    rename_decisions: Mapping[str, RenameDecision] | None = None,
) -> tuple[ReviewedFileState, ...]:
    """Derive all previews, then validate original and planned sibling names.

    A title edit can create or resolve a collision with another requested rename.
    The two passes use the complete destination set, rather than carrying old
    blockers into a new ChangeSet. Apply preflight checks live filesystem facts;
    this pure operation uses the directory entries already known to the session.
    """
    sources = {source.file_id: source for source in group.group.files}
    policy = FilenameRenderPolicy(
        minimum_track_digits=rename_settings.minimum_track_digits,
        minimum_disc_digits=rename_settings.minimum_disc_digits,
    )
    decisions = rename_decisions or {}
    preliminary: list[ReviewedFileState] = []

    # First render each file without reserving sibling destinations. Every file
    # in the second pass must see the same complete set of proposed names.
    for reviewed in reviewed_files:
        decision = decisions.get(reviewed.file_id, rename_intent(reviewed))

        if not rename_settings.enabled:
            decision = RenameDecision.KEEP_FILENAME

        source = sources[reviewed.file_id]
        derived_changes = build_change_set(
            source,
            reviewed.reviews,
            decision,
            template=rename_settings.template,
            rename_policy=policy,
            validation=ChangeValidationFacts(track_mapping_resolved=reviewed.track_mapping_resolved),
        )
        preliminary.append(replace(reviewed, change_set=derived_changes))

    changes = tuple(item.change_set for item in preliminary if item.change_set is not None)
    others = (item.change_set for other in state.groups if other.group.group_id != group.group.group_id
              for item in other.reviewed_files if item.change_set is not None)
    checked = _checked_destinations(state, changes, (*changes, *others))
    return tuple(replace(item, change_set=change) for item, change in zip(preliminary, checked, strict=True))


def rebuild_changed_review_groups(
    state: SessionState,
    changed_group_ids: set[str],
    rename_settings: RenameSettings,
    *,
    rename_decisions: Mapping[str, RenameDecision],
) -> SessionState:
    """Revalidate already-staged decisions against every group's destinations."""
    groups = []
    for group in state.groups:
        if group.group.group_id in changed_group_ids:
            rebuilt = rebuild_reviewed_files(
                state, group, group.reviewed_files, rename_settings, rename_decisions=rename_decisions,
            )
            group = replace(group, reviewed_files=rebuilt)
        groups.append(group)

    return revalidate_review_destinations(replace(state, groups=tuple(groups)))
