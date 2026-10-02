# Injected probes and writers expose the ordering of preflight and transactions.
# Failures can then be placed at exact boundaries without changing real music files.

from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from metadata_polisher.application.apply import (
    ApplyBatchRequest,
    ApplyBatchResult,
    ApplyBatchStatus,
    ApplyFileOutcome,
    ApplyFileOutcomeStatus,
    ApplyFileRequest,
    ApplyGroupOutcome,
    ApplyGroupRequest,
    ApplyService,
    ApplySkipReason,
    ApplyStage,
    FileCapacitySnapshot,
    FilePreflightSnapshot,
    LocalApplyPathProbe,
    LocalApplyPreflightInspector,
    PathStorageSnapshot,
)
from metadata_polisher.application.changes import (
    ChangeIssueCode,
    ChangeSetStatus,
    ChangeValidationFacts,
    FileChangeSet,
    RenameDecision,
    build_change_set,
)
from metadata_polisher.application.review import (
    build_field_review_state,
    set_manual_decision,
)
from metadata_polisher.domain.errors import Issue, MediaErrorCode
from metadata_polisher.domain.media import (
    FilenameHints,
    LocalMediaFile,
    MediaReadResult,
    StreamInfo,
)
from metadata_polisher.domain.metadata import (
    FieldReadState,
    MetadataField,
    MetadataSnapshot,
    Position,
)
from metadata_polisher.domain.review import FieldReviewState
from metadata_polisher.execution.cancellation import (
    MutableCancellationToken,
    NeverCancelledToken,
)
from metadata_polisher.execution.events import (
    FileApplyStatus,
    FileSkipped,
    FileTransactionStage,
    OperationEvent,
    OperationProgress,
    OperationStageChanged,
)
from metadata_polisher.infrastructure.reporting import (
    ProcessingReportRequest,
    ReportErrorCode,
    ReportFileStatus,
    ReportOperationResult,
    ReportOutputPolicy,
    ReportRenameResult,
    ReportWriteResult,
    ReportWriteStatus,
    SelectedReleaseReference,
)
from metadata_polisher.infrastructure.transaction import (
    BackupPolicy,
    FileApplyResult,
)
from metadata_polisher.rename.template import FilenameRenderPolicy


@dataclass
class RecordingEventSink:
    events: list[OperationEvent]

    def emit(self, event: OperationEvent) -> None:
        self.events.append(event)


class StubAdapter:
    format_id = "flac"
    extensions = frozenset({".flac"})


_DEFAULT_TEST_VALIDATION = ChangeValidationFacts()
_DEFAULT_TEST_RENAME_POLICY = FilenameRenderPolicy()


def make_preflight_snapshot(
    source: LocalMediaFile,
    adapter: StubAdapter,
    validation: ChangeValidationFacts = _DEFAULT_TEST_VALIDATION,
) -> FilePreflightSnapshot:
    return FilePreflightSnapshot(
        file_id=source.file_id,
        adapter=adapter,
        validation=validation,
        capacity=FileCapacitySnapshot(
            source_size_bytes=1,
            temporary_storage=PathStorageSnapshot("test-source", 1_000_000),
        ),
    )


def make_source(file_id: str, path: str, title: str) -> LocalMediaFile:
    metadata = MetadataSnapshot(
        title=title,
        artists=("Artist",),
        album="Album",
        album_artists=("Album Artist",),
        composers=("Composer",),
        track=Position(number=1, total=2),
        disc=Position(number=1, total=1),
        date="2026",
        genres=("Soundtrack",),
    )

    return LocalMediaFile(
        path=Path(path),
        format_id="flac",
        read_result=MediaReadResult(
            metadata=metadata,
            field_states={field: FieldReadState.PRESENT for field in MetadataField},
            stream_info=StreamInfo(180.0, 44_100, 2, 16, "FLAC"),
        ),
        filename_hints=FilenameHints(),
        file_id=file_id,
    )


def make_reviews(source: LocalMediaFile, new_title: str) -> tuple[FieldReviewState, ...]:
    reviews = []

    for field in MetadataField:
        review = build_field_review_state(
            field=field,
            read_state=FieldReadState.PRESENT,
            existing_value=getattr(source.read_result.metadata, field.value),
            proposals=(),
        )

        if field is MetadataField.TITLE:
            review = set_manual_decision(review, new_title)

        reviews.append(review)

    return tuple(reviews)


def make_file_request(
    file_id: str,
    path: str,
    *,
    new_title: str | None = None,
    rename_decision: RenameDecision = RenameDecision.KEEP_FILENAME,
) -> ApplyFileRequest:
    source = make_source(file_id, path, f"Old {file_id}")

    return ApplyFileRequest(
        source=source,
        reviews=make_reviews(source, new_title or f"New {file_id}"),
        rename_decision=rename_decision,
        track_mapping_resolved=True,
    )


def make_batch(
    groups: tuple[ApplyGroupRequest, ...],
    *,
    rename_template: str = "%title%",
    rename_policy: FilenameRenderPolicy = _DEFAULT_TEST_RENAME_POLICY,
) -> ApplyBatchRequest:
    return ApplyBatchRequest(
        operation_id="APPLY-1",
        base_session_revision=9,
        base_library_revision=4,
        scan_root=Path("library"),
        groups=groups,
        backup=BackupPolicy(False, None, Path("library"), "APPLY-1"),
        report=ReportOutputPolicy(enabled=False),
        rename_template=rename_template,
        rename_policy=rename_policy,
    )


def test_apply_preflights_the_whole_ordered_batch_before_writing() -> None:
    call_order: list[str] = []
    adapter = StubAdapter()

    class RecordingPreflight:
        def inspect(
            self,
            source: LocalMediaFile,
            proposed_changes: FileChangeSet,
            backup: BackupPolicy,
        ) -> FilePreflightSnapshot:
            assert proposed_changes.file_id == source.file_id
            assert backup.operation_id == "APPLY-1"
            call_order.append(f"preflight:{source.file_id}")

            return make_preflight_snapshot(source, adapter)

    class RecordingWriter:
        def apply_file(
            self,
            source: LocalMediaFile,
            changes: FileChangeSet,
            adapter: object,
            backup: BackupPolicy,
            cancellation: NeverCancelledToken,
            events: RecordingEventSink,
        ) -> FileApplyResult:
            del adapter, backup, cancellation, events
            assert changes.file_id == source.file_id
            call_order.append(f"write:{source.file_id}")

            return FileApplyResult(
                source_path=source.path,
                final_path=source.path,
                status=FileApplyStatus.SUCCEEDED,
                completed_stage=FileTransactionStage.COMPLETED,
            )

    class ForbiddenReportWriter:
        def write(self, **_kwargs: object) -> ReportWriteResult:
            raise AssertionError("disabled reporting must not call the report writer")

    files = (
        make_file_request("file-b2", "library/b/02.flac"),
        make_file_request("file-b1", "library/b/01.flac"),
        make_file_request("file-a1", "library/a/01.flac"),
    )
    release = SelectedReleaseReference("musicbrainz", "musicbrainz", "release-1", 0)
    request = make_batch(
        (
            ApplyGroupRequest("group-b", 7, release, files[:2]),
            ApplyGroupRequest("group-a", 6, release, files[2:]),
        ),
        rename_template="[%discnumber%]%tracknumber% - %title%",
        rename_policy=FilenameRenderPolicy(minimum_track_digits=3),
    )
    events = RecordingEventSink([])

    result = ApplyService(
        preflight=RecordingPreflight(),
        writer=RecordingWriter(),
        report_writer=ForbiddenReportWriter(),
    ).apply(
        request,
        cancellation=NeverCancelledToken(),
        events=events,
    )

    assert call_order == [
        "preflight:file-b2",
        "preflight:file-b1",
        "preflight:file-a1",
        "write:file-b2",
        "write:file-b1",
        "write:file-a1",
    ]
    assert result.status is ApplyBatchStatus.SUCCEEDED
    assert result.operation_id == "APPLY-1"
    assert result.base_session_revision == 9
    assert result.base_library_revision == 4
    assert tuple(group.group_id for group in result.groups) == ("group-b", "group-a")
    assert tuple(group.base_group_revision for group in result.groups) == (7, 6)
    assert tuple(file.file_id for group in result.groups for file in group.files) == (
        "file-b2",
        "file-b1",
        "file-a1",
    )
    assert all(
        file.status is ApplyFileOutcomeStatus.APPLIED
        for group in result.groups
        for file in group.files
    )
    assert result.report_result == ReportWriteResult(ReportWriteStatus.DISABLED, None, None)
    assert events.events == [
        OperationStageChanged("APPLY-1", ApplyStage.PREFLIGHTING),
        OperationProgress("APPLY-1", ApplyStage.PREFLIGHTING, 0, 3),
        OperationProgress("APPLY-1", ApplyStage.PREFLIGHTING, 1, 3),
        OperationProgress("APPLY-1", ApplyStage.PREFLIGHTING, 2, 3),
        OperationProgress("APPLY-1", ApplyStage.PREFLIGHTING, 3, 3),
        OperationStageChanged("APPLY-1", ApplyStage.APPLYING_FILES),
        OperationProgress("APPLY-1", ApplyStage.APPLYING_FILES, 0, 3),
        OperationProgress("APPLY-1", ApplyStage.APPLYING_FILES, 1, 3),
        OperationProgress("APPLY-1", ApplyStage.APPLYING_FILES, 2, 3),
        OperationProgress("APPLY-1", ApplyStage.APPLYING_FILES, 3, 3),
        OperationStageChanged("APPLY-1", ApplyStage.REFRESHING_FILES),
        OperationProgress("APPLY-1", ApplyStage.REFRESHING_FILES, 0, 3),
        OperationProgress("APPLY-1", ApplyStage.REFRESHING_FILES, 1, 3),
        OperationProgress("APPLY-1", ApplyStage.REFRESHING_FILES, 2, 3),
        OperationProgress("APPLY-1", ApplyStage.REFRESHING_FILES, 3, 3),
    ]


