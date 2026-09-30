"""Regrouping retains local intent and usable Undo without stale provider evidence."""

from dataclasses import replace

import pytest

from metadata_polisher.application.changes import RenameDecision
from metadata_polisher.domain.metadata import MetadataField
from metadata_polisher.domain.review import DecisionOrigin, FieldDecisionKind, ReviewReasonCode
from metadata_polisher.infrastructure.settings import RenameSettings
from metadata_polisher.session.group_editing import merge_session_groups, set_disc_override, split_session_group
from metadata_polisher.session.review_editing import (
    apply_field_decision,
    review_undo_targets,
    set_rename_decision,
    undo_last_review_action,
)
from metadata_polisher.session.state import GroupState
from tests.unit.session.test_lookup_editing import make_selected_session
from tests.unit.session.test_review_editing import make_local_session, make_source


def by_file(state):
    return {review.file_id: review for group in state.groups for review in group.reviewed_files}


@pytest.mark.parametrize("decision,value", [
    (FieldDecisionKind.USE_MANUAL, "Reviewed title"),
    (FieldDecisionKind.CLEAR, None),
    (FieldDecisionKind.KEEP_EXISTING, None),
])
def test_split_then_merge_preserves_independent_field_and_rename_decisions(decision, value):
    first, second = make_source("first.flac"), make_source("second.flac")
    state = make_local_session(first, second)
    settings = RenameSettings()
    state = apply_field_decision(
        state, "album", first.file_id, MetadataField.TITLE, decision, settings, manual_value=value,
    )
    state = set_rename_decision(state, "album", (first.file_id,), RenameDecision.APPLY_RENAME, settings)

    split = split_session_group(state, "album", (first.file_id,))
    retained = by_file(split)[first.file_id]
    title = next(review for review in retained.reviews if review.field is MetadataField.TITLE)

    assert title.decision is decision
    assert title.decision_origin is DecisionOrigin.USER
    assert title.manual_value == value
    assert retained.change_set.rename_decision is RenameDecision.APPLY_RENAME
    assert all(group.selected_release is None and group.effective_track_mapping is None for group in split.groups)

    merged = merge_session_groups(split, tuple(group.group.group_id for group in split.groups))
    assert by_file(merged)[first.file_id].reviews == retained.reviews
    assert by_file(merged)[first.file_id].change_set.rename_decision is RenameDecision.APPLY_RENAME

    # Undo traverses the preserved rename and field actions even after ownership
    # changes twice. It restores local data, never obsolete track associations.
    assert review_undo_targets(merged)
    undone_rename = undo_last_review_action(merged, settings)
    assert by_file(undone_rename)[first.file_id].change_set.rename_decision is RenameDecision.KEEP_FILENAME
    undone_field = undo_last_review_action(undone_rename, settings)
    assert by_file(undone_field)[first.file_id].change_set.final_metadata.title == "Overture"
    assert not review_undo_targets(undone_field)


def test_merge_revalidates_candidate_choices_without_undo_resurrecting_proposals():
    state = make_selected_session()
    source = state.groups[0].group.files[0]
    settings = RenameSettings()
    state = apply_field_decision(
        state, "album", source.file_id, MetadataField.TITLE, FieldDecisionKind.USE_PROPOSAL,
        settings, proposal_index=1,
    )
    other = make_local_session(make_source("other.flac")).groups[0]
    other = GroupState(replace(other.group, group_id="other"))
    state = replace(state, groups=(*state.groups, other))

    merged = merge_session_groups(state, ("album", "other"))
    retained = by_file(merged)[source.file_id]
    title = next(review for review in retained.reviews if review.field is MetadataField.TITLE)

    assert title.decision is FieldDecisionKind.UNRESOLVED
    assert title.requires_review
    assert ReviewReasonCode.CANDIDATE_DEPENDENCY_CHANGED in title.reason_codes
    assert not title.proposals
    assert merged.groups[0].effective_track_mapping is None
    undone = undo_last_review_action(merged, settings)
    assert all(not review.proposals for item in by_file(undone).values() for review in item.reviews)


def test_merge_with_stale_group_rejects_before_discarding_healthy_manual_decisions():
    state = make_local_session(make_source("healthy.flac"))
    file_id = state.groups[0].group.files[0].file_id
    state = apply_field_decision(
        state, "album", file_id, MetadataField.TITLE, FieldDecisionKind.USE_MANUAL,
        RenameSettings(), manual_value="Retain this review",
    )
    stale = make_local_session(make_source("stale.flac")).groups[0]
    stale = GroupState(replace(stale.group, group_id="stale"), requires_rescan=True)
    state = replace(state, groups=(*state.groups, stale))

    with pytest.raises(ValueError, match="[Rr]escan.*merg"):
        merge_session_groups(state, ("album", "stale"))

    assert by_file(state)[file_id].change_set.final_metadata.title == "Retain this review"


@pytest.mark.parametrize("decision,value", [
    (FieldDecisionKind.USE_MANUAL, "Reviewed title"),
    (FieldDecisionKind.CLEAR, None),
    (FieldDecisionKind.KEEP_EXISTING, None),
])
def test_disc_hint_change_preserves_independent_review_and_filename_intent(decision, value):
    state = make_selected_session()
    source = state.groups[0].group.files[0]
    settings = RenameSettings()
    state = apply_field_decision(
        state, "album", source.file_id, MetadataField.TITLE, decision, settings, manual_value=value,
    )
    state = set_rename_decision(state, "album", (source.file_id,), RenameDecision.APPLY_RENAME, settings)

    changed = set_disc_override(state, "album", 2)
    retained = by_file(changed)[source.file_id]
    title = next(review for review in retained.reviews if review.field is MetadataField.TITLE)

    assert title.decision is decision
    assert title.decision_origin is DecisionOrigin.USER
    assert title.manual_value == value
    assert retained.change_set.rename_decision is RenameDecision.APPLY_RENAME
    assert changed.groups[0].disc_number_override == 2
    assert changed.groups[0].selected_release is None
    assert changed.groups[0].effective_track_mapping is None
    assert all(not review.proposals for review in retained.reviews)

    # Independent actions remain undoable after the matching evidence changes.
    # Undo must operate on local projections and cannot restore stale proposals.
    undone = undo_last_review_action(changed, settings)
    assert by_file(undone)[source.file_id].change_set.rename_decision is RenameDecision.KEEP_FILENAME
    assert all(not review.proposals for item in by_file(undone).values() for review in item.reviews)


def test_disc_hint_change_marks_candidate_approval_for_review_again():
    state = make_selected_session()
    source = state.groups[0].group.files[0]
    state = apply_field_decision(
        state, "album", source.file_id, MetadataField.TITLE, FieldDecisionKind.USE_PROPOSAL,
        RenameSettings(), proposal_index=1,
    )

    changed = set_disc_override(state, "album", 2)
    retained = by_file(changed)[source.file_id]
    title = next(review for review in retained.reviews if review.field is MetadataField.TITLE)

    assert title.decision is FieldDecisionKind.UNRESOLVED
    assert title.requires_review
    assert ReviewReasonCode.CANDIDATE_DEPENDENCY_CHANGED in title.reason_codes
    assert not title.proposals
    assert not review_undo_targets(changed)
