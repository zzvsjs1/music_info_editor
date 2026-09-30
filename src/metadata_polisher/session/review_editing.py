"""Pure human-review commands and fresh filename validation over session state."""

from collections.abc import Sequence
from dataclasses import dataclass, replace
from enum import StrEnum

from metadata_polisher.application.changes import RenameDecision
from metadata_polisher.application.review import (
    set_clear_decision,
    set_keep_existing_decision,
    set_manual_decision,
    set_proposal_decision,
)
from metadata_polisher.domain.metadata import FieldReadState, MetadataField, Position, metadata_value
from metadata_polisher.domain.review import (
    DecisionOrigin,
    FieldConfidence,
    FieldDecisionKind,
    FieldReviewState,
    FieldValue,
    ReviewReasonCode,
)
from metadata_polisher.infrastructure.settings import RenameSettings
from metadata_polisher.session.review_history import (
    REVIEW_UNDO_LIMIT,
    record_review_action,
    review_undo_targets,
    undo_last_review_action,
)
from metadata_polisher.session.review_previews import (
    ReviewFileSource,
    complete_reviewed_files,
    complete_reviewed_files_by_id,
    local_reviews,
    rebuild_changed_review_groups,
    rebuild_reviewed_files,
    rename_intent,
    review_sources_by_id,
)
from metadata_polisher.session.state import (
    GroupState,
    ReleaseMediumIdentity,
    ReviewedFileState,
    ReviewUndoEntry,
    ReviewUndoFile,
    SessionState,
)

# Existing session callers keep one command import surface. Preview rebuilding
# and Undo implementation live separately, while these exports remain the same
# objects rather than additional forwarding functions.
__all__ = (
    "REVIEW_UNDO_LIMIT",
    "AggregateValueState",
    "AggregatedFieldReview",
    "AggregatedValue",
    "BatchReviewAction",
    "BatchReviewCommand",
    "BatchReviewOutcome",
    "BatchReviewResult",
    "accept_safe_additions",
    "aggregate_review_fields",
    "apply_batch_review",
    "apply_field_decision",
    "apply_rename_choices",
    "local_reviews_after_lookup_reset",
    "rebuild_reviewed_files",
    "reconcile_lookup_reviews",
    "refresh_rename_previews",
    "regrouped_review_undo",
    "retain_user_decision",
    "review_undo_targets",
    "set_rename_decision",
    "undo_last_review_action",
)


class BatchReviewAction(StrEnum):
    """Explicit operations over each captured file, never over visible row numbers."""

    KEEP_EXISTING = "keep_existing"
    USE_CANDIDATE = "use_candidate"
    SET_COMMON_VALUE = "set_common_value"
    CLEAR = "clear"
    ACCEPT_SAFE_ADDITIONS = "accept_safe_additions"
    INCLUDE_RENAMES = "include_renames"
    KEEP_FILENAMES = "keep_filenames"


_RENAME_ACTIONS = frozenset((BatchReviewAction.INCLUDE_RENAMES, BatchReviewAction.KEEP_FILENAMES))
_ALBUM_FIELDS = frozenset((MetadataField.ALBUM, MetadataField.ALBUM_ARTISTS, MetadataField.DATE))
_TRACK_FIELDS = frozenset((MetadataField.TITLE, MetadataField.ARTISTS, MetadataField.COMPOSERS, MetadataField.TRACK))


