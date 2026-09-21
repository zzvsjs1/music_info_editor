"""Read-only guidance available without leaving or changing the review session."""

from PySide6.QtWidgets import QDialog, QPushButton, QTextBrowser, QVBoxLayout, QWidget

from metadata_polisher.ui.layout import fit_initial_size


class WorkflowHelpDialog(QDialog):
    """Keep the workflow and keyboard reference in a bounded, reusable window."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setWindowTitle("How to review metadata")
        self.setModal(False)
        layout = QVBoxLayout(self)
        contents = QTextBrowser(self)
        contents.setAccessibleName("Workflow and keyboard help")
        contents.setHtml("""
            <h2>Review first, write when ready</h2>
            <ol>
              <li>Choose a folder and an album, then highlight the tracks to review.</li>
              <li>Open Metadata review. Select a field; double-click its Final value
                  or press F2 to enter a correction. Full values shows copyable details.</li>
              <li>Keep existing preserves each file's value. Use candidate uses each
                  file's own proposal. Set common value deliberately shares your entry.</li>
              <li>Clear removes the selected fields. Accept safe additions only fills
                  missing values with an unambiguous supported proposal.</li>
              <li>Choose filename changes separately. Add the files you want to the
                  write batch, then use Review changes and inspect the final summary.</li>
            </ol>
            <p><b>Only Apply changes in the final confirmation writes files.</b>
            Highlighting, reviewing metadata and choosing filenames do not add files
            to the write batch. Undo review changes pending decisions, not completed writes.</p>
            <h3>Review scope</h3>
            <p>Selected files means the highlighted tracks. Current group, Included files
            and All library files can contain tracks outside the visible table.
            Check the file and field counts before a batch action.</p>
            <h3>Keyboard reference</h3>
            <p>Ctrl+O: choose folder<br>F5 or Enter in folder: rescan<br>
            Ctrl+A in tracks: select all displayed tracks<br>Ctrl+Shift+A: clear highlighting<br>
            Enter on a track or Ctrl+E: open review<br>F2 on a field: manual value<br>
            Alt+Left / Alt+Right: previous / next track<br>Ctrl+Z in review: undo review<br>
            Ctrl+L: find metadata for selected albums<br>Ctrl+Enter: review the write batch<br>
            Escape: close the current review/dialogue<br>F1: open this help</p>
            <p>Use Settings to select a provider or local editing only. “Use Settings”
            inherits your language preference; Auto is an explicit automatic choice.</p>
        """)
        layout.addWidget(contents, 1)
        close = QPushButton("Close", self)
        close.clicked.connect(self.close)
        layout.addWidget(close)
        fit_initial_size(self, 700, 570)
