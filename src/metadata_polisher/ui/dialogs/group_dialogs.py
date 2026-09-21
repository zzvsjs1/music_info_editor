"""Small native dialogs for explicit in-memory grouping choices."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QListWidget, QSpinBox, QVBoxLayout, QWidget

from metadata_polisher.scanner.grouping import AlbumGroup


class MergeGroupsDialog(QDialog):
    def __init__(
        self, groups: tuple[AlbumGroup, ...], parent: QWidget | None = None, *, review_warning: str = "",
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Merge groups")
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Merge these groups for review? Files stay in their current folders.", self))
        listing = QListWidget(self)

        # List the captured groups for confirmation; merging is a session edit
        # performed by the controller and never moves their files on disk.
        for group in groups:
            listing.addItem(f"{group.album_title or group.files[0].path.parent.name} — {len(group.files)} files")

        layout.addWidget(listing)

        if review_warning:
            warning = QLabel(review_warning, self)
            warning.setTextFormat(Qt.TextFormat.PlainText)
            warning.setWordWrap(True)
            layout.addWidget(warning)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self)

        if review_warning:
            # A loss of candidate-dependent work needs a deliberate acceptance;
            # Enter and Escape should preserve the existing reviewed session.
            buttons.button(QDialogButtonBox.StandardButton.Cancel).setDefault(True)

        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class DiscOverrideDialog(QDialog):
    def __init__(self, number: int | None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Disc number for lookup")
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Use this disc hint for lookup. Changing it clears the group's current review.", self))
        self.number = QSpinBox(self)
        self.number.setRange(0, 9999)
        self.number.setSpecialValueText("Auto")
        self.number.setValue(number or 0)
        layout.addWidget(self.number)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def disc_number(self) -> int | None:
        # The Auto entry removes an explicit lookup hint. It must not become a
        # literal disc zero in the matching evidence supplied by the controller.
        return self.number.value() or None