@dataclass(frozen=True)
class BatchReviewCommand:
    """A reviewed scope captured before a dialogue or another UI interaction."""

    file_ids: tuple[str, ...]
    fields: tuple[MetadataField, ...]
    expected_revision: int
    action: BatchReviewAction
    common_value: FieldValue | None = None
    confirm_cross_group: bool = False

    def __post_init__(self) -> None:
        raw_ids: object = self.file_ids

        if isinstance(raw_ids, (str, bytes)) or not isinstance(raw_ids, Sequence):
            raise TypeError("file_ids must be a sequence of stable identities")

        ids = tuple(raw_ids)
        fields = tuple(self.fields)

        if not ids or any(not isinstance(item, str) or not item.strip() for item in ids) or len(set(ids)) != len(ids):
            raise ValueError("Choose a non-empty selection of unique file identities.")

        if any(not isinstance(item, MetadataField) for item in fields) or len(set(fields)) != len(fields):
            raise ValueError("Choose unique metadata fields.")

        if type(self.expected_revision) is not int or self.expected_revision < 0:
            raise ValueError("expected_revision must be a non-negative integer")

        if not isinstance(self.action, BatchReviewAction):
            raise TypeError("action must be a BatchReviewAction")

        if bool(fields) == (self.action in _RENAME_ACTIONS):
            raise ValueError("Field actions require fields; filename actions have their own independent scope.")

        if type(self.confirm_cross_group) is not bool:
            raise TypeError("confirm_cross_group must be a boolean")

        if self.action is BatchReviewAction.SET_COMMON_VALUE:
            if self.common_value is None:
                raise ValueError("A common value must be explicit; use Clear to remove a field.")
        elif self.common_value is not None:
            raise ValueError("Only Set common value accepts a shared value.")

        object.__setattr__(self, "file_ids", ids)
        object.__setattr__(self, "fields", fields)


@dataclass(frozen=True)
class BatchReviewOutcome:
    """One file/field outcome; field=None denotes that file's rename intent."""

    file_id: str
    field: MetadataField | None
    reason: str


@dataclass(frozen=True)
class BatchReviewResult:
    """Counts come from these disjoint operations, not inferred disk changes."""

    state: SessionState
    affected: tuple[BatchReviewOutcome, ...] = ()
    skipped: tuple[BatchReviewOutcome, ...] = ()
    blocked: tuple[BatchReviewOutcome, ...] = ()


class AggregateValueState(StrEnum):
    """Presentation states which cannot accidentally become writable tag text."""

    VALUE = "value"
    MIXED = "mixed"
    MISSING = "missing"
    EMPTY = "empty"
    UNREADABLE = "unreadable"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class AggregatedValue:
    state: AggregateValueState
    value: FieldValue | None = None


@dataclass(frozen=True)
class AggregatedFieldReview:
    field: MetadataField
    existing: AggregatedValue
    proposed: AggregatedValue
    final: AggregatedValue


def _group_for_edit(state: SessionState, group_id: str, rename_settings: RenameSettings) -> GroupState:
    if state.active_operation is not None:
        raise ValueError("Wait for the current operation before editing metadata.")

    if not isinstance(rename_settings, RenameSettings):
        raise TypeError("rename_settings must be RenameSettings")

    group = next((item for item in state.groups if item.group.group_id == group_id), None)

    if group is None:
        raise ValueError("The selected group no longer exists.")

    if group.requires_rescan:
        raise ValueError("Rescan this group before editing metadata because its files have changed.")

    return group


def _install_reviews(
    state: SessionState,
    group: GroupState,
    reviewed_files: tuple[ReviewedFileState, ...],
) -> SessionState:
    # The worker receipt is an exact projection of its automatic decisions.
    # Human review retains the provider evidence while replacing that projection.
    replacement = replace(
        group,
        reviewed_files=reviewed_files,
        selected_metadata=None,
        revision=group.revision + 1,
    )

    updated = replace(
        state,
        groups=tuple(replacement if item is group else item for item in state.groups),
        revision=state.revision + 1,
    )

    return record_review_action(state, updated)


