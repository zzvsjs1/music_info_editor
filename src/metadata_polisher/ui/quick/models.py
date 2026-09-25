"""Expose QML-friendly roles around the shared table model projections."""

from typing import cast

from PySide6.QtCore import (
    Property,
    QAbstractItemModel,
    QByteArray,
    QIdentityProxyModel,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    Qt,
)

from metadata_polisher.domain.metadata import MetadataField
from metadata_polisher.session.state import UnsupportedSelection


class QuickTableModel(QIdentityProxyModel):
    """Keep formatting and headers in the shared models; add presentation roles."""

    IDENTITY = Qt.ItemDataRole.UserRole + 1
    HIGHLIGHTED = IDENTITY + 1
    INCLUDED = IDENTITY + 2

    def __init__(self, source: QAbstractItemModel, parent: QObject) -> None:
        super().__init__(parent)
        self._highlighted: frozenset[str] = frozenset()
        self.setSourceModel(source)
        source.dataChanged.connect(self._source_data_changed)

    def roleNames(self) -> dict[int, QByteArray]:
        return {
            Qt.ItemDataRole.DisplayRole: QByteArray(b"display"),
            self.IDENTITY: QByteArray(b"stableId"),
            self.HIGHLIGHTED: QByteArray(b"highlighted"),
            self.INCLUDED: QByteArray(b"included"),
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
        # QIdentityProxyModel forwards the original roles. Notify the additional
        # Boolean role too, otherwise a reused checkbox can keep its old value.
        if not roles or Qt.ItemDataRole.CheckStateRole in roles:
            self._notify_roles([self.INCLUDED])
