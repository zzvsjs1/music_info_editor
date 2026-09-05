"""Pure derivation and preflight validation of immutable file changes."""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import cast

from metadata_polisher.domain.media import LocalMediaFile
from metadata_polisher.domain.metadata import (
    FieldReadState,
    MetadataChange,
    MetadataField,
    MetadataSnapshot,
    Position,
)
from metadata_polisher.domain.review import FieldDecisionKind, FieldReviewState, FieldValue
from metadata_polisher.rename.template import (
    FilenameRenderPolicy,
    TemplateError,
    parse_template,
    render_template,
)
from metadata_polisher.rename.windows import (
    FilenameIssueCode,
    FilenameIssueSeverity,
    sanitise_windows_filename,
    validate_windows_filename_collision,
)

DEFAULT_RENAME_TEMPLATE = "[%discnumber%.]%tracknumber%. %title%"


class RenameDecision(StrEnum):
    """Independent user decision controlling whether a preview becomes a write."""

    APPLY_RENAME = "apply_rename"
    KEEP_FILENAME = "keep_filename"


class ChangeSetStatus(StrEnum):
    """Preflight outcome for one derived file change set."""

    VALID = "valid"
    VALID_WITH_WARNINGS = "valid_with_warnings"
    BLOCKED = "blocked"


class ChangeIssueSeverity(StrEnum):
    """Whether an issue is informative or prevents the requested operation."""

    WARNING = "warning"
    BLOCKING = "blocking"


class ChangeIssueCode(StrEnum):
    """Stable reasons emitted by pure ChangeSet validation."""

    SOURCE_NOT_READABLE = "SOURCE_NOT_READABLE"
    DIRECTORY_NOT_WRITABLE = "DIRECTORY_NOT_WRITABLE"
    DIRECTORY_LISTING_UNAVAILABLE = "DIRECTORY_LISTING_UNAVAILABLE"
    ADAPTER_UNAVAILABLE = "ADAPTER_UNAVAILABLE"
    INSUFFICIENT_TEMPORARY_SPACE = "INSUFFICIENT_TEMPORARY_SPACE"
    BACKUP_ROOT_UNAVAILABLE = "BACKUP_ROOT_UNAVAILABLE"
    BACKUP_DESTINATION_COLLISION = "BACKUP_DESTINATION_COLLISION"
    INSUFFICIENT_BACKUP_SPACE = "INSUFFICIENT_BACKUP_SPACE"
    UNSUPPORTED_WRITE = "UNSUPPORTED_WRITE"
    UNRESOLVED_TRACK_MAPPING = "UNRESOLVED_TRACK_MAPPING"
    UNRESOLVED_FIELD_PRESERVED = "UNRESOLVED_FIELD_PRESERVED"
    INVALID_RENAME_TEMPLATE = "INVALID_RENAME_TEMPLATE"
    RENAME_RENDER_FAILED = "RENAME_RENDER_FAILED"
    INVALID_DESTINATION_FILENAME = "INVALID_DESTINATION_FILENAME"
    FILENAME_REPAIRED = "FILENAME_REPAIRED"
    DESTINATION_COLLISION = "DESTINATION_COLLISION"
    CASE_ONLY_RENAME_UNSUPPORTED = "CASE_ONLY_RENAME_UNSUPPORTED"


def _validate_bool(name: str, value: object) -> None:
    if type(value) is not bool:
        raise TypeError(f"{name} must be a bool")