def retain_user_decision(
    old: FieldReviewState,
    fresh: FieldReviewState,
    *,
    candidate_dependency_unchanged: bool = True,
) -> FieldReviewState:
    """Retain independent human choices; a new association requires review again."""
    if old.decision_origin is not DecisionOrigin.USER:
        return fresh

    # Keep, Clear and manual values express local intent independently of the
    # candidate. Choosing a proposal additionally depends on its source evidence.
    if old.decision is FieldDecisionKind.KEEP_EXISTING:
        return set_keep_existing_decision(fresh)

    if old.decision is FieldDecisionKind.CLEAR:
        return set_clear_decision(fresh)

    if old.decision is FieldDecisionKind.USE_MANUAL:
        assert old.manual_value is not None
        return set_manual_decision(fresh, old.manual_value)

    if old.decision is FieldDecisionKind.USE_PROPOSAL and old.selected_proposal is not None:
        selected = old.selected_proposal
        equivalent = next((
            item for item in fresh.proposals
            if item.value == selected.value and item.language == selected.language
            and item.script == selected.script and item.provenances == selected.provenances
        ), None)

        if candidate_dependency_unchanged and equivalent is not None:
            return set_proposal_decision(fresh, equivalent)

        # Even identical text can belong to a different track or release. Retain
        # the original local value until the newly associated candidate is reviewed.
        return replace(
            fresh, decision=FieldDecisionKind.UNRESOLVED, selected_proposal=None, manual_value=None,
            decision_origin=DecisionOrigin.DEFAULT, requires_review=True,
            reason_codes=tuple(dict.fromkeys((*fresh.reason_codes, ReviewReasonCode.CANDIDATE_DEPENDENCY_CHANGED))),
        )

    return fresh


def local_reviews_after_lookup_reset(
    state: SessionState, group: GroupState, rename_settings: RenameSettings,
) -> tuple[ReviewedFileState, ...]:
    """Drop obsolete provider evidence while keeping independent local decisions."""
    sources = {source.file_id: source for source in group.group.files}
    retained = []
    rename_decisions = {}

    for old in group.reviewed_files:
        if not any(review.decision_origin is DecisionOrigin.USER for review in old.reviews) and (
            rename_intent(old) is RenameDecision.KEEP_FILENAME
        ):
            continue

        fresh = local_reviews(sources[old.file_id])
        retained.append(_retain_local_fields(fresh, old))
        rename_decisions[old.file_id] = rename_intent(old)

    return rebuild_reviewed_files(state, group, retained, rename_settings, rename_decisions=rename_decisions)


def _retain_local_fields(fresh: ReviewedFileState, previous: ReviewedFileState) -> ReviewedFileState:
    """Project independent choices onto local evidence after a lookup is reset."""
    old_fields = {review.field: review for review in previous.reviews}
    reviews: list[FieldReviewState] = []

    for review in fresh.reviews:
        retained = retain_user_decision(
            old_fields[review.field], review, candidate_dependency_unchanged=False,
        )
        reviews.append(retained)

    return replace(fresh, reviews=tuple(reviews))


def _has_independent_field_change(item: ReviewUndoFile) -> bool:
    before_fields = {review.field: review for review in item.before.reviews}
    after_fields = {review.field: review for review in item.after.reviews}
    independent = {FieldDecisionKind.KEEP_EXISTING, FieldDecisionKind.USE_MANUAL, FieldDecisionKind.CLEAR}

    for field, before in before_fields.items():
        after = after_fields[field]

        if before == after:
            continue

        for review in (before, after):
            if review.decision_origin is DecisionOrigin.USER and review.decision in independent:
                return True

    return False


def _project_local_history_review(
    state: SessionState,
    located: ReviewFileSource,
    fresh: ReviewedFileState,
    previous: ReviewedFileState,
    rename_settings: RenameSettings,
) -> ReviewedFileState:
    retained = _retain_local_fields(fresh, previous)
    rebuilt = rebuild_reviewed_files(
        state, located.group, (retained,), rename_settings,
        rename_decisions={located.source.file_id: rename_intent(previous)},
    )

    return rebuilt[0]


def regrouped_review_undo(
    state: SessionState, affected_file_ids: frozenset[str], rename_settings: RenameSettings,
) -> tuple[ReviewUndoEntry, ...]:
    """Retain local Undo actions after group membership invalidates provider data.

    History follows the unchanged source identity, rather than the retired group
    ID. Rebuild both sides against local metadata so Undo cannot resurrect an old
    release, proposal or mapping. Candidate-only actions have nothing to restore.
    """
    sources = review_sources_by_id(state)
    history = []

    for entry in state.review_undo:
        retained = []

        for item in entry.files:
            file_id = item.source.file_id

            if file_id not in affected_file_ids:
                retained.append(item)
                continue

            located = sources.get(file_id)

            if located is None or located.source != item.source:
                continue

            if not _has_independent_field_change(item) and rename_intent(item.before) is rename_intent(item.after):
                continue

            fresh = local_reviews(located.source)
            before = _project_local_history_review(state, located, fresh, item.before, rename_settings)
            after = _project_local_history_review(state, located, fresh, item.after, rename_settings)
            retained.append(ReviewUndoFile(source=located.source, before=before, after=after))

        if retained:
            history.append(ReviewUndoEntry(tuple(retained)))

    return tuple(history)


