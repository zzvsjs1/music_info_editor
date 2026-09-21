from concurrent.futures import Future

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFileDialog

from metadata_polisher.bootstrap import create_application
from metadata_polisher.execution.cancellation import MutableCancellationToken
from metadata_polisher.infrastructure.settings import load_settings
from metadata_polisher.session.state import UnsupportedSelection


class ControlledHandle:
    def __init__(self, operation_id):
        self.operation_id = operation_id
        self.future = Future()
        self.token = MutableCancellationToken()

    def cancel(self):
        self.token.cancel()

    def is_cancel_requested(self):
        return self.token.is_cancelled()

    def done(self):
        return self.future.done()

    def result(self, timeout=None):
        return self.future.result(timeout)

    def add_done_callback(self, callback):
        self.future.add_done_callback(lambda _: callback(self))


class ControlledExecutor:
    # Queue work until run_next so assertions can inspect the busy UI before
    # completion, while the real Qt bridge still controls signal delivery.
    def __init__(self):
        self.pending = []

    def submit(self, operation_id, work, events):
        handle = ControlledHandle(operation_id)
        self.pending.append((handle, work, events))
        return handle

    def run_next(self):
        handle, work, events = self.pending.pop(0)

        try:
            handle.future.set_result(work(handle.token, events))
        except Exception as error:
            handle.future.set_exception(error)

    def shutdown(self, wait=True):
        return


def test_browse_runs_local_scan_and_shows_unsupported_rows(qtbot, tmp_path, monkeypatch):
    import wave

    root = tmp_path / "library"
    root.mkdir()

    # Generate a tiny valid supported file beside an unsupported fixture; this
    # exercises actual local scanning without depending on the user's library.
    with wave.open(str(root / "01. Theme.wav"), "wb") as audio:
        audio.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
        audio.writeframes(b"\x00\x00" * 80)

    (root / "02. Bonus.opus").write_bytes(b"unsupported fixture")
    settings_file = tmp_path / "settings.json"
    executor = ControlledExecutor()
    _, window = create_application([], executor=executor, settings_file=settings_file)
    qtbot.addWidget(window)
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *args: str(root))

    assert not executor.pending
    qtbot.mouseClick(window.browse_button, Qt.MouseButton.LeftButton)
    assert len(executor.pending) == 1
    assert window.session_state.groups == ()
    assert not window.browse_button.isEnabled()
    assert not window.rescan_button.isEnabled()

    with qtbot.waitSignal(window.operation_bridge.completed):
        executor.run_next()

    assert window.session_state.root == root
    assert len(window.session_state.groups) == 1
    assert window.browse_button.isEnabled()
    assert window.rescan_button.isEnabled()
    assert window.group_model.rowCount() == 2

    window.group_view.setCurrentIndex(window.group_model.index(0, 0))
    assert window.file_model.data(window.file_model.index(0, 2)) == "01. Theme.wav"

    window.group_view.setCurrentIndex(window.group_model.index(1, 0))
    assert isinstance(window.session_state.selection, UnsupportedSelection)
    assert window.file_model.data(window.file_model.index(0, 1)) == "Not supported yet"
    assert window.file_model.data(window.file_model.index(0, 2)) == "02. Bonus.opus"
    assert window.diff_model.rowCount() == 0
    assert load_settings(settings_file).settings.general.last_root_folder == str(root)

    qtbot.mouseClick(window.rescan_button, Qt.MouseButton.LeftButton)
    assert len(executor.pending) == 1

    with qtbot.waitSignal(window.operation_bridge.completed):
        executor.run_next()

    assert len(window.session_state.groups) == 1


def test_cancelled_browse_does_not_scan_or_save(qtbot, tmp_path, monkeypatch):
    executor = ControlledExecutor()
    settings_file = tmp_path / "settings.json"
    _, window = create_application([], executor=executor, settings_file=settings_file)
    qtbot.addWidget(window)
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *args: "")
    qtbot.mouseClick(window.browse_button, Qt.MouseButton.LeftButton)

    assert executor.pending == []
    assert not settings_file.exists()


