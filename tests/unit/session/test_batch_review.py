"""Batch decisions retain per-file values and operate on captured review scope."""

# Different per-file values make accidental copying from the first selected row
# visible; captured IDs and revisions define the scope of every batch action.


from dataclasses import replace

import pytest

from metadata_polisher.application.changes import RenameDecision, build_change_set
from metadata_polisher.application.lookup import CandidateLookupResult, LookupSearchResult
from metadata_polisher.application.review import build_field_review_state
from metadata_polisher.domain.matching import (
    LocalisedText,
    MetadataProvenance,
    ProviderTrack,
    ReleaseCandidate,
    ReleaseMedium,
)
from metadata_polisher.domain.metadata import FieldReadState, MetadataField, MetadataSnapshot, Position
from metadata_polisher.domain.review import DecisionOrigin, FieldDecisionKind, FieldProposal, FieldReviewState
from metadata_polisher.infrastructure.settings import RenameSettings
from metadata_polisher.matching.release_scoring import build_local_release_evidence, rank_release_candidates
from metadata_polisher.matching.track_mapping import map_tracks
from metadata_polisher.providers.coordinator import CoordinatedCandidate
from metadata_polisher.session.lookup_editing import set_search_query_override
from metadata_polisher.session.review_editing import (
    REVIEW_UNDO_LIMIT,
    AggregateValueState,
    BatchReviewAction,
    BatchReviewCommand,
    aggregate_review_fields,
    apply_batch_review,
    undo_last_review_action,
)
from metadata_polisher.session.state import (
    GroupState,
    ReleaseSelectionState,
    ReviewedFileState,
    SessionState,
    mark_groups_requires_rescan,
)
from tests.unit.session.test_lookup_editing import make_query
from tests.unit.session.test_review_editing import make_local_session, make_source, proposal
from tests.unit.session.test_review_editing import make_selected_session as make_single_selected_session


def make_batch_session(*, absent_title_candidate: int | None = None) -> SessionState:
    """Supply three independently mapped titles without a network or media fixture."""
    sources = tuple(
        make_source(f"{index + 1:02}.flac", MetadataSnapshot(
            title=title, album="Original album", track=Position(index + 1, 3), disc=Position(1, 2),
        ))
        for index, title in enumerate(("Local first", "Local second", "Local third"))
    )
    state = make_local_session(*sources)
    group = state.groups[0].group
    candidate = ReleaseCandidate(
        "musicbrainz", "musicbrainz", "release", (LocalisedText("Original album", None, None),), (), None,
        (ReleaseMedium(1, None, tuple(
            ProviderTrack(index + 1, (LocalisedText(source.read_result.metadata.title, None, None),), (), (), 180.0)
            for index, source in enumerate(sources)
        )),), None,
    )
    provenance = MetadataProvenance("musicbrainz", "musicbrainz", "release", None, None, "LOOKUP-BATCH")
    coordinated = CoordinatedCandidate(candidate, (provenance,))
    lookup = LookupSearchResult("album", (make_query("Original album"),), (coordinated,), ())
    ranking = rank_release_candidates(build_local_release_evidence(group), (candidate,))
    candidates = CandidateLookupResult(lookup, ranking, (), ())
    mapping = map_tracks(sources, candidate, selected_medium_index=0)
    reviewed_files = []

    for index, source in enumerate(sources):
        supplied: tuple[FieldProposal, ...] = (
            proposal(MetadataField.ALBUM, "Candidate album"),
            proposal(MetadataField.GENRES, ("Soundtrack",)),
        )

        if index != absent_title_candidate:
            supplied = (*supplied, proposal(MetadataField.TITLE, f"Candidate {index + 1}"))

        # Each field keeps its own read state. The tests must not accidentally
        # describe unreadable or absent data as a single shared empty value.
        reviews = tuple(build_field_review_state(
            field=field,
            read_state=source.read_result.field_states[field],
            existing_value=(getattr(source.read_result.metadata, field.value)
                            if source.read_result.field_states[field] is FieldReadState.PRESENT else None),
            proposals=tuple(item for item in supplied if item.field is field),
        ) for field in MetadataField)
        changes = build_change_set(source, reviews, RenameDecision.KEEP_FILENAME)
        reviewed_files.append(ReviewedFileState(source.file_id, supplied, reviews, True, changes))

    selected = GroupState(
        group, lookup_result=lookup, release_ranking=ranking, candidate_lookup=candidates,
        selected_release=ReleaseSelectionState(coordinated, 0), automatic_track_mapping=mapping,
        reviewed_files=tuple(reviewed_files),
    )

    return replace(state, groups=(selected,))