def reconcile_lookup_reviews(
    state: SessionState, previous: GroupState, fresh: GroupState, rename_settings: RenameSettings,
) -> GroupState:
    """Merge worker proposals with human choices using release and track dependencies."""
    old_files = {item.file_id: item for item in previous.reviewed_files}
    old_mapping = previous.effective_track_mapping
    new_mapping = fresh.effective_track_mapping
    old_tracks = {item.local_file_id: item.provider_track_index for item in old_mapping.mappings} if old_mapping else {}
    new_tracks = {item.local_file_id: item.provider_track_index for item in new_mapping.mappings} if new_mapping else {}
    same_release = (
        previous.selected_release is not None and fresh.selected_release is not None
        and previous.selected_release.identity == fresh.selected_release.identity
    )
    retained = []
    renames = {}

    for reviewed in fresh.reviewed_files:
        old = old_files.get(reviewed.file_id)

        if old is None:
            retained.append(reviewed)
            continue

        old_fields = {review.field: review for review in old.reviews}
        reviews = []
        same_track = old_tracks.get(reviewed.file_id) == new_tracks.get(reviewed.file_id)

        for review in reviewed.reviews:
            previous_review = old_fields.get(review.field)

            if previous_review is not None:
                dependency_unchanged = same_release and (review.field not in _TRACK_FIELDS or same_track)
                review = retain_user_decision(
                    previous_review, review, candidate_dependency_unchanged=dependency_unchanged,
                )

            reviews.append(review)

        retained.append(replace(reviewed, reviews=tuple(reviews), change_set=None))
        renames[reviewed.file_id] = rename_intent(old)

    if not old_files:
        return fresh

    rebuilt = rebuild_reviewed_files(state, fresh, retained, rename_settings, rename_decisions=renames)

    return replace(fresh, reviewed_files=rebuilt, selected_metadata=None)


def _choose_field_decision(
    review: FieldReviewState,
    decision: FieldDecisionKind,
    manual_value: FieldValue | None,
    proposal_index: int,
) -> FieldReviewState:
    if decision is FieldDecisionKind.KEEP_EXISTING:
        return set_keep_existing_decision(review)

    if decision is FieldDecisionKind.CLEAR:
        return set_clear_decision(review)

    if decision is FieldDecisionKind.USE_MANUAL:
        if manual_value is None:
            raise ValueError("A manual decision requires a non-empty value; use Clear to remove a value.")

        return set_manual_decision(review, manual_value)

    if decision is FieldDecisionKind.USE_PROPOSAL:
        if type(proposal_index) is not int or not 0 <= proposal_index < len(review.proposals):
            raise ValueError("Choose one of the available field proposals.")

        return set_proposal_decision(review, review.proposals[proposal_index])

    raise ValueError("Choose Keep Existing, Use Proposed, Manual Value or Clear.")


def apply_field_decision(
    state: SessionState,
    group_id: str,
    file_id: str,
    field: MetadataField,
    decision: FieldDecisionKind,
    rename_settings: RenameSettings,
    *,
    manual_value: FieldValue | None = None,
    proposal_index: int = 0,
) -> SessionState:
    """Apply one explicit field decision and rebuild the group's derived changes."""
    group = _group_for_edit(state, group_id, rename_settings)

    if not isinstance(field, MetadataField):
        raise TypeError("field must be a MetadataField")

    if file_id not in {source.file_id for source in group.group.files}:
        raise ValueError("The selected file no longer belongs to this group.")

    revised: list[ReviewedFileState] = []

    for reviewed in complete_reviewed_files(group):
        if reviewed.file_id != file_id:
            revised.append(reviewed)
            continue

        reviews: list[FieldReviewState] = []

        for review in reviewed.reviews:
            if review.field is field:
                review = _choose_field_decision(review, decision, manual_value, proposal_index)

            reviews.append(review)

        revised.append(replace(reviewed, reviews=tuple(reviews), change_set=None))
    rename_decisions = {
        reviewed.file_id: reviewed.change_set.rename_decision
        for reviewed in group.reviewed_files
        if reviewed.change_set is not None
    }

    return _install_reviews(
        state, group,
        rebuild_reviewed_files(state, group, revised, rename_settings, rename_decisions=rename_decisions),
    )


