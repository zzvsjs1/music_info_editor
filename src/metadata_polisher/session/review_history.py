"""Capture and restore bounded in-memory review actions with stale-source checks."""

from dataclasses import dataclass, replace

from metadata_polisher.application.changes import RenameDecision
from metadata_polisher.infrastructure.settings import RenameSettings
from metadata_polisher.session.review_previews import (
    complete_reviewed_files_by_id,
    rebuild_changed_review_groups,
    rename_intent,
    review_sources_by_id,
    reviewed_files_by_id,
)
from metadata_polisher.session.state import (
    GroupState,
    ReviewedFileState,
    ReviewUndoEntry,
    ReviewUndoFile,
    SessionState,
)

REVIEW_UNDO_LIMIT = 30


@dataclass(frozen=True)
class NextReviewUndoAction:
    """Describe the usable files and history remaining after the chosen action."""

    history: tuple[ReviewUndoEntry, ...] = ()
    targets: tuple[ReviewUndoFile, ...] = ()


def record_review_action(before: SessionState, after: SessionState) -> SessionState:
    """Capture one complete human action, including all members of a batch."""
    previous = complete_reviewed_files_by_id(before)
    sources = review_sources_by_id(before)
    changed: list[ReviewUndoFile] = []

    for group in after.groups:
        for reviewed in group.reviewed_files:
            old = previous.get(reviewed.file_id)

            if old is None:
                continue

            if reviewed.reviews == old.reviews and rename_intent(reviewed) is rename_intent(old):
                continue

            changed.append(ReviewUndoFile(sources[reviewed.file_id].source, old, reviewed))

    if not changed:
        return before

    # A multi-file action still occupies one history entry. Retain only the
    # latest actions to bound memory, independently of how many files they touch.
    entry = ReviewUndoEntry(tuple(changed))
    history = (*before.review_undo, entry)[-REVIEW_UNDO_LIMIT:]

    return replace(after, review_undo=history)


def next_review_undo_action(state: SessionState) -> NextReviewUndoAction:
    """Skip stale actions and select the newest action with usable file members."""
    sources = review_sources_by_id(state)
    current = reviewed_files_by_id(state)
    history = list(state.review_undo)

    # Availability and restoration share these exact checks. A wholly stale
    # action is skipped, while unchanged members of a partial batch remain usable.
    while history:
        entry = history.pop()
        usable: list[ReviewUndoFile] = []

        for item in entry.files:
            file_id = item.source.file_id
            located = sources.get(file_id)
            live = current.get(file_id)

            if located is None or live is None:
                continue

            if located.group.requires_rescan or located.source != item.source:
                continue

            before_fields = {review.field: review for review in item.before.reviews}
            after_fields = {review.field: review for review in item.after.reviews}
            live_fields = {review.field: review for review in live.reviews}
            changed_fields = {
                field for field in before_fields if before_fields[field] != after_fields[field]
            }
            rename_changed = rename_intent(item.before) is not rename_intent(item.after)

            # The touched fields must still hold this action's output. Restoring
            # them after another decision would overwrite more recent user intent.
            if any(live_fields.get(field) != after_fields[field] for field in changed_fields):
                continue

            if rename_changed and rename_intent(live) is not rename_intent(item.after):
                continue

            usable.append(item)

        if usable:
            return NextReviewUndoAction(history=tuple(history), targets=tuple(usable))

    return NextReviewUndoAction()


def review_undo_targets(state: SessionState) -> tuple[ReviewUndoFile, ...]:
    """Describe precisely what Undo can restore, independently of UI selection."""
    if state.active_operation is not None:
        return ()

    return next_review_undo_action(state).targets


def undo_last_review_action(state: SessionState, rename_settings: RenameSettings) -> SessionState:
    """Undo one valid review action without restoring stale or already-written files."""
    if state.active_operation is not None:
        raise ValueError("Wait for the current operation before undoing review decisions.")

    if not state.review_undo:
        return state

    action = next_review_undo_action(state)

    if not action.targets:
        return replace(state, review_undo=action.history)

    sources = review_sources_by_id(state)
    current = reviewed_files_by_id(state)
    revised: dict[str, ReviewedFileState] = {}
    renames: dict[str, RenameDecision] = {}

    for item in action.targets:
        file_id = item.source.file_id
        live = current[file_id]
        before_fields = {review.field: review for review in item.before.reviews}
        after_fields = {review.field: review for review in item.after.reviews}
        changed_fields = {
            field for field in before_fields if before_fields[field] != after_fields[field]
        }
        rename_changed = rename_intent(item.before) is not rename_intent(item.after)

        # Restore only this action's fields. Later unrelated field choices and
        # the current filename validation must survive the Undo operation.
        restored_reviews = []

        for review in live.reviews:
            restored = before_fields[review.field] if review.field in changed_fields else review
            restored_reviews.append(restored)

        revised[file_id] = replace(live, change_set=None, reviews=tuple(restored_reviews))
        renames[file_id] = rename_intent(item.before) if rename_changed else rename_intent(live)

    changed_groups = {sources[file_id].group.group.group_id for file_id in revised}
    groups: list[GroupState] = []

    for group in state.groups:
        if group.group.group_id not in changed_groups:
            groups.append(group)
            continue

        reviewed_files = tuple(revised.get(item.file_id, item) for item in group.reviewed_files)
        groups.append(replace(group, selected_metadata=None, reviewed_files=reviewed_files))

    staged = replace(state, review_undo=action.history, groups=tuple(groups))
    staged = rebuild_changed_review_groups(staged, changed_groups, rename_settings, rename_decisions=renames)
    groups = []

    for group in staged.groups:
        if group.group.group_id in changed_groups:
            group = replace(group, revision=group.revision + 1)

        groups.append(group)

    return replace(staged, revision=state.revision + 1, groups=tuple(groups))
