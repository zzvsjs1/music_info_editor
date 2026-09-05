"""Native review controls for explicit local-to-provider track assignments."""

from collections.abc import Sequence
from functools import partial
from typing import cast

from PySide6.QtCore import Qt
from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from metadata_polisher.application.review import set_manual_track_assignment
from metadata_polisher.domain.matching import ReleaseCandidate
from metadata_polisher.domain.media import LocalMediaFile
from metadata_polisher.matching.track_mapping import TrackMapping, TrackMappingResult
from metadata_polisher.ui.layout import configure_columns, fit_initial_size


def _format_duration(seconds: float | None) -> str:
    """Present known durations in the same minutes:seconds form as file rows."""
    if seconds is None:
        return "—"

    minutes, remaining = divmod(max(0, round(seconds)), 60)
    return f"{minutes}:{remaining:02d}"


def _assignment_evidence(assignment: TrackMapping | None) -> str:
    if assignment is None:
        return "Unmapped"

    # Explicit human confirmation has no automatic matching score. Displaying its
    # internal confidence and zero contribution as a score would mislead review.
    if "MANUAL_TRACK_ASSIGNMENT" in assignment.reason_codes:
        heading = "Manual assignment"
    else:
        heading = f"{assignment.score:g} / 100 · {assignment.classification.value.capitalize()}"

    details = [f"{item.code} — {item.detail}" for item in assignment.evidence]
    return "\n".join((heading, *details))


class TrackMappingDialog(QDialog):
    """Edit an immutable working mapping through the application review service."""

    def __init__(
        self,
        files: Sequence[LocalMediaFile],
        candidate: ReleaseCandidate,
        mapping: TrackMappingResult,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Review track mapping")
        self._files = tuple(files)
        self._candidate = candidate
        self._mapping = mapping
        medium = candidate.media[mapping.selected_medium_index]
        layout = QVBoxLayout(self)
        instructions = QLabel(
            f"Disc {medium.medium_number or '?'}: assign a provider track to each local file, "
            "or leave it unmapped. Clear an existing assignment before reusing its track.",
            self,
        )
        instructions.setWordWrap(True)
        layout.addWidget(instructions)
        self.table = QTableView(self)
        self.table.setObjectName("trackMappingTable")
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._model = QStandardItemModel(0, 5, self.table)
        self._model.setHorizontalHeaderLabels(
            ("Local file", "Local title", "Duration", "Provider track", "Assignment evidence")
        )
        self.table.setModel(self._model)
        self.selectors: dict[str, QComboBox] = {}

        for row, source in enumerate(self._files):
            values = (
                source.path.name,
                source.read_result.metadata.title or "—",
                _format_duration(source.read_result.stream_info.duration_seconds),
                "",
                "",
            )
            cells = [QStandardItem(value) for value in values]

            for cell in cells:
                cell.setEditable(False)

            self._model.appendRow(cells)
            selector = QComboBox(self.table)
            selector.addItem("Unmapped", None)

            for index, track in enumerate(medium.tracks):
                titles = " / ".join(dict.fromkeys(title.value for title in track.titles)) or "Untitled track"
                selector.addItem(
                    f"{track.track_number or '?'}. {titles} · {_format_duration(track.duration_seconds)}",
                    index,
                )

            # A selector is bound to the local file ID. Provider combo data holds
            # a track index within the selected medium, not its printed number.
            self.selectors[source.file_id] = selector
            self.table.setIndexWidget(self._model.index(row, 3), selector)
            selector.currentIndexChanged.connect(partial(self._assignment_changed, source.file_id))

        layout.addWidget(self.table)
        self.evidence_label = QLabel(self)
        self.evidence_label.setTextFormat(Qt.TextFormat.PlainText)
        self.evidence_label.setWordWrap(True)
        layout.addWidget(self.evidence_label)
        self.error_label = QLabel(self)
        self.error_label.setTextFormat(Qt.TextFormat.PlainText)
        self.error_label.setWordWrap(True)
        layout.addWidget(self.error_label)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._refresh_mapping()
        configure_columns(self.table, (230, 180, 70, 280, 220))
        self.table.horizontalHeader().setStretchLastSection(True)
        fit_initial_size(self, 1100, 600)

    def mapping(self) -> TrackMappingResult:
        """Return the current mapping; callers publish it only after acceptance."""
        return self._mapping

    def _assignment_changed(self, local_file_id: str, _combo_index: int) -> None:
        provider_index = cast(int | None, self.selectors[local_file_id].currentData())

        try:
            changed = set_manual_track_assignment(
                self._files,
                self._candidate,
                self._mapping,
                local_file_id=local_file_id,
                provider_track_index=provider_index,
            )
        except (TypeError, ValueError) as error:
            # The application retains ownership of one-to-one validation. Restore
            # the last valid visual choice without emitting a second edit request.
            self.error_label.setText(str(error))
            self._refresh_mapping()
            return

        # The application returns a new mapping; the original captured mapping
        # stays intact if the user cancels after trying several assignments.
        self._mapping = changed
        self.error_label.clear()
        self._refresh_mapping()

    def _refresh_mapping(self) -> None:
        assignments = {item.local_file_id: item for item in self._mapping.mappings}

        for row, source in enumerate(self._files):
            assignment = assignments.get(source.file_id)
            selector = self.selectors[source.file_id]
            selected_index = 0 if assignment is None else selector.findData(assignment.provider_track_index)
            was_blocked = selector.blockSignals(True)

            try:
                selector.setCurrentIndex(selected_index)
            finally:
                selector.blockSignals(was_blocked)

            self._model.item(row, 4).setText(_assignment_evidence(assignment))

        self.evidence_label.setText(
            "\n".join(f"{item.code} — {item.detail}" for item in self._mapping.evidence)
        )
        self.table.resizeRowsToContents()