def test_one_blocked_fresh_preflight_prevents_every_batch_write() -> None:
    inspected: list[str] = []
    writer_calls: list[str] = []
    adapter = StubAdapter()

    class PartlyBlockedPreflight:
        def inspect(
            self,
            source: LocalMediaFile,
            proposed_changes: FileChangeSet,
            backup: BackupPolicy,
        ) -> FilePreflightSnapshot:
            del proposed_changes, backup
            inspected.append(source.file_id)

            return make_preflight_snapshot(
                source,
                adapter,
                ChangeValidationFacts(source_readable=source.file_id != "file-2"),
            )

    class ForbiddenWriter:
        def apply_file(self, source: LocalMediaFile, *_args: object) -> FileApplyResult:
            writer_calls.append(source.file_id)
            raise AssertionError("no writer may run after a batch preflight blocker")

    release = SelectedReleaseReference("musicbrainz", "musicbrainz", "release-1", 0)
    request = make_batch(
        (
            ApplyGroupRequest(
                "group-1",
                7,
                release,
                (
                    make_file_request("file-1", "library/a/01.flac"),
                    make_file_request("file-2", "library/a/02.flac"),
                ),
            ),
        )
    )

    result = ApplyService(
        preflight=PartlyBlockedPreflight(),
        writer=ForbiddenWriter(),  # type: ignore[arg-type]
    ).apply(request, cancellation=NeverCancelledToken())

    assert inspected == ["file-1", "file-2"]
    assert writer_calls == []
    assert result.status is ApplyBatchStatus.FAILED
    assert tuple(file.status for file in result.groups[0].files) == (
        ApplyFileOutcomeStatus.SKIPPED,
        ApplyFileOutcomeStatus.SKIPPED,
    )
    assert tuple(file.skip_reason for file in result.groups[0].files) == (
        ApplySkipReason.BATCH_PREFLIGHT_BLOCKED,
        ApplySkipReason.BATCH_PREFLIGHT_BLOCKED,
    )
    assert result.groups[0].files[0].change_set.status is ChangeSetStatus.VALID
    assert tuple(
        issue.code for issue in result.groups[0].files[1].change_set.validation.issues
    ) == (ChangeIssueCode.SOURCE_NOT_READABLE,)


def test_batch_blocker_preserves_true_no_change_outcome() -> None:
    adapter = StubAdapter()

    class BlockedPreflight:
        def inspect(
            self,
            source: LocalMediaFile,
            proposed_changes: FileChangeSet,
            backup: BackupPolicy,
        ) -> FilePreflightSnapshot:
            del proposed_changes, backup

            return make_preflight_snapshot(
                source,
                adapter,
                ChangeValidationFacts(directory_writable=False),
            )

    class ForbiddenWriter:
        def apply_file(self, *_args: object, **_kwargs: object) -> FileApplyResult:
            raise AssertionError("blocked batch must not enter the writer")

    no_change = make_file_request(
        "no-change",
        "library/a/00.flac",
        new_title="Old no-change",
    )
    request = make_batch(
        (
            ApplyGroupRequest(
                "group-1",
                7,
                None,
                (
                    no_change,
                    make_file_request("blocked", "library/a/01.flac"),
                ),
            ),
        )
    )
    events = RecordingEventSink([])

    result = ApplyService(
        preflight=BlockedPreflight(),
        writer=ForbiddenWriter(),  # type: ignore[arg-type]
    ).apply(request, cancellation=NeverCancelledToken(), events=events)

    assert result.status is ApplyBatchStatus.FAILED
    assert result.groups[0].files[0].status is ApplyFileOutcomeStatus.NO_CHANGES
    assert result.groups[0].files[0].skip_reason is None
    assert result.groups[0].files[1].status is ApplyFileOutcomeStatus.SKIPPED
    assert result.groups[0].files[1].skip_reason is ApplySkipReason.BATCH_PREFLIGHT_BLOCKED
    assert [
        (event.file_id, event.reason)
        for event in events.events
        if isinstance(event, FileSkipped)
    ] == [
        ("no-change", ApplySkipReason.NO_CHANGES),
        ("blocked", ApplySkipReason.BATCH_PREFLIGHT_BLOCKED),
    ]


def test_case_insensitive_cross_request_destination_collision_blocks_whole_batch() -> None:
    inspected_destinations: list[Path] = []
    adapter = StubAdapter()

    class EmptyDirectoryPreflight:
        def inspect(
            self,
            source: LocalMediaFile,
            proposed_changes: FileChangeSet,
            backup: BackupPolicy,
        ) -> FilePreflightSnapshot:
            del backup
            assert proposed_changes.rename_change is not None
            inspected_destinations.append(proposed_changes.rename_change.new_path)

            return make_preflight_snapshot(source, adapter)

    class ForbiddenWriter:
        def apply_file(self, *_args: object, **_kwargs: object) -> FileApplyResult:
            raise AssertionError("a cross-request collision must block before writer call 1")

    release = SelectedReleaseReference("vgmdb", "vgmdb", "album-1", 0)
    request = make_batch(
        (
            ApplyGroupRequest(
                "group-1",
                8,
                release,
                (
                    make_file_request(
                        "file-1",
                        "library/a/01.flac",
                        new_title="Same Name",
                        rename_decision=RenameDecision.APPLY_RENAME,
                    ),
                    make_file_request(
                        "file-2",
                        "library/a/02.flac",
                        new_title="same name",
                        rename_decision=RenameDecision.APPLY_RENAME,
                    ),
                ),
            ),
        ),
        rename_template="%title%",
    )

    result = ApplyService(
        preflight=EmptyDirectoryPreflight(),
        writer=ForbiddenWriter(),  # type: ignore[arg-type]
    ).apply(request, cancellation=NeverCancelledToken())

    assert inspected_destinations == [
        Path("library/a/Same Name.flac"),
        Path("library/a/same name.flac"),
    ]
    assert result.status is ApplyBatchStatus.FAILED
    assert all(
        outcome.skip_reason is ApplySkipReason.BATCH_PREFLIGHT_BLOCKED
        for outcome in result.groups[0].files
    )
    assert all(
        outcome.change_set.status is ChangeSetStatus.BLOCKED
        and ChangeIssueCode.DESTINATION_COLLISION
        in {issue.code for issue in outcome.change_set.validation.issues}
        for outcome in result.groups[0].files
    )


def test_no_change_is_explicit_and_never_enters_preflight_or_writer() -> None:
    inspected: list[str] = []
    written: list[str] = []
    adapter = StubAdapter()

    class RecordingPreflight:
        def inspect(
            self,
            source: LocalMediaFile,
            proposed_changes: FileChangeSet,
            backup: BackupPolicy,
        ) -> FilePreflightSnapshot:
            del proposed_changes, backup
            inspected.append(source.file_id)

            return make_preflight_snapshot(source, adapter)

    class RecordingWriter:
        def apply_file(
            self,
            source: LocalMediaFile,
            changes: FileChangeSet,
            adapter: object,
            backup: BackupPolicy,
            cancellation: NeverCancelledToken,
            events: RecordingEventSink,
        ) -> FileApplyResult:
            del changes, adapter, backup, cancellation, events
            written.append(source.file_id)

            return FileApplyResult(
                source.path,
                source.path,
                FileApplyStatus.SUCCEEDED,
                FileTransactionStage.COMPLETED,
            )

    no_change = make_file_request(
        "file-no-change",
        "library/a/01.flac",
        new_title="Old file-no-change",
    )
    changed = make_file_request("file-changed", "library/a/02.flac")
    request = make_batch(
        (
            ApplyGroupRequest("group-1", 7, None, (no_change, changed)),
        )
    )

    result = ApplyService(
        preflight=RecordingPreflight(),
        writer=RecordingWriter(),
    ).apply(request, cancellation=NeverCancelledToken())

    assert inspected == ["file-changed"]
    assert written == ["file-changed"]
    assert result.status is ApplyBatchStatus.SUCCEEDED
    assert result.groups[0].files[0].status is ApplyFileOutcomeStatus.NO_CHANGES
    assert result.groups[0].files[0].transaction_result is None
    assert not result.groups[0].files[0].filesystem_changed


def test_first_file_failure_stops_only_its_album_then_later_album_continues() -> None:
    writer_calls: list[str] = []
    adapter = StubAdapter()

    class ValidPreflight:
        def inspect(
            self,
            source: LocalMediaFile,
            proposed_changes: FileChangeSet,
            backup: BackupPolicy,
        ) -> FilePreflightSnapshot:
            del proposed_changes, backup

            return make_preflight_snapshot(source, adapter)

    class FirstFileFailsWriter:
        def apply_file(
            self,
            source: LocalMediaFile,
            changes: FileChangeSet,
            adapter: object,
            backup: BackupPolicy,
            cancellation: NeverCancelledToken,
            events: RecordingEventSink,
        ) -> FileApplyResult:
            del changes, adapter, backup, cancellation, events
            writer_calls.append(source.file_id)

            if source.file_id == "album-1-file-1":
                return FileApplyResult(
                    source.path,
                    source.path,
                    FileApplyStatus.FAILED,
                    FileTransactionStage.WRITING_METADATA,
                    (Issue(MediaErrorCode.TAG_WRITE_FAILED, "The tag write failed."),),
                )

            return FileApplyResult(
                source.path,
                source.path,
                FileApplyStatus.SUCCEEDED,
                FileTransactionStage.COMPLETED,
            )

    request = make_batch(
        (
            ApplyGroupRequest(
                "group-1",
                7,
                None,
                (
                    make_file_request("album-1-file-1", "library/a/01.flac"),
                    make_file_request("album-1-file-2", "library/a/02.flac"),
                ),
            ),
            ApplyGroupRequest(
                "group-2",
                8,
                None,
                (make_file_request("album-2-file-1", "library/b/01.flac"),),
            ),
        )
    )

    result = ApplyService(
        preflight=ValidPreflight(),
        writer=FirstFileFailsWriter(),
    ).apply(request, cancellation=NeverCancelledToken())

    assert writer_calls == ["album-1-file-1", "album-2-file-1"]
    assert result.status is ApplyBatchStatus.PARTIAL
    assert tuple(file.status for file in result.groups[0].files) == (
        ApplyFileOutcomeStatus.FAILED,
        ApplyFileOutcomeStatus.SKIPPED,
    )
    assert (
        result.groups[0].files[1].skip_reason
        is ApplySkipReason.ALBUM_STOPPED_AFTER_FAILURE
    )
    assert result.groups[1].files[0].status is ApplyFileOutcomeStatus.APPLIED
    assert not result.groups[0].files[0].filesystem_changed
    assert result.groups[1].files[0].filesystem_changed


def test_cancellation_after_a_successful_file_skips_every_later_file() -> None:
    writer_calls: list[str] = []
    token = MutableCancellationToken()
    adapter = StubAdapter()

    class ValidPreflight:
        def inspect(
            self,
            source: LocalMediaFile,
            proposed_changes: FileChangeSet,
            backup: BackupPolicy,
        ) -> FilePreflightSnapshot:
            del proposed_changes, backup

            return make_preflight_snapshot(source, adapter)

    class CancellingWriter:
        def apply_file(
            self,
            source: LocalMediaFile,
            changes: FileChangeSet,
            adapter: object,
            backup: BackupPolicy,
            cancellation: MutableCancellationToken,
            events: RecordingEventSink,
        ) -> FileApplyResult:
            del changes, adapter, backup, cancellation, events
            writer_calls.append(source.file_id)
            token.cancel()

            return FileApplyResult(
                source.path,
                source.path,
                FileApplyStatus.SUCCEEDED,
                FileTransactionStage.COMPLETED,
            )

    request = make_batch(
        (
            ApplyGroupRequest(
                "group-1",
                7,
                None,
                (
                    make_file_request("file-1", "library/a/01.flac"),
                    make_file_request("file-2", "library/a/02.flac"),
                ),
            ),
            ApplyGroupRequest(
                "group-2",
                8,
                None,
                (make_file_request("file-3", "library/b/01.flac"),),
            ),
        )
    )

    result = ApplyService(
        preflight=ValidPreflight(),
        writer=CancellingWriter(),
    ).apply(request, cancellation=token)

    assert writer_calls == ["file-1"]
    assert result.status is ApplyBatchStatus.CANCELLED
    assert tuple(file.status for group in result.groups for file in group.files) == (
        ApplyFileOutcomeStatus.APPLIED,
        ApplyFileOutcomeStatus.SKIPPED,
        ApplyFileOutcomeStatus.SKIPPED,
    )
    assert tuple(file.skip_reason for group in result.groups for file in group.files) == (
        None,
        ApplySkipReason.CANCELLED_BEFORE_START,
        ApplySkipReason.CANCELLED_BEFORE_START,
    )


