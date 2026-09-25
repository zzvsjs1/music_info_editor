"""Expose QML-friendly roles around the shared table model projections."""

from typing import cast

from PySide6.QtCore import (
    Property,
    QAbstractItemModel,
    QByteArray,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    QSortFilterProxyModel,
    Qt,
    Signal,
    Slot,
)

from metadata_polisher.domain.metadata import MetadataField
from metadata_polisher.session.state import UnsupportedSelection
from metadata_polisher.ui.models.diff_model import PLACEHOLDER_ROLE, MetadataDiffModel
from metadata_polisher.ui.models.file_table_model import FileTableModel


class QuickTableModel(QSortFilterProxyModel):
    """Keep formatting and headers in the shared models; add presentation roles."""

    IDENTITY = Qt.ItemDataRole.UserRole + 1
    HIGHLIGHTED = IDENTITY + 1
    INCLUDED = IDENTITY + 2
    PLACEHOLDER = IDENTITY + 3
    sortingChanged = Signal()

    def __init__(self, source: QAbstractItemModel, parent: QObject, *, sortable: bool = False) -> None:
        super().__init__(parent)
        self._highlighted: frozenset[str] = frozenset()
        self._sortable = sortable
        self._sort_column = -1
        self._sort_descending = False
        # Explicit header sorting should not move a checkbox away from the
        # pointer as its inclusion state changes. Complete snapshot resets and
        # deliberate sort requests still apply the selected order.
        self.setDynamicSortFilter(False)
        self.setSourceModel(source)
        source.dataChanged.connect(self._source_data_changed)
        self._apply_sort()

    sortingEnabled = Property(bool, lambda self: self._sortable, constant=True)
    sortColumnIndex = Property(int, lambda self: self._sort_column, notify=sortingChanged)
    sortDescending = Property(bool, lambda self: self._sort_descending, notify=sortingChanged)
    sortLabel = Property(
        str,
        lambda self: (
            "Disc → Track → File" if isinstance(self.sourceModel(), FileTableModel)
            else "Original order"
        ) if self._sort_column < 0 else str(self.headerData(self._sort_column, Qt.Orientation.Horizontal)),
        notify=sortingChanged,
    )

    def _apply_sort(self) -> None:
        # Qt uses -1 to restore source order. The file default instead compares
        # a composite disc/track/name key through a real column in lessThan.
        column = self._sort_column

        if column < 0 and self._sortable and isinstance(self.sourceModel(), FileTableModel):
            column = 2

        self.invalidate()
        self.sort(column, Qt.SortOrder.DescendingOrder if self._sort_descending else Qt.SortOrder.AscendingOrder)

    @Slot(int, bool)
    def sortByColumn(self, column: int, descending: bool) -> None:
        if not self._sortable or not 0 <= column < self.columnCount():
            return

        self._sort_column = column
        self._sort_descending = descending
        self._apply_sort()
        self.sortingChanged.emit()

    @Slot()
    def restoreDefaultSort(self) -> None:
        if not self._sortable:
            return

        self._sort_column = -1
        self._sort_descending = False
        self._apply_sort()
        self.sortingChanged.emit()

    def lessThan(self, left: QModelIndex | QPersistentModelIndex, right: QModelIndex | QPersistentModelIndex) -> bool:
        source = self.sourceModel()

        if isinstance(source, FileTableModel):
            left_key = source.sort_key(left, library_order=self._sort_column < 0)
            right_key = source.sort_key(right, library_order=self._sort_column < 0)

            if left_key[0] != right_key[0] and self._sort_descending:
                # Qt reverses this comparison for descending order. Reverse
                # only the missing-value bucket here so absent numeric values
                # remain at the end in either direction, after known values.
                return left_key[0] > right_key[0]

            return left_key < right_key

        # Albums use case-insensitive labels, with their stable identity as a
        # deterministic tie breaker. Their source/session order stays intact.
        left_text, right_text = str(left.data() or ""), str(right.data() or "")

        return (left_text.casefold(), left_text, str(left.data(Qt.ItemDataRole.UserRole))) < (
            right_text.casefold(), right_text, str(right.data(Qt.ItemDataRole.UserRole)),
        )

    def visible_ids(self) -> tuple[str, ...]:
        """Resolve current visual rows to stable identities for navigation."""
        return tuple(str(self.index(row, 0).data(self.IDENTITY)) for row in range(self.rowCount()))

    def roleNames(self) -> dict[int, QByteArray]:
        return {
            Qt.ItemDataRole.DisplayRole: QByteArray(b"display"),
            self.IDENTITY: QByteArray(b"stableId"),
            self.HIGHLIGHTED: QByteArray(b"highlighted"),
            self.INCLUDED: QByteArray(b"included"),
            self.PLACEHOLDER: QByteArray(b"placeholder"),
            Qt.ItemDataRole.ToolTipRole: QByteArray(b"tooltip"),
        }

    # The shared source model owns labels and column order. QML must not keep a
    # second translated list which can drift from the table it actually displays.
    columnTitles = Property(
        list,
        lambda self: [str(self.headerData(column, Qt.Orientation.Horizontal)) for column in range(self.columnCount())],
        constant=True,
    )

    def data(
        self,
        index: QModelIndex | QPersistentModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> object | None:
        if not index.isValid():
            return None

        if role in {self.IDENTITY, self.HIGHLIGHTED}:
            value = super().data(index, Qt.ItemDataRole.UserRole)
            identity = (
                value.value
                if isinstance(value, MetadataField)
                else "@unsupported"
                if isinstance(value, UnsupportedSelection)
                else str(value)
            )
            return identity if role == self.IDENTITY else identity in self._highlighted

        if role == self.INCLUDED:
            # The shared file model deliberately accepts Qt check states, not
            # arbitrary booleans. QML commands go through the backend instead.
            first = self.index(index.row(), 0)
            return bool(super().data(first, Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Checked)

        if role == self.PLACEHOLDER:
            # Sources without typed absence simply return None. This lets every
            # table share one delegate without guessing from visible strings.
            return bool(super().data(index, PLACEHOLDER_ROLE))

        if role == Qt.ItemDataRole.ToolTipRole and not isinstance(self.sourceModel(), MetadataDiffModel):
            # Library models supply row diagnostics, whereas the review model
            # already includes the cell value. Preserve complete elided library
            # names before appending those diagnostics to the shared tooltip.
            value = str(super().data(index, Qt.ItemDataRole.DisplayRole) or "")
            details = super().data(index, role)

            return f"{value}\n{details}" if details else value

        return cast(object, super().data(index, role))

    def setData(
        self,
        index: QModelIndex | QPersistentModelIndex,
        value: object,
        role: int = Qt.ItemDataRole.EditRole,
    ) -> bool:
        # A delegate must not bypass stable-ID commands or review validation.
        return False

    def set_highlighted(self, identities: frozenset[str]) -> None:
        if identities == self._highlighted:
            return

        self._highlighted = identities
        self._notify_roles([self.HIGHLIGHTED])

    def _notify_roles(self, roles: list[int]) -> None:
        if self.rowCount() and self.columnCount():
            self.dataChanged.emit(self.index(0, 0), self.index(self.rowCount() - 1, self.columnCount() - 1), roles)

    def _source_data_changed(self, _first: QModelIndex, _last: QModelIndex, roles: list[int]) -> None:
        # QSortFilterProxyModel forwards the original roles. Notify the additional
        # Boolean role too, otherwise a reused checkbox can keep its old value.
        if not roles or Qt.ItemDataRole.CheckStateRole in roles:
            self._notify_roles([self.INCLUDED])