@pytest.mark.parametrize("outcome", ["completed", "failed", "cancelled"])
def test_scan_progress_closes_only_after_success(qtbot, tmp_path, outcome):
    executor = ControlledExecutor()
    root = tmp_path / "library"
    root.mkdir()
    _, window = create_application([], executor=executor, settings_file=tmp_path / "settings.json")
    qtbot.addWidget(window)
    window.root_path_edit.setText(str(root))
    qtbot.mouseClick(window.rescan_button, Qt.MouseButton.LeftButton)
    controller = window.operation_controller
    assert controller is not None
    dialog = controller.progress_dialog
    assert dialog is not None
    assert dialog.isVisible()

    # Completion must be delivered through the queued bridge before the scan
    # window disappears. Failures and cancellation stay visible for inspection.
    if outcome == "failed":
        handle, _, _ = executor.pending.pop()

        with qtbot.waitSignal(window.operation_bridge.failed):
            handle.future.set_exception(OSError("scan inaccessible"))
    else:
        if outcome == "cancelled":
            assert controller.cancel_active()
            assert dialog.isVisible()

        terminal = getattr(window.operation_bridge, outcome)

        with qtbot.waitSignal(terminal):
            executor.run_next()

    assert window.session_state.active_operation is None
    assert window.rescan_button.isEnabled()
    assert dialog.stage_label.text() == outcome.capitalize()
    assert dialog.isVisible() is (outcome != "completed")


@pytest.mark.parametrize("corrupt", [False, True])
def test_scan_failure_or_corrupt_settings_preserves_existing_bytes(qtbot, tmp_path, corrupt):
    executor = ControlledExecutor()
    settings_file = tmp_path / "settings.json"
    original = b"{broken" if corrupt else b'{"schema_version": 1}'
    settings_file.write_bytes(original)
    root = tmp_path / "library"
    root.mkdir()
    _, window = create_application([], executor=executor, settings_file=settings_file)
    qtbot.addWidget(window)
    window.root_path_edit.setText(str(root))
    qtbot.mouseClick(window.rescan_button, Qt.MouseButton.LeftButton)

    if corrupt:
        with qtbot.waitSignal(window.operation_bridge.completed):
            executor.run_next()

        assert "corrupt settings were preserved" in window.workflow_message_label.text()
    else:
        handle, _, _ = executor.pending.pop()

        with qtbot.waitSignal(window.operation_bridge.failed):
            handle.future.set_exception(OSError("scan inaccessible"))

        assert "scan inaccessible" in window.workflow_message_label.text()

    assert settings_file.read_bytes() == original
    assert window.session_state.active_operation is None
    assert window.rescan_button.isEnabled()
    assert window.root_path_edit.text() == str(root)
    qtbot.mouseClick(window.rescan_button, Qt.MouseButton.LeftButton)
    assert len(executor.pending) == 1


@pytest.mark.parametrize("external_edit", [False, True])
def test_scan_preserves_future_layout_or_externally_updated_settings(qtbot, tmp_path, external_edit):
    executor = ControlledExecutor()
    path = tmp_path / "settings.json"
    original = b'{"schema_version": 1, "ui": {"version": 99, "custom": true}}'

    if not external_edit:
        path.write_bytes(original)

    _, window = create_application([], executor=executor, settings_file=path)
    qtbot.addWidget(window)
    root = tmp_path / "library"
    root.mkdir()

    if external_edit:
        original = b'{"schema_version": 1, "general": {"last_root_folder": "external-folder"}}'
        path.write_bytes(original)

    window.root_path_edit.setText(str(root))
    qtbot.mouseClick(window.rescan_button, Qt.MouseButton.LeftButton)

    with qtbot.waitSignal(window.operation_bridge.completed):
        executor.run_next()

    assert window.session_state.root == root
    assert path.read_bytes() == original
