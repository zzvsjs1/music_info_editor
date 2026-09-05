# Summary counts describe intended file/field operations, including blocked renames.
# A preview by itself is not consent to write a filename.

from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from metadata_polisher.application.apply_summary import build_apply_summary
from metadata_polisher.application.changes import (
    ChangeIssueCode,
    ChangeIssueSeverity,
    ChangeValidationIssue,
    ChangeValidationResult,
    FileChangeSet,
    RenameChange,
    RenameDecision,
)
from metadata_polisher.domain.metadata import MetadataChange, MetadataField, MetadataSnapshot, Position


def changes(
    file_id: str,
    metadata: tuple[MetadataChange, ...] = (),
    *,
    rename: bool = False,
    issues: tuple[ChangeValidationIssue, ...] = (),
) -> FileChangeSet:
    preview = RenameChange(Path(f"library/{file_id}.flac"), Path(f"library/{file_id}-renamed.flac"))
    values = {item.field.value: item.new_value for item in metadata}

    return FileChangeSet(
        file_id,
        metadata,
        preview if rename and not any(item.severity is ChangeIssueSeverity.BLOCKING for item in issues) else None,
        replace(MetadataSnapshot(), **values),
        RenameDecision.APPLY_RENAME if rename else RenameDecision.KEEP_FILENAME,
        preview,
        ChangeValidationResult(issues),
    )


def sample_changes() -> tuple[FileChangeSet, ...]:
    return (
        changes(
            "first",
            (
                MetadataChange(MetadataField.TITLE, None, "Opening"),
                MetadataChange(MetadataField.COMPOSERS, (), ("First composer", "Second composer")),
                MetadataChange(MetadataField.DISC, Position(), Position(1, 2)),
            ),
            rename=True,
        ),
        changes(
            "second",
            (
                MetadataChange(MetadataField.TITLE, "Old title", "New title"),
                MetadataChange(MetadataField.ARTISTS, ("Old artist",), ()),
                MetadataChange(MetadataField.GENRES, ("Rock",), ("Soundtrack",)),
            ),
        ),
        changes("unchanged"),
    )


def test_summary_counts_selected_files_and_real_reviewed_changes_by_field() -> None:
    inputs = list(sample_changes())
    summary = build_apply_summary(inputs, backup_enabled=False, report_enabled=True)
    inputs.clear()

    assert summary.file_count == 3
    assert summary.write_file_count == 2
    assert tuple((item.field, item.count) for item in summary.additions) == (
        (MetadataField.TITLE, 1), (MetadataField.COMPOSERS, 1), (MetadataField.DISC, 1),
    )
    assert tuple((item.field, item.count) for item in summary.replacements) == (
        (MetadataField.TITLE, 1), (MetadataField.GENRES, 1),
    )
    assert tuple((item.field, item.count) for item in summary.removals) == ((MetadataField.ARTISTS, 1),)
    assert summary.rename_count == 1
    assert summary.backup_enabled is False
    assert summary.report_enabled is True
    assert summary.blocking_issues == ()
    assert summary.can_apply is True

    with pytest.raises(FrozenInstanceError):
        summary.file_count = 100


@pytest.mark.parametrize(
    ("field", "empty", "populated"),
    (
        (MetadataField.TITLE, None, "Title"),
        (MetadataField.COMPOSERS, (), ("Composer",)),
        (MetadataField.DISC, Position(), Position(None, 3)),
    ),
)
def test_additions_and_explicit_removals_recognise_semantic_empty_values(
    field: MetadataField, empty: object, populated: object,
) -> None:
    addition = changes("add", (MetadataChange(field, empty, populated),))
    removal = changes("remove", (MetadataChange(field, populated, empty),))
    summary = build_apply_summary((addition, removal), backup_enabled=True, report_enabled=False)

    assert tuple((item.field, item.count) for item in summary.additions) == ((field, 1),)
    assert tuple((item.field, item.count) for item in summary.removals) == ((field, 1),)
    assert summary.replacements == ()


def test_unresolved_mapping_blockers_retain_file_field_and_reason_and_prevent_apply() -> None:
    blocker = ChangeValidationIssue(
        ChangeIssueCode.UNRESOLVED_TRACK_MAPPING,
        ChangeIssueSeverity.BLOCKING,
        "Choose a track mapping before using this provider title.",
        MetadataField.TITLE,
    )
    warning = ChangeValidationIssue(
        ChangeIssueCode.UNRESOLVED_FIELD_PRESERVED,
        ChangeIssueSeverity.WARNING,
        "An optional composer remains unchanged.",
        MetadataField.COMPOSERS,
    )
    summary = build_apply_summary(
        (changes("blocked", (MetadataChange(MetadataField.TITLE, None, "New title"),), issues=(blocker, warning)),),
        backup_enabled=False,
        report_enabled=False,
    )

    assert summary.can_apply is False
    assert len(summary.blocking_issues) == 1
    issue = summary.blocking_issues[0]
    assert issue.file_id == "blocked"
    assert issue.code is ChangeIssueCode.UNRESOLVED_TRACK_MAPPING
    assert issue.field is MetadataField.TITLE
    assert issue.message == blocker.message


def test_accepted_but_blocked_rename_stays_in_intended_counts() -> None:
    blocker = ChangeValidationIssue(
        ChangeIssueCode.DESTINATION_COLLISION, ChangeIssueSeverity.BLOCKING, "Destination already exists.",
    )
    planned = changes("blocked-rename", rename=True, issues=(blocker,))
    assert planned.rename_change is None
    summary = build_apply_summary((planned,), backup_enabled=False, report_enabled=False)

    assert summary.rename_count == 1
    assert summary.write_file_count == 1
    assert summary.can_apply is False


def test_rejected_preview_is_not_counted_as_a_filename_write() -> None:
    unchanged = changes("unchanged")
    assert unchanged.rename_preview is not None
    summary = build_apply_summary((unchanged,), backup_enabled=False, report_enabled=False)

    assert summary.file_count == 1
    assert summary.write_file_count == 0
    assert summary.rename_count == 0
    assert summary.can_apply is False
    assert summary.additions == summary.replacements == summary.removals == ()


def test_empty_selection_cannot_apply_and_duplicate_file_ids_are_rejected() -> None:
    summary = build_apply_summary((), backup_enabled=False, report_enabled=False)
    assert summary.file_count == 0
    assert summary.can_apply is False

    file = changes("same", rename=True)

    with pytest.raises(ValueError, match="unique|duplicate"):
        build_apply_summary((file, file), backup_enabled=False, report_enabled=False)


def test_warnings_do_not_block_safe_unrelated_changes() -> None:
    warning = ChangeValidationIssue(
        ChangeIssueCode.UNRESOLVED_FIELD_PRESERVED, ChangeIssueSeverity.WARNING, "Composer remains unchanged.",
    )
    summary = build_apply_summary(
        (changes("safe", (MetadataChange(MetadataField.DISC, Position(), Position(1)),), issues=(warning,)),),
        backup_enabled=True,
        report_enabled=True,
    )

    assert summary.can_apply is True
    assert summary.blocking_issues == ()