def _copy_typed_sequence[T](name: str, values: object, item_type: type[T]) -> tuple[T, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise TypeError(f"{name} must be an ordered sequence")

    copied = tuple(values)

    if any(not isinstance(value, item_type) for value in copied):
        raise TypeError(f"{name} must contain only {item_type.__name__} values")

    return cast(tuple[T, ...], copied)


@dataclass(frozen=True)
class ChangeValidationFacts:
    """Immutable preflight facts captured outside the pure ChangeSet builder."""

    source_readable: bool = True
    directory_writable: bool = True
    directory_listing_available: bool = True
    adapter_available: bool = True
    temporary_space_sufficient: bool = True
    backup_root_writable: bool = True
    backup_destination_available: bool = True
    backup_space_sufficient: bool = True
    case_only_rename_supported: bool = True
    track_mapping_resolved: bool = True
    unsupported_write_fields: tuple[MetadataField, ...] = ()
    existing_names: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in (
            "source_readable",
            "directory_writable",
            "directory_listing_available",
            "adapter_available",
            "temporary_space_sufficient",
            "backup_root_writable",
            "backup_destination_available",
            "backup_space_sufficient",
            "case_only_rename_supported",
            "track_mapping_resolved",
        ):
            _validate_bool(name, getattr(self, name))

        unsupported_fields = _copy_typed_sequence(
            "unsupported_write_fields",
            self.unsupported_write_fields,
            MetadataField,
        )
        existing_names = _copy_typed_sequence("existing_names", self.existing_names, str)

        if len(unsupported_fields) != len(set(unsupported_fields)):
            raise ValueError("unsupported_write_fields must be unique")

        if any(not name for name in existing_names):
            raise ValueError("existing_names cannot contain an empty filename")

        object.__setattr__(self, "unsupported_write_fields", unsupported_fields)
        object.__setattr__(self, "existing_names", existing_names)


@dataclass(frozen=True)
class ChangeValidationIssue:
    """One structured warning or blocker associated with a derived operation."""

    code: ChangeIssueCode
    severity: ChangeIssueSeverity
    message: str
    field: MetadataField | None = None
    filename_issue_code: FilenameIssueCode | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.code, ChangeIssueCode):
            raise TypeError("code must be a ChangeIssueCode")

        if not isinstance(self.severity, ChangeIssueSeverity):
            raise TypeError("severity must be a ChangeIssueSeverity")

        if not isinstance(self.message, str):
            raise TypeError("message must be a string")

        if not self.message.strip():
            raise ValueError("message cannot be blank")

        if self.field is not None and not isinstance(self.field, MetadataField):
            raise TypeError("field must be a MetadataField or None")

        if self.filename_issue_code is not None and not isinstance(
            self.filename_issue_code,
            FilenameIssueCode,
        ):
            raise TypeError("filename_issue_code must be a FilenameIssueCode or None")


@dataclass(frozen=True)
class ChangeValidationResult:
    """Immutable issues with status derived solely from their severities."""

    issues: tuple[ChangeValidationIssue, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "issues",
            _copy_typed_sequence("issues", self.issues, ChangeValidationIssue),
        )

    @property
    def status(self) -> ChangeSetStatus:
        """Classify blockers before warnings, regardless of issue ordering."""
        if any(issue.severity is ChangeIssueSeverity.BLOCKING for issue in self.issues):
            return ChangeSetStatus.BLOCKED

        if self.issues:
            return ChangeSetStatus.VALID_WITH_WARNINGS

        return ChangeSetStatus.VALID


@dataclass(frozen=True)
class RenameChange:
    """One reviewed same-directory path change preserving the media extension."""

    old_path: Path
    new_path: Path

    def __post_init__(self) -> None:
        if not isinstance(self.old_path, Path) or not isinstance(self.new_path, Path):
            raise TypeError("old_path and new_path must be Path values")

        if self.old_path.parent != self.new_path.parent:
            raise ValueError("a rename destination must remain in the source directory")

        if str(self.old_path) == str(self.new_path):
            raise ValueError("a rename must change the path")


