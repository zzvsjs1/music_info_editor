"""Immutable counts and blocking reasons for the final Apply review boundary."""

from collections.abc import Sequence
from dataclasses import dataclass

from metadata_polisher.application.changes import ChangeIssueCode, ChangeIssueSeverity, FileChangeSet, RenameDecision
from metadata_polisher.domain.metadata import MetadataField, Position


@dataclass(frozen=True)
class FieldChangeCount:
    """Number of files with one category of change to a managed field."""

    field: MetadataField
    count: int

    def __post_init__(self) -> None:
        if not isinstance(self.field, MetadataField):
            raise TypeError("field must be a MetadataField")

        if type(self.count) is not int or self.count <= 0:
            raise ValueError("count must be a positive integer")


@dataclass(frozen=True)
class ApplySummaryIssue:
    """A blocking reason tied to its selected source file and optional field."""

    file_id: str
    code: ChangeIssueCode
    message: str
    field: MetadataField | None = None


@dataclass(frozen=True)
class ApplySummary:
    """Intended operations and retained blockers before the user confirms Apply."""

    file_count: int
    write_file_count: int
    additions: tuple[FieldChangeCount, ...]
    replacements: tuple[FieldChangeCount, ...]
    removals: tuple[FieldChangeCount, ...]
    rename_count: int
    blocking_issues: tuple[ApplySummaryIssue, ...]
    backup_enabled: bool
    report_enabled: bool

    def __post_init__(self) -> None:
        for count in (self.file_count, self.write_file_count, self.rename_count):
            if type(count) is not int or count < 0:
                raise ValueError("summary counts must be non-negative integers")

        if not self.rename_count <= self.write_file_count <= self.file_count:
            raise ValueError("rename and write counts cannot exceed the selected file count")

        if type(self.backup_enabled) is not bool or type(self.report_enabled) is not bool:
            raise TypeError("backup_enabled and report_enabled must be bool values")

        # Copy each nested collection so retaining the summary across a modal
        # confirmation never depends on a caller's mutable list of counts/issues.
        object.__setattr__(self, "additions", tuple(self.additions))
        object.__setattr__(self, "replacements", tuple(self.replacements))
        object.__setattr__(self, "removals", tuple(self.removals))
        object.__setattr__(self, "blocking_issues", tuple(self.blocking_issues))

        for counts in (self.additions, self.replacements, self.removals):
            if any(not isinstance(item, FieldChangeCount) for item in counts):
                raise TypeError("field counts must contain FieldChangeCount values")

            if len({item.field for item in counts}) != len(counts):
                raise ValueError("a summary count category cannot contain duplicate fields")

        if any(not isinstance(item, ApplySummaryIssue) for item in self.blocking_issues):
            raise TypeError("blocking_issues must contain ApplySummaryIssue values")

    @property
    def addition_count(self) -> int:
        """Count added field values, with a tuple of composers counting as one field."""
        return sum(item.count for item in self.additions)

    @property
    def replacement_count(self) -> int:
        return sum(item.count for item in self.replacements)

    @property
    def removal_count(self) -> int:
        return sum(item.count for item in self.removals)

    @property
    def can_apply(self) -> bool:
        """Require an intended write and resolution of every selected blocker."""
        return self.write_file_count > 0 and not self.blocking_issues


def _has_semantic_value(value: object) -> bool:
    if value is None:
        return False

    if isinstance(value, str):
        return bool(value.strip())

    if isinstance(value, tuple):
        return bool(value)

    if isinstance(value, Position):
        return value.number is not None or value.total is not None

    return True


def _ordered_counts(counts: dict[MetadataField, int]) -> tuple[FieldChangeCount, ...]:
    return tuple(FieldChangeCount(field, counts[field]) for field in MetadataField if counts.get(field, 0))


def build_apply_summary(
    change_sets: Sequence[FileChangeSet],
    *,
    backup_enabled: bool,
    report_enabled: bool,
) -> ApplySummary:
    """Summarise the supplied derived changes without writing or making new decisions."""
    copied = tuple(change_sets)

    if any(not isinstance(item, FileChangeSet) for item in copied):
        raise TypeError("change_sets must contain only FileChangeSet values")

    if len({item.file_id for item in copied}) != len(copied):
        raise ValueError("change_sets must contain unique file IDs")

    additions: dict[MetadataField, int] = {}
    replacements: dict[MetadataField, int] = {}
    removals: dict[MetadataField, int] = {}
    blockers: list[ApplySummaryIssue] = []
    write_file_count = 0
    rename_count = 0

    for changes in copied:
        # A blocked requested rename still belongs in the confirmation's intended
        # counts. A Keep decision can show the same preview without requesting a
        # filename write, so preview presence alone must never count as consent.
        rename_requested = (
            changes.rename_decision is RenameDecision.APPLY_RENAME
            and changes.rename_preview is not None
        )
        rename_count += int(rename_requested)
        write_file_count += int(bool(changes.metadata_changes) or rename_requested)

        # Count one operation per file/field, not per item inside a multi-value
        # tag. Replacing three composers is one replacement in the review summary.
        for change in changes.metadata_changes:
            old_present = _has_semantic_value(change.old_value)
            new_present = _has_semantic_value(change.new_value)

            if not old_present and new_present:
                counts = additions
            elif old_present and not new_present:
                counts = removals
            else:
                counts = replacements

            counts[change.field] = counts.get(change.field, 0) + 1

        # Preserve each blocker with its file identity so the confirmation can
        # explain the affected item instead of presenting only an aggregate count.
        blockers.extend(
            ApplySummaryIssue(changes.file_id, issue.code, issue.message, issue.field)
            for issue in changes.validation.issues
            if issue.severity is ChangeIssueSeverity.BLOCKING
        )

    return ApplySummary(
        file_count=len(copied),
        write_file_count=write_file_count,
        additions=_ordered_counts(additions),
        replacements=_ordered_counts(replacements),
        removals=_ordered_counts(removals),
        rename_count=rename_count,
        blocking_issues=tuple(blockers),
        backup_enabled=backup_enabled,
        report_enabled=report_enabled,
    )
