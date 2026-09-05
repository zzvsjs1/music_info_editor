# Worker outcomes model disk effects separately from session freshness. A stale
# result must not replace current reviews, but changed files still need invalidation.

from dataclasses import replace
from pathlib import Path

from metadata_polisher.application.apply import (
    ApplyBatchResult,
    ApplyBatchStatus,
    ApplyFileOutcome,
    ApplyFileOutcomeStatus,
    ApplyGroupOutcome,
)
from metadata_polisher.application.changes import RenameDecision, build_change_set
from metadata_polisher.application.review import build_field_review_state, set_manual_decision
from metadata_polisher.domain.errors import Issue, MediaErrorCode
from metadata_polisher.domain.media import (
    LocalMediaFile,
    MediaReadResult,
    StreamInfo,
    UnsupportedMediaFile,
)
from metadata_polisher.domain.metadata import (
    FieldReadState,
    MetadataField,
    MetadataSnapshot,
    Position,
)
from metadata_polisher.domain.review import FieldReviewState
from metadata_polisher.execution.events import FileApplyStatus, FileTransactionStage
from metadata_polisher.infrastructure.reporting import ReportWriteResult, ReportWriteStatus
from metadata_polisher.infrastructure.transaction import FileApplyResult
from metadata_polisher.scanner.grouping import AlbumGroup, GroupingReason
from metadata_polisher.session.state import (
    GroupSelection,
    GroupState,
    OperationKind,
    ResultApplicationStatus,
    ReviewedFileState,
    SessionState,
    StaleResultReason,
    apply_batch_result,
    begin_operation,
    finish_operation,
)


def make_file(file_id: str, path: str, title: str = "Old title") -> LocalMediaFile:
    states = {field: FieldReadState.MISSING for field in MetadataField}
    states[MetadataField.TITLE] = FieldReadState.PRESENT
    states[MetadataField.TRACK] = FieldReadState.PRESENT

    return LocalMediaFile(
        path=Path(path),
        format_id="flac",
        read_result=MediaReadResult(
            metadata=MetadataSnapshot(title=title, track=Position(number=1)),
            field_states=states,
            stream_info=StreamInfo(180.0, 48_000, 2, 24, "FLAC"),
        ),
        file_id=file_id,
    )


def make_reviews(
    source: LocalMediaFile,
    new_title: str | None = None,
) -> tuple[FieldReviewState, ...]:
    existing_values: dict[MetadataField, object | None] = {
        MetadataField.TITLE: source.read_result.metadata.title,
        MetadataField.ARTISTS: None,
        MetadataField.ALBUM: None,
        MetadataField.ALBUM_ARTISTS: None,
        MetadataField.COMPOSERS: None,
        MetadataField.TRACK: source.read_result.metadata.track,
        MetadataField.DISC: None,
        MetadataField.DATE: None,
        MetadataField.GENRES: None,
    }
    reviews = tuple(
        build_field_review_state(
            field=field,
            read_state=source.read_result.field_states[field],
            existing_value=existing_values[field],  # type: ignore[arg-type]
            proposals=(),
        )
        for field in MetadataField
    )

    if new_title is None:
        return reviews

    return (set_manual_decision(reviews[0], new_title), *reviews[1:])


def make_group_state(
    group_id: str,
    files: tuple[LocalMediaFile, ...],
    revision: int,
    *,
    language_override: str | None = None,
) -> GroupState:
    reviewed = tuple(
        ReviewedFileState(
            file_id=source.file_id,
            reviews=make_reviews(source),
            change_set=build_change_set(
                source,
                make_reviews(source),
                RenameDecision.KEEP_FILENAME,
            ),
        )
        for source in files
    )

    return GroupState(
        group=AlbumGroup(
            group_id=group_id,
            files=files,
            album_title="Album",
            reason=GroupingReason.DIRECTORY_ALBUM_CONSISTENT,
        ),
        reviewed_files=reviewed,
        language_override=language_override,
        revision=revision,
    )


