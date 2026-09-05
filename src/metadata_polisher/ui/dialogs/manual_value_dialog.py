"""Native input controls for typed, explicitly chosen manual metadata values."""

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QPlainTextEdit,
    QSpinBox,
    QWidget,
)

from metadata_polisher.application.review import set_manual_decision
from metadata_polisher.domain.metadata import FieldReadState, MetadataField, Position
from metadata_polisher.domain.review import (
    DecisionOrigin,
    FieldDecisionKind,
    FieldReviewState,
    FieldValue,
)


class ManualValueDialog(QDialog):
    """Convert native input to semantic values and delegate validation to review."""

    def __init__(
        self,
        field: MetadataField,
        value: FieldValue | None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Manual value: {field.value.replace('_', ' ')}")
        self._field = field
        self._base_review = FieldReviewState(
            field=field,
            read_state=FieldReadState.MISSING,
            existing_value=None,
            proposals=(),
            decision=FieldDecisionKind.KEEP_EXISTING,
            selected_proposal=None,
            manual_value=None,
            decision_origin=DecisionOrigin.DEFAULT,
            requires_review=False,
            reason_codes=(),
        )

        # A supplied seed has already been reviewed elsewhere, but using the same
        # boundary check prevents a wrong semantic type from silently losing data.
        if value is not None:
            set_manual_decision(self._base_review, value)

        self._is_position = field in {MetadataField.TRACK, MetadataField.DISC}
        self._is_multiple = field in {
            MetadataField.ARTISTS,
            MetadataField.ALBUM_ARTISTS,
            MetadataField.COMPOSERS,
            MetadataField.GENRES,
        }
        layout = QFormLayout(self)

        if self._is_position:
            self.number_spin = QSpinBox(self)
            self.total_spin = QSpinBox(self)

            for control in (self.number_spin, self.total_spin):
                control.setRange(0, 2_147_483_647)
                control.setSpecialValueText("Missing")

            if isinstance(value, Position):
                self.number_spin.setValue(value.number or 0)
                self.total_spin.setValue(value.total or 0)

            layout.addRow("Number", self.number_spin)
            layout.addRow("Total", self.total_spin)
        else:
            self.text_edit = QPlainTextEdit(self)

            if isinstance(value, tuple):
                self.text_edit.setPlainText("\n".join(value))
            elif isinstance(value, str):
                self.text_edit.setPlainText(value)

            label = "Values (one per line)" if self._is_multiple else "Value"
            layout.addRow(label, self.text_edit)

        hint = QLabel("Enter a value. Use the separate Clear action to remove this field.", self)
        hint.setWordWrap(True)
        layout.addRow(hint)
        self.error_label = QLabel(self)
        self.error_label.setWordWrap(True)
        layout.addRow(self.error_label)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def value(self) -> FieldValue:
        """Return validated input without modifying any live review or proposal."""
        candidate: FieldValue

        if self._is_position:
            # Zero is the spin box's Missing sentinel, not track/disc zero. Keep
            # number and total independent so either component can be reviewed.
            candidate = Position(
                number=self.number_spin.value() or None,
                total=self.total_spin.value() or None,
            )
        elif self._is_multiple:
            # Only line boundaries separate values. Punctuation belongs to the
            # person's text, and blank entries remain visible to domain validation.
            candidate = tuple(self.text_edit.toPlainText().splitlines())
        else:
            candidate = self.text_edit.toPlainText()

        # Reuse domain validation even on direct value() calls. The dialogue
        # must not invent its own rules for empty text, lists or positions.
        validated = set_manual_decision(self._base_review, candidate).manual_value
        assert validated is not None
        return validated

    def accept(self) -> None:
        """Keep invalid input editable instead of translating it into Clear."""
        try:
            self.value()
        except (TypeError, ValueError) as error:
            self.error_label.setText(str(error))
            return

        self.error_label.clear()
        super().accept()
