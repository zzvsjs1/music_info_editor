"""Targeted post-write snapshots must not discard unchecked review decisions."""

# Verified snapshots refresh only files touched by Apply. Unchecked siblings and
# manual group membership must survive the same result reduction.


from dataclasses import replace
from pathlib import Path

import pytest

from metadata_polisher.application.apply import ApplyFileOutcome, ApplyFileOutcomeStatus, ApplyGroupOutcome
from metadata_polisher.application.changes import RenameDecision, build_change_set
from metadata_polisher.domain.media import LocalMediaFile
from metadata_polisher.domain.metadata import FieldReadState, MetadataField, MetadataSnapshot
from metadata_polisher.domain.review import FieldDecisionKind
from metadata_polisher.execution.events import FileApplyStatus, FileTransactionStage
from metadata_polisher.infrastructure.settings import RenameSettings
from metadata_polisher.infrastructure.transaction import FileApplyResult
from metadata_polisher.scanner.grouping import GroupingReason
from metadata_polisher.session.review_editing import apply_field_decision
from metadata_polisher.session.state import (
    OperationKind,
    ResultApplicationStatus,
    ReviewedFileState,
    ReviewUndoEntry,
    ReviewUndoFile,
    SessionState,
    apply_batch_result,
    begin_operation,
)
from tests.unit.session.test_apply_reducer import (
    make_file,
    make_group_state,
    make_outcome,
    make_result,
    make_reviews,
)
from tests.unit.session.test_review_editing import make_local_session, make_source


def album_write_outcome(state: SessionState, source: LocalMediaFile) -> ApplyFileOutcome:
    """Build a verified receipt from the actual reviewed album decision."""
    # Model both the committed decision and the matching refreshed read state.
    # A proposed album value alone would not be a verified source snapshot.
    reviewed = next(item for item in state.groups[0].reviewed_files if item.file_id == source.file_id)
    changes = reviewed.change_set
    assert changes is not None
    album = changes.final_metadata.album
    refreshed = replace(
        source,
        read_result=replace(
            source.read_result,
            metadata=changes.final_metadata,
            field_states={
                **source.read_result.field_states,
                MetadataField.ALBUM: FieldReadState.PRESENT if album is not None else FieldReadState.MISSING,
            },
        ),
    )

    return ApplyFileOutcome(
        file_id=source.file_id,
        source_path=source.path,
        selected_release=None,
        reviews=reviewed.reviews,
        change_set=changes,
        status=ApplyFileOutcomeStatus.APPLIED,
        transaction_result=FileApplyResult(
            source.path, source.path, FileApplyStatus.SUCCEEDED, FileTransactionStage.COMPLETED,
        ),
        refreshed_source=refreshed,
    )


@pytest.mark.parametrize(
    ("old_album", "new_album"),
    (("Album", "Corrected album"), (None, "New album"), ("Album", None)),
)
@pytest.mark.parametrize("reason", (GroupingReason.DIRECTORY_ALBUM_CONSISTENT, GroupingReason.MANUAL_MERGE))
def test_verified_album_write_refreshes_group_title_without_regrouping(
    old_album: str | None,
    new_album: str | None,
    reason: GroupingReason,
) -> None:
    source = make_source(metadata=MetadataSnapshot(title="Overture", album=old_album))
    state = make_local_session(source)
    group = state.groups[0]
    state = replace(state, groups=(replace(group, group=replace(group.group, album_title=old_album, reason=reason)),))
    state = apply_field_decision(
        state, "album", source.file_id, MetadataField.ALBUM,
        FieldDecisionKind.CLEAR if new_album is None else FieldDecisionKind.USE_MANUAL,
        RenameSettings(), manual_value=new_album,
    )

    # Review edits are still pending. The library title changes only after a
    # verified disk receipt supplies fresh metadata for the same stable files.
    assert state.groups[0].group.album_title == old_album
    started = begin_operation(state, "APPLY-1", OperationKind.APPLY, ("album",))
    outcome = album_write_outcome(started, source)
    result = make_result(started, (ApplyGroupOutcome("album", state.groups[0].revision, (outcome,)),))

    reduced = apply_batch_result(started, result)

    assert reduced.status is ResultApplicationStatus.APPLIED
    updated = reduced.state.groups[0]
    assert updated.group.album_title == new_album
    assert updated.group.group_id == "album"
    assert updated.group.reason is reason
    assert updated.group.files[0].file_id == source.file_id
    assert reduced.state.selection == state.selection
    assert not updated.requires_rescan


def test_partial_album_write_uses_all_current_tags_and_retains_unwritten_review() -> None:
    written = make_source("first.flac")
    sibling = make_source("second.flac")
    state = make_local_session(written, sibling)

    for source in (written, sibling):
        state = apply_field_decision(
            state, "album", source.file_id, MetadataField.ALBUM,
            FieldDecisionKind.USE_MANUAL, RenameSettings(), manual_value="Corrected album",
        )

    sibling_review = next(item for item in state.groups[0].reviewed_files if item.file_id == sibling.file_id)
    started = begin_operation(state, "APPLY-1", OperationKind.APPLY, ("album",))
    outcome = album_write_outcome(started, written)
    result = make_result(started, (ApplyGroupOutcome("album", state.groups[0].revision, (outcome,)),))

    reduced = apply_batch_result(started, result)

    assert reduced.status is ResultApplicationStatus.APPLIED
    updated = reduced.state.groups[0]
    # The sibling still has the original tag on disk. Mixed current album tags
    # must not be labelled with either the stale title or the pending common edit.
    assert updated.group.album_title is None
    assert updated.group.group_id == "album"
    assert updated.group.files == (outcome.refreshed_source, sibling)
    assert updated.group.files[1] is sibling
    assert next(item for item in updated.reviewed_files if item.file_id == sibling.file_id) is sibling_review


