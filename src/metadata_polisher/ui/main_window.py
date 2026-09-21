"""Native album navigation and files with a separate metadata review window."""

from typing import TYPE_CHECKING

from PySide6.QtCore import QItemSelectionModel, QModelIndex, Qt, Signal, Slot
from PySide6.QtGui import QCloseEvent, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTableView,
    QToolButton,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from metadata_polisher.domain.media import LocalMediaFile
from metadata_polisher.domain.metadata import MetadataField
from metadata_polisher.execution.executor import ProcessingExecutor
from metadata_polisher.infrastructure.diagnostics import OperationIdSource, ThreadSafeOperationIds
from metadata_polisher.session.lookup_editing import (
    available_language_choices,
    incomplete_searchable_group_ids,
    is_group_searchable,
)
from metadata_polisher.session.state import GroupSelection, GroupState, SessionState, UnsupportedSelection
from metadata_polisher.ui.dialogs.metadata_review_window import MetadataReviewWindow
from metadata_polisher.ui.dialogs.workflow_help_dialog import WorkflowHelpDialog
from metadata_polisher.ui.layout import ElidedLabel, StatusLabel
from metadata_polisher.ui.models import FileTableModel, GroupListModel, MetadataDiffModel
from metadata_polisher.ui.models.group_model import (
    GroupPresentationStatus,
    classify_group,
)
from metadata_polisher.ui.operation_controller import OperationController
from metadata_polisher.ui.pending_work import confirm_discard_pending
from metadata_polisher.ui.qt_bridge import QtOperationBridge

if TYPE_CHECKING:
    from metadata_polisher.ui.apply_controller import ApplyController
    from metadata_polisher.ui.diagnostics_controller import DiagnosticsController
    from metadata_polisher.ui.library_controller import LibraryController
    from metadata_polisher.ui.lookup_controller import LookupController
    from metadata_polisher.ui.review_controller import ReviewController
    from metadata_polisher.ui.settings_controller import SettingsController


