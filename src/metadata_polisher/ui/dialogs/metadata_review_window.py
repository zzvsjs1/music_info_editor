"""A reusable, non-modal workspace for reviewing the main window's selection."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QVBoxLayout, QWidget

from metadata_polisher.ui.layout import fit_initial_size
from metadata_polisher.ui.review_pane import ReviewPane


class MetadataReviewWindow(QDialog):
    """Share existing review controls without owning a second session snapshot."""

    def __init__(self, pane: ReviewPane, parent: QWidget) -> None:
        super().__init__(parent, Qt.WindowType.Window | Qt.WindowType.WindowMinMaxButtonsHint
                         | Qt.WindowType.WindowCloseButtonHint)
        self.setObjectName("metadataReviewWindow")
        self.setWindowTitle("Metadata review — Metadata Polisher")
        self.setModal(False)
        self.pane = pane
        # Retain the window's public references for layout and interaction
        # callers while the pane owns their construction and presentation.
        self.target_label = pane.target_label
        self.scroll_area = pane.scroll_area
        layout = QVBoxLayout(self)
        layout.addWidget(pane)

        self.update_targets(0, "selected")
        fit_initial_size(self, 1080, 760)

    def update_targets(self, count: int, scope: str) -> None:
        """Refresh context without reopening or hiding the user's window."""
        self.pane.update_targets(count, scope)

    def open_review(self) -> None:
        """Reuse the same controls and geometry, including after Escape or X."""
        if self.isMinimized():
            self.showNormal()
        else:
            self.show()

        self.raise_()
        self.activateWindow()
