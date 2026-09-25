"""Read-only file rows derived from one immutable group snapshot."""

from dataclasses import dataclass

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QObject, QPersistentModelIndex, Qt, Signal

from metadata_polisher.application.changes import ChangeSetStatus
from metadata_polisher.domain.media import LocalMediaFile, UnsupportedMediaFile
from metadata_polisher.domain.metadata import FieldReadState, Position
from metadata_polisher.session.state import GroupState, ReviewedFileState, VerifiedWriteReceipt

# The invalid parent is read-only and represents this flat model's root.
_ROOT_INDEX = QModelIndex()
_NO_INCLUDED_FILES: frozenset[str] = frozenset()

# Every column produces the same comparable shape. Numeric components stay
# numeric; the final name and identity components make equal values stable.
type FileSortKey = tuple[int, float, int, float, str, str, str]

FILE_TABLE_HEADERS = (
    "Include",
    "Status",
    "File",
    "Track",
    "Disc",
    "Title",
    "Artist",
    "Composer",
    "Duration",
    "Format",
    "Match",
    "Rename",
)


@dataclass(frozen=True)
class FileTableRow:
    """One immutable visible row keyed by a scanned file ID."""

    file_id: str
    values: tuple[str, ...]
    tooltip: str
    can_include: bool = True
    track_number: int | None = None
    disc_number: int | None = None
    duration_seconds: float | None = None
    match_score: float | None = None


def _format_position(position: Position) -> str:
    if position.number is None and position.total is None:
        return "—"

    if position.total is None:
        return str(position.number)

    return f"{position.number or '—'}/{position.total}"


def _format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"

    rounded = max(0, round(seconds))
    # Round the complete duration first: 59.6 seconds becomes 1:00 rather than
    # leaving a displayed seconds component of 60 beside the previous minute.
    minutes, remaining = divmod(rounded, 60)

    return f"{minutes}:{remaining:02d}"


def _file_status(
    source: LocalMediaFile,
    reviewed: ReviewedFileState | None,
    written: bool = False,
) -> tuple[str, str]:
    # Current read/validation problems and pending decisions take priority over
    # an earlier write receipt, so Written never hides newly required review.
    read_states = source.read_result.field_states.values()

    if source.read_result.issues or any(
        state is FieldReadState.UNREADABLE for state in read_states
    ):
        return "✖", "✖ Read error"

    if reviewed is None or reviewed.change_set is None:
        if written:
            return "✓", "✓ Written"

        return "○", "○ Not reviewed"

    change_set = reviewed.change_set

    if change_set.status is ChangeSetStatus.BLOCKED:
        return "✖", "✖ Blocked"

    if (
        not reviewed.track_mapping_resolved
        or any(review.requires_review for review in reviewed.reviews)
    ):
        return "!", "! Needs review"

    if change_set.metadata_changes or change_set.rename_change is not None:
        return "✓", "+ Ready"

    if written:
        return "✓", "✓ Written"

    return "—", "— Unchanged"


def _row_tooltip(
    source: LocalMediaFile,
    reviewed: ReviewedFileState | None,
) -> str:
    codes = [issue.code.value for issue in source.read_result.issues]
    codes.extend(
        f"{field.value.upper()}_{state.value.upper()}"
        for field, state in source.read_result.field_states.items()
        if state in {FieldReadState.UNREADABLE, FieldReadState.UNSUPPORTED}
    )

    if reviewed is not None and reviewed.change_set is not None:
        codes.extend(issue.code.value for issue in reviewed.change_set.validation.issues)

    if reviewed is not None:
        codes.extend(
            code.value
            for review in reviewed.reviews
            for code in review.reason_codes
        )

    unique_codes = tuple(dict.fromkeys(codes))

    if unique_codes:
        return "\n".join(unique_codes)

    return "No review state is available." if reviewed is None else "No review warnings."


