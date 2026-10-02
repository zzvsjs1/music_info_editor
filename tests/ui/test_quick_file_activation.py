"""File activation must use the clicked identity after scrolling and reuse."""

from dataclasses import replace
from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtQuick import QQuickItem, QQuickWindow
from PySide6.QtTest import QSignalSpy, QTest

from metadata_polisher.domain.metadata import Position
from metadata_polisher.session.state import GroupSelection, SessionState
from metadata_polisher.ui.quick.application import create_quick_engine
from metadata_polisher.ui.quick.backend import QuickBackend
from tests.ui.helpers import ControlledExecutor, make_group
from tests.ui.test_quick_table_interaction import viewport, visual_items


@pytest.fixture
def file_scene(qapp, qtbot, tmp_path):
    base = make_group("album", "track-01", "Track 01")
    source = base.group.files[0]
    files = tuple(
        replace(
            source,
            file_id=f"track-{number:02d}",
            path=Path("library") / f"track-{number:02d}.flac",
            read_result=replace(
                source.read_result,
                metadata=replace(source.read_result.metadata, title=f"Track {number:02d}",
                                 track=Position(number, 34)),
            ),
        )
        for number in range(1, 35)
    )
    group = replace(base, group=replace(base.group, files=files))
    state = SessionState(root=Path("library"), groups=(group,), selection=GroupSelection("album"))
    backend = QuickBackend(state=state, executor=ControlledExecutor(), settings_file=tmp_path / "settings.json")
    backend.selectFile("track-16", False)
    backend.set_included_file_ids(frozenset({"track-02"}))
    warnings = []
    engine = create_quick_engine(backend, warnings=warnings)
    window = engine.rootObjects()[0]
    window.resize(1120, 720)
    window.requestActivate()
    assert QTest.qWaitForWindowActive(window, 2000)
    qtbot.wait(80)

    try:
        yield window, backend
    finally:
        backend.shutdown()

        for child in window.findChildren(QQuickWindow):
            child.hide()

        window.hide()
        engine.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        assert not warnings, warnings


def cell_point(window, table, identity):
    # Keep visual-only delegates alive while their Python wrappers are used.
    items = list(visual_items(table))
    window._activation_items = getattr(window, "_activation_items", []) + items
    cell = next(item for item in items
                if item.property("stableId") == identity and item.property("column") == 2)
    point = cell.mapToScene(QPointF(cell.width() / 2, cell.height() / 2))
    view = viewport(table)
    local = view.mapFromScene(point)
    assert 0 < local.x() < view.width() and 0 < local.y() < view.height()
    return point.toPoint()


@pytest.mark.parametrize("review_open", [False, True])
@pytest.mark.parametrize("selecting_click", [False, True])
def test_double_click_after_wheel_reviews_the_hit_file(file_scene, qtbot, review_open, selecting_click):
    window, backend = file_scene
    table = window.findChild(QQuickItem, "fileTable")
    view = viewport(table)
    original_state = backend.session_state
    original_inclusion = backend.includedFileIds

    if review_open:
        backend.openReview()
        qtbot.waitUntil(lambda: backend.reviewVisible)
        window.requestActivate()
        assert QTest.qWaitForWindowActive(window, 2000)

    # Scrolling changes the visible delegates while track 16 remains selected.
    # Cover both an ordinary pointer sequence and activation delivered without
    # a selecting click: the activation's stable ID must be sufficient itself.
    initial_y = view.property("contentY")
    position = view.mapToScene(QPointF(view.width() / 2, view.height() / 2))
    event = QWheelEvent(
        position, QPointF(window.mapToGlobal(position.toPoint())), QPoint(), QPoint(0, -120),
        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False,
    )
    QCoreApplication.sendEvent(window, event)
    qtbot.waitUntil(lambda: view.property("contentY") > initial_y + table.property("rowHeight") * 2)
    qtbot.waitUntil(lambda: not view.property("moving"))
    assert backend.selectedFileIds == ["track-16"]
    activated = QSignalSpy(table.cellActivated)
    point = cell_point(window, table, "track-19")

    if selecting_click:
        QTest.mouseDClick(window, Qt.MouseButton.LeftButton, pos=point)
    else:
        table.cellActivated.emit("track-19", "track-19.flac", 2)

    qtbot.waitUntil(lambda: activated.count() == 1)
    assert activated.at(0)[0] == "track-19"
    assert backend.selectedFileIds == ["track-19"]
    assert backend.reviewVisible
    assert backend.review.index(0, 2).data() == "Track 19"
    assert backend.includedFileIds == original_inclusion
    assert backend.session_state is original_state
    assert not backend.executor.pending


def test_activation_inside_highlighted_files_preserves_the_batch(file_scene, qtbot):
    window, backend = file_scene
    backend.selectFile("track-19", True)
    table = window.findChild(QQuickItem, "fileTable")
    original_state = backend.session_state
    original_inclusion = backend.includedFileIds
    table.cellActivated.emit("track-19", "track-19.flac", 2)

    qtbot.waitUntil(lambda: backend.reviewVisible)
    assert backend.selectedFileIds == ["track-16", "track-19"]
    assert backend.review.index(0, 2).data() == "Mixed values"
    assert backend.includedFileIds == original_inclusion
    assert backend.session_state is original_state
    assert not backend.executor.pending


@pytest.mark.parametrize(("identity", "column"), [("removed-file", 2), ("track-19", 0)])
def test_invalid_or_inclusion_cell_activation_does_not_open_old_review(file_scene, identity, column):
    window, backend = file_scene
    table = window.findChild(QQuickItem, "fileTable")
    table.cellActivated.emit(identity, "", column)

    assert not backend.reviewVisible
    assert backend.selectedFileIds == ["track-16"]
    assert backend.includedFileIds == ["track-02"]
    assert not backend.executor.pending