@dataclass(frozen=True)
class FileChangeSet:
    """Freshly derived semantic and filename changes for one source file."""

    file_id: str
    metadata_changes: tuple[MetadataChange, ...]
    rename_change: RenameChange | None
    final_metadata: MetadataSnapshot
    rename_decision: RenameDecision
    rename_preview: RenameChange | None
    validation: ChangeValidationResult

    def __post_init__(self) -> None:
        if not isinstance(self.file_id, str):
            raise TypeError("file_id must be a string")

        if not self.file_id:
            raise ValueError("file_id cannot be empty")

        metadata_changes = _copy_typed_sequence(
            "metadata_changes",
            self.metadata_changes,
            MetadataChange,
        )
        fields = tuple(change.field for change in metadata_changes)

        if any(not isinstance(field, MetadataField) for field in fields):
            raise TypeError("every metadata change field must be a MetadataField")

        if len(fields) != len(set(fields)):
            raise ValueError("metadata_changes must contain each field at most once")

        declaration_order = {field: index for index, field in enumerate(MetadataField)}

        if tuple(sorted(fields, key=declaration_order.__getitem__)) != fields:
            raise ValueError("metadata_changes must follow MetadataField declaration order")

        if any(change.old_value == change.new_value for change in metadata_changes):
            raise ValueError("metadata_changes cannot contain semantic no-ops")

        if self.rename_change is not None and not isinstance(self.rename_change, RenameChange):
            raise TypeError("rename_change must be RenameChange or None")

        if self.rename_preview is not None and not isinstance(self.rename_preview, RenameChange):
            raise TypeError("rename_preview must be RenameChange or None")

        if not isinstance(self.final_metadata, MetadataSnapshot):
            raise TypeError("final_metadata must be a MetadataSnapshot")

        if not isinstance(self.rename_decision, RenameDecision):
            raise TypeError("rename_decision must be a RenameDecision")

        if not isinstance(self.validation, ChangeValidationResult):
            raise TypeError("validation must be a ChangeValidationResult")

        if self.rename_decision is RenameDecision.KEEP_FILENAME and self.rename_change is not None:
            raise ValueError("KEEP_FILENAME cannot contain a rename_change")

        if self.rename_change is not None and self.rename_change != self.rename_preview:
            raise ValueError("rename_change must match the validated rename_preview")

        object.__setattr__(self, "metadata_changes", metadata_changes)

    @property
    def status(self) -> ChangeSetStatus:
        """Expose validation classification directly to apply and UI callers."""
        return self.validation.status


ChangeSet = FileChangeSet


def _copy_complete_reviews(values: object) -> tuple[FieldReviewState, ...]:
    # A missing review entry would make preservation depend on a caller's
    # omissions. Require one explicit state for every managed field instead.
    reviews = _copy_typed_sequence("reviews", values, FieldReviewState)
    fields = tuple(review.field for review in reviews)

    if len(fields) != len(set(fields)):
        raise ValueError("reviews must contain each MetadataField exactly once")

    required_fields = frozenset(MetadataField)
    supplied_fields = frozenset(fields)

    if supplied_fields != required_fields:
        missing = ", ".join(sorted(field.value for field in required_fields - supplied_fields))
        raise ValueError(f"reviews must cover every MetadataField; missing=[{missing}]")

    return reviews


def _metadata_value(metadata: MetadataSnapshot, field: MetadataField) -> FieldValue | None:
    if field is MetadataField.TITLE:
        return metadata.title

    if field is MetadataField.ARTISTS:
        return metadata.artists

    if field is MetadataField.ALBUM:
        return metadata.album

    if field is MetadataField.ALBUM_ARTISTS:
        return metadata.album_artists

    if field is MetadataField.COMPOSERS:
        return metadata.composers

    if field is MetadataField.TRACK:
        return metadata.track

    if field is MetadataField.DISC:
        return metadata.disc

    if field is MetadataField.DATE:
        return metadata.date

    if field is MetadataField.GENRES:
        return metadata.genres

    raise ValueError(f"Unsupported metadata field: {field!r}")


