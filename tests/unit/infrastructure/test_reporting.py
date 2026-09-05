"""Tests for the explicit, privacy-safe processing report boundary."""

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from metadata_polisher.application.changes import (
    ChangeIssueCode,
    ChangeIssueSeverity,
    ChangeValidationIssue,
    ChangeValidationResult,
    FileChangeSet,
    RenameChange,
    RenameDecision,
)
from metadata_polisher.domain.errors import Issue, MediaErrorCode
from metadata_polisher.domain.matching import MetadataProvenance
from metadata_polisher.domain.metadata import (
    FieldReadState,
    MetadataChange,
    MetadataField,
    MetadataSnapshot,
    Position,
)
from metadata_polisher.domain.review import (
    ConsolidatedProposal,
    DecisionOrigin,
    FieldConfidence,
    FieldDecisionKind,
    FieldProposal,
    FieldReviewState,
    ReviewReasonCode,
)
from metadata_polisher.execution.events import FileApplyStatus, FileTransactionStage
from metadata_polisher.infrastructure import reporting
from metadata_polisher.infrastructure.transaction import FileApplyResult
from metadata_polisher.matching.release_scoring import MatchReasonCode


# The expected document is assembled independently of the report serializer,
# so newly leaked fields or inaccurate partial outcomes remain observable.
def test_serialises_partial_completion_with_an_exact_allowlisted_schema() -> None:
    title_change = reporting.ReportFieldChange(
        field=MetadataField.TITLE,
        old_value=None,
        new_value="星の歌",
        decision=FieldDecisionKind.USE_PROPOSAL,
        decision_origin=DecisionOrigin.DEFAULT,
        provider_sources=(
            reporting.ProposalSourceReference("vgmdb", "vgmdb", "album-42"),
            reporting.ProposalSourceReference("musicbrainz", "musicbrainz", "release-42"),
        ),
        reason_codes=(
            ReviewReasonCode.PROPOSAL_SELECTED,
            ReviewReasonCode.EXISTING_VALUE_MISSING,
        ),
    )
    track_change = reporting.ReportFieldChange(
        field=MetadataField.TRACK,
        old_value=Position(number=1),
        new_value=Position(number=1, total=12),
        decision=FieldDecisionKind.USE_MANUAL,
        decision_origin=DecisionOrigin.USER,
    )
    release = reporting.SelectedReleaseReference(
        engine_id="musicbrainz",
        source_id="musicbrainz",
        release_id="release-42",
        medium_index=0,
    )
    files = (
        reporting.ReportFileEntry(
            selected_release=release,
            original_path=Path("01.flac"),
            final_path=Path("01. 星の歌.flac"),
            status=reporting.ReportFileStatus.SUCCEEDED,
            completed_stage=FileTransactionStage.COMPLETED,
            changes=(title_change, track_change),
            reason_codes=(
                MatchReasonCode.TRACK_MAPPING_COMPLETE,
                MatchReasonCode.ALBUM_TITLE_EXACT,
            ),
            validation_codes=(ChangeIssueCode.FILENAME_REPAIRED,),
            apply_codes=(),
            rename_result=reporting.ReportRenameResult.SUCCEEDED,
        ),
        reporting.ReportFileEntry(
            selected_release=release,
            original_path=Path("02.flac"),
            final_path=Path("02.flac"),
            status=reporting.ReportFileStatus.FAILED,
            completed_stage=FileTransactionStage.WRITING_METADATA,
            changes=(),
            reason_codes=(MatchReasonCode.TRACK_MAPPING_PARTIAL,),
            validation_codes=(),
            apply_codes=(MediaErrorCode.TAG_WRITE_FAILED,),
            rename_result=reporting.ReportRenameResult.NOT_ATTEMPTED,
        ),
        reporting.ReportFileEntry(
            selected_release=None,
            original_path=Path("03.flac"),
            final_path=Path("03.flac"),
            status=reporting.ReportFileStatus.SKIPPED,
            completed_stage=FileTransactionStage.NOT_STARTED,
            changes=(),
            reason_codes=(),
            validation_codes=(),
            apply_codes=(),
            rename_result=reporting.ReportRenameResult.NOT_REQUESTED,
        ),
    )
    report = reporting.ProcessingReport(
        operation_id="APPLY-0042",
        occurred_at=datetime(2026, 9, 5, 1, 2, 3, tzinfo=UTC),
        result=reporting.ReportOperationResult.PARTIAL,
        files=files,
    )

    encoded = reporting.serialise_processing_report(report)

    expected = {
        "schema_version": 1,
        "operation": {
            "id": "APPLY-0042",
            "time_utc": "2026-09-05T01:02:03.000000Z",
            "result": "partial",
        },
        "files": [
            {
                "selected_release": {
                    "engine_id": "musicbrainz",
                    "source_id": "musicbrainz",
                    "release_id": "release-42",
                    "medium_index": 0,
                },
                "original_path": "01.flac",
                "final_path": "01. 星の歌.flac",
                "status": "succeeded",
                "completed_stage": "completed",
                "changes": [
                    {
                        "field": "title",
                        "old": None,
                        "new": "星の歌",
                        "decision": "use_proposal",
                        "decision_origin": "default",
                        "provider_sources": [
                            {
                                "engine_id": "musicbrainz",
                                "source_id": "musicbrainz",
                                "record_id": "release-42",
                            },
                            {
                                "engine_id": "vgmdb",
                                "source_id": "vgmdb",
                                "record_id": "album-42",
                            },
                        ],
                        "reason_codes": [
                            "EXISTING_VALUE_MISSING",
                            "PROPOSAL_SELECTED",
                        ],
                    },
                    {
                        "field": "track",
                        "old": {"number": 1, "total": None},
                        "new": {"number": 1, "total": 12},
                        "decision": "use_manual",
                        "decision_origin": "user",
                        "provider_sources": [],
                        "reason_codes": [],
                    },
                ],
                "reason_codes": ["ALBUM_TITLE_EXACT", "TRACK_MAPPING_COMPLETE"],
                "validation_codes": ["FILENAME_REPAIRED"],
                "apply_codes": [],
                "rename_result": "succeeded",
            },
            {
                "selected_release": {
                    "engine_id": "musicbrainz",
                    "source_id": "musicbrainz",
                    "release_id": "release-42",
                    "medium_index": 0,
                },
                "original_path": "02.flac",
                "final_path": "02.flac",
                "status": "failed",
                "completed_stage": "writing_metadata",
                "changes": [],
                "reason_codes": ["TRACK_MAPPING_PARTIAL"],
                "validation_codes": [],
                "apply_codes": ["TAG_WRITE_FAILED"],
                "rename_result": "not_attempted",
            },
            {
                "selected_release": None,
                "original_path": "03.flac",
                "final_path": "03.flac",
                "status": "skipped",
                "completed_stage": "not_started",
                "changes": [],
                "reason_codes": [],
                "validation_codes": [],
                "apply_codes": [],
                "rename_result": "not_requested",
            },
        ],
    }

    assert encoded == json.dumps(expected, ensure_ascii=False, indent=2) + "\n"
    assert "星の歌" in encoded


