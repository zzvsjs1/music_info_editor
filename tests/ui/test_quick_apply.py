"""Only a current, explicitly confirmed QML summary may submit transactions."""

from dataclasses import replace

import pytest
from PySide6.QtCore import Signal

from metadata_polisher.application.apply import (
    ApplyBatchResult,
    ApplyBatchStatus,
    ApplyFileOutcome,
    ApplyFileOutcomeStatus,
    ApplyGroupOutcome,
)
from metadata_polisher.application.changes import RenameDecision, build_change_set
from metadata_polisher.domain.metadata import MetadataField
from metadata_polisher.domain.review import FieldDecisionKind
from metadata_polisher.execution.cancellation import OperationCancelledError
from metadata_polisher.execution.events import FileApplyStatus, FileSkipReason, FileTransactionStage
from metadata_polisher.infrastructure.reporting import ReportWriteResult, ReportWriteStatus
from metadata_polisher.infrastructure.settings import RenameSettings
from metadata_polisher.infrastructure.transaction import FileApplyResult
from metadata_polisher.session.review_editing import apply_field_decision
from metadata_polisher.session.state import finish_operation
from metadata_polisher.ui.quick.apply import QuickApply
from metadata_polisher.ui.quick.backend import QuickBackend
from tests.ui.helpers import changed_local_session
from tests.ui.test_quick_lookup import LookupHost, complete


class ApplyHost(LookupHost):
    controller_failed = Signal(str, object)

    def __init__(self, state):
        super().__init__(state)
        self._included = frozenset(source.file_id for group in state.groups for source in group.group.files)
        self._selected = tuple(self._included)
        self.app_dir = None

    def _scope_ids(self):
        return self._selected

    def set_included_file_ids(self, ids):
        self._included = frozenset(ids)
        self.changed.emit()


class ResultService:
    """Produce validated receipts for fixtures without invoking filesystem code."""

    def __init__(self):
        self.requests = []
        self.failure = False
        self.refresh = True
        self.stop_after_first = False

    def apply(self, request, *, cancellation, events):
        self.requests.append(request)
        if self.failure:
            raise RuntimeError("Worker failed without a receipt")
        if cancellation.is_cancelled():
            raise OperationCancelledError()

        groups = []
        position = 0
        for group in request.groups:
            outcomes = []
            for item in group.files:
                source = item.source
                changes = build_change_set(source, item.reviews, item.rename_decision,
                                           request.rename_template, request.rename_policy)
                if self.stop_after_first and position:
                    outcome = ApplyFileOutcome(source.file_id, source.path, group.selected_release, item.reviews,
                                               changes, ApplyFileOutcomeStatus.SKIPPED,
                                               skip_reason=FileSkipReason.CANCELLED_BEFORE_START)
                elif not changes.metadata_changes and changes.rename_change is None:
                    outcome = ApplyFileOutcome(source.file_id, source.path, group.selected_release, item.reviews,
                                               changes, ApplyFileOutcomeStatus.NO_CHANGES)
                else:
                    final = changes.rename_change.new_path if changes.rename_change else source.path
                    refreshed = replace(source, path=final, read_result=replace(
                        source.read_result, metadata=changes.final_metadata,
                    )) if self.refresh else None
                    outcome = ApplyFileOutcome(
                        source.file_id, source.path, group.selected_release, item.reviews, changes,
                        ApplyFileOutcomeStatus.APPLIED,
                        transaction_result=FileApplyResult(source.path, final, FileApplyStatus.SUCCEEDED,
                                                           FileTransactionStage.COMPLETED),
                        refreshed_source=refreshed,
                    )
                outcomes.append(outcome)
                position += 1
            groups.append(ApplyGroupOutcome(group.group_id, group.base_group_revision, tuple(outcomes)))

        return ApplyBatchResult(request.operation_id, request.base_session_revision, request.base_library_revision,
                                ApplyBatchStatus.CANCELLED if self.stop_after_first else ApplyBatchStatus.SUCCEEDED,
                                tuple(groups), ReportWriteResult(ReportWriteStatus.DISABLED, None, None))


@pytest.fixture
def apply_ui(qapp):
    host = ApplyHost(changed_local_session())
    service = ResultService()
    return host, QuickApply(host, apply_service=service), service


def test_summary_cancel_and_highlighting_never_submit(apply_ui):
    host, facade, service = apply_ui
    snapshot = host.session_state
    assert facade.beginApply()
    assert facade.summaryVisible and facade.summaryCanApply
    assert len(facade.summaryRows) == 2
    assert not host.executor.pending
    facade.cancelApply()
    assert not facade.summaryVisible
    assert not host.executor.pending and not service.requests
    assert host.session_state is snapshot