def test_token_set_by_the_final_success_does_not_relabel_the_batch_cancelled() -> None:
    token = MutableCancellationToken()
    adapter = StubAdapter()

    class ValidPreflight:
        def inspect(
            self,
            source: LocalMediaFile,
            proposed_changes: FileChangeSet,
            backup: BackupPolicy,
        ) -> FilePreflightSnapshot:
            del proposed_changes, backup

            return make_preflight_snapshot(source, adapter)

    class LateTokenWriter:
        def apply_file(
            self,
            source: LocalMediaFile,
            changes: FileChangeSet,
            adapter: object,
            backup: BackupPolicy,
            cancellation: MutableCancellationToken,
            events: RecordingEventSink,
        ) -> FileApplyResult:
            del changes, adapter, backup, cancellation, events
            token.cancel()

            return FileApplyResult(
                source.path,
                source.path,
                FileApplyStatus.SUCCEEDED,
                FileTransactionStage.COMPLETED,
            )

    request = make_batch(
        (
            ApplyGroupRequest(
                "group-1",
                7,
                None,
                (make_file_request("only-file", "library/a/01.flac"),),
            ),
        )
    )

    result = ApplyService(
        preflight=ValidPreflight(),
        writer=LateTokenWriter(),
    ).apply(request, cancellation=token)

    assert result.status is ApplyBatchStatus.SUCCEEDED
    assert result.groups[0].files[0].status is ApplyFileOutcomeStatus.APPLIED


def test_cancelled_transaction_stops_the_complete_batch() -> None:
    writer_calls: list[str] = []
    adapter = StubAdapter()

    class ValidPreflight:
        def inspect(
            self,
            source: LocalMediaFile,
            proposed_changes: FileChangeSet,
            backup: BackupPolicy,
        ) -> FilePreflightSnapshot:
            del proposed_changes, backup

            return make_preflight_snapshot(source, adapter)

    class CancelledWriter:
        def apply_file(
            self,
            source: LocalMediaFile,
            changes: FileChangeSet,
            adapter: object,
            backup: BackupPolicy,
            cancellation: NeverCancelledToken,
            events: RecordingEventSink,
        ) -> FileApplyResult:
            del changes, adapter, backup, cancellation, events
            writer_calls.append(source.file_id)

            return FileApplyResult(
                source.path,
                source.path,
                FileApplyStatus.CANCELLED,
                FileTransactionStage.COPYING_TEMPORARY,
            )

    request = make_batch(
        (
            ApplyGroupRequest(
                "group-1",
                7,
                None,
                (
                    make_file_request("file-1", "library/a/01.flac"),
                    make_file_request("file-2", "library/a/02.flac"),
                ),
            ),
        )
    )

    result = ApplyService(
        preflight=ValidPreflight(),
        writer=CancelledWriter(),
    ).apply(request, cancellation=NeverCancelledToken())

    assert writer_calls == ["file-1"]
    assert result.status is ApplyBatchStatus.CANCELLED
    assert tuple(file.status for file in result.groups[0].files) == (
        ApplyFileOutcomeStatus.CANCELLED,
        ApplyFileOutcomeStatus.SKIPPED,
    )
    assert result.groups[0].files[1].skip_reason is ApplySkipReason.CANCELLED_BEFORE_START


def test_enabled_report_runs_once_after_partial_outcomes_and_failure_is_separate() -> None:
    call_order: list[str] = []
    captured_requests: list[ProcessingReportRequest] = []
    adapter = StubAdapter()

    class ValidPreflight:
        def inspect(
            self,
            source: LocalMediaFile,
            proposed_changes: FileChangeSet,
            backup: BackupPolicy,
        ) -> FilePreflightSnapshot:
            del proposed_changes, backup

            return make_preflight_snapshot(source, adapter)

    class PartialWriter:
        def apply_file(
            self,
            source: LocalMediaFile,
            changes: FileChangeSet,
            adapter: object,
            backup: BackupPolicy,
            cancellation: NeverCancelledToken,
            events: RecordingEventSink,
        ) -> FileApplyResult:
            del changes, adapter, backup, cancellation, events
            call_order.append(f"write:{source.file_id}")

            if source.file_id == "failed-file":
                return FileApplyResult(
                    source.path,
                    source.path,
                    FileApplyStatus.FAILED,
                    FileTransactionStage.WRITING_METADATA,
                    (Issue(MediaErrorCode.TAG_WRITE_FAILED, "The tag write failed."),),
                )

            return FileApplyResult(
                source.path,
                source.path,
                FileApplyStatus.SUCCEEDED,
                FileTransactionStage.COMPLETED,
            )

    class FailingReportWriter:
        def write(
            self,
            *,
            policy: ReportOutputPolicy,
            request: ProcessingReportRequest,
        ) -> ReportWriteResult:
            assert policy == ReportOutputPolicy(enabled=True, directory="reports")
            call_order.append("report")
            captured_requests.append(request)

            return ReportWriteResult(
                ReportWriteStatus.FAILED,
                None,
                ReportErrorCode.WRITE_FAILED,
            )

    release_1 = SelectedReleaseReference("musicbrainz", "musicbrainz", "release-1", 0)
    release_2 = SelectedReleaseReference("vgmdb", "vgmdb", "album-2", 1)
    base = make_batch(
        (
            ApplyGroupRequest(
                "group-1",
                7,
                release_1,
                (
                    make_file_request("failed-file", "library/a/01.flac"),
                    make_file_request("album-skipped-file", "library/a/02.flac"),
                ),
            ),
            ApplyGroupRequest(
                "group-2",
                8,
                release_2,
                (make_file_request("applied-file", "library/b/01.flac"),),
            ),
        )
    )
    request = ApplyBatchRequest(
        operation_id=base.operation_id,
        base_session_revision=base.base_session_revision,
        base_library_revision=base.base_library_revision,
        scan_root=base.scan_root,
        groups=base.groups,
        backup=base.backup,
        report=ReportOutputPolicy(enabled=True, directory="reports"),
        rename_template=base.rename_template,
        rename_policy=base.rename_policy,
    )
    events = RecordingEventSink([])

    result = ApplyService(
        preflight=ValidPreflight(),
        writer=PartialWriter(),
        report_writer=FailingReportWriter(),
    ).apply(request, cancellation=NeverCancelledToken(), events=events)

    assert call_order == ["write:failed-file", "write:applied-file", "report"]
    assert result.status is ApplyBatchStatus.PARTIAL
    assert result.report_result == ReportWriteResult(
        ReportWriteStatus.FAILED,
        None,
        ReportErrorCode.WRITE_FAILED,
    )
    assert len(captured_requests) == 1
    report_request = captured_requests[0]
    assert report_request.operation_id == "APPLY-1"
    assert report_request.result is ReportOperationResult.PARTIAL
    assert tuple(file.status for file in report_request.files) == (
        ReportFileStatus.FAILED,
        ReportFileStatus.SKIPPED,
        ReportFileStatus.SUCCEEDED,
    )
    assert tuple(file.selected_release for file in report_request.files) == (
        release_1,
        release_1,
        release_2,
    )
    assert report_request.files[0].apply_codes == (MediaErrorCode.TAG_WRITE_FAILED,)
    assert report_request.files[1].changes
    assert report_request.files[1].reason_codes == (
        ApplySkipReason.ALBUM_STOPPED_AFTER_FAILURE,
    )
    assert report_request.files[1].completed_stage is FileTransactionStage.NOT_STARTED
    assert report_request.files[1].rename_result is ReportRenameResult.NOT_REQUESTED
    assert events.events[-4:] == [
        OperationProgress("APPLY-1", ApplyStage.REFRESHING_FILES, 1, 1),
        OperationStageChanged("APPLY-1", ApplyStage.WRITING_REPORT),
        OperationProgress("APPLY-1", ApplyStage.WRITING_REPORT, 0, 1),
        OperationProgress("APPLY-1", ApplyStage.WRITING_REPORT, 1, 1),
    ]


def test_unexpected_report_exception_isolated_from_successful_audio_result() -> None:
    adapter = StubAdapter()

    class ValidPreflight:
        def inspect(
            self,
            source: LocalMediaFile,
            proposed_changes: FileChangeSet,
            backup: BackupPolicy,
        ) -> FilePreflightSnapshot:
            del proposed_changes, backup

            return make_preflight_snapshot(source, adapter)

    class SuccessfulWriter:
        def apply_file(
            self,
            source: LocalMediaFile,
            *_args: object,
        ) -> FileApplyResult:
            return FileApplyResult(
                source.path,
                source.path,
                FileApplyStatus.SUCCEEDED,
                FileTransactionStage.COMPLETED,
            )

    class RaisingReportWriter:
        def write(self, **_kwargs: object) -> ReportWriteResult:
            raise OSError("report volume disappeared")

    base = make_batch(
        (
            ApplyGroupRequest(
                "group-1",
                7,
                None,
                (make_file_request("file-1", "library/a/01.flac"),),
            ),
        )
    )
    request = ApplyBatchRequest(
        operation_id=base.operation_id,
        base_session_revision=base.base_session_revision,
        base_library_revision=base.base_library_revision,
        scan_root=base.scan_root,
        groups=base.groups,
        backup=base.backup,
        report=ReportOutputPolicy(enabled=True),
        rename_template=base.rename_template,
        rename_policy=base.rename_policy,
    )

    result = ApplyService(
        preflight=ValidPreflight(),
        writer=SuccessfulWriter(),  # type: ignore[arg-type]
        report_writer=RaisingReportWriter(),  # type: ignore[arg-type]
    ).apply(request, cancellation=NeverCancelledToken())

    assert result.status is ApplyBatchStatus.SUCCEEDED
    assert result.groups[0].files[0].status is ApplyFileOutcomeStatus.APPLIED
    assert result.report_result == ReportWriteResult(
        ReportWriteStatus.FAILED,
        None,
        ReportErrorCode.WRITE_FAILED,
    )


