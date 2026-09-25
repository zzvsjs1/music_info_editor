"""Read-only metadata diff rows for one selected reviewed file."""

from dataclasses import dataclass
from typing import cast

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QObject, QPersistentModelIndex, Qt

from metadata_polisher.domain.media import LocalMediaFile
from metadata_polisher.domain.metadata import (
    FieldReadState,
    MetadataField,
    MetadataSnapshot,
    Position,
)
from metadata_polisher.domain.review import FieldDecisionKind, FieldReviewState, FieldValue
from metadata_polisher.session.review_editing import AggregatedValue, AggregateValueState, aggregate_review_fields
from metadata_polisher.session.state import ReviewedFileState, SessionState, VerifiedWriteReceipt

# The invalid parent is read-only and represents this flat model's root.
_ROOT_INDEX = QModelIndex()

# Keep semantic absence separate from display text: an actual title can be
# called Empty or No suggestion and must still look like ordinary metadata.
PLACEHOLDER_ROLE = Qt.ItemDataRole.UserRole + 10

DIFF_HEADERS = (
    "Field",
    "Status",
    "Existing",
    "Proposed",
    "Final",
    "Sources",
)

FIELD_LABELS = {
    MetadataField.TITLE: "Title",
    MetadataField.ARTISTS: "Artists",
    MetadataField.ALBUM: "Album",
    MetadataField.ALBUM_ARTISTS: "Album artists",
    MetadataField.COMPOSERS: "Composers",
    MetadataField.TRACK: "Track",
    MetadataField.DISC: "Disc",
    MetadataField.DATE: "Date",
    MetadataField.GENRES: "Genres",
}


@dataclass(frozen=True)
class DiffRow:
    """One managed field projection keyed by its MetadataField enum member."""

    field: MetadataField
    values: tuple[str, ...]
    tooltip: str
    placeholder_columns: frozenset[int] = frozenset()


def format_field_value(value: FieldValue | None) -> str:
    if value is None:
        return "—"

    if isinstance(value, tuple):
        return "; ".join(value) or "—"

    if isinstance(value, Position):
        if value.number is None and value.total is None:
            return "—"

        if value.total is None:
            return str(value.number) if value.number is not None else "—"

        return f"{value.number or '—'}/{value.total}"

    return value


def _is_empty(value: FieldValue | None) -> bool:
    return value is None or value == () or value == Position() or value == ""


def _display_value(value: FieldValue | None, empty_label: str = "Empty") -> str:
    return empty_label if _is_empty(value) else format_field_value(value)


def _existing_text(value: FieldValue | None, read_state: FieldReadState) -> str:
    # A read failure is unavailable evidence, never an empty field which could
    # safely receive an addition. Preserve this distinction in the value cell.
    if read_state in {FieldReadState.UNREADABLE, FieldReadState.UNSUPPORTED}:
        return "Unreadable" if read_state is FieldReadState.UNREADABLE else "Unsupported"

    return _display_value(value)


def _final_value(review: FieldReviewState) -> FieldValue | None:
    if review.decision is FieldDecisionKind.USE_PROPOSAL:
        return review.selected_proposal.value if review.selected_proposal is not None else None

    if review.decision is FieldDecisionKind.USE_MANUAL:
        return review.manual_value

    if review.decision is FieldDecisionKind.CLEAR:
        return None

    return review.existing_value


def _sources(review: FieldReviewState) -> str:
    proposal = review.proposals[0] if review.proposals else None

    if proposal is None:
        return "—"

    labels = tuple(
        dict.fromkeys(
            f"{member.provenance.engine_id} / {member.provenance.source_id}"
            for member in proposal.members
        )
    )

    return "; ".join(labels)


def _status(review: FieldReviewState) -> str:
    if review.read_state in {FieldReadState.UNREADABLE, FieldReadState.UNSUPPORTED}:
        return "⛔ Blocked"

    if review.requires_review:
        return "Needs review"

    # Describe the review decision rather than deriving an action from whether
    # the final value happens to equal the existing value. A deliberate edit can
    # be a semantic no-op and is still a completed review choice.
    return {
        FieldDecisionKind.USE_PROPOSAL: "Accepted",
        FieldDecisionKind.KEEP_EXISTING: "Kept",
        FieldDecisionKind.USE_MANUAL: "Edited",
        FieldDecisionKind.CLEAR: "Cleared",
        FieldDecisionKind.UNRESOLVED: "Needs review",
    }[review.decision]


def _tooltip(review: FieldReviewState) -> str:
    codes = tuple(
        dict.fromkeys(
            code.value
            for code in (
                *review.reason_codes,
                *(code for proposal in review.proposals for code in proposal.reason_codes),
                *(
                    code
                    for proposal in review.proposals
                    for member in proposal.members
                    for code in member.reason_codes
                ),
            )
        )
    )

    return "\n".join(codes) if codes else "No review reason codes."


