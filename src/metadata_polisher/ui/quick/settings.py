"""Stage QML preferences and publish them only after a successful atomic save."""

from dataclasses import replace
from typing import TYPE_CHECKING, cast

from PySide6.QtCore import Property, QObject, QUrl, Signal, Slot

from metadata_polisher.application.provider_connection import ProviderConnectionResult, test_provider_connection
from metadata_polisher.execution.cancellation import CancellationToken
from metadata_polisher.execution.events import OperationEventSink
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
    save_settings,
)
from metadata_polisher.providers.base import RequestContext
from metadata_polisher.providers.catalogue import provider_label, provider_summary
from metadata_polisher.providers.network import describe_network_route, validate_network_settings
from metadata_polisher.rename.completion import template_completion
from metadata_polisher.rename.template import FilenameRenderPolicy, TemplateField, parse_template
from metadata_polisher.session.lookup_editing import refresh_inherited_language
from metadata_polisher.session.review_editing import refresh_rename_previews
from metadata_polisher.session.state import OperationKind, SessionState

if TYPE_CHECKING:
    from metadata_polisher.ui.quick.backend import QuickBackend
    from metadata_polisher.ui.quick.lookup import QuickLookup


# Keep the complete workflow reference alongside the settings facade so the
# help window and preferences describe the same review and writing boundaries.
HELP_TEXT = """
<h2>Review first, write when ready</h2>
<ol>
<li>Choose a folder and an album, then highlight the tracks to review.</li>
<li>Open Metadata review. Select a field; double-click its Final value
or press F2 to enter a correction. Show full values opens copyable details.</li>
<li>Keep existing preserves each file's value. Use proposed uses each
file's own suggestion. Edit deliberately shares your entry across selected fields.</li>
<li>Clear removes the selected fields. Accept safe suggestions only fills
missing values with an unambiguous, high-confidence supported suggestion.</li>
<li>Review filename suggestions separately. Use Add selected files for the
files you want to change, then Review &amp; Apply and inspect the final summary.</li>
</ol>
<p><b>Only Apply changes in the final confirmation writes files.</b>
Highlighting, reviewing metadata and choosing filenames do not add files
to Changes to apply. Undo review changes pending decisions, not completed writes.</p>
<h3>Review scope</h3>
<p>Selected files means the highlighted tracks. Current group, Included files
and All library files can contain tracks outside the visible table.
Check the file and field counts before a batch action.</p>
<h3>Table order</h3>
<p>Click an album, file or secondary-table header to sort; click again to
reverse direction. Files start in Disc, Track, then File order, using numeric
positions. A track without a disc number uses disc 1 for ordering; files without
either number follow in filename order. Right-click a file header and choose
Disc / track order to restore the default. Selection and inclusion stay with
their files. Metadata review fields keep their fixed order.</p>
<h3>Keyboard reference</h3>
<p>Ctrl+O: choose folder<br>F5 or Enter in folder: rescan<br>
Ctrl+A in tracks: select all displayed tracks<br>Ctrl+Shift+A: clear highlighting<br>
Enter on a track or Ctrl+E: open review<br>F2 on a field: manual value<br>
Alt+Left / Alt+Right: previous / next track<br>Ctrl+Z in review: undo review<br>
Ctrl+L: find metadata for selected albums<br>Ctrl+Enter: Review &amp; Apply<br>
Escape: close the current review/dialogue<br>F1: open this help</p>
<p>Use Settings to select a provider or local editing only. “Use Settings”
inherits your language preference; Auto is an explicit automatic choice.</p>
"""


