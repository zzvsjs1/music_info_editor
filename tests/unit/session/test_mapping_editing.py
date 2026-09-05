# Start with incomplete automatic evidence, then apply a human pairing to test
# which track-dependent choices need rebuilding and which local decisions survive.

from dataclasses import replace

import pytest

from metadata_polisher.application.changes import ChangeIssueCode, RenameDecision
from metadata_polisher.application.review import (
    build_field_review_state,
    build_selected_file_results,
    set_manual_track_assignment,
)
from metadata_polisher.domain.metadata import MetadataField
from metadata_polisher.domain.review import FieldConfidence, FieldDecisionKind
from metadata_polisher.infrastructure.settings import RenameSettings
from metadata_polisher.matching.release_scoring import MatchClassification
from metadata_polisher.session.group_editing import set_disc_override
from metadata_polisher.session.lookup_editing import set_search_query_override
from metadata_polisher.session.mapping_editing import apply_manual_track_mapping
from metadata_polisher.session.review_editing import apply_field_decision, set_rename_decision
from metadata_polisher.session.state import ReviewedFileState
from tests.unit.session.test_lookup_editing import make_selected_session


def partial_session():
    # Leave both sides explicitly unmatched while retaining the automatic result.
    # Manual review can then resolve the pairing without rewriting that evidence.
    state = make_selected_session()
    group = state.groups[0]
    file_id = group.group.files[0].file_id
    mapping = replace(group.automatic_track_mapping, mappings=(), unmatched_local_file_ids=(file_id,),
                      unmatched_provider_indexes=(0,), classification=MatchClassification.REVIEW)
    results = build_selected_file_results(
        group.group.files, group.selected_release.candidate, medium_index=0, mapping_result=mapping,
        release_classification=MatchClassification.HIGH, preferred_language="auto",
    )
    reviews = tuple(ReviewedFileState(item.file_id, item.proposals, item.reviews,
                                     item.track_mapping_resolved, item.change_set) for item in results)
    group = replace(group, automatic_track_mapping=mapping, reviewed_files=reviews, selected_metadata=None)
    state = replace(state, groups=(group,))
    return apply_field_decision(state, "album", file_id, MetadataField.DISC,
                                FieldDecisionKind.USE_PROPOSAL, RenameSettings())


def resolved_mapping(state):
    group = state.groups[0]
    return set_manual_track_assignment(
        group.group.files, group.selected_release.candidate.candidate,
        group.automatic_track_mapping, local_file_id=group.group.files[0].file_id, provider_track_index=0,
    )


def test_manual_mapping_resolves_block_and_retains_original_automatic_evidence():
    state = partial_session()
    original = state.groups[0]
    assert not original.reviewed_files[0].track_mapping_resolved
    assert not any(item.field is MetadataField.TITLE for item in original.reviewed_files[0].proposals)
    manual = resolved_mapping(state)
    changed = apply_manual_track_mapping(state, "album", manual, RenameSettings())
    group = changed.groups[0]
    assert group.automatic_track_mapping is original.automatic_track_mapping
    assert group.manual_track_mapping is manual
    assert group.effective_track_mapping is manual
    assert group.reviewed_files[0].track_mapping_resolved
    assert ChangeIssueCode.UNRESOLVED_TRACK_MAPPING not in {
        item.code for item in group.reviewed_files[0].change_set.validation.issues
    }
    assert any(item.field is MetadataField.TITLE for item in group.reviewed_files[0].proposals)
    assert group.candidate_lookup is original.candidate_lookup
    assert group.group is original.group
    assert changed.revision == state.revision + 1


def test_mapping_rebuild_preserves_manual_field_and_rename_decisions():
    state = partial_session()
    file_id = state.groups[0].group.files[0].file_id
    state = apply_field_decision(state, "album", file_id, MetadataField.TITLE,
                                 FieldDecisionKind.USE_MANUAL, RenameSettings(), manual_value="My title")
    state = set_rename_decision(state, "album", (file_id,), RenameDecision.APPLY_RENAME, RenameSettings())
    changed = apply_manual_track_mapping(state, "album", resolved_mapping(state), RenameSettings())
    change = changed.groups[0].reviewed_files[0].change_set
    assert change.final_metadata.title == "My title"
    assert change.rename_decision is RenameDecision.APPLY_RENAME
    assert "My title" in change.rename_preview.new_path.name


def test_same_provider_choice_survives_manual_mapping_confidence_change():
    state = make_selected_session()
    group = state.groups[0]
    reviewed = group.reviewed_files[0]
    proposals = tuple(replace(item, confidence=FieldConfidence.LOW) for item in reviewed.proposals)
    reviews = tuple(build_field_review_state(
        field=old.field, read_state=old.read_state, existing_value=old.existing_value,
        proposals=tuple(item for item in proposals if item.field is old.field), preferred_language="auto",
    ) for old in reviewed.reviews)
    reviewed = replace(reviewed, proposals=proposals, reviews=reviews, change_set=None)
    group = replace(group, reviewed_files=(reviewed,), selected_metadata=None)
    state = replace(state, groups=(group,))
    title = next(item for item in reviews if item.field is MetadataField.TITLE)
    selected_index = next(index for index, item in enumerate(title.proposals) if item.value == "Overture")
    state = apply_field_decision(state, "album", reviewed.file_id, MetadataField.TITLE,
                                 FieldDecisionKind.USE_PROPOSAL, RenameSettings(), proposal_index=selected_index)
    assert state.groups[0].reviewed_files[0].change_set.final_metadata.title == "Overture"

    changed = apply_manual_track_mapping(state, "album", resolved_mapping(state), RenameSettings())
    assert changed.groups[0].reviewed_files[0].change_set.final_metadata.title == "Overture"


@pytest.mark.parametrize("invalidate", [
    lambda state: set_disc_override(state, "album", 2),
    lambda state: set_search_query_override(state, "album", state.groups[0].lookup_result.queries[0]),
])
def test_lookup_invalidation_clears_manual_mapping(invalidate):
    state = partial_session()
    state = apply_manual_track_mapping(state, "album", resolved_mapping(state), RenameSettings())
    changed = invalidate(state)
    assert changed.groups[0].manual_track_mapping is None
    assert changed.groups[0].effective_track_mapping is None