def _proposal(
    *,
    source_id: str,
    record_id: str,
    source_url: str,
    value: str,
) -> ConsolidatedProposal:
    member = FieldProposal(
        field=MetadataField.TITLE,
        value=value,
        confidence=FieldConfidence.HIGH,
        provenance=MetadataProvenance(
            engine_id="engine",
            source_id=source_id,
            record_id=record_id,
            source_url=source_url,
            language="ja",
            operation_id="LOOKUP-0009",
        ),
        language="ja",
        script="Jpan",
        reason_codes=(ReviewReasonCode.FIELD_MATCH_HIGH,),
    )

    return ConsolidatedProposal(
        field=MetadataField.TITLE,
        value=value,
        language="ja",
        script="Jpan",
        confidence=FieldConfidence.HIGH,
        members=(member,),
        reason_codes=(ReviewReasonCode.PROPOSAL_CONFIDENT,),
    )


def test_builders_emit_only_selected_safe_provenance_and_typed_codes() -> None:
    selected = _proposal(
        source_id="selected-source",
        record_id="selected-record",
        source_url=(
            "https://provider.invalid/release?token=RAW_PROVIDER_SECRET"
            "&Authorization=Bearer+AUTH_SECRET"
        ),
        value="星の歌",
    )
    unselected = _proposal(
        source_id="Cookie=TRANSIENT_SECRET",
        record_id="unselected-record",
        source_url="https://provider.invalid/raw-payload-secret",
        value="Different title",
    )
    review = FieldReviewState(
        field=MetadataField.TITLE,
        read_state=FieldReadState.PRESENT,
        existing_value="Old title",
        proposals=(selected, unselected),
        decision=FieldDecisionKind.USE_PROPOSAL,
        selected_proposal=selected,
        manual_value=None,
        decision_origin=DecisionOrigin.USER,
        requires_review=False,
        reason_codes=(ReviewReasonCode.PROPOSAL_SELECTED,),
    )
    metadata_change = MetadataChange(
        field=MetadataField.TITLE,
        old_value="Old title",
        new_value="星の歌",
    )
    source_path = Path("album/01.flac")
    change_set = FileChangeSet(
        file_id="file-1",
        metadata_changes=(metadata_change,),
        rename_change=None,
        final_metadata=MetadataSnapshot(title="星の歌"),
        rename_decision=RenameDecision.KEEP_FILENAME,
        rename_preview=None,
        validation=ChangeValidationResult(
            issues=(
                ChangeValidationIssue(
                    code=ChangeIssueCode.FILENAME_REPAIRED,
                    severity=ChangeIssueSeverity.WARNING,
                    message="lyrics=UNRELATED_TAG_SECRET; artwork=ARTWORK_SECRET",
                ),
            )
        ),
    )
    apply_result = FileApplyResult(
        source_path=source_path,
        final_path=source_path,
        status=FileApplyStatus.FAILED,
        completed_stage=FileTransactionStage.WRITING_METADATA,
        issues=(
            Issue(
                code=MediaErrorCode.TAG_WRITE_FAILED,
                message="Cookie: APPLY_MESSAGE_SECRET",
                technical_detail="Authorization: Bearer APPLY_DETAIL_SECRET",
            ),
        ),
    )

    entry = reporting.ReportFileEntry.from_apply_result(
        selected_release=reporting.SelectedReleaseReference(
            engine_id="engine",
            source_id="selected-source",
            release_id="release-9",
            medium_index=1,
        ),
        change_set=change_set,
        reviews=(review,),
        apply_result=apply_result,
        reason_codes=(MatchReasonCode.TRACK_MAPPING_COMPLETE,),
    )
    encoded = reporting.serialise_processing_report(
        reporting.ProcessingReport(
            operation_id="APPLY-0009",
            occurred_at=datetime(2026, 9, 5, tzinfo=UTC),
            result=reporting.ReportOperationResult.FAILED,
            files=(entry,),
        )
    )

    assert [source.source_id for source in entry.changes[0].provider_sources] == [
        "selected-source"
    ]
    assert entry.changes[0].reason_codes == (
        ReviewReasonCode.FIELD_MATCH_HIGH,
        ReviewReasonCode.PROPOSAL_CONFIDENT,
        ReviewReasonCode.PROPOSAL_SELECTED,
    )
    assert entry.validation_codes == (ChangeIssueCode.FILENAME_REPAIRED,)
    assert entry.apply_codes == (MediaErrorCode.TAG_WRITE_FAILED,)

    for forbidden in (
        "source_url",
        "RAW_PROVIDER_SECRET",
        "AUTH_SECRET",
        "TRANSIENT_SECRET",
        "raw-payload-secret",
        "UNRELATED_TAG_SECRET",
        "ARTWORK_SECRET",
        "APPLY_MESSAGE_SECRET",
        "APPLY_DETAIL_SECRET",
        "Cookie",
        "Authorization",
        "lyrics",
        "artwork",
    ):
        assert forbidden not in encoded


