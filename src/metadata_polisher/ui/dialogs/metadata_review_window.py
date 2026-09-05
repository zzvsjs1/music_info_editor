"""A reusable, non-modal workspace for reviewing the main window's selection."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from metadata_polisher.ui.layout import fit_initial_size


class MetadataReviewWindow(QDialog):
    """Share existing review controls without owning a second session snapshot."""

    def __init__(self, body: QWidget, scope_control: QWidget, footer: QWidget, parent: QWidget) -> None:
        super().__init__(parent, Qt.WindowType.Window | Qt.WindowType.WindowMinMaxButtonsHint
                         | Qt.WindowType.WindowCloseButtonHint)
        self.setObjectName("metadataReviewWindow")
        self.setWindowTitle("Metadata review — Metadata Polisher")
        self.setModal(False)
        self.target_label = QLabel(self)
        self.target_label.setWordWrap(True)

        header = QHBoxLayout()
        header.addWidget(self.target_label, 1)
        header.addWidget(scope_control)

        # Keep the scope selector and Apply controls reachable on small screens.
        # The body scrolls independently, so long validation messages cannot
        # force either top-level window beyond the available desktop.
        self.scroll_area = QScrollArea(self)
        self.scroll_area.setObjectName("reviewScrollArea")
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setMinimumSize(0, 0)
        self.scroll_area.setWidget(body)
        layout = QVBoxLayout(self)
        layout.addLayout(header)
        layout.addWidget(self.scroll_area, 1)
        layout.addWidget(footer)

        # Enter inside the field table must not accidentally include or apply a
        # batch through QDialog's automatic default-button behaviour.
        for button in self.findChildren(QPushButton):
            button.setAutoDefault(False)

        self.update_targets(0, "selected")
        fit_initial_size(self, 1080, 760)

    def update_targets(self, count: int, scope: str) -> None:
        """Refresh context without reopening or hiding the user's window."""
        # Scope changes update context even while this window is hidden. Its
        # visibility is the user's choice and is not derived from selection size.
        description = {
            "selected": "selected", "group": "in current group",
            "included": "included", "library": "in library",
        }[scope]
        text = f"Metadata review · {count} {description}"

        if not count:
            text += " — Select tracks or change the review scope"

        self.target_label.setText(text)

    def open_review(self) -> None:
        """Reuse the same controls and geometry, including after Escape or X."""
        if self.isMinimized():
            self.showNormal()
        else:
            self.show()

        self.raise_()
        self.activateWindow()