def _metadata_value(metadata: MetadataSnapshot, field: MetadataField) -> FieldValue | None:
    if field is MetadataField.TITLE:
        return metadata.title

    if field is MetadataField.ARTISTS:
        return metadata.artists

    if field is MetadataField.ALBUM:
        return metadata.album

    if field is MetadataField.ALBUM_ARTISTS:
        return metadata.album_artists

    if field is MetadataField.COMPOSERS:
        return metadata.composers

    if field is MetadataField.TRACK:
        return metadata.track

    if field is MetadataField.DISC:
        return metadata.disc

    if field is MetadataField.DATE:
        return metadata.date

    return metadata.genres


def _diff_rows(
    source: LocalMediaFile,
    reviewed: ReviewedFileState | None,
    written_files: tuple[VerifiedWriteReceipt, ...] = (),
) -> tuple[DiffRow, ...]:
    # Match the complete refreshed source, not just its reusable file ID. A receipt
    # for an older disk snapshot cannot certify the values currently on display.
    written_fields = frozenset(field for receipt in written_files if receipt.source == source
                               for field in receipt.changed_fields)
    reviews_by_field = (
        {review.field: review for review in reviewed.reviews}
        if reviewed is not None
        else {}
    )
    rows: list[DiffRow] = []

    for field in MetadataField:
        review = reviews_by_field.get(field)

        if review is None:
            read_state = source.read_result.field_states[field]
            # A latent value in the snapshot is not readable evidence when the
            # reader marked this field missing, unsupported or unreadable.
            existing = (
                _metadata_value(source.read_result.metadata, field)
                if read_state is FieldReadState.PRESENT
                else None
            )
            status = {
                FieldReadState.PRESENT: "Needs review",
                FieldReadState.MISSING: "Needs review",
                FieldReadState.UNREADABLE: "✖ Unreadable",
                FieldReadState.UNSUPPORTED: "! Unsupported",
            }[read_state]
            if field in written_fields:
                status = "✓ Written"
            rows.append(
                DiffRow(
                    field,
                    (
                        FIELD_LABELS[field],
                        status,
                        _existing_text(existing, read_state),
                        "No suggestion",
                        _existing_text(existing, read_state),
                        "—",
                    ),
                    f"Read state: {read_state.value}",
                    frozenset({2, 3, 4} if read_state is FieldReadState.MISSING else {3}),
                )
            )

            continue

        recommended = review.proposals[0].value if review.proposals else None
        final = _final_value(review)

        # Position decisions can supply only one component. The ChangeSet has
        # already resolved the retained number/total, so it is the final preview's
        # authority just as it is the writer's authority.
        if reviewed is not None and reviewed.change_set is not None:
            final = cast(FieldValue | None, next(
                (change.new_value for change in reviewed.change_set.metadata_changes if change.field is field),
                review.existing_value,
            ))

        status = _status(review)

        # A receipt describes the last verified disk result. Any pending edit or
        # unresolved review takes precedence over that historical acknowledgement.
        if field in written_fields and status == "Kept" and final == review.existing_value:
            status = "✓ Written"

        readable = review.read_state in {FieldReadState.PRESENT, FieldReadState.MISSING}
        placeholders = frozenset(
            column for column, absent in (
                (2, readable and _is_empty(review.existing_value)),
                (3, not review.proposals),
                (4, readable and _is_empty(final)),
            ) if absent
        )

        rows.append(
            DiffRow(
                field=field,
                values=(
                    FIELD_LABELS[field],
                    status,
                    _existing_text(review.existing_value, review.read_state),
                    _display_value(recommended, "No suggestion"),
                    _existing_text(final, review.read_state),
                    _sources(review),
                ),
                tooltip=f"{_tooltip(review)}\nSources: {_sources(review)}",
                placeholder_columns=placeholders,
            )
        )

    return tuple(rows)