def make_outcome(
    source: LocalMediaFile,
    *,
    filesystem_effect: bool,
) -> ApplyFileOutcome:
    reviews = make_reviews(
        source,
        "New title" if filesystem_effect else None,
    )
    change_set = build_change_set(source, reviews, RenameDecision.KEEP_FILENAME)

    if not filesystem_effect:
        return ApplyFileOutcome(
            file_id=source.file_id,
            source_path=source.path,
            selected_release=None,
            reviews=reviews,
            change_set=change_set,
            status=ApplyFileOutcomeStatus.NO_CHANGES,
        )

    return ApplyFileOutcome(
        file_id=source.file_id,
        source_path=source.path,
        selected_release=None,
        reviews=reviews,
        change_set=change_set,
        status=ApplyFileOutcomeStatus.APPLIED,
        transaction_result=FileApplyResult(
            source_path=source.path,
            final_path=source.path,
            status=FileApplyStatus.SUCCEEDED,
            completed_stage=FileTransactionStage.COMPLETED,
        ),
    )


def make_result(
    started: SessionState,
    groups: tuple[ApplyGroupOutcome, ...],
    *,
    operation_id: str = "APPLY-1",
) -> ApplyBatchResult:
    active = started.active_operation
    assert active is not None

    return ApplyBatchResult(
        operation_id=operation_id,
        base_session_revision=active.base_session_revision,
        base_library_revision=active.base_library_revision,
        status=ApplyBatchStatus.SUCCEEDED,
        groups=groups,
        report_result=ReportWriteResult(ReportWriteStatus.DISABLED, None, None),
    )


def test_exact_apply_invalidates_only_groups_with_proven_filesystem_effects() -> None:
    first = make_file("a-1", "library/A/01.flac")
    second = make_file("a-2", "library/A/02.flac")
    unchanged = make_file("b-1", "library/B/01.flac")
    group_a = make_group_state("group-a", (first, second), 2, language_override="ja")
    group_b = make_group_state("group-b", (unchanged,), 3, language_override="ko")
    issue = Issue(MediaErrorCode.TAG_READ_FAILED, "An earlier scan issue remains visible.")
    base = SessionState(
        root=Path("library"),
        groups=(group_a, group_b),
        unsupported_files=(UnsupportedMediaFile(Path("library/unknown.ape")),),
        scan_issues=(issue,),
        selection=GroupSelection("group-b"),
        revision=5,
        library_revision=1,
    )
    started = begin_operation(base, "APPLY-1", OperationKind.APPLY, ("group-a", "group-b"))
    result = make_result(
        started,
        (
            ApplyGroupOutcome(
                "group-a",
                2,
                (
                    make_outcome(first, filesystem_effect=True),
                    make_outcome(second, filesystem_effect=True),
                ),
            ),
            ApplyGroupOutcome(
                "group-b",
                3,
                (make_outcome(unchanged, filesystem_effect=False),),
            ),
        ),
    )

    reduced = apply_batch_result(started, result)

    assert reduced.status is ResultApplicationStatus.APPLIED
    assert reduced.reason is None
    assert reduced.state.groups[0].requires_rescan
    assert reduced.state.groups[0].reviewed_files == ()
    assert reduced.state.groups[0].language_override == "ja"
    assert reduced.state.groups[0].revision == 3
    assert reduced.state.groups[1] is started.groups[1]
    assert reduced.state.revision == 6
    assert reduced.state.library_revision == 1
    assert reduced.state.root == started.root
    assert reduced.state.unsupported_files == started.unsupported_files
    assert reduced.state.scan_issues == started.scan_issues
    assert reduced.state.selection == started.selection
    assert reduced.state.provider_cache is started.provider_cache
    assert reduced.state.active_operation is started.active_operation


def test_exact_no_effect_result_tolerates_unrelated_session_revision_drift() -> None:
    source = make_file("file-1", "library/A/01.flac")
    group = make_group_state("group-a", (source,), 2)
    base = SessionState(root=Path("library"), groups=(group,), revision=5, library_revision=1)
    started = begin_operation(base, "APPLY-1", OperationKind.APPLY, ("group-a",))
    current = replace(started, revision=6)
    result = make_result(
        started,
        (
            ApplyGroupOutcome(
                "group-a",
                2,
                (make_outcome(source, filesystem_effect=False),),
            ),
        ),
    )

    reduced = apply_batch_result(current, result)

    assert reduced.status is ResultApplicationStatus.APPLIED
    assert reduced.state is current