@pytest.mark.parametrize("cancel_before_inspection", (False, True))
@pytest.mark.parametrize("separate_album", (False, True))
def test_preflight_cancellation_preserves_later_no_change_files(
    cancel_before_inspection: bool, separate_album: bool,
) -> None:
    token = MutableCancellationToken()
    adapter = StubAdapter()

    class CancellingPreflight:
        def inspect(self, source, _changes, _backup):
            token.cancel()
            return make_preflight_snapshot(source, adapter)

    class ForbiddenWriter:
        def apply_file(self, *_args, **_kwargs):
            raise AssertionError("a cancelled preflight must never enter the writer")

    if cancel_before_inspection:
        token.cancel()

    changed = make_file_request("changed", "library/a/01.flac")
    unchanged = make_file_request("unchanged", "library/b/02.flac", new_title="Old unchanged")
    groups = (
        (ApplyGroupRequest("first", 1, None, (changed,)), ApplyGroupRequest("second", 1, None, (unchanged,)))
        if separate_album else (ApplyGroupRequest("first", 1, None, (changed, unchanged)),)
    )
    events = RecordingEventSink([])
    result = ApplyService(preflight=CancellingPreflight(), writer=ForbiddenWriter()).apply(
        make_batch(groups), cancellation=token, events=events,
    )

    # Preflight never started a transaction. A later no-op therefore remains a
    # truthful no-change result regardless of its position or album membership.
    outcomes = tuple(file for group in result.groups for file in group.files)
    assert result.status is ApplyBatchStatus.CANCELLED
    assert outcomes[0].skip_reason is ApplySkipReason.CANCELLED_BEFORE_START
    assert outcomes[1].status is ApplyFileOutcomeStatus.NO_CHANGES
    assert not any(file.transaction_result is not None for file in outcomes)
    assert not any(
        isinstance(event, OperationStageChanged) and event.stage == ApplyStage.APPLYING_FILES
        for event in events.events
    )


def test_preflight_cancellation_stops_inspection_without_entering_apply_stage() -> None:
    token = MutableCancellationToken()
    inspected: list[str] = []
    captured_reports: list[ProcessingReportRequest] = []
    adapter = StubAdapter()

    class CancellingPreflight:
        def inspect(
            self,
            source: LocalMediaFile,
            proposed_changes: FileChangeSet,
            backup: BackupPolicy,
        ) -> FilePreflightSnapshot:
            del proposed_changes, backup
            inspected.append(source.file_id)
            token.cancel()

            return make_preflight_snapshot(source, adapter)

    class ForbiddenWriter:
        def apply_file(self, *_args: object, **_kwargs: object) -> FileApplyResult:
            raise AssertionError("preflight cancellation must prevent every writer call")

    class CapturingReportWriter:
        def write(
            self,
            *,
            policy: ReportOutputPolicy,
            request: ProcessingReportRequest,
        ) -> ReportWriteResult:
            assert policy.enabled
            captured_reports.append(request)

            return ReportWriteResult(ReportWriteStatus.WRITTEN, Path("report.json"), None)

    no_change = make_file_request(
        "no-change",
        "library/a/00.flac",
        new_title="Old no-change",
    )
    base = make_batch(
        (
            ApplyGroupRequest(
                "group-1",
                7,
                None,
                (
                    no_change,
                    make_file_request("changed-1", "library/a/01.flac"),
                    make_file_request("changed-2", "library/a/02.flac"),
                ),
            ),
            ApplyGroupRequest(
                "group-2",
                8,
                None,
                (make_file_request("changed-3", "library/b/01.flac"),),
            ),
        )
    )
    request = ApplyBatchRequest(
        operation_id=base.operation_id,
        base_session_revision=base.base_session_revision,
        base_library_revision=base.base_library_revision,
        scan_root=base.scan_root,
        groups=base.groups,
        backup=base.backup,
        report=ReportOutputPolicy(enabled=True),
        rename_template=base.rename_template,
        rename_policy=base.rename_policy,
    )
    events = RecordingEventSink([])

    result = ApplyService(
        preflight=CancellingPreflight(),
        writer=ForbiddenWriter(),  # type: ignore[arg-type]
        report_writer=CapturingReportWriter(),
    ).apply(request, cancellation=token, events=events)

    assert inspected == ["changed-1"]
    assert result.status is ApplyBatchStatus.CANCELLED
    assert tuple(file.file_id for group in result.groups for file in group.files) == (
        "no-change",
        "changed-1",
        "changed-2",
        "changed-3",
    )
    assert tuple(file.status for group in result.groups for file in group.files) == (
        ApplyFileOutcomeStatus.NO_CHANGES,
        ApplyFileOutcomeStatus.SKIPPED,
        ApplyFileOutcomeStatus.SKIPPED,
        ApplyFileOutcomeStatus.SKIPPED,
    )
    assert tuple(file.skip_reason for group in result.groups for file in group.files) == (
        None,
        ApplySkipReason.CANCELLED_BEFORE_START,
        ApplySkipReason.CANCELLED_BEFORE_START,
        ApplySkipReason.CANCELLED_BEFORE_START,
    )
    assert not any(
        isinstance(event, OperationStageChanged)
        and event.stage == ApplyStage.APPLYING_FILES
        for event in events.events
    )
    assert [
        (event.current, event.total)
        for event in events.events
        if isinstance(event, OperationProgress)
        and event.stage == ApplyStage.PREFLIGHTING
    ] == [(0, 4), (1, 4), (2, 4)]
    assert len(captured_reports) == 1
    assert captured_reports[0].result is ReportOperationResult.CANCELLED
    assert tuple(entry.reason_codes[-1] for entry in captured_reports[0].files) == (
        ApplySkipReason.NO_CHANGES,
        ApplySkipReason.CANCELLED_BEFORE_START,
        ApplySkipReason.CANCELLED_BEFORE_START,
        ApplySkipReason.CANCELLED_BEFORE_START,
    )
    assert [
        (event.file_id, event.reason)
        for event in events.events
        if isinstance(event, FileSkipped)
    ] == [
        ("no-change", ApplySkipReason.NO_CHANGES),
        ("changed-1", ApplySkipReason.CANCELLED_BEFORE_START),
        ("changed-2", ApplySkipReason.CANCELLED_BEFORE_START),
        ("changed-3", ApplySkipReason.CANCELLED_BEFORE_START),
    ]


def test_local_preflight_is_read_only_and_resolves_one_current_adapter(tmp_path: Path) -> None:
    scan_root = tmp_path / "library"
    album_directory = scan_root / "album"
    album_directory.mkdir(parents=True)
    source_path = album_directory / "01.flac"
    source_path.write_bytes(b"audio-bytes")
    (album_directory / "cover.jpg").write_bytes(b"image")
    source = make_source("file-1", str(source_path), "Old title")
    request = ApplyFileRequest(
        source=source,
        reviews=make_reviews(source, "New title"),
        rename_decision=RenameDecision.KEEP_FILENAME,
        track_mapping_resolved=True,
    )
    proposed = build_change_set(
        source,
        request.reviews,
        request.rename_decision,
        "%title%",
        FilenameRenderPolicy(),
        ChangeValidationFacts(),
    )
    adapter = StubAdapter()
    detected: list[Path] = []

    class RecordingRegistry:
        def detect(self, path: Path) -> StubAdapter:
            detected.append(path)

            return adapter

    before = tuple(
        sorted(
            (
                str(path.relative_to(tmp_path)),
                path.is_dir(),
                None if path.is_dir() else path.read_bytes(),
            )
            for path in tmp_path.rglob("*")
        )
    )
    snapshot = LocalApplyPreflightInspector(
        registry=RecordingRegistry(),  # type: ignore[arg-type]
    ).inspect(
        source,
        proposed,
        BackupPolicy(False, None, scan_root, "APPLY-1"),
    )
    after = tuple(
        sorted(
            (
                str(path.relative_to(tmp_path)),
                path.is_dir(),
                None if path.is_dir() else path.read_bytes(),
            )
            for path in tmp_path.rglob("*")
        )
    )

    assert detected == [source_path]
    assert snapshot.file_id == "file-1"
    assert snapshot.adapter is adapter
    assert snapshot.validation == ChangeValidationFacts(
        existing_names=("01.flac", "cover.jpg"),
    )
    assert snapshot.capacity.source_size_bytes == len(b"audio-bytes")
    assert snapshot.capacity.temporary_storage.free_bytes > 0
    assert snapshot.capacity.backup_storage is None
    assert before == after


def test_out_of_root_source_is_not_passed_to_adapter_probe(tmp_path: Path) -> None:
    scan_root = tmp_path / "library"
    scan_root.mkdir()
    source_path = tmp_path / "outside.flac"
    source_path.write_bytes(b"audio")
    source = make_source("file-1", str(source_path), "Old title")
    request = make_file_request("placeholder", "library/placeholder.flac")
    proposed = build_change_set(
        source,
        make_reviews(source, "New title"),
        request.rename_decision,
        "%title%",
        FilenameRenderPolicy(),
        ChangeValidationFacts(),
    )

    class ForbiddenRegistry:
        def detect(self, path: Path) -> StubAdapter:
            raise AssertionError(f"unsafe adapter probe for {path}")

    snapshot = LocalApplyPreflightInspector(
        registry=ForbiddenRegistry(),  # type: ignore[arg-type]
    ).inspect(
        source,
        proposed,
        BackupPolicy(False, None, scan_root, "APPLY-1"),
    )

    assert snapshot.adapter is None
    assert not snapshot.validation.source_readable
    assert not snapshot.validation.adapter_available


def test_directory_enumeration_failure_is_a_blocking_fresh_fact(tmp_path: Path) -> None:
    scan_root = tmp_path / "library"
    scan_root.mkdir()
    source_path = scan_root / "01.flac"
    source_path.write_bytes(b"audio")
    source = make_source("file-1", str(source_path), "Old title")
    proposed = build_change_set(
        source,
        make_reviews(source, "New title"),
        RenameDecision.KEEP_FILENAME,
    )

    class FailedListingProbe(LocalApplyPathProbe):
        def directory_names(self, path: Path) -> tuple[str, ...] | None:
            del path

            return None

    class Registry:
        def detect(self, path: Path) -> StubAdapter:
            del path

            return StubAdapter()

    snapshot = LocalApplyPreflightInspector(
        registry=Registry(),  # type: ignore[arg-type]
        probe=FailedListingProbe(),
    ).inspect(
        source,
        proposed,
        BackupPolicy(False, None, scan_root, "APPLY-1"),
    )

    assert not snapshot.validation.directory_listing_available


def test_existing_backup_root_file_is_not_treated_as_a_writable_directory(
    tmp_path: Path,
) -> None:
    scan_root = tmp_path / "library"
    scan_root.mkdir()
    source_path = scan_root / "01.flac"
    source_path.write_bytes(b"audio")
    backup_root = tmp_path / "backup-file"
    backup_root.write_bytes(b"not a directory")
    source = make_source("file-1", str(source_path), "Old title")
    proposed = build_change_set(
        source,
        make_reviews(source, "New title"),
        RenameDecision.KEEP_FILENAME,
    )

    class Registry:
        def detect(self, path: Path) -> StubAdapter:
            del path

            return StubAdapter()

    snapshot = LocalApplyPreflightInspector(
        registry=Registry(),  # type: ignore[arg-type]
    ).inspect(
        source,
        proposed,
        BackupPolicy(True, backup_root, scan_root, "APPLY-1"),
    )

    assert not snapshot.validation.backup_root_writable
    assert backup_root.read_bytes() == b"not a directory"