class QuickSettings(QObject):
    """Own dialogue drafts while the host retains authoritative session state."""

    changed = Signal()
    settings_changed = Signal(object)

    def __init__(self, host: QuickBackend) -> None:
        super().__init__(host)
        self._host = host
        self._opened = False
        self._original = host.app_settings
        self._expected_state = host.session_state
        self._file_snapshot: bytes | None = None
        self._draft: dict[str, str | int | bool | None] = {}
        self._tools: dict[str, str] = {}
        self._username = ""
        self._password = ""
        self._credential_snapshot = CredentialSnapshot(0, None)
        self._error = ""
        self._field_errors: dict[str, str] = {}
        self._test_status = "Not tested in this dialogue."
        self._test_operation: str | None = None
        self._test_provider: str | None = None
        self._load_draft(self._original)

        # The host installs its terminal reducers first. Our completion callback
        # consequently captures the settled state, permitting Save after a test.
        host.bridge.completed.connect(self._test_completed)
        host.bridge.cancelled.connect(self._test_cancelled)
        host.bridge.failed.connect(self._test_failed)

    def _load_draft(self, settings: AppSettings) -> None:
        self._draft = {
            "renameEnabled": settings.rename.enabled,
            "template": settings.rename.template,
            "trackDigits": settings.rename.minimum_track_digits,
            "discDigits": settings.rename.minimum_disc_digits,
            "preferredLanguage": settings.matching.preferred_language,
            "providerId": settings.providers.selected_provider_id,
            "networkMode": settings.network.mode,
            "proxyHost": settings.network.proxy_host,
            "proxyPort": settings.network.proxy_port,
            "backupEnabled": settings.backup.enabled,
            "backupDirectory": settings.backup.directory,
            "reportsEnabled": settings.reports.enabled,
            "reportsDirectory": settings.reports.directory,
            "detailedTracing": settings.diagnostics.detailed_tracing,
        }
        self._tools = dict(settings.external_tools)

    def _provider_options(self) -> list[dict[str, str | None]]:
        options = [{"id": item, "label": provider_label(item)} for item in ("musicbrainz_direct", "vgmdb", None)]
        selected = cast(str | None, self._draft["providerId"])

        if selected not in {"musicbrainz_direct", "vgmdb", None}:
            options.append({"id": selected, "label": "Unavailable stored provider"})

        return options

    def _route_options(self) -> list[dict[str, str]]:
        options = [{"id": "direct", "label": "Direct"}, {"id": "manual_proxy", "label": "Manual HTTP proxy"}]
        selected = str(self._draft["networkMode"])

        if selected not in {"direct", "manual_proxy"}:
            options.append({"id": selected, "label": "Unavailable stored route"})

        return options

    def _credentials_status(self) -> str:
        return (
            "Proxy credentials are configured for this session."
            if self._credential_snapshot.proxy_auth is not None
            else "No proxy credentials are configured for this session."
        )

    # Settings edits remain a detached draft until Save validates and publishes
    # them. QML receives a copy of the map, never the mutable backing dictionary.
    opened = Property(bool, lambda self: self._opened, notify=changed)
    # Qt requires the registered QVariantMap name for a native JavaScript map;
    # its Python annotation does not include this supported string overload.
    draft = Property("QVariantMap", lambda self: dict(self._draft), notify=changed)  # type: ignore[arg-type]
    tools = Property(
        list, lambda self: [{"name": name, "path": path} for name, path in sorted(self._tools.items())], notify=changed
    )
    error = Property(str, lambda self: self._error, notify=changed)
    fieldErrors = Property("QVariantMap", lambda self: dict(self._field_errors), notify=changed)  # type: ignore[arg-type]
    providerOptions = Property(list, _provider_options, notify=changed)
    routeOptions = Property(list, _route_options, notify=changed)
    providerSummary = Property(str, lambda self: provider_summary(self._draft["providerId"]), notify=changed)
    routeDescription = Property(str, lambda self: describe_network_route(self._network()), notify=changed)

    # Credentials and connection-test receipts have a separate session lifetime
    # from persistent preferences. Cancelling Settings must not revoke a login.
    username = Property(str, lambda self: self._username, notify=changed)
    password = Property(str, lambda self: self._password, notify=changed)
    credentialsStatus = Property(str, _credentials_status, notify=changed)
    testStatus = Property(str, lambda self: self._test_status, notify=changed)
    testRunning = Property(bool, lambda self: self._test_operation is not None, notify=changed)
    testEnabled = Property(
        bool,
        lambda self: self._test_operation is None and self._draft["providerId"] in {"musicbrainz_direct", "vgmdb"},
        notify=changed,
    )

    def _disk_snapshot(self) -> bytes | None:
        path = self._host.settings_file

        if path is None:
            return None

        try:
            return path.read_bytes()
        except FileNotFoundError:
            return None

    @Slot(result=bool)
    def open(self) -> bool:
        if self._opened:
            return True

        if self._host.session_state.active_operation is not None:
            return False

        try:
            self._file_snapshot = self._disk_snapshot()
        except OSError:
            self._host.set_status("Settings could not be opened because the settings file could not be read.")
            return False

        self._original = self._host.app_settings
        self._expected_state = self._host.session_state
        self._load_draft(self._original)
        self._install_credentials(self._host.credentials.snapshot())
        self._error = ""
        self._field_errors = {}
        self._opened = True
        self.changed.emit()
        return True

    def _can_change(self) -> bool:
        return self._opened and self._test_operation is None

    @Slot(str, "QVariant")
    def setField(self, name: str, value: object) -> None:
        if not self._can_change() or name not in self._draft:
            return

        expected = type(self._draft[name])

        valid = value is None or isinstance(value, str) if name == "providerId" else type(value) is expected

        if not valid:
            return

        self._draft[name] = cast(str | int | bool | None, value)

        # Editing a field retires only its own validation result. Conditional
        # controls also recheck their dependent fields, so disabled preferences
        # cannot retain a blocker that no longer applies. Untouched fields wait
        # for focus loss or Save, and a disk/session failure remains visible.
        related = {
            "backupEnabled": ("backupDirectory",),
            "networkMode": ("networkMode", "proxyHost", "proxyPort"),
        }.get(name, (name,))
        visible_errors = set(related) & self._field_errors.keys()

        if visible_errors:
            errors = self._collect_field_errors()

            for field in visible_errors:
                if field in errors:
                    self._field_errors[field] = errors[field]
                else:
                    self._field_errors.pop(field, None)

        if name in {"providerId", "networkMode", "proxyHost", "proxyPort"}:
            self._test_status = "Not tested with this route and credentials."

        self.changed.emit()

    @Slot(str)
    def validateField(self, name: str) -> None:
        if not self._can_change() or name not in self._draft:
            return

        errors = self._collect_field_errors()

        if name in errors:
            self._field_errors[name] = errors[name]
        else:
            self._field_errors.pop(name, None)

        self.changed.emit()

    @Slot(str, str)
    def setCredentials(self, username: str, password: str) -> None:
        if self._can_change():
            self._username, self._password = username, password
            self._test_status = "Not tested with this route and credentials."
            self.changed.emit()

    @Slot(str, str)
    def addTool(self, name: str, path: str) -> None:
        if not self._can_change():
            return

        if not name.strip():
            self._field_errors["toolName"] = "Enter a tool name before choosing its executable."
        else:
            self._field_errors.pop("toolName", None)

            # The frontend supplies paths only after an explicit file-picker
            # choice; opening Settings never discovers or executes programs.
            if path:
                self._tools[name.strip()] = path

        self.changed.emit()

    @Slot(str)
    def removeTool(self, name: str) -> None:
        if self._can_change():
            self._tools.pop(name, None)
            self.changed.emit()

    def _network(self) -> NetworkSettings:
        return NetworkSettings(
            str(self._draft["networkMode"]), str(self._draft["proxyHost"]), cast(int, self._draft["proxyPort"])
        )

    def _collect_field_errors(self) -> dict[str, str]:
        """Collect independent problems through the authoritative validators."""
        errors: dict[str, str] = {}

        try:
            parse_template(str(self._draft["template"]))
        except (TypeError, ValueError) as error:
            errors["template"] = str(error)

        # Validate each padding field with a valid opposite field. Otherwise
        # the first exception would hide another error in the same submission.
        for field, track in (("trackDigits", True), ("discDigits", False)):
            value = cast(int, self._draft[field])

            try:
                FilenameRenderPolicy(value if track else 1, 1 if track else value)
            except (TypeError, ValueError) as error:
                errors[field] = str(error)

        if not str(self._draft["preferredLanguage"]).strip():
            errors["preferredLanguage"] = "Enter a preferred language, or auto for automatic selection."

        network = self._network()

        if network.mode == "manual_proxy":
            # Keep the provider validator as the single source of route rules.
            # Known-valid counterpart values let both host and port report their
            # own errors without parsing exception text or duplicating its rules.
            for field, probe in (
                ("proxyHost", replace(network, proxy_port=8080)),
                ("proxyPort", replace(network, proxy_host="localhost")),
            ):
                try:
                    validate_network_settings(probe)
                except (TypeError, ValueError) as error:
                    errors[field] = str(error)
        else:
            try:
                validate_network_settings(network)
            except (TypeError, ValueError) as error:
                errors["networkMode"] = str(error)

        if self._draft["backupEnabled"] and not str(self._draft["backupDirectory"]).strip():
            errors["backupDirectory"] = "Choose a backup directory when permanent backups are enabled."

        return errors

    def _settings(self) -> AppSettings:
        template = str(self._draft["template"])
        backup_directory = str(self._draft["backupDirectory"])
        language = str(self._draft["preferredLanguage"])
        network = self._network()

        return replace(
            self._original,
            rename=RenameSettings(
                bool(self._draft["renameEnabled"]),
                template,
                cast(int, self._draft["trackDigits"]),
                cast(int, self._draft["discDigits"]),
            ),
            matching=MatchingSettings(language),
            providers=ProvidersSettings(cast(str | None, self._draft["providerId"])),
            network=network,
            backup=BackupSettings(bool(self._draft["backupEnabled"]), backup_directory),
            reports=ReportsSettings(bool(self._draft["reportsEnabled"]), str(self._draft["reportsDirectory"])),
            diagnostics=DiagnosticsSettings(bool(self._draft["detailedTracing"])),
            external_tools=self._tools,
        )

    @Slot(result=bool)
    def save(self) -> bool:
        if not self._can_change() or self._host.session_state.active_operation is not None:
            return False

        try:
            if self._host.session_state is not self._expected_state or self._host.app_settings != self._original:
                raise ValueError("The session changed while Settings was open. Open Settings again.")

            if self._disk_snapshot() != self._file_snapshot:
                raise ValueError("The settings file changed while Settings was open. Open Settings again.")

            self._error = ""
            self._field_errors = self._collect_field_errors()

            if self._field_errors:
                self.changed.emit()
                return False

            settings = self._settings()
            updated = self._host.session_state

            # Prepare pure previews before persistence. Neither field decisions
            # nor inherited-language changes reach the live session on failure.
            if settings.rename != self._original.rename:
                updated = refresh_rename_previews(updated, settings.rename)

            if settings.matching.preferred_language != self._original.matching.preferred_language:
                updated = refresh_inherited_language(updated, settings.matching.preferred_language, settings.rename)

            if self._host.settings_file is not None:
                save_settings(self._host.settings_file, settings)
        except (OSError, TypeError, ValueError) as error:
            self._error = f"Settings could not be saved: {error}"
            self.changed.emit()
            return False

        self._host.app_settings = settings
        self._host.set_state(updated)
        self._host.set_status("Settings saved.")
        self.settings_changed.emit(settings)
        self._close()
        return True

    def _close(self) -> None:
        self._opened = False
        self._username = self._password = ""
        self._credential_snapshot = CredentialSnapshot(0, None)
        self._error = ""
        self._field_errors = {}
        self.changed.emit()

    @Slot(result=bool)
    def reject(self) -> bool:
        if self._test_operation is not None:
            self.cancelTest()
            return False

        self._close()
        return True

    def _install_credentials(self, snapshot: CredentialSnapshot) -> None:
        self._credential_snapshot = snapshot
        self._username, self._password = snapshot.proxy_auth or ("", "")
        self._test_status = "Not tested with this route and credentials."

    def _lookup(self) -> QuickLookup:
        # PySide's descriptor annotation describes Property itself rather than
        # the QObject returned when the descriptor is accessed on the host.
        return cast("QuickLookup", self._host.lookupUi)

    @Slot()
    def saveSessionLogin(self) -> None:
        if self._can_change():
            self._host.credentials.set_proxy(self._username, self._password)
            self._lookup().invalidate_session_credentials()
            self._install_credentials(self._host.credentials.snapshot())
            self.changed.emit()

    @Slot()
    def forgetSessionLogin(self) -> None:
        if self._can_change():
            self._host.credentials.forget()
            self._lookup().invalidate_session_credentials()
            self._install_credentials(self._host.credentials.snapshot())
            self.changed.emit()

    @Slot(result=bool)
    def testProvider(self) -> bool:
        selected = self._draft["providerId"]

        if (
            not self._can_change()
            or self._host.session_state.active_operation is not None
            or selected not in {"musicbrainz_direct", "vgmdb"}
        ):
            return False

        auth = (self._username, self._password) if self._username or self._password else None
        credentials = CredentialSnapshot(self._credential_snapshot.generation, auth)

        try:
            network = self._network()
            validate_network_settings(network)
            service = self._lookup().connection_service(
                ProvidersSettings(selected),
                network=network,
                credentials=credentials,
            )
        except (TypeError, ValueError) as error:
            self._test_status = str(error)
            self.changed.emit()
            return False

        if service is None:
            self._test_status = str(self._host.status) or "The provider test was not started."
            self.changed.emit()
            return False

        operation_id = self._host._ids.next_id("PROVIDER_TEST")
        context = RequestContext(operation_id, "auto", credential_generation=credentials.generation)

        def work(token: CancellationToken, events: OperationEventSink) -> ProviderConnectionResult:
            # The worker captures service and immutable request data, never
            # reading QML drafts or the host's mutable session from its thread.
            return test_provider_connection(service, selected, context, token, events)

        def reducer(state: SessionState, result: object) -> SessionState:
            if not isinstance(result, ProviderConnectionResult) or result.operation_id != operation_id:
                raise ValueError("The provider test returned an invalid result.")

            return state

        self._test_operation, self._test_provider = operation_id, selected
        self._test_status = f"Testing {provider_label(selected)}…"
        self.changed.emit()
        started = self._host.submit_operation(operation_id, OperationKind.PROVIDER_TEST, (), work, reducer)

        if not started:
            self._finish_test(operation_id, "The provider test could not be started.")

        return started

    @Slot()
    def cancelTest(self) -> None:
        if self._test_operation is not None:
            self._host.cancelScan()
            self._test_status = "Cancelling test; waiting for the bounded request to finish…"
            self.changed.emit()

    @Slot(str, object)
    def _test_completed(self, operation_id: str, result: object) -> None:
        if (
            not isinstance(result, ProviderConnectionResult)
            or result.operation_id != operation_id
            or result.provider_id != self._test_provider
        ):
            self._finish_test(operation_id, "Provider test failed; no successful connection was established.")
            return

        message = (
            f"{provider_label(result.provider_id)} connection test passed; "
            "the adapter returned a valid catalogue response."
            if result.issue is None
            else f"{result.issue.code.value}: {result.issue.message}"
        )
        self._finish_test(operation_id, message)

    @Slot(str)
    def _test_cancelled(self, operation_id: str) -> None:
        self._finish_test(operation_id, "Provider test cancelled.")

    @Slot(str, object)
    def _test_failed(self, operation_id: str, _error: object) -> None:
        self._finish_test(operation_id, "Provider test failed; no successful connection was established.")

    def _finish_test(self, operation_id: str, message: str) -> None:
        if operation_id != self._test_operation:
            return

        self._test_operation = self._test_provider = None
        self._expected_state = self._host.session_state
        self._lookup().finish_connection_test()
        self._test_status = message
        self.changed.emit()

    @Slot(QUrl, result=str)
    def pathFromUrl(self, url: QUrl) -> str:
        return url.toLocalFile()

    @Slot(str, int, result="QVariantMap")
    def templateCompletion(self, text: str, cursor: int) -> dict[str, object]:
        # QML cursors count UTF-16 units. Convert both directions so characters
        # such as emoji before a field cannot shift the replacement boundary.
        prefix = text.encode("utf-16-le")[: max(0, cursor) * 2].decode("utf-16-le", errors="ignore")
        completion = template_completion(text, len(prefix))

        if completion is None:
            return {}

        options = [
            f"%{field.value}%"
            for field in TemplateField
            if f"%{field.value}%".lower().startswith(completion.prefix.lower())
        ]
        return {
            "start": len(text[: completion.start].encode("utf-16-le")) // 2,
            "end": len(text[: completion.end].encode("utf-16-le")) // 2,
            "options": options,
        }