def selected_ids(state: SessionState) -> tuple[str, ...]:
    return tuple(source.file_id for group in state.groups for source in group.group.files)


def reviews_by_id(state: SessionState) -> dict[str, ReviewedFileState]:
    return {review.file_id: review for group in state.groups for review in group.reviewed_files}


def field_for(state: SessionState, file_id: str, field: MetadataField) -> FieldReviewState:
    return next(review for review in reviews_by_id(state)[file_id].reviews if review.field is field)


def final_title(state: SessionState, file_id: str) -> str | None:
    changes = reviews_by_id(state)[file_id].change_set
    assert changes is not None

    return changes.final_metadata.title


def command_for(state: SessionState, action: BatchReviewAction, *, fields=(MetadataField.TITLE,)):
    # Capture the revision with stable IDs just as the dialogue does; re-reading
    # selection after the command is created would conceal stale-scope bugs.
    return BatchReviewCommand(
        file_ids=selected_ids(state), fields=fields, expected_revision=state.revision, action=action,
    )


@pytest.mark.parametrize(
    ("action", "expected"),
    (
        (BatchReviewAction.KEEP_EXISTING, ("Local first", "Local second", "Local third")),
        (BatchReviewAction.USE_CANDIDATE, ("Candidate 1", "Candidate 2", "Candidate 3")),
    ),
)
def test_three_titles_keep_their_own_existing_or_candidate_value(action, expected):
    state = make_batch_session()
    ids = selected_ids(state)
    result = apply_batch_review(state, command_for(state, action), RenameSettings())

    assert tuple(final_title(result.state, file_id) for file_id in ids) == expected
    assert len(result.affected) == 3
    assert result.skipped == result.blocked == ()
    assert {item.file_id for item in result.affected} == set(ids)
    assert all(
        field_for(result.state, file_id, MetadataField.TITLE).decision_origin is DecisionOrigin.USER for file_id in ids
    )
    assert all(not field_for(result.state, file_id, MetadataField.TITLE).requires_review for file_id in ids)
    assert tuple(source.read_result.metadata.title for source in state.groups[0].group.files) == (
        "Local first", "Local second", "Local third",
    )


def test_missing_candidate_is_skipped_and_never_becomes_clear():
    state = make_batch_session(absent_title_candidate=1)
    ids = selected_ids(state)
    result = apply_batch_review(state, command_for(state, BatchReviewAction.USE_CANDIDATE), RenameSettings())

    assert tuple(final_title(result.state, file_id) for file_id in ids) == (
        "Candidate 1", "Local second", "Candidate 3",
    )
    assert len(result.affected) == 2 and len(result.skipped) == 1 and not result.blocked
    assert result.skipped[0].file_id == ids[1]
    assert result.skipped[0].field is MetadataField.TITLE
    assert field_for(result.state, ids[1], MetadataField.TITLE).decision is not FieldDecisionKind.CLEAR


def test_common_album_changes_exactly_the_declared_files_and_preserves_titles():
    state = make_batch_session()
    ids = selected_ids(state)
    command = BatchReviewCommand(
        file_ids=(ids[2], ids[0]), fields=(MetadataField.ALBUM,), expected_revision=state.revision,
        action=BatchReviewAction.SET_COMMON_VALUE, common_value="Reviewed common album",
    )
    result = apply_batch_review(state, command, RenameSettings())
    reviews = reviews_by_id(result.state)

    assert len(result.affected) == 2 and not result.skipped and not result.blocked
    assert tuple(reviews[file_id].change_set.final_metadata.album for file_id in ids) == (
        "Reviewed common album", "Original album", "Reviewed common album",
    )
    assert tuple(final_title(result.state, file_id) for file_id in ids) == (
        "Local first", "Local second", "Local third",
    )