def test_distinct_storage_budgets_temp_peak_and_backup_sum_independently() -> None:
    adapter = StubAdapter()
    writer_calls: list[str] = []

    class CapacityPreflight:
        def inspect(
            self,
            source: LocalMediaFile,
            proposed_changes: FileChangeSet,
            backup: BackupPolicy,
        ) -> FilePreflightSnapshot:
            del proposed_changes
            assert backup.enabled

            return FilePreflightSnapshot(
                source.file_id,
                adapter,
                ChangeValidationFacts(),
                FileCapacitySnapshot(
                    source_size_bytes=60,
                    temporary_storage=PathStorageSnapshot("source-volume", 100),
                    backup_storage=PathStorageSnapshot("backup-volume", 120),
                    backup_target=Path("backups/APPLY-1") / source.file_id,
                ),
            )

    class SuccessfulWriter:
        def apply_file(
            self,
            source: LocalMediaFile,
            *_args: object,
        ) -> FileApplyResult:
            writer_calls.append(source.file_id)

            return FileApplyResult(
                source.path,
                source.path,
                FileApplyStatus.SUCCEEDED,
                FileTransactionStage.COMPLETED,
            )

    base = make_batch(
        (
            ApplyGroupRequest(
                "group-1",
                7,
                None,
                (
                    make_file_request("file-1", "library/a/01.flac"),
                    make_file_request("file-2", "library/b/01.flac"),
                ),
            ),
        )
    )
    backup = BackupPolicy(True, Path("backups"), Path("library"), "APPLY-1")
    request = ApplyBatchRequest(
        operation_id=base.operation_id,
        base_session_revision=base.base_session_revision,
        base_library_revision=base.base_library_revision,
        scan_root=base.scan_root,
        groups=base.groups,
        backup=backup,
        report=base.report,
        rename_template=base.rename_template,
        rename_policy=base.rename_policy,
    )

    result = ApplyService(
        preflight=CapacityPreflight(),
        writer=SuccessfulWriter(),  # type: ignore[arg-type]
    ).apply(request, cancellation=NeverCancelledToken())

    assert writer_calls == ["file-1", "file-2"]
    assert result.status is ApplyBatchStatus.SUCCEEDED


def test_invalid_rename_only_request_is_a_batch_blocker_not_no_changes() -> None:
    no_metadata_change = make_file_request(
        "file-1",
        "library/a/01.flac",
        new_title="Old file-1",
        rename_decision=RenameDecision.APPLY_RENAME,
    )
    request = make_batch(
        (ApplyGroupRequest("group-1", 7, None, (no_metadata_change,)),),
        rename_template="%unknown_field%",
    )

    class ForbiddenPreflight:
        def inspect(self, *_args: object) -> FilePreflightSnapshot:
            raise AssertionError("a pure invalid template needs no filesystem probe")

    class ForbiddenWriter:
        def apply_file(self, *_args: object) -> FileApplyResult:
            raise AssertionError("an invalid requested rename must never be written")

    result = ApplyService(
        preflight=ForbiddenPreflight(),  # type: ignore[arg-type]
        writer=ForbiddenWriter(),  # type: ignore[arg-type]
    ).apply(request, cancellation=NeverCancelledToken())

    outcome = result.groups[0].files[0]
    assert result.status is ApplyBatchStatus.FAILED
    assert outcome.status is ApplyFileOutcomeStatus.SKIPPED
    assert outcome.skip_reason is ApplySkipReason.BATCH_PREFLIGHT_BLOCKED
    assert ChangeIssueCode.INVALID_RENAME_TEMPLATE in {
        issue.code for issue in outcome.change_set.validation.issues
    }


def test_file_outcome_side_effect_truth_covers_cleanup_orphans_and_path_changes() -> None:
    changed_request = make_file_request("changed", "library/a/01.flac")
    changed_set = build_change_set(
        changed_request.source,
        changed_request.reviews,
        changed_request.rename_decision,
    )

    def transactional_outcome(
        status: ApplyFileOutcomeStatus,
        result: FileApplyResult,
        *,
        request: ApplyFileRequest = changed_request,
        change_set: FileChangeSet = changed_set,
    ) -> ApplyFileOutcome:
        return ApplyFileOutcome(
            file_id=request.source.file_id,
            source_path=request.source.path,
            selected_release=None,
            reviews=request.reviews,
            change_set=change_set,
            status=status,
            transaction_result=result,
        )

    success = transactional_outcome(
        ApplyFileOutcomeStatus.APPLIED,
        FileApplyResult(
            changed_request.source.path,
            changed_request.source.path,
            FileApplyStatus.SUCCEEDED,
            FileTransactionStage.COMPLETED,
        ),
    )
    clean_failure = transactional_outcome(
        ApplyFileOutcomeStatus.FAILED,
        FileApplyResult(
            changed_request.source.path,
            changed_request.source.path,
            FileApplyStatus.FAILED,
            FileTransactionStage.WRITING_METADATA,
            (Issue(MediaErrorCode.TAG_WRITE_FAILED, "The tag write failed."),),
        ),
    )
    cancelled = transactional_outcome(
        ApplyFileOutcomeStatus.CANCELLED,
        FileApplyResult(
            changed_request.source.path,
            changed_request.source.path,
            FileApplyStatus.CANCELLED,
            FileTransactionStage.COPYING_TEMPORARY,
        ),
    )
    cleanup_orphan = transactional_outcome(
        ApplyFileOutcomeStatus.FAILED,
        FileApplyResult(
            changed_request.source.path,
            changed_request.source.path,
            FileApplyStatus.FAILED,
            FileTransactionStage.VERIFYING_TEMPORARY,
            (Issue(MediaErrorCode.CLEANUP_FAILED, "The temporary copy remains."),),
        ),
    )
    rename_request = make_file_request(
        "renamed",
        "library/a/02.flac",
        rename_decision=RenameDecision.APPLY_RENAME,
    )
    rename_set = build_change_set(
        rename_request.source,
        rename_request.reviews,
        rename_request.rename_decision,
        "%title%",
    )
    assert rename_set.rename_change is not None
    renamed_failure = transactional_outcome(
        ApplyFileOutcomeStatus.FAILED,
        FileApplyResult(
            rename_request.source.path,
            rename_set.rename_change.new_path,
            FileApplyStatus.FAILED,
            FileTransactionStage.CLEANING_ORIGINAL,
            (Issue(MediaErrorCode.CLEANUP_FAILED, "The original could not be removed."),),
        ),
        request=rename_request,
        change_set=rename_set,
    )
    no_change_request = make_file_request(
        "no-change",
        "library/a/03.flac",
        new_title="Old no-change",
    )
    no_change_set = build_change_set(
        no_change_request.source,
        no_change_request.reviews,
        no_change_request.rename_decision,
    )
    no_change = ApplyFileOutcome(
        file_id=no_change_request.source.file_id,
        source_path=no_change_request.source.path,
        selected_release=None,
        reviews=no_change_request.reviews,
        change_set=no_change_set,
        status=ApplyFileOutcomeStatus.NO_CHANGES,
    )

    assert (success.filesystem_changed, success.requires_rescan) == (True, True)
    assert (clean_failure.filesystem_changed, clean_failure.requires_rescan) == (False, False)
    assert (cancelled.filesystem_changed, cancelled.requires_rescan) == (False, False)
    assert (cleanup_orphan.filesystem_changed, cleanup_orphan.requires_rescan) == (True, True)
    assert (renamed_failure.filesystem_changed, renamed_failure.requires_rescan) == (True, True)
    assert (no_change.filesystem_changed, no_change.requires_rescan) == (False, False)


def test_file_outcome_rejects_inconsistent_public_transaction_truth() -> None:
    request = make_file_request("file-1", "library/a/01.flac")
    change_set = build_change_set(
        request.source,
        request.reviews,
        request.rename_decision,
    )
    success = FileApplyResult(
        request.source.path,
        request.source.path,
        FileApplyStatus.SUCCEEDED,
        FileTransactionStage.COMPLETED,
    )

    def make_outcome(**overrides: object) -> ApplyFileOutcome:
        values: dict[str, object] = {
            "file_id": request.source.file_id,
            "source_path": request.source.path,
            "selected_release": None,
            "reviews": request.reviews,
            "change_set": change_set,
            "status": ApplyFileOutcomeStatus.APPLIED,
            "transaction_result": success,
        }
        values.update(overrides)

        return ApplyFileOutcome(**values)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="reviews"):
        make_outcome(reviews=tuple(reversed(request.reviews)))

    with pytest.raises(ValueError, match="source_path"):
        make_outcome(
            transaction_result=replace(success, source_path=Path("library/a/other.flac")),
        )

    with pytest.raises(ValueError, match="completed"):
        make_outcome(
            transaction_result=replace(
                success,
                completed_stage=FileTransactionStage.WRITING_METADATA,
            ),
        )

    with pytest.raises(ValueError, match="real operation"):
        make_outcome(
            change_set=replace(
                change_set,
                metadata_changes=(),
                final_metadata=request.source.read_result.metadata,
            ),
        )

    with pytest.raises(ValueError, match="NO_CHANGES"):
        make_outcome(
            status=ApplyFileOutcomeStatus.SKIPPED,
            transaction_result=None,
            skip_reason=ApplySkipReason.NO_CHANGES,
        )


def test_group_and_batch_results_reject_identity_and_status_drift() -> None:
    first_request = make_file_request("file-1", "library/a/01.flac")
    first_change_set = build_change_set(
        first_request.source,
        first_request.reviews,
        first_request.rename_decision,
    )
    first = ApplyFileOutcome(
        file_id="file-1",
        source_path=first_request.source.path,
        selected_release=None,
        reviews=first_request.reviews,
        change_set=first_change_set,
        status=ApplyFileOutcomeStatus.APPLIED,
        transaction_result=FileApplyResult(
            first_request.source.path,
            first_request.source.path,
            FileApplyStatus.SUCCEEDED,
            FileTransactionStage.COMPLETED,
        ),
    )
    second_request = make_file_request("file-2", "library/a/01.flac")
    second_change_set = build_change_set(
        second_request.source,
        second_request.reviews,
        second_request.rename_decision,
    )
    second = ApplyFileOutcome(
        file_id="file-2",
        source_path=second_request.source.path,
        selected_release=None,
        reviews=second_request.reviews,
        change_set=second_change_set,
        status=ApplyFileOutcomeStatus.APPLIED,
        transaction_result=FileApplyResult(
            second_request.source.path,
            second_request.source.path,
            FileApplyStatus.SUCCEEDED,
            FileTransactionStage.COMPLETED,
        ),
    )

    with pytest.raises(ValueError, match="source paths"):
        ApplyGroupOutcome("group-1", 2, (first, second))

    group = ApplyGroupOutcome("group-1", 2, (first,))
    report_failure = ReportWriteResult(
        ReportWriteStatus.FAILED,
        None,
        ReportErrorCode.WRITE_FAILED,
    )
    valid = ApplyBatchResult(
        operation_id="APPLY-1",
        base_session_revision=3,
        base_library_revision=1,
        status=ApplyBatchStatus.SUCCEEDED,
        groups=(group,),
        report_result=report_failure,
    )

    assert valid.report_result is report_failure

    with pytest.raises(ValueError, match="status"):
        replace(valid, status=ApplyBatchStatus.FAILED)

    with pytest.raises(ValueError, match="groups"):
        replace(valid, groups=())