def _has_semantic_value(value: FieldValue | None) -> bool:
    if value is None:
        return False

    if isinstance(value, str):
        return bool(value.strip())

    if isinstance(value, tuple):
        return bool(value)

    return value.number is not None or value.total is not None


def _validate_review_source_alignment(
    source: LocalMediaFile,
    reviews_by_field: dict[MetadataField, FieldReviewState],
) -> None:
    """Reject review state that was derived from a different scan snapshot."""
    source_metadata = source.read_result.metadata

    for field in MetadataField:
        review = reviews_by_field[field]
        source_read_state = source.read_result.field_states[field]

        if review.read_state is not source_read_state:
            raise ValueError(
                f"review read_state for {field.value} does not match the source read state"
            )

        source_value = _metadata_value(source_metadata, field)

        if source_read_state is FieldReadState.MISSING:
            expected_existing: FieldValue | None = None
        elif _has_semantic_value(source_value):
            expected_existing = source_value
        elif source_read_state is FieldReadState.PRESENT:
            raise ValueError(
                f"source value for present field {field.value} has no semantic value"
            )
        else:
            expected_existing = None

        if review.existing_value != expected_existing:
            raise ValueError(
                f"review existing_value for {field.value} does not match the source snapshot"
            )


def _cleared_value(field: MetadataField) -> FieldValue | None:
    if field in {MetadataField.TITLE, MetadataField.ALBUM, MetadataField.DATE}:
        return None

    if field in {
        MetadataField.ARTISTS,
        MetadataField.ALBUM_ARTISTS,
        MetadataField.COMPOSERS,
        MetadataField.GENRES,
    }:
        return ()

    if field in {MetadataField.TRACK, MetadataField.DISC}:
        return Position()

    raise ValueError(f"Unsupported metadata field: {field!r}")


def _overlay_position(existing: Position, selected: Position) -> Position:
    # Partial positions supply only known components: choosing track 3 keeps a
    # known total of 12. Removing both components requires an explicit Clear.
    return Position(
        number=selected.number if selected.number is not None else existing.number,
        total=selected.total if selected.total is not None else existing.total,
    )


def _resolve_value(
    source_metadata: MetadataSnapshot,
    review: FieldReviewState,
) -> FieldValue | None:
    existing = _metadata_value(source_metadata, review.field)

    # Unresolved means the user has not chosen a replacement; it does not mean
    # the old value should disappear from either the tags or the rename preview.
    if review.decision in {FieldDecisionKind.KEEP_EXISTING, FieldDecisionKind.UNRESOLVED}:
        return existing

    if review.decision is FieldDecisionKind.CLEAR:
        return _cleared_value(review.field)

    if review.decision is FieldDecisionKind.USE_PROPOSAL:
        if review.selected_proposal is None:
            raise ValueError("USE_PROPOSAL review is missing selected_proposal")

        selected = review.selected_proposal.value
    else:
        if review.manual_value is None:
            raise ValueError("USE_MANUAL review is missing manual_value")

        selected = review.manual_value

    if review.field in {MetadataField.TRACK, MetadataField.DISC}:
        if not isinstance(existing, Position) or not isinstance(selected, Position):
            raise TypeError("track and disc decisions must resolve Position values")

        return _overlay_position(existing, selected)

    return selected


def _derive_final_metadata(
    source_metadata: MetadataSnapshot,
    reviews_by_field: dict[MetadataField, FieldReviewState],
) -> MetadataSnapshot:
    values = {
        field: _resolve_value(source_metadata, reviews_by_field[field])
        for field in MetadataField
    }

    return MetadataSnapshot(
        title=cast(str | None, values[MetadataField.TITLE]),
        artists=cast(tuple[str, ...], values[MetadataField.ARTISTS]),
        album=cast(str | None, values[MetadataField.ALBUM]),
        album_artists=cast(tuple[str, ...], values[MetadataField.ALBUM_ARTISTS]),
        composers=cast(tuple[str, ...], values[MetadataField.COMPOSERS]),
        track=cast(Position, values[MetadataField.TRACK]),
        disc=cast(Position, values[MetadataField.DISC]),
        date=cast(str | None, values[MetadataField.DATE]),
        genres=cast(tuple[str, ...], values[MetadataField.GENRES]),
    )