class MainWindow(QMainWindow):
    """Own Qt presentation state while domain state remains immutable."""

    group_selection_requested = Signal(str)
    unsupported_selection_requested = Signal()
    review_context_changed = Signal()

    def __init__(self, operation_ids: OperationIdSource | None = None) -> None:
        super().__init__()
        self.setWindowTitle("Metadata Polisher")

        self._session_state = SessionState(root=None)
        self.operation_ids = operation_ids if operation_ids is not None else ThreadSafeOperationIds()
        self._visible_group_id: str | None = None
        self._displayed_group: GroupState | None = None
        self._projecting_review = False
        self._restoring_file_selection = False
        # Write inclusion survives ordinary navigation independently of Qt's
        # highlighted rows; highlighting alone never adds or removes write members.
        self.included_file_ids: frozenset[str] = frozenset()
        self.processing_executor: ProcessingExecutor | None = None
        self.operation_bridge: QtOperationBridge | None = None
        self.operation_controller: OperationController | None = None
        self.library_controller: LibraryController | None = None
        self.lookup_controller: LookupController | None = None
        self.review_controller: ReviewController | None = None
        self.apply_controller: ApplyController | None = None
        self.settings_controller: SettingsController | None = None
        self.diagnostics_controller: DiagnosticsController | None = None
        self.help_dialog: WorkflowHelpDialog | None = None
        self.group_model = GroupListModel(self._session_state, self)
        self.file_model = FileTableModel(parent=self)
        self.diff_model = MetadataDiffModel(parent=self)

        self._build_widgets()
        self._build_layout()
        self._connect_navigation()
        self.reset_layout()

    @property
    def session_state(self) -> SessionState:
        """Return the current immutable state installed by the UI controller."""
        return self._session_state

    def set_session_state(self, state: SessionState) -> None:
        """Replace every model projection from one coherent state snapshot."""
        if not isinstance(state, SessionState):
            raise TypeError("state must be a SessionState")

        previous = self._session_state
        self._session_state = state
        # A rescan/regroup can remove sources. Discard orphaned checkbox IDs so
        # they cannot silently refer to files outside the current library.
        known_ids = {source.file_id for group in state.groups for source in group.group.files}
        self.included_file_ids = self.included_file_ids & known_ids
        self.file_model.set_included_file_ids(self.included_file_ids)

        if previous.groups != state.groups or previous.unsupported_files != state.unsupported_files:
            # Navigation changes only the selected ID. Resetting a model for
            # that change would discard the other groups in a Ctrl selection.
            self.group_view.selectionModel().blockSignals(True)
            self.group_model.set_session_state(state)
            self.group_view.selectionModel().blockSignals(False)
        self._refresh_group_actions()
        if state.root != previous.root:
            # A busy or failed operation still refers to the last completed
            # scan. Preserve the requested folder so Rescan can retry it.
            self.root_path_edit.setText(str(state.root) if state.root is not None else "")
        ready_count = sum(
            classify_group(group) is GroupPresentationStatus.COMPLETE
            for group in state.groups
        )
        review_count = len(state.groups) - ready_count
        self.summary_counts_label.setText(
            f"{ready_count} ready · {review_count} review · "
            f"{len(state.unsupported_files)} unsupported"
        )

        selected_group_id: str | UnsupportedSelection | None = (
            state.selection.group_id
            if isinstance(state.selection, GroupSelection)
            else state.selection
        )
        selected_row = next(
            (
                row
                for row in range(self.group_model.rowCount())
                if self.group_model.data(
                    self.group_model.index(row, 0),
                    Qt.ItemDataRole.UserRole,
                )
                == selected_group_id
            ),
            None,
        )

        if selected_row is None:
            self.group_view.clearSelection()
            self.group_view.setCurrentIndex(QModelIndex())
            self._show_group(None)

            return

        # A model reset may preserve an equal QModelIndex without emitting
        # currentChanged. Refresh by stable ID before asking the view to select it.
        self._show_selection(selected_group_id)
        selected_index = self.group_model.index(selected_row, 0)

        if self.group_view.currentIndex() != selected_index:
            self.group_view.setCurrentIndex(selected_index)

        self.review_context_changed.emit()

    def retain_operation_runtime(
        self,
        executor: ProcessingExecutor,
        bridge: QtOperationBridge,
        controller: OperationController,
    ) -> None:
        """Retain composed runtime objects whose Qt connections must stay alive."""
        self.processing_executor = executor
        self.operation_bridge = bridge
        self.operation_controller = controller

    def _build_widgets(self) -> None:
        self.root_path_edit = QLineEdit(self)
        self.root_path_edit.setObjectName("rootPathEdit")
        self.root_path_edit.setPlaceholderText("Choose a music library folder")

        self.browse_button = self._button("Browse", "browseButton")
        self.rescan_button = self._button("Rescan", "rescanButton")
        self.find_selected_button = self._button(
            "Find Metadata for Selected",
            "findSelectedButton",
        )
        self.find_all_incomplete_button = self._button(
            "Find All Incomplete",
            "findAllIncompleteButton",
        )
        self.apply_selected_button = self._button("Review changes…", "applySelectedButton")
        self.apply_all_button = self._button("Apply All", "applyAllButton")
        self.apply_results_button = self._button("Apply results…", "applyResultsButton")
        self.include_selected_button = self._button("Add to write batch", "includeSelectedButton")
        self.exclude_selected_button = self._button("Remove from batch", "excludeSelectedButton")
        self.select_all_files_button = self._button("Select all", "selectAllFilesButton")
        self.select_all_files_button.setToolTip("Select all files in the displayed group (Ctrl+A)")
        self.clear_file_selection_button = self._button("Clear selection", "clearFileSelectionButton")
        self.clear_file_selection_button.setToolTip("Clear highlighted files (Ctrl+Shift+A)")
        self.open_review_button = self._button("Metadata review…", "openReviewButton")
        self.rename_files_button = self._button("Rename files…", "renameFilesButton")
        self.previous_file_button = self._button("Previous", "previousReviewFileButton")
        self.next_file_button = self._button("Next", "nextReviewFileButton")
        self.include_review_scope_button = self._button("Add scope to write batch", "includeReviewScopeButton")
        self.review_apply_button = self._button("Review changes…", "reviewApplyButton")
        self.review_inclusion_label = QLabel("0 included for writing", self)
        self.apply_guidance_label = QLabel(self)
        self.apply_guidance_label.setWordWrap(True)
        self.review_apply_guidance_label = QLabel(self)
        self.review_apply_guidance_label.setWordWrap(True)
        self.review_message_label = StatusLabel("", self)
        self.selection_scope_label = QLabel("0 highlighted · 0 included", self)
        self.selection_scope_label.setWordWrap(True)
        self.active_provider_label = QLabel("Lookup provider: MusicBrainz Direct", self)
        self.active_provider_label.setWordWrap(True)
        self.cancel_button = self._button("Cancel operation", "cancelButton")
        self.settings_button = self._button("Settings", "settingsButton")
        self.diagnostics_button = self._button("Diagnostics…", "diagnosticsButton")
        self.help_button = self._button("Help", "workflowHelpButton")
        self.help_button.setToolTip("Workflow, review scope and keyboard shortcuts (F1)")
        self.help_button.clicked.connect(self.open_workflow_help)
        self.help_button.hide()
        self.file_guidance_label = QLabel("Choose a folder to scan your music library.", self)
        self.file_guidance_label.setWordWrap(True)
        self.field_guidance_label = QLabel("Select a field, or double-click its Final value to edit.", self)
        self.field_guidance_label.setWordWrap(True)
        self.field_details_button = self._button("Full values ▸", "fieldDetailsButton")
        self.field_details_button.setCheckable(True)
        self.field_details_button.setToolTip("Read and copy full values without editing metadata")
        self.field_details = QPlainTextEdit(self)
        self.field_details.setReadOnly(True)
        self.field_details.setAccessibleName("Full metadata values and review details")
        self.field_details.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse | Qt.TextInteractionFlag.TextSelectableByKeyboard,
        )
        self.field_details.setMaximumHeight(6 * self.field_details.fontMetrics().lineSpacing() + 12)
        self.field_details.hide()
        self.field_details_button.toggled.connect(self._refresh_field_details)
        self.review_context_changed.connect(self._refresh_field_details)
        self.split_group_button = self._button("Split selected files", "splitGroupButton")
        self.merge_groups_button = self._button("Merge groups", "mergeGroupsButton")
        self.disc_override_button = self._button("Disc number…", "discOverrideButton")
        self.choose_candidate_button = self._button("Choose candidate…", "chooseCandidateButton")
        self.track_mapping_button = self._button("Map tracks…", "trackMappingButton")
        self.edit_search_button = self._button("Edit search terms…", "editSearchButton")
        self.language_combo = QComboBox(self)
        self.language_combo.setObjectName("languageCombo")
        self.language_combo.addItem("Auto", "auto")
        self.proposal_combo = QComboBox(self)
        self.proposal_combo.setObjectName("proposalCombo")
        self.keep_existing_button = self._button("Keep Existing", "keepExistingButton")
        self.use_proposed_button = self._button("Use Proposed", "useProposedButton")
        self.manual_value_button = self._button("Manual…", "manualValueButton")
        self.clear_value_button = self._button("Clear", "clearValueButton")
        self.accept_safe_additions_button = self._button("Accept Safe Additions", "acceptSafeAdditionsButton")
        self.keep_filename_button = self._button("Keep filename", "keepFilenameButton")
        self.apply_rename_button = self._button("Use proposed filename", "applyRenameButton")
        self.rename_previews_button = self._button("Filename previews…", "renamePreviewsButton")
        self.undo_review_button = self._button("Undo review", "undoReviewButton")
        self.review_scope_combo = QComboBox(self)
        self.review_scope_combo.addItem("Selected files", "selected")
        self.review_scope_combo.addItem("Current group", "group")
        self.review_scope_combo.addItem("Included files", "included")
        self.review_scope_combo.addItem("All library files", "library")
        self.review_scope_label = QLabel("Select files and fields to review.", self)
        self.review_scope_label.setWordWrap(True)
        self.rename_validation_label = QLabel("", self)
        self.rename_validation_label.setWordWrap(True)

        self.main_splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.main_splitter.setObjectName("mainSplitter")

        self.group_view = QTreeView(self.main_splitter)
        self.group_view.setObjectName("groupView")
        self.group_view.setModel(self.group_model)
        self.group_view.setRootIsDecorated(False)
        self.group_view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.group_view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)

        self.file_table_view = QTableView(self.main_splitter)
        self.file_table_view.setObjectName("fileTableView")
        self.file_table_view.setModel(self.file_model)
        self.file_table_view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.file_table_view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)

        self.diff_table_view = QTableView(self)
        self.diff_table_view.setObjectName("diffTableView")
        self.diff_table_view.setModel(self.diff_model)
        self.diff_table_view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.diff_table_view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)

        self.rename_current_label = ElidedLabel("Current filename: —", self)
        self.rename_current_label.setObjectName("renameCurrentLabel")
        self.rename_proposed_label = ElidedLabel("Proposed filename: —", self)
        self.rename_proposed_label.setObjectName("renameProposedLabel")
        self.rename_template_label = ElidedLabel(
            "Template: [%discnumber%.]%tracknumber%. %title%",
            self,
        )
        self.rename_template_label.setObjectName("renameTemplateLabel")

        self.summary_counts_label = QLabel("0 ready · 0 review · 0 unsupported", self)
        self.summary_counts_label.setObjectName("summaryCountsLabel")
        self.operation_stage_label = QLabel("Idle", self)
        self.operation_stage_label.setObjectName("operationStageLabel")
        self.operation_progress_bar = QProgressBar(self)
        self.operation_progress_bar.setObjectName("operationProgressBar")
        self.operation_progress_bar.setRange(0, 100)
        self.operation_progress_bar.setValue(0)
        self.operation_progress_bar.hide()
        self.apply_safety_label = QLabel(
            "Review decisions stay in memory. Only Apply changes in the final confirmation writes files.",
            self,
        )
        self.apply_safety_label.setObjectName("applySafetyLabel")
        self.apply_safety_label.setWordWrap(True)
        self.workflow_message_label = StatusLabel("", self)
        self.workflow_message_label.setObjectName("workflowMessageLabel")
        self.workflow_message_label.text_changed.connect(self.review_message_label.setText)

    def _build_layout(self) -> None:
        central = QWidget(self)
        root_layout = QVBoxLayout(central)

        root_controls = QHBoxLayout()
        root_controls.addWidget(self.root_path_edit, 1)
        root_controls.addWidget(self.browse_button)
        root_controls.addWidget(self.rescan_button)
        root_layout.addLayout(root_controls)

        actions = QHBoxLayout()
        actions.addWidget(self.find_selected_button)
        actions.addWidget(self.find_all_incomplete_button)
        actions.addStretch(1)
        self.apply_all_button.hide()
        actions.addWidget(self.settings_button)
        actions.addWidget(self.diagnostics_button)
        root_layout.addLayout(actions)
        root_layout.addWidget(self.active_provider_label)
        # Secondary commands retain their existing controller connections. Their
        # menu actions mirror each button's enabled state when the menu opens.
        secondary = QToolButton(self)
        secondary.setText("Group tools")
        secondary.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(secondary)
        button_actions = []

        for button in (self.split_group_button, self.merge_groups_button, self.disc_override_button,
                       self.choose_candidate_button, self.track_mapping_button, self.edit_search_button):
            action = menu.addAction(button.text())
            action.triggered.connect(button.click)
            button.hide()
            button_actions.append((button, action))

        menu.addSeparator()
        menu.addAction("Reset layout", self.reset_layout)
        menu.addAction("Help and shortcuts (F1)", self.open_workflow_help)
        def refresh_menu() -> None:
            for button, action in button_actions:
                action.setEnabled(button.isEnabled())

        menu.aboutToShow.connect(refresh_menu)
        secondary.setMenu(menu)
        actions.insertWidget(2, secondary)
        actions.insertWidget(3, QLabel("Language:", self))
        actions.insertWidget(4, self.language_combo)

        group_pane = self._pane("Albums / groups", self.group_view)
        file_pane = self._pane("Files / tracks", self.file_table_view)
        selection = QHBoxLayout()
        selection.addWidget(self.select_all_files_button)
        selection.addWidget(self.clear_file_selection_button)
        selection.addStretch(1)
        selection.addWidget(self.rename_files_button)
        selection.addWidget(self.open_review_button)
        inclusion = QHBoxLayout()
        inclusion.addWidget(self.selection_scope_label, 1)
        inclusion.addWidget(self.include_selected_button)
        inclusion.addWidget(self.exclude_selected_button)
        file_layout = file_pane.layout()
        assert isinstance(file_layout, QVBoxLayout)
        file_layout.insertLayout(1, selection)
        file_layout.insertWidget(2, self.file_guidance_label)
        file_layout.addLayout(inclusion)

        review_footer = QWidget(self)
        review_footer_layout = QVBoxLayout(review_footer)
        review_footer_layout.setContentsMargins(0, 0, 0, 0)
        review_footer_layout.addWidget(QLabel("Write batch", review_footer))
        review_safety = QLabel(self.apply_safety_label.text(), review_footer)
        review_safety.setWordWrap(True)
        review_footer_layout.addWidget(review_safety)
        review_footer_layout.addWidget(self.review_message_label)
        review_footer_layout.addWidget(self.review_apply_guidance_label)
        review_actions = QHBoxLayout()
        review_actions.addWidget(self.review_inclusion_label, 1)
        review_actions.addWidget(self.include_review_scope_button)
        review_actions.addWidget(self.review_apply_button)
        review_footer_layout.addLayout(review_actions)
        self.review_window = MetadataReviewWindow(self._diff_pane(), self.review_scope_combo, review_footer, self)
        self.review_scope_combo.setAccessibleName("Review scope")
        self.root_path_edit.setAccessibleName("Music folder")
        self.language_combo.setAccessibleName("Metadata language preference")

        self.main_splitter.insertWidget(0, group_pane)
        self.main_splitter.addWidget(file_pane)
        self.main_splitter.setStretchFactor(0, 0)
        self.main_splitter.setStretchFactor(1, 1)
        self.main_splitter.setChildrenCollapsible(False)
        self.main_splitter.setHandleWidth(7)
        self.main_splitter.setStyleSheet("QSplitter::handle { background: palette(midlight); }"
                                        "QSplitter::handle:hover { background: palette(mid); }")

        self.main_splitter.handle(1).setToolTip("Drag to resize Albums / groups and Files / tracks")
        root_layout.addWidget(self.main_splitter, 1)

        progress = QHBoxLayout()
        progress.addWidget(self.summary_counts_label)
        progress.addStretch(1)
        progress.addWidget(self.operation_stage_label)
        progress.addWidget(self.operation_progress_bar)
        progress.addWidget(self.cancel_button)
        root_layout.addLayout(progress)
        root_layout.addWidget(self.apply_safety_label)
        root_layout.addWidget(self.workflow_message_label)
        footer = QHBoxLayout()
        footer.addWidget(self.apply_guidance_label, 1)
        footer.addWidget(self.apply_results_button)
        footer.addWidget(self.apply_selected_button)
        root_layout.addLayout(footer)

        self.setCentralWidget(central)

    @Slot()
    def open_workflow_help(self) -> None:
        """Reading help leaves the current selection and pending edits intact."""
        if self.help_dialog is None:
            self.help_dialog = WorkflowHelpDialog(self)

        self.help_dialog.show()
        self.help_dialog.raise_()
        self.help_dialog.activateWindow()

    @Slot()
    def _refresh_field_details(self) -> None:
        """Expose complete projected values without creating an edit dialogue."""
        fields = self.selected_fields()
        self.field_details_button.setEnabled(bool(fields))
        self.field_guidance_label.setVisible(not fields)
        parts: list[str] = []

        for row in range(self.diff_model.rowCount()):
            if self.diff_model.index(row, 0).data(Qt.ItemDataRole.UserRole) not in fields:
                continue

            lines = [f"{self.diff_model.headerData(column, Qt.Orientation.Horizontal)}: "
                     f"{self.diff_model.index(row, column).data()}"
                     for column in range(self.diff_model.columnCount())]
            lines.append(str(self.diff_model.index(row, 0).data(Qt.ItemDataRole.ToolTipRole) or ""))
            parts.append("\n".join(lines))

        text = "\n\n".join(parts)

        # Preserve a user's copy selection during unrelated repaint signals.
        if text != self.field_details.toPlainText():
            self.field_details.setPlainText(text)

        expanded = self.field_details_button.isChecked()
        self.field_details_button.setText("Full values ▾" if expanded else "Full values ▸")
        self.field_details.setVisible(expanded and bool(text))

    def reset_layout(self) -> None:
        """Apply readable initial proportions only on startup or explicit reset."""
        from metadata_polisher.ui.layout import configure_columns, fit_initial_size

        fit_initial_size(self, 1180, 820)
        self.main_splitter.setSizes([300, max(1, self.width() - 325)])
        # The review window has its own usable size; historical vertical pane
        # proportions no longer reduce the main file table's height.
        if self.review_window.isMaximized():
            self.review_window.setWindowState(self.review_window.windowState() & ~Qt.WindowState.WindowMaximized)

        available = self.review_window.screen().availableGeometry()
        self.review_window.resize(min(1080, available.width() - 32), min(760, available.height() - 64))
        configure_columns(self.group_view, (190, 100, 60, 80))
        configure_columns(self.file_table_view, (56, 100, 260, 65, 65, 240, 160, 160, 70, 65, 90, 100))

        for column in (6, 7, 10, 11):
            self.file_table_view.setColumnHidden(column, True)

        configure_columns(self.diff_table_view, (100, 108, 180, 180, 180, 180))
        self.diff_table_view.setColumnHidden(5, True)
        # Give the final visible value spare room while retaining independently
        # resized Field/Existing/Proposed columns across projection refreshes.
        self.diff_table_view.horizontalHeader().setStretchLastSection(True)
        # Reserve eight track rows, their header and the horizontal scrollbar.
        self.file_table_view.setMinimumHeight(
            8 * self.file_table_view.verticalHeader().defaultSectionSize()
            + self.file_table_view.horizontalHeader().height()
            + self.file_table_view.horizontalScrollBar().sizeHint().height()
            + 2 * self.file_table_view.frameWidth(),
        )
        self.diff_table_view.setMinimumHeight(
            5 * self.diff_table_view.verticalHeader().defaultSectionSize()
            + self.diff_table_view.horizontalHeader().height() + 2 * self.diff_table_view.frameWidth(),
        )

    def open_metadata_review(self, *, select_first: bool = False) -> None:
        """Enter review explicitly; lookup can highlight a first track for context."""
        if select_first and not self.review_target_file_ids() and self.file_model.rowCount():
            # Highlighting is presentation only: it does not tick Include or
            # accept a proposal. Keep non-empty group/inclusion scopes intact.
            self.review_scope_combo.setCurrentIndex(self.review_scope_combo.findData("selected"))
            self.file_table_view.selectRow(0)

        self.review_window.open_review()

    def closeEvent(self, event: QCloseEvent) -> None:
        """Hide the owned review window when the main application can close."""
        if self.session_state.active_operation is None and not confirm_discard_pending(self, "exit"):
            event.ignore()
            return

        super().closeEvent(event)

        if event.isAccepted():
            self.review_window.close()

    def _connect_navigation(self) -> None:
        self.open_review_button.clicked.connect(lambda: self.open_metadata_review(select_first=True))
        self.include_review_scope_button.clicked.connect(lambda: self.set_included_file_ids(
            self.included_file_ids | frozenset(self.review_target_file_ids()),
        ))
        self.review_apply_button.clicked.connect(self.apply_selected_button.click)

        # Scope shortcuts to the file table: Ctrl+A inside an editor or the
        # review's field table must retain that widget's normal select-all.
        self.select_all_files_shortcut = QShortcut(QKeySequence.StandardKey.SelectAll, self.file_table_view)
        self.select_all_files_shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
        self.select_all_files_shortcut.activated.connect(self.select_all_files_button.click)
        self.clear_file_selection_shortcut = QShortcut(QKeySequence("Ctrl+Shift+A"), self.file_table_view)
        self.clear_file_selection_shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
        self.clear_file_selection_shortcut.activated.connect(self.clear_file_selection_button.click)
        self.group_view.selectionModel().currentChanged.connect(self._on_group_changed)
        self.file_table_view.selectionModel().currentChanged.connect(self._on_file_changed)
        self.group_view.selectionModel().selectionChanged.connect(self._refresh_group_actions)
        self.file_table_view.selectionModel().selectionChanged.connect(self._refresh_group_actions)
        self.file_table_view.selectionModel().selectionChanged.connect(self._refresh_review_projection)
        self.review_scope_combo.currentIndexChanged.connect(self._refresh_review_projection)
        self.include_selected_button.clicked.connect(lambda: self.set_included_file_ids(
            self.included_file_ids | frozenset(self.selected_file_ids()),
        ))
        self.exclude_selected_button.clicked.connect(lambda: self.set_included_file_ids(
            self.included_file_ids - frozenset(self.selected_file_ids()),
        ))
        self.file_model.inclusion_requested.connect(self._set_file_inclusion)
        self.diff_table_view.selectionModel().currentChanged.connect(self.review_context_changed)
        self.diff_table_view.selectionModel().selectionChanged.connect(self.review_context_changed)
        self._refresh_group_actions()

    def selected_group_ids(self) -> tuple[str, ...]:
        identifiers = (
            self.group_model.data(index, Qt.ItemDataRole.UserRole)
            for index in self.group_view.selectionModel().selectedRows()
        )
        return tuple(value for value in identifiers if isinstance(value, str))

    def selected_file_ids(self) -> tuple[str, ...]:
        identifiers = (
            self.file_model.data(index, Qt.ItemDataRole.UserRole)
            for index in self.file_table_view.selectionModel().selectedRows()
        )
        return tuple(value for value in identifiers if isinstance(value, str))

    def current_file_id(self) -> str | None:
        # Current is keyboard focus, which Qt may retain after deselection.
        # Commands that require highlighted files use selected_file_ids instead.
        value = self.file_model.data(self.file_table_view.currentIndex(), Qt.ItemDataRole.UserRole)

        return value if isinstance(value, str) else None

    def current_field(self) -> MetadataField | None:
        value = self.diff_model.data(self.diff_table_view.currentIndex(), Qt.ItemDataRole.UserRole)

        return value if isinstance(value, MetadataField) else None

    def selected_fields(self) -> tuple[MetadataField, ...]:
        """Capture stable field identities for a multi-field review command."""
        return tuple(value for index in self.diff_table_view.selectionModel().selectedRows()
                     if isinstance(value := index.data(Qt.ItemDataRole.UserRole), MetadataField))

    def review_target_file_ids(self) -> tuple[str, ...]:
        # Resolve the visible scope before a command is captured. Included,
        # group and library scopes may intentionally contain unhighlighted files.
        scope = self.review_scope_combo.currentData()

        if scope == "included":
            return tuple(source.file_id for group in self._session_state.groups for source in group.group.files
                         if source.file_id in self.included_file_ids)

        if scope == "group":
            group = self._group_by_id(self._visible_group_id)
            return tuple(source.file_id for source in group.group.files) if group else ()

        if scope == "library":
            return tuple(source.file_id for group in self._session_state.groups for source in group.group.files)

        # Unsupported rows remain selectable for inspection, but their path
        # identities must never enter supported-media review or write commands.
        known = {source.file_id for group in self._session_state.groups for source in group.group.files}
        return tuple(file_id for file_id in self.selected_file_ids() if file_id in known)

    def set_included_file_ids(self, file_ids: frozenset[str]) -> None:
        """Change the next write batch independently from review highlighting."""
        if self._session_state.active_operation is not None:
            return

        known = {source.file_id for group in self._session_state.groups for source in group.group.files}
        self.included_file_ids = frozenset(file_ids) & known
        self.file_model.set_included_file_ids(self.included_file_ids)
        self._refresh_group_actions()
        self._refresh_review_projection()
        self.review_context_changed.emit()

    def _set_file_inclusion(self, file_id: str, included: bool) -> None:
        self.set_included_file_ids(self.included_file_ids | {file_id} if included
                                   else self.included_file_ids - {file_id})

    def _refresh_review_projection(self) -> None:
        # Resetting a model also emits selection changes. These guards prevent
        # those intermediate empty selections from recursively clearing review.
        if self._projecting_review or self._restoring_file_selection:
            return

        self._projecting_review = True
        fields = self.selected_fields()
        current = self.current_field()
        file_ids = self.review_target_file_ids()

        try:
            if len(file_ids) > 1:
                self.diff_model.set_selection(self._session_state, file_ids)
                self.rename_current_label.setText(f"Current filenames: {len(file_ids)} files — see Filename previews")
                self.rename_proposed_label.setText("Proposed filenames: each file has its own reviewed preview")

            elif file_ids:
                for group in self._session_state.groups:
                    source = next((source for source in group.group.files if source.file_id == file_ids[0]), None)

                    if source is not None:
                        reviewed = next((item for item in group.reviewed_files if item.file_id == source.file_id), None)
                        self.diff_model.set_file(source, reviewed, self._session_state.written_files)
                        self.rename_current_label.setText(f"Current filename: {source.path.name}")
                        preview = reviewed.change_set.rename_preview if reviewed and reviewed.change_set else None
                        self.rename_proposed_label.setText(
                            f"Proposed filename: {preview.new_path.name if preview else '—'}",
                        )
                        break

            else:
                self.diff_model.set_file(None, None)
                self.rename_current_label.setText("Current filename: —")
                self.rename_proposed_label.setText("Proposed filename: —")

            for row, field in enumerate(MetadataField):
                index = self.diff_model.index(row, 0)

                if field in fields:
                    self.diff_table_view.selectionModel().select(
                        index, QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows,
                    )

                if field is current:
                    # Restore keyboard focus separately: NoUpdate preserves an
                    # intentionally deselected current field as deselected.
                    self.diff_table_view.selectionModel().setCurrentIndex(
                        index, QItemSelectionModel.SelectionFlag.NoUpdate,
                    )

        finally:
            self._projecting_review = False

        self.review_window.update_targets(len(file_ids), self.review_scope_combo.currentData())
        if not file_ids and self.selected_file_ids() and self.review_scope_combo.currentData() == "selected":
            self.review_window.target_label.setText(
                "Unsupported files can be inspected here, but cannot be edited or added to the write batch.",
            )

        if len(file_ids) == 1:
            source = next((source for group in self.session_state.groups for source in group.group.files
                           if source.file_id == file_ids[0]), None)

            if source is not None:
                self.review_window.target_label.setText(f"Metadata review · {source.path.name}")

        self.include_review_scope_button.setEnabled(self._session_state.active_operation is None and bool(file_ids))
        self.review_context_changed.emit()

    def _refresh_group_actions(self) -> None:
        idle = self._session_state.active_operation is None
        group = self._group_by_id(self._visible_group_id)
        selected_files = self.selected_file_ids()
        self.file_guidance_label.setText(
            "Choose an album to see its tracks." if self._session_state.groups
            else "Choose a folder to scan your music library."
        )
        self.file_guidance_label.setVisible(group is None and not isinstance(
            self._session_state.selection, UnsupportedSelection,
        ))
        self.selection_scope_label.setText(
            f"{len(selected_files)} highlighted · {len(self.included_file_ids)} included",
        )
        self.review_inclusion_label.setText(f"{len(self.included_file_ids)} included for writing")
        self.select_all_files_button.setEnabled(idle and group is not None and bool(group.group.files))
        self.clear_file_selection_button.setEnabled(idle and bool(selected_files))
        supported_selection = group is not None and bool(selected_files)
        self.include_selected_button.setEnabled(idle and supported_selection)
        self.exclude_selected_button.setEnabled(idle and supported_selection)
        self.split_group_button.setEnabled(
            idle and group is not None and 0 < len(selected_files) < len(group.group.files)
        )
        self.merge_groups_button.setEnabled(idle and len(self.selected_group_ids()) >= 2)
        self.disc_override_button.setEnabled(idle and group is not None)
        self.choose_candidate_button.setEnabled(idle and group is not None and group.candidate_lookup is not None)
        self.edit_search_button.setEnabled(idle and group is not None and not group.requires_rescan)
        selected_groups = set(self.selected_group_ids())
        searchable = sum(item.group.group_id in selected_groups and is_group_searchable(item)
                         for item in self.session_state.groups)
        self.find_selected_button.setEnabled(idle and bool(searchable))
        self.find_selected_button.setToolTip(
            f"Find metadata for {searchable} searchable of {len(selected_groups)} selected albums (Ctrl+L)",
        )
        self.find_all_incomplete_button.setEnabled(idle and bool(incomplete_searchable_group_ids(self._session_state)))
        self.language_combo.setEnabled(idle and group is not None and not group.requires_rescan)
        selected_language = group.language_override if group is not None else None
        settings_language = (self.library_controller.settings.matching.preferred_language
                             if self.library_controller is not None else "auto")
        # Rebuilding choices is presentation, not a new language decision. Block
        # the signal that would otherwise rerank proposals during this refresh.
        self.language_combo.blockSignals(True)
        self.language_combo.clear()

        for language, label in available_language_choices(group, settings_language):
            self.language_combo.addItem(label, language)

        self.language_combo.setCurrentIndex(self.language_combo.findData(selected_language))
        self.language_combo.blockSignals(False)

    def _button(self, text: str, object_name: str) -> QPushButton:
        button = QPushButton(text, self)
        button.setObjectName(object_name)

        return button

    @staticmethod
    def _pane(title: str, view: QWidget) -> QWidget:
        pane = QWidget()
        layout = QVBoxLayout(pane)
        layout.addWidget(QLabel(title, pane))
        layout.addWidget(view, 1)

        return pane

    def _diff_pane(self) -> QWidget:
        pane = QWidget()
        layout = QVBoxLayout(pane)
        layout.setSpacing(4)
        layout.setContentsMargins(0, 0, 0, 0)
        scope = QHBoxLayout()
        scope.addWidget(QLabel("Metadata ·", pane))
        scope.addWidget(self.review_scope_label, 1)
        scope.addWidget(self.previous_file_button)
        scope.addWidget(self.next_file_button)
        scope.addWidget(self.undo_review_button)
        layout.addLayout(scope)
        detail_actions = QHBoxLayout()
        detail_actions.addWidget(self.field_guidance_label, 1)
        detail_actions.addWidget(self.field_details_button)
        layout.addLayout(detail_actions)
        layout.addWidget(self.diff_table_view, 1)
        layout.addWidget(self.field_details)
        layout.addWidget(self.proposal_combo)
        field_actions = QHBoxLayout()

        for button in (self.keep_existing_button, self.use_proposed_button, self.manual_value_button):
            field_actions.addWidget(button)

        # Removing metadata is a different decision from choosing its value.
        # Separate it spatially and name the effect; no colour cue is required.
        field_actions.addSpacing(16)
        self.clear_value_button.setText("Clear selected fields")
        self.clear_value_button.setToolTip("Remove the highlighted fields from files in this review scope")
        field_actions.addWidget(self.clear_value_button)
        layout.addLayout(field_actions)
        safe_actions = QHBoxLayout()
        safe_actions.addWidget(self.accept_safe_additions_button)
        self.accept_safe_additions_button.setToolTip(
            "Fill missing values with unambiguous supported proposals in this scope. Existing values remain unchanged.",
        )
        safe_actions.addStretch(1)
        layout.addLayout(safe_actions)
        layout.addWidget(QLabel("Filenames", pane))
        layout.addWidget(self.rename_current_label)
        layout.addWidget(self.rename_proposed_label)
        layout.addWidget(self.rename_template_label)
        rename_actions = QHBoxLayout()
        rename_actions.addWidget(self.keep_filename_button)
        rename_actions.addWidget(self.apply_rename_button)
        rename_actions.addWidget(self.rename_previews_button)
        layout.addLayout(rename_actions)
        layout.addWidget(self.rename_validation_label)

        return pane

    @Slot(QModelIndex, QModelIndex)
    def _on_group_changed(self, current: QModelIndex, previous: QModelIndex) -> None:
        del previous

        group_id = self.group_model.data(current, Qt.ItemDataRole.UserRole)

        if isinstance(group_id, UnsupportedSelection):
            self._show_selection(group_id)

            if not isinstance(self._session_state.selection, UnsupportedSelection):
                self.unsupported_selection_requested.emit()

            return

        selected_group_id = group_id if isinstance(group_id, str) else None
        self._show_group(selected_group_id)

        current_selection = self._session_state.selection

        if selected_group_id is not None and (
            not isinstance(current_selection, GroupSelection)
            or current_selection.group_id != selected_group_id
        ):
            self.group_selection_requested.emit(selected_group_id)

    def _show_selection(self, selection: str | UnsupportedSelection | None) -> None:
        if isinstance(selection, UnsupportedSelection):
            self._show_group(None)
            self.file_model.set_unsupported_files(self._session_state.unsupported_files)
        else:
            self._show_group(selection)

    def _show_group(self, group_id: str | None) -> None:
        group = self._group_by_id(group_id)

        if group is not None and group is self._displayed_group:
            self._refresh_group_actions()
            return

        # Preserve the stable file/field IDs while replacing the immutable
        # review snapshot. Row numbers alone can refer to a different file.
        same_group = group_id == self._visible_group_id
        current_file = self.current_file_id() if same_group else None
        selected_files = self.selected_file_ids() if same_group else ()
        current_field = self.current_field() if same_group else None
        selected_fields = self.selected_fields() if same_group else ()
        self._displayed_group = group
        self._visible_group_id = group_id if group is not None else None
        # Qt emits empty selections during a model reset. Defer projecting the
        # review until stable file IDs have been restored from the new snapshot.
        self._restoring_file_selection = True
        self.file_model.set_group_state(group, self._session_state.written_files)
        self.diff_model.set_file(None, None)
        self.rename_current_label.setText("Current filename: —")
        self.rename_proposed_label.setText("Proposed filename: —")

        for row in range(self.file_model.rowCount()):
            index = self.file_model.index(row, 0)
            file_id = index.data(Qt.ItemDataRole.UserRole)

            if file_id in selected_files:
                self.file_table_view.selectionModel().select(
                    index, QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows,
                )

            if file_id == current_file:
                self.file_table_view.selectionModel().setCurrentIndex(
                    index, QItemSelectionModel.SelectionFlag.NoUpdate,
                )
                self._on_file_changed(index, QModelIndex())

        self._restoring_file_selection = False
        self._refresh_review_projection()

        for row, field in enumerate(MetadataField):
            index = self.diff_model.index(row, 0)

            if field in selected_fields:
                self.diff_table_view.selectionModel().select(
                    index, QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows,
                )

            if field is current_field:
                self.diff_table_view.selectionModel().setCurrentIndex(index, QItemSelectionModel.SelectionFlag.NoUpdate)

        self._refresh_group_actions()
        self.review_context_changed.emit()

    @Slot(QModelIndex, QModelIndex)
    def _on_file_changed(self, current: QModelIndex, previous: QModelIndex) -> None:
        del current, previous

        # One projection owns model replacement and captures field IDs before
        # resetting them. Resetting here first would lose the user's editing
        # position every time they move to the next track.
        self._refresh_review_projection()

    def _source_by_id(self, file_id: object) -> LocalMediaFile | None:
        visible_group = self._group_by_id(self._visible_group_id)

        if visible_group is None or not isinstance(file_id, str):
            return None

        return next(
            (
                source
                for source in visible_group.group.files
                if source.file_id == file_id
            ),
            None,
        )

    def _group_by_id(self, group_id: str | None) -> GroupState | None:
        if group_id is None:
            return None

        return next(
            (
                group
                for group in self._session_state.groups
                if group.group.group_id == group_id
            ),
            None,
        )
