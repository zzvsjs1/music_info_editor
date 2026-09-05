from PySide6.QtCore import Qt

from metadata_polisher.infrastructure.diagnostics import SequentialOperationIds
from metadata_polisher.infrastructure.settings import AppSettings, DiagnosticsSettings, save_settings
from tests.ui.test_lookup_workflow import lookup_window


# Operation IDs join UI activity to retained trace evidence; the test follows
# the composed workflow so isolated logger output cannot hide broken wiring.
def test_shared_operation_ids_and_enabled_lookup_traces_retain_structured_evidence(qtbot, tmp_path):
    save_settings(tmp_path / "settings.json", AppSettings(diagnostics=DiagnosticsSettings(True)))
    window, executor, _ = lookup_window(qtbot, tmp_path)
    window.operation_ids = SequentialOperationIds()
    qtbot.mouseClick(window.find_selected_button, Qt.MouseButton.LeftButton)
    assert executor.pending[0][0].operation_id == "LOOKUP-0001"

    with qtbot.waitSignal(window.operation_bridge.completed):
        executor.run_next()

    dialog = window.lookup_controller.candidate_dialog
    dialog.table.selectRow(0)
    qtbot.mouseClick(dialog.choose_button, Qt.MouseButton.LeftButton)
    assert executor.pending[0][0].operation_id == "LOOKUP-0002"

    with qtbot.waitSignal(window.operation_bridge.completed):
        executor.run_next()

    contents = (tmp_path / "logs" / "metadata-polisher.log").read_text(encoding="utf-8")
    assert "LOOKUP-0001" in contents and "LOOKUP-0002" in contents
    assert "release_candidates" in contents
    assert "effective_track_mapping" in contents
    assert "provenance" in contents
    assert "TRACK_TITLE_EXACT" in contents
