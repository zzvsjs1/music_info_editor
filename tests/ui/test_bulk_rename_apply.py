"""The visible batch workflow reaches safe writing through either review scope."""

import wave

import pytest
from mutagen.id3 import TALB, TIT2, TRCK
from mutagen.wave import WAVE
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from metadata_polisher.application.apply import ApplyFileOutcomeStatus
from metadata_polisher.bootstrap import create_application
from tests.ui.test_scan_workflow import ControlledExecutor


@pytest.mark.parametrize("scope", ["selected", "library", "rename_dialog"])
# Rename-only writes should preserve the complete generated audio bytes. The
# alternative scopes also prove that selection cannot rename unrelated groups.
def test_select_all_to_confirmed_rename_keeps_file_bytes_and_scope(qtbot, tmp_path, scope):
    originals = {}

    # Two album folders distinguish Select all in the displayed table from
    # All library files. Every write uses disposable, generated audio.
    for album, titles in (("Album one", ("Opening", "Finale")), ("Album two", ("Bonus",))):
        directory = tmp_path / "library" / album
        directory.mkdir(parents=True)

        for number, title in enumerate(titles, 1):
            path = directory / f"original-{number}.wav"

            with wave.open(str(path), "wb") as audio:
                audio.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
                audio.writeframes(b"\x00\x00" * 80)

            tags = WAVE(path)
            tags.add_tags()
            tags.tags.add(TALB(encoding=3, text=[album]))
            tags.tags.add(TIT2(encoding=3, text=[title]))
            tags.tags.add(TRCK(encoding=3, text=[str(number)]))
            tags.save()
            originals[path] = path.read_bytes()

    executor = ControlledExecutor()
    _, window = create_application([], executor=executor, settings_file=tmp_path / "settings.json")
    qtbot.addWidget(window)
    window.root_path_edit.setText(str(tmp_path / "library"))
    window.rescan_button.click()
    executor.run_next()
    qtbot.waitUntil(lambda: window.session_state.active_operation is None)
    assert len(window.session_state.groups) == 2
    window.group_view.setCurrentIndex(window.group_model.index(0, 0))
    window.select_all_files_button.click()
    if scope != "rename_dialog":
        window.open_review_button.click()
        window.review_scope_combo.setCurrentIndex(window.review_scope_combo.findData(scope))

    targets = set(window.review_target_file_ids())
    assert len(targets) == (3 if scope == "library" else 2)

    if scope == "rename_dialog":
        def prepare_and_cancel_summary():
            dialog = QApplication.activeModalWidget()
            QTimer.singleShot(0, lambda: QApplication.activeModalWidget().reject())
            dialog.accept()

        QTimer.singleShot(0, prepare_and_cancel_summary)
        window.rename_files_button.click()
    else:
        window.apply_rename_button.click()
        assert not window.included_file_ids
        window.include_review_scope_button.click()

    assert window.included_file_ids == targets
    assert window.review_apply_button.isEnabled()
    assert window.review_message_label.text() == window.workflow_message_label.text()

    # Cancelling from the new review-window entry point must leave both the
    # source bytes and the worker queue untouched before the real confirmation.
    QTimer.singleShot(0, lambda: QApplication.activeModalWidget().reject())
    window.review_apply_button.click()
    assert executor.pending == []
    assert all(path.read_bytes() == data for path, data in originals.items())

    QTimer.singleShot(0, lambda: QApplication.activeModalWidget().accept())
    window.review_apply_button.click()
    assert len(executor.pending) == 1
    assert not window.review_apply_button.isEnabled()
    assert not window.include_review_scope_button.isEnabled()
    executor.run_next()
    qtbot.waitUntil(lambda: window.session_state.active_operation is None)
    result = window.apply_controller.last_result
    outcomes = [item for group in result.groups for item in group.files]
    assert len(outcomes) == len(targets)
    assert all(item.status is ApplyFileOutcomeStatus.APPLIED for item in outcomes)

    for item in outcomes:
        assert item.final_path != item.source_path
        assert item.final_path.read_bytes() == originals[item.source_path]
        assert not item.source_path.exists()

    changed_paths = {item.source_path for item in outcomes}
    assert all(path.read_bytes() == data for path, data in originals.items() if path not in changed_paths)
