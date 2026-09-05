"""Explicit review choices survive changes that do not invalidate their evidence."""

# Identical proposal text can come from a different track. Preservation therefore
# depends on the selected association as well as the visible value.


from dataclasses import replace

import pytest

from metadata_polisher.application.lookup import GroupLookupResult, SelectedMetadataResult
from metadata_polisher.application.review import build_selected_file_results, set_manual_track_assignment
from metadata_polisher.domain.metadata import MetadataField
from metadata_polisher.domain.review import DecisionOrigin, FieldDecisionKind
from metadata_polisher.infrastructure.settings import RenameSettings
from metadata_polisher.matching.release_scoring import (
    MatchClassification,
    build_local_release_evidence,
    rank_release_candidates,
)
from metadata_polisher.matching.track_mapping import map_tracks
from metadata_polisher.session.lookup_editing import set_search_query_override
from metadata_polisher.session.mapping_editing import apply_manual_track_mapping
from metadata_polisher.session.review_editing import apply_field_decision
from metadata_polisher.session.state import (
    OperationKind,
    ReleaseSelectionState,
    ResultApplicationStatus,
    ReviewedFileState,
    apply_group_lookup_result,
    begin_operation,
)
from tests.unit.session.test_lookup_editing import make_query, make_selected_session, title_review


@pytest.mark.parametrize(
    "decision", (FieldDecisionKind.USE_MANUAL, FieldDecisionKind.KEEP_EXISTING, FieldDecisionKind.CLEAR),
)
def test_search_terms_preserve_explicit_local_review_decisions(decision):
    state = make_selected_session()
    file_id = state.groups[0].group.files[0].file_id
    state = apply_field_decision(
        state, "album", file_id, MetadataField.TITLE, decision, RenameSettings(),
        manual_value="My reviewed title" if decision is FieldDecisionKind.USE_MANUAL else None,
    )
    before = state.groups[0].reviewed_files[0].change_set.final_metadata.title
    changed = set_search_query_override(state, "album", make_query("New search terms"))

    assert title_review(changed).decision is decision
    assert title_review(changed).decision_origin is DecisionOrigin.USER
    assert not title_review(changed).requires_review
    # Search terms invalidate the old lookup evidence. Only the explicit Title
    # decision is preserved; an automatic Disc addition is not a user approval.
    assert changed.groups[0].reviewed_files[0].change_set.final_metadata.title == before
    assert changed.groups[0].search_query_override == make_query("New search terms")


@pytest.mark.parametrize("decision", (FieldDecisionKind.USE_MANUAL, FieldDecisionKind.KEEP_EXISTING))
@pytest.mark.parametrize("with_selected_details", (False, True))
def test_new_lookup_preserves_explicit_manual_and_keep_decisions(decision, with_selected_details):
    state = make_selected_session()
    file_id = state.groups[0].group.files[0].file_id
    state = apply_field_decision(
        state, "album", file_id, MetadataField.TITLE, decision, RenameSettings(),
        manual_value="My reviewed title" if decision is FieldDecisionKind.USE_MANUAL else None,
    )
    group = state.groups[0]
    before = group.reviewed_files[0].change_set.final_metadata.title
    operation_id = "LOOKUP-REPLACEMENT"
    coordinated = group.selected_release.candidate
    coordinated = replace(coordinated, provenance=tuple(
        replace(item, operation_id=operation_id) for item in coordinated.provenance
    ))
    lookup = replace(group.candidate_lookup.lookup_result, candidates=(coordinated,))
    candidates = replace(group.candidate_lookup, lookup_result=lookup)
    selected = None

    if with_selected_details:
        # The worker starts from source data, so its automatic reviews must be
        # reconciled with explicit UI decisions instead of replacing them whole.
        reviewed = build_selected_file_results(
            group.group.files, coordinated, medium_index=0, mapping_result=group.automatic_track_mapping,
            release_classification=MatchClassification.HIGH, preferred_language="auto",
        )
        selected = SelectedMetadataResult(
            candidates, group.selected_release.identity, coordinated, group.automatic_track_mapping, reviewed, (),
        )

    result = GroupLookupResult(
        operation_id, state.revision, state.library_revision, "album", group.revision, candidates, selected,
    )
    running = begin_operation(state, operation_id, OperationKind.LOOKUP, ("album",))
    reduced = apply_group_lookup_result(running, result)

    assert reduced.status is ResultApplicationStatus.APPLIED
    assert title_review(reduced.state).decision is decision
    assert title_review(reduced.state).decision_origin is DecisionOrigin.USER
    assert not title_review(reduced.state).requires_review
    assert reduced.state.groups[0].reviewed_files[0].change_set.final_metadata.title == before


def test_remap_to_different_track_revalidates_identical_proposal_values():
    # Use unchanged visible text across a changed association: equality of strings
    # alone must not carry a previous candidate approval onto a different track.
    state = make_selected_session()
    group = state.groups[0]
    original = group.selected_release.candidate.candidate
    first_track = original.media[0].tracks[0]
    candidate = replace(original, media=(replace(original.media[0], tracks=(
        first_track, replace(first_track, track_number=2),
    )),))
    coordinated = replace(group.selected_release.candidate, candidate=candidate)
    lookup = replace(group.lookup_result, candidates=(coordinated,))
    ranking = rank_release_candidates(build_local_release_evidence(group.group), (candidate,))
    candidates = replace(group.candidate_lookup, lookup_result=lookup, release_ranking=ranking)
    automatic = map_tracks(group.group.files, candidate, selected_medium_index=0)
    file_id = group.group.files[0].file_id
    original_assignment = set_manual_track_assignment(
        group.group.files, candidate, automatic, local_file_id=file_id, provider_track_index=0,
    )
    results = build_selected_file_results(
        group.group.files, coordinated, medium_index=0, mapping_result=original_assignment,
        release_classification=MatchClassification.HIGH, preferred_language="auto",
    )
    group = replace(
        group, lookup_result=lookup, release_ranking=ranking, candidate_lookup=candidates,
        selected_release=ReleaseSelectionState(coordinated, 0), automatic_track_mapping=original_assignment,
        selected_metadata=None, reviewed_files=tuple(
            ReviewedFileState(item.file_id, item.proposals, item.reviews, item.track_mapping_resolved, item.change_set)
            for item in results
        ),
    )
    state = replace(state, groups=(group,))
    english_index = next(index for index, item in enumerate(title_review(state).proposals) if item.value == "Overture")
    state = apply_field_decision(
        state, "album", file_id, MetadataField.TITLE, FieldDecisionKind.USE_PROPOSAL, RenameSettings(),
        proposal_index=english_index,
    )
    assert title_review(state).decision_origin is DecisionOrigin.USER
    changed_assignment = set_manual_track_assignment(
        group.group.files, candidate, original_assignment, local_file_id=file_id, provider_track_index=1,
    )
    changed = apply_manual_track_mapping(state, "album", changed_assignment, RenameSettings())

    # Equal display text and release provenance do not prove that the user's
    # choice applies to a newly associated track in the same release.
    review = title_review(changed)
    assert review.decision_origin is DecisionOrigin.DEFAULT
    assert review.requires_review
    assert changed.groups[0].effective_track_mapping.mappings[0].provider_track_index == 1