def test_clear_multiple_fields_reports_file_field_scope_and_does_not_write(monkeypatch):
    from metadata_polisher.infrastructure.transaction import TransactionalFileWriter

    def unexpected_write(*_args, **_kwargs):
        pytest.fail("A review decision must not call the transactional writer")

    monkeypatch.setattr(TransactionalFileWriter, "apply_file", unexpected_write)
    state = make_batch_session()
    command = command_for(state, BatchReviewAction.CLEAR, fields=(MetadataField.TITLE, MetadataField.ALBUM))
    result = apply_batch_review(state, command, RenameSettings())

    assert len(result.affected) == 6 and not result.skipped and not result.blocked
    assert {(item.file_id, item.field) for item in result.affected} == {
        (file_id, field) for file_id in selected_ids(state) for field in command.fields
    }
    assert all(review.change_set.final_metadata.title is None and review.change_set.final_metadata.album is None
               for review in reviews_by_id(result.state).values())
    assert state.groups[0].group.files[0].read_result.metadata.title == "Local first"


def test_batch_scope_uses_ids_after_visible_order_changes():
    state = make_batch_session()
    ids = selected_ids(state)
    command = BatchReviewCommand(
        file_ids=(ids[0],), fields=(MetadataField.TITLE,), expected_revision=state.revision,
        action=BatchReviewAction.USE_CANDIDATE,
    )
    group = state.groups[0]
    reordered_group = replace(group, group=replace(group.group, files=tuple(reversed(group.group.files))))
    reordered = replace(state, groups=(reordered_group,))
    result = apply_batch_review(reordered, command, RenameSettings())

    assert final_title(result.state, ids[0]) == "Candidate 1"
    assert final_title(result.state, ids[2]) == "Local third"


def test_stale_captured_revision_cannot_overwrite_newer_review():
    state = make_batch_session()
    pending = command_for(state, BatchReviewAction.USE_CANDIDATE)
    changed = apply_batch_review(state, command_for(state, BatchReviewAction.KEEP_EXISTING), RenameSettings()).state

    with pytest.raises(ValueError, match="revision|stale|changed"):
        apply_batch_review(changed, pending, RenameSettings())

    assert all(field_for(changed, file_id, MetadataField.TITLE).decision is FieldDecisionKind.KEEP_EXISTING
               for file_id in selected_ids(state))


@pytest.mark.parametrize("read_state", (FieldReadState.UNREADABLE, FieldReadState.UNSUPPORTED))
def test_batch_reports_unwritable_fields_without_losing_valid_members(read_state):
    good = make_source("good.flac")
    damaged = make_source("damaged.flac")
    damaged = replace(damaged, read_result=replace(damaged.read_result, field_states={
        **damaged.read_result.field_states, MetadataField.TITLE: read_state,
    }))
    state = make_local_session(good, damaged)
    command = BatchReviewCommand(
        file_ids=selected_ids(state), fields=(MetadataField.TITLE,), expected_revision=state.revision,
        action=BatchReviewAction.SET_COMMON_VALUE, common_value="Reviewed title",
    )
    result = apply_batch_review(state, command, RenameSettings())

    assert len(result.affected) == len(result.blocked) == 1 and not result.skipped
    assert result.affected[0].file_id == good.file_id
    assert result.blocked[0].file_id == damaged.file_id
    assert final_title(result.state, good.file_id) == "Reviewed title"
    assert field_for(result.state, damaged.file_id, MetadataField.TITLE).read_state is read_state