def _metadata_changes(
    source: MetadataSnapshot,
    final: MetadataSnapshot,
) -> tuple[MetadataChange, ...]:
    # Compare semantic values, not raw tag spellings. Unchanged fields are absent
    # from the write request so the adapter can leave their physical tags alone.
    return tuple(
        MetadataChange(
            field=field,
            old_value=_metadata_value(source, field),
            new_value=_metadata_value(final, field),
        )
        for field in MetadataField
        if _metadata_value(source, field) != _metadata_value(final, field)
    )


def _rename_problem_severity(rename_decision: RenameDecision) -> ChangeIssueSeverity:
    # A bad optional preview can remain visible while tag edits proceed. It
    # becomes a blocker only when the user includes that rename in the write.
    if rename_decision is RenameDecision.APPLY_RENAME:
        return ChangeIssueSeverity.BLOCKING

    return ChangeIssueSeverity.WARNING


def _derive_rename(
    source_path: Path,
    final_metadata: MetadataSnapshot,
    rename_decision: RenameDecision,
    template: str,
    rename_policy: FilenameRenderPolicy,
    existing_names: tuple[str, ...],
) -> tuple[RenameChange | None, RenameChange | None, tuple[ChangeValidationIssue, ...]]:
    severity = _rename_problem_severity(rename_decision)

    try:
        template_ast = parse_template(template)
    except TemplateError as error:
        issue = ChangeValidationIssue(
            code=ChangeIssueCode.INVALID_RENAME_TEMPLATE,
            severity=severity,
            message=str(error),
        )

        return None, None, (issue,)

    try:
        rendered_stem = render_template(template_ast, final_metadata, rename_policy)
    except TemplateError as error:
        issue = ChangeValidationIssue(
            code=ChangeIssueCode.RENAME_RENDER_FAILED,
            severity=severity,
            message=str(error),
        )

        return None, None, (issue,)

    if not rendered_stem.strip():
        issue = ChangeValidationIssue(
            code=ChangeIssueCode.INVALID_DESTINATION_FILENAME,
            severity=severity,
            message="The filename template rendered an empty stem.",
            filename_issue_code=FilenameIssueCode.EMPTY_COMPONENT,
        )

        return None, None, (issue,)

    # The template controls the stem; retaining the source suffix avoids making
    # a metadata rename look like an audio-format conversion.
    rendered_name = f"{rendered_stem}{source_path.suffix}"
    filename_validation = sanitise_windows_filename(rendered_name)
    issues: list[ChangeValidationIssue] = []

    for filename_issue in filename_validation.issues:
        if filename_issue.severity is FilenameIssueSeverity.BLOCKING:
            code = ChangeIssueCode.INVALID_DESTINATION_FILENAME
            issue_severity = severity
        else:
            code = ChangeIssueCode.FILENAME_REPAIRED
            issue_severity = ChangeIssueSeverity.WARNING

        issues.append(
            ChangeValidationIssue(
                code=code,
                severity=issue_severity,
                message=filename_issue.message,
                filename_issue_code=filename_issue.code,
            )
        )

    if filename_validation.sanitised_name is None:
        return None, None, tuple(issues)

    destination_path = source_path.with_name(filename_validation.sanitised_name)

    if str(destination_path) == str(source_path):
        return None, None, tuple(issues)

    preview = RenameChange(old_path=source_path, new_path=destination_path)
    collision = validate_windows_filename_collision(
        destination_path.name,
        existing_names,
        current_name=source_path.name,
    )

    if not collision.is_valid:
        issues.append(
            ChangeValidationIssue(
                code=ChangeIssueCode.DESTINATION_COLLISION,
                severity=severity,
                message="The destination filename collides with an existing directory entry.",
                filename_issue_code=FilenameIssueCode.DESTINATION_COLLISION,
            )
        )

    # Retain the preview for explanation even when a collision prevents it from
    # becoming an executable rename. Intent, preview and write are distinct.
    rename_blocked = any(issue.severity is ChangeIssueSeverity.BLOCKING for issue in issues)
    rename_change = (
        preview
        if rename_decision is RenameDecision.APPLY_RENAME and not rename_blocked
        else None
    )

    return preview, rename_change, tuple(issues)