def test_stale_regrouped_file_is_invalidated_in_its_current_group() -> None:
    moved = make_file("moved", "library/A/01.flac")
    anchor_a = make_file("anchor-a", "library/A/02.flac")
    anchor_b = make_file("anchor-b", "library/B/01.flac")
    original_a = make_group_state("group-a", (moved, anchor_a), 2)
    original_b = make_group_state("group-b", (anchor_b,), 3)
    base = SessionState(
        root=Path("library"),
        groups=(original_a, original_b),
        revision=5,
        library_revision=1,
    )
    started = begin_operation(base, "APPLY-1", OperationKind.APPLY, ("group-a",))
    current_a = make_group_state("group-a", (anchor_a,), 3)
    current_b = make_group_state("group-b", (moved, anchor_b), 4, language_override="ja")
    current = replace(started, groups=(current_a, current_b), revision=6)
    result = make_result(
        started,
        (
            ApplyGroupOutcome(
                "group-a",
                2,
                (make_outcome(moved, filesystem_effect=True),),
            ),
        ),
    )

    reduced = apply_batch_result(current, result)

    assert reduced.status is ResultApplicationStatus.STALE
    assert reduced.reason is StaleResultReason.GROUP_REVISION_CHANGED
    assert reduced.state.groups[0] is current_a
    assert reduced.state.groups[1].requires_rescan
    assert reduced.state.groups[1].language_override == "ja"
    assert reduced.state.groups[1].revision == 5
    assert reduced.state.revision == 7
    assert reduced.state.active_operation is current.active_operation


def test_stale_effect_requires_the_exact_current_file_id_and_path_pair() -> None:
    original = make_file("stable-id", "library/A/01.flac")
    original_group = make_group_state("group-a", (original,), 2)
    base = SessionState(
        root=Path("library"),
        groups=(original_group,),
        revision=5,
        library_revision=1,
    )
    started = begin_operation(base, "APPLY-1", OperationKind.APPLY, ("group-a",))
    same_id = make_file("stable-id", "library/A/moved.flac")
    same_path = make_file("different-id", "library/A/01.flac")
    current_a = make_group_state("group-a", (same_id,), 3)
    current_b = make_group_state("group-b", (same_path,), 0)
    current = replace(started, groups=(current_a, current_b), revision=6)
    result = make_result(
        started,
        (
            ApplyGroupOutcome(
                "group-a",
                2,
                (make_outcome(original, filesystem_effect=True),),
            ),
        ),
    )

    reduced = apply_batch_result(current, result)

    assert reduced.status is ResultApplicationStatus.STALE
    assert reduced.reason is StaleResultReason.GROUP_REVISION_CHANGED
    assert reduced.state is current


def test_library_drift_still_invalidates_an_exact_current_file_pair() -> None:
    source = make_file("file-1", "library/A/01.flac")
    group = make_group_state("group-a", (source,), 2)
    base = SessionState(root=Path("library"), groups=(group,), revision=5, library_revision=1)
    started = begin_operation(base, "APPLY-1", OperationKind.APPLY, ("group-a",))
    current = replace(started, revision=6, library_revision=2)
    result = make_result(
        started,
        (
            ApplyGroupOutcome(
                "group-a",
                2,
                (make_outcome(source, filesystem_effect=True),),
            ),
        ),
    )

    reduced = apply_batch_result(current, result)

    assert reduced.status is ResultApplicationStatus.STALE
    assert reduced.reason is StaleResultReason.LIBRARY_REVISION_CHANGED
    assert reduced.state.groups[0].requires_rescan
    assert reduced.state.library_revision == 2
    assert reduced.state.active_operation is current.active_operation


def test_missing_old_target_still_invalidates_exact_pair_in_a_new_group() -> None:
    source = make_file("file-1", "library/A/01.flac")
    old_group = make_group_state("group-a", (source,), 2)
    base = SessionState(
        root=Path("library"),
        groups=(old_group,),
        revision=5,
        library_revision=1,
    )
    started = begin_operation(base, "APPLY-1", OperationKind.APPLY, ("group-a",))
    new_group = make_group_state("group-new", (source,), 0, language_override="ja")
    current = replace(
        started,
        groups=(new_group,),
        revision=6,
        library_revision=2,
    )
    result = make_result(
        started,
        (
            ApplyGroupOutcome(
                "group-a",
                2,
                (make_outcome(source, filesystem_effect=True),),
            ),
        ),
    )

    reduced = apply_batch_result(current, result)

    assert reduced.status is ResultApplicationStatus.STALE
    assert reduced.reason is StaleResultReason.GROUP_MISSING
    assert reduced.state.groups[0].requires_rescan
    assert reduced.state.groups[0].language_override == "ja"


