"""Connect local library commands to services and central session replacement."""

import logging
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QObject, Slot
from PySide6.QtWidgets import QDialog, QFileDialog

from metadata_polisher.application.scanning import ScanLibraryResult, ScanLibraryService
from metadata_polisher.execution.cancellation import CancellationToken
from metadata_polisher.execution.events import OperationEventSink
from metadata_polisher.infrastructure.settings import GeneralSettings, load_settings, save_settings
from metadata_polisher.session.group_editing import merge_session_groups, set_disc_override, split_session_group
from metadata_polisher.session.state import GroupSelection, OperationKind, UnsupportedSelection, set_selection
from metadata_polisher.ui.dialogs.group_dialogs import DiscOverrideDialog, MergeGroupsDialog
from metadata_polisher.ui.main_window import MainWindow
from metadata_polisher.ui.pending_work import confirm_discard_pending

LOGGER = logging.getLogger(__name__)


class LibraryController(QObject):
    """Keep filesystem work in services and widget state as a projection only."""

    def __init__(self, window: MainWindow, scanner: ScanLibraryService, settings_file: Path) -> None:
        super().__init__(window)
        self._window = window
        self._scanner = scanner
        self._settings_file = settings_file
        loaded = load_settings(settings_file)
        self.settings = loaded.settings
        self._settings_error = loaded.error
        window.root_path_edit.setText(self.settings.general.last_root_folder)
        window.browse_button.clicked.connect(self.browse)
        window.rescan_button.clicked.connect(self.rescan)
        window.group_selection_requested.connect(self.select_group)
        window.unsupported_selection_requested.connect(self.select_unsupported)
        window.split_group_button.clicked.connect(self.split_group)
        window.merge_groups_button.clicked.connect(self.merge_groups)
        window.disc_override_button.clicked.connect(self.disc_override)

        assert window.operation_bridge is not None
        assert window.operation_controller is not None
        window.operation_bridge.completed.connect(self._scan_completed)
        window.operation_controller.controller_failed.connect(self._failed)

        if loaded.warnings:
            self._show_issue("\n".join(loaded.warnings))

    def _show_issue(self, message: str) -> None:
        LOGGER.warning("%s", message)
        self._window.workflow_message_label.setText(message)

    @Slot()
    def browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self._window,
            "Choose music library",
            self._window.root_path_edit.text(),
        )

        if folder:
            self._start_scan(Path(folder))

    @Slot()
    def rescan(self) -> None:
        text = self._window.root_path_edit.text().strip()

        if text:
            self._start_scan(Path(text))

    def _start_scan(self, root: Path) -> None:
        snapshot = self._window.session_state

        if snapshot.active_operation is not None:
            return

        # Validate the destination before offering to discard review work. An
        # invalid folder cannot replace the session and should not require consent.
        if not root.is_dir():
            self._show_issue("Choose an existing library folder before scanning.")
            return

        root = root.resolve()

        if not confirm_discard_pending(self._window, "scan this folder"):
            return

        # Keep the existing session while scanning. Only an accepted successful
        # result replaces groups; cancellation/failure leaves their choices intact.
        self._window.root_path_edit.setText(str(root))
        operation_id = self._window.operation_ids.next_id("SCAN")
        scanner = self._scanner

        def work(token: CancellationToken, events: OperationEventSink) -> ScanLibraryResult:
            # Capture immutable lineage and dependencies on the UI thread. The
            # worker never reads a widget or changes the current session.
            return scanner.scan_library(
                operation_id=operation_id,
                base_session_revision=snapshot.revision,
                base_library_revision=snapshot.library_revision,
                root=root,
                cancellation=token,
                events=events,
            )

        self._window.workflow_message_label.clear()
        assert self._window.operation_controller is not None
        self._window.operation_controller.start(operation_id, OperationKind.SCAN, (), work)

    @Slot(str)
    def select_group(self, group_id: str) -> None:
        self._window.set_session_state(set_selection(self._window.session_state, GroupSelection(group_id)))

    @Slot()
    def select_unsupported(self) -> None:
        self._window.set_session_state(set_selection(self._window.session_state, UnsupportedSelection()))

    @Slot(str, object)
    def _scan_completed(self, operation_id: str, result: object) -> None:
        state = self._window.session_state

        if not isinstance(result, ScanLibraryResult) or result.operation_id != operation_id:
            return

        # The operation controller is connected first and has already reduced
        # the result. A stale completion must not replace the remembered root.
        if state.root != result.root or state.library_revision != result.base_library_revision + 1:
            return

        # A fresh scan starts a new review session. Stable file IDs must not
        # silently reinclude an old write batch after its reviews were discarded.
        self._window.set_included_file_ids(frozenset())

        previous_settings = self.settings
        current = load_settings(self._settings_file)
        self.settings = replace(self.settings, general=GeneralSettings(str(result.root)))
        messages = [issue.message for issue in state.scan_issues]

        if self._settings_error is not None:
            messages.append("The library was scanned; corrupt settings were preserved. " + self._settings_error)
        elif (current.error is not None or current.settings != previous_settings
              or any("'ui.version'" in warning for warning in current.warnings)):
            # Remember the scanned folder in RAM, but never use this automatic
            # convenience save to replace external edits or a newer layout schema.
            messages.append("The library was scanned; externally changed or unsupported settings were preserved.")
        else:
            try:
                save_settings(self._settings_file, self.settings)
            except OSError as error:
                messages.append(f"The library was scanned, but settings could not be saved: {error}")

        self._window.workflow_message_label.setText("\n".join(messages))

    @Slot(str, object)
    def _failed(self, operation_id: str, error: object) -> None:
        self._show_issue(f"{operation_id} failed: {error}")

    @Slot()
    def split_group(self) -> None:
        state = self._window.session_state

        if not isinstance(state.selection, GroupSelection):
            return

        try:
            # The service enforces unique membership using stable file IDs. Qt
            # only supplies the highlighted subset and renders the returned groups.
            updated = split_session_group(state, state.selection.group_id, self._window.selected_file_ids())
        except ValueError as error:
            self._show_issue(str(error))
            return

        self._window.set_session_state(updated)

    @Slot()
    def merge_groups(self) -> None:
        state = self._window.session_state
        ids = self._window.selected_group_ids()
        groups = tuple(item.group for item in state.groups if item.group.group_id in ids)

        if state.active_operation is not None or len(groups) < 2:
            return

        dialog = MergeGroupsDialog(groups, self._window)

        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._window.set_session_state(merge_session_groups(state, ids))

    @Slot()
    def disc_override(self) -> None:
        state = self._window.session_state

        if state.active_operation is not None or not isinstance(state.selection, GroupSelection):
            return

        group_id = state.selection.group_id
        current = next(item for item in state.groups if item.group.group_id == group_id)
        dialog = DiscOverrideDialog(current.disc_number_override, self._window)

        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._window.set_session_state(set_disc_override(state, group_id, dialog.disc_number()))