_TRACK_SPECIFIC_FIELDS = frozenset(
    (
        MetadataField.TITLE,
        MetadataField.ARTISTS,
        MetadataField.COMPOSERS,
        MetadataField.TRACK,
    )
)


def _blocking_issue(code: ChangeIssueCode, message: str) -> ChangeValidationIssue:
    return ChangeValidationIssue(
        code=code,
        severity=ChangeIssueSeverity.BLOCKING,
        message=message,
    )


def _derive_preflight_issues(
    reviews_by_field: dict[MetadataField, FieldReviewState],
    metadata_changes: tuple[MetadataChange, ...],
    rename_operation_requested: bool,
    validation: ChangeValidationFacts,
) -> tuple[ChangeValidationIssue, ...]:
    issues: list[ChangeValidationIssue] = []
    changed_fields = frozenset(change.field for change in metadata_changes)
    operation_requested = bool(metadata_changes) or rename_operation_requested

    if operation_requested and not validation.source_readable:
        issues.append(
            _blocking_issue(
                ChangeIssueCode.SOURCE_NOT_READABLE,
                "The source file is not readable for a transactional write.",
            )
        )

    if operation_requested and not validation.directory_writable:
        issues.append(
            _blocking_issue(
                ChangeIssueCode.DIRECTORY_NOT_WRITABLE,
                "The source directory is not writable.",
            )
        )

    if operation_requested and not validation.directory_listing_available:
        issues.append(
            _blocking_issue(
                ChangeIssueCode.DIRECTORY_LISTING_UNAVAILABLE,
                "The source directory could not be enumerated safely.",
            )
        )

    if operation_requested and not validation.adapter_available:
        issues.append(
            _blocking_issue(
                ChangeIssueCode.ADAPTER_UNAVAILABLE,
                "No writable media adapter is available for the source file.",
            )
        )

    if operation_requested and not validation.temporary_space_sufficient:
        issues.append(
            _blocking_issue(
                ChangeIssueCode.INSUFFICIENT_TEMPORARY_SPACE,
                "There is insufficient space for a safe sibling temporary copy.",
            )
        )

    if operation_requested and not validation.backup_root_writable:
        issues.append(
            _blocking_issue(
                ChangeIssueCode.BACKUP_ROOT_UNAVAILABLE,
                "The configured backup root is not available for a safe copy.",
            )
        )

    if operation_requested and not validation.backup_destination_available:
        issues.append(
            _blocking_issue(
                ChangeIssueCode.BACKUP_DESTINATION_COLLISION,
                "The operation backup destination already exists or is duplicated.",
            )
        )

    if operation_requested and not validation.backup_space_sufficient:
        issues.append(
            _blocking_issue(
                ChangeIssueCode.INSUFFICIENT_BACKUP_SPACE,
                "There is insufficient space for the complete operation backup.",
            )
        )

    if rename_operation_requested and not validation.case_only_rename_supported:
        issues.append(
            _blocking_issue(
                ChangeIssueCode.CASE_ONLY_RENAME_UNSUPPORTED,
                "Case-only renames are deferred because V1 cannot publish them safely.",
            )
        )

    explicitly_unsupported = frozenset(validation.unsupported_write_fields)

    for field in MetadataField:
        review = reviews_by_field[field]

        if field in changed_fields and (
            field in explicitly_unsupported or review.read_state is FieldReadState.UNSUPPORTED
        ):
            issues.append(
                ChangeValidationIssue(
                    code=ChangeIssueCode.UNSUPPORTED_WRITE,
                    severity=ChangeIssueSeverity.BLOCKING,
                    message="The selected metadata field cannot be written by this adapter.",
                    field=field,
                )
            )

        if (
            field in changed_fields
            and field in _TRACK_SPECIFIC_FIELDS
            and review.decision is FieldDecisionKind.USE_PROPOSAL
            and not validation.track_mapping_resolved
        ):
            issues.append(
                ChangeValidationIssue(
                    code=ChangeIssueCode.UNRESOLVED_TRACK_MAPPING,
                    severity=ChangeIssueSeverity.BLOCKING,
                    message="A track-specific provider change requires a resolved track mapping.",
                    field=field,
                )
            )

        # An unresolved optional field stays unchanged and warns the reviewer;
        # it need not block a safe edit to another field on the same file.
        if review.decision is FieldDecisionKind.UNRESOLVED:
            issues.append(
                ChangeValidationIssue(
                    code=ChangeIssueCode.UNRESOLVED_FIELD_PRESERVED,
                    severity=ChangeIssueSeverity.WARNING,
                    message="An unresolved field is being preserved unchanged.",
                    field=field,
                )
            )

    return tuple(issues)


