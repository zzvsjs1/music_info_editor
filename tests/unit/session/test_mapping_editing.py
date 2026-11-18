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
from metadata_polisher.domain.review import DecisionOrigin, FieldConfidence, FieldDecisionKind
from metadata_polisher.infrastructure.settings import RenameSettings
from metadata_polisher.matching.release_scoring import MatchClassification
from metadata_polisher.matching.track_mapping import map_tracks
from metadata_polisher.session.group_editing import set_disc_override
from metadata_polisher.session.lookup_editing import set_search_query_override
from metadata_polisher.session.mapping_editing import apply_manual_track_mapping
from metadata_polisher.session.review_editing import (
    BatchReviewAction,
    BatchReviewCommand,
    apply_batch_review,
    apply_field_decision,
    set_rename_decision,
    undo_last_review_action,
)
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


@pytest.mark.parametrize("command", ("language", "safe_additions"))
def test_reassigned_artist_needs_explicit_approval_after_other_review_commands(command):
    from metadata_polisher.session.lookup_editing import change_language

    state, file_id, mapping = session_with_alternate_track()
    settings = RenameSettings()
    approved = apply_field_decision(state, "album", file_id, MetadataField.ARTISTS,
                                    FieldDecisionKind.USE_PROPOSAL, settings)
    remapped = apply_manual_track_mapping(approved, "album", mapping, settings)
    if command == "language":
        changed = change_language(remapped, "album", "ja", settings)
    else:
        changed = apply_batch_review(remapped, BatchReviewCommand(
            (file_id,), (MetadataField.ARTISTS,), remapped.revision, BatchReviewAction.ACCEPT_SAFE_ADDITIONS,
        ), settings).state

    assert changed.groups[0].reviewed_files[0].change_set.final_metadata.artists == ()
    selected = apply_field_decision(changed, "album", file_id, MetadataField.ARTISTS,
                                    FieldDecisionKind.USE_PROPOSAL, settings)
    assert selected.groups[0].reviewed_files[0].change_set.final_metadata.artists == ("ゆき",)


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


def test_undo_manual_title_after_mapping_restores_current_track_evidence():
    state = partial_session()
    file_id = state.groups[0].group.files[0].file_id
    settings = RenameSettings()
    manual = apply_field_decision(state, "album", file_id, MetadataField.TITLE,
                                  FieldDecisionKind.USE_MANUAL, settings, manual_value="Independent title")
    remapped = apply_manual_track_mapping(manual, "album", resolved_mapping(manual), settings)
    undone = undo_last_review_action(remapped, settings)
    expected = apply_manual_track_mapping(state, "album", resolved_mapping(state), settings)

    assert undone.groups[0].reviewed_files == expected.groups[0].reviewed_files
    assert undone.groups[0].manual_track_mapping == remapped.groups[0].manual_track_mapping


def session_with_alternate_track():
    state = make_selected_session()
    group = state.groups[0]
    selected = group.selected_release.candidate
    medium = selected.candidate.media[0]
    first = replace(medium.tracks[0], artists=("あき",))
    second = replace(first, track_number=2, artists=("ゆき",))
    candidate = replace(selected.candidate, media=(replace(medium, tracks=(first, second)),))
    # The enriched selection retains the ranked release's identity and supplies
    # a second provider track. The local file has no existing artist tag.
    selection = replace(group.selected_release, candidate=replace(selected, candidate=candidate))
    mapping = map_tracks(group.group.files, candidate, selected_medium_index=0)
    group = replace(group, selected_release=selection, selected_metadata=None,
                    automatic_track_mapping=mapping, reviewed_files=())
    state = replace(state, groups=(group,))
    file_id = group.group.files[0].file_id
    first_assignment = set_manual_track_assignment(group.group.files, candidate, mapping,
                                                   local_file_id=file_id, provider_track_index=0)
    second_assignment = set_manual_track_assignment(group.group.files, candidate, first_assignment,
                                                    local_file_id=file_id, provider_track_index=1)
    return apply_manual_track_mapping(state, "album", first_assignment, RenameSettings()), file_id, second_assignment


