# Review transforms are pure: tag decisions and rename intent produce new plans.
# Sibling collisions must be recalculated after edits rather than copied from old plans.

from dataclasses import replace
from pathlib import Path

import pytest

from metadata_polisher.application.changes import (
    ChangeIssueCode,
    ChangeSetStatus,
    ChangeValidationFacts,
    FileChangeSet,
    RenameDecision,
    build_change_set,
)
from metadata_polisher.application.lookup import CandidateLookupResult, LookupSearchResult, SelectedMetadataResult
from metadata_polisher.application.review import ReviewedFileResult, build_field_review_state
from metadata_polisher.domain.matching import (
    LocalisedText,
    MetadataProvenance,
    ProviderTrack,
    ReleaseCandidate,
    ReleaseMedium,
    ReleaseSearchQuery,
)
from metadata_polisher.domain.media import LocalMediaFile, MediaReadResult, StreamInfo
from metadata_polisher.domain.metadata import FieldReadState, MetadataField, MetadataSnapshot, Position
from metadata_polisher.domain.review import (
    DecisionOrigin,
    FieldConfidence,
    FieldDecisionKind,
    FieldProposal,
    FieldReviewState,
    FieldValue,
    ReviewReasonCode,
)
from metadata_polisher.infrastructure.settings import RenameSettings
from metadata_polisher.matching.release_scoring import build_local_release_evidence, rank_release_candidates
from metadata_polisher.matching.track_mapping import map_tracks
from metadata_polisher.providers.coordinator import CoordinatedCandidate
from metadata_polisher.scanner.grouping import AlbumGroup, GroupingReason
from metadata_polisher.session.lookup_editing import change_language
from metadata_polisher.session.review_editing import (
    BatchReviewAction,
    BatchReviewCommand,
    accept_safe_additions,
    apply_batch_review,
    apply_field_decision,
    set_rename_decision,
)
from metadata_polisher.session.state import (
    GroupSelection,
    GroupState,
    OperationKind,
    ReleaseSelectionState,
    ReviewedFileState,
    SessionState,
    begin_operation,
)


def make_source(name: str = "source.flac", metadata: MetadataSnapshot | None = None) -> LocalMediaFile:
    metadata = metadata or MetadataSnapshot(title="Overture", album="Album", track=Position(1))
    states = {
        field: FieldReadState.PRESENT
        if getattr(metadata, field.value) not in (None, (), Position())
        else FieldReadState.MISSING
        for field in MetadataField
    }

    return LocalMediaFile(
        Path("library/Album") / name,
        "flac",
        MediaReadResult(metadata, states, StreamInfo(180.0, 48_000, 2, 24, "FLAC")),
    )


def make_local_session(*sources: LocalMediaFile) -> SessionState:
    group = AlbumGroup("album", sources or (make_source(),), "Album", GroupingReason.DIRECTORY_ALBUM_CONSISTENT)

    return SessionState(Path("library"), groups=(GroupState(group),), selection=GroupSelection("album"))


def proposal(
    field: MetadataField,
    value: FieldValue,
    confidence: FieldConfidence = FieldConfidence.HIGH,
    record: str = "release",
) -> FieldProposal:
    return FieldProposal(
        field,
        value,
        confidence,
        MetadataProvenance("musicbrainz", "musicbrainz", record, None, None, "LOOKUP-0001"),
    )


def make_selected_session(
    proposals: tuple[FieldProposal, ...],
    *,
    metadata: MetadataSnapshot | None = None,
    mapping_resolved: bool = True,
) -> SessionState:
    state = make_local_session(make_source(metadata=metadata))
    group = state.groups[0].group
    source = group.files[0]
    candidate = ReleaseCandidate(
        "musicbrainz", "musicbrainz", "release", (LocalisedText("Album", None, None),), (), None,
        (ReleaseMedium(1, None, (ProviderTrack(1, (LocalisedText("Overture", None, None),), (), (), 180.0),)),),
        None,
    )
    provenance = MetadataProvenance("musicbrainz", "musicbrainz", "release", None, None, "LOOKUP-0001")
    coordinated = CoordinatedCandidate(candidate, (provenance,))
    query = ReleaseSearchQuery("Album", (), None, None, 1, ("Overture",))
    lookup = LookupSearchResult("album", (query,), (coordinated,), ())
    ranking = rank_release_candidates(build_local_release_evidence(group), (candidate,))
    candidates = CandidateLookupResult(lookup, ranking, (), ())
    mapping = map_tracks(group.files, candidate, selected_medium_index=0)
    reviews = tuple(
        build_field_review_state(
            field=field,
            read_state=source.read_result.field_states[field],
            existing_value=(
                getattr(source.read_result.metadata, field.value)
                if source.read_result.field_states[field] is FieldReadState.PRESENT
                else None
            ),
            proposals=tuple(item for item in proposals if item.field is field),
        )
        for field in MetadataField
    )
    changes = build_change_set(
        source, reviews, RenameDecision.KEEP_FILENAME,
        validation=ChangeValidationFacts(track_mapping_resolved=mapping_resolved),
    )
    result = ReviewedFileResult(source.file_id, proposals, reviews, mapping_resolved, changes)
    selected = SelectedMetadataResult(candidates, ranking.identities[0], coordinated, mapping, (result,), ())
    group_state = GroupState(
        group,
        lookup_result=lookup,
        release_ranking=ranking,
        candidate_lookup=candidates,
        selected_release=ReleaseSelectionState(coordinated, 0),
        automatic_track_mapping=mapping,
        reviewed_files=(ReviewedFileState(source.file_id, proposals, reviews, mapping_resolved, changes),),
        selected_metadata=selected,
    )

    return replace(state, groups=(group_state,))


