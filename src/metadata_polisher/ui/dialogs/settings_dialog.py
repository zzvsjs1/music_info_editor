"""Native settings editor that stages preferences without writing to disk."""

from dataclasses import replace
from functools import partial

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from metadata_polisher.infrastructure.session_credentials import CredentialSnapshot
from metadata_polisher.infrastructure.settings import (
    AppSettings,
    BackupSettings,
    DiagnosticsSettings,
    MatchingSettings,
    NetworkSettings,
    ProvidersSettings,
    RenameSettings,
    ReportsSettings,
)
from metadata_polisher.providers.catalogue import provider_label, provider_summary
from metadata_polisher.providers.network import describe_network_route, validate_network_settings
from metadata_polisher.rename.template import MAXIMUM_PADDING_DIGITS, parse_template
from metadata_polisher.ui.template_edit import TemplateLineEdit


def _integer_control(value: int, minimum: int, parent: QWidget, *, maximum: int = 2_147_483_647) -> QSpinBox:
    control = QSpinBox(parent)
    control.setRange(minimum, maximum)
    control.setValue(value)
    return control


class SettingsDialog(QDialog):
    """Collect typed preferences for the controller to validate, save and apply."""

    provider_test_requested = Signal()
    provider_test_cancel_requested = Signal()
    session_login_save_requested = Signal()
    session_login_forget_requested = Signal()

    def __init__(
        self, settings: AppSettings, parent: QWidget | None = None, *,
        credentials: CredentialSnapshot | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.resize(760, 490)
        self._original = settings
        self._credential_snapshot = credentials if credentials is not None else CredentialSnapshot(0, None)
        self._provider_test_running = False
        self._external_tools = dict(settings.external_tools)
        layout = QVBoxLayout(self)
        tabs = QTabWidget(self)
        layout.addWidget(tabs)
        self._build_rename_tab(tabs)
        self._build_providers_tab(tabs)
        self._build_network_tab(tabs)
        self._build_output_tab(tabs)
        self._build_tools_tab(tabs)
        self.error_label = QLabel(self)
        self.error_label.setTextFormat(Qt.TextFormat.PlainText)
        self.error_label.setWordWrap(True)
        layout.addWidget(self.error_label)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel, self,
        )
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

    def _build_rename_tab(self, tabs: QTabWidget) -> None:
        page = QWidget(tabs)
        layout = QFormLayout(page)
        self.rename_enabled = QCheckBox("Enable filename renaming", page)
        self.rename_enabled.setChecked(self._original.rename.enabled)
        self.template_edit = TemplateLineEdit(self._original.rename.template, page)
        self.track_digits_spin = _integer_control(
            self._original.rename.minimum_track_digits, 1, page, maximum=MAXIMUM_PADDING_DIGITS,
        )
        self.disc_digits_spin = _integer_control(
            self._original.rename.minimum_disc_digits, 1, page, maximum=MAXIMUM_PADDING_DIGITS,
        )
        layout.addRow(self.rename_enabled)
        layout.addRow("Filename template", self.template_edit)
        layout.addRow("Minimum track digits", self.track_digits_spin)
        layout.addRow("Minimum disc digits", self.disc_digits_spin)
        help_text = QLabel(
            "Type % to choose a field, then press Enter or Tab to insert it. "
            "Press Escape to close suggestions, or Ctrl+Space to reopen them inside a field. "
            "Put optional content in brackets, for example [%discnumber%.]. "
            "The file extension is retained automatically. Minimum digit widths are limited to 1–10: "
            "ten digits cover the largest editable track or disc number without excessive zero padding.",
            page,
        )
        help_text.setWordWrap(True)
        layout.addRow(help_text)
        tabs.addTab(page, "Renaming")

    def _build_providers_tab(self, tabs: QTabWidget) -> None:
        page = QWidget(tabs)
        layout = QFormLayout(page)
        self.preferred_language_edit = QLineEdit(self._original.matching.preferred_language, page)
        layout.addRow("Preferred language (auto, ja, en…)", self.preferred_language_edit)
        self.provider_combo = QComboBox(page)

        for provider_id in ("musicbrainz_direct", "vgmdb", None):
            self.provider_combo.addItem(provider_label(provider_id), provider_id)

        selected = self._original.providers.selected_provider_id
        selected_index = self.provider_combo.findData(selected)

        if selected_index < 0:
            # Preserve an unavailable stored ID for explicit correction. Choosing
            # a default here would silently change the next lookup's provider.
            self.provider_combo.addItem("Unavailable stored provider", selected)
            selected_index = self.provider_combo.count() - 1

        self.provider_combo.setCurrentIndex(selected_index)
        layout.addRow("Lookup provider", self.provider_combo)
        self.provider_capabilities_label = QLabel(page)
        self.provider_capabilities_label.setTextFormat(Qt.TextFormat.PlainText)
        self.provider_capabilities_label.setWordWrap(True)
        layout.addRow(self.provider_capabilities_label)
        self.test_provider_button = QPushButton("Test selected provider", page)
        self.cancel_provider_test_button = QPushButton("Cancel test", page)
        self.cancel_provider_test_button.setEnabled(False)
        actions = QHBoxLayout()
        actions.addWidget(self.test_provider_button)
        actions.addWidget(self.cancel_provider_test_button)
        layout.addRow(actions)
        self.provider_test_status = QLabel("Not tested in this dialogue.", page)
        self.provider_test_status.setTextFormat(Qt.TextFormat.PlainText)
        self.provider_test_status.setWordWrap(True)
        layout.addRow(self.provider_test_status)
        self.provider_test_progress = QProgressBar(page)
        self.provider_test_progress.setRange(0, 0)
        self.provider_test_progress.hide()
        layout.addRow(self.provider_test_progress)
        self.test_provider_button.clicked.connect(self.provider_test_requested)
        self.cancel_provider_test_button.clicked.connect(self.provider_test_cancel_requested)
        self.provider_combo.currentIndexChanged.connect(self._provider_changed)
        self._provider_changed()
        hint = QLabel(
            "Provider changes apply to the next operation. Existing candidates and review decisions are retained.",
            page,
        )
        hint.setWordWrap(True)
        layout.addRow(hint)
        tabs.addTab(page, "Providers and language")

    @Slot()
    def _provider_changed(self) -> None:
        selected = self.provider_combo.currentData()
        self.provider_capabilities_label.setText(provider_summary(selected))
        self.provider_test_status.setText("Not tested in this dialogue.")
        self.test_provider_button.setEnabled(selected in {"musicbrainz_direct", "vgmdb"})

    def set_provider_test_running(self, running: bool) -> None:
        """Freeze the captured selection until the bounded background test settles."""
        self._provider_test_running = running
        self.provider_combo.setEnabled(not running)
        self.buttons.setEnabled(not running)
        self.test_provider_button.setEnabled(
            not running and self.provider_combo.currentData() in {"musicbrainz_direct", "vgmdb"},
        )
        self.cancel_provider_test_button.setEnabled(running)
        self.provider_test_progress.setVisible(running)
        self.network_mode_combo.setEnabled(not running)
        self._network_changed()

    def reject(self) -> None:
        if self._provider_test_running:
            self.provider_test_cancel_requested.emit()
            self.provider_test_status.setText("Cancelling test; waiting for the bounded request to finish…")
            return

        self._clear_credential_edits()
        super().reject()

    def _build_network_tab(self, tabs: QTabWidget) -> None:
        page = QWidget(tabs)
        layout = QFormLayout(page)
        self.network_mode_combo = QComboBox(page)
        self.network_mode_combo.addItem("Direct", "direct")
        self.network_mode_combo.addItem("Manual HTTP proxy", "manual_proxy")
        mode = self._original.network.mode
        index = self.network_mode_combo.findData(mode)

        if index < 0:
            self.network_mode_combo.addItem("Unavailable stored route", mode)
            index = self.network_mode_combo.count() - 1

        self.network_mode_combo.setCurrentIndex(index)
        layout.addRow("External services route", self.network_mode_combo)
        self.proxy_host_edit = QLineEdit(self._original.network.proxy_host, page)
        self.proxy_host_edit.setPlaceholderText("Hostname or IP address, without http://")
        # Keep an invalid stored port visibly invalid instead of clamping it to
        # a different valid destination. Validation happens before Save/Test.
        self.proxy_port_spin = _integer_control(self._original.network.proxy_port, 0, page)
        layout.addRow("HTTP proxy host", self.proxy_host_edit)
        layout.addRow("Port", self.proxy_port_spin)
        self.effective_route_label = QLabel(page)
        self.effective_route_label.setTextFormat(Qt.TextFormat.PlainText)
        self.effective_route_label.setWordWrap(True)
        layout.addRow(self.effective_route_label)
        self.proxy_username_edit = QLineEdit(page)
        self.proxy_password_edit = QLineEdit(page)
        self.proxy_password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        layout.addRow("Session proxy username", self.proxy_username_edit)
        layout.addRow("Session proxy password", self.proxy_password_edit)
        self.save_session_login_button = QPushButton("Save for this session", page)
        self.forget_session_login_button = QPushButton("Forget session login", page)
        actions = QHBoxLayout()
        actions.addWidget(self.save_session_login_button)
        actions.addWidget(self.forget_session_login_button)
        layout.addRow(actions)
        self.session_credentials_status = QLabel(page)
        self.session_credentials_status.setTextFormat(Qt.TextFormat.PlainText)
        self.session_credentials_status.setWordWrap(True)
        layout.addRow(self.session_credentials_status)
        hint = QLabel(
            "Proxy credentials stay in memory and are lost when the application closes. "
            "Test selected provider uses the draft credentials. Save for this session activates them; "
            "closing Settings discards unsaved credential edits. Route preferences use the main Save button. "
            "Direct and Manual HTTP proxy ignore environment proxy settings. "
            "HTTPS certificate checks stay enabled.", page,
        )
        hint.setWordWrap(True)
        layout.addRow(hint)
        self.network_mode_combo.currentIndexChanged.connect(self._network_changed)
        self.proxy_host_edit.textChanged.connect(self._route_changed)
        self.proxy_port_spin.valueChanged.connect(self._route_changed)
        # A connection result describes the exact captured draft. Editing either
        # credential makes that result stale just as changing its route does.
        # During a running test all four draft controls are already disabled.
        self.proxy_username_edit.textChanged.connect(self._route_changed)
        self.proxy_password_edit.textChanged.connect(self._route_changed)
        self.save_session_login_button.clicked.connect(self.session_login_save_requested)
        self.forget_session_login_button.clicked.connect(self.session_login_forget_requested)
        self.install_credential_snapshot(self._credential_snapshot)
        self._network_changed()
        tabs.addTab(page, "Network and session login")

    @Slot()
    def _network_changed(self) -> None:
        manual = self.network_mode_combo.currentData() == "manual_proxy" and not self._provider_test_running

        for control in (self.proxy_host_edit, self.proxy_port_spin, self.proxy_username_edit, self.proxy_password_edit):
            control.setEnabled(manual)

        # Saving credentials is explicit and harmless even while Direct is the
        # current route; they are sent only after Manual HTTP proxy is selected.
        self.save_session_login_button.setEnabled(not self._provider_test_running)
        self.forget_session_login_button.setEnabled(not self._provider_test_running)
        self._route_changed()

    @Slot()
    def _route_changed(self) -> None:
        self.effective_route_label.setText(describe_network_route(self.network_settings()))

        if not self._provider_test_running:
            self.provider_test_status.setText("Not tested with this route and credentials.")

    def network_settings(self) -> NetworkSettings:
        return NetworkSettings(
            self.network_mode_combo.currentData(), self.proxy_host_edit.text(), self.proxy_port_spin.value(),
        )

    def session_proxy_auth(self) -> tuple[str, str] | None:
        username, password = self.proxy_username_edit.text(), self.proxy_password_edit.text()
        return (username, password) if username or password else None

    def draft_credential_snapshot(self) -> CredentialSnapshot:
        # Copy editor values into a request snapshot; this accessor neither
        # activates them nor includes secrets in serialisable AppSettings.
        return CredentialSnapshot(self._credential_snapshot.generation, self.session_proxy_auth())

    def install_credential_snapshot(self, snapshot: CredentialSnapshot) -> None:
        self._credential_snapshot = snapshot
        username, password = snapshot.proxy_auth or ("", "")
        self.proxy_username_edit.setText(username)
        self.proxy_password_edit.setText(password)
        self.session_credentials_status.setText(
            "Proxy credentials are configured for this session."
            if snapshot.proxy_auth is not None else "No proxy credentials are configured for this session.",
        )
        self.provider_test_status.setText("Not tested with this route and credentials.")

    def _clear_credential_edits(self) -> None:
        # These are dialogue-local references. Closing Settings clears unsaved
        # drafts without forgetting credentials already saved in the controller.
        self._credential_snapshot = CredentialSnapshot(0, None)
        self.proxy_username_edit.clear()
        self.proxy_password_edit.clear()

    def _build_output_tab(self, tabs: QTabWidget) -> None:
        page = QWidget(tabs)
        layout = QFormLayout(page)
        self.backup_enabled = QCheckBox("Keep permanent backups", page)
        self.backup_enabled.setChecked(self._original.backup.enabled)
        self.backup_directory_edit = QLineEdit(self._original.backup.directory, page)
        self.backup_browse_button = QPushButton("Browse…", page)
        backup_path = QHBoxLayout()
        backup_path.addWidget(self.backup_directory_edit)
        backup_path.addWidget(self.backup_browse_button)
        layout.addRow(self.backup_enabled)
        layout.addRow("Backup directory", backup_path)
        self.reports_enabled = QCheckBox("Save processing reports", page)
        self.reports_enabled.setChecked(self._original.reports.enabled)
        self.reports_directory_edit = QLineEdit(self._original.reports.directory, page)
        self.reports_directory_edit.setPlaceholderText("Blank uses the application reports directory")
        self.reports_browse_button = QPushButton("Browse…", page)
        reports_path = QHBoxLayout()
        reports_path.addWidget(self.reports_directory_edit)
        reports_path.addWidget(self.reports_browse_button)
        layout.addRow(self.reports_enabled)
        layout.addRow("Report directory", reports_path)
        self.detailed_trace_check = QCheckBox("Enable detailed diagnostic tracing", page)
        self.detailed_trace_check.setChecked(self._original.diagnostics.detailed_tracing)
        layout.addRow(self.detailed_trace_check)
        self.backup_browse_button.clicked.connect(
            partial(self._browse_directory, self.backup_directory_edit, "Choose backup directory")
        )
        self.reports_browse_button.clicked.connect(
            partial(self._browse_directory, self.reports_directory_edit, "Choose report directory")
        )
        tabs.addTab(page, "Backups, reports and diagnostics")

    def _build_tools_tab(self, tabs: QTabWidget) -> None:
        page = QWidget(tabs)
        layout = QVBoxLayout(page)
        self.external_tools_table = QTableWidget(0, 2, page)
        self.external_tools_table.setHorizontalHeaderLabels(("Tool name", "Executable path"))
        self.external_tools_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.external_tools_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.external_tools_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.external_tools_table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.external_tools_table)
        controls = QHBoxLayout()
        self.tool_name_edit = QLineEdit(page)
        self.tool_name_edit.setPlaceholderText("Tool name")
        self.add_tool_button = QPushButton("Browse executable…", page)
        self.remove_tool_button = QPushButton("Remove selected", page)
        controls.addWidget(self.tool_name_edit)
        controls.addWidget(self.add_tool_button)
        controls.addWidget(self.remove_tool_button)
        layout.addLayout(controls)
        hint = QLabel("Enter a tool name and choose its executable. Reusing a name replaces its saved path.", page)
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.add_tool_button.clicked.connect(self._add_tool)
        self.remove_tool_button.clicked.connect(self._remove_tool)
        self._refresh_tools()
        tabs.addTab(page, "External tools")

    def _browse_directory(self, editor: QLineEdit, title: str) -> None:
        chosen = QFileDialog.getExistingDirectory(self, title, editor.text())

        if chosen:
            editor.setText(chosen)

    @Slot()
    def _add_tool(self) -> None:
        name = self.tool_name_edit.text().strip()

        if not name:
            self.error_label.setText("Enter a tool name before choosing its executable.")
            return

        # An explicit picker is the only source of new executable paths. Merely
        # opening Settings neither searches PATH nor starts a tool-specific probe.
        selected, _filter = QFileDialog.getOpenFileName(
            self,
            "Choose executable",
            self._external_tools.get(name, ""),
            "Executables (*.exe);;All files (*)",
        )

        if selected:
            self._external_tools[name] = selected
            self.error_label.clear()
            self._refresh_tools()

    @Slot()
    def _remove_tool(self) -> None:
        selected = self.external_tools_table.selectionModel().selectedRows()

        if not selected:
            return

        selected_item = self.external_tools_table.item(selected[0].row(), 0)

        if selected_item is None:
            return

        name = selected_item.text()
        del self._external_tools[name]
        self._refresh_tools()

    def _refresh_tools(self) -> None:
        self.external_tools_table.setRowCount(len(self._external_tools))

        for row, (name, path) in enumerate(sorted(self._external_tools.items())):
            self.external_tools_table.setItem(row, 0, QTableWidgetItem(name))
            self.external_tools_table.setItem(row, 1, QTableWidgetItem(path))

    def settings(self) -> AppSettings:
        """Return validated staged preferences; persistence belongs to the caller."""
        template = self.template_edit.text()
        parse_template(template)
        backup_directory = self.backup_directory_edit.text()

        if self.backup_enabled.isChecked() and not backup_directory.strip():
            raise ValueError("Choose a backup directory when permanent backups are enabled.")

        language = self.preferred_language_edit.text()

        if not language.strip():
            raise ValueError("Enter a preferred language, or auto for automatic selection.")

        network = self.network_settings()
        validate_network_settings(network)

        # Start from the complete original snapshot so tabs that do not edit a
        # setting cannot reset it; secret credentials have their own RAM-only path.
        return replace(
            self._original,
            rename=RenameSettings(
                self.rename_enabled.isChecked(), template,
                self.track_digits_spin.value(), self.disc_digits_spin.value(),
            ),
            matching=MatchingSettings(language),
            providers=ProvidersSettings(self.provider_combo.currentData()),
            network=network,
            backup=BackupSettings(self.backup_enabled.isChecked(), backup_directory),
            reports=ReportsSettings(self.reports_enabled.isChecked(), self.reports_directory_edit.text()),
            diagnostics=DiagnosticsSettings(self.detailed_trace_check.isChecked()),
            external_tools=self._external_tools,
        )

    def accept(self) -> None:
        """Keep invalid settings available for correction without saving anything."""
        try:
            self.settings()
        except (TypeError, ValueError) as error:
            self.error_label.setText(str(error))
            return

        self.error_label.clear()
        self._clear_credential_edits()
        super().accept()
