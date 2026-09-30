"""Prepare local review snapshots and validate the complete rename destination set."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from metadata_polisher.application.changes import ChangeValidationFacts, RenameDecision, build_change_set
from metadata_polisher.application.review import build_field_review_state
from metadata_polisher.domain.media import LocalMediaFile
from metadata_polisher.domain.metadata import FieldReadState, MetadataField, Position, metadata_value
from metadata_polisher.domain.review import FieldReviewState
from metadata_polisher.infrastructure.settings import RenameSettings
from metadata_polisher.rename.template import FilenameRenderPolicy
from metadata_polisher.session.state import GroupState, ReviewedFileState, SessionState


@dataclass(frozen=True)
class ReviewFileSource:
    """Locate one stable file identity without relying on positional unpacking."""

    group: GroupState
    source: LocalMediaFile


@dataclass(frozen=True)
class PlannedRenameDestination:
    """Keep the destination together with the identity that reserves its name."""

    file_id: str
    path: Path


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


def complete_reviewed_files_by_id(state: SessionState) -> dict[str, ReviewedFileState]:
    """Include local-only files when a human command needs a complete review scope."""
    reviews: dict[str, ReviewedFileState] = {}

    for group in state.groups:
        for reviewed in complete_reviewed_files(group):
            reviews[reviewed.file_id] = reviewed

    return reviews


def local_reviews(source: LocalMediaFile) -> ReviewedFileState:
    reviews: list[FieldReviewState] = []

    for field in MetadataField:
        read_state = source.read_result.field_states[field]
        value = metadata_value(source.read_result.metadata, field)

        # A damaged field may still carry readable evidence. Keep its read state
        # separate, and do not fabricate an existing value from an empty tuple,
        # position or string merely because the adapter supplied a container.
        if (
            read_state is FieldReadState.MISSING
            or value in (None, (), Position())
            or isinstance(value, str) and not value.strip()
        ):
            value = None

        reviews.append(build_field_review_state(
            field=field,
            read_state=read_state,
            existing_value=value,
            proposals=(),
        ))

    return ReviewedFileState(source.file_id, reviews=tuple(reviews))


def complete_reviewed_files(group: GroupState) -> tuple[ReviewedFileState, ...]:
    existing = {reviewed.file_id: reviewed for reviewed in group.reviewed_files}
    completed: list[ReviewedFileState] = []

    for source in group.group.files:
        reviewed = existing.get(source.file_id)

        if reviewed is None or not reviewed.reviews:
            reviewed = local_reviews(source)

        completed.append(reviewed)

    return tuple(completed)


def rename_intent(reviewed: ReviewedFileState) -> RenameDecision:
    if reviewed.change_set is None:
        return RenameDecision.KEEP_FILENAME

    return reviewed.change_set.rename_decision


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
    known_paths: list[Path] = []

    for current_group in state.groups:
        for source in current_group.group.files:
            known_paths.append(source.path)

    for unsupported in state.unsupported_files:
        known_paths.append(unsupported.path)

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

    available_reviews = list(preliminary)

    for other_group in state.groups:
        if other_group.group.group_id != group.group.group_id:
            available_reviews.extend(other_group.reviewed_files)

    planned_destinations: list[PlannedRenameDestination] = []

    for reviewed in available_reviews:
        planned_changes = reviewed.change_set

        if planned_changes is None or planned_changes.rename_decision is not RenameDecision.APPLY_RENAME:
            continue

        if planned_changes.rename_preview is not None:
            planned_destinations.append(PlannedRenameDestination(
                file_id=reviewed.file_id,
                path=planned_changes.rename_preview.new_path,
            ))

    rebuilt: list[ReviewedFileState] = []

    for reviewed in preliminary:
        source = sources[reviewed.file_id]
        preliminary_changes = reviewed.change_set
        assert preliminary_changes is not None
        existing_names: list[str] = []

        for path in known_paths:
            if path.parent == source.path.parent:
                existing_names.append(path.name)

        for destination in planned_destinations:
            # A file must not reserve its own preview against itself, which
            # would incorrectly report a collision for every requested rename.
            if destination.file_id == reviewed.file_id:
                continue

            if destination.path.parent == source.path.parent:
                existing_names.append(destination.path.name)

        validated = build_change_set(
            source,
            reviewed.reviews,
            preliminary_changes.rename_decision,
            template=rename_settings.template,
            rename_policy=policy,
            validation=ChangeValidationFacts(
                track_mapping_resolved=reviewed.track_mapping_resolved,
                existing_names=tuple(existing_names),
            ),
        )
        rebuilt.append(replace(reviewed, change_set=validated))

    return tuple(rebuilt)


def rebuild_changed_review_groups(
    state: SessionState,
    changed_group_ids: set[str],
    rename_settings: RenameSettings,
    *,
    rename_decisions: Mapping[str, RenameDecision],
) -> SessionState:
    """Revalidate already-staged decisions against every group's destinations."""
    staged = state

    # Rebuild every group against one immutable snapshot per pass. Publishing
    # groups incrementally within a pass would make collision facts depend on
    # their iteration order; the second pass sees all first-pass destinations.
    for _pass in range(2):
        groups: list[GroupState] = []

        for group in staged.groups:
            if group.group.group_id not in changed_group_ids:
                groups.append(group)
                continue

            rebuilt = rebuild_reviewed_files(
                staged, group, group.reviewed_files, rename_settings,
                rename_decisions=rename_decisions,
            )
            groups.append(replace(group, reviewed_files=rebuilt))

        staged = replace(staged, groups=tuple(groups))

    return staged