_DEFAULT_RENAME_POLICY = FilenameRenderPolicy()
_DEFAULT_VALIDATION_FACTS = ChangeValidationFacts()


def build_change_set(
    source: LocalMediaFile,
    reviews: Sequence[FieldReviewState],
    rename_decision: RenameDecision,
    template: str = DEFAULT_RENAME_TEMPLATE,
    rename_policy: FilenameRenderPolicy = _DEFAULT_RENAME_POLICY,
    validation: ChangeValidationFacts = _DEFAULT_VALIDATION_FACTS,
) -> FileChangeSet:
    """Rebuild final metadata and actual differences solely from immutable inputs."""
    if not isinstance(source, LocalMediaFile):
        raise TypeError("source must be a LocalMediaFile")

    if not isinstance(rename_decision, RenameDecision):
        raise TypeError("rename_decision must be a RenameDecision")

    if not isinstance(template, str):
        raise TypeError("template must be a string")

    if not isinstance(rename_policy, FilenameRenderPolicy):
        raise TypeError("rename_policy must be a FilenameRenderPolicy")

    if not isinstance(validation, ChangeValidationFacts):
        raise TypeError("validation must be ChangeValidationFacts")

    copied_reviews = _copy_complete_reviews(reviews)
    reviews_by_field = {review.field: review for review in copied_reviews}
    # Reject decisions based on another scan before deriving a new plan. This
    # prevents old 'existing' values from silently authorising fresh writes.
    _validate_review_source_alignment(source, reviews_by_field)

    source_metadata = source.read_result.metadata
    # Derive in this order so filenames use the user's final field decisions,
    # including manual values and preserved local values, rather than proposals.
    final_metadata = _derive_final_metadata(source_metadata, reviews_by_field)
    metadata_changes = _metadata_changes(source_metadata, final_metadata)
    rename_preview, rename_change, rename_issues = _derive_rename(
        source.path,
        final_metadata,
        rename_decision,
        template,
        rename_policy,
        validation.existing_names,
    )
    preflight_issues = _derive_preflight_issues(
        reviews_by_field,
        metadata_changes,
        rename_decision is RenameDecision.APPLY_RENAME and rename_preview is not None,
        validation,
    )

    return FileChangeSet(
        file_id=source.file_id,
        metadata_changes=metadata_changes,
        rename_change=rename_change,
        final_metadata=final_metadata,
        rename_decision=rename_decision,
        rename_preview=rename_preview,
        validation=ChangeValidationResult(preflight_issues + rename_issues),
    )