def test_batch_parent_matching_does_not_casefold_sharp_s_into_ss() -> None:
    adapter = StubAdapter()
    writer_calls: list[str] = []

    class EmptyDirectoryPreflight:
        def inspect(
            self,
            source: LocalMediaFile,
            proposed_changes: FileChangeSet,
            backup: BackupPolicy,
        ) -> FilePreflightSnapshot:
            del proposed_changes, backup

            return make_preflight_snapshot(source, adapter)

    class SuccessfulWriter:
        def apply_file(
            self,
            source: LocalMediaFile,
            *_args: object,
        ) -> FileApplyResult:
            writer_calls.append(source.file_id)
            destination = source.path.with_name("Same.flac")

            return FileApplyResult(
                source.path,
                destination,
                FileApplyStatus.SUCCEEDED,
                FileTransactionStage.COMPLETED,
            )

    request = make_batch(
        (
            ApplyGroupRequest(
                "group-1",
                7,
                None,
                (
                    make_file_request(
                        "file-1",
                        "library/Straße/01.flac",
                        new_title="Same",
                        rename_decision=RenameDecision.APPLY_RENAME,
                    ),
                    make_file_request(
                        "file-2",
                        "library/STRASSE/01.flac",
                        new_title="Same",
                        rename_decision=RenameDecision.APPLY_RENAME,
                    ),
                ),
            ),
        ),
        rename_template="%title%",
    )

    result = ApplyService(
        preflight=EmptyDirectoryPreflight(),
        writer=SuccessfulWriter(),  # type: ignore[arg-type]
    ).apply(request, cancellation=NeverCancelledToken())

    assert writer_calls == ["file-1", "file-2"]
    assert result.status is ApplyBatchStatus.SUCCEEDED


def test_missing_backup_root_is_validated_through_parent_without_creation(
    tmp_path: Path,
) -> None:
    scan_root = tmp_path / "library"
    scan_root.mkdir()
    source_path = scan_root / "01.flac"
    source_path.write_bytes(b"audio")
    backup_root = tmp_path / "backups" / "nested"
    source = make_source("file-1", str(source_path), "Old title")
    proposed = build_change_set(
        source,
        make_reviews(source, "New title"),
        RenameDecision.KEEP_FILENAME,
    )

    class Registry:
        def detect(self, path: Path) -> StubAdapter:
            del path

            return StubAdapter()

    snapshot = LocalApplyPreflightInspector(
        registry=Registry(),  # type: ignore[arg-type]
    ).inspect(
        source,
        proposed,
        BackupPolicy(True, backup_root, scan_root, "APPLY-1"),
    )

    assert snapshot.validation.backup_root_writable
    assert snapshot.validation.backup_destination_available
    assert snapshot.capacity.backup_target == backup_root / "APPLY-1" / "01.flac"
    assert snapshot.capacity.backup_storage is not None
    assert not backup_root.exists()


def test_disabled_backup_ignores_irrelevant_negative_backup_facts() -> None:
    adapter = StubAdapter()
    writer_calls: list[str] = []

    class IrrelevantBackupFactsPreflight:
        def inspect(
            self,
            source: LocalMediaFile,
            proposed_changes: FileChangeSet,
            backup: BackupPolicy,
        ) -> FilePreflightSnapshot:
            del proposed_changes
            assert not backup.enabled

            return make_preflight_snapshot(
                source,
                adapter,
                ChangeValidationFacts(
                    backup_root_writable=False,
                    backup_destination_available=False,
                    backup_space_sufficient=False,
                ),
            )

    class SuccessfulWriter:
        def apply_file(self, source: LocalMediaFile, *_args: object) -> FileApplyResult:
            writer_calls.append(source.file_id)

            return FileApplyResult(
                source.path,
                source.path,
                FileApplyStatus.SUCCEEDED,
                FileTransactionStage.COMPLETED,
            )

    request = make_batch(
        (
            ApplyGroupRequest(
                "group-1",
                7,
                None,
                (make_file_request("file-1", "library/a/01.flac"),),
            ),
        )
    )
    result = ApplyService(
        preflight=IrrelevantBackupFactsPreflight(),
        writer=SuccessfulWriter(),  # type: ignore[arg-type]
    ).apply(request, cancellation=NeverCancelledToken())

    assert writer_calls == ["file-1"]
    assert result.status is ApplyBatchStatus.SUCCEEDED


def test_completed_blocking_preflight_wins_over_a_late_token() -> None:
    token = MutableCancellationToken()
    adapter = StubAdapter()

    class BlockingFinalPreflight:
        def inspect(
            self,
            source: LocalMediaFile,
            proposed_changes: FileChangeSet,
            backup: BackupPolicy,
        ) -> FilePreflightSnapshot:
            del proposed_changes, backup
            token.cancel()

            return make_preflight_snapshot(
                source,
                adapter,
                ChangeValidationFacts(directory_writable=False),
            )

    class ForbiddenWriter:
        def apply_file(self, *_args: object) -> FileApplyResult:
            raise AssertionError("blocked preflight cannot write")

    request = make_batch(
        (
            ApplyGroupRequest(
                "group-1",
                7,
                None,
                (make_file_request("file-1", "library/a/01.flac"),),
            ),
        )
    )
    result = ApplyService(
        preflight=BlockingFinalPreflight(),
        writer=ForbiddenWriter(),  # type: ignore[arg-type]
    ).apply(request, cancellation=token)

    assert result.status is ApplyBatchStatus.FAILED
    assert result.groups[0].files[0].skip_reason is ApplySkipReason.BATCH_PREFLIGHT_BLOCKED


def test_outcome_and_batch_causality_reject_impossible_results() -> None:
    request = make_file_request("file-1", "library/a/01.flac")
    valid_change_set = build_change_set(
        request.source,
        request.reviews,
        request.rename_decision,
    )
    blocked_change_set = build_change_set(
        request.source,
        request.reviews,
        request.rename_decision,
        validation=ChangeValidationFacts(directory_writable=False),
    )
    success = FileApplyResult(
        request.source.path,
        request.source.path,
        FileApplyStatus.SUCCEEDED,
        FileTransactionStage.COMPLETED,
    )

    with pytest.raises(ValueError, match="blocked"):
        ApplyFileOutcome(
            request.source.file_id,
            request.source.path,
            None,
            request.reviews,
            blocked_change_set,
            ApplyFileOutcomeStatus.APPLIED,
            success,
        )

    no_change_request = make_file_request(
        "no-change",
        "library/a/02.flac",
        new_title="Old no-change",
    )
    no_change_set = build_change_set(
        no_change_request.source,
        no_change_request.reviews,
        no_change_request.rename_decision,
    )

    cancelled_no_change = ApplyFileOutcome(
        no_change_request.source.file_id,
        no_change_request.source.path,
        None,
        no_change_request.reviews,
        no_change_set,
        ApplyFileOutcomeStatus.SKIPPED,
        skip_reason=ApplySkipReason.CANCELLED_BEFORE_START,
    )

    with pytest.raises(ValueError, match="earlier transaction"):
        ApplyBatchResult(
            "APPLY-1",
            3,
            1,
            ApplyBatchStatus.CANCELLED,
            (ApplyGroupOutcome("group-no-change", 2, (cancelled_no_change,)),),
            ReportWriteResult(ReportWriteStatus.DISABLED, None, None),
        )

    with pytest.raises((TypeError, ValueError), match="reason_codes"):
        ApplyFileRequest(
            source=request.source,
            reviews=request.reviews,
            rename_decision=request.rename_decision,
            track_mapping_resolved=True,
            reason_codes=(ApplySkipReason.CANCELLED_BEFORE_START,),  # type: ignore[arg-type]
        )

    rename_request = make_file_request(
        "rename",
        "library/a/03.flac",
        rename_decision=RenameDecision.APPLY_RENAME,
    )
    rename_set = build_change_set(
        rename_request.source,
        rename_request.reviews,
        rename_request.rename_decision,
        "%title%",
    )
    assert rename_set.rename_change is not None

    with pytest.raises(ValueError, match="failed transaction"):
        ApplyFileOutcome(
            rename_request.source.file_id,
            rename_request.source.path,
            None,
            rename_request.reviews,
            rename_set,
            ApplyFileOutcomeStatus.FAILED,
            FileApplyResult(
                rename_request.source.path,
                rename_set.rename_change.new_path,
                FileApplyStatus.FAILED,
                FileTransactionStage.WRITING_METADATA,
                (Issue(MediaErrorCode.TAG_WRITE_FAILED, "The tag write failed."),),
            ),
        )

    invalid_batch_skip = ApplyFileOutcome(
        request.source.file_id,
        request.source.path,
        None,
        request.reviews,
        valid_change_set,
        ApplyFileOutcomeStatus.SKIPPED,
        skip_reason=ApplySkipReason.BATCH_PREFLIGHT_BLOCKED,
    )
    invalid_batch_group = ApplyGroupOutcome("group-1", 2, (invalid_batch_skip,))

    with pytest.raises(ValueError, match="preflight blocker"):
        ApplyBatchResult(
            "APPLY-1",
            3,
            1,
            ApplyBatchStatus.FAILED,
            (invalid_batch_group,),
            ReportWriteResult(ReportWriteStatus.DISABLED, None, None),
        )

    invalid_album_skip = replace(
        invalid_batch_skip,
        skip_reason=ApplySkipReason.ALBUM_STOPPED_AFTER_FAILURE,
    )

    with pytest.raises(ValueError, match="earlier failed"):
        ApplyGroupOutcome("group-1", 2, (invalid_album_skip,))


