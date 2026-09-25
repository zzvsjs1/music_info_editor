"""Portable folder memory and diagnostic receipts match the shared workflows."""

import json
from dataclasses import replace

import pytest

from metadata_polisher.application.lookup import GroupLookupResult
from metadata_polisher.execution.cancellation import NeverCancelledToken
from metadata_polisher.infrastructure.settings import AppSettings, RenameSettings, load_settings, save_settings
from metadata_polisher.session.apply_preparation import prepare_apply_request
from metadata_polisher.session.state import GroupSelection, SessionState
from metadata_polisher.ui.quick.backend import QuickBackend
from metadata_polisher.ui.quick.diagnostics import QuickDiagnostics
from metadata_polisher.ui.quick.library import QuickLibrary
from tests.ui.helpers import ControlledExecutor, changed_local_session
from tests.ui.test_quick_apply import ResultService
from tests.unit.session.test_lookup_editing import make_selected_session
from tests.unit.session.test_review_editing import make_local_session, make_source


@pytest.fixture
def make_backend(qapp):
    instances = []

    def make(**kwargs):
        backend = QuickBackend(executor=ControlledExecutor(), **kwargs)
        instances.append(backend)
        return backend

    yield make
    for backend in instances:
        backend.shutdown()


def test_successful_scan_root_is_remembered_without_resetting_preferences(make_backend, tmp_path):
    path = tmp_path / "settings.json"
    settings = AppSettings(rename=RenameSettings(template="%title%"))
    save_settings(path, settings)
    root = tmp_path / "scanned"
    backend = make_backend(settings=settings, settings_file=path, state=SessionState(root=root))
    library = QuickLibrary(backend)
    assert library.remember_root(root) == ""
    assert backend.app_settings.general.last_root_folder == str(root)
    assert load_settings(path).settings == backend.app_settings
    assert backend.app_settings.rename == settings.rename


@pytest.mark.parametrize("change", ("external", "corrupt", "new_ui"))
def test_scan_root_memory_preserves_external_or_invalid_settings(make_backend, tmp_path, change):
    path = tmp_path / "settings.json"
    settings = AppSettings()
    save_settings(path, settings)
    root = tmp_path / "scanned"
    backend = make_backend(settings=settings, settings_file=path, state=SessionState(root=root))
    library = QuickLibrary(backend)
    if change == "external":
        save_settings(path, replace(settings, rename=RenameSettings(template="%album%")))
    elif change == "corrupt":
        path.write_text("{broken", encoding="utf-8")
    else:
        document = json.loads(path.read_text(encoding="utf-8"))
        document["ui"]["version"] = 999
        path.write_text(json.dumps(document), encoding="utf-8")
    before = path.read_bytes()
    assert library.remember_root(root)
    assert path.read_bytes() == before
    assert backend.app_settings.general.last_root_folder == str(root)


class RecordingTrace:
    enabled = True

    def __init__(self):
        self.records = []

    def record(self, operation_id, stage, details):
        self.records.append((operation_id, stage, details))


def test_lookup_trace_uses_result_group_and_rejects_obsolete_receipts(make_backend):
    state = make_selected_session()
    target = state.groups[0]
    other = make_local_session(make_source("other.flac")).groups[0]
    other = replace(other, group=replace(other.group, group_id="other"))
    state = replace(state, groups=(target, other), selection=GroupSelection("other"))
    backend = make_backend(state=state)
    diagnostics = QuickDiagnostics(backend)
    trace = RecordingTrace()
    diagnostics.trace = trace
    result = GroupLookupResult("LOOKUP-0001", state.revision, state.library_revision,
                               target.group.group_id, target.revision, target.candidate_lookup)
    diagnostics.completed(result.operation_id, result)
    assert len(trace.records) == 1
    assert trace.records[0][1] == "lookup_result"
    assert trace.records[0][2]["group_id"] == "album"
    backend.set_state(make_local_session(make_source("replacement.flac")))
    diagnostics.completed(result.operation_id, result)
    assert len(trace.records) == 1


def test_apply_trace_retains_transaction_truth_and_cancel_lifecycle(make_backend, caplog):
    state = changed_local_session()
    backend = make_backend(state=state)
    diagnostics = QuickDiagnostics(backend)
    trace = RecordingTrace()
    diagnostics.trace = trace
    ids = tuple(source.file_id for group in state.groups for source in group.group.files)
    request = prepare_apply_request(state, ids, backend.app_settings, "APPLY-0001")
    result = ResultService().apply(request, cancellation=NeverCancelledToken(), events=None)
    diagnostics.completed(result.operation_id, result)
    assert trace.records[0][1] == "apply_result"
    assert trace.records[0][2]["status"] == "succeeded"
    assert trace.records[0][2]["report_status"] == "disabled"
    assert [row["file_id"] for row in trace.records[0][2]["files"]] == list(ids)
    assert all(row["status"] == "applied" and "final_path" in row and "reason_codes" in row
               for row in trace.records[0][2]["files"])
    with caplog.at_level("INFO"):
        backend.bridge.cancelled.emit("CANCEL-0001")
    assert "Operation CANCEL-0001 cancelled" in caplog.text


def test_explanation_rows_retain_zero_contributions_and_full_details(make_backend):
    state = make_selected_session()
    backend = make_backend(state=state)
    rows = backend._diagnostics.evidence_rows(False)
    selected = state.groups[0].selected_release.identity
    evidence = next(entry.result.evidence for entry in state.groups[0].release_ranking.entries
                    if entry.identity == selected)
    assert rows == [{"reason": item.code, "contribution": f"{item.contribution:g}", "detail": item.detail}
                    for item in evidence]