def test_repeated_identical_decision_does_not_create_another_undo_entry():
    state = make_batch_session()
    state = apply_batch_review(state, command_for(state, BatchReviewAction.KEEP_EXISTING), RenameSettings()).state
    result = apply_batch_review(state, command_for(state, BatchReviewAction.KEEP_EXISTING), RenameSettings())

    assert not result.affected and not result.blocked
    assert len(result.skipped) == 3
    assert result.state.review_undo == state.review_undo
    assert not any(
        change.field is MetadataField.TITLE
        for review in reviews_by_id(result.state).values() for change in review.change_set.metadata_changes
    )


def test_cross_group_album_edit_requires_explicit_scope_confirmation():
    state = make_batch_session()
    other = make_source("other.flac", MetadataSnapshot(
        title="Other", album="Separate release", track=Position(1), disc=Position(2, 2),
    ))
    other_state = make_local_session(other).groups[0]
    other_group = replace(other_state, group=replace(other_state.group, group_id="other"))
    state = replace(state, groups=(*state.groups, other_group))
    command = BatchReviewCommand(
        file_ids=(selected_ids(state)[0], other.file_id), fields=(MetadataField.ALBUM,),
        expected_revision=state.revision,
        action=BatchReviewAction.SET_COMMON_VALUE, common_value="Intentional shared album",
    )
    blocked = apply_batch_review(state, command, RenameSettings())

    assert len(blocked.blocked) == 2 and not blocked.affected
    assert blocked.state.groups == state.groups

    accepted = apply_batch_review(state, replace(command, confirm_cross_group=True), RenameSettings())
    assert len(accepted.affected) == 2 and not accepted.blocked


@pytest.mark.parametrize("field", (MetadataField.TRACK, MetadataField.DISC))
def test_common_positions_cannot_flatten_a_multidisc_selection(field):
    sources = (
        make_source("disc1.flac", MetadataSnapshot(title="First", track=Position(1, 9), disc=Position(1, 2))),
        make_source("disc2.flac", MetadataSnapshot(title="Second", track=Position(2, 12), disc=Position(2, 2))),
    )
    state = make_local_session(*sources)
    command = BatchReviewCommand(
        file_ids=selected_ids(state), fields=(field,), expected_revision=state.revision,
        action=BatchReviewAction.SET_COMMON_VALUE, common_value=Position(1, 9), confirm_cross_group=True,
    )
    result = apply_batch_review(state, command, RenameSettings())

    assert len(result.blocked) == 2 and not result.affected
    assert result.state.groups == state.groups


def test_aggregate_mixed_value_is_typed_and_has_no_writable_sentinel():
    state = make_batch_session()
    rows = aggregate_review_fields(state, selected_ids(state), (MetadataField.TITLE, MetadataField.ALBUM))
    title, album = rows

    assert title.field is MetadataField.TITLE
    assert title.existing.state is AggregateValueState.MIXED and title.existing.value is None
    assert title.proposed.state is AggregateValueState.MIXED and title.proposed.value is None
    assert album.existing.state is AggregateValueState.VALUE and album.existing.value == "Original album"


def test_aggregate_final_uses_the_actual_merged_position():
    state = make_single_selected_session(
        (proposal(MetadataField.TRACK, Position(total=9)),),
        metadata=MetadataSnapshot(title="Track seven", album="Album", track=Position(7)),
    )
    command = command_for(state, BatchReviewAction.USE_CANDIDATE, fields=(MetadataField.TRACK,))
    changed = apply_batch_review(state, command, RenameSettings()).state
    row = aggregate_review_fields(changed, selected_ids(changed), (MetadataField.TRACK,))[0]

    assert row.proposed.value == Position(total=9)
    assert row.final.value == Position(7, 9)


@pytest.mark.parametrize("read_state", (FieldReadState.MISSING, FieldReadState.UNREADABLE, FieldReadState.UNSUPPORTED))
def test_aggregate_distinguishes_missing_unreadable_and_unsupported(read_state):
    source = make_source(metadata=MetadataSnapshot(album="Album", track=Position(1)))
    source = replace(source, read_result=replace(source.read_result, field_states={
        **source.read_result.field_states, MetadataField.TITLE: read_state,
    }))
    state = make_local_session(source)
    row = aggregate_review_fields(state, (source.file_id,), (MetadataField.TITLE,))[0]

    assert row.existing.state.value == read_state.value
    assert row.existing.value is None


