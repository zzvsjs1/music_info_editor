"""Read-only group rows derived from immutable session state."""

from dataclasses import dataclass
from enum import StrEnum

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QObject, QPersistentModelIndex, Qt

from metadata_polisher.application.changes import ChangeSetStatus
from metadata_polisher.domain.errors import MatchingErrorCode
from metadata_polisher.domain.metadata import FieldReadState
from metadata_polisher.session.state import GroupState, SessionState, UnsupportedSelection

GROUP_HEADERS = ("Album", "Status")

# These flat models only inspect the parent, so one invalid value can safely
# represent the root without constructing a Qt object in each default argument.
_ROOT_INDEX = QModelIndex()


class GroupPresentationStatus(StrEnum):
    """Typed group classifications shared by rows and summary counts."""

    RESCAN_REQUIRED = "✖ Rescan required"
    METADATA_ERROR = "✖ Metadata error"
    READ_ERROR = "✖ Read error"
    BLOCKED = "✖ Blocked"
    NEEDS_REVIEW = "! Needs review"
    NOT_SEARCHED = "○ Not searched"
    NO_SUITABLE_MATCH = "○ No suitable match"
    INCOMPLETE = "△ Incomplete"
    COMPLETE = "✓ Complete"


@dataclass(frozen=True)
class GroupRow:
    """One album or unsupported collection row with an unambiguous identity."""

    group_id: str | UnsupportedSelection
    title: str
    status: str
    tooltip: str


def classify_group(group: GroupState) -> GroupPresentationStatus:
    """Derive one deterministic state without parsing user-facing text."""
    # This is a precedence list, not a collection of independent badges. Stop at
    # the first problem so stale/read-blocked groups cannot appear complete just
    # because candidate lookup or some field reviews succeeded earlier.
    if group.requires_rescan:
        return GroupPresentationStatus.RESCAN_REQUIRED

    if group.selection_failure is not None:
        return GroupPresentationStatus.METADATA_ERROR

    if any(source.read_result.issues for source in group.group.files) or any(
        state is FieldReadState.UNREADABLE
        for source in group.group.files
        for state in source.read_result.field_states.values()
    ):
        return GroupPresentationStatus.READ_ERROR

    change_sets = tuple(
        reviewed.change_set
        for reviewed in group.reviewed_files
        if reviewed.change_set is not None
    )

    if any(change_set.status is ChangeSetStatus.BLOCKED for change_set in change_sets):
        return GroupPresentationStatus.BLOCKED

    if (
        group.warnings
        or any(
            state is FieldReadState.UNSUPPORTED
            for source in group.group.files
            for state in source.read_result.field_states.values()
        )
        or any(not reviewed.track_mapping_resolved for reviewed in group.reviewed_files)
        or any(
            review.requires_review
            for reviewed in group.reviewed_files
            for review in reviewed.reviews
        )
    ):
        return GroupPresentationStatus.NEEDS_REVIEW

    if group.release_ranking is not None and group.release_ranking.ambiguous:
        return GroupPresentationStatus.NEEDS_REVIEW

    if group.candidate_lookup is None:
        return GroupPresentationStatus.NOT_SEARCHED

    matching_codes = {
        issue.code for issue in group.candidate_lookup.matching_issues
    }

    if (
        not group.candidate_lookup.release_ranking.entries
        or MatchingErrorCode.NO_CANDIDATE in matching_codes
        or MatchingErrorCode.INSUFFICIENT_EVIDENCE in matching_codes
    ):
        return GroupPresentationStatus.NO_SUITABLE_MATCH

    if (
        group.selected_release is None
        or group.automatic_track_mapping is None
        or len(group.reviewed_files) != len(group.group.files)
        or any(reviewed.change_set is None for reviewed in group.reviewed_files)
    ):
        return GroupPresentationStatus.INCOMPLETE

    # Complete describes the review pipeline's coverage; it is not proof that
    # metadata was written. Disk success is tracked separately by write receipts.
    return GroupPresentationStatus.COMPLETE


def _group_row(group: GroupState) -> GroupRow:
    title = group.group.album_title or group.group.files[0].path.parent.name or "Untitled album"
    status = classify_group(group).value
    tooltip_lines = (
        f"Group: {group.group.group_id}",
        f"Files: {len(group.group.files)}",
        f"Status: {status}",
    )

    return GroupRow(
        group_id=group.group.group_id,
        title=title,
        status=status,
        tooltip="\n".join(tooltip_lines),
    )


def _group_rows(state: SessionState) -> tuple[GroupRow, ...]:
    rows = tuple(_group_row(group) for group in state.groups)

    if not state.unsupported_files:
        return rows

    # Unsupported files remain a separate session collection. A typed identity
    # avoids inventing an album ID that could collide with a real scanned group.
    return (
        *rows,
        GroupRow(
            group_id=UnsupportedSelection(),
            title="Unsupported files",
            status="Not supported yet",
            tooltip=f"Files: {len(state.unsupported_files)}\nNot supported yet",
        ),
    )


class GroupListModel(QAbstractTableModel):
    """Thin table model whose rows are replaced from a complete SessionState."""

    def __init__(
        self,
        state: SessionState | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._rows = _group_rows(state) if state is not None else ()

    def set_session_state(self, state: SessionState) -> None:
        if not isinstance(state, SessionState):
            raise TypeError("state must be a SessionState")

        self.beginResetModel()
        self._rows = _group_rows(state)
        self.endResetModel()

    def rowCount(self, parent: QModelIndex | QPersistentModelIndex = _ROOT_INDEX) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex | QPersistentModelIndex = _ROOT_INDEX) -> int:
        return 0 if parent.isValid() else len(GROUP_HEADERS)

    def data(
        self,
        index: QModelIndex | QPersistentModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> object | None:
        if (
            not index.isValid()
            or index.model() is not self
            or not 0 <= index.row() < len(self._rows)
            or not 0 <= index.column() < len(GROUP_HEADERS)
        ):
            return None

        row = self._rows[index.row()]

        if role == Qt.ItemDataRole.DisplayRole:
            return (row.title, row.status)[index.column()]

        if role == Qt.ItemDataRole.ToolTipRole:
            return row.tooltip

        if role == Qt.ItemDataRole.UserRole:
            # Album names are neither unique nor immutable; navigation must use
            # this group identity even when two visible titles are identical.
            return row.group_id

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
            and 0 <= section < len(GROUP_HEADERS)
        ):
            return GROUP_HEADERS[section]

        return None

    def flags(self, index: QModelIndex | QPersistentModelIndex) -> Qt.ItemFlag:
        if (
            not index.isValid()
            or index.model() is not self
            or not 0 <= index.row() < len(self._rows)
            or not 0 <= index.column() < len(GROUP_HEADERS)
        ):
            return Qt.ItemFlag.NoItemFlags

        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    def setData(
        self,
        index: QModelIndex | QPersistentModelIndex,
        value: object,
        role: int = Qt.ItemDataRole.EditRole,
    ) -> bool:
        del index, value, role

        return False
