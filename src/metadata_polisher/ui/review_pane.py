"""Reusable review controls and presentation over a caller-owned diff model."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from metadata_polisher.domain.metadata import MetadataField
from metadata_polisher.ui.layout import ElidedLabel, StatusLabel, configure_columns
from metadata_polisher.ui.models import MetadataDiffModel

APPLY_SAFETY_TEXT = "Review decisions stay in memory. Only Apply changes in the final confirmation writes files."


class ReviewPane(QWidget):
    """Own review layout without owning session state, decisions or write scope."""

    def __init__(self, model: MetadataDiffModel, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._model = model
        self._build_widgets()
        self._build_layout()

        # The pane can also be hosted by a dialogue. Enter in its table must
        # never activate an unrelated decision or write-batch button by default.
        for button in self.findChildren(QPushButton):
            button.setAutoDefault(False)

        self.update_targets(0, "selected")

    def _button(self, text: str, object_name: str) -> QPushButton:
        button = QPushButton(text, self)
        button.setObjectName(object_name)

        return button

    def _build_widgets(self) -> None:
        self.target_label = QLabel(self)
        self.target_label.setWordWrap(True)
        self.review_scope_combo = QComboBox(self)
        self.review_scope_combo.addItem("Selected files", "selected")
        self.review_scope_combo.addItem("Current group", "group")
        self.review_scope_combo.addItem("Included files", "included")
        self.review_scope_combo.addItem("All library files", "library")
        self.review_scope_combo.setAccessibleName("Review scope")
        self.review_scope_label = QLabel("Select files and fields to review.", self)
        self.review_scope_label.setWordWrap(True)
        self.previous_file_button = self._button("Previous", "previousReviewFileButton")
        self.next_file_button = self._button("Next", "nextReviewFileButton")
        self.undo_review_button = self._button("Undo review", "undoReviewButton")

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

        self.diff_table_view = QTableView(self)
        self.diff_table_view.setObjectName("diffTableView")
        self.diff_table_view.setModel(self._model)
        self.diff_table_view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.diff_table_view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.proposal_combo = QComboBox(self)
        self.proposal_combo.setObjectName("proposalCombo")
        self.keep_existing_button = self._button("Keep Existing", "keepExistingButton")
        self.use_proposed_button = self._button("Use Proposed", "useProposedButton")
        self.manual_value_button = self._button("Manual…", "manualValueButton")
        self.clear_value_button = self._button("Clear selected fields", "clearValueButton")
        self.clear_value_button.setToolTip("Remove the highlighted fields from files in this review scope")
        self.accept_safe_additions_button = self._button("Accept Safe Additions", "acceptSafeAdditionsButton")
        self.accept_safe_additions_button.setToolTip(
            "Fill missing values with unambiguous supported proposals in this scope. Existing values remain unchanged.",
        )

        self.rename_current_label = ElidedLabel("Current filename: —", self)
        self.rename_current_label.setObjectName("renameCurrentLabel")
        self.rename_proposed_label = ElidedLabel("Proposed filename: —", self)
        self.rename_proposed_label.setObjectName("renameProposedLabel")
        self.rename_template_label = ElidedLabel("Template: [%discnumber%.]%tracknumber%. %title%", self)
        self.rename_template_label.setObjectName("renameTemplateLabel")
        self.keep_filename_button = self._button("Keep filename", "keepFilenameButton")
        self.apply_rename_button = self._button("Use proposed filename", "applyRenameButton")
        self.rename_previews_button = self._button("Filename previews…", "renamePreviewsButton")
        self.rename_validation_label = QLabel("", self)
        self.rename_validation_label.setWordWrap(True)

        self.review_inclusion_label = QLabel("0 included for writing", self)
        self.include_review_scope_button = self._button("Add scope to write batch", "includeReviewScopeButton")
        self.review_apply_button = self._button("Review changes…", "reviewApplyButton")
        self.review_apply_guidance_label = QLabel(self)
        self.review_apply_guidance_label.setWordWrap(True)
        self.review_message_label = StatusLabel("", self)

    def _build_layout(self) -> None:
        header = QHBoxLayout()
        header.addWidget(self.target_label, 1)
        header.addWidget(self.review_scope_combo)

        # Scope and write actions remain outside the scrolling body so even
        # long diagnostics cannot move those controls beyond a small desktop.
        self.scroll_area = QScrollArea(self)
        self.scroll_area.setObjectName("reviewScrollArea")
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setMinimumSize(0, 0)
        self.scroll_area.setWidget(self._build_body())
        layout = QVBoxLayout(self)
        # The host supplies the outer window margins; avoid doubling them when
        # this pane is placed inside MetadataReviewWindow.
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(header)
        layout.addWidget(self.scroll_area, 1)
        layout.addWidget(self._build_footer())

    def _build_body(self) -> QWidget:
        body = QWidget(self)
        layout = QVBoxLayout(body)
        layout.setSpacing(4)
        layout.setContentsMargins(0, 0, 0, 0)
        scope = QHBoxLayout()
        scope.addWidget(QLabel("Metadata ·", body))
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
        # Retain the existing separation without requiring a colour cue.
        field_actions.addSpacing(16)
        field_actions.addWidget(self.clear_value_button)
        layout.addLayout(field_actions)
        safe_actions = QHBoxLayout()
        safe_actions.addWidget(self.accept_safe_additions_button)
        safe_actions.addStretch(1)
        layout.addLayout(safe_actions)
        layout.addWidget(QLabel("Filenames", body))
        layout.addWidget(self.rename_current_label)
        layout.addWidget(self.rename_proposed_label)
        layout.addWidget(self.rename_template_label)
        rename_actions = QHBoxLayout()
        rename_actions.addWidget(self.keep_filename_button)
        rename_actions.addWidget(self.apply_rename_button)
        rename_actions.addWidget(self.rename_previews_button)
        layout.addLayout(rename_actions)
        layout.addWidget(self.rename_validation_label)

        return body

    def _build_footer(self) -> QWidget:
        footer = QWidget(self)
        layout = QVBoxLayout(footer)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(QLabel("Write batch", footer))
        safety = QLabel(APPLY_SAFETY_TEXT, footer)
        safety.setWordWrap(True)
        layout.addWidget(safety)
        layout.addWidget(self.review_message_label)
        layout.addWidget(self.review_apply_guidance_label)
        actions = QHBoxLayout()
        actions.addWidget(self.review_inclusion_label, 1)
        actions.addWidget(self.include_review_scope_button)
        actions.addWidget(self.review_apply_button)
        layout.addLayout(actions)

        return footer

    def refresh_field_details(self, fields: tuple[MetadataField, ...]) -> None:
        """Render supplied field identities without changing the review selection."""
        self.field_details_button.setEnabled(bool(fields))
        self.field_guidance_label.setVisible(not fields)
        parts: list[str] = []

        for row in range(self._model.rowCount()):
            if self._model.index(row, 0).data(Qt.ItemDataRole.UserRole) not in fields:
                continue

            lines = [f"{self._model.headerData(column, Qt.Orientation.Horizontal)}: "
                     f"{self._model.index(row, column).data()}"
                     for column in range(self._model.columnCount())]
            lines.append(str(self._model.index(row, 0).data(Qt.ItemDataRole.ToolTipRole) or ""))
            parts.append("\n".join(lines))

        text = "\n\n".join(parts)

        # Preserve the user's copy selection during unrelated repaint signals.
        if text != self.field_details.toPlainText():
            self.field_details.setPlainText(text)

        expanded = self.field_details_button.isChecked()
        self.field_details_button.setText("Full values ▾" if expanded else "Full values ▸")
        self.field_details.setVisible(expanded and bool(text))

    def reset_layout(self) -> None:
        """Restore review column defaults only on start-up or explicit reset."""
        configure_columns(self.diff_table_view, (100, 108, 180, 180, 180, 180))
        self.diff_table_view.setColumnHidden(5, True)
        self.diff_table_view.horizontalHeader().setStretchLastSection(True)
        self.diff_table_view.setMinimumHeight(
            5 * self.diff_table_view.verticalHeader().defaultSectionSize()
            + self.diff_table_view.horizontalHeader().height() + 2 * self.diff_table_view.frameWidth(),
        )

    def update_targets(self, count: int, scope: str) -> None:
        """Refresh context without changing the host window's visibility."""
        description = {
            "selected": "selected", "group": "in current group",
            "included": "included", "library": "in library",
        }[scope]
        text = f"Metadata review · {count} {description}"

        if not count:
            text += " — Select tracks or change the review scope"

        self.target_label.setText(text)