def field_review(state: SessionState, field: MetadataField, index: int = 0) -> FieldReviewState:
    return next(item for item in state.groups[0].reviewed_files[index].reviews if item.field is field)


def change_set(state: SessionState, index: int = 0) -> FileChangeSet:
    changes = state.groups[0].reviewed_files[index].change_set
    assert changes is not None

    return changes


@pytest.mark.parametrize(
    ("decision", "expected", "reason"),
    (
        (FieldDecisionKind.KEEP_EXISTING, "Overture", ReviewReasonCode.KEEP_EXISTING_SELECTED),
        (FieldDecisionKind.USE_PROPOSAL, "New title", ReviewReasonCode.PROPOSAL_SELECTED),
        (FieldDecisionKind.USE_MANUAL, "My title", ReviewReasonCode.MANUAL_VALUE_SELECTED),
        (FieldDecisionKind.CLEAR, None, ReviewReasonCode.CLEAR_SELECTED),
    ),
)
def test_field_decisions_rebuild_changes_and_retain_provider_evidence(
    decision: FieldDecisionKind, expected: str | None, reason: ReviewReasonCode,
) -> None:
    state = make_selected_session((proposal(MetadataField.TITLE, "New title"),))
    original = state.groups[0]
    source = original.group.files[0]
    changed = apply_field_decision(
        state, "album", source.file_id, MetadataField.TITLE, decision, RenameSettings(),
        manual_value="My title" if decision is FieldDecisionKind.USE_MANUAL else None,
    )

    assert change_set(changed).final_metadata.title == expected
    assert field_review(changed, MetadataField.TITLE).decision_origin is DecisionOrigin.USER
    assert reason in field_review(changed, MetadataField.TITLE).reason_codes
    assert changed.groups[0].reviewed_files[0].proposals == original.reviewed_files[0].proposals
    assert changed.groups[0].selected_release is original.selected_release
    assert changed.groups[0].automatic_track_mapping is original.automatic_track_mapping
    assert changed.groups[0].candidate_lookup is original.candidate_lookup
    assert changed.groups[0].selected_metadata is None
    assert changed.revision == state.revision + 1
    assert changed.groups[0].revision == original.revision + 1
    assert changed.library_revision == state.library_revision
    assert changed.provider_cache is state.provider_cache
    assert change_set(changed) is not change_set(state)
    assert source.read_result.metadata.title == "Overture"
    assert field_review(state, MetadataField.TITLE).decision_origin is DecisionOrigin.DEFAULT


def test_manual_edit_initialises_local_reviews_without_a_lookup() -> None:
    state = make_local_session()
    source = state.groups[0].group.files[0]
    changed = apply_field_decision(
        state, "album", source.file_id, MetadataField.COMPOSERS, FieldDecisionKind.USE_MANUAL,
        RenameSettings(), manual_value=("First composer", "Second composer"),
    )

    assert change_set(changed).final_metadata.composers == ("First composer", "Second composer")
    assert {item.field for item in changed.groups[0].reviewed_files[0].reviews} == set(MetadataField)
    assert changed.groups[0].selected_release is None
    assert changed.groups[0].reviewed_files[0].proposals == ()
    assert state.groups[0].reviewed_files == ()


def test_unresolved_mapping_stays_blocked_for_selected_track_provider_changes() -> None:
    state = make_selected_session((proposal(MetadataField.TITLE, "Provider title"),), mapping_resolved=False)
    file_id = state.groups[0].group.files[0].file_id
    changed = apply_field_decision(
        state, "album", file_id, MetadataField.TITLE, FieldDecisionKind.USE_PROPOSAL, RenameSettings(),
    )

    assert change_set(changed).status is ChangeSetStatus.BLOCKED
    assert ChangeIssueCode.UNRESOLVED_TRACK_MAPPING in {item.code for item in change_set(changed).validation.issues}
    assert changed.groups[0].reviewed_files[0].track_mapping_resolved is False

    kept = apply_field_decision(
        changed, "album", file_id, MetadataField.TITLE, FieldDecisionKind.KEEP_EXISTING, RenameSettings(),
    )
    assert ChangeIssueCode.UNRESOLVED_TRACK_MAPPING not in {item.code for item in change_set(kept).validation.issues}