def test_rename_batch_uses_each_preview_and_undoes_only_review_intent():
    state = make_batch_session()
    ids = selected_ids(state)
    result = apply_batch_review(
        state, command_for(state, BatchReviewAction.INCLUDE_RENAMES, fields=()), RenameSettings(),
    )
    renamed = reviews_by_id(result.state)

    assert len(result.affected) == 3 and all(item.field is None for item in result.affected)
    assert {renamed[file_id].change_set.rename_change.new_path.name for file_id in ids} == {
        "1.01. Local first.flac", "1.02. Local second.flac", "1.03. Local third.flac",
    }
    assert all(renamed[file_id].reviews == reviews_by_id(state)[file_id].reviews for file_id in ids)

    undone = undo_last_review_action(result.state, RenameSettings())
    assert all(
        review.change_set.rename_decision is RenameDecision.KEEP_FILENAME for review in reviews_by_id(undone).values()
    )


def test_safe_additions_obeys_selected_file_field_scope():
    state = make_batch_session()
    ids = selected_ids(state)
    command = BatchReviewCommand(
        file_ids=(ids[0], ids[2]), fields=(MetadataField.TITLE, MetadataField.GENRES), expected_revision=state.revision,
        action=BatchReviewAction.ACCEPT_SAFE_ADDITIONS,
    )
    result = apply_batch_review(state, command, RenameSettings())

    assert {(item.file_id, item.field) for item in result.affected} == {
        (ids[0], MetadataField.GENRES), (ids[2], MetadataField.GENRES),
    }
    assert len(result.skipped) == 2 and not result.blocked
    assert field_for(result.state, ids[1], MetadataField.GENRES).decision_origin is DecisionOrigin.DEFAULT
    assert all(
        field_for(result.state, file_id, MetadataField.TITLE).decision_origin is DecisionOrigin.DEFAULT
        for file_id in ids
    )


def test_undo_reverts_one_batch_and_preserves_unrelated_group_changes():
    state = make_batch_session()
    other = make_source("other.flac")
    other_state = make_local_session(other).groups[0]
    other_group = replace(other_state, group=replace(other_state.group, group_id="other"))
    state = replace(state, groups=(*state.groups, other_group))
    command = BatchReviewCommand(
        file_ids=tuple(source.file_id for source in state.groups[0].group.files), fields=(MetadataField.TITLE,),
        expected_revision=state.revision, action=BatchReviewAction.USE_CANDIDATE,
    )
    changed = apply_batch_review(state, command, RenameSettings()).state
    changed = set_search_query_override(changed, "other", make_query("Unrelated search edit"))
    unrelated = changed.groups[1]
    undone = undo_last_review_action(changed, RenameSettings())

    assert tuple(final_title(undone, file_id) for file_id in command.file_ids) == (
        "Local first", "Local second", "Local third",
    )
    assert undone.groups[1] is unrelated
    assert undone.revision > changed.revision
    assert undone.library_revision == state.library_revision


def test_review_undo_is_bounded():
    state = make_batch_session()
    first = selected_ids(state)[0]

    for index in range(REVIEW_UNDO_LIMIT + 3):
        command = BatchReviewCommand(
            file_ids=(first,), fields=(MetadataField.TITLE,), expected_revision=state.revision,
            action=BatchReviewAction.SET_COMMON_VALUE, common_value=f"Manual review {index}",
        )
        state = apply_batch_review(state, command, RenameSettings()).state

    assert 0 < len(state.review_undo) <= REVIEW_UNDO_LIMIT


def test_undo_never_restores_a_stale_file_snapshot():
    state = make_batch_session()
    changed = apply_batch_review(state, command_for(state, BatchReviewAction.USE_CANDIDATE), RenameSettings()).state
    invalidated = mark_groups_requires_rescan(changed, ("album",))
    undone = undo_last_review_action(invalidated, RenameSettings())

    assert undone.groups[0].requires_rescan
    assert undone.groups[0].reviewed_files == ()
