from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication

from metadata_polisher.infrastructure.diagnostics import build_group_diagnostic_summary
from metadata_polisher.ui.dialogs.diagnostics_dialog import DiagnosticsDialog
from tests.ui.test_review_workflow import review_window
from tests.unit.session.test_lookup_editing import make_selected_session


# Clipboard output must match the already redacted display. Intercept the
# desktop URL handler so testing Open logs never launches an external window.
def test_diagnostics_dialog_copies_redacted_summary_and_opens_only_log_folder(qtbot, tmp_path, monkeypatch):
    document = build_group_diagnostic_summary(make_selected_session().groups[0])
    document["Authorization"] = "Bearer secret-value"
    opened = []
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: opened.append(url.toLocalFile()) or True)
    dialog = DiagnosticsDialog(document, tmp_path / "logs")
    qtbot.addWidget(dialog)
    assert dialog.summary_edit.isReadOnly()
    qtbot.mouseClick(dialog.copy_button, Qt.MouseButton.LeftButton)
    copied = QApplication.clipboard().text()
    assert "secret-value" not in copied
    assert "REDACTED" in copied
    assert "LOOKUP-0001" in copied
    assert "effective_track_mapping" in copied
    qtbot.mouseClick(dialog.open_logs_button, Qt.MouseButton.LeftButton)
    assert [Path(path) for path in opened] == [tmp_path / "logs"]


def test_window_diagnostics_exposes_match_and_mapping_evidence(qtbot, tmp_path):
    window = review_window(qtbot, tmp_path)
    qtbot.mouseClick(window.diagnostics_button, Qt.MouseButton.LeftButton)
    dialog = window.diagnostics_controller.dialog
    assert dialog.isVisible()
    qtbot.mouseClick(dialog.why_match_button, Qt.MouseButton.LeftButton)
    assert dialog.explanation_dialog.table.model().rowCount() > 0
    dialog.explanation_dialog.close()
    qtbot.mouseClick(dialog.why_track_mapping_button, Qt.MouseButton.LeftButton)
    evidence = dialog.explanation_dialog.table.model()
    codes = [evidence.index(row, 0).data() for row in range(evidence.rowCount())]
    assert any(code.startswith("TRACK_") for code in codes)