def set_rename_decision(
    state: SessionState,
    group_id: str,
    file_ids: Sequence[str],
    decision: RenameDecision,
    rename_settings: RenameSettings,
) -> SessionState:
    """Choose filenames independently of the retained field review decisions."""
    group = _group_for_edit(state, group_id, rename_settings)

    if not isinstance(decision, RenameDecision):
        raise TypeError("decision must be a RenameDecision")

    if isinstance(file_ids, (str, bytes)) or not file_ids or len(file_ids) != len(set(file_ids)):
        raise ValueError("Choose a non-empty selection of unique files.")

    if not set(file_ids).issubset(source.file_id for source in group.group.files):
        raise ValueError("Every selected file must belong to this group.")

    if decision is RenameDecision.APPLY_RENAME and not rename_settings.enabled:
        raise ValueError("Enable filename renaming in Settings before accepting filename changes.")

    rebuilt = rebuild_reviewed_files(
        state, group, complete_reviewed_files(group), rename_settings,
        rename_decisions=dict.fromkeys(file_ids, decision),
    )

    return _install_reviews(state, group, rebuilt)


def accept_safe_additions(state: SessionState, group_id: str, rename_settings: RenameSettings) -> SessionState:
    """Accept untouched missing fields with one confident choice, preserving user decisions."""
    group = _group_for_edit(state, group_id, rename_settings)
    revised: list[ReviewedFileState] = []

    for reviewed in complete_reviewed_files(group):
        reviews = tuple(
            set_proposal_decision(review, review.proposals[0])
            if _is_safe_addition(review)
            else review
            for review in reviewed.reviews
        )
        revised.append(replace(reviewed, reviews=reviews, change_set=None))

    rename_decisions = {
        reviewed.file_id: reviewed.change_set.rename_decision
        for reviewed in group.reviewed_files
        if reviewed.change_set is not None
    }

    return _install_reviews(
        state, group,
        rebuild_reviewed_files(state, group, revised, rename_settings, rename_decisions=rename_decisions),
    )


def refresh_rename_previews(state: SessionState, rename_settings: RenameSettings) -> SessionState:
    """Rebuild settings-dependent previews without changing provider or field decisions."""
    if state.active_operation is not None:
        raise ValueError("Wait for the current operation before changing filename settings.")

    affected: set[str] = set()

    for group in state.groups:
        if group.requires_rescan:
            continue

        if any(item.reviews for item in group.reviewed_files):
            affected.add(group.group.group_id)

    if not affected:
        return state

    staged = state

    # The first pass renders every group with the new settings. The second sees
    # that complete destination set, including groups sharing a physical folder.
    for _pass in range(2):
        groups = []

        for group in staged.groups:
            if group.group.group_id not in affected:
                groups.append(group)
                continue

            rebuilt = rebuild_reviewed_files(
                staged, group, tuple(item for item in group.reviewed_files if item.reviews), rename_settings,
            )
            by_id = {item.file_id: item for item in rebuilt}
            reviewed_files = tuple(by_id.get(item.file_id, item) for item in group.reviewed_files)
            groups.append(replace(group, selected_metadata=None, reviewed_files=reviewed_files))

        staged = replace(staged, groups=tuple(groups))

    groups = []

    for group in staged.groups:
        if group.group.group_id in affected:
            group = replace(group, revision=group.revision + 1)

        groups.append(group)

    return replace(staged, revision=state.revision + 1, groups=tuple(groups))