def _file_rows(group: GroupState, written_files: tuple[VerifiedWriteReceipt, ...] = ()) -> tuple[FileTableRow, ...]:
    reviewed_by_id = {reviewed.file_id: reviewed for reviewed in group.reviewed_files}
    mapping_by_id = (
        {
            mapping.local_file_id: mapping
            for mapping in group.effective_track_mapping.mappings
        }
        if group.effective_track_mapping is not None
        else {}
    )
    rows: list[FileTableRow] = []

    for source in group.group.files:
        metadata = source.read_result.metadata
        reviewed = reviewed_by_id.get(source.file_id)
        written = any(receipt.source == source for receipt in written_files)
        _apply_text, status = _file_status(source, reviewed, written)
        mapping = mapping_by_id.get(source.file_id)
        match = (
            f"{mapping.classification.value.title()} ({mapping.score:.0f})"
            if mapping is not None
            else "—"
        )

        if mapping is not None and "MANUAL_TRACK_ASSIGNMENT" in mapping.reason_codes:
            match = "Manual"
        rename = "—"

        if reviewed is not None and reviewed.change_set is not None:
            preview = reviewed.change_set.rename_preview

            if preview is not None:
                rename = preview.new_path.name

        rows.append(
            FileTableRow(
                file_id=source.file_id,
                values=(
                    "",
                    status,
                    source.path.name,
                    _format_position(metadata.track),
                    _format_position(metadata.disc),
                    metadata.title or "—",
                    "; ".join(metadata.artists) or "—",
                    "; ".join(metadata.composers) or "—",
                    _format_duration(source.read_result.stream_info.duration_seconds),
                    source.format_id.upper(),
                    match,
                    rename,
                ),
                tooltip=_row_tooltip(source, reviewed),
                can_include=not group.requires_rescan,
                track_number=metadata.track.number,
                disc_number=metadata.disc.number,
                duration_seconds=source.read_result.stream_info.duration_seconds,
                match_score=mapping.score if mapping is not None else None,
            )
        )

    return tuple(rows)


