"""Explicit release-medium selection from immutable ranked lookup results."""

from PySide6.QtCore import QEvent, QObject, Qt, Signal, Slot
from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from metadata_polisher.application.lookup import CandidateLookupResult, ReleaseMediumIdentity
from metadata_polisher.domain.errors import ProviderErrorCode
from metadata_polisher.matching.release_scoring import RankedReleaseMedium
from metadata_polisher.matching.track_mapping import TrackMappingResult
from metadata_polisher.ui.dialogs.match_explanation_dialog import MatchExplanationDialog
from metadata_polisher.ui.layout import StatusLabel, configure_columns, fit_initial_size


def _provider_recovery_text(code: ProviderErrorCode) -> str:
    """Choose a useful next action from the failure category, without guessing titles."""
    if code is ProviderErrorCode.ACCESS_DENIED:
        return (
            "The provider blocked access. Retry later, or choose another provider explicitly in Settings. "
            "Changing the album title will not resolve an access block."
        )

    if code is ProviderErrorCode.PROXY_AUTHENTICATION_REQUIRED:
        return "Check the session proxy username and password in Settings, then test the corrected credentials."

    if code is ProviderErrorCode.PROXY_CONNECTION_FAILED:
        return "Check the proxy host, port and running proxy service in Settings, then test the connection."

    if code is ProviderErrorCode.TLS_VERIFICATION_FAILED:
        return (
            "Check the system clock and the connection's certificate trust, then retry; "
            "certificate checks stay enabled."
        )

    if code is ProviderErrorCode.AUTHENTICATION_REQUIRED:
        return "Check the selected provider's access requirements in Settings, then test the connection."

    if code is ProviderErrorCode.RATE_LIMITED:
        return "Wait before retrying this provider; its request limit was reached."

    return (
        "Retry Find Metadata. If requests still fail, check the connection "
        "and test the selected provider in Settings."
    )


def _lookup_issue_text(result: CandidateLookupResult) -> str:
    messages = []

    # A completed search with no matches is different from a request that never
    # succeeded. Retain provider failures below instead of calling both empty.
    for summary in result.lookup_result.summaries:
        if summary.successful_queries == 0:
            continue

        release_word = "release" if summary.candidate_count == 1 else "releases"
        outcome = (
            f"{summary.candidate_count} matching {release_word}"
            if summary.candidate_count
            else "No matching releases"
        )
        messages.append(f"{summary.engine_id}: {outcome} ({summary.successful_queries} searches completed).")

    messages.extend(dict.fromkeys(
        f"{failure.engine_id}: {failure.issue.code.value} — {failure.issue.message}"
        for failure in result.lookup_result.failures
    ))

    # Successful empty queries and failed requests can coexist. Explain each
    # failure's recovery independently; alternative titles are useful only when
    # at least one query actually completed successfully below.
    messages.extend(dict.fromkeys(
        f"{failure.engine_id}: {_provider_recovery_text(failure.issue.code)}"
        for failure in result.lookup_result.failures
        if isinstance(failure.issue.code, ProviderErrorCode)
    ))

    for notice in result.hydration_notices:
        identity = " / ".join(notice.candidate_identity)
        message = (
            f"{notice.issue.code.value} — {notice.issue.message}"
            if notice.issue is not None
            else notice.reason_code.value.replace("_", " ").capitalize()
        )
        messages.append(f"{identity}: {notice.reason_code.value} — {message}")

    messages.extend(f"{issue.code.value} — {issue.message}" for issue in result.matching_issues)

    if not result.lookup_result.candidates and any(
        summary.successful_queries > 0 for summary in result.lookup_result.summaries
    ):
        messages.append("Close this window and use Edit search terms to try another album title or artist.")

    return "\n".join(messages)


