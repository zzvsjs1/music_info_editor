"""Lookup evidence changes retain safe review Undo over the current evidence."""

from dataclasses import replace

import pytest

from metadata_polisher.application.apply import ApplyGroupOutcome
from metadata_polisher.domain.metadata import MetadataField
from metadata_polisher.domain.review import DecisionOrigin, FieldDecisionKind, ReviewReasonCode
from metadata_polisher.infrastructure.settings import RenameSettings
from metadata_polisher.session.lookup_editing import change_language, set_search_query_override
from metadata_polisher.session.review_editing import (
    apply_field_decision,
    review_undo_targets,
    undo_last_review_action,
)
from metadata_polisher.session.state import (
    OperationKind,
    apply_batch_result,
    begin_operation,
    finish_operation,
    mark_groups_requires_rescan,
)
from tests.unit.session.test_apply_reducer import make_result
from tests.unit.session.test_apply_refresh import album_write_outcome
from tests.unit.session.test_lookup_editing import make_query, make_selected_session, title_review
from tests.unit.session.test_review_editing import make_local_session, make_source


def change_evidence(state, operation):
    if operation == "language":
        return change_language(state, "album", "en", RenameSettings())

    return set_search_query_override(state, "album", make_query("Different search terms"), RenameSettings())


def choose_title(state, decision, value=None, *, proposal_index=0):
    source = state.groups[0].group.files[0]

    return apply_field_decision(
        state, "album", source.file_id, MetadataField.TITLE, decision, RenameSettings(),
        manual_value=value, proposal_index=proposal_index,
    )


@pytest.mark.parametrize("operation", ("language", "search"))
@pytest.mark.parametrize("decision,value", (
    (FieldDecisionKind.USE_MANUAL, "Reviewed title"),
    (FieldDecisionKind.KEEP_EXISTING, None),
    (FieldDecisionKind.CLEAR, None),
))
def test_independent_decision_remains_undoable_after_evidence_change(operation, decision, value):
    state = choose_title(make_selected_session(), decision, value)
    changed = change_evidence(state, operation)
    current_proposals = title_review(changed).proposals

    assert title_review(changed).decision is decision
    assert title_review(changed).decision_origin is DecisionOrigin.USER
    assert review_undo_targets(changed)

    undone = undo_last_review_action(changed, RenameSettings())
    restored = title_review(undone)

    # Undo reverses the human choice, while keeping the newer lookup context.
    # It must neither revert the selected language nor repopulate old proposals.
    assert restored.decision_origin is DecisionOrigin.DEFAULT
    assert restored.proposals == current_proposals
    assert undone.groups[0].reviewed_files[0].change_set.final_metadata.title == "序曲"
    assert undone.groups[0].language_override == changed.groups[0].language_override
    assert undone.groups[0].search_query_override == changed.groups[0].search_query_override
    assert undone.groups[0].selected_release == changed.groups[0].selected_release
    assert not review_undo_targets(undone)

    if operation == "language":
        assert restored.proposals[0].value == "Overture"
    else:
        assert not restored.proposals
        assert undone.groups[0].effective_track_mapping is None


@pytest.mark.parametrize("operation", ("language", "search"))
def test_sequential_manual_actions_remain_separate_undo_steps(operation):
    state = make_selected_session()

    for value in ("First reviewed title", "Second reviewed title"):
        state = choose_title(state, FieldDecisionKind.USE_MANUAL, value)

    state = choose_title(state, FieldDecisionKind.CLEAR)
    changed = change_evidence(state, operation)
    evidence = title_review(changed).proposals

    for expected in ("Second reviewed title", "First reviewed title", "序曲"):
        assert review_undo_targets(changed)
        changed = undo_last_review_action(changed, RenameSettings())
        assert changed.groups[0].reviewed_files[0].change_set.final_metadata.title == expected
        assert title_review(changed).proposals == evidence

    assert not review_undo_targets(changed)


