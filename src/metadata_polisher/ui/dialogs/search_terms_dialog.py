"""Edit semantic search evidence for one group in the current session."""

from dataclasses import replace

from PySide6.QtWidgets import QDialog, QDialogButtonBox, QFormLayout, QLineEdit, QSpinBox, QWidget

from metadata_polisher.domain.matching import ReleaseSearchQuery


class SearchTermsDialog(QDialog):
    def __init__(self, query: ReleaseSearchQuery, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Edit search terms")
        self._query = query
        layout = QFormLayout(self)
        self.album_edit = QLineEdit(query.album or "", self)
        self.artists_edit = QLineEdit("; ".join(query.artists), self)
        self.year_edit = QSpinBox(self)
        self.year_edit.setRange(0, 9999)
        self.year_edit.setSpecialValueText("Any year")
        self.year_edit.setValue(query.year or 0)
        layout.addRow("Album", self.album_edit)
        layout.addRow("Artists (separate with ;)", self.artists_edit)
        layout.addRow("Year", self.year_edit)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def query(self) -> ReleaseSearchQuery:
        # Replace only editable hints so any other captured search evidence is
        # retained. This constructs a session query, not a metadata tag update.
        return replace(
            self._query,
            album=self.album_edit.text().strip() or None,
            artists=tuple(value.strip() for value in self.artists_edit.text().split(";") if value.strip()),
            year=self.year_edit.value() or None,
        )