def _is_safe_addition(review: FieldReviewState) -> bool:
    return (
        review.decision_origin is DecisionOrigin.DEFAULT
        and review.read_state is FieldReadState.MISSING
        and len(review.proposals) == 1
        and review.proposals[0].confidence is FieldConfidence.HIGH
        and ReviewReasonCode.PROPOSAL_AMBIGUOUS not in review.reason_codes
    )


def _batch_field_decision(review: FieldReviewState, command: BatchReviewCommand) -> FieldReviewState | None:
    if command.action is BatchReviewAction.KEEP_EXISTING:
        return set_keep_existing_decision(review)

    if command.action is BatchReviewAction.CLEAR:
        return set_clear_decision(review)

    if command.action is BatchReviewAction.SET_COMMON_VALUE:
        assert command.common_value is not None
        return set_manual_decision(review, command.common_value)

    if command.action is BatchReviewAction.USE_CANDIDATE:
        return set_proposal_decision(review, review.proposals[0]) if review.proposals else None

    if command.action is BatchReviewAction.ACCEPT_SAFE_ADDITIONS and _is_safe_addition(review):
        return set_proposal_decision(review, review.proposals[0])

    return None


def _position_scope_blocked(
    command: BatchReviewCommand, field: MetadataField, selected: tuple[ReviewFileSource, ...],
) -> bool:
    if command.action is not BatchReviewAction.SET_COMMON_VALUE or len(selected) < 2:
        return False

    if field is MetadataField.TRACK:
        # A track position is an individual assignment. A shared editor cannot
        # express a sequence; users choose each candidate or edit one track.
        return True

    if field is not MetadataField.DISC:
        return False

    disc_numbers: set[int] = set()
    selected_media: set[ReleaseMediumIdentity] = set()
    local_groups: set[str] = set()

    for located in selected:
        number = located.source.read_result.metadata.disc.number

        if number is not None:
            disc_numbers.add(number)

        selection = located.group.selected_release

        if selection is None:
            local_groups.add(located.group.group.group_id)
        else:
            selected_media.add(selection.identity)

    # A local-only group and a catalogue medium are distinct scope identities.
    # Counting their separate sets retains that rule without mixing two tuple
    # shapes or relying on positional unpacking to discover their meaning.
    return len(disc_numbers) > 1 or len(selected_media) + len(local_groups) > 1