class MetadataDiffModel(QAbstractTableModel):
    """Thin six-column model preserving MetadataField declaration order."""

    def __init__(
        self,
        source_file: LocalMediaFile | None = None,
        reviewed_file: ReviewedFileState | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        _validate_file_pair(source_file, reviewed_file)
        self._rows = (
            _diff_rows(source_file, reviewed_file)
            if source_file is not None
            else ()
        )

    def set_file(
        self,
        source_file: LocalMediaFile | None,
        reviewed_file: ReviewedFileState | None,
        written_files: tuple[VerifiedWriteReceipt, ...] = (),
    ) -> None:
        _validate_file_pair(source_file, reviewed_file)

        self.beginResetModel()
        self._rows = (
            _diff_rows(source_file, reviewed_file, written_files)
            if source_file is not None
            else ()
        )
        self.endResetModel()

    def set_selection(self, state: SessionState, file_ids: tuple[str, ...]) -> None:
        """Present heterogeneous values without manufacturing writable text."""
        aggregate = aggregate_review_fields(state, file_ids, tuple(MetadataField))
        per_file = {
            source.file_id: _diff_rows(source, next((reviewed for reviewed in group.reviewed_files
                                                     if reviewed.file_id == source.file_id), None), state.written_files)
            for group in state.groups for source in group.group.files if source.file_id in file_ids
        }

        def text(value: AggregatedValue, *, proposed: bool = False) -> str:
            # Mixed is only a display label. The aggregate retains its typed state
            # so a batch command still resolves each file's own semantic value.
            if value.state in {AggregateValueState.MISSING, AggregateValueState.EMPTY}:
                return "No suggestion" if proposed else "Empty"

            return (format_field_value(value.value) if value.state is AggregateValueState.VALUE
                    else "Mixed values" if value.state is AggregateValueState.MIXED
                    else value.state.value.capitalize())

        rows = []

        for index, item in enumerate(aggregate):
            statuses = tuple(dict.fromkeys(rows[index].values[1] for rows in per_file.values()))
            details = "\n".join(f"{file_id}: {rows[index].values[2]} → {rows[index].values[4]}"
                                for file_id, rows in per_file.items())
            rows.append(DiffRow(item.field, (
                FIELD_LABELS[item.field], " / ".join(statuses), text(item.existing), text(item.proposed, proposed=True),
                text(item.final), "Per-file sources in details",
            ), details, frozenset(
                column for column, value in ((2, item.existing), (3, item.proposed), (4, item.final))
                if value.state in {AggregateValueState.MISSING, AggregateValueState.EMPTY}
            )))

        self.beginResetModel()
        self._rows = tuple(rows)
        self.endResetModel()

    def rowCount(self, parent: QModelIndex | QPersistentModelIndex = _ROOT_INDEX) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex | QPersistentModelIndex = _ROOT_INDEX) -> int:
        return 0 if parent.isValid() else len(DIFF_HEADERS)

    def data(
        self,
        index: QModelIndex | QPersistentModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> object | None:
        if (
            not index.isValid()
            or index.model() is not self
            or not 0 <= index.row() < len(self._rows)
            or not 0 <= index.column() < len(DIFF_HEADERS)
        ):
            return None

        row = self._rows[index.row()]

        if role in {Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.AccessibleTextRole}:
            return row.values[index.column()]

        if role == Qt.ItemDataRole.ToolTipRole:
            # The cell's complete text stays inspectable when the table elides
            # it, followed by the existing provenance and review diagnostics.
            return f"{DIFF_HEADERS[index.column()]}: {row.values[index.column()]}\n{row.tooltip}"

        if role == PLACEHOLDER_ROLE:
            return index.column() in row.placeholder_columns

        if role == Qt.ItemDataRole.UserRole:
            # Controllers retain this enum across resets; visible row positions
            # belong only to the current projection and are not review identities.
            return row.field

        return None

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> object | None:
        if role != Qt.ItemDataRole.DisplayRole:
            return None

        if orientation == Qt.Orientation.Horizontal and 0 <= section < len(DIFF_HEADERS):
            return DIFF_HEADERS[section]

        if orientation == Qt.Orientation.Vertical and 0 <= section < len(self._rows):
            return FIELD_LABELS[self._rows[section].field]

        return None

    def flags(self, index: QModelIndex | QPersistentModelIndex) -> Qt.ItemFlag:
        if (
            not index.isValid()
            or index.model() is not self
            or not 0 <= index.row() < len(self._rows)
            or not 0 <= index.column() < len(DIFF_HEADERS)
        ):
            return Qt.ItemFlag.NoItemFlags

        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    def setData(
        self,
        index: QModelIndex | QPersistentModelIndex,
        value: object,
        role: int = Qt.ItemDataRole.EditRole,
    ) -> bool:
        # Editing a cell cannot bypass the explicit review command and ChangeSet
        # validation path, including when Qt invokes setData programmatically.
        del index, value, role

        return False


def _validate_file_pair(
    source_file: LocalMediaFile | None,
    reviewed_file: ReviewedFileState | None,
) -> None:
    if source_file is not None and not isinstance(source_file, LocalMediaFile):
        raise TypeError("source_file must be a LocalMediaFile or None")

    if reviewed_file is not None and not isinstance(reviewed_file, ReviewedFileState):
        raise TypeError("reviewed_file must be a ReviewedFileState or None")

    if source_file is None and reviewed_file is not None:
        raise ValueError("reviewed_file requires a source_file")

    if (
        source_file is not None
        and reviewed_file is not None
        and reviewed_file.file_id != source_file.file_id
    ):
        raise ValueError("reviewed_file must belong to source_file")
