"""Explicit, versioned processing-report schema and JSON serialisation."""

import json
import os
import re
import tempfile
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import cast

from metadata_polisher.application.changes import (
    ChangeIssueCode,
    FileChangeSet,
    RenameDecision,
)
from metadata_polisher.domain.errors import (
    MatchingErrorCode,
    MediaErrorCode,
    ProviderErrorCode,
)
from metadata_polisher.domain.matching import MetadataProvenance
from metadata_polisher.domain.metadata import MetadataChange, MetadataField, Position
from metadata_polisher.domain.review import (
    DecisionOrigin,
    FieldDecisionKind,
    FieldReviewState,
    FieldValue,
    ReviewReasonCode,
)
from metadata_polisher.execution.events import (
    FileApplyStatus,
    FileSkipReason,
    FileTransactionStage,
)
from metadata_polisher.infrastructure.transaction import FileApplyResult
from metadata_polisher.matching.release_scoring import MatchReasonCode

REPORT_SCHEMA_VERSION = 1


class ReportOperationResult(StrEnum):
    """Actual terminal state of the complete Apply operation."""

    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ReportFileStatus(StrEnum):
    """Actual terminal state of one file requested by an Apply operation."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"


class ReportRenameResult(StrEnum):
    """Actual rename outcome rather than the earlier review decision."""

    NOT_REQUESTED = "not_requested"
    NOT_ATTEMPTED = "not_attempted"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ORIGINAL_RETAINED = "original_retained"


class ReportWriteStatus(StrEnum):
    """Outcome of the optional report boundary, separate from audio Apply."""

    DISABLED = "disabled"
    WRITTEN = "written"
    FAILED = "failed"


class ReportErrorCode(StrEnum):
    """Stable reasons a requested report was not written."""

    INVALID_REPORT = "INVALID_REPORT"
    WRITE_FAILED = "WRITE_FAILED"


type ReportReasonCode = (
    ReviewReasonCode | MatchReasonCode | MatchingErrorCode | FileSkipReason
)
type ReportApplyCode = ProviderErrorCode | MediaErrorCode | MatchingErrorCode

_STRING_FIELDS = frozenset((MetadataField.TITLE, MetadataField.ALBUM, MetadataField.DATE))
_SEQUENCE_FIELDS = frozenset(
    (
        MetadataField.ARTISTS,
        MetadataField.ALBUM_ARTISTS,
        MetadataField.COMPOSERS,
        MetadataField.GENRES,
    )
)
_POSITION_FIELDS = frozenset((MetadataField.TRACK, MetadataField.DISC))
_SAFE_OPERATION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}\Z")
_WINDOWS_RESERVED_COMPONENTS = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{number}" for number in range(1, 10)}
    | {f"LPT{number}" for number in range(1, 10)}
)


def _copy_typed_sequence[T](
    name: str,
    values: object,
    item_type: type[T] | tuple[type[T], ...],
) -> tuple[T, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise TypeError(f"{name} must be an ordered sequence")

    copied = tuple(values)

    if any(not isinstance(value, item_type) for value in copied):
        raise TypeError(f"{name} contains an unsupported value")

    return cast(tuple[T, ...], copied)


def _validate_identifier(name: str, value: object) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")

    if not value.strip():
        raise ValueError(f"{name} cannot be blank")


def _validate_operation_id(value: object) -> None:
    _validate_identifier("operation_id", value)
    assert isinstance(value, str)

    if _SAFE_OPERATION_ID.fullmatch(value) is None:
        raise ValueError("operation_id must be one safe filename component")

    if value.upper() in _WINDOWS_RESERVED_COMPONENTS:
        raise ValueError("operation_id cannot be a reserved Windows filename")


def _normalise_optional_identifier(name: str, value: object) -> str | None:
    if value is None:
        return None

    _validate_identifier(name, value)

    return cast(str, value)


def _normalise_field_value(
    field_name: MetadataField,
    value: object,
    *,
    name: str,
) -> FieldValue | None:
    if value is None:
        return None

    if field_name in _STRING_FIELDS:
        if not isinstance(value, str):
            raise TypeError(f"{name} for {field_name.value} must be a string or None")

        return value

    if field_name in _SEQUENCE_FIELDS:
        return _copy_typed_sequence(name, value, str)

    if field_name in _POSITION_FIELDS:
        if not isinstance(value, Position):
            raise TypeError(f"{name} for {field_name.value} must be a Position or None")

        return value

    raise TypeError("field must be a MetadataField")


def _normalise_codes[T: StrEnum](
    name: str,
    values: object,
    allowed_types: type[T] | tuple[type[T], ...],
) -> tuple[T, ...]:
    copied = _copy_typed_sequence(name, values, allowed_types)
    # Collapse repeated codes and sort by their stable wire value, making a
    # report independent of the order in which equivalent issues were observed.
    by_value: dict[str, T] = {}

    for code in copied:
        by_value.setdefault(code.value, code)

    return tuple(by_value[value] for value in sorted(by_value))


@dataclass(frozen=True)
class SelectedReleaseReference:
    """Safe identity of the release-medium actually selected for one file."""

    engine_id: str
    source_id: str
    release_id: str
    medium_index: int

    def __post_init__(self) -> None:
        _validate_identifier("engine_id", self.engine_id)
        _validate_identifier("source_id", self.source_id)
        _validate_identifier("release_id", self.release_id)

        if type(self.medium_index) is not int:
            raise TypeError("medium_index must be an integer")

        if self.medium_index < 0:
            raise ValueError("medium_index cannot be negative")


@dataclass(frozen=True)
class ProposalSourceReference:
    """Allow-listed provider identity for one selected field proposal."""

    engine_id: str
    source_id: str
    record_id: str | None

    def __post_init__(self) -> None:
        _validate_identifier("engine_id", self.engine_id)
        _validate_identifier("source_id", self.source_id)
        object.__setattr__(
            self,
            "record_id",
            _normalise_optional_identifier("record_id", self.record_id),
        )

    @classmethod
    def from_provenance(cls, provenance: MetadataProvenance) -> ProposalSourceReference:
        """Copy only report-approved identity fields from provider provenance."""
        if not isinstance(provenance, MetadataProvenance):
            raise TypeError("provenance must be MetadataProvenance")

        # Source URLs, language hints, and lookup operation details deliberately
        # remain outside the audit schema because URLs can contain credentials.
        return cls(
            engine_id=provenance.engine_id,
            source_id=provenance.source_id,
            record_id=provenance.record_id,
        )


@dataclass(frozen=True)
class ReportFieldChange:
    """One actual changed field with its reviewed decision and safe provenance."""

    field: MetadataField
    old_value: FieldValue | None
    new_value: FieldValue | None
    decision: FieldDecisionKind
    decision_origin: DecisionOrigin
    provider_sources: tuple[ProposalSourceReference, ...] = ()
    reason_codes: tuple[ReviewReasonCode, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.field, MetadataField):
            raise TypeError("field must be a MetadataField")

        old_value = _normalise_field_value(self.field, self.old_value, name="old_value")
        new_value = _normalise_field_value(self.field, self.new_value, name="new_value")

        if old_value == new_value:
            raise ValueError("a reported field change cannot be a semantic no-op")

        if not isinstance(self.decision, FieldDecisionKind):
            raise TypeError("decision must be a FieldDecisionKind")

        if not isinstance(self.decision_origin, DecisionOrigin):
            raise TypeError("decision_origin must be a DecisionOrigin")

        sources = _copy_typed_sequence(
            "provider_sources",
            self.provider_sources,
            ProposalSourceReference,
        )
        source_by_identity = {
            (source.engine_id, source.source_id, source.record_id or ""): source
            for source in sources
        }
        sorted_sources = tuple(source_by_identity[key] for key in sorted(source_by_identity))

        if self.decision is not FieldDecisionKind.USE_PROPOSAL and sorted_sources:
            raise ValueError("only a selected provider proposal may have provider_sources")

        if self.decision is FieldDecisionKind.USE_PROPOSAL and not sorted_sources:
            raise ValueError("a selected provider proposal requires provider_sources")

        reason_codes = _normalise_codes(
            "reason_codes",
            self.reason_codes,
            ReviewReasonCode,
        )
        object.__setattr__(self, "old_value", old_value)
        object.__setattr__(self, "new_value", new_value)
        object.__setattr__(self, "provider_sources", sorted_sources)
        object.__setattr__(self, "reason_codes", reason_codes)

    @classmethod
    def from_reviewed_change(
        cls,
        change: MetadataChange,
        review: FieldReviewState,
    ) -> ReportFieldChange:
        """Project one actual change and its selected review into safe report data."""
        if not isinstance(change, MetadataChange):
            raise TypeError("change must be a MetadataChange")

        if not isinstance(review, FieldReviewState):
            raise TypeError("review must be a FieldReviewState")

        if change.field is not review.field:
            raise ValueError("change and review must describe the same field")

        selected = review.selected_proposal
        provider_sources: tuple[ProposalSourceReference, ...] = ()
        proposal_reason_codes: tuple[ReviewReasonCode, ...] = ()

        if selected is not None:
            provider_sources = tuple(
                ProposalSourceReference.from_provenance(member.provenance)
                for member in selected.members
            )
            proposal_reason_codes = (
                *selected.reason_codes,
                *(code for member in selected.members for code in member.reason_codes),
            )

        return cls(
            field=change.field,
            old_value=change.old_value,
            new_value=change.new_value,
            decision=review.decision,
            decision_origin=review.decision_origin,
            provider_sources=provider_sources,
            reason_codes=(*review.reason_codes, *proposal_reason_codes),
        )


@dataclass(frozen=True)
class ReportFileEntry:
    """Allow-listed final decision and actual outcome for one local file."""

    selected_release: SelectedReleaseReference | None
    original_path: Path
    final_path: Path
    status: ReportFileStatus
    completed_stage: FileTransactionStage
    changes: tuple[ReportFieldChange, ...]
    reason_codes: tuple[ReportReasonCode, ...]
    validation_codes: tuple[ChangeIssueCode, ...]
    apply_codes: tuple[ReportApplyCode, ...]
    rename_result: ReportRenameResult

    def __post_init__(self) -> None:
        if self.selected_release is not None and not isinstance(
            self.selected_release,
            SelectedReleaseReference,
        ):
            raise TypeError("selected_release must be SelectedReleaseReference or None")

        if not isinstance(self.original_path, Path) or not isinstance(self.final_path, Path):
            raise TypeError("original_path and final_path must be Path values")

        if not isinstance(self.status, ReportFileStatus):
            raise TypeError("status must be a ReportFileStatus")

        if not isinstance(self.completed_stage, FileTransactionStage):
            raise TypeError("completed_stage must be a FileTransactionStage")

        if not isinstance(self.rename_result, ReportRenameResult):
            raise TypeError("rename_result must be a ReportRenameResult")

        paths_differ = self.original_path != self.final_path

        if (
            self.rename_result is ReportRenameResult.SUCCEEDED
            and (self.status is not ReportFileStatus.SUCCEEDED or not paths_differ)
        ):
            raise ValueError(
                "a succeeded rename_result requires a succeeded file at a new path"
            )

        if (
            self.rename_result is ReportRenameResult.ORIGINAL_RETAINED
            and (self.status is not ReportFileStatus.FAILED or not paths_differ)
        ):
            raise ValueError(
                "an original_retained rename_result requires a failed file at a new path"
            )

        if self.status is ReportFileStatus.SUCCEEDED:
            if self.completed_stage is not FileTransactionStage.COMPLETED:
                raise ValueError("a succeeded file must complete its transaction")

            expected_rename_result = (
                ReportRenameResult.SUCCEEDED
                if paths_differ
                else ReportRenameResult.NOT_REQUESTED
            )

            if self.rename_result is not expected_rename_result:
                raise ValueError("rename_result does not match the succeeded file paths")
        elif self.completed_stage is FileTransactionStage.COMPLETED:
            raise ValueError("only a succeeded file may have the completed stage")

        if (
            self.status is ReportFileStatus.SKIPPED
            and self.completed_stage is not FileTransactionStage.NOT_STARTED
        ):
            raise ValueError("a skipped file must have the not_started stage")

        changes = _copy_typed_sequence("changes", self.changes, ReportFieldChange)
        fields = tuple(change.field for change in changes)

        if len(fields) != len(set(fields)):
            raise ValueError("changes must contain each field at most once")

        field_order = {field_name: index for index, field_name in enumerate(MetadataField)}

        if tuple(sorted(fields, key=field_order.__getitem__)) != fields:
            raise ValueError("changes must follow MetadataField declaration order")

        reason_codes = _normalise_codes(
            "reason_codes",
            self.reason_codes,
            (ReviewReasonCode, MatchReasonCode, MatchingErrorCode, FileSkipReason),
        )
        validation_codes = _normalise_codes(
            "validation_codes",
            self.validation_codes,
            ChangeIssueCode,
        )
        apply_codes = _normalise_codes(
            "apply_codes",
            self.apply_codes,
            (ProviderErrorCode, MediaErrorCode, MatchingErrorCode),
        )
        object.__setattr__(self, "changes", changes)
        object.__setattr__(self, "reason_codes", reason_codes)
        object.__setattr__(self, "validation_codes", validation_codes)
        object.__setattr__(self, "apply_codes", apply_codes)

    @classmethod
    def from_apply_result(
        cls,
        *,
        selected_release: SelectedReleaseReference | None,
        change_set: FileChangeSet,
        reviews: Sequence[FieldReviewState],
        apply_result: FileApplyResult,
        reason_codes: Sequence[ReportReasonCode] = (),
    ) -> ReportFileEntry:
        """Project final reviewed decisions and a real file result into the schema."""
        if not isinstance(change_set, FileChangeSet):
            raise TypeError("change_set must be a FileChangeSet")

        if not isinstance(apply_result, FileApplyResult):
            raise TypeError("apply_result must be a FileApplyResult")

        rename = change_set.rename_change

        if rename is not None and apply_result.source_path != rename.old_path:
            raise ValueError("apply_result source_path does not match the reviewed rename")

        if apply_result.status is FileApplyStatus.SUCCEEDED:
            expected_final_path = (
                rename.new_path if rename is not None else apply_result.source_path
            )

            if apply_result.final_path != expected_final_path:
                raise ValueError("successful apply_result has an unexpected final_path")

        copied_reviews = _copy_typed_sequence("reviews", reviews, FieldReviewState)
        review_by_field: dict[MetadataField, FieldReviewState] = {}

        for review in copied_reviews:
            if review.field in review_by_field:
                raise ValueError("reviews must contain each field at most once")

            review_by_field[review.field] = review

        missing_reviews = tuple(
            change.field
            for change in change_set.metadata_changes
            if change.field not in review_by_field
        )

        if missing_reviews:
            names = ", ".join(field_name.value for field_name in missing_reviews)
            raise ValueError(f"reviews are missing changed fields: {names}")

        status_by_apply_status = {
            FileApplyStatus.SUCCEEDED: ReportFileStatus.SUCCEEDED,
            FileApplyStatus.FAILED: ReportFileStatus.FAILED,
            FileApplyStatus.CANCELLED: ReportFileStatus.CANCELLED,
        }

        return cls(
            selected_release=selected_release,
            original_path=apply_result.source_path,
            final_path=apply_result.final_path,
            status=status_by_apply_status[apply_result.status],
            completed_stage=apply_result.completed_stage,
            changes=tuple(
                ReportFieldChange.from_reviewed_change(
                    change,
                    review_by_field[change.field],
                )
                for change in change_set.metadata_changes
            ),
            reason_codes=tuple(reason_codes),
            validation_codes=tuple(
                issue.code for issue in change_set.validation.issues
            ),
            apply_codes=tuple(issue.code for issue in apply_result.issues),
            rename_result=_derive_rename_result(change_set, apply_result),
        )

    @classmethod
    def from_skipped(
        cls,
        *,
        selected_release: SelectedReleaseReference | None,
        source_path: Path,
        change_set: FileChangeSet,
        reviews: Sequence[FieldReviewState],
        skip_reason: FileSkipReason,
        reason_codes: Sequence[ReportReasonCode] = (),
    ) -> ReportFileEntry:
        """Project a reviewed no-write outcome without inventing FileApplyResult."""
        if not isinstance(source_path, Path):
            raise TypeError("source_path must be a Path")

        if not isinstance(change_set, FileChangeSet):
            raise TypeError("change_set must be a FileChangeSet")

        if not isinstance(skip_reason, FileSkipReason):
            raise TypeError("skip_reason must be a FileSkipReason")

        copied_reviews = _copy_typed_sequence("reviews", reviews, FieldReviewState)
        review_by_field: dict[MetadataField, FieldReviewState] = {}

        for review in copied_reviews:
            if review.field in review_by_field:
                raise ValueError("reviews must contain each field at most once")

            review_by_field[review.field] = review

        missing_reviews = tuple(
            change.field
            for change in change_set.metadata_changes
            if change.field not in review_by_field
        )

        if missing_reviews:
            names = ", ".join(field_name.value for field_name in missing_reviews)
            raise ValueError(f"reviews are missing changed fields: {names}")

        same_name_no_op = (
            skip_reason is FileSkipReason.NO_CHANGES
            and change_set.rename_preview is None
            and change_set.rename_change is None
        )
        rename_result = (
            ReportRenameResult.NOT_ATTEMPTED
            if change_set.rename_decision is RenameDecision.APPLY_RENAME
            and not same_name_no_op
            else ReportRenameResult.NOT_REQUESTED
        )

        return cls(
            selected_release=selected_release,
            original_path=source_path,
            final_path=source_path,
            status=ReportFileStatus.SKIPPED,
            completed_stage=FileTransactionStage.NOT_STARTED,
            changes=tuple(
                ReportFieldChange.from_reviewed_change(
                    change,
                    review_by_field[change.field],
                )
                for change in change_set.metadata_changes
            ),
            reason_codes=(*reason_codes, skip_reason),
            validation_codes=tuple(issue.code for issue in change_set.validation.issues),
            apply_codes=(),
            rename_result=rename_result,
        )


def _derive_rename_result(
    change_set: FileChangeSet,
    apply_result: FileApplyResult,
) -> ReportRenameResult:
    if change_set.rename_change is None:
        return ReportRenameResult.NOT_REQUESTED

    if apply_result.status is FileApplyStatus.SUCCEEDED:
        return ReportRenameResult.SUCCEEDED

    # Reaching original cleanup means a verified renamed output already exists.
    # This failure therefore records two retained files, not a failed tag write.
    if (
        apply_result.completed_stage is FileTransactionStage.CLEANING_ORIGINAL
        and apply_result.final_path == change_set.rename_change.new_path
    ):
        return ReportRenameResult.ORIGINAL_RETAINED

    if apply_result.completed_stage in {
        FileTransactionStage.RENAMING,
        FileTransactionStage.VERIFYING_FINAL,
    }:
        return ReportRenameResult.FAILED

    return ReportRenameResult.NOT_ATTEMPTED


@dataclass(frozen=True)
class ProcessingReport:
    """Complete versioned report document ready for deterministic serialisation."""

    operation_id: str
    occurred_at: datetime
    result: ReportOperationResult
    files: tuple[ReportFileEntry, ...]
    schema_version: int = field(default=REPORT_SCHEMA_VERSION, init=False)

    def __post_init__(self) -> None:
        _validate_operation_id(self.operation_id)

        if not isinstance(self.occurred_at, datetime):
            raise TypeError("occurred_at must be a datetime")

        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() != timedelta(0):
            raise ValueError("occurred_at must be an aware UTC datetime")

        if not isinstance(self.result, ReportOperationResult):
            raise TypeError("result must be a ReportOperationResult")

        object.__setattr__(
            self,
            "files",
            _copy_typed_sequence("files", self.files, ReportFileEntry),
        )


@dataclass(frozen=True)
class ProcessingReportRequest:
    """Apply-owned report inputs before the writer obtains an enabled clock time."""

    operation_id: str
    result: ReportOperationResult
    files: tuple[ReportFileEntry, ...]

    def __post_init__(self) -> None:
        _validate_operation_id(self.operation_id)

        if not isinstance(self.result, ReportOperationResult):
            raise TypeError("result must be a ReportOperationResult")

        object.__setattr__(
            self,
            "files",
            _copy_typed_sequence("files", self.files, ReportFileEntry),
        )


@dataclass(frozen=True)
class ReportOutputPolicy:
    """Optional destination, with blank text selecting the application default."""

    enabled: bool = False
    directory: str = ""

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise TypeError("enabled must be a bool")

        if not isinstance(self.directory, str):
            raise TypeError("directory must be a string")


@dataclass(frozen=True)
class ReportWriteResult:
    """Report-only result which never changes the completed audio Apply outcome."""

    status: ReportWriteStatus
    path: Path | None
    error_code: ReportErrorCode | None

    def __post_init__(self) -> None:
        if not isinstance(self.status, ReportWriteStatus):
            raise TypeError("status must be a ReportWriteStatus")

        if self.path is not None and not isinstance(self.path, Path):
            raise TypeError("path must be a Path or None")

        if self.error_code is not None and not isinstance(self.error_code, ReportErrorCode):
            raise TypeError("error_code must be a ReportErrorCode or None")

        if self.status is ReportWriteStatus.WRITTEN:
            if self.path is None or self.error_code is not None:
                raise ValueError("a written report requires a path and no error_code")
        elif self.status is ReportWriteStatus.FAILED:
            if self.path is not None or self.error_code is None:
                raise ValueError("a failed report requires an error_code and no path")
        elif self.path is not None or self.error_code is not None:
            raise ValueError("a disabled report cannot have a path or error_code")


def _field_value_document(value: FieldValue | None) -> object:
    if isinstance(value, Position):
        return {"number": value.number, "total": value.total}

    if isinstance(value, tuple):
        return list(value)

    return value


def _selected_release_document(
    release: SelectedReleaseReference | None,
) -> dict[str, object] | None:
    if release is None:
        return None

    return {
        "engine_id": release.engine_id,
        "source_id": release.source_id,
        "release_id": release.release_id,
        "medium_index": release.medium_index,
    }


# Project identities and reviewed values explicitly; full provenance objects
# may carry URLs or other retrieval details that do not belong in saved reports.
def _field_change_document(change: ReportFieldChange) -> dict[str, object]:
    return {
        "field": change.field.value,
        "old": _field_value_document(change.old_value),
        "new": _field_value_document(change.new_value),
        "decision": change.decision.value,
        "decision_origin": change.decision_origin.value,
        "provider_sources": [
            {
                "engine_id": source.engine_id,
                "source_id": source.source_id,
                "record_id": source.record_id,
            }
            for source in change.provider_sources
        ],
        "reason_codes": [code.value for code in change.reason_codes],
    }


def _file_document(file_entry: ReportFileEntry) -> dict[str, object]:
    return {
        "selected_release": _selected_release_document(file_entry.selected_release),
        "original_path": str(file_entry.original_path),
        "final_path": str(file_entry.final_path),
        "status": file_entry.status.value,
        "completed_stage": file_entry.completed_stage.value,
        "changes": [_field_change_document(change) for change in file_entry.changes],
        "reason_codes": [code.value for code in file_entry.reason_codes],
        "validation_codes": [code.value for code in file_entry.validation_codes],
        "apply_codes": [code.value for code in file_entry.apply_codes],
        "rename_result": file_entry.rename_result.value,
    }


def serialise_processing_report(report: ProcessingReport) -> str:
    """Return canonical UTF-8-ready JSON containing only approved schema fields."""
    if not isinstance(report, ProcessingReport):
        raise TypeError("report must be a ProcessingReport")

    time_utc = report.occurred_at.isoformat(timespec="microseconds").replace(
        "+00:00",
        "Z",
    )
    document = {
        "schema_version": report.schema_version,
        "operation": {
            "id": report.operation_id,
            "time_utc": time_utc,
            "result": report.result.value,
        },
        "files": [_file_document(file_entry) for file_entry in report.files],
    }

    return json.dumps(document, ensure_ascii=False, indent=2) + "\n"


def _utc_now() -> datetime:
    return datetime.now(UTC)


class ProcessingReportWriter:
    """Write one optional report through a flushed atomic sibling replacement."""

    def __init__(
        self,
        default_directory: Path,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        if not isinstance(default_directory, Path):
            raise TypeError("default_directory must be a Path")

        if not callable(clock):
            raise TypeError("clock must be callable")

        self._default_directory = default_directory
        self._clock = clock

    def write(
        self,
        *,
        policy: ReportOutputPolicy,
        request: ProcessingReportRequest,
    ) -> ReportWriteResult:
        """Write when enabled; report failure remains independent from audio success."""
        if not isinstance(policy, ReportOutputPolicy):
            raise TypeError("policy must be a ReportOutputPolicy")

        if not isinstance(request, ProcessingReportRequest):
            raise TypeError("request must be a ProcessingReportRequest")

        # Disabled reporting is a true no-op. In particular, reading the clock or
        # creating the default reports directory would be observable persistence.
        if not policy.enabled:
            return ReportWriteResult(ReportWriteStatus.DISABLED, None, None)

        try:
            report = ProcessingReport(
                operation_id=request.operation_id,
                occurred_at=self._clock(),
                result=request.result,
                files=request.files,
            )
            encoded = serialise_processing_report(report)
        except (TypeError, ValueError, OverflowError):
            # Schema/clock validation happens before the first filesystem call, so
            # malformed report data cannot leave a directory or partial document.
            return ReportWriteResult(
                ReportWriteStatus.FAILED,
                None,
                ReportErrorCode.INVALID_REPORT,
            )

        directory = (
            Path(policy.directory)
            if policy.directory.strip()
            else self._default_directory
        )
        target = directory / f"metadata-polisher-{request.operation_id}.json"
        temporary_path: Path | None = None

        try:
            directory.mkdir(parents=True, exist_ok=True)

            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                dir=directory,
                prefix=f".{target.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                temporary.write(encoded)
                temporary.flush()
                os.fsync(temporary.fileno())

            # The temporary handle is closed before replacement, which is required
            # by Windows and prevents readers from seeing a partial JSON document.
            os.replace(temporary_path, target)
            temporary_path = None
        except (OSError, UnicodeError):
            if temporary_path is not None:
                # Only our unique sibling is eligible for cleanup. A failed
                # cleanup must never widen into scanning/deleting other files.
                with suppress(OSError):
                    temporary_path.unlink(missing_ok=True)

            return ReportWriteResult(
                ReportWriteStatus.FAILED,
                None,
                ReportErrorCode.WRITE_FAILED,
            )

        return ReportWriteResult(ReportWriteStatus.WRITTEN, target, None)
