"""Construct the Qt application shell without embedding business logic."""

from collections.abc import Sequence
from pathlib import Path

from PySide6.QtWidgets import QApplication

from metadata_polisher.application.apply import ApplyBatchResult, ApplyService, LocalApplyPreflightInspector
from metadata_polisher.application.lookup import GroupLookupResult, LookupService
from metadata_polisher.application.provider_connection import ProviderConnectionResult
from metadata_polisher.application.scanning import ScanLibraryResult, ScanLibraryService
from metadata_polisher.execution.executor import ProcessingExecutor, SerialBackgroundExecutor
from metadata_polisher.formats.registry import FormatRegistry
from metadata_polisher.infrastructure.diagnostics import OperationIdSource
from metadata_polisher.infrastructure.paths import ApplicationPaths
from metadata_polisher.infrastructure.reporting import ProcessingReportWriter
from metadata_polisher.infrastructure.settings import RenameSettings
from metadata_polisher.infrastructure.transaction import TransactionalFileWriter
from metadata_polisher.session.state import (
    OperationKind,
    ResultApplicationStatus,
    SessionState,
    StaleResultReason,
    StateApplicationResult,
    apply_batch_result,
    apply_group_lookup_result,
    apply_scan_library_result,
)
from metadata_polisher.ui.apply_controller import ApplyController
from metadata_polisher.ui.diagnostics_controller import DiagnosticsController
from metadata_polisher.ui.interaction_controller import InteractionController
from metadata_polisher.ui.layout import LayoutPreferences
from metadata_polisher.ui.library_controller import LibraryController
from metadata_polisher.ui.lookup_controller import LookupController
from metadata_polisher.ui.main_window import MainWindow
from metadata_polisher.ui.operation_controller import (
    OperationController,
    ResultReducerBinding,
)
from metadata_polisher.ui.qt_bridge import QtOperationBridge
from metadata_polisher.ui.review_controller import ReviewController
from metadata_polisher.ui.settings_controller import SettingsController


def _reduce_scan_result(
    state: SessionState,
    result: object,
) -> StateApplicationResult:
    if not isinstance(result, ScanLibraryResult):
        raise TypeError("scan reducer requires ScanLibraryResult")

    return apply_scan_library_result(state, result)


def _reduce_lookup_result(
    state: SessionState,
    result: object,
    rename_settings: RenameSettings,
) -> StateApplicationResult:
    if not isinstance(result, GroupLookupResult):
        raise TypeError("lookup reducer requires GroupLookupResult")

    return apply_group_lookup_result(state, result, rename_settings)


def _reduce_apply_result(
    state: SessionState,
    result: object,
) -> StateApplicationResult:
    if not isinstance(result, ApplyBatchResult):
        raise TypeError("Apply reducer requires ApplyBatchResult")

    return apply_batch_result(state, result)


def _reduce_provider_test(state: SessionState, result: object) -> StateApplicationResult:
    """Connection tests retain library state and reject a superseded operation."""
    if not isinstance(result, ProviderConnectionResult):
        raise TypeError("provider-test reducer requires ProviderConnectionResult")

    active = state.active_operation
    current = (active is not None and active.operation_id == result.operation_id
               and active.kind is OperationKind.PROVIDER_TEST
               and active.base_session_revision == state.revision
               and active.base_library_revision == state.library_revision)
    return StateApplicationResult(
        state, ResultApplicationStatus.APPLIED if current else ResultApplicationStatus.STALE,
        None if current else StaleResultReason.OPERATION_MISMATCH,
    )


