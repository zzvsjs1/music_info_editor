"""Commit staged settings and refresh only settings-dependent session previews."""

from pathlib import Path

from PySide6.QtCore import QObject, Signal, Slot
from PySide6.QtWidgets import QDialog

from metadata_polisher.application.provider_connection import ProviderConnectionResult, test_provider_connection
from metadata_polisher.execution.cancellation import CancellationToken
from metadata_polisher.execution.events import OperationEventSink
from metadata_polisher.infrastructure.settings import ProvidersSettings, save_settings
from metadata_polisher.providers.base import RequestContext
from metadata_polisher.providers.catalogue import provider_label
from metadata_polisher.session.review_editing import refresh_rename_previews
from metadata_polisher.session.state import OperationKind, SessionState
from metadata_polisher.ui.dialogs.settings_dialog import SettingsDialog
from metadata_polisher.ui.main_window import MainWindow


class SettingsController(QObject):
    """Preserve both saved and in-memory settings when persistence fails."""

    settings_changed = Signal(object)

    def __init__(self, window: MainWindow, settings_file: Path) -> None:
        super().__init__(window)
        self._window = window
        self._settings_file = settings_file
        self._active_dialog: SettingsDialog | None = None
        self._dialog_state: SessionState | None = None
        self._provider_test_operation: str | None = None
        window.settings_button.clicked.connect(self.edit_settings)
        assert window.operation_bridge is not None
        window.operation_bridge.completed.connect(self._test_completed)
        window.operation_bridge.cancelled.connect(self._test_cancelled)
        window.operation_bridge.failed.connect(self._test_failed)

    @Slot()
    def edit_settings(self) -> None:
        window = self._window
        library = window.library_controller
        state = window.session_state

        if state.active_operation is not None or library is None:
            return

        current = library.settings
        assert window.lookup_controller is not None
        dialog = SettingsDialog(current, window, credentials=window.lookup_controller.session_credentials.snapshot())
        self._active_dialog = dialog
        self._dialog_state = state
        dialog.provider_test_requested.connect(self._test_provider)
        dialog.provider_test_cancel_requested.connect(self._cancel_test)
        dialog.session_login_save_requested.connect(self._save_session_login)
        dialog.session_login_forget_requested.connect(self._forget_session_login)

        if dialog.exec() != QDialog.DialogCode.Accepted:
            self._active_dialog = None
            self._dialog_state = None
            return

        # An explicit provider test can legitimately advance operation state
        # while Settings is open; its completion updates the expected snapshot.
        expected_state = self._dialog_state
        self._active_dialog = None
        self._dialog_state = None

        if window.session_state is not expected_state or library.settings != current:
            window.workflow_message_label.setText("The session changed while Settings was open. Open Settings again.")
            return

        settings = dialog.settings()
        state = window.session_state

        try:
            # Prepare an immutable preview first, then persist settings before
            # publishing either object. A failed save leaves the live review intact.
            updated = refresh_rename_previews(state, settings.rename) if settings.rename != current.rename else state
            save_settings(self._settings_file, settings)
        except (OSError, ValueError) as error:
            window.workflow_message_label.setText(f"Settings could not be saved: {error}")
            return

        # Installing the pure preview follows the successful atomic save. A
        # provider preference affects the next lookup and leaves current evidence
        # and explicit field choices intact.
        library.settings = settings
        window.set_session_state(updated)
        assert window.lookup_controller is not None
        window.lookup_controller.refresh_provider_label()
        window.workflow_message_label.setText("Settings saved.")
        self.settings_changed.emit(settings)

    @Slot()
    def _test_provider(self) -> None:
        dialog = self._active_dialog
        window = self._window

        if dialog is None or window.session_state.active_operation is not None:
            return

        selected = dialog.provider_combo.currentData()

        if selected not in {"musicbrainz_direct", "vgmdb"}:
            return

        assert window.lookup_controller is not None
        # Testing a draft login is not Save for this session. Capture it for this
        # request without replacing the active process-wide credential snapshot.
        credential_snapshot = dialog.draft_credential_snapshot()
        service = window.lookup_controller.connection_service(
            ProvidersSettings(selected), network=dialog.network_settings(), credentials=credential_snapshot,
        )

        if service is None:
            dialog.provider_test_status.setText(
                window.workflow_message_label.text() or "The provider test was not started.",
            )
            return

        operation_id = window.operation_ids.next_id("PROVIDER_TEST")
        context = RequestContext(operation_id, "auto", credential_generation=credential_snapshot.generation)

        def work(token: CancellationToken, events: OperationEventSink) -> ProviderConnectionResult:
            return test_provider_connection(service, selected, context, token, events)

        self._provider_test_operation = operation_id
        dialog.set_provider_test_running(True)
        dialog.provider_test_status.setText(f"Testing {provider_label(selected)}…")
        assert window.operation_controller is not None

        try:
            window.operation_controller.start(operation_id, OperationKind.PROVIDER_TEST, (), work)
        except Exception:
            self._finish_test(operation_id, "The provider test could not be started.")
            raise

    @Slot()
    def _save_session_login(self) -> None:
        dialog = self._active_dialog

        if dialog is None or self._provider_test_operation is not None:
            return

        lookup = self._window.lookup_controller
        assert lookup is not None
        auth = dialog.session_proxy_auth()
        lookup.session_credentials.set_proxy(*(auth or ("", "")))
        lookup.invalidate_session_credentials()
        dialog.install_credential_snapshot(lookup.session_credentials.snapshot())

    @Slot()
    def _forget_session_login(self) -> None:
        dialog = self._active_dialog

        if dialog is None or self._provider_test_operation is not None:
            return

        lookup = self._window.lookup_controller
        assert lookup is not None
        lookup.session_credentials.forget()
        lookup.invalidate_session_credentials()
        dialog.install_credential_snapshot(lookup.session_credentials.snapshot())

    @Slot()
    def _cancel_test(self) -> None:
        if self._provider_test_operation is None:
            return

        assert self._window.operation_controller is not None
        self._window.operation_controller.cancel_active()

        if self._active_dialog is not None:
            self._active_dialog.provider_test_status.setText(
                "Cancelling test; waiting for the bounded request to finish…",
            )

    @Slot(str, object)
    def _test_completed(self, operation_id: str, result: object) -> None:
        if not isinstance(result, ProviderConnectionResult):
            return

        message = (
            f"{provider_label(result.provider_id)} connection test passed; "
            "the adapter returned a valid catalogue response."
            if result.issue is None else f"{result.issue.code.value}: {result.issue.message}"
        )
        self._finish_test(operation_id, message)

    @Slot(str)
    def _test_cancelled(self, operation_id: str) -> None:
        self._finish_test(operation_id, "Provider test cancelled.")

    @Slot(str, object)
    def _test_failed(self, operation_id: str, _error: object) -> None:
        self._finish_test(operation_id, "Provider test failed; no successful connection was established.")

    def _finish_test(self, operation_id: str, message: str) -> None:
        if operation_id != self._provider_test_operation:
            return

        self._provider_test_operation = None
        self._dialog_state = self._window.session_state
        assert self._window.lookup_controller is not None
        self._window.lookup_controller.finish_connection_test()

        if self._active_dialog is not None:
            self._active_dialog.set_provider_test_running(False)
            self._active_dialog.provider_test_status.setText(message)