def test_precancelled_no_op_succeeds_but_pure_blocker_fails_without_apply_stage() -> None:
    token = MutableCancellationToken()
    token.cancel()

    class ForbiddenPreflight:
        def inspect(self, *_args: object) -> FilePreflightSnapshot:
            raise AssertionError("pre-cancelled pure drafts need no external probe")

    class ForbiddenWriter:
        def apply_file(self, *_args: object) -> FileApplyResult:
            raise AssertionError("pre-cancelled pure drafts need no writer")

    no_change = make_file_request(
        "no-change",
        "library/a/01.flac",
        new_title="Old no-change",
    )
    no_change_events = RecordingEventSink([])
    no_change_result = ApplyService(
        preflight=ForbiddenPreflight(),  # type: ignore[arg-type]
        writer=ForbiddenWriter(),  # type: ignore[arg-type]
    ).apply(
        make_batch((ApplyGroupRequest("group-1", 2, None, (no_change,)),)),
        cancellation=token,
        events=no_change_events,
    )

    assert no_change_result.status is ApplyBatchStatus.SUCCEEDED
    assert no_change_result.groups[0].files[0].status is ApplyFileOutcomeStatus.NO_CHANGES
    assert not any(
        isinstance(event, OperationStageChanged)
        and event.stage == ApplyStage.APPLYING_FILES
        for event in no_change_events.events
    )

    invalid_rename = make_file_request(
        "blocked",
        "library/a/02.flac",
        new_title="Old blocked",
        rename_decision=RenameDecision.APPLY_RENAME,
    )
    blocked_events = RecordingEventSink([])
    blocked_result = ApplyService(
        preflight=ForbiddenPreflight(),  # type: ignore[arg-type]
        writer=ForbiddenWriter(),  # type: ignore[arg-type]
    ).apply(
        make_batch(
            (ApplyGroupRequest("group-2", 2, None, (invalid_rename,)),),
            rename_template="%unknown%",
        ),
        cancellation=token,
        events=blocked_events,
    )

    assert blocked_result.status is ApplyBatchStatus.FAILED
    assert blocked_result.groups[0].files[0].skip_reason is ApplySkipReason.BATCH_PREFLIGHT_BLOCKED
    assert not any(
        isinstance(event, OperationStageChanged)
        and event.stage == ApplyStage.APPLYING_FILES
        for event in blocked_events.events
    )


def test_wrong_report_writer_result_is_isolated_after_audio_success() -> None:
    adapter = StubAdapter()

    class Preflight:
        def inspect(
            self,
            source: LocalMediaFile,
            proposed_changes: FileChangeSet,
            backup: BackupPolicy,
        ) -> FilePreflightSnapshot:
            del proposed_changes, backup

            return make_preflight_snapshot(source, adapter)

    class Writer:
        def apply_file(self, source: LocalMediaFile, *_args: object) -> FileApplyResult:
            return FileApplyResult(
                source.path,
                source.path,
                FileApplyStatus.SUCCEEDED,
                FileTransactionStage.COMPLETED,
            )

    class WrongReportWriter:
        def write(self, **_kwargs: object) -> object:
            return object()

    base = make_batch(
        (
            ApplyGroupRequest(
                "group-1",
                2,
                None,
                (make_file_request("file-1", "library/a/01.flac"),),
            ),
        )
    )
    request = replace(base, report=ReportOutputPolicy(enabled=True))
    result = ApplyService(
        preflight=Preflight(),
        writer=Writer(),  # type: ignore[arg-type]
        report_writer=WrongReportWriter(),  # type: ignore[arg-type]
    ).apply(request, cancellation=NeverCancelledToken())

    assert result.status is ApplyBatchStatus.SUCCEEDED
    assert result.report_result == ReportWriteResult(
        ReportWriteStatus.FAILED,
        None,
        ReportErrorCode.WRITE_FAILED,
    )


def test_backup_operation_path_cannot_traverse_an_existing_file(tmp_path: Path) -> None:
    scan_root = tmp_path / "library"
    scan_root.mkdir()
    source_path = scan_root / "01.flac"
    source_path.write_bytes(b"audio")
    backup_root = tmp_path / "backups"
    backup_root.mkdir()
    (backup_root / "APPLY-1").write_bytes(b"blocks operation directory")
    source = make_source("file-1", str(source_path), "Old title")
    proposed = build_change_set(
        source,
        make_reviews(source, "New title"),
        RenameDecision.KEEP_FILENAME,
    )

    class Registry:
        def detect(self, path: Path) -> StubAdapter:
            del path

            return StubAdapter()

    snapshot = LocalApplyPreflightInspector(
        registry=Registry(),  # type: ignore[arg-type]
    ).inspect(
        source,
        proposed,
        BackupPolicy(True, backup_root, scan_root, "APPLY-1"),
    )

    assert not snapshot.validation.backup_destination_available


def test_missing_backup_root_cannot_hide_an_existing_file_ancestor(
    tmp_path: Path,
) -> None:
    scan_root = tmp_path / "library"
    scan_root.mkdir()
    source_path = scan_root / "01.flac"
    source_path.write_bytes(b"audio")
    blocking_path = tmp_path / "blocking-file"
    blocking_path.write_bytes(b"not a directory")
    backup_root = blocking_path / "nested"
    source = make_source("file-1", str(source_path), "Old title")
    proposed = build_change_set(
        source,
        make_reviews(source, "New title"),
        RenameDecision.KEEP_FILENAME,
    )

    class Registry:
        def detect(self, path: Path) -> StubAdapter:
            del path

            return StubAdapter()

    snapshot = LocalApplyPreflightInspector(
        registry=Registry(),  # type: ignore[arg-type]
    ).inspect(
        source,
        proposed,
        BackupPolicy(True, backup_root, scan_root, "APPLY-1"),
    )

    assert not snapshot.validation.backup_root_writable
    assert not snapshot.validation.backup_destination_available


def test_same_name_apply_rename_no_op_reports_not_requested() -> None:
    source = make_source("file-1", "library/a/Same.flac", "Same")
    file_request = ApplyFileRequest(
        source=source,
        reviews=make_reviews(source, "Same"),
        rename_decision=RenameDecision.APPLY_RENAME,
        track_mapping_resolved=True,
    )
    captured: list[ProcessingReportRequest] = []

    class ForbiddenPreflight:
        def inspect(self, *_args: object) -> FilePreflightSnapshot:
            raise AssertionError("same-name no-op needs no preflight")

    class ForbiddenWriter:
        def apply_file(self, *_args: object) -> FileApplyResult:
            raise AssertionError("same-name no-op needs no transaction")

    class ReportWriter:
        def write(
            self,
            *,
            policy: ReportOutputPolicy,
            request: ProcessingReportRequest,
        ) -> ReportWriteResult:
            del policy
            captured.append(request)

            return ReportWriteResult(ReportWriteStatus.WRITTEN, Path("report.json"), None)

    base = make_batch(
        (ApplyGroupRequest("group-1", 2, None, (file_request,)),),
        rename_template="%title%",
    )
    result = ApplyService(
        preflight=ForbiddenPreflight(),  # type: ignore[arg-type]
        writer=ForbiddenWriter(),  # type: ignore[arg-type]
        report_writer=ReportWriter(),
    ).apply(
        replace(base, report=ReportOutputPolicy(enabled=True)),
        cancellation=NeverCancelledToken(),
    )

    assert result.status is ApplyBatchStatus.SUCCEEDED
    assert captured[0].files[0].rename_result is ReportRenameResult.NOT_REQUESTED


def test_case_only_rename_is_blocked_before_the_transaction_boundary() -> None:
    adapter = StubAdapter()
    inspected: list[str] = []

    class ValidPreflight:
        def inspect(
            self,
            source: LocalMediaFile,
            proposed_changes: FileChangeSet,
            backup: BackupPolicy,
        ) -> FilePreflightSnapshot:
            del proposed_changes, backup
            inspected.append(source.file_id)

            return make_preflight_snapshot(source, adapter)

    class ForbiddenWriter:
        def apply_file(self, *_args: object) -> FileApplyResult:
            raise AssertionError("V1 cannot publish a case-only rename safely")

    file_request = make_file_request(
        "case-only",
        "library/a/Old title.flac",
        new_title="old title",
        rename_decision=RenameDecision.APPLY_RENAME,
    )
    result = ApplyService(
        preflight=ValidPreflight(),
        writer=ForbiddenWriter(),  # type: ignore[arg-type]
    ).apply(
        make_batch(
            (ApplyGroupRequest("group-1", 2, None, (file_request,)),),
            rename_template="%title%",
        ),
        cancellation=NeverCancelledToken(),
    )

    assert inspected == ["case-only"]
    outcome = result.groups[0].files[0]
    assert result.status is ApplyBatchStatus.FAILED
    assert outcome.skip_reason is ApplySkipReason.BATCH_PREFLIGHT_BLOCKED
    assert ChangeIssueCode.CASE_ONLY_RENAME_UNSUPPORTED in {
        issue.code for issue in outcome.change_set.validation.issues
    }


def test_album_failure_does_not_stop_a_later_true_no_change_file() -> None:
    adapter = StubAdapter()

    class ValidPreflight:
        def inspect(
            self,
            source: LocalMediaFile,
            proposed_changes: FileChangeSet,
            backup: BackupPolicy,
        ) -> FilePreflightSnapshot:
            del proposed_changes, backup

            return make_preflight_snapshot(source, adapter)

    class FailingWriter:
        def apply_file(self, source: LocalMediaFile, *_args: object) -> FileApplyResult:
            return FileApplyResult(
                source.path,
                source.path,
                FileApplyStatus.FAILED,
                FileTransactionStage.WRITING_METADATA,
                (Issue(MediaErrorCode.TAG_WRITE_FAILED, "The tag write failed."),),
            )

    no_change = make_file_request(
        "no-change",
        "library/a/02.flac",
        new_title="Old no-change",
    )
    result = ApplyService(
        preflight=ValidPreflight(),
        writer=FailingWriter(),  # type: ignore[arg-type]
    ).apply(
        make_batch(
            (
                ApplyGroupRequest(
                    "group-1",
                    2,
                    None,
                    (
                        make_file_request("failed", "library/a/01.flac"),
                        no_change,
                    ),
                ),
            )
        ),
        cancellation=NeverCancelledToken(),
    )

    assert result.status is ApplyBatchStatus.FAILED
    assert result.groups[0].files[1].status is ApplyFileOutcomeStatus.NO_CHANGES
    assert result.groups[0].files[1].skip_reason is None


def test_post_transaction_cancellation_marks_a_later_no_change_file_cancelled() -> None:
    token = MutableCancellationToken()
    adapter = StubAdapter()

    class ValidPreflight:
        def inspect(
            self,
            source: LocalMediaFile,
            proposed_changes: FileChangeSet,
            backup: BackupPolicy,
        ) -> FilePreflightSnapshot:
            del proposed_changes, backup

            return make_preflight_snapshot(source, adapter)

    class CancellingWriter:
        def apply_file(self, source: LocalMediaFile, *_args: object) -> FileApplyResult:
            token.cancel()

            return FileApplyResult(
                source.path,
                source.path,
                FileApplyStatus.SUCCEEDED,
                FileTransactionStage.COMPLETED,
            )

    no_change = make_file_request(
        "no-change",
        "library/a/02.flac",
        new_title="Old no-change",
    )
    result = ApplyService(
        preflight=ValidPreflight(),
        writer=CancellingWriter(),  # type: ignore[arg-type]
    ).apply(
        make_batch(
            (
                ApplyGroupRequest(
                    "group-1",
                    2,
                    None,
                    (
                        make_file_request("applied", "library/a/01.flac"),
                        no_change,
                    ),
                ),
            )
        ),
        cancellation=token,
    )

    assert result.status is ApplyBatchStatus.CANCELLED
    assert result.groups[0].files[0].status is ApplyFileOutcomeStatus.APPLIED
    assert result.groups[0].files[1].status is ApplyFileOutcomeStatus.SKIPPED
    assert (
        result.groups[0].files[1].skip_reason
        is ApplySkipReason.CANCELLED_BEFORE_START
    )