def test_refreshed_success_retains_unchecked_sibling_review_and_trims_only_written_undo() -> None:
    written = make_file("written", "library/album/01.flac")
    sibling = make_file("sibling", "library/album/02.flac")
    group = make_group_state("album", (written, sibling), 2, language_override="ja")
    before_written, before_sibling = group.reviewed_files
    written_reviews = make_reviews(written, "New title")
    sibling_reviews = make_reviews(sibling, "Pending sibling title")
    reviewed_written = ReviewedFileState(
        written.file_id,
        reviews=written_reviews,
        change_set=build_change_set(written, written_reviews, RenameDecision.KEEP_FILENAME),
    )
    reviewed_sibling = ReviewedFileState(
        sibling.file_id,
        reviews=sibling_reviews,
        change_set=build_change_set(sibling, sibling_reviews, RenameDecision.KEEP_FILENAME),
    )
    written_undo = ReviewUndoFile(written, before_written, reviewed_written)
    sibling_undo = ReviewUndoFile(sibling, before_sibling, reviewed_sibling)
    group = replace(group, reviewed_files=(reviewed_written, reviewed_sibling))
    started = begin_operation(
        SessionState(
            root=Path("library"),
            groups=(group,),
            revision=2,
            review_undo=(
                ReviewUndoEntry((written_undo, sibling_undo)),
                ReviewUndoEntry((written_undo,)),
            ),
        ),
        "APPLY-1",
        OperationKind.APPLY,
        ("album",),
    )
    refreshed = replace(
        written,
        read_result=replace(
            written.read_result,
            metadata=replace(written.read_result.metadata, title="New title"),
        ),
    )
    outcome = replace(
        make_outcome(written, filesystem_effect=True),
        refreshed_source=refreshed,
    )
    result = make_result(started, (ApplyGroupOutcome("album", 2, (outcome,)),))

    reduced = apply_batch_result(started, result)

    assert reduced.status is ResultApplicationStatus.APPLIED
    refreshed_group = reduced.state.groups[0]
    assert refreshed_group.group.files == (refreshed, sibling)
    assert refreshed_group.group.files[1] is sibling
    assert not refreshed_group.requires_rescan
    assert refreshed_group.language_override == "ja"
    assert next(
        item for item in refreshed_group.reviewed_files if item.file_id == "sibling"
    ) is reviewed_sibling
    assert all(
        item.change_set is None or not item.change_set.metadata_changes
        for item in refreshed_group.reviewed_files if item.file_id == "written"
    )
    assert reduced.state.review_undo == (ReviewUndoEntry((sibling_undo,)),)
    assert len(reduced.state.written_files) == 1
    receipt = reduced.state.written_files[0]
    assert receipt.source == refreshed
    assert receipt.changed_fields == frozenset({MetadataField.TITLE})
    assert not receipt.renamed


def test_refresh_receipt_cannot_make_a_changed_group_lineage_falsely_fresh() -> None:
    source = make_file("written", "library/album/01.flac")
    group = make_group_state("album", (source,), 2)
    started = begin_operation(
        SessionState(root=Path("library"), groups=(group,), revision=2),
        "APPLY-1",
        OperationKind.APPLY,
        ("album",),
    )
    refreshed = replace(
        source,
        read_result=replace(
            source.read_result,
            metadata=replace(source.read_result.metadata, title="New title"),
        ),
    )
    outcome = replace(
        make_outcome(source, filesystem_effect=True),
        refreshed_source=refreshed,
    )
    result = make_result(started, (ApplyGroupOutcome("album", 2, (outcome,)),))
    changed = replace(started, groups=(replace(group, revision=3),), revision=3)

    reduced = apply_batch_result(changed, result)

    assert reduced.status is ResultApplicationStatus.STALE
    assert reduced.state.groups[0].requires_rescan
    assert reduced.state.groups[0].group.files == (source,)


def test_verified_write_receipt_expires_when_the_source_changes_or_a_scan_replaces_it() -> None:
    from metadata_polisher.session.state import GroupState, ScanResultEnvelope, VerifiedWriteReceipt, apply_scan_result

    source = make_file("written", "library/album/01.flac")
    group = make_group_state("album", (source,), 0)
    receipt = VerifiedWriteReceipt(source, frozenset({MetadataField.TITLE}), False)
    state = SessionState(root=Path("library"), groups=(group,), written_files=(receipt,))
    changed_source = replace(
        source,
        read_result=replace(
            source.read_result, metadata=replace(source.read_result.metadata, title="Later disk title"),
        ),
    )
    changed_group = replace(group, group=replace(group.group, files=(changed_source,)), reviewed_files=())

    assert replace(state, groups=(changed_group,)).written_files == ()
    scanning = begin_operation(state, "SCAN-1", OperationKind.SCAN, ())
    rescanned = apply_scan_result(
        scanning,
        ScanResultEnvelope("SCAN-1", 0, 0, Path("library"), (GroupState(group=group.group),)),
    )
    assert rescanned.state.written_files == ()
