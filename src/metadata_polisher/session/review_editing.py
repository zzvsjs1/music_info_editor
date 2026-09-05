"""Pure human-review commands and fresh filename validation over session state."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import cast

from metadata_polisher.application.changes import ChangeValidationFacts, RenameDecision, build_change_set
from metadata_polisher.application.review import (
    build_field_review_state,
    set_clear_decision,
    set_keep_existing_decision,
    set_manual_decision,
    set_proposal_decision,
)
from metadata_polisher.domain.media import LocalMediaFile
from metadata_polisher.domain.metadata import FieldReadState, MetadataField, Position
from metadata_polisher.domain.review import (
    DecisionOrigin,
    FieldConfidence,
    FieldDecisionKind,
    FieldReviewState,
    FieldValue,
    ReviewReasonCode,
)
from metadata_polisher.infrastructure.settings import RenameSettings
from metadata_polisher.rename.template import FilenameRenderPolicy
from metadata_polisher.session.state import (
    GroupState,
    ReviewedFileState,
    ReviewUndoEntry,
    ReviewUndoFile,
    SessionState,
)

REVIEW_UNDO_LIMIT = 30


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


def _local_reviews(source: LocalMediaFile) -> ReviewedFileState:
    reviews: list[FieldReviewState] = []

    for field in MetadataField:
        read_state = source.read_result.field_states[field]
        value = cast(FieldValue | None, getattr(source.read_result.metadata, field.value))

        # Missing, unreadable and unsupported fields have distinct review states.
        # Preserve a readable value attached to a damaged field, but do not turn
        # an empty tuple or Position into a fabricated existing value.
        if (
            read_state is FieldReadState.MISSING
            or value in (None, (), Position())
            or isinstance(value, str) and not value.strip()
        ):
            value = None

        reviews.append(
            build_field_review_state(
                field=field,
                read_state=read_state,
                existing_value=value,
                proposals=(),
            )
        )

    return ReviewedFileState(source.file_id, reviews=tuple(reviews))


def _complete_reviewed_files(group: GroupState) -> tuple[ReviewedFileState, ...]:
    existing = {item.file_id: item for item in group.reviewed_files}

    return tuple(
        existing[source.file_id]
        if source.file_id in existing and existing[source.file_id].reviews
        else _local_reviews(source)
        for source in group.group.files
    )


def rebuild_reviewed_files(
    state: SessionState,
    group: GroupState,
    reviewed_files: Sequence[ReviewedFileState],
    rename_settings: RenameSettings,
    *,
    rename_decisions: Mapping[str, RenameDecision] | None = None,
) -> tuple[ReviewedFileState, ...]:
    """Derive every preview, then check source and planned sibling destinations.

    A changed title can introduce or remove a collision with a different file's
    requested rename. Two passes ensure that both files use the same current
    preview set and that old blockers are never copied into a fresh ChangeSet.
    Filesystem facts are checked by Apply preflight; this pure preview uses the
    directory entries already known to the session.
    """
    sources = {source.file_id: source for source in group.group.files}
    known_paths = tuple(source.path for item in state.groups for source in item.group.files) + tuple(
        source.path for source in state.unsupported_files
    )
    policy = FilenameRenderPolicy(
        minimum_track_digits=rename_settings.minimum_track_digits,
        minimum_disc_digits=rename_settings.minimum_disc_digits,
    )
    decisions = rename_decisions or {}
    # First derive each filename without sibling destinations. This creates the
    # complete set needed to recognise two files requesting the same new name.
    preliminary: list[ReviewedFileState] = []

    for reviewed in reviewed_files:
        decision = decisions.get(
            reviewed.file_id,
            reviewed.change_set.rename_decision
            if reviewed.change_set is not None
            else RenameDecision.KEEP_FILENAME,
        )

        if not rename_settings.enabled:
            decision = RenameDecision.KEEP_FILENAME

        source = sources[reviewed.file_id]
        changes = build_change_set(
            source,
            reviewed.reviews,
            decision,
            template=rename_settings.template,
            rename_policy=policy,
            validation=ChangeValidationFacts(track_mapping_resolved=reviewed.track_mapping_resolved),
        )
        preliminary.append(replace(reviewed, change_set=changes))

    planned_paths: list[tuple[str, Path]] = []
    other_reviews = tuple(
        reviewed
        for other in state.groups
        if other.group.group_id != group.group.group_id
        for reviewed in other.reviewed_files
    )

    for reviewed in (*preliminary, *other_reviews):
        planned_changes = reviewed.change_set

        if (
            planned_changes is not None
            and planned_changes.rename_decision is RenameDecision.APPLY_RENAME
            and planned_changes.rename_preview is not None
        ):
            planned_paths.append((reviewed.file_id, planned_changes.rename_preview.new_path))

    # Now validate against original names and every other included preview. Do
    # not reserve this file's own preview against itself, which would always collide.
    rebuilt: list[ReviewedFileState] = []

    for reviewed in preliminary:
        source = sources[reviewed.file_id]
        preliminary_changes = reviewed.change_set
        assert preliminary_changes is not None
        existing_names = tuple(path.name for path in known_paths if path.parent == source.path.parent) + tuple(
            path.name
            for file_id, path in planned_paths
            if file_id != reviewed.file_id and path.parent == source.path.parent
        )
        validated = build_change_set(
            source,
            reviewed.reviews,
            preliminary_changes.rename_decision,
            template=rename_settings.template,
            rename_policy=policy,
            validation=ChangeValidationFacts(
                track_mapping_resolved=reviewed.track_mapping_resolved,
                existing_names=existing_names,
            ),
        )
        rebuilt.append(replace(reviewed, change_set=validated))

    return tuple(rebuilt)


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

    return _record_review_action(state, updated)


def _rename_intent(reviewed: ReviewedFileState) -> RenameDecision:
    return reviewed.change_set.rename_decision if reviewed.change_set is not None else RenameDecision.KEEP_FILENAME


def _record_review_action(before: SessionState, after: SessionState) -> SessionState:
    """Store one action after the complete transform, including a whole batch."""
    previous = {
        item.file_id: item for group in before.groups for item in _complete_reviewed_files(group)
    }
    sources = {source.file_id: source for group in before.groups for source in group.group.files}
    changed = tuple(
        ReviewUndoFile(sources[item.file_id], previous[item.file_id], item)
        for group in after.groups for item in group.reviewed_files
        if item.file_id in previous
        and (
            item.reviews != previous[item.file_id].reviews
            or _rename_intent(item) is not _rename_intent(previous[item.file_id])
        )
    )

    if not changed:
        return before

    # One user action creates one undo entry even if it changes many files.
    # Bound memory use by retaining only the most recent review actions.
    history = (*before.review_undo, ReviewUndoEntry(changed))[-REVIEW_UNDO_LIMIT:]

    return replace(after, review_undo=history)


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
            _rename_intent(old) is RenameDecision.KEEP_FILENAME
        ):
            continue

        fresh = _local_reviews(sources[old.file_id])
        old_fields = {review.field: review for review in old.reviews}
        reviews = tuple(retain_user_decision(
            old_fields[review.field], review, candidate_dependency_unchanged=False,
        ) for review in fresh.reviews)
        retained.append(replace(fresh, reviews=reviews))
        rename_decisions[old.file_id] = _rename_intent(old)

    return rebuild_reviewed_files(state, group, retained, rename_settings, rename_decisions=rename_decisions)


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
        reviews = tuple(
            retain_user_decision(
                old_fields[review.field], review,
                candidate_dependency_unchanged=(
                    same_release and (review.field not in _TRACK_FIELDS
                                      or old_tracks.get(reviewed.file_id) == new_tracks.get(reviewed.file_id))
                ),
            ) if review.field in old_fields else review
            for review in reviewed.reviews
        )
        retained.append(replace(reviewed, reviews=reviews, change_set=None))
        renames[reviewed.file_id] = _rename_intent(old)

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

    revised = tuple(
        replace(
            reviewed,
            reviews=tuple(
                _choose_field_decision(review, decision, manual_value, proposal_index)
                if review.field is field
                else review
                for review in reviewed.reviews
            ),
            change_set=None,
        )
        if reviewed.file_id == file_id
        else reviewed
        for reviewed in _complete_reviewed_files(group)
    )
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
        state, group, _complete_reviewed_files(group), rename_settings,
        rename_decisions=dict.fromkeys(file_ids, decision),
    )

    return _install_reviews(state, group, rebuilt)


def accept_safe_additions(state: SessionState, group_id: str, rename_settings: RenameSettings) -> SessionState:
    """Accept untouched missing fields with one confident choice, preserving user decisions."""
    group = _group_for_edit(state, group_id, rename_settings)
    revised: list[ReviewedFileState] = []

    for reviewed in _complete_reviewed_files(group):
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

    affected = {group.group.group_id for group in state.groups
                if not group.requires_rescan and any(item.reviews for item in group.reviewed_files)}

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
            groups.append(replace(group, selected_metadata=None, reviewed_files=tuple(
                by_id.get(item.file_id, item) for item in group.reviewed_files
            )))

        staged = replace(staged, groups=tuple(groups))

    return replace(staged, revision=state.revision + 1, groups=tuple(
        replace(group, revision=group.revision + 1) if group.group.group_id in affected else group
        for group in staged.groups
    ))


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
    command: BatchReviewCommand, field: MetadataField, selected: tuple[tuple[GroupState, LocalMediaFile], ...],
) -> bool:
    if command.action is not BatchReviewAction.SET_COMMON_VALUE or len(selected) < 2:
        return False

    if field is MetadataField.TRACK:
        # A track position is an individual assignment. A shared editor cannot
        # express a sequence; users choose each candidate or edit one track.
        return True

    if field is not MetadataField.DISC:
        return False

    disc_numbers = {source.read_result.metadata.disc.number for _, source in selected
                    if source.read_result.metadata.disc.number is not None}
    media = {
        group.selected_release.identity if group.selected_release else (group.group.group_id,)
        for group, _ in selected
    }

    return len(disc_numbers) > 1 or len(media) > 1


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

    indexed = {source.file_id: (group, source) for group in state.groups for source in group.group.files}

    if not set(command.file_ids).issubset(indexed):
        raise ValueError("A selected file no longer belongs to this library.")

    selected = tuple(indexed[file_id] for file_id in command.file_ids)
    cross_group = len({group.group.group_id for group, _ in selected}) > 1
    # Record each file/field outcome separately. A blocked composer choice need
    # not hide a valid album edit, and these are review changes, not disk writes.
    affected: list[BatchReviewOutcome] = []
    skipped: list[BatchReviewOutcome] = []
    blocked: list[BatchReviewOutcome] = []
    reviews = {item.file_id: item for group in state.groups for item in _complete_reviewed_files(group)}
    rename_decisions = {file_id: _rename_intent(review) for file_id, review in reviews.items()}
    changed_groups: set[str] = set()

    for file_id in command.file_ids:
        group, _source = indexed[file_id]
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
        return BatchReviewResult(state, (), tuple(skipped), tuple(blocked))

    staged = replace(state, groups=tuple(
        replace(
            group, selected_metadata=None,
            reviewed_files=tuple(reviews[source.file_id] for source in group.group.files),
        )
        if group.group.group_id in changed_groups else group
        for group in state.groups
    ))

    # Stage all decisions before deriving sibling collisions. Two passes make
    # cross-group destinations independent of the ordering of selected files.
    for _pass in range(2):
        staged = replace(staged, groups=tuple(
            replace(group, reviewed_files=rebuild_reviewed_files(
                staged, group, group.reviewed_files, rename_settings, rename_decisions=rename_decisions,
            )) if group.group.group_id in changed_groups else group
            for group in staged.groups
        ))

    staged = replace(staged, revision=state.revision + 1, groups=tuple(
        replace(group, revision=group.revision + 1) if group.group.group_id in changed_groups else group
        for group in staged.groups
    ))
    updated = _record_review_action(state, staged)

    return BatchReviewResult(updated, tuple(affected), tuple(skipped), tuple(blocked))


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
    for ids, action in ((keep_ids, BatchReviewAction.KEEP_FILENAMES),
                        (include_ids, BatchReviewAction.INCLUDE_RENAMES)):
        if not ids:
            continue

        result = apply_batch_review(staged, BatchReviewCommand(ids, (), staged.revision, action), rename_settings)
        staged = result.state
        affected.extend(result.affected)
        skipped.extend(result.skipped)
        blocked.extend(result.blocked)

    staged = _record_review_action(state, replace(staged, review_undo=state.review_undo))
    return BatchReviewResult(staged, tuple(affected), tuple(skipped), tuple(blocked))


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


def aggregate_review_fields(
    state: SessionState, file_ids: Sequence[str], fields: Sequence[MetadataField],
) -> tuple[AggregatedFieldReview, ...]:
    """Return display-only aggregates; a Mixed value contains no writable string."""
    ids = tuple(file_ids)

    if not ids or len(set(ids)) != len(ids):
        raise ValueError("Choose a non-empty selection of unique files.")

    reviews = {item.file_id: item for group in state.groups for item in _complete_reviewed_files(group)}

    if not set(ids).issubset(reviews):
        raise ValueError("Every selected file must still belong to this library.")

    rows = []

    for field in fields:
        if not isinstance(field, MetadataField):
            raise TypeError("fields must contain MetadataField values")

        values = tuple(next(review for review in reviews[file_id].reviews if review.field is field) for file_id in ids)
        final = []

        for file_id, review in zip(ids, values, strict=True):
            changes = reviews[file_id].change_set

            if review.decision is FieldDecisionKind.CLEAR:
                final.append(AggregatedValue(AggregateValueState.EMPTY))
            elif changes is not None and review.decision in {
                FieldDecisionKind.USE_MANUAL, FieldDecisionKind.USE_PROPOSAL,
            }:
                # Position proposals can supply only a number or only a total.
                # The ChangeSet retains the other local component; display that
                # actual final value rather than the incomplete candidate input.
                final.append(_value_state(cast(FieldValue | None, getattr(changes.final_metadata, field.value))))
            elif review.decision is FieldDecisionKind.USE_MANUAL:
                final.append(_value_state(review.manual_value))
            elif review.decision is FieldDecisionKind.USE_PROPOSAL and review.selected_proposal is not None:
                final.append(_value_state(review.selected_proposal.value))
            else:
                final.append(_value_state(review.existing_value, review.read_state))

        rows.append(AggregatedFieldReview(
            field, _aggregate(tuple(_value_state(review.existing_value, review.read_state) for review in values)),
            _aggregate(tuple(
                _value_state(review.proposals[0].value if review.proposals else None) for review in values
            )),
            _aggregate(tuple(final)),
        ))

    return tuple(rows)


def _next_review_undo_action(
    state: SessionState,
) -> tuple[tuple[ReviewUndoEntry, ...], tuple[ReviewUndoFile, ...]]:
    """Find the newest usable action and the history that will remain after it."""
    sources = {source.file_id: (group, source) for group in state.groups for source in group.group.files}
    current = {item.file_id: item for group in state.groups for item in group.reviewed_files}
    history = list(state.review_undo)

    # UI availability and Undo must use identical source/evidence checks. A
    # wholly stale action is skipped; untouched members of a batch remain usable.
    while history:
        entry = history.pop()
        usable = []

        for item in entry.files:
            file_id = item.source.file_id
            located = sources.get(file_id)
            live = current.get(file_id)

            if located is None or live is None or located[0].requires_rescan or located[1] != item.source:
                continue

            before_fields = {review.field: review for review in item.before.reviews}
            after_fields = {review.field: review for review in item.after.reviews}
            live_fields = {review.field: review for review in live.reviews}
            changed_fields = {field for field in before_fields if before_fields[field] != after_fields[field]}
            rename_changed = _rename_intent(item.before) is not _rename_intent(item.after)

            # Undo is valid only while the touched fields still have this action's
            # output. Otherwise restoring them could overwrite a later decision.
            if any(live_fields.get(field) != after_fields[field] for field in changed_fields):
                continue

            if rename_changed and _rename_intent(live) is not _rename_intent(item.after):
                continue

            usable.append(item)

        if usable:
            return tuple(history), tuple(usable)

    return (), ()


def review_undo_targets(state: SessionState) -> tuple[ReviewUndoFile, ...]:
    """Describe precisely what Undo can restore, independently of UI selection."""
    if state.active_operation is not None:
        return ()

    return _next_review_undo_action(state)[1]


def undo_last_review_action(state: SessionState, rename_settings: RenameSettings) -> SessionState:
    """Undo one valid review action, without restoring stale or already-written files."""
    if state.active_operation is not None:
        raise ValueError("Wait for the current operation before undoing review decisions.")

    if not state.review_undo:
        return state

    history, targets = _next_review_undo_action(state)

    if not targets:
        return replace(state, review_undo=history)

    sources = {source.file_id: (group, source) for group in state.groups for source in group.group.files}
    current = {item.file_id: item for group in state.groups for item in group.reviewed_files}
    revised: dict[str, ReviewedFileState] = {}
    renames: dict[str, RenameDecision] = {}

    for item in targets:
        file_id = item.source.file_id
        live = current[file_id]
        before_fields = {review.field: review for review in item.before.reviews}
        after_fields = {review.field: review for review in item.after.reviews}
        changed_fields = {field for field in before_fields if before_fields[field] != after_fields[field]}
        rename_changed = _rename_intent(item.before) is not _rename_intent(item.after)

        # Restore only the fields changed by this action so later unrelated
        # review choices and current filename validation remain intact.
        revised[file_id] = replace(live, change_set=None, reviews=tuple(
            before_fields[review.field] if review.field in changed_fields else review for review in live.reviews
        ))
        renames[file_id] = _rename_intent(item.before) if rename_changed else _rename_intent(live)

    changed_groups = {sources[file_id][0].group.group_id for file_id in revised}
    staged = replace(state, review_undo=tuple(history), groups=tuple(
        replace(group, selected_metadata=None, reviewed_files=tuple(revised.get(item.file_id, item)
                                                                  for item in group.reviewed_files))
        if group.group.group_id in changed_groups else group for group in state.groups
    ))

    for _pass in range(2):
        staged = replace(staged, groups=tuple(
            replace(group, reviewed_files=rebuild_reviewed_files(
                staged, group, group.reviewed_files, rename_settings, rename_decisions=renames,
            )) if group.group.group_id in changed_groups else group for group in staged.groups
        ))

    return replace(staged, revision=state.revision + 1, groups=tuple(
        replace(group, revision=group.revision + 1) if group.group.group_id in changed_groups else group
        for group in staged.groups
    ))