def create_application(
    argv: Sequence[str],
    *,
    executor: ProcessingExecutor | None = None,
    settings_file: Path | None = None,
    scanner: ScanLibraryService | None = None,
    lookup_service: LookupService | None = None,
    apply_service: ApplyService | None = None,
    operation_ids: OperationIdSource | None = None,
) -> tuple[QApplication, MainWindow]:
    """Return the process application and the native review window."""
    # Qt permits one application object per process. Reuse the GUI instance in
    # embedded/test callers, but reject a core-only instance lacking GUI support.
    existing_application = QApplication.instance()

    if existing_application is None:
        application = QApplication(list(argv))
    elif isinstance(existing_application, QApplication):
        application = existing_application
    else:
        raise RuntimeError("A non-GUI Qt application already exists")

    window = MainWindow(operation_ids)
    executor = executor if executor is not None else SerialBackgroundExecutor()
    # Workers return typed results through queued Qt signals; the controller
    # applies them to the latest session on the UI thread using these reducers.
    bridge = QtOperationBridge(executor, window)
    controller = OperationController(
        bridge=bridge,
        get_state=lambda: window.session_state,
        set_state=window.set_session_state,
        result_reducers=(
            ResultReducerBinding(ScanLibraryResult, _reduce_scan_result),
            ResultReducerBinding(GroupLookupResult, lambda state, result: _reduce_lookup_result(
                state, result, window.library_controller.settings.rename
                if window.library_controller is not None else RenameSettings(),
            )),
            ResultReducerBinding(ApplyBatchResult, _reduce_apply_result),
            ResultReducerBinding(ProviderConnectionResult, _reduce_provider_test),
        ),
        conflicting_controls=(
            window.root_path_edit,
            window.browse_button,
            window.rescan_button,
            window.find_selected_button,
            window.find_all_incomplete_button,
            window.apply_selected_button,
            window.review_apply_button,
            window.apply_all_button,
            window.settings_button,
            window.split_group_button,
            window.merge_groups_button,
            window.disc_override_button,
            window.choose_candidate_button,
            window.track_mapping_button,
            window.edit_search_button,
            window.language_combo,
            window.proposal_combo,
            window.keep_existing_button,
            window.use_proposed_button,
            window.manual_value_button,
            window.clear_value_button,
            window.accept_safe_additions_button,
            window.keep_filename_button,
            window.apply_rename_button,
            window.undo_review_button,
            window.include_selected_button,
            window.exclude_selected_button,
            window.include_review_scope_button,
            window.select_all_files_button,
            window.clear_file_selection_button,
            window.review_scope_combo,
        ),
        stage_label=window.operation_stage_label,
        progress_bar=window.operation_progress_bar,
        parent=window,
    )
    # Retain the runtime for the window's lifetime before attaching actions that
    # can submit work. Shared services and injected test doubles are wired here.
    window.retain_operation_runtime(executor, bridge, controller)
    window.library_controller = LibraryController(
        window,
        scanner if scanner is not None else ScanLibraryService(FormatRegistry()),
        settings_file if settings_file is not None else ApplicationPaths.detect().settings_file,
    )
    window.lookup_controller = LookupController(window, lookup_service)
    window.review_controller = ReviewController(window)
    app_dir = settings_file.parent if settings_file is not None else ApplicationPaths.detect().app_dir
    window.apply_controller = ApplyController(window, apply_service if apply_service is not None else ApplyService(
        preflight=LocalApplyPreflightInspector(FormatRegistry()),
        writer=TransactionalFileWriter(),
        report_writer=ProcessingReportWriter(app_dir / "reports"),
    ))
    window.settings_controller = SettingsController(
        window, settings_file if settings_file is not None else app_dir / "settings.json",
    )
    window.diagnostics_controller = DiagnosticsController(window, app_dir)
    InteractionController(window)
    LayoutPreferences(window, settings_file if settings_file is not None else app_dir / "settings.json")

    def shut_down_executor() -> None:
        # Cancellation is cooperative: a writer reaches its safe file boundary
        # before the executor joins and provider connections are released.
        controller.cancel_active()
        executor.shutdown(wait=True)

        if window.lookup_controller is not None:
            window.lookup_controller.close()

    application.aboutToQuit.connect(shut_down_executor)

    return application, window