def test_all_no_change_batch_never_enters_the_apply_stage() -> None:
    no_change = make_file_request(
        "no-change",
        "library/a/01.flac",
        new_title="Old no-change",
    )
    events = RecordingEventSink([])

    class ForbiddenPreflight:
        def inspect(self, *_args: object) -> FilePreflightSnapshot:
            raise AssertionError("a no-change batch needs no external preflight")

    class ForbiddenWriter:
        def apply_file(self, *_args: object) -> FileApplyResult:
            raise AssertionError("a no-change batch needs no transaction")

    result = ApplyService(
        preflight=ForbiddenPreflight(),  # type: ignore[arg-type]
        writer=ForbiddenWriter(),  # type: ignore[arg-type]
    ).apply(
        make_batch((ApplyGroupRequest("group-1", 2, None, (no_change,)),)),
        cancellation=NeverCancelledToken(),
        events=events,
    )

    assert result.status is ApplyBatchStatus.SUCCEEDED
    assert result.groups[0].files[0].status is ApplyFileOutcomeStatus.NO_CHANGES
    assert not any(
        isinstance(event, OperationStageChanged)
        and event.stage == ApplyStage.APPLYING_FILES
        for event in events.events
    )


def test_report_projection_failure_is_isolated_after_audio_failure() -> None:
    adapter = StubAdapter()

    class ValidPreflight:
        def inspect(
            self,
            source: LocalMediaFile,
            proposed_changes: FileChangeSet,
            backup: BackupPolicy,
        ) -> FilePreflightSnapshot:
            del proposed_changes, backup

            return make_preflight_snapshot(source, adapter)

    class MalformedFailureWriter:
        def apply_file(self, source: LocalMediaFile, *_args: object) -> FileApplyResult:
            return FileApplyResult(
                source.path,
                source.path,
                FileApplyStatus.FAILED,
                FileTransactionStage.WRITING_METADATA,
                (Issue("NOT_A_TYPED_CODE", "The lower boundary returned bad data."),),  # type: ignore[arg-type]
            )

    class ForbiddenReportWriter:
        def write(self, **_kwargs: object) -> ReportWriteResult:
            raise AssertionError("invalid report projection must not reach the writer")

    base = make_batch(
        (
            ApplyGroupRequest(
                "group-1",
                2,
                None,
                (make_file_request("failed", "library/a/01.flac"),),
            ),
        )
    )
    result = ApplyService(
        preflight=ValidPreflight(),
        writer=MalformedFailureWriter(),  # type: ignore[arg-type]
        report_writer=ForbiddenReportWriter(),
    ).apply(
        replace(base, report=ReportOutputPolicy(enabled=True)),
        cancellation=NeverCancelledToken(),
    )

    assert result.status is ApplyBatchStatus.FAILED
    assert result.groups[0].files[0].status is ApplyFileOutcomeStatus.FAILED
    assert result.report_result == ReportWriteResult(
        ReportWriteStatus.FAILED,
        None,
        ReportErrorCode.INVALID_REPORT,
    )


def test_cancellation_between_preflight_and_first_write_cancels_later_no_op() -> None:
    adapter = StubAdapter()

    class BoundaryCancellationToken:
        def __init__(self) -> None:
            self.check_count = 0

        def is_cancelled(self) -> bool:
            self.check_count += 1

            return self.check_count >= 5

        def raise_if_cancelled(self) -> None:
            if self.is_cancelled():
                raise RuntimeError("the writer must never receive this cancellation")

    class ValidPreflight:
        def inspect(
            self,
            source: LocalMediaFile,
            proposed_changes: FileChangeSet,
            backup: BackupPolicy,
        ) -> FilePreflightSnapshot:
            del proposed_changes, backup

            return make_preflight_snapshot(source, adapter)

    class ForbiddenWriter:
        def apply_file(self, *_args: object) -> FileApplyResult:
            raise AssertionError("cancellation at the file boundary must prevent the write")

    token = BoundaryCancellationToken()
    no_change = make_file_request(
        "no-change",
        "library/a/02.flac",
        new_title="Old no-change",
    )
    result = ApplyService(
        preflight=ValidPreflight(),
        writer=ForbiddenWriter(),  # type: ignore[arg-type]
    ).apply(
        make_batch(
            (
                ApplyGroupRequest(
                    "group-1",
                    2,
                    None,
                    (
                        make_file_request("changed", "library/a/01.flac"),
                        no_change,
                    ),
                ),
            )
        ),
        cancellation=token,
    )

    assert result.status is ApplyBatchStatus.CANCELLED
    assert tuple(file.status for file in result.groups[0].files) == (
        ApplyFileOutcomeStatus.SKIPPED,
        ApplyFileOutcomeStatus.SKIPPED,
    )
    assert tuple(file.skip_reason for file in result.groups[0].files) == (
        ApplySkipReason.CANCELLED_BEFORE_START,
        ApplySkipReason.CANCELLED_BEFORE_START,
    )


def test_batch_result_rejects_work_after_cancellation_and_mislabelled_blockers() -> None:
    first_request = make_file_request("first", "library/a/01.flac")
    second_request = make_file_request("second", "library/b/01.flac")
    first_set = build_change_set(
        first_request.source,
        first_request.reviews,
        first_request.rename_decision,
    )
    second_set = build_change_set(
        second_request.source,
        second_request.reviews,
        second_request.rename_decision,
    )
    cancelled_transaction = ApplyFileOutcome(
        first_request.source.file_id,
        first_request.source.path,
        None,
        first_request.reviews,
        first_set,
        ApplyFileOutcomeStatus.CANCELLED,
        FileApplyResult(
            first_request.source.path,
            first_request.source.path,
            FileApplyStatus.CANCELLED,
            FileTransactionStage.COPYING_TEMPORARY,
        ),
    )
    applied = ApplyFileOutcome(
        second_request.source.file_id,
        second_request.source.path,
        None,
        second_request.reviews,
        second_set,
        ApplyFileOutcomeStatus.APPLIED,
        FileApplyResult(
            second_request.source.path,
            second_request.source.path,
            FileApplyStatus.SUCCEEDED,
            FileTransactionStage.COMPLETED,
        ),
    )

    with pytest.raises(ValueError, match="after cancellation"):
        ApplyBatchResult(
            "APPLY-1",
            3,
            1,
            ApplyBatchStatus.CANCELLED,
            (
                ApplyGroupOutcome("group-1", 2, (cancelled_transaction,)),
                ApplyGroupOutcome("group-2", 2, (applied,)),
            ),
            ReportWriteResult(ReportWriteStatus.DISABLED, None, None),
        )

    cancelled_before_start = replace(
        cancelled_transaction,
        status=ApplyFileOutcomeStatus.SKIPPED,
        transaction_result=None,
        skip_reason=ApplySkipReason.CANCELLED_BEFORE_START,
    )

    with pytest.raises(ValueError, match="after cancellation"):
        ApplyBatchResult(
            "APPLY-1",
            3,
            1,
            ApplyBatchStatus.CANCELLED,
            (
                ApplyGroupOutcome("group-1", 2, (cancelled_before_start,)),
                ApplyGroupOutcome("group-2", 2, (applied,)),
            ),
            ReportWriteResult(ReportWriteStatus.DISABLED, None, None),
        )

    blocked_set = build_change_set(
        first_request.source,
        first_request.reviews,
        first_request.rename_decision,
        validation=ChangeValidationFacts(directory_writable=False),
    )
    mislabelled_blocker = replace(cancelled_before_start, change_set=blocked_set)

    with pytest.raises(ValueError, match="BATCH_PREFLIGHT_BLOCKED"):
        ApplyBatchResult(
            "APPLY-1",
            3,
            1,
            ApplyBatchStatus.CANCELLED,
            (ApplyGroupOutcome("group-1", 2, (mislabelled_blocker,)),),
            ReportWriteResult(ReportWriteStatus.DISABLED, None, None),
        )


def test_same_storage_capacity_combines_temp_peak_and_cumulative_backups() -> None:
    # A shared volume must cover both the largest temporary copy and all backups.
    # Testing only each budget separately would miss their simultaneous space use.
    adapter = StubAdapter()
    writer_calls: list[str] = []

    class CapacityPreflight:
        def inspect(
            self,
            source: LocalMediaFile,
            proposed_changes: FileChangeSet,
            backup: BackupPolicy,
        ) -> FilePreflightSnapshot:
            del proposed_changes
            assert backup.enabled
            storage = PathStorageSnapshot("volume-1", free_bytes=150)

            return FilePreflightSnapshot(
                source.file_id,
                adapter,
                ChangeValidationFacts(),
                FileCapacitySnapshot(
                    source_size_bytes=60,
                    temporary_storage=storage,
                    backup_storage=storage,
                    backup_target=Path("backups/APPLY-1") / source.file_id,
                ),
            )

    class ForbiddenWriter:
        def apply_file(self, source: LocalMediaFile, *_args: object) -> FileApplyResult:
            writer_calls.append(source.file_id)
            raise AssertionError("insufficient cumulative capacity must block the batch")

    base = make_batch(
        (
            ApplyGroupRequest(
                "group-1",
                7,
                None,
                (
                    make_file_request("file-1", "library/a/01.flac"),
                    make_file_request("file-2", "library/b/01.flac"),
                ),
            ),
        )
    )
    request = ApplyBatchRequest(
        operation_id=base.operation_id,
        base_session_revision=base.base_session_revision,
        base_library_revision=base.base_library_revision,
        scan_root=base.scan_root,
        groups=base.groups,
        backup=BackupPolicy(True, Path("backups"), Path("library"), "APPLY-1"),
        report=base.report,
        rename_template=base.rename_template,
        rename_policy=base.rename_policy,
    )

    result = ApplyService(
        preflight=CapacityPreflight(),
        writer=ForbiddenWriter(),  # type: ignore[arg-type]
    ).apply(request, cancellation=NeverCancelledToken())

    assert writer_calls == []
    assert result.status is ApplyBatchStatus.FAILED
    assert all(
        ChangeIssueCode.INSUFFICIENT_BACKUP_SPACE
        in {issue.code for issue in outcome.change_set.validation.issues}
        for outcome in result.groups[0].files
    )