class FileTableModel(QAbstractTableModel):
    """Thin model refreshed by replacing one complete GroupState snapshot."""

    inclusion_requested = Signal(str, bool)

    def __init__(
        self,
        group: GroupState | None = None,
        parent: QObject | None = None,
        *,
        included_file_ids: frozenset[str] = _NO_INCLUDED_FILES,
    ) -> None:
        super().__init__(parent)
        self._rows = _file_rows(group) if group is not None else ()
        self._included_file_ids = included_file_ids
        self._inclusion_enabled = True

    def set_included_file_ids(self, file_ids: frozenset[str]) -> None:
        """Refresh checkbox projection from the owning window's in-memory scope."""
        if self._included_file_ids == file_ids:
            return

        self._included_file_ids = frozenset(file_ids)

        if self._rows:
            self.dataChanged.emit(
                self.index(0, 0), self.index(len(self._rows) - 1, 0), [Qt.ItemDataRole.CheckStateRole],
            )

    def set_inclusion_enabled(self, enabled: bool) -> None:
        """Freeze checkbox interaction while a confirmed operation is running."""
        if self._inclusion_enabled == enabled:
            return

        self._inclusion_enabled = enabled

        if self._rows:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self._rows) - 1, 0))

    def set_group_state(self, group: GroupState | None, written_files: tuple[VerifiedWriteReceipt, ...] = ()) -> None:
        if group is not None and not isinstance(group, GroupState):
            raise TypeError("group must be a GroupState or None")

        # Reset brackets tell Qt that all row objects have been replaced. The
        # window restores highlighting by file ID after this projection changes.
        self.beginResetModel()
        self._rows = _file_rows(group, written_files) if group is not None else ()
        self.endResetModel()

    def set_unsupported_files(self, files: tuple[UnsupportedMediaFile, ...]) -> None:
        """Display recognised unsupported media without metadata or write choices."""
        sources = tuple(files)

        if any(not isinstance(source, UnsupportedMediaFile) for source in sources):
            raise TypeError("files must contain only UnsupportedMediaFile values")

        # Keep the usual columns so switching collections does not change the
        # table layout. Unsupported files have no reviewed metadata to apply.
        rows = tuple(
            FileTableRow(
                file_id=str(source.path),
                values=(
                    "—",
                    "Not supported yet",
                    source.path.name,
                    "—",
                    "—",
                    "—",
                    "—",
                    "—",
                    "—",
                    source.path.suffix.removeprefix(".").upper(),
                    "—",
                    "—",
                ),
                tooltip=f"Not supported yet\n{source.path}",
                can_include=False,
            )
            for source in sources
        )
        self.beginResetModel()
        self._rows = rows
        self.endResetModel()

    def rowCount(self, parent: QModelIndex | QPersistentModelIndex = _ROOT_INDEX) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex | QPersistentModelIndex = _ROOT_INDEX) -> int:
        return 0 if parent.isValid() else len(FILE_TABLE_HEADERS)

    def sort_key(self, index: QModelIndex | QPersistentModelIndex, *, library_order: bool = False) -> FileSortKey:
        """Return typed presentation keys without reordering scanned sources."""
        row = self._rows[index.row()]
        filename = row.values[2]
        tie = (filename.casefold(), filename, row.file_id)

        if library_order:
            # A track with no disc belongs to the usual first-disc sequence.
            # A known disc without a track follows that disc's numbered tracks;
            # completely unnumbered files follow all numbered material by name.
            return (
                int(row.disc_number is None and row.track_number is None),
                row.disc_number if row.disc_number is not None else 1,
                int(row.track_number is None),
                row.track_number if row.track_number is not None else 0,
                *tie,
            )

        column = index.column()

        if column == 0:
            return (0, int(row.file_id in self._included_file_ids), 0, 0, *tie)

        numeric = {
            3: row.track_number,
            4: row.disc_number,
            8: row.duration_seconds,
            10: row.match_score,
        }

        if column in numeric:
            value = numeric[column]

            return (int(value is None), value if value is not None else 0, 0, 0, *tie)

        text = row.values[column]

        return (0, 0, 0, 0, text.casefold(), text, row.file_id)

    def data(
        self,
        index: QModelIndex | QPersistentModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> object | None:
        if (
            not index.isValid()
            or index.model() is not self
            or not 0 <= index.row() < len(self._rows)
            or not 0 <= index.column() < len(FILE_TABLE_HEADERS)
        ):
            return None

        row = self._rows[index.row()]

        if role == Qt.ItemDataRole.DisplayRole:
            return row.values[index.column()]

        if role == Qt.ItemDataRole.CheckStateRole and index.column() == 0 and row.can_include:
            # Membership is looked up by identity, so reordering rows cannot move
            # an existing write checkbox onto a different audio file.
            return Qt.CheckState.Checked if row.file_id in self._included_file_ids else Qt.CheckState.Unchecked

        if role == Qt.ItemDataRole.AccessibleTextRole and index.column() == 0:
            return (
                "Included in the next write batch" if row.file_id in self._included_file_ids
                else "Excluded from writing"
            )

        if role == Qt.ItemDataRole.ToolTipRole:
            return row.tooltip

        if role == Qt.ItemDataRole.UserRole:
            return row.file_id

        return None

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> object | None:
        if (
            role == Qt.ItemDataRole.DisplayRole
            and orientation == Qt.Orientation.Horizontal
            and 0 <= section < len(FILE_TABLE_HEADERS)
        ):
            return FILE_TABLE_HEADERS[section]

        return None

    def flags(self, index: QModelIndex | QPersistentModelIndex) -> Qt.ItemFlag:
        if (
            not index.isValid()
            or index.model() is not self
            or not 0 <= index.row() < len(self._rows)
            or not 0 <= index.column() < len(FILE_TABLE_HEADERS)
        ):
            return Qt.ItemFlag.NoItemFlags

        flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

        if index.column() == 0 and self._rows[index.row()].can_include and self._inclusion_enabled:
            flags |= Qt.ItemFlag.ItemIsUserCheckable

        return flags

    def setData(
        self,
        index: QModelIndex | QPersistentModelIndex,
        value: object,
        role: int = Qt.ItemDataRole.EditRole,
    ) -> bool:
        if (
            role != Qt.ItemDataRole.CheckStateRole or index.column() != 0
            or not self.flags(index) & Qt.ItemFlag.ItemIsUserCheckable
            or isinstance(value, bool)
            or value not in (Qt.CheckState.Checked, Qt.CheckState.Unchecked,
                             Qt.CheckState.Checked.value, Qt.CheckState.Unchecked.value)
        ):
            return False

        # A checkbox requests an inclusion change using the stable file identity.
        # The owner updates the projection; no metadata or ChangeSet is edited here.
        self.inclusion_requested.emit(
            self._rows[index.row()].file_id, value in (Qt.CheckState.Checked, Qt.CheckState.Checked.value),
        )

        return True
