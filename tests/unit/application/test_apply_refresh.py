"""A committed write is reread separately from transaction and report outcomes."""

# A successful transaction and its later metadata reread are separate outcomes.
# These cases check that cancellation or a read error cannot erase a completed write.


from dataclasses import replace
from pathlib import Path

import pytest

from metadata_polisher.application.apply import (
    ApplyFileOutcomeStatus,
    ApplyGroupRequest,
    ApplyService,
)
from metadata_polisher.application.changes import RenameDecision
from metadata_polisher.domain.errors import Issue, MediaErrorCode
from metadata_polisher.domain.media import MediaReadResult
from metadata_polisher.execution.cancellation import MutableCancellationToken
from metadata_polisher.execution.events import (
    FileApplyStatus,
    FileTransactionStage,
    OperationStageChanged,
)
from metadata_polisher.infrastructure.transaction import FileApplyResult
from tests.unit.application.test_apply_service import (
    RecordingEventSink,
    make_batch,
    make_file_request,
    make_preflight_snapshot,
)


@pytest.mark.parametrize("rename", (False, True), ids=("metadata", "metadata-and-rename"))
def test_successful_apply_rereads_actual_final_path_even_after_cancellation(rename: bool) -> None:
    edited = make_file_request(
        "edited",
        "library/album/01.flac",
        rename_decision=RenameDecision.APPLY_RENAME if rename else RenameDecision.KEEP_FILENAME,
    )
    later = make_file_request("later", "library/album/02.flac")
    request = make_batch((ApplyGroupRequest("album", 0, None, (edited, later)),))
    cancellation = MutableCancellationToken()
    events = RecordingEventSink([])
    read_paths: list[Path] = []
    final_read = replace(
        edited.source.read_result,
        metadata=replace(edited.source.read_result.metadata, title="New edited"),
    )

    class Adapter:
        format_id = "flac"

        def read(self, path: Path) -> MediaReadResult:
            read_paths.append(path)

            return final_read

    adapter = Adapter()

    class Preflight:
        def inspect(self, source, _changes, _backup):
            return make_preflight_snapshot(source, adapter)

    class Writer:
        def apply_file(self, source, changes, *_args):
            assert source.file_id == "edited"
            cancellation.cancel()

            return FileApplyResult(
                source.path,
                changes.rename_change.new_path if changes.rename_change else source.path,
                FileApplyStatus.SUCCEEDED,
                FileTransactionStage.COMPLETED,
            )

    result = ApplyService(preflight=Preflight(), writer=Writer()).apply(
        request,
        cancellation=cancellation,
        events=events,
    )
    written, skipped = result.groups[0].files

    assert read_paths == [written.final_path]
    assert written.status is ApplyFileOutcomeStatus.APPLIED
    assert written.refreshed_source is not None
    assert written.refreshed_source.file_id == edited.source.file_id
    assert written.refreshed_source.path == written.final_path
    assert written.refreshed_source.read_result == final_read
    assert written.refresh_issues == ()
    assert not written.requires_rescan
    assert skipped.status is ApplyFileOutcomeStatus.SKIPPED
    assert skipped.refreshed_source is None
    assert any(
        isinstance(event, OperationStageChanged) and event.stage == "refreshing_files"
        for event in events.events
    )


def test_failed_refresh_retains_successful_write_and_precise_separate_issue() -> None:
    edited = make_file_request("edited", "library/album/01.flac")
    request = make_batch((ApplyGroupRequest("album", 0, None, (edited,)),))

    class Adapter:
        format_id = "flac"

        def read(self, _path: Path) -> MediaReadResult:
            raise OSError("The final file could not be reopened.")

    class Preflight:
        def inspect(self, source, _changes, _backup):
            return make_preflight_snapshot(source, Adapter())

    class Writer:
        def apply_file(self, source, *_args):
            return FileApplyResult(
                source.path,
                source.path,
                FileApplyStatus.SUCCEEDED,
                FileTransactionStage.COMPLETED,
            )

    result = ApplyService(preflight=Preflight(), writer=Writer()).apply(
        request,
        cancellation=MutableCancellationToken(),
    )
    outcome = result.groups[0].files[0]

    assert outcome.status is ApplyFileOutcomeStatus.APPLIED
    assert outcome.refreshed_source is None
    assert outcome.requires_rescan
    assert len(outcome.refresh_issues) == 1
    assert outcome.refresh_issues[0].code is MediaErrorCode.TAG_READ_FAILED
    assert "refresh" in outcome.refresh_issues[0].message.casefold()


def test_failed_and_never_started_files_are_not_reported_as_fresh() -> None:
    files = (
        make_file_request("failed", "library/album/01.flac"),
        make_file_request("skipped", "library/album/02.flac"),
        make_file_request("unchanged", "library/album/03.flac", new_title="Old unchanged"),
    )
    request = make_batch((ApplyGroupRequest("album", 0, None, files),))

    class Adapter:
        format_id = "flac"

        def read(self, _path: Path) -> MediaReadResult:
            raise AssertionError("No successful file is available for targeted refresh.")

    class Preflight:
        def inspect(self, source, _changes, _backup):
            return make_preflight_snapshot(source, Adapter())

    class Writer:
        def apply_file(self, source, *_args):
            return FileApplyResult(
                source.path,
                source.path,
                FileApplyStatus.FAILED,
                FileTransactionStage.WRITING_METADATA,
                (Issue(MediaErrorCode.TAG_WRITE_FAILED, "Temporary tag write failed."),),
            )

    result = ApplyService(preflight=Preflight(), writer=Writer()).apply(
        request,
        cancellation=MutableCancellationToken(),
    )

    assert tuple(outcome.status for outcome in result.groups[0].files) == (
        ApplyFileOutcomeStatus.FAILED,
        ApplyFileOutcomeStatus.SKIPPED,
        ApplyFileOutcomeStatus.NO_CHANGES,
    )
    assert all(outcome.refreshed_source is None for outcome in result.groups[0].files)