def test_safe_additions_only_accept_untouched_missing_unambiguous_confident_fields() -> None:
    metadata = MetadataSnapshot(
        title="Overture", album="Album", track=Position(1), composers=("Local composer",), genres=("Local genre",),
    )
    state = make_selected_session(
        (
            proposal(MetadataField.DISC, Position(2, 3)),
            proposal(MetadataField.ARTISTS, ("Unsure artist",), FieldConfidence.REVIEW),
            proposal(MetadataField.DATE, "2020"),
            proposal(MetadataField.DATE, "2021", record="other"),
            proposal(MetadataField.COMPOSERS, ("Provider composer",)),
            proposal(MetadataField.GENRES, ("Provider genre",)),
            proposal(MetadataField.ALBUM_ARTISTS, ("Album artist",)),
        ),
        metadata=metadata,
    )
    file_id = state.groups[0].group.files[0].file_id
    state = apply_field_decision(
        state, "album", file_id, MetadataField.ALBUM_ARTISTS, FieldDecisionKind.KEEP_EXISTING, RenameSettings(),
    )
    changed = accept_safe_additions(state, "album", RenameSettings())
    final = change_set(changed).final_metadata

    assert final.disc == Position(2, 3)
    assert field_review(changed, MetadataField.DISC).decision_origin is DecisionOrigin.USER
    assert final.artists == ()
    assert final.date is None
    assert final.composers == ("Local composer",)
    assert final.genres == ("Local genre",)

    # A deliberate per-field rejection remains authoritative when an album-wide
    # convenience action is used later; changing it requires a new field action.
    assert final.album_artists == ()
    assert field_review(changed, MetadataField.ALBUM_ARTISTS).decision is FieldDecisionKind.KEEP_EXISTING
    assert changed.groups[0].reviewed_files[0].proposals == state.groups[0].reviewed_files[0].proposals


def test_reviewed_disc_updates_default_preview_and_filename_decision_is_independent() -> None:
    state = make_selected_session((proposal(MetadataField.DISC, Position(1, 7)),))
    file_id = state.groups[0].group.files[0].file_id
    kept_disc = apply_field_decision(
        state, "album", file_id, MetadataField.DISC, FieldDecisionKind.KEEP_EXISTING, RenameSettings(),
    )
    assert change_set(kept_disc).rename_preview is not None
    assert change_set(kept_disc).rename_preview.new_path.name == "01. Overture.flac"

    accepted = apply_field_decision(
        kept_disc, "album", file_id, MetadataField.DISC, FieldDecisionKind.USE_PROPOSAL, RenameSettings(),
    )
    assert change_set(accepted).rename_preview is not None
    assert change_set(accepted).rename_preview.new_path.name == "1.01. Overture.flac"
    assert change_set(accepted).rename_change is None

    renamed = set_rename_decision(accepted, "album", (file_id,), RenameDecision.APPLY_RENAME, RenameSettings())
    assert change_set(renamed).rename_change == change_set(accepted).rename_preview
    assert change_set(renamed).metadata_changes == change_set(accepted).metadata_changes

    rejected = set_rename_decision(renamed, "album", (file_id,), RenameDecision.KEEP_FILENAME, RenameSettings())
    assert change_set(rejected).rename_change is None
    assert change_set(rejected).final_metadata == change_set(renamed).final_metadata


def test_duplicate_planned_destinations_block_both_files_and_rebuild_after_edit() -> None:
    state = make_local_session(make_source("a.flac"), make_source("b.flac"))
    ids = tuple(source.file_id for source in state.groups[0].group.files)
    changed = set_rename_decision(state, "album", ids, RenameDecision.APPLY_RENAME, RenameSettings())

    for index in (0, 1):
        assert change_set(changed, index).status is ChangeSetStatus.BLOCKED
        assert ChangeIssueCode.DESTINATION_COLLISION in {
            item.code for item in change_set(changed, index).validation.issues
        }

    revised = apply_field_decision(
        changed, "album", ids[1], MetadataField.TITLE, FieldDecisionKind.USE_MANUAL,
        RenameSettings(), manual_value="A different title",
    )

    # Both affected previews must be revalidated. Rebuilding only the edited
    # file would leave the first file blocked by a collision that no longer exists.
    for index in (0, 1):
        assert change_set(revised, index).status is not ChangeSetStatus.BLOCKED
        assert change_set(revised, index).rename_change is not None
        assert ChangeIssueCode.DESTINATION_COLLISION not in {
            item.code for item in change_set(revised, index).validation.issues
        }


