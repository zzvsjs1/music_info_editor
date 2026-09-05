from PySide6.QtWidgets import QDialog

from metadata_polisher.application.apply_summary import build_apply_summary
from metadata_polisher.application.changes import ChangeIssueCode, ChangeIssueSeverity, ChangeValidationIssue
from metadata_polisher.ui.dialogs.apply_summary_dialog import ApplySummaryDialog
from tests.unit.application.test_apply_summary import changes, sample_changes


def test_apply_summary_displays_reviewed_counts_and_backup_report_choices(qtbot):
    summary = build_apply_summary(sample_changes(), backup_enabled=False, report_enabled=True)
    dialog = ApplySummaryDialog(summary)
    qtbot.addWidget(dialog)
    text = dialog.summary_label.text()

    assert "3 files selected" in text
    assert "3 values added" in text
    assert "Composers: 1" in text
    assert "Disc: 1" in text
    assert "2 existing values replaced" in text
    assert "1 value cleared" in text
    assert "1 filename to rename" in text
    assert "0 blocking conflicts" in text
    assert "Permanent backup: Off" in text
    assert "JSON report: On" in text
    assert dialog.apply_button.isEnabled()

    dialog.accept()
    assert dialog.result() == QDialog.DialogCode.Accepted


# A disabled button alone is insufficient: direct acceptance must preserve the
# same blocking rule as clicking Apply, with a visible reason the user can fix.
def test_blocked_summary_explains_source_and_reason_and_cannot_be_accepted(qtbot):
    issue = ChangeValidationIssue(
        ChangeIssueCode.UNRESOLVED_TRACK_MAPPING, ChangeIssueSeverity.BLOCKING,
        "Choose a track before applying this provider value.",
    )
    summary = build_apply_summary(
        (changes("blocked-file", rename=True, issues=(issue,)),), backup_enabled=True, report_enabled=False,
    )
    dialog = ApplySummaryDialog(summary)
    qtbot.addWidget(dialog)
    assert "1 filename to rename" in dialog.summary_label.text()
    assert "1 blocking conflict" in dialog.summary_label.text()
    assert "Permanent backup: On" in dialog.summary_label.text()
    assert "JSON report: Off" in dialog.summary_label.text()
    assert "blocked-file" in dialog.issues_label.text()
    assert "UNRESOLVED_TRACK_MAPPING" in dialog.issues_label.text()
    assert issue.message in dialog.issues_label.text()
    assert not dialog.apply_button.isEnabled()

    dialog.accept()
    assert dialog.result() != QDialog.DialogCode.Accepted


def test_summary_with_zero_changes_is_disabled_and_cancel_does_not_accept(qtbot):
    summary = build_apply_summary((changes("unchanged"),), backup_enabled=False, report_enabled=False)
    dialog = ApplySummaryDialog(summary)
    qtbot.addWidget(dialog)
    assert not dialog.apply_button.isEnabled()
    assert "No changes" in dialog.issues_label.text()
    dialog.accept()
    assert dialog.result() != QDialog.DialogCode.Accepted

    valid = ApplySummaryDialog(build_apply_summary(sample_changes(), backup_enabled=False, report_enabled=False))
    qtbot.addWidget(valid)
    valid.reject()
    assert valid.result() == QDialog.DialogCode.Rejected