@pytest.mark.parametrize("change", ("state", "settings", "inclusion"))
def test_confirmation_rejects_stale_captured_inputs(apply_ui, change):
    host, facade, _ = apply_ui
    assert facade.beginApply()
    if change == "state":
        host.set_state(replace(host.session_state, revision=host.session_state.revision + 1))
    elif change == "settings":
        host.app_settings = replace(host.app_settings, rename=RenameSettings(template="%title%"))
    else:
        host.set_included_file_ids(frozenset())
    assert not facade.confirmApply()
    assert not host.executor.pending
    assert facade.summaryError


def test_verified_completion_consumes_inclusion_and_keeps_reusable_results(apply_ui, qtbot):
    host, facade, service = apply_ui
    assert facade.beginApply()
    assert facade.confirmApply()
    assert not facade.confirmApply()
    complete(qtbot, host)
    assert len(service.requests) == 1
    assert not host._included
    assert not host.session_state.groups[0].reviewed_files
    assert len(host.session_state.written_files) == 2
    assert facade.resultsVisible
    assert [row["status"] for row in facade.resultsRows] == ["applied", "applied"]
    original_rows = facade.resultsRows
    facade.closeResults()
    facade.showResults()
    assert facade.resultsRows == original_rows


def test_cancelled_partial_batch_retains_unstarted_inclusion(apply_ui, qtbot):
    host, facade, service = apply_ui
    service.stop_after_first = True
    second_id = host.session_state.groups[0].group.files[1].file_id
    assert facade.beginApply() and facade.confirmApply()
    complete(qtbot, host)
    assert host._included == frozenset((second_id,))
    assert "1 not started" in facade.resultsText
    assert facade.resultsRows[1]["status"] == "not_started"


def test_unknown_outcomes_require_rescan_and_keep_all_inclusion(apply_ui, qtbot):
    host, facade, service = apply_ui
    service.failure = True
    included = host._included
    assert facade.beginApply() and facade.confirmApply()
    with qtbot.waitSignal(host.bridge.failed):
        host.executor.run_next()
    assert host.session_state.groups[0].requires_rescan
    assert host._included == included
    assert all(row["status"] == "unknown" for row in facade.resultsRows)
    assert "unknown" in facade.resultsText.lower()


def test_noop_consumption_requires_the_exact_review(apply_ui, qtbot):
    host, facade, _ = apply_ui
    source = host.session_state.groups[0].group.files[1]
    host.set_state(apply_field_decision(host.session_state, "album", source.file_id, MetadataField.TITLE,
                                       FieldDecisionKind.KEEP_EXISTING, host.app_settings.rename))
    assert facade.beginApply() and facade.confirmApply()
    complete(qtbot, host)
    assert not host._included
    assert facade.resultsRows[1]["status"] == "no_changes"


def test_rename_draft_cancel_preserves_live_choices_and_accept_opens_summary(apply_ui):
    host, facade, _ = apply_ui
    host.app_settings = replace(host.app_settings, rename=RenameSettings(template="%title%"))
    snapshot = host.session_state
    assert facade.beginRename()
    assert facade.renameCanAccept
    assert host.session_state is snapshot
    facade.cancelRename()
    assert host.session_state is snapshot
    assert not host.executor.pending
    assert facade.beginRename()
    assert facade.acceptRename()
    assert facade.summaryVisible
    assert not host.executor.pending
    assert all(item.change_set.rename_decision is RenameDecision.APPLY_RENAME
               for item in host.session_state.groups[0].reviewed_files)


def test_refresh_failure_does_not_consume_inclusion(apply_ui, qtbot):
    host, facade, service = apply_ui
    service.refresh = False
    included = host._included
    assert facade.beginApply() and facade.confirmApply()
    complete(qtbot, host)
    assert host._included == included
    assert host.session_state.groups[0].requires_rescan


def test_real_coordinator_reconciles_first_late_apply_and_ignores_duplicate(qapp, qtbot):
    from tests.ui.helpers import ControlledExecutor

    backend = QuickBackend(state=changed_local_session(), executor=ControlledExecutor())
    service = ResultService()
    facade = QuickApply(backend, apply_service=service)
    included = frozenset(source.file_id for group in backend.session_state.groups for source in group.group.files)
    backend.set_included_file_ids(included)
    assert facade.beginApply() and facade.confirmApply()
    operation_id = backend.session_state.active_operation.operation_id

    # Model an obsolete operation marker before its first queued result arrives.
    # Positive disk effects still invalidate stale snapshots; they cannot simply
    # disappear because the active ID has already changed or been cleared.
    backend.set_state(finish_operation(backend.session_state, operation_id))
    complete(qtbot, backend)
    assert backend.session_state.groups[0].requires_rescan
    assert backend._included == included
    assert all(row["status"] == "applied" for row in facade.resultsRows)
    snapshot = backend.session_state
    rows = facade.resultsRows
    backend.bridge.completed.emit(operation_id, facade.last_result)
    assert backend.session_state is snapshot
    assert facade.resultsRows == rows
    backend.shutdown()