def _empty_report_request() -> reporting.ProcessingReportRequest:
    return reporting.ProcessingReportRequest(
        operation_id="APPLY-0042",
        result=reporting.ReportOperationResult.SUCCEEDED,
        files=(),
    )


def test_disabled_writer_does_no_filesystem_or_clock_work(tmp_path: Path) -> None:
    default_directory = tmp_path / "must-not-exist"

    def forbidden_clock() -> datetime:
        raise AssertionError("the disabled writer must not read the clock")

    writer = reporting.ProcessingReportWriter(
        default_directory=default_directory,
        clock=forbidden_clock,
    )

    result = writer.write(
        policy=reporting.ReportOutputPolicy(enabled=False, directory=""),
        request=_empty_report_request(),
    )

    assert result == reporting.ReportWriteResult(
        status=reporting.ReportWriteStatus.DISABLED,
        path=None,
        error_code=None,
    )
    assert not default_directory.exists()


def test_enabled_writer_uses_default_directory_and_atomically_replaces(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    default_directory = tmp_path / "reports"
    fixed_time = datetime(2026, 9, 5, 1, 2, 3, tzinfo=UTC)
    fsync_calls: list[int] = []
    replacements: list[tuple[Path, Path]] = []
    real_fsync = os.fsync
    real_replace = os.replace

    def recording_fsync(file_descriptor: int) -> None:
        fsync_calls.append(file_descriptor)
        real_fsync(file_descriptor)

    def recording_replace(source: str | bytes | os.PathLike[str] | os.PathLike[bytes],
                          destination: str | bytes | os.PathLike[str] | os.PathLike[bytes]) -> None:
        source_path = Path(source)
        destination_path = Path(destination)
        replacements.append((source_path, destination_path))
        assert source_path.parent == destination_path.parent
        assert source_path != destination_path
        assert source_path.exists()
        real_replace(source, destination)

    monkeypatch.setattr(reporting.os, "fsync", recording_fsync)
    monkeypatch.setattr(reporting.os, "replace", recording_replace)
    writer = reporting.ProcessingReportWriter(
        default_directory=default_directory,
        clock=lambda: fixed_time,
    )

    result = writer.write(
        policy=reporting.ReportOutputPolicy(enabled=True, directory="   "),
        request=_empty_report_request(),
    )

    expected_path = default_directory / "metadata-polisher-APPLY-0042.json"
    assert result == reporting.ReportWriteResult(
        status=reporting.ReportWriteStatus.WRITTEN,
        path=expected_path,
        error_code=None,
    )
    assert fsync_calls
    assert replacements == [(replacements[0][0], expected_path)]
    assert not replacements[0][0].exists()
    assert list(default_directory.iterdir()) == [expected_path]
    assert expected_path.read_text(encoding="utf-8") == reporting.serialise_processing_report(
        reporting.ProcessingReport(
            operation_id="APPLY-0042",
            occurred_at=fixed_time,
            result=reporting.ReportOperationResult.SUCCEEDED,
            files=(),
        )
    )


# Inject failure after temporary output exists to exercise cleanup while
# proving unrelated files in the report directory are never removed.
def test_writer_failure_removes_only_its_temporary_sibling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_directory = tmp_path / "chosen-reports"
    output_directory.mkdir()
    target = output_directory / "metadata-polisher-APPLY-0042.json"
    target.write_text("previous report\n", encoding="utf-8")
    unrelated = output_directory / ".unrelated.tmp"
    unrelated.write_text("keep me\n", encoding="utf-8")
    attempted_temporaries: list[Path] = []

    def failing_replace(source: object, _destination: object) -> None:
        attempted_temporaries.append(Path(source))  # type: ignore[arg-type]
        raise OSError("replace failed")

    monkeypatch.setattr(reporting.os, "replace", failing_replace)
    writer = reporting.ProcessingReportWriter(
        default_directory=tmp_path / "unused-default",
        clock=lambda: datetime(2026, 9, 5, tzinfo=UTC),
    )

    result = writer.write(
        policy=reporting.ReportOutputPolicy(
            enabled=True,
            directory=str(output_directory),
        ),
        request=_empty_report_request(),
    )

    assert result == reporting.ReportWriteResult(
        status=reporting.ReportWriteStatus.FAILED,
        path=None,
        error_code=reporting.ReportErrorCode.WRITE_FAILED,
    )
    assert len(attempted_temporaries) == 1
    assert not attempted_temporaries[0].exists()
    assert unrelated.read_text(encoding="utf-8") == "keep me\n"
    assert target.read_text(encoding="utf-8") == "previous report\n"


def test_invalid_utc_clock_result_fails_before_creating_output_directory(
    tmp_path: Path,
) -> None:
    output_directory = tmp_path / "reports"
    writer = reporting.ProcessingReportWriter(
        default_directory=output_directory,
        clock=lambda: datetime(2026, 9, 5),
    )

    result = writer.write(
        policy=reporting.ReportOutputPolicy(enabled=True, directory=""),
        request=_empty_report_request(),
    )

    assert result == reporting.ReportWriteResult(
        status=reporting.ReportWriteStatus.FAILED,
        path=None,
        error_code=reporting.ReportErrorCode.INVALID_REPORT,
    )
    assert not output_directory.exists()


@pytest.mark.parametrize("operation_id", ("../escape", "bad:name", "CON", ""))
def test_operation_id_must_be_one_safe_filename_component(operation_id: str) -> None:
    with pytest.raises(ValueError, match="operation_id"):
        reporting.ProcessingReportRequest(
            operation_id=operation_id,
            result=reporting.ReportOperationResult.SUCCEEDED,
            files=(),
        )


def test_apply_result_builder_rejects_a_false_successful_rename() -> None:
    old_path = Path("album/01.flac")
    new_path = Path("album/01. New.flac")
    rename = RenameChange(old_path=old_path, new_path=new_path)
    change_set = FileChangeSet(
        file_id="file-1",
        metadata_changes=(),
        rename_change=rename,
        final_metadata=MetadataSnapshot(),
        rename_decision=RenameDecision.APPLY_RENAME,
        rename_preview=rename,
        validation=ChangeValidationResult(),
    )
    inconsistent_result = FileApplyResult(
        source_path=old_path,
        final_path=old_path,
        status=FileApplyStatus.SUCCEEDED,
        completed_stage=FileTransactionStage.COMPLETED,
    )

    with pytest.raises(ValueError, match="final_path"):
        reporting.ReportFileEntry.from_apply_result(
            selected_release=None,
            change_set=change_set,
            reviews=(),
            apply_result=inconsistent_result,
        )


def test_file_schema_rejects_a_rename_success_on_a_failed_file() -> None:
    with pytest.raises(ValueError, match="rename_result"):
        reporting.ReportFileEntry(
            selected_release=None,
            original_path=Path("01.flac"),
            final_path=Path("01. Renamed.flac"),
            status=reporting.ReportFileStatus.FAILED,
            completed_stage=FileTransactionStage.RENAMING,
            changes=(),
            reason_codes=(),
            validation_codes=(),
            apply_codes=(MediaErrorCode.RENAME_FAILED,),
            rename_result=reporting.ReportRenameResult.SUCCEEDED,
        )


def test_frozen_schema_copies_ordered_inputs_and_encodes_multi_values() -> None:
    source_inputs = [
        reporting.ProposalSourceReference("engine", "source", "record")
    ]
    old_artists = ["Old Artist"]
    new_artists = ["New Artist", "Guest Artist"]
    reason_inputs = [ReviewReasonCode.PROPOSAL_SELECTED]
    change = reporting.ReportFieldChange(
        field=MetadataField.ARTISTS,
        old_value=old_artists,  # type: ignore[arg-type]
        new_value=new_artists,  # type: ignore[arg-type]
        decision=FieldDecisionKind.USE_PROPOSAL,
        decision_origin=DecisionOrigin.USER,
        provider_sources=source_inputs,  # type: ignore[arg-type]
        reason_codes=reason_inputs,  # type: ignore[arg-type]
    )
    change_inputs = [change]
    entry = reporting.ReportFileEntry(
        selected_release=None,
        original_path=Path("01.flac"),
        final_path=Path("01.flac"),
        status=reporting.ReportFileStatus.SUCCEEDED,
        completed_stage=FileTransactionStage.COMPLETED,
        changes=change_inputs,  # type: ignore[arg-type]
        reason_codes=(),
        validation_codes=(),
        apply_codes=(),
        rename_result=reporting.ReportRenameResult.NOT_REQUESTED,
    )
    file_inputs = [entry]
    report = reporting.ProcessingReport(
        operation_id="APPLY-0001",
        occurred_at=datetime(2026, 9, 5, tzinfo=UTC),
        result=reporting.ReportOperationResult.SUCCEEDED,
        files=file_inputs,  # type: ignore[arg-type]
    )

    source_inputs.clear()
    old_artists.append("Late mutation")
    new_artists.clear()
    reason_inputs.clear()
    change_inputs.clear()
    file_inputs.clear()

    document = json.loads(reporting.serialise_processing_report(report))
    encoded_change = document["files"][0]["changes"][0]
    assert encoded_change["old"] == ["Old Artist"]
    assert encoded_change["new"] == ["New Artist", "Guest Artist"]
    assert encoded_change["provider_sources"] == [
        {"engine_id": "engine", "source_id": "source", "record_id": "record"}
    ]
    assert encoded_change["reason_codes"] == ["PROPOSAL_SELECTED"]


# Cleanup failure is a distinct outcome: the renamed file verified, and the
# original still exists. It cannot be described as an ordinary failed rename.
def test_apply_result_builder_records_verified_rename_with_original_retained() -> None:
    old_path = Path("album/01.flac")
    new_path = Path("album/01. Renamed.flac")
    rename = RenameChange(old_path=old_path, new_path=new_path)
    change_set = FileChangeSet(
        file_id="file-1",
        metadata_changes=(),
        rename_change=rename,
        final_metadata=MetadataSnapshot(),
        rename_decision=RenameDecision.APPLY_RENAME,
        rename_preview=rename,
        validation=ChangeValidationResult(),
    )
    apply_result = FileApplyResult(
        source_path=old_path,
        final_path=new_path,
        status=FileApplyStatus.FAILED,
        completed_stage=FileTransactionStage.CLEANING_ORIGINAL,
        issues=(
            Issue(
                code=MediaErrorCode.CLEANUP_FAILED,
                message="The original could not be removed.",
            ),
        ),
    )

    entry = reporting.ReportFileEntry.from_apply_result(
        selected_release=None,
        change_set=change_set,
        reviews=(),
        apply_result=apply_result,
    )

    assert entry.original_path == old_path
    assert entry.final_path == new_path
    assert entry.status is reporting.ReportFileStatus.FAILED
    assert entry.rename_result is reporting.ReportRenameResult.ORIGINAL_RETAINED
    assert entry.apply_codes == (MediaErrorCode.CLEANUP_FAILED,)