def apply_batch_review(
    state: SessionState, command: BatchReviewCommand, rename_settings: RenameSettings,
) -> BatchReviewResult:
    """Apply one captured review action atomically, with per-file/per-field outcomes."""
    if not isinstance(command, BatchReviewCommand):
        raise TypeError("command must be a BatchReviewCommand")

    if not isinstance(rename_settings, RenameSettings):
        raise TypeError("rename_settings must be RenameSettings")

    if state.active_operation is not None:
        raise ValueError("Wait for the current operation before editing metadata.")

    # A dialogue can outlive the selection it was opened for. Reject stale
    # commands before touching any member, even if some captured IDs still exist.
    if command.expected_revision != state.revision:
        raise ValueError("The review revision changed; capture the current selection again.")

    indexed = review_sources_by_id(state)

    if not set(command.file_ids).issubset(indexed):
        raise ValueError("A selected file no longer belongs to this library.")

    selected = tuple(indexed[file_id] for file_id in command.file_ids)
    cross_group = len({located.group.group.group_id for located in selected}) > 1
    # Record each file/field outcome separately. A blocked composer choice need
    # not hide a valid album edit, and these are review changes, not disk writes.
    affected: list[BatchReviewOutcome] = []
    skipped: list[BatchReviewOutcome] = []
    blocked: list[BatchReviewOutcome] = []
    reviews = complete_reviewed_files_by_id(state)
    rename_decisions = {file_id: rename_intent(review) for file_id, review in reviews.items()}
    changed_groups: set[str] = set()

    for file_id in command.file_ids:
        group = indexed[file_id].group
        fields: tuple[MetadataField | None, ...] = (None,) if command.action in _RENAME_ACTIONS else command.fields

        for field in fields:
            reason = ""

            if group.requires_rescan:
                reason = "Rescan this file before changing its review."
            elif field is None and command.action is BatchReviewAction.INCLUDE_RENAMES and not rename_settings.enabled:
                reason = "Enable filename renaming in Settings first."
            elif field is not None and _position_scope_blocked(command, field, selected):
                reason = "Track and disc positions require their own track/medium scope; use each file's candidate."
            elif (
                field in _ALBUM_FIELDS and cross_group and not command.confirm_cross_group
                and command.action in {
                    BatchReviewAction.SET_COMMON_VALUE, BatchReviewAction.CLEAR, BatchReviewAction.USE_CANDIDATE,
                }
            ):
                reason = "Confirm the album-level edit across different groups or releases."

            if reason:
                blocked.append(BatchReviewOutcome(file_id, field, reason))
                continue

            if field is None:
                decision = (
                    RenameDecision.APPLY_RENAME if command.action is BatchReviewAction.INCLUDE_RENAMES
                    else RenameDecision.KEEP_FILENAME
                )

                if rename_decisions[file_id] is decision:
                    skipped.append(BatchReviewOutcome(file_id, None, "Filename intent is already selected."))
                    continue

                rename_decisions[file_id] = decision
            else:
                current = next(review for review in reviews[file_id].reviews if review.field is field)

                if current.read_state in {FieldReadState.UNREADABLE, FieldReadState.UNSUPPORTED} and (
                    command.action is not BatchReviewAction.KEEP_EXISTING
                ):
                    blocked.append(BatchReviewOutcome(file_id, field, "This field cannot safely be edited."))
                    continue

                if (
                    field in _TRACK_FIELDS and not reviews[file_id].track_mapping_resolved
                    and command.action in {BatchReviewAction.USE_CANDIDATE, BatchReviewAction.ACCEPT_SAFE_ADDITIONS}
                    and current.proposals
                ):
                    blocked.append(BatchReviewOutcome(file_id, field, "Resolve this file's track mapping first."))
                    continue

                try:
                    replacement = _batch_field_decision(current, command)
                except (TypeError, ValueError) as error:
                    blocked.append(BatchReviewOutcome(file_id, field, str(error)))
                    continue

                if replacement is None or replacement == current:
                    skipped.append(BatchReviewOutcome(file_id, field, "No eligible candidate or new decision."))
                    continue

                reviews[file_id] = replace(
                    reviews[file_id], change_set=None,
                    reviews=tuple(replacement if item.field is field else item for item in reviews[file_id].reviews),
                )

            affected.append(BatchReviewOutcome(file_id, field, "Review decision updated; no files written."))
            changed_groups.add(group.group.group_id)

    if not affected:
        return BatchReviewResult(state=state, skipped=tuple(skipped), blocked=tuple(blocked))

    groups: list[GroupState] = []

    for group in state.groups:
        if group.group.group_id in changed_groups:
            reviewed_files = tuple(reviews[source.file_id] for source in group.group.files)
            group = replace(group, selected_metadata=None, reviewed_files=reviewed_files)

        groups.append(group)

    staged = replace(state, groups=tuple(groups))
    staged = rebuild_changed_review_groups(
        staged, changed_groups, rename_settings, rename_decisions=rename_decisions,
    )
    groups = []

    for group in staged.groups:
        if group.group.group_id in changed_groups:
            group = replace(group, revision=group.revision + 1)

        groups.append(group)

    staged = replace(staged, revision=state.revision + 1, groups=tuple(groups))
    updated = record_review_action(state, staged)

    return BatchReviewResult(state=updated, affected=tuple(affected), skipped=tuple(skipped), blocked=tuple(blocked))


