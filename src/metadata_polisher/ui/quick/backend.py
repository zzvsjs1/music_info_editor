"""Qt Quick session projection and commands over the existing application services."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

from PySide6.QtCore import Property, QObject, Qt, QUrl, Signal, Slot

from metadata_polisher.application.apply_summary import ApplySummary, build_apply_summary
from metadata_polisher.application.changes import ChangeIssueCode, ChangeIssueSeverity, RenameDecision
from metadata_polisher.application.scanning import ScanLibraryResult, ScanLibraryService
from metadata_polisher.domain.metadata import FieldReadState, MetadataField, Position
from metadata_polisher.domain.review import FieldDecisionKind, FieldReviewState, FieldValue
from metadata_polisher.execution.cancellation import CancellationToken
from metadata_polisher.execution.events import (
    FileCompleted,
    FileFailed,
    FileStageChanged,
    FileStarted,
    OperationEventSink,
    OperationProgress,
    OperationStageChanged,
    ProviderStarted,
)
from metadata_polisher.execution.executor import (
    OperationHandle,
    OperationWork,
    ProcessingExecutor,
    SerialBackgroundExecutor,
)
from metadata_polisher.formats.registry import FormatRegistry
from metadata_polisher.infrastructure.diagnostics import ThreadSafeOperationIds
from metadata_polisher.infrastructure.logging_setup import redact_sensitive_text
from metadata_polisher.infrastructure.session_credentials import SessionCredentials
from metadata_polisher.infrastructure.settings import AppSettings, RenameSettings
from metadata_polisher.session.apply_preparation import reviewed_file_ids
from metadata_polisher.session.review_editing import (
    BatchReviewAction,
    BatchReviewCommand,
    aggregate_review_fields,
    apply_batch_review,
    apply_field_decision,
    review_undo_targets,
    undo_last_review_action,
)
from metadata_polisher.session.state import (
    GroupSelection,
    GroupState,
    OperationKind,
    ResultApplicationStatus,
    SessionState,
    StateApplicationResult,
    UnsupportedSelection,
    apply_scan_library_result,
    begin_operation,
    finish_operation,
    set_selection,
)
from metadata_polisher.ui.models import FileTableModel, MetadataDiffModel
from metadata_polisher.ui.models.diff_model import FIELD_LABELS
from metadata_polisher.ui.models.group_model import GroupListModel, GroupPresentationStatus, classify_group
from metadata_polisher.ui.pending_work import pending_work_summary
from metadata_polisher.ui.qt_bridge import QtOperationBridge
from metadata_polisher.ui.quick.diagnostics import QuickDiagnostics
from metadata_polisher.ui.quick.models import QuickTableModel
from metadata_polisher.ui.quick.preferences import QuickLayout

if TYPE_CHECKING:
    from metadata_polisher.ui.quick.library import QuickLibrary
    from metadata_polisher.ui.quick.lookup import QuickLookup

_LIST_FIELDS = frozenset(
    {MetadataField.ARTISTS, MetadataField.ALBUM_ARTISTS, MetadataField.COMPOSERS, MetadataField.GENRES}
)
_POSITION_FIELDS = frozenset({MetadataField.TRACK, MetadataField.DISC})

# QML SpinBox stores signed 32-bit integers, matching the original QSpinBox.
_MAXIMUM_POSITION_COMPONENT = 2_147_483_647
_PROGRESS_LOG_LIMIT = 200
_PROGRESS_STAGE_LIMIT = 120


def _short_progress_stage(message: str) -> str:
    """Keep a stage suitable for compact controls; details retain its full text."""
    first_line = message.splitlines()[0] if message else ""

    if len(first_line) <= _PROGRESS_STAGE_LIMIT:
        return first_line

    return first_line[:_PROGRESS_STAGE_LIMIT - 1] + "…"


@dataclass(frozen=True)
class _EditTarget:
    """A dialogue retains semantic identities and the revision it was opened on."""

    file_ids: tuple[str, ...]
    field: MetadataField
    revision: int
    fields: tuple[MetadataField, ...]


class QuickBackend(QObject):
    """Own session choices; project them through properties and explicit commands."""

    changed = Signal()
    cancellation_requested = Signal(str)
    controller_failed = Signal(str, object)

    def __init__(
        self,
        *,
        state: SessionState | None = None,
        executor: ProcessingExecutor | None = None,
        scanner: ScanLibraryService | None = None,
        settings: AppSettings | None = None,
        settings_file: Path | None = None,
        app_dir: Path | None = None,
    ) -> None:
        super().__init__()
        self.session_state = state if state is not None else SessionState(root=None)
        self.app_settings = settings or AppSettings()
        self.settings_file = settings_file
        self.app_dir = app_dir
        self.credentials = SessionCredentials()
        self.executor = executor if executor is not None else SerialBackgroundExecutor()
        self.bridge = QtOperationBridge(self.executor, self)
        self._scanner = scanner if scanner is not None else ScanLibraryService(FormatRegistry())
        self._ids = ThreadSafeOperationIds()
        self._handle: OperationHandle[object] | None = None
        self._reducers: dict[str, Callable[[SessionState, object], SessionState | StateApplicationResult]] = {}
        self._operation_kinds: dict[str, OperationKind] = {}
        self._reduced: set[str] = set()
        self._closed = False
        self._cancelling = False
        self._selected: tuple[str, ...] = ()
        self._file_focus = ""
        self._file_anchor = ""
        self._group_anchor = ""
        self._field_anchor = MetadataField.TITLE.value
        self._included: frozenset[str] = frozenset()
        self._field = MetadataField.TITLE
        # A focused row is navigation state, not permission to change a field.
        # The user must explicitly highlight fields before a review command.
        self._fields: tuple[MetadataField, ...] = ()
        self._group_selection: tuple[str, ...] = ()
        self._scope = "selected"
        self._review_visible = False
        self._proposal = 0
        self._confirmation: tuple[SessionState, Callable[[], None]] | None = None
        self._confirmation_title = ""
        self._confirmation_text = ""
        self._progress_visible = False
        self._progress_kind = OperationKind.SCAN
        self._progress_log: list[str] = []
        self._progress_stage = "Idle"
        self._progress_failed = False
        self._progress_current = 0
        self._progress_total = 0
        self._progress_provider = ""
        self._progress_item = ""
        self._lookup_ui: QObject | None = None
        self._settings_ui: QObject | None = None
        self._library_ui: QObject | None = None
        self._apply_ui: QObject | None = None
        self._edit: _EditTarget | None = None
        self._edit_value = ""
        self._edit_position = Position()
        self._edit_error = ""
        self._status = "Choose a music folder, scan it, then select files to review."
        self._progress = -1.0
        self._albums = GroupListModel(parent=self)
        self._album_proxy = QuickTableModel(self._albums, self, sortable=True)
        self._files = FileTableModel(parent=self)
        self._review = MetadataDiffModel(parent=self)
        self._file_proxy = QuickTableModel(self._files, self, sortable=True)
        self._review_proxy = QuickTableModel(self._review, self)
        self._album_proxy.sortingChanged.connect(self._sorting_changed)
        self._file_proxy.sortingChanged.connect(self._sorting_changed)
        self.bridge.operation_event.connect(self._on_event)
        self.bridge.completed.connect(self._on_completed)
        self.bridge.cancelled.connect(self._on_cancelled)
        self.bridge.failed.connect(self._on_failed)
        self._diagnostics = QuickDiagnostics(self)
        self._layout = QuickLayout(self)
        self._ensure_group()
        self._refresh()

    @property
    def _rename(self) -> RenameSettings:
        return self.app_settings.rename

    @property
    def selected_group_ids(self) -> tuple[str, ...]:
        return self._group_selection

    def set_state(self, state: SessionState) -> None:
        """Publish a canonical session while pruning only invalid UI identities."""
        if state.selection != self.session_state.selection:
            self._group_selection = ()

        self.session_state = state
        known = {file.file_id for group in state.groups for file in group.group.files}
        self._included &= known
        self._ensure_group()
        self._refresh()

    def set_status(self, message: str) -> None:
        self._status = redact_sensitive_text(message)
        self.changed.emit()

    def _group(self) -> GroupState | None:
        selection = self.session_state.selection
        if isinstance(selection, GroupSelection):
            return next(
                (group for group in self.session_state.groups if group.group.group_id == selection.group_id), None
            )

        return None

    def _ensure_group(self) -> None:
        if (
            self._group() is None
            and self.session_state.groups
            and not isinstance(self.session_state.selection, UnsupportedSelection)
        ):
            self.session_state = set_selection(
                self.session_state,
                GroupSelection(self.session_state.groups[0].group.group_id),
            )

        known = {group.group.group_id for group in self.session_state.groups}
        self._group_selection = tuple(key for key in self._group_selection if key in known)
        if not self._group_selection and self._group() is not None:
            self._group_selection = (self._group_id(),)

    def _is_busy(self) -> bool:
        return self.session_state.active_operation is not None

    def _pending(self) -> bool:
        return pending_work_summary(self.session_state, self._included).has_pending_work

    def _can_edit(self) -> bool:
        # A mixed scope remains actionable when at least one field/file pair
        # is readable. The batch service reports individual blocked members.
        ids = self._scope_ids()
        return bool(
            not self._closed
            and not self._is_busy()
            and ids
            and self._fields
            and any(
                not group.requires_rescan
                and any(
                    source.read_result.field_states[field] in {FieldReadState.PRESENT, FieldReadState.MISSING}
                    for field in self._fields
                )
                for group in self.session_state.groups
                for source in group.group.files
                if source.file_id in ids
            )
        )

    def _scope_ids(self) -> tuple[str, ...]:
        """Resolve semantic targets in library order, independently of focus."""
        if self._scope == "group":
            group = self._group()
            return tuple(file.file_id for file in group.group.files) if group else ()

        ids = tuple(file.file_id for group in self.session_state.groups for file in group.group.files)
        if self._scope == "library":
            return ids

        if self._scope == "included":
            return tuple(key for key in ids if key in self._included)

        # Visual sorting changes navigation, while commands retain canonical
        # library order just like the group, included, and whole-library scopes.
        return tuple(key for key in ids if key in self._selected)

    def _group_id(self) -> str:
        group = self._group()
        return group.group.group_id if group is not None else ""

    def _groups(self) -> list[dict[str, str]]:
        return [
            {
                "id": group.group.group_id,
                "label": f"{group.group.album_title or 'Untitled album'} ({len(group.group.files)} files)",
            }
            for group in self.session_state.groups
        ]

    def _details(self) -> str:
        if not self._scope_ids() or not self._fields:
            return "Select a file and a field to inspect its complete values."

        sections = []
        for row, field in enumerate(MetadataField):
            if field not in self._fields:
                continue

            lines = [FIELD_LABELS[field]]
            for column in range(1, self._review.columnCount()):
                label = self._review.headerData(column, Qt.Orientation.Horizontal)
                lines.append(f"{label}: {self._review.index(row, column).data()}")

            # Tooltips retain per-file values for mixed selections and source
            # evidence for single files. Keep tag text plain and copyable.
            lines.append(str(self._review.index(row, 0).data(Qt.ItemDataRole.ToolTipRole) or ""))
            sections.append("\n".join(lines))

        return "\n\n".join(sections)

    def _edit_type(self) -> str:
        field = self._edit.field if self._edit is not None else self._field
        if field in _LIST_FIELDS:
            return "list"

        if field in _POSITION_FIELDS:
            return "position"

        return "text"

    def _edit_title(self) -> str:
        target = self._edit

        if target is not None and (len(target.file_ids) > 1 or len(target.fields) > 1):
            # Describe the captured command, not the potentially changing focus.
            # A common value can affect several fields and files at once.
            labels = ", ".join(FIELD_LABELS[field] for field in target.fields)
            count = len(target.file_ids)
            return f"Set common {labels} for {count} {'file' if count == 1 else 'files'}"

        return f"Manual value: {(target.field if target else self._field).value}"

    def _edit_hint(self) -> str:
        field = self._edit.field if self._edit is not None else self._field
        if field in _LIST_FIELDS:
            return "One value per line. Punctuation stays within a value."

        if field in _POSITION_FIELDS:
            return "Number and total are independent. Missing removes that component. Use Clear to remove the field."

        return "Enter a value. Use Clear field to remove it. Mixed selections start empty."

    def _current_file_row(self) -> int:
        if self._group() is None or not self._selected:
            return -1

        ids = self._file_proxy.visible_ids()
        focus = self._file_focus or self._selected[-1]

        return ids.index(focus) if focus in ids else -1

    def _current_group_row(self) -> int:
        ids = self._album_proxy.visible_ids()
        focus = "@unsupported" if isinstance(self.session_state.selection, UnsupportedSelection) else self._group_id()

        return ids.index(focus) if focus in ids else -1

    def _sorting_changed(self) -> None:
        # A header only rearranges presentation identities. Keep both anchors
        # and the semantic session untouched, and publish their new row numbers.
        self._selected = tuple(key for key in self._file_proxy.visible_ids() if key in self._selected)
        self._group_selection = tuple(key for key in self._album_proxy.visible_ids() if key in self._group_selection)
        self.changed.emit()

    def _summary(self) -> str:
        ready = sum(classify_group(group) is GroupPresentationStatus.COMPLETE for group in self.session_state.groups)
        return (
            f"{ready} ready · {len(self.session_state.groups) - ready} review · "
            f"{len(self.session_state.unsupported_files)} unsupported"
        )

    def _file_progress(self) -> str:
        if self._scope != "selected" or len(self._selected) != 1:
            count = len(self._scope_ids())

            return f"{count} {'file' if count == 1 else 'files'}" if count else "No files selected"

        group = self._group()
        row = self._current_file_row()

        return f"{row + 1} of {len(group.group.files)}" if group is not None and row >= 0 else ""

    def _review_message(self) -> str:
        # Scan details already show a concise, pluralised success summary. The
        # legacy status string may append settings-save warnings: retain those
        # verbatim while suppressing only the exact duplicate success line.
        count = sum(len(group.group.files) for group in self.session_state.groups)
        success = f"Scanned {count} supported files in {len(self.session_state.groups)} albums."
        first, _separator, remainder = self._status.partition("\n")

        return remainder if first == success else self._status

    def _review_scope_label(self) -> str:
        ids = self._scope_ids()
        count = len(ids)
        suggestions = sum(
            bool(review.proposals)
            for group in self.session_state.groups
            for file in group.reviewed_files
            if file.file_id in ids
            for review in file.reviews
        )
        detail = (
            f"{suggestions} {'suggestion' if suggestions == 1 else 'suggestions'}"
            if suggestions else "No suggestions"
        )

        return f"{count} {'file' if count == 1 else 'files'} · {detail}"

    def _filename_suggestion_count(self) -> int:
        ids = self._scope_ids()

        # A preview is a suggestion, not consent to rename. Count it without
        # changing its independent KEEP_FILENAME / APPLY_RENAME decision.
        return sum(
            file.change_set is not None and file.change_set.rename_preview is not None
            for group in self.session_state.groups
            for file in group.reviewed_files
            if file.file_id in ids
        )

    def _filename_summary(self) -> str:
        if self._filename_needs_attention():
            return "Filename needs attention"

        count = self._filename_suggestion_count()

        if not count:
            return "No rename suggested"

        return "Rename suggested" if len(self._scope_ids()) == 1 else (
            f"{count} {'rename suggestion' if count == 1 else 'rename suggestions'}"
        )

    def _filename_needs_attention(self) -> bool:
        ids = self._scope_ids()
        filename_codes = {
            ChangeIssueCode.INVALID_RENAME_TEMPLATE, ChangeIssueCode.RENAME_RENDER_FAILED,
            ChangeIssueCode.INVALID_DESTINATION_FILENAME, ChangeIssueCode.FILENAME_REPAIRED,
            ChangeIssueCode.DESTINATION_COLLISION, ChangeIssueCode.CASE_ONLY_RENAME_UNSUPPORTED,
        }

        # Failed rendering can remove the preview entirely. Keep its warning
        # discoverable rather than treating the absent preview as a clean file.
        return any(
            issue.code in filename_codes
            for group in self.session_state.groups
            for file in group.reviewed_files
            if file.file_id in ids and file.change_set is not None
            for issue in file.change_set.validation.issues
        )

    def _scan_warning_count(self) -> int:
        return len(self.session_state.scan_issues) + len(self.session_state.unsupported_files)

    def _scan_summary(self) -> str:
        if self.session_state.root is None:
            return "No files scanned"

        count = sum(len(group.group.files) for group in self.session_state.groups)
        albums = len(self.session_state.groups)
        summary = (
            f"Scanned {count} {'file' if count == 1 else 'files'} "
            f"in {albums} {'album' if albums == 1 else 'albums'}"
        )
        warnings = self._scan_warning_count()

        return summary + (f" · {warnings} {'warning' if warnings == 1 else 'warnings'}" if warnings else "")

    def _scan_details(self) -> str:
        lines = [
            text
            for issue in self.session_state.scan_issues
            for text in (issue.message, issue.technical_detail)
            if text
        ]

        if self.session_state.unsupported_files:
            lines.extend(("Unsupported files:", *(str(file.path) for file in self.session_state.unsupported_files)))

        return redact_sensitive_text("\n".join(lines))

    def _included_summary(self) -> ApplySummary:
        # Reuse the final confirmation's pure counter so a mere inclusion or a
        # filename preview cannot be described as a pending write. No request,
        # transaction, or confirmation is created by reading this presentation.
        changes = tuple(
            file.change_set
            for group in self.session_state.groups
            for file in group.reviewed_files
            if file.file_id in self._included and file.change_set is not None
        )

        return build_apply_summary(
            changes,
            backup_enabled=self.app_settings.backup.enabled,
            report_enabled=self.app_settings.reports.enabled,
        )

    def _changes_to_apply_summary(self) -> str:
        count = len(self._included)

        if not count:
            return "No changes selected"

        summary = self._included_summary()

        # The existing canApply gate allows opening a confirmation with current
        # reviewed blockers. Calling those files ready would conceal the work
        # still required, so retain the explanation until blockers are resolved.
        if not self._included.issubset(reviewed_file_ids(self.session_state)) or summary.blocking_issues:
            return f"{count} selected {'file needs' if count == 1 else 'files need'} review"

        if not summary.write_file_count:
            return "No changes selected"

        count = summary.write_file_count
        detail = "with changes" if self._is_busy() else "ready to apply"

        return f"{count} {'file' if count == 1 else 'files'} {detail}"

    def _facade(self, kind: str) -> QObject:
        attribute = f"_{kind}_ui"
        existing = getattr(self, attribute)
        if existing is None:
            # Composition is lazy so domain-only tests need not initialise HTTP
            # clients, preferences or write services just to project a table.
            from metadata_polisher.ui.quick.apply import QuickApply
            from metadata_polisher.ui.quick.library import QuickLibrary
            from metadata_polisher.ui.quick.lookup import QuickLookup
            from metadata_polisher.ui.quick.settings import QuickSettings

            existing = {"lookup": QuickLookup, "settings": QuickSettings, "library": QuickLibrary, "apply": QuickApply}[
                kind
            ](self)
            setattr(self, attribute, existing)
            if kind == "settings":
                existing.settings_changed.connect(lambda _: self._diagnostics.configure())

        return cast(QObject, existing)

    def _single_review(self) -> FieldReviewState | None:
        ids = self._scope_ids()
        if len(ids) == 1 and len(self._fields) == 1:
            return next(
                (
                    review
                    for group in self.session_state.groups
                    for file in group.reviewed_files
                    if file.file_id == ids[0]
                    for review in file.reviews
                    if review.field == self._field
                ),
                None,
            )
        return None

    def _proposals(self) -> list[dict[str, str]]:
        from metadata_polisher.ui.models.diff_model import format_field_value

        review = self._single_review()
        if review is None:
            return []
        return [
            {
                "key": str(index),
                "label": f"{format_field_value(proposal.value)}"
                f" · {proposal.language or ''} · "
                f"{', '.join(dict.fromkeys(member.provenance.source_id for member in proposal.members))}"
                f" · {proposal.confidence.value}",
            }
            for index, proposal in enumerate(review.proposals)
        ]

    def _restore_proposal(self) -> None:
        """Resolve the picker's index from the accepted semantic proposal."""
        review = self._single_review()
        self._proposal = 0
        if review is not None and review.selected_proposal is not None and review.selected_proposal in review.proposals:
            # Indices belong only to this file/field's current proposal list.
            # Restoring the stored object avoids borrowing a previous file's
            # index or replacing an accepted second choice with the first.
            self._proposal = review.proposals.index(review.selected_proposal)

    def _filename(self, proposed: bool) -> str:
        ids = self._scope_ids()
        if len(ids) != 1:
            return f"{len(ids)} files — see Filename previews…" if ids else ""
        source = next(
            file for group in self.session_state.groups for file in group.group.files if file.file_id == ids[0]
        )
        reviewed = next(
            (file for group in self.session_state.groups for file in group.reviewed_files if file.file_id == ids[0]),
            None,
        )
        if proposed and reviewed and reviewed.change_set and reviewed.change_set.rename_preview:
            return reviewed.change_set.rename_preview.new_path.name
        if proposed:
            return "No suggestion"
        return source.path.name

    def _rename_validation(self) -> str:
        ids = self._scope_ids()
        changes = tuple(
            file.change_set
            for group in self.session_state.groups
            for file in group.reviewed_files
            if file.file_id in ids and file.change_set is not None
        )
        if len(ids) > 1:
            # The denominator is the entire explicit scope, including files
            # which do not yet have a review or an included rename decision.
            included = sum(change.rename_decision is RenameDecision.APPLY_RENAME for change in changes)
            lines = [f"Renames included: {included} of {len(ids)} files."]
            issues = tuple(issue for change in changes for issue in change.validation.issues)
            if issues:
                blocking = sum(issue.severity is ChangeIssueSeverity.BLOCKING for issue in issues)
                lines.append(
                    f"Blocking issues: {blocking} · Warnings: {len(issues) - blocking}. See Filename previews…"
                )

            return "\n".join(lines)

        if not changes:
            # Keeping an untouched file is a valid no-op in the domain layer,
            # so it need not allocate a ReviewedFileState or change set.
            return "Filename kept" if ids else ""

        change = changes[0]
        decision = "Rename will apply" if change.rename_decision is RenameDecision.APPLY_RENAME else "Filename kept"
        lines = [decision]
        for issue in change.validation.issues:
            severity = "Blocked" if issue.severity is ChangeIssueSeverity.BLOCKING else "Warning"
            lines.append(f"{severity}: {issue.message} ({issue.code.value})")

        return "\n".join(lines)

    def _can_review_filenames(self) -> bool:
        """Keeping a filename stays available when proposed renaming is disabled."""
        ids = self._scope_ids()
        return bool(
            ids
            and not self._closed
            and not self._is_busy()
            and self._edit is None
            and any(
                not group.requires_rescan
                for group in self.session_state.groups
                if any(file.file_id in ids for file in group.group.files)
            )
        )

    def _can_rename(self) -> bool:
        return self._rename.enabled and self._can_review_filenames()

    # One notification publishes a consistent snapshot. QML has no setters for
    # semantic state; every mutation below checks eligibility before dispatch.
    files = Property(QObject, lambda self: self._file_proxy, constant=True)
    albumModel = Property(QObject, lambda self: self._album_proxy, constant=True)
    review = Property(QObject, lambda self: self._review_proxy, constant=True)

    # File/group highlighting and write inclusion have separate identities.
    # Selecting another row never implicitly adds it to the eventual write batch.
    groups = Property(list, _groups, notify=changed)
    groupId = Property(str, _group_id, notify=changed)
    selectedGroupIds = Property(list, lambda self: list(self._group_selection), notify=changed)
    currentGroupRow = Property(int, _current_group_row, notify=changed)
    selectedFileIds = Property(list, lambda self: list(self._selected), notify=changed)
    includedFileIds = Property(list, lambda self: sorted(self._included), notify=changed)

    # selectedField is the navigation focus. Only selectedFields authorises
    # editing; an untouched review therefore exposes no actionable field row.
    selectedField = Property(str, lambda self: self._field.value, notify=changed)
    selectedFields = Property(list, lambda self: [field.value for field in self._fields], notify=changed)
    currentFileRow = Property(int, _current_file_row, notify=changed)
    fileProgress = Property(str, _file_progress, notify=changed)
    currentFieldRow = Property(
        int,
        lambda self: list(MetadataField).index(self._field) if self._scope_ids() and self._fields else -1,
        notify=changed,
    )
    status = Property(str, lambda self: self._status, notify=changed)
    revision = Property(int, lambda self: self.session_state.revision, notify=changed)
    progress = Property(float, lambda self: self._progress, notify=changed)
    busy = Property(bool, _is_busy, notify=changed)
    canEdit = Property(bool, _can_edit, notify=changed)
    canUndo = Property(
        bool, lambda self: not self._closed and bool(review_undo_targets(self.session_state)), notify=changed
    )
    hasPendingWork = Property(bool, _pending, notify=changed)
    # A scan or exit discards the whole session, including decisions in albums
    # outside the current selection. Reuse the same pure summary as the guard
    # so the confirmation describes exactly the work that makes it necessary.
    pendingWorkDescription = Property(
        str, lambda self: pending_work_summary(self.session_state, self._included).description(), notify=changed,
    )
    fieldDetails = Property(str, _details, notify=changed)

    # These values seed the owned editor once. Draft text lives in that window
    # until the explicit commit command validates the original IDs and revision.
    editing = Property(bool, lambda self: self._edit is not None, notify=changed)
    editValue = Property(str, lambda self: self._edit_value, notify=changed)
    editType = Property(str, _edit_type, notify=changed)
    editTitle = Property(str, _edit_title, notify=changed)
    editNumber = Property(int, lambda self: self._edit_position.number or 0, notify=changed)
    editTotal = Property(int, lambda self: self._edit_position.total or 0, notify=changed)
    maximumPositionComponent = Property(int, lambda self: _MAXIMUM_POSITION_COMPONENT, constant=True)
    editLabel = Property(
        str, lambda self: FIELD_LABELS[self._edit.field if self._edit else self._field], notify=changed
    )
    editHint = Property(str, _edit_hint, notify=changed)
    editError = Property(str, lambda self: self._edit_error, notify=changed)

    # Review scope is independent of table highlighting and batch inclusion.
    reviewVisible = Property(bool, lambda self: self._review_visible, notify=changed)
    reviewScope = Property(str, lambda self: self._scope, notify=changed)
    scopeFileIds = Property(list, lambda self: list(self._scope_ids()), notify=changed)
    reviewTargetLabel = Property(str, lambda self: self._filename(False), notify=changed)
    reviewScopeLabel = Property(str, _review_scope_label, notify=changed)
    reviewMessage = Property(str, _review_message, notify=changed)
    changesToApplySummary = Property(str, _changes_to_apply_summary, notify=changed)
    hasChangesToApply = Property(bool, lambda self: self._included_summary().write_file_count > 0, notify=changed)
    scanSummary = Property(str, _scan_summary, notify=changed)
    scanHasWarnings = Property(bool, lambda self: self._scan_warning_count() > 0, notify=changed)
    scanDetails = Property(str, _scan_details, notify=changed)
    rootPath = Property(
        str, lambda self: str(self.session_state.root or self.app_settings.general.last_root_folder), notify=changed
    )
    summary = Property(str, _summary, notify=changed)
    providerLabel = Property(str, lambda self: self._facade("lookup").providerLabel, notify=changed)
    canNavigate = Property(
        bool,
        lambda self: (
            not self._is_busy() and self._scope == "selected" and len(self._selected) == 1 and self._edit is None
        ),
        notify=changed,
    )
    canUseCandidate = Property(
        bool,
        lambda self: (
            self._can_edit() and (bool(self._proposals()) or len(self._scope_ids()) > 1 or len(self._fields) > 1)
        ),
        notify=changed,
    )

    # Disabling proposed renaming must still allow existing rename intentions
    # to be removed. Safe metadata additions use scope eligibility as well.
    canRename = Property(bool, _can_rename, notify=changed)
    canKeepFilename = Property(bool, _can_review_filenames, notify=changed)
    canAcceptSafe = Property(
        bool,
        lambda self: (
            self._can_review_filenames()
            and any(
                file.file_id in self._scope_ids()
                for group in self.session_state.groups
                if not group.requires_rescan
                for file in group.reviewed_files
            )
        ),
        notify=changed,
    )
    currentFilename = Property(str, lambda self: self._filename(False), notify=changed)
    proposedFilename = Property(str, lambda self: self._filename(True), notify=changed)
    filenameSummary = Property(str, _filename_summary, notify=changed)
    hasFilenameSuggestion = Property(bool, lambda self: self._filename_suggestion_count() > 0, notify=changed)
    filenameNeedsAttention = Property(bool, _filename_needs_attention, notify=changed)
    renameTemplate = Property(str, lambda self: self._rename.template, notify=changed)
    renameValidation = Property(str, _rename_validation, notify=changed)
    proposalOptions = Property(list, _proposals, notify=changed)
    proposalKey = Property(str, lambda self: str(self._proposal), notify=changed)

    # Confirmation captures an immutable session; merely showing the popup
    # cannot grant permission to a later, different review state.
    confirmationVisible = Property(bool, lambda self: self._confirmation is not None, notify=changed)
    confirmationTitle = Property(str, lambda self: self._confirmation_title, notify=changed)
    confirmationText = Property(str, lambda self: self._confirmation_text, notify=changed)

    # A fractional bar is presentation only. Preserve the original counts for
    # exact reporting and provider/item context across intermediate events.
    progressVisible = Property(bool, lambda self: self._progress_visible, notify=changed)
    progressTitle = Property(
        str,
        lambda self: {
            OperationKind.SCAN: "Scanning library",
            OperationKind.LOOKUP: "Finding metadata",
            OperationKind.APPLY: "Applying changes",
            OperationKind.PROVIDER_TEST: "Testing provider",
        }[self._progress_kind],
        notify=changed,
    )
    progressKind = Property(str, lambda self: self._progress_kind.value, notify=changed)
    progressLog = Property(str, lambda self: "\n".join(self._progress_log), notify=changed)
    progressStage = Property(str, lambda self: self._progress_stage, notify=changed)
    progressFailed = Property(bool, lambda self: self._progress_failed, notify=changed)
    progressCount = Property(
        str,
        lambda self: f"{self._progress_current} / {self._progress_total} in this stage" if self._progress_total else "",
        notify=changed,
    )
    progressContext = Property(
        str,
        lambda self: " · ".join(filter(None, (self._progress_provider, self._progress_item))),
        notify=changed,
    )
    cancelling = Property(bool, lambda self: self._cancelling, notify=changed)

    # Lazily constructed service facades share this session owner. QML never
    # keeps an alternative mutable copy of review or operation state.
    lookupUi = Property(QObject, lambda self: self._facade("lookup"), constant=True)
    settingsUi = Property(QObject, lambda self: self._facade("settings"), constant=True)
    libraryUi = Property(QObject, lambda self: self._facade("library"), constant=True)
    applyUi = Property(QObject, lambda self: self._facade("apply"), constant=True)
    layoutUi = Property(QObject, lambda self: self._layout, constant=True)
    diagnosticSummary = Property(str, lambda self: self._diagnostics.summary(), notify=changed)
    releaseExplanation = Property(str, lambda self: self._diagnostics.explanation(False), notify=changed)
    trackExplanation = Property(str, lambda self: self._diagnostics.explanation(True), notify=changed)
    releaseEvidence = Property(list, lambda self: self._diagnostics.evidence_rows(False), notify=changed)
    trackEvidence = Property(list, lambda self: self._diagnostics.evidence_rows(True), notify=changed)

    def _help_text(self) -> str:
        from metadata_polisher.ui.quick.settings import HELP_TEXT

        return HELP_TEXT

    helpText = Property(str, _help_text, constant=True)

    @Slot()
    def copyDiagnostics(self) -> None:
        self._diagnostics.copy()

    @Slot()
    def openLogs(self) -> None:
        self._diagnostics.open_logs()

    def _refresh(self) -> None:
        group = self._group()
        visible = {source.file_id for source in group.group.files} if group is not None else set()
        self._selected = tuple(file_id for file_id in self._selected if file_id in visible)
        if self._file_focus not in visible:
            self._file_focus = self._selected[-1] if self._selected else ""

        # A source reset reapplies its active sort. Publish current membership
        # first so an Include sort uses the same values as the new checkboxes.
        self._files.set_included_file_ids(self._included)
        self._files.set_group_state(group, self.session_state.written_files)
        if isinstance(self.session_state.selection, UnsupportedSelection):
            self._files.set_unsupported_files(self.session_state.unsupported_files)
        self._albums.set_session_state(self.session_state)
        self._selected = tuple(key for key in self._file_proxy.visible_ids() if key in self._selected)
        self._group_selection = tuple(key for key in self._album_proxy.visible_ids() if key in self._group_selection)
        self._album_proxy.set_highlighted(frozenset(self._group_selection) if group else frozenset({"@unsupported"}))
        self._refresh_review()

    def _refresh_review(self) -> None:
        # Single-file projection retains complete provider evidence; a wider
        # scope uses the shared aggregate model for mixed and unavailable values.
        ids = self._scope_ids()
        group = next(
            (
                item
                for item in self.session_state.groups
                if ids and any(source.file_id == ids[0] for source in item.group.files)
            ),
            None,
        )
        if group is not None and len(ids) == 1:
            file_id = ids[0]
            source = next(source for source in group.group.files if source.file_id == file_id)
            reviewed = next((file for file in group.reviewed_files if file.file_id == file_id), None)
            self._review.set_file(source, reviewed, self.session_state.written_files)
        elif ids:
            self._review.set_selection(self.session_state, ids)
        else:
            self._review.set_file(None, None)

        self._file_proxy.set_highlighted(frozenset(self._selected))
        self._review_proxy.set_highlighted(frozenset(field.value for field in self._fields))
        self._restore_proposal()
        self.changed.emit()

    @Slot(str)
    def selectGroup(self, group_id: str) -> None:
        if (
            self._is_busy()
            or self._edit is not None
            or self._confirmation is not None
            or (
                group_id != "@unsupported"
                and not any(group.group.group_id == group_id for group in self.session_state.groups)
            )
        ):
            return

        self._group_selection = (group_id,) if group_id != "@unsupported" else ()
        self._group_anchor = group_id
        if group_id != self._group_id():
            self.session_state = set_selection(
                self.session_state, UnsupportedSelection() if group_id == "@unsupported" else GroupSelection(group_id)
            )
            self._selected = ()
        self._refresh()

    @staticmethod
    def _extended(
        ids: tuple[str, ...], selected: tuple[str, ...], key: str, toggle: bool, extend: bool, anchor: str = ""
    ) -> tuple[str, ...]:
        """Apply Ctrl/Shift selection without letting row indices escape the UI."""
        if key not in ids:
            return selected

        chosen = set(selected) if toggle else set()
        if extend and selected:
            first, last = sorted((ids.index(anchor if anchor in ids else selected[0]), ids.index(key)))
            chosen.update(ids[first : last + 1])
        elif toggle and key in chosen:
            chosen.remove(key)
        else:
            chosen.add(key)

        # Retain model order after set operations so batch commands and their
        # feedback are deterministic regardless of the user's click order.
        return tuple(item for item in ids if item in chosen)

    @Slot(str, bool, bool)
    def selectGroupExtended(self, key: str, toggle: bool, extend: bool) -> None:
        if self._is_busy() or self._edit is not None or self._confirmation is not None:
            return
        if key == "@unsupported":
            self.selectGroup(key)
            return
        ids = tuple(key for key in self._album_proxy.visible_ids() if key != "@unsupported")
        anchor = self._group_anchor
        chosen = self._extended(ids, self._group_selection, key, toggle, extend, anchor)
        self.selectGroup(key)
        if extend:
            self._group_anchor = anchor
        self._group_selection = chosen
        self._refresh()

    @Slot(int, bool)
    def moveGroup(self, delta: int, extend: bool) -> None:
        ids = self._album_proxy.visible_ids()
        if ids:
            current = self._current_group_row()
            self.selectGroupExtended(ids[max(0, min(len(ids) - 1, current + delta))], False, extend)

    @Slot()
    def selectAllGroups(self) -> None:
        if self._is_busy() or self._edit is not None or self._confirmation is not None:
            return

        ids = tuple(key for key in self._album_proxy.visible_ids() if key != "@unsupported")

        if not ids:
            return

        # The unsupported-files collection cannot become a lookup/merge
        # target. When it owns focus, choose a real album before highlighting
        # every supported group in its existing display order.
        if self._group_id() not in ids:
            self.selectGroup(ids[0])

        self._group_selection = ids
        self._group_anchor = self._group_id()
        self._album_proxy.set_highlighted(frozenset(ids))
        self.changed.emit()

    @Slot()
    def clearGroupSelection(self) -> None:
        if self._is_busy() or self._edit is not None or self._confirmation is not None:
            return

        # Clearing the operation targets leaves the current album and its
        # file viewport intact. Updating only highlight roles also avoids a
        # model reset that would unnecessarily move the user's scroll position.
        self._group_selection = ()
        self._group_anchor = self._group_id()
        self._album_proxy.set_highlighted(frozenset())
        self.changed.emit()

    @Slot(str, bool, bool)
    def selectFileExtended(self, key: str, toggle: bool, extend: bool) -> None:
        group = self._group()
        if self._is_busy() or group is None:
            return
        ids = self._file_proxy.visible_ids()
        if key not in ids:
            return
        self._selected = self._extended(ids, self._selected, key, toggle, extend, self._file_anchor)
        self._file_focus = key
        if not extend:
            self._file_anchor = key
        self._refresh_review()

    @Slot(int, bool)
    def moveFileExtended(self, delta: int, extend: bool) -> None:
        group = self._group()
        if group and group.group.files:
            ids = self._file_proxy.visible_ids()
            row = max(0, min(len(ids) - 1, self._current_file_row() + delta))
            self.selectFileExtended(ids[row], False, extend)

    @Slot(str, bool)
    def selectFile(self, file_id: str, toggle: bool) -> None:
        group = self._group()
        if self._is_busy() or group is None or not any(source.file_id == file_id for source in group.group.files):
            return

        selected = set(self._selected) if toggle else set()
        if file_id in selected:
            selected.remove(file_id)
        else:
            selected.add(file_id)

        self._selected = tuple(key for key in self._file_proxy.visible_ids() if key in selected)
        self._file_focus = file_id
        self._file_anchor = file_id
        self._refresh_review()

    @Slot()
    def selectAllFiles(self) -> None:
        group = self._group()
        if not self._is_busy() and group is not None:
            self._scope = "selected"
            self._selected = self._file_proxy.visible_ids()
            self._refresh_review()

    @Slot()
    def clearSelection(self) -> None:
        if not self._is_busy():
            self._scope = "selected"
            self._selected = ()
            self._refresh_review()

    @Slot(int)
    def moveFile(self, delta: int) -> None:
        group = self._group()
        if group is None or not group.group.files:
            return

        ids = self._file_proxy.visible_ids()
        current = self._current_file_row() if self._selected else (-1 if delta > 0 else len(ids))
        self.selectFile(ids[max(0, min(len(ids) - 1, current + delta))], False)

    @Slot(str, bool)
    def setIncluded(self, file_id: str, included: bool) -> None:
        group = self._group()
        if (
            self._is_busy()
            or self._edit is not None
            or group is None
            or group.requires_rescan
            or not any(source.file_id == file_id for source in group.group.files)
        ):
            return

        self._included = self._included | {file_id} if included else self._included - {file_id}
        self._files.set_included_file_ids(self._included)
        self.changed.emit()

    @Slot()
    def toggleSelectedIncluded(self) -> None:
        include = not set(self._selected).issubset(self._included)
        for file_id in self._selected:
            self.setIncluded(file_id, include)

    @Slot(str)
    def selectField(self, field: str) -> None:
        if self._edit is not None or field not in {item.value for item in MetadataField}:
            return

        self._field = MetadataField(field)
        self._field_anchor = field
        self._fields = (self._field,)
        self._restore_proposal()
        self._review_proxy.set_highlighted(frozenset({field}))
        self.changed.emit()

    @Slot(str, bool, bool)
    def selectFieldExtended(self, field: str, toggle: bool, extend: bool) -> None:
        if self._edit is not None:
            return
        anchor = self._field_anchor
        fields = self._extended(
            tuple(item.value for item in MetadataField),
            tuple(item.value for item in self._fields),
            field,
            toggle,
            extend,
            anchor,
        )
        self.selectField(field)
        if extend:
            self._field_anchor = anchor
        self._fields = tuple(MetadataField(item) for item in fields)
        if self._fields and self._field not in self._fields:
            self._field = self._fields[0]
        self._refresh_review()

    @Slot(int, bool)
    def moveFieldExtended(self, delta: int, extend: bool) -> None:
        fields = list(MetadataField)
        # Down from an untouched review starts on Title, rather than skipping
        # it merely because Title is the private fallback focus identity.
        current = fields.index(self._field) if self._fields else -1
        field = fields[max(0, min(len(fields) - 1, current + delta))]
        self.selectFieldExtended(field.value, False, extend)

    @Slot()
    def selectAllFields(self) -> None:
        if self._is_busy() or self._edit is not None or self._confirmation is not None or not self._scope_ids():
            return

        # Retain the focused field as the next Shift-selection anchor while
        # highlighting every displayed field. These identities describe review
        # scope only; they do not accept values or add files to the write batch.
        self._fields = tuple(MetadataField)
        self._field_anchor = self._field.value
        self._restore_proposal()
        self._review_proxy.set_highlighted(frozenset(field.value for field in self._fields))
        self.changed.emit()

    @Slot()
    def clearFieldSelection(self) -> None:
        if self._is_busy() or self._edit is not None or self._confirmation is not None:
            return

        # The private focus identity remains available, but an empty highlight
        # authorises no field action. Normal Down navigation then starts at
        # Title through moveFieldExtended's established empty-selection rule.
        self._fields = ()
        self._field_anchor = self._field.value
        self._restore_proposal()
        self._review_proxy.set_highlighted(frozenset())
        self.changed.emit()

    @Slot(str)
    def setReviewScope(self, scope: str) -> None:
        if scope in {"selected", "group", "included", "library"} and self._edit is None:
            self._scope = scope
            self._refresh_review()

    @Slot()
    def openReview(self) -> None:
        group = self._group()
        if not self._scope_ids() and group is not None and group.group.files:
            self.selectFile(self._file_proxy.visible_ids()[0], False)
        self._review_visible = True
        self.changed.emit()

    @Slot()
    def closeReview(self) -> None:
        self._review_visible = False
        self.changed.emit()

    def set_included_file_ids(self, ids: frozenset[str]) -> None:
        if not self._is_busy():
            known = {source.file_id for group in self.session_state.groups for source in group.group.files}
            self._included = ids & known
            self._files.set_included_file_ids(self._included)
            self._refresh_review()

    @Slot(bool)
    def includeSelection(self, include: bool) -> None:
        self.set_included_file_ids(
            self._included | set(self._selected) if include else self._included - set(self._selected)
        )

    @Slot()
    def includeScope(self) -> None:
        self.set_included_file_ids(self._included | set(self._scope_ids()))

    @Slot(str)
    def setProposal(self, key: str) -> None:
        if key in {option["key"] for option in self._proposals()}:
            self._proposal = int(key)
            self.changed.emit()

    @Slot(int)
    def moveField(self, delta: int) -> None:
        self.moveFieldExtended(delta, False)

    @Slot(result=bool)
    def beginEdit(self) -> bool:
        if not self._can_edit() or self._edit is not None:
            return False

        def category(field: MetadataField) -> str:
            if field in _LIST_FIELDS:
                return "list"

            if field in _POSITION_FIELDS:
                return "position"

            return "text"

        if len({category(field) for field in self._fields}) != 1:
            self.set_status("Choose fields of the same value type to set a common value.")
            return False

        self._edit = _EditTarget(self._scope_ids(), self._field, self.session_state.revision, self._fields)
        value = aggregate_review_fields(self.session_state, self._scope_ids(), (self._field,))[0].final.value
        self._edit_position = value if isinstance(value, Position) else Position()
        if isinstance(value, tuple):
            self._edit_value = "\n".join(value)
        elif isinstance(value, Position):
            self._edit_value = f"{value.number or ''}/{value.total or ''}"
        else:
            self._edit_value = value or ""

        self._edit_error = ""
        self.changed.emit()
        return True

    @Slot(int, int, result=bool)
    def commitPositionEdit(self, number: int, total: int) -> bool:
        """Translate the two Missing sentinels at the existing edit boundary."""
        if self._edit is None or self._edit.field not in _POSITION_FIELDS:
            return False

        # commitEdit still owns type/domain validation, immutable target IDs,
        # revision checks and cross-group confirmation for both editor forms.
        return self.commitEdit(f"{number or ''}/{total or ''}")

    @Slot(str, result=bool)
    def commitEdit(self, text: str) -> bool:
        target = self._edit
        if target is None:
            return False

        try:
            if target.file_ids != self._scope_ids() or target.fields != self._fields:
                raise ValueError("The selected files changed. Cancel and open the editor again.")

            value: FieldValue = text
            if target.field in _LIST_FIELDS:
                value = tuple(text.splitlines())
            elif target.field in _POSITION_FIELDS:
                parts = text.strip().split("/")
                if len(parts) > 2:
                    raise ValueError("Enter a number or number/total.")

                value = Position(
                    int(parts[0]) if parts[0].strip() else None,
                    int(parts[1]) if len(parts) == 2 and parts[1].strip() else None,
                )

            command = BatchReviewCommand(
                target.file_ids, target.fields, target.revision, BatchReviewAction.SET_COMMON_VALUE, value
            )
            if self._needs_cross_group(command):
                self._edit = None
                self._confirm_batch(command)
                return True

            result = apply_batch_review(self.session_state, command, self._rename)
            if result.blocked and not result.affected:
                raise ValueError("\n".join(dict.fromkeys(item.reason for item in result.blocked)))
        except (TypeError, ValueError) as error:
            self._edit_error = str(error)
            self.changed.emit()
            return False

        self.session_state = result.state
        self._edit = None
        self._status = (
            f"Review: {len(result.affected)} affected · {len(result.skipped)} skipped · "
            f"{len(result.blocked)} blocked. No files have been written.\n"
            + "\n".join(f"{item.file_id}: {item.reason}" for item in (*result.skipped, *result.blocked))
        )
        self._refresh()
        return True

    @Slot()
    def cancelEdit(self) -> None:
        self._edit = None
        self._edit_error = ""
        self.changed.emit()

    @Slot(str)
    def reviewAction(self, action: str) -> None:
        eligible = self._can_review_filenames() if action == "accept_safe_additions" else self._can_edit()
        if (
            not eligible
            or self._edit is not None
            or action not in {"keep_existing", "clear", "use_candidate", "accept_safe_additions"}
        ):
            return

        ids = self._scope_ids()
        if action == "use_candidate" and len(ids) == 1 and len(self._fields) == 1:
            group = next(
                group
                for group in self.session_state.groups
                if any(file.file_id == ids[0] for file in group.group.files)
            )
            try:
                self.set_state(
                    apply_field_decision(
                        self.session_state,
                        group.group.group_id,
                        ids[0],
                        self._field,
                        FieldDecisionKind.USE_PROPOSAL,
                        self._rename,
                        proposal_index=self._proposal,
                    )
                )
            except (TypeError, ValueError) as error:
                self.set_status(str(error))
            return

        fields = tuple(MetadataField) if action == "accept_safe_additions" else self._fields
        command = BatchReviewCommand(ids, fields, self.session_state.revision, BatchReviewAction(action))
        if self._needs_cross_group(command):
            self._confirm_batch(command)
        else:
            self._apply_batch(command)

    def _needs_cross_group(self, command: BatchReviewCommand) -> bool:
        groups = [
            group
            for group in self.session_state.groups
            if any(file.file_id in command.file_ids for file in group.group.files)
        ]
        return (
            len(groups) > 1
            and bool(set(command.fields) & {MetadataField.ALBUM, MetadataField.ALBUM_ARTISTS, MetadataField.DATE})
            and command.action
            in {BatchReviewAction.SET_COMMON_VALUE, BatchReviewAction.USE_CANDIDATE, BatchReviewAction.CLEAR}
        )

    def _confirm_batch(self, command: BatchReviewCommand) -> None:
        from dataclasses import replace

        self.request_confirmation(
            "Review scope spans albums",
            f"This action affects {len(command.file_ids)} files across multiple groups.\n"
            "Continue with this explicitly selected album-field scope?",
            lambda: self._apply_batch(replace(command, confirm_cross_group=True)),
        )

    def _apply_batch(self, command: BatchReviewCommand) -> None:
        try:
            result = apply_batch_review(self.session_state, command, self._rename)
            self.set_state(result.state)
            details = "\n".join(f"{item.file_id}: {item.reason}" for item in (*result.skipped, *result.blocked))
            self.set_status(
                f"Review: {len(result.affected)} affected · {len(result.skipped)} skipped · "
                f"{len(result.blocked)} blocked. No files changed.\n{details}"
            )
        except (TypeError, ValueError) as error:
            self.set_status(str(error))

    @Slot(bool)
    def renameAction(self, include: bool) -> None:
        eligible = self._can_rename() if include else self._can_review_filenames()
        if eligible:
            self._apply_batch(
                BatchReviewCommand(
                    self._scope_ids(),
                    (),
                    self.session_state.revision,
                    BatchReviewAction.INCLUDE_RENAMES if include else BatchReviewAction.KEEP_FILENAMES,
                )
            )

    def request_confirmation(self, title: str, message: str, action: Callable[[], None]) -> None:
        self._confirmation = (self.session_state, action)
        self._confirmation_title = title
        self._confirmation_text = message
        self.changed.emit()

    @Slot(result=bool)
    def confirmAction(self) -> bool:
        pending = self._confirmation
        self._confirmation = None
        if pending is None:
            return False
        if self.session_state is not pending[0] or self._is_busy():
            self.set_status("The review changed. Open the action again to confirm its current scope.")
            return False
        pending[1]()
        self.changed.emit()
        return True

    @Slot()
    def cancelConfirmation(self) -> None:
        self._confirmation = None
        self.changed.emit()

    @Slot()
    def undo(self) -> None:
        if self._edit is None and not self._is_busy() and not self._closed:
            self.session_state = undo_last_review_action(self.session_state, self._rename)
            self._status = "Last review action undone. Inclusion choices are unchanged."
            self._refresh()

    @Slot(QUrl, result=str)
    def pathFromUrl(self, url: QUrl) -> str:
        return url.toLocalFile()

    @Slot(str, bool, result=bool)
    def scan(self, path: str, discard_confirmed: bool) -> bool:
        if self._closed or self._is_busy() or self._edit is not None:
            return False

        try:
            root = Path(path.strip()).resolve()
            if not path.strip() or not root.is_dir():
                raise ValueError("Choose an existing music folder before scanning.")
            if self._pending() and not discard_confirmed:
                raise ValueError("Confirm discarding the current review choices before scanning again.")
        except (OSError, ValueError) as error:
            self._status = str(error)
            self.changed.emit()
            return False

        snapshot = self.session_state
        scanner = self._scanner
        operation_id = self._ids.next_id("SCAN")

        def work(token: CancellationToken, events: OperationEventSink) -> ScanLibraryResult:
            # This closure captures immutable lineage; it never reads a QObject
            # or the live session from the worker thread.
            return scanner.scan_library(
                operation_id=operation_id,
                base_session_revision=snapshot.revision,
                base_library_revision=snapshot.library_revision,
                root=root,
                cancellation=token,
                events=events,
            )

        return self.submit_operation(
            operation_id,
            OperationKind.SCAN,
            (),
            work,
            lambda state, result: apply_scan_library_result(state, cast(ScanLibraryResult, result)),
        )

    def submit_operation[T](
        self,
        operation_id: str,
        kind: OperationKind,
        targets: tuple[str, ...],
        work: OperationWork[T],
        reducer: Callable[[SessionState, object], SessionState | StateApplicationResult],
    ) -> bool:
        if self._closed or self._is_busy():
            return False

        try:
            started = begin_operation(self.session_state, operation_id, kind, targets)
        except (TypeError, ValueError) as error:
            self.set_status(str(error))
            return False

        self._reducers[operation_id] = reducer
        self._operation_kinds[operation_id] = kind
        self._progress_kind = kind
        self._progress_visible = True
        self._progress_log = []
        self._progress_stage = "Starting…"
        self._progress_failed = False
        self._progress_current = 0
        self._progress_total = 0
        self._progress_provider = ""
        self._progress_item = ""
        self._progress = -1.0
        self._cancelling = False
        self.set_state(started)
        try:
            self._handle = self.bridge.submit(operation_id, work)
        except Exception as error:
            self._on_failed(operation_id, error)
            # Facades listen to the bridge terminal boundary even for failures
            # before the executor can create its Future.
            self.bridge.failed.emit(operation_id, error)
            return False

        return True

    def _matches(self, operation_id: str) -> bool:
        active = self.session_state.active_operation
        return not self._closed and active is not None and active.operation_id == operation_id

    def _append_progress_log(self, message: str) -> None:
        # The limit counts actual text lines, including those inside a single
        # exception. Redact the complete message before splitting it so a
        # credential spanning structured text cannot evade sanitisation.
        lines = redact_sensitive_text(message).splitlines()
        self._progress_log = (self._progress_log + lines)[-_PROGRESS_LOG_LIMIT:]

    @Slot(object)
    def _on_event(self, event: object) -> None:
        if not self._matches(getattr(event, "operation_id", "")):
            return

        message = redact_sensitive_text(
            " · ".join(
                str(getattr(event, name))
                for name in ("stage", "file_id", "engine_id", "message", "status")
                if getattr(event, name, None) is not None
            )
        )
        if message:
            self._append_progress_log(message)

        if self._cancelling:
            self.changed.emit()
            return

        if isinstance(event, (OperationProgress, OperationStageChanged)):
            self._progress_stage = _short_progress_stage(
                redact_sensitive_text(event.stage.replace("_", " ").capitalize())
            )
            self._status = self._progress_stage
            self._progress_current = event.current if isinstance(event, OperationProgress) else 0
            self._progress_total = event.total if isinstance(event, OperationProgress) else 0
            self._progress = self._progress_current / self._progress_total if self._progress_total else -1.0

        # Provider and item context outlive an individual event. A progress
        # count does not carry these identities and must not erase them.
        if isinstance(event, ProviderStarted):
            self._progress_provider = redact_sensitive_text(event.engine_id)

        if isinstance(event, (FileStarted, FileStageChanged)):
            self._progress_item = redact_sensitive_text(event.source_path.name)
            if isinstance(event, FileStageChanged):
                self._progress_stage = event.stage.value.replace("_", " ").capitalize()
                self._status = self._progress_stage

        elif isinstance(event, (FileCompleted, FileFailed)):
            self._progress_item = ""

        self.changed.emit()

    def _finish(self, operation_id: str, message: str, *, stage: str = "Completed", failed: bool = False) -> None:
        matches = self._matches(operation_id)
        self.session_state = finish_operation(self.session_state, operation_id)

        if matches:
            self._handle = None
            self._cancelling = False
            self._status = redact_sensitive_text(message)
            self._progress_stage = _short_progress_stage(stage)
            self._progress_failed = failed
            self._append_progress_log(self._status)
            self._progress = -1.0

            # Every terminal Apply path transfers presentation to its retained
            # results, including a reducer exception after real writes. This
            # does not discard receipts or bypass scoped rescan handling.
            if self._progress_kind is OperationKind.APPLY:
                self._progress_visible = False

        self._refresh()

    @Slot(str, object)
    def _on_completed(self, operation_id: str, result: object) -> None:
        if self._closed or operation_id in self._reduced or operation_id not in self._reducers:
            return
        # The first late Apply result can describe actual disk writes. Always
        # reconcile it; its immutable reducer decides which lineage is stale.
        reducer = self._reducers.pop(operation_id)
        self._reduced.add(operation_id)
        kind = self._operation_kinds.pop(operation_id)
        matched = self._matches(operation_id)
        try:
            if getattr(result, "operation_id", operation_id) != operation_id:
                raise ValueError("The result belongs to a different operation.")
            reduced = reducer(self.session_state, result)
            self.session_state = reduced.state if isinstance(reduced, StateApplicationResult) else reduced
        except Exception as error:
            self._finish(
                operation_id, f"Result reconciliation failed: {error}",
                stage="Result reconciliation failed", failed=True,
            )
            self.controller_failed.emit(operation_id, error)
            return

        message = "Completed."
        accepted = not isinstance(reduced, StateApplicationResult) or reduced.status is ResultApplicationStatus.APPLIED
        if isinstance(result, ScanLibraryResult) and accepted:
            self._selected = ()
            self._included = frozenset()
            self._ensure_group()
            count = len(result.scan_result.supported_files)
            message = f"Scanned {count} supported files in {len(self.session_state.groups)} albums."
            settings_warning = cast("QuickLibrary", self._facade("library")).remember_root(result.root)
            if settings_warning:
                message += "\n" + settings_warning
            if self.session_state.unsupported_files:
                message += "\nUnsupported files:\n" + "\n".join(
                    str(file.path) for file in self.session_state.unsupported_files
                )
            if self.session_state.scan_issues:
                message += "\n" + "\n".join(issue.message for issue in self.session_state.scan_issues)
        elif isinstance(result, ScanLibraryResult):
            message = "The scan became outdated; the current review was preserved."

        if matched and kind is OperationKind.SCAN and accepted:
            self._progress_visible = False
        self._finish(operation_id, message)

    @Slot(str)
    def _on_cancelled(self, operation_id: str) -> None:
        if self._matches(operation_id):
            self._finish(operation_id, "Operation cancelled; the current review was preserved.", stage="Cancelled")

    @Slot(str, object)
    def _on_failed(self, operation_id: str, error: object) -> None:
        if self._matches(operation_id):
            self._finish(
                operation_id, f"Operation failed: {error}\nThe current review was preserved.",
                stage="Failed", failed=True,
            )

    @Slot()
    def cancelScan(self) -> None:
        if self._handle is not None and not self._cancelling:
            self._handle.cancel()
            self._cancelling = True
            self._status = "Cancelling operation…"
            self.cancellation_requested.emit(self._handle.operation_id)
            self.changed.emit()

    @Slot()
    def showProgress(self) -> None:
        self._progress_visible = True
        self.changed.emit()

    @Slot()
    def hideProgress(self) -> None:
        if self._is_busy() and self._progress_kind is OperationKind.APPLY:
            self.cancelScan()
        else:
            self._progress_visible = False
            self.changed.emit()

    def shutdown(self) -> None:
        """Keep the bridge alive until cooperative worker cancellation has joined."""
        if not self._closed:
            self.cancelScan()
            self._closed = True
            self.executor.shutdown(wait=True)
            if self._lookup_ui is not None:
                cast("QuickLookup", self._lookup_ui).close()
            self.credentials.forget()
            self._layout.persist()
