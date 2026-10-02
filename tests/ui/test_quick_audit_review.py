"""Review evidence remains identifiable, complete and current before any write."""

from dataclasses import replace
from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtQuick import QQuickItem, QQuickWindow

from metadata_polisher.session.state import GroupSelection, SessionState
from metadata_polisher.ui.quick.application import create_quick_engine
from metadata_polisher.ui.quick.backend import QuickBackend
from tests.ui.helpers import ControlledExecutor, make_group
from tests.unit.session.test_batch_review import make_batch_session


@pytest.fixture
def audit_backend(qapp, tmp_path):
    instances = []

    def create(state):
        backend = QuickBackend(
            state=state,
            executor=ControlledExecutor(),
            settings_file=tmp_path / "settings.json",
        )
        instances.append(backend)

        return backend

    yield create

    for backend in instances:
        backend.shutdown()


def _same_named_files():
    # Stable IDs distinguish the tracks internally, while their identical
    # basenames require directory context in the user's final confirmation.
    groups = []

    for name in ("first", "second"):
        group = make_group(name, name, f"Original {name}")
        source = replace(group.group.files[0], path=Path("library") / name / "01.flac")
        groups.append(replace(group, group=replace(group.group, files=(source,))))

    return SessionState(Path("library"), groups=tuple(groups), selection=GroupSelection("first"))


def _prepare_same_named_changes(backend):
    for name in ("first", "second"):
        backend.selectGroup(name)
        backend.selectFile(name, False)
        backend.selectField("title")
        assert backend.beginEdit()
        assert backend.commitEdit(f"Corrected {name}")

    backend.set_included_file_ids(frozenset({"first", "second"}))
    assert backend.applyUi.beginApply()


def test_final_confirmation_distinguishes_same_names_and_exposes_actual_changes(audit_backend):
    backend = audit_backend(_same_named_files())
    _prepare_same_named_changes(backend)
    rows = backend.applyUi.summaryRows

    assert [row["file"] for row in rows] == ["first/01.flac", "second/01.flac"]

    for name, row in zip(("first", "second"), rows, strict=True):
        assert f"Title: Original {name} → Corrected {name}" in row.get("fieldDetails", "")

    assert not backend.executor.pending


def test_final_confirmation_qml_details_retain_paths_and_before_after_values(audit_backend, qtbot):
    backend = audit_backend(_same_named_files())
    warnings = []
    engine = create_quick_engine(backend, warnings=warnings)
    main = engine.rootObjects()[0]

    try:
        _prepare_same_named_changes(backend)
        window = main.findChild(QQuickWindow, "applySummaryWindow")
        assert window is not None
        qtbot.waitUntil(window.isVisible)
        table = window.findChild(QQuickItem, "applySummaryFilesTable")
        assert table is not None
        table.setProperty("currentId", "second")
        qtbot.waitUntil(lambda: "Original second" in table.property("details"))
        details = table.property("details")

        assert "second/01.flac" in details
        assert "Title: Original second → Corrected second" in details
        assert not backend.executor.pending
    finally:
        for child in main.findChildren(QQuickWindow):
            child.hide()

        main.hide()
        engine.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    assert not warnings, warnings


def test_bulk_full_values_include_each_proposal_and_its_source_before_acceptance(audit_backend):
    backend = audit_backend(make_batch_session(absent_title_candidate=1))
    backend.selectAllFiles()
    backend.selectField("title")
    original = backend.session_state
    details = backend.fieldDetails

    for filename in ("01.flac", "02.flac", "03.flac"):
        assert f"File: Album/{filename}" in details

    assert "Existing: Local first" in details
    assert "Proposed: Candidate 1" in details
    assert "Final: Local first" in details
    assert "Proposed: No suggestion" in details
    assert "Proposed: Candidate 3" in details
    assert "Sources: musicbrainz / musicbrainz" in details
    assert "Record: release" in details
    assert backend.session_state is original
    assert not backend.executor.pending


def test_open_filename_preview_follows_edits_and_filename_decisions(audit_backend):
    backend = audit_backend(make_batch_session())
    source = backend.session_state.groups[0].group.files[0]
    backend.selectFile(source.file_id, False)
    backend.applyUi.showPreviews()
    previous_name = backend.applyUi.previewRows[0]["preview"]
    backend.selectField("title")
    assert backend.beginEdit()
    assert backend.commitEdit("New preview title")
    rows = backend.applyUi.previewRows

    assert backend.applyUi.previewsVisible
    assert rows[0]["preview"] != previous_name
    assert "New preview title" in rows[0]["preview"]
    backend.renameAction(True)
    assert backend.applyUi.previewRows[0]["decision"] == "Include rename"
    backend.renameAction(False)
    assert backend.applyUi.previewRows[0]["decision"] == "Keep filename"
    assert not backend.executor.pending


def test_open_filename_preview_follows_scope_and_clears_removed_targets(audit_backend):
    backend = audit_backend(make_batch_session())
    first, second, third = backend.session_state.groups[0].group.files
    backend.selectFile(first.file_id, False)
    backend.applyUi.showPreviews()
    backend.selectFile(second.file_id, False)

    assert [row["id"] for row in backend.applyUi.previewRows] == [second.file_id]
    backend.setReviewScope("library")
    assert [row["id"] for row in backend.applyUi.previewRows] == [
        first.file_id, second.file_id, third.file_id,
    ]
    backend.clearSelection()
    assert backend.applyUi.previewRows == []
    assert backend.applyUi.previewsVisible
    assert not backend.executor.pending