def apply_rename_choices(
    state: SessionState, include_ids: tuple[str, ...], keep_ids: tuple[str, ...], rename_settings: RenameSettings,
) -> BatchReviewResult:
    """Compose a filename dialogue's choices as one reversible review action."""
    if set(include_ids) & set(keep_ids):
        raise ValueError("A file cannot be both renamed and kept in the same action.")

    staged = state
    affected: list[BatchReviewOutcome] = []
    skipped: list[BatchReviewOutcome] = []
    blocked: list[BatchReviewOutcome] = []

    # Clear old rename intent before calculating new sibling destinations.
    # Intermediate undo entries are replaced with one net action below.
    for action in (BatchReviewAction.KEEP_FILENAMES, BatchReviewAction.INCLUDE_RENAMES):
        ids = keep_ids if action is BatchReviewAction.KEEP_FILENAMES else include_ids

        if not ids:
            continue

        command = BatchReviewCommand(file_ids=ids, fields=(), expected_revision=staged.revision, action=action)
        result = apply_batch_review(staged, command, rename_settings)
        staged = result.state
        affected.extend(result.affected)
        skipped.extend(result.skipped)
        blocked.extend(result.blocked)

    staged = record_review_action(state, replace(staged, review_undo=state.review_undo))
    return BatchReviewResult(state=staged, affected=tuple(affected), skipped=tuple(skipped), blocked=tuple(blocked))


def _value_state(value: FieldValue | None, read_state: FieldReadState | None = None) -> AggregatedValue:
    if read_state in {FieldReadState.UNREADABLE, FieldReadState.UNSUPPORTED, FieldReadState.MISSING}:
        return AggregatedValue(AggregateValueState(read_state.value))

    if value is None:
        return AggregatedValue(AggregateValueState.MISSING)

    if value in ((), Position(), ""):
        return AggregatedValue(AggregateValueState.EMPTY)

    return AggregatedValue(AggregateValueState.VALUE, value)


def _aggregate(values: tuple[AggregatedValue, ...]) -> AggregatedValue:
    # MIXED carries no text payload, so a display label cannot accidentally
    # become a common metadata value when the user reviews several files.
    first = values[0]

    return first if all(value == first for value in values) else AggregatedValue(AggregateValueState.MIXED)


def _final_aggregate_value(reviewed: ReviewedFileState, review: FieldReviewState) -> AggregatedValue:
    if review.decision is FieldDecisionKind.CLEAR:
        return AggregatedValue(AggregateValueState.EMPTY)

    if reviewed.change_set is not None and review.decision in {
        FieldDecisionKind.USE_MANUAL, FieldDecisionKind.USE_PROPOSAL,
    }:
        # A position proposal can supply only a number or only a total. The
        # ChangeSet merges the other local component, so display that actual
        # final value rather than the incomplete input that produced it.
        final_value = metadata_value(reviewed.change_set.final_metadata, review.field)

        return _value_state(final_value)

    if review.decision is FieldDecisionKind.USE_MANUAL:
        return _value_state(review.manual_value)

    if review.decision is FieldDecisionKind.USE_PROPOSAL and review.selected_proposal is not None:
        return _value_state(review.selected_proposal.value)

    return _value_state(review.existing_value, review.read_state)


def aggregate_review_fields(
    state: SessionState, file_ids: Sequence[str], fields: Sequence[MetadataField],
) -> tuple[AggregatedFieldReview, ...]:
    """Return display-only aggregates; a Mixed value contains no writable string."""
    ids = tuple(file_ids)

    if not ids or len(set(ids)) != len(ids):
        raise ValueError("Choose a non-empty selection of unique files.")

    reviews = complete_reviewed_files_by_id(state)

    if not set(ids).issubset(reviews):
        raise ValueError("Every selected file must still belong to this library.")

    rows = []

    for field in fields:
        if not isinstance(field, MetadataField):
            raise TypeError("fields must contain MetadataField values")

        existing: list[AggregatedValue] = []
        proposed: list[AggregatedValue] = []
        final: list[AggregatedValue] = []

        for file_id in ids:
            reviewed = reviews[file_id]
            review = next(item for item in reviewed.reviews if item.field is field)
            proposal = review.proposals[0].value if review.proposals else None
            existing.append(_value_state(review.existing_value, review.read_state))
            proposed.append(_value_state(proposal))
            final.append(_final_aggregate_value(reviewed, review))

        rows.append(AggregatedFieldReview(
            field=field,
            existing=_aggregate(tuple(existing)),
            proposed=_aggregate(tuple(proposed)),
            final=_aggregate(tuple(final)),
        ))

    return tuple(rows)
