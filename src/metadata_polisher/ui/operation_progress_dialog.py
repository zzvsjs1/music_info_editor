"""Modeless progress presentation; worker ownership stays in the controller."""

from PySide6.QtCore import QElapsedTimer, Qt, QTimer, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from metadata_polisher.infrastructure.logging_setup import redact_sensitive_text
from metadata_polisher.ui.layout import fit_initial_size


class OperationProgressDialog(QDialog):
    """Hide lookup work; request, rather than force, cancellation of writes."""

    cancel_requested = Signal()

    def __init__(self, operation_id: str, *, writing: bool, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.operation_id = operation_id
        self._writing = writing
        self._terminal = False
        self._cancelling = False
        self.setObjectName("operationProgressDialog")
        self.setWindowTitle("Applying reviewed changes" if writing else "Metadata operation")
        self.setModal(False)
        layout = QVBoxLayout(self)
        self.safety_label = QLabel(
            "Only the confirmed changes are being applied. Cancellation waits for a safe file boundary."
            if writing else "No files are being changed.", self,
        )
        self.safety_label.setWordWrap(True)
        self.stage_label = QLabel("Starting…", self)
        self.stage_label.setWordWrap(True)
        self.context_label = QLabel("", self)
        self.context_label.setWordWrap(True)
        self.progress_bar = QProgressBar(self)
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setFormat("%v / %m in this stage")
        self.elapsed_label = QLabel("Elapsed: 0:00", self)

        for widget in (self.safety_label, self.stage_label, self.context_label,
                       self.progress_bar, self.elapsed_label):
            if isinstance(widget, QLabel):
                widget.setTextFormat(Qt.TextFormat.PlainText)

            layout.addWidget(widget)

        self.details_button = QPushButton("Show details", self)
        self.details_button.setCheckable(True)
        layout.addWidget(self.details_button)
        self.detail_log = QPlainTextEdit(self)
        self.detail_log.setReadOnly(True)
        # Detail events are in-memory diagnostics, never an unbounded private
        # response archive. Keep useful recent stages without retaining payloads.
        self.detail_log.setMaximumBlockCount(200)
        self.detail_log.hide()
        layout.addWidget(self.detail_log)
        self.details_button.toggled.connect(self._toggle_details)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.cancel_button = QPushButton("Cancel", self)
        self.cancel_button.clicked.connect(self.request_cancel)
        buttons.addWidget(self.cancel_button)
        self.hide_button = QPushButton("Hide", self)
        self.hide_button.setVisible(not writing)
        self.hide_button.clicked.connect(self.hide)
        buttons.addWidget(self.hide_button)
        layout.addLayout(buttons)
        self._elapsed = QElapsedTimer()
        self._elapsed.start()
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._update_elapsed)
        self._timer.start()
        fit_initial_size(self, 650, 330)

    def _toggle_details(self, visible: bool) -> None:
        self.detail_log.setVisible(visible)
        self.details_button.setText("Hide details" if visible else "Show details")

    def _update_elapsed(self) -> None:
        # The timer reports elapsed time only; it is never used to estimate a
        # percentage for HTTP or file work whose remaining duration is unknown.
        seconds = self._elapsed.elapsed() // 1000
        self.elapsed_label.setText(f"Elapsed: {seconds // 60}:{seconds % 60:02d}")

    def append_log(self, message: str) -> None:
        if not self._terminal:
            self.detail_log.appendPlainText(redact_sensitive_text(message))

    def update_status(self, stage: str, provider: str, item: str, current: int, total: int) -> None:
        if self._terminal:
            return

        safe_stage = redact_sensitive_text(stage)

        if self._cancelling and "Cancelling" not in safe_stage:
            safe_stage = f"Cancelling… {safe_stage}"

        self.stage_label.setText(safe_stage)
        self.context_label.setText(redact_sensitive_text(" · ".join(filter(None, (provider, item)))))

        # Qt's 0..0 range is an indeterminate indicator. Unknown work should not
        # appear to be zero per cent of a fabricated, fixed total.
        if total == 0:
            self.progress_bar.setRange(0, 0)
        else:
            self.progress_bar.setRange(0, total)
            self.progress_bar.setValue(current)

    def request_cancel(self) -> None:
        # Cancellation is a one-way request until a terminal payload arrives;
        # repeating Escape or Close must not enqueue another cancellation.
        if self._terminal or self._cancelling:
            return

        self._cancelling = True
        self.cancel_button.setEnabled(False)
        self.stage_label.setText(f"Cancelling… {self.stage_label.text()}")
        self.append_log("Cancellation requested; waiting for a safe boundary.")
        self.cancel_requested.emit()

    def finish(self, status: str) -> None:
        if self._terminal:
            return

        self.append_log(status)
        self._terminal = True
        self._timer.stop()
        self._update_elapsed()
        self.stage_label.setText(redact_sensitive_text(status))
        self.cancel_button.setEnabled(False)
        self.hide_button.setText("Close")
        self.hide_button.show()

    def reject(self) -> None:
        # QDialog routes Escape directly to reject(), independently of the
        # title-bar close event. Both routes need the same safe-write policy.
        if self._writing and not self._terminal:
            self.request_cancel()
        else:
            self.hide()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._writing and not self._terminal:
            self.request_cancel()
            event.ignore()
        else:
            event.accept()