def test_late_operation_mismatch_still_reconciles_proven_disk_effects() -> None:
    source = make_file("file-1", "library/A/01.flac")
    group = make_group_state("group-a", (source,), 2)
    base = SessionState(root=Path("library"), groups=(group,), revision=5, library_revision=1)
    started = begin_operation(base, "APPLY-1", OperationKind.APPLY, ("group-a",))
    finished = finish_operation(started, "APPLY-1")
    newer = begin_operation(finished, "SCAN-2", OperationKind.SCAN, ())
    result = make_result(
        started,
        (
            ApplyGroupOutcome(
                "group-a",
                2,
                (make_outcome(source, filesystem_effect=True),),
            ),
        ),
    )

    reduced = apply_batch_result(newer, result)

    assert reduced.status is ResultApplicationStatus.STALE
    assert reduced.reason is StaleResultReason.OPERATION_MISMATCH
    assert reduced.state.groups[0].requires_rescan
    assert reduced.state.active_operation is newer.active_operation

    repeated = apply_batch_result(reduced.state, result)

    assert repeated.status is ResultApplicationStatus.STALE
    assert repeated.reason is StaleResultReason.OPERATION_MISMATCH
    assert repeated.state is reduced.state
    assert repeated.state.groups[0] is reduced.state.groups[0]


def test_apply_target_order_mismatch_is_stale_but_still_reconciles_effects() -> None:
    source_a = make_file("a", "library/A/01.flac")
    source_b = make_file("b", "library/B/01.flac")
    group_a = make_group_state("group-a", (source_a,), 2)
    group_b = make_group_state("group-b", (source_b,), 3)
    base = SessionState(
        root=Path("library"),
        groups=(group_a, group_b),
        revision=5,
        library_revision=1,
    )
    started = begin_operation(base, "APPLY-1", OperationKind.APPLY, ("group-a", "group-b"))
    result = make_result(
        started,
        (
            ApplyGroupOutcome(
                "group-b",
                3,
                (make_outcome(source_b, filesystem_effect=False),),
            ),
            ApplyGroupOutcome(
                "group-a",
                2,
                (make_outcome(source_a, filesystem_effect=True),),
            ),
        ),
    )

    reduced = apply_batch_result(started, result)

    assert reduced.status is ResultApplicationStatus.STALE
    assert reduced.reason is StaleResultReason.OPERATION_MISMATCH
    assert reduced.state.groups[0].requires_rescan
    assert reduced.state.groups[1] is started.groups[1]


def test_mislabelled_current_group_is_stale_but_effects_are_reconciled_globally() -> None:
    source_a = make_file("a", "library/A/01.flac")
    source_b = make_file("b", "library/B/01.flac")
    group_a = make_group_state("group-a", (source_a,), 2)
    group_b = make_group_state("group-b", (source_b,), 3)
    base = SessionState(
        root=Path("library"),
        groups=(group_a, group_b),
        revision=5,
        library_revision=1,
    )
    started = begin_operation(base, "APPLY-1", OperationKind.APPLY, ("group-a", "group-b"))
    result = make_result(
        started,
        (
            ApplyGroupOutcome(
                "group-a",
                2,
                (make_outcome(source_b, filesystem_effect=True),),
            ),
            ApplyGroupOutcome(
                "group-b",
                3,
                (make_outcome(source_a, filesystem_effect=True),),
            ),
        ),
    )

    reduced = apply_batch_result(started, result)

    assert reduced.status is ResultApplicationStatus.STALE
    assert reduced.reason is StaleResultReason.OPERATION_MISMATCH
    assert reduced.state.groups[0].requires_rescan
    assert reduced.state.groups[1].requires_rescan
    assert reduced.state.revision == 6