class CandidateDialog(QDialog):
    """Present ranked candidates while requiring an explicit Choose action."""

    candidate_selected = Signal(object)

    def __init__(
        self, candidate_lookup: CandidateLookupResult, parent: QWidget | None = None,
        *, mapped_identity: ReleaseMediumIdentity | None = None, mapping: TrackMappingResult | None = None,
        search_failure: CandidateLookupResult | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Choose metadata release")
        self._entries = candidate_lookup.release_ranking.entries
        self._accepting = False
        self.explanation_dialog: MatchExplanationDialog | None = None
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Select a release and disc to build metadata proposals.", self))
        self.table = QTableView(self)
        self.table.setObjectName("candidateTable")
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        model = QStandardItemModel(0, 8, self.table)
        sort_role = Qt.ItemDataRole.UserRole + 1
        model.setSortRole(sort_role)
        model.setHorizontalHeaderLabels(
            (
                "Engine",
                "Source",
                "Album title",
                "Date",
                "Disc / tracks",
                "Languages",
                "Match score",
                "Mapping coverage",
            )
        )

        for entry in self._entries:
            release = entry.release
            coverage = "Not mapped yet"

            if mapping is not None and entry.identity == mapped_identity:
                matched = len(mapping.mappings)
                total = matched + len(mapping.unmatched_local_file_ids)
                coverage = f"{matched} / {total} local tracks"

            languages = tuple(
                dict.fromkeys(title.language or title.script or "Unspecified" for title in release.titles)
            )
            values = (
                release.engine_id,
                release.source_id,
                " / ".join(dict.fromkeys(title.value for title in release.titles)) or "Untitled release",
                release.date or "—",
                f"Disc {entry.medium.medium_number or '?'} / {release.disc_total or '?'} · "
                f"{len(entry.medium.tracks)} loaded tracks",
                ", ".join(languages) or "Unspecified",
                f"{entry.result.score:.1f} / 100 · {entry.result.classification.value.capitalize()}",
                coverage,
            )
            cells = [QStandardItem(value) for value in values]

            for column, cell in enumerate(cells):
                cell.setEditable(False)
                cell.setData(entry, Qt.ItemDataRole.UserRole)
                # Keep identity separate from ordering. Only score has numeric
                # semantics; all other columns retain their visible text order.
                cell.setData(entry.result.score if column == 6 else cell.text(), sort_role)
                cell.setToolTip(cell.text())

            # CandidateLookupResult contains release scores, not the selected
            # track mapping. A score must never masquerade as mapping coverage.
            cells[-1].setToolTip("Track mapping is calculated after you choose a release and disc.")
            model.appendRow(cells)

        self.table.setModel(model)
        configure_columns(self.table, (100, 100, 340, 85, 160, 100, 145, 150))
        self.table.setColumnHidden(1, True)
        self.table.setColumnHidden(5, True)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(-1, Qt.SortOrder.AscendingOrder)
        layout.addWidget(self.table)
        issue_text = _lookup_issue_text(candidate_lookup)

        if search_failure is not None and search_failure != candidate_lookup:
            # The latest request can fail while earlier candidates remain usable.
            # Keep both receipts visible without mixing their matching evidence.
            issue_text = "\n".join(filter(None, (
                "The latest search failed. Previous results and review decisions are retained.",
                _lookup_issue_text(search_failure),
                issue_text,
            )))

        # Keep complete, copyable notices inside their own scroll area. The
        # table and action row retain space even for hundreds of result details.
        self.issues_label = StatusLabel(issue_text, self)
        self.issues_label.setAccessibleName("Candidate search messages and details")
        self.issues_label.setObjectName("candidateIssuesLabel")
        layout.addWidget(self.issues_label)
        actions = QHBoxLayout()
        self.why_button = QPushButton("Why?", self)
        self.choose_button = QPushButton("Choose", self)
        close_button = QPushButton("Close", self)

        for button in (self.why_button, self.choose_button, close_button):
            button.setAutoDefault(False)
            actions.addWidget(button)

        layout.addLayout(actions)
        self.table.selectionModel().selectionChanged.connect(self._refresh_actions)
        self.why_button.clicked.connect(self._show_explanation)
        self.choose_button.clicked.connect(self._choose_candidate)
        self.table.doubleClicked.connect(self._choose_candidate)
        self.table.activated.connect(self._choose_candidate)
        self.table.installEventFilter(self)
        close_button.clicked.connect(self.reject)
        self._refresh_actions()

        # Qt's default table size hint hides the disc column for long album
        # titles. Start with room for review, bounded by the current screen.
        fit_initial_size(self, 1200, 650)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self.table and event.type() == QEvent.Type.KeyPress:
            from PySide6.QtGui import QKeyEvent

            if isinstance(event, QKeyEvent) and event.key() in {Qt.Key.Key_Return, Qt.Key.Key_Enter}:
                self._choose_candidate()
                return True

        return super().eventFilter(watched, event)

    def _selected_entry(self) -> RankedReleaseMedium | None:
        rows = self.table.selectionModel().selectedRows()

        if len(rows) != 1:
            return None

        # Sorting moves visible rows. Read the attached ranked entry rather than
        # using the row number to index the original ranking tuple.
        entry = rows[0].data(Qt.ItemDataRole.UserRole)

        if isinstance(entry, RankedReleaseMedium) and entry in self._entries and entry.medium.tracks:
            return entry

        return None

    @Slot()
    def _refresh_actions(self) -> None:
        selected = self._selected_entry() is not None
        self.why_button.setEnabled(selected)
        self.choose_button.setEnabled(selected)

    @Slot()
    def _choose_candidate(self) -> None:
        entry = self._selected_entry()

        # One gesture may emit both doubleClicked and activated. The latch is set
        # before signalling the controller, which may itself process Qt events.
        if entry is None or self._accepting:
            return

        # Only this explicit action crosses the selection boundary. Merely
        # highlighting a row or reading its evidence leaves review state alone.
        self._accepting = True
        self.candidate_selected.emit(entry.identity)
        self.accept()

    @Slot()
    def _show_explanation(self) -> None:
        entry = self._selected_entry()

        if entry is None:
            return

        if self.explanation_dialog is not None:
            self.explanation_dialog.close()

        self.explanation_dialog = MatchExplanationDialog(entry.result.evidence, self)
        self.explanation_dialog.show()