@pytest.mark.parametrize("include_disc", (False, True))
def test_undo_after_reassignment_cannot_approve_the_new_tracks_artist(include_disc):
    state, file_id, mapping = session_with_alternate_track()
    settings = RenameSettings()
    fields = (MetadataField.ARTISTS, MetadataField.DISC) if include_disc else (MetadataField.ARTISTS,)
    approved = apply_batch_review(state, BatchReviewCommand(
        (file_id,), fields, state.revision, BatchReviewAction.USE_CANDIDATE,
    ), settings).state
    assert approved.groups[0].reviewed_files[0].change_set.final_metadata.artists == ("あき",)
    remapped = apply_manual_track_mapping(approved, "album", mapping, settings)
    undone = undo_last_review_action(remapped, settings)
    reviewed = undone.groups[0].reviewed_files[0]
    artist = next(field for field in reviewed.reviews if field.field is MetadataField.ARTISTS)
    assert artist.proposals[0].value == ("ゆき",)
    assert artist.decision is FieldDecisionKind.UNRESOLVED
    assert reviewed.change_set.final_metadata.artists == ()
    if include_disc:
        disc = next(field for field in reviewed.reviews if field.field is MetadataField.DISC)
        assert disc.decision_origin is DecisionOrigin.DEFAULT
    else:
        assert undone is remapped


def test_repeated_mapping_and_undo_preserve_the_need_to_review_a_reassigned_field():
    state, file_id, mapping = session_with_alternate_track()
    settings = RenameSettings()
    approved = apply_field_decision(state, "album", file_id, MetadataField.ARTISTS,
                                    FieldDecisionKind.USE_PROPOSAL, settings)
    remapped = apply_manual_track_mapping(approved, "album", mapping, settings)
    edited = apply_field_decision(remapped, "album", file_id, MetadataField.ARTISTS,
                                  FieldDecisionKind.USE_MANUAL, settings, manual_value=("My artist",))
    repeated = apply_manual_track_mapping(edited, "album", mapping, settings)
    undone = undo_last_review_action(repeated, settings)
    assert undone.groups[0].reviewed_files[0].change_set.final_metadata.artists == ()
    assert not undone.review_undo
    repeated = apply_manual_track_mapping(undone, "album", mapping, settings)
    assert repeated.groups[0].reviewed_files[0].change_set.final_metadata.artists == ()


def test_undo_past_an_independent_edit_preserves_the_reassigned_fields_review_requirement():
    state, file_id, mapping = session_with_alternate_track()
    settings = RenameSettings()
    manual = apply_field_decision(state, "album", file_id, MetadataField.ARTISTS,
                                  FieldDecisionKind.USE_MANUAL, settings, manual_value=("My artist",))
    approved = apply_field_decision(manual, "album", file_id, MetadataField.ARTISTS,
                                    FieldDecisionKind.USE_PROPOSAL, settings)
    remapped = apply_manual_track_mapping(approved, "album", mapping, settings)
    restored_manual = undo_last_review_action(remapped, settings)
    assert restored_manual.groups[0].reviewed_files[0].change_set.final_metadata.artists == ("My artist",)
    restored_default = undo_last_review_action(restored_manual, settings)
    reviewed = restored_default.groups[0].reviewed_files[0]
    assert next(field for field in reviewed.reviews if field.field is MetadataField.ARTISTS).requires_review
    assert reviewed.change_set.final_metadata.artists == ()
    assert not restored_default.review_undo


@pytest.mark.parametrize("invalidate", [
    lambda state: set_disc_override(state, "album", 2),
    lambda state: set_search_query_override(
        state, "album", replace(state.groups[0].lookup_result.queries[0], album="A different album"),
    ),
])
def test_lookup_invalidation_clears_manual_mapping(invalidate):
    state = partial_session()
    state = apply_manual_track_mapping(state, "album", resolved_mapping(state), RenameSettings())
    changed = invalidate(state)
    assert changed.groups[0].manual_track_mapping is None
    assert changed.groups[0].effective_track_mapping is None