def test_search_reset_undoes_manual_choice_without_restoring_prior_candidate_approval():
    state = choose_title(make_selected_session(), FieldDecisionKind.USE_PROPOSAL, proposal_index=1)
    state = choose_title(state, FieldDecisionKind.USE_MANUAL, "Reviewed title")
    changed = change_evidence(state, "search")

    assert review_undo_targets(changed)
    undone = undo_last_review_action(changed, RenameSettings())
    restored = title_review(undone)

    assert restored.decision is FieldDecisionKind.UNRESOLVED
    assert restored.requires_review
    assert ReviewReasonCode.CANDIDATE_DEPENDENCY_CHANGED in restored.reason_codes
    assert not restored.proposals
    assert restored.selected_proposal is None
    assert undone.groups[0].selected_release is None
    assert undone.groups[0].effective_track_mapping is None
    assert not review_undo_targets(undone)


def test_search_reset_does_not_make_candidate_only_history_usable():
    state = choose_title(make_selected_session(), FieldDecisionKind.USE_PROPOSAL, proposal_index=1)
    changed = change_evidence(state, "search")

    assert not review_undo_targets(changed)
    undone = undo_last_review_action(changed, RenameSettings())
    assert not title_review(undone).proposals
    assert title_review(undone).selected_proposal is None


def test_language_undo_keeps_current_ranking_when_reverting_a_candidate_choice():
    state = choose_title(make_selected_session(), FieldDecisionKind.USE_PROPOSAL, proposal_index=1)
    changed = change_evidence(state, "language")

    assert review_undo_targets(changed)
    undone = undo_last_review_action(changed, RenameSettings())
    assert title_review(undone).decision_origin is DecisionOrigin.DEFAULT
    assert title_review(undone).proposals == title_review(changed).proposals
    assert title_review(undone).proposals[0].value == "Overture"
    assert undone.groups[0].language_override == "en"


@pytest.mark.parametrize("operation", ("language", "search"))
def test_evidence_changes_leave_unrelated_history_usable_without_reviving_stale_files(operation):
    state = choose_title(make_selected_session(), FieldDecisionKind.USE_MANUAL, "Reviewed title")
    other_source = make_source("other.flac")
    other = make_local_session(other_source).groups[0]
    other = replace(other, group=replace(other.group, group_id="other"))
    state = replace(state, groups=(*state.groups, other))
    state = apply_field_decision(
        state, "other", other_source.file_id, MetadataField.TITLE, FieldDecisionKind.USE_MANUAL,
        RenameSettings(), manual_value="Other pending title",
    )
    state = mark_groups_requires_rescan(state, ("other",))
    changed = change_evidence(state, operation)

    targets = review_undo_targets(changed)
    assert tuple(item.source.file_id for item in targets) == (state.groups[0].group.files[0].file_id,)

    undone = undo_last_review_action(changed, RenameSettings())
    assert undone.groups[1] is changed.groups[1]
    assert undone.groups[1].requires_rescan
    assert not undone.groups[1].reviewed_files
    assert not review_undo_targets(undone)


@pytest.mark.parametrize("operation", ("language", "search"))
def test_evidence_changes_cannot_recreate_undo_consumed_by_a_verified_write(operation):
    source = make_source()
    state = make_local_session(source)
    state = apply_field_decision(
        state, "album", source.file_id, MetadataField.ALBUM, FieldDecisionKind.USE_MANUAL,
        RenameSettings(), manual_value="Written album",
    )
    running = begin_operation(state, "APPLY-1", OperationKind.APPLY, ("album",))
    outcome = album_write_outcome(running, source)
    result = make_result(running, (ApplyGroupOutcome("album", state.groups[0].revision, (outcome,)),))
    refreshed = finish_operation(apply_batch_result(running, result).state, "APPLY-1")

    assert not refreshed.review_undo
    changed = change_evidence(refreshed, operation)
    undone = undo_last_review_action(changed, RenameSettings())

    assert not review_undo_targets(changed)
    assert not undone.review_undo
    assert undone.groups[0].group.files[0].read_result.metadata.album == "Written album"
    assert undone.written_files == refreshed.written_files