def test_known_sibling_in_another_group_blocks_preview_without_touching_filesystem() -> None:
    state = make_local_session()
    sibling = make_source("01. OVERTURE.flac")
    other = GroupState(AlbumGroup("other", (sibling,), "Other", GroupingReason.DIRECTORY_ALBUM_CONSISTENT))
    state = replace(state, groups=(*state.groups, other))
    file_id = state.groups[0].group.files[0].file_id
    changed = set_rename_decision(state, "album", (file_id,), RenameDecision.APPLY_RENAME, RenameSettings())

    assert change_set(changed).status is ChangeSetStatus.BLOCKED
    assert ChangeIssueCode.DESTINATION_COLLISION in {item.code for item in change_set(changed).validation.issues}
    assert changed.groups[1] is other

    kept = set_rename_decision(changed, "album", (file_id,), RenameDecision.KEEP_FILENAME, RenameSettings())
    assert change_set(kept).status is ChangeSetStatus.VALID_WITH_WARNINGS
    assert change_set(kept).rename_change is None


@pytest.mark.parametrize("reverse_groups", (False, True))
def test_cross_group_batch_rename_collisions_do_not_depend_on_group_order(reverse_groups) -> None:
    first, second = make_source("first.flac"), make_source("second.flac")
    initial = make_local_session(first, second)
    original_group = initial.groups[0].group
    groups = (
        GroupState(replace(original_group, group_id="first", files=(first,))),
        GroupState(replace(original_group, group_id="second", files=(second,))),
    )
    state = replace(initial, groups=tuple(reversed(groups)) if reverse_groups else groups, selection=None)
    command = BatchReviewCommand(
        file_ids=(first.file_id, second.file_id), fields=(), expected_revision=state.revision,
        action=BatchReviewAction.INCLUDE_RENAMES,
    )
    result = apply_batch_review(state, command, RenameSettings(template="%title%"))

    # Every group must see the complete destination set before the second pass.
    # Outcome order still follows the captured IDs rather than the group order.
    assert tuple(item.file_id for item in result.affected) == command.file_ids
    assert result.blocked == result.skipped == ()
    assert len(result.state.review_undo) == 1

    for group in result.state.groups:
        changes = group.reviewed_files[0].change_set
        assert changes is not None
        assert changes.status is ChangeSetStatus.BLOCKED
        assert ChangeIssueCode.DESTINATION_COLLISION in {issue.code for issue in changes.validation.issues}


def test_language_reranking_retains_planned_destination_blockers() -> None:
    state = make_local_session(make_source("a.flac"), make_source("b.flac"))
    ids = tuple(source.file_id for source in state.groups[0].group.files)
    state = set_rename_decision(state, "album", ids, RenameDecision.APPLY_RENAME, RenameSettings())
    assert all(change_set(state, index).status is ChangeSetStatus.BLOCKED for index in (0, 1))

    changed = change_language(state, "album", "en", RenameSettings())

    for index in (0, 1):
        assert change_set(changed, index).status is ChangeSetStatus.BLOCKED
        assert ChangeIssueCode.DESTINATION_COLLISION in {
            item.code for item in change_set(changed, index).validation.issues
        }


@pytest.mark.parametrize("operation", ["field", "rename", "bulk"])
@pytest.mark.parametrize("blocked_reason", ["busy", "rescan"])
def test_review_transforms_reject_busy_or_stale_groups(operation: str, blocked_reason: str) -> None:
    state = make_local_session()
    file_id = state.groups[0].group.files[0].file_id

    if blocked_reason == "busy":
        state = begin_operation(state, "SCAN-0002", OperationKind.SCAN, ())
    else:
        state = replace(state, groups=(replace(state.groups[0], requires_rescan=True),))

    with pytest.raises(ValueError):
        if operation == "field":
            apply_field_decision(
                state, "album", file_id, MetadataField.TITLE, FieldDecisionKind.KEEP_EXISTING, RenameSettings(),
            )
        elif operation == "rename":
            set_rename_decision(state, "album", (file_id,), RenameDecision.APPLY_RENAME, RenameSettings())
        else:
            accept_safe_additions(state, "album", RenameSettings())


def test_blank_manual_value_is_rejected_instead_of_clearing_the_field() -> None:
    state = make_local_session()
    file_id = state.groups[0].group.files[0].file_id

    with pytest.raises(ValueError, match="blank"):
        apply_field_decision(
            state, "album", file_id, MetadataField.TITLE, FieldDecisionKind.USE_MANUAL,
            RenameSettings(), manual_value="  ",
        )

    assert state.groups[0].reviewed_files == ()
