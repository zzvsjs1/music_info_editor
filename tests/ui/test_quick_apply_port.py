"""Exercise Apply dialogue geometry and keyboard use against the real QML scene."""

from dataclasses import replace

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPointF, Qt
from PySide6.QtQuick import QQuickItem, QQuickWindow
from PySide6.QtTest import QTest

from metadata_polisher.application.apply_summary import ApplySummaryIssue
from metadata_polisher.application.changes import ChangeIssueCode
from metadata_polisher.infrastructure.settings import AppSettings, RenameSettings, UiSettings
from metadata_polisher.ui.quick.application import create_quick_engine
from metadata_polisher.ui.quick.backend import QuickBackend
from tests.ui.helpers import ControlledExecutor, changed_local_session
from tests.ui.test_quick_apply import ResultService


@pytest.fixture
def apply_scene(qapp):
    # Stored Widgets keys must continue to size their QML replacements.
    settings = AppSettings(
        rename=RenameSettings(template="%title%"),
        ui=UiSettings(dialog_sizes={"ApplySummaryDialog": (820, 560)}),
    )
    host = QuickBackend(state=changed_local_session(), settings=settings, executor=ControlledExecutor())
    host.set_included_file_ids(frozenset(source.file_id for source in host.session_state.groups[0].group.files))
    warnings = []
    engine = create_quick_engine(host, warnings=warnings)
    main = engine.rootObjects()[0]

    try:
        yield main, host
    finally:
        for child in main.findChildren(QQuickWindow):
            child.hide()

        main.hide()
        engine.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        host.shutdown()
        assert not warnings, warnings


def _window(main, name):
    window = main.findChild(QQuickWindow, name)
    assert window is not None
    window.requestActivate()
    assert QTest.qWaitForWindowActive(window, 2000)
    QTest.qWait(60)
    return window


def _row_view(window):
    # Identify the visible row navigation surface by Qt's ListView contract,
    # without depending on the reusable component's private QML IDs.
    return next(
        item for item in window.findChildren(QQuickItem)
        if item.isVisible() and item.metaObject().indexOfProperty("currentIndex") >= 0
        and item.metaObject().indexOfProperty("count") >= 0
        and item.property("count") == 2
    )


def _visual_items(item):
    # Repeater delegates belong to the visual hierarchy even when QObject's
    # ownership hierarchy does not include them beneath the window.
    for child in item.childItems():
        yield child
        yield from _visual_items(child)


def test_confirmation_rows_explain_the_actual_changed_fields(apply_scene):
    _, host = apply_scene
    assert host.applyUi.beginApply()
    assert [row.get("fieldNames") for row in host.applyUi.summaryRows] == ["Title", "Title"]
    assert [row.get("id") for row in host.applyUi.summaryRows] == [
        source.file_id for source in host.session_state.groups[0].group.files
    ]
    assert not host.executor.pending


def test_long_apply_summary_keeps_actions_inside_the_window(apply_scene):
    main, host = apply_scene
    assert host.applyUi.beginApply()
    captured = host.applyUi._confirmation
    issues = tuple(
        ApplySummaryIssue(f"file-{index}", ChangeIssueCode.DESTINATION_COLLISION, "Destination exists. " * 20)
        for index in range(40)
    )
    host.applyUi._confirmation = replace(captured, summary=replace(captured.summary, blocking_issues=issues))
    host.applyUi.changed.emit()
    window = _window(main, "applySummaryWindow")
    window.resize(640, 480)
    QTest.qWait(80)

    for name in ("cancelApplyButton", "confirmApplyButton"):
        button = window.findChild(QQuickItem, name)
        bottom = button.mapToScene(QPointF(0, button.height())).y()
        assert 0 < bottom <= window.height()

    # The complete diagnostic remains selectable, rather than being elided to
    # make the buttons fit. Keyboard copying must retain the last conflict.
    summaries = [
        item for item in window.findChildren(QQuickItem)
        if item.property("text") == host.applyUi.summaryText
    ]
    assert any(item.property("readOnly") is True and item.property("selectByMouse") is True for item in summaries)
    assert "file-39" in host.applyUi.summaryText
    assert not host.executor.pending


def test_short_rename_summary_leaves_room_for_the_file_table(apply_scene):
    main, host = apply_scene
    first = host.session_state.groups[0].group.files[0]
    host.selectFile(first.file_id, False)
    assert host.applyUi.beginRename()
    window = _window(main, "renameFilesWindow")
    summary = window.findChild(QQuickItem, "renameSummaryText")
    table = window.findChild(QQuickItem, "renameFilesTable")
    assert summary is not None and table is not None

    # Two lines of status must not keep a large blank diagnostic pane open.
    # A bounded summary still leaves the rename rows as the main work area.
    assert 0 < summary.height() < 96
    assert table.mapToScene(QPointF()).y() >= summary.mapToScene(QPointF(0, summary.height())).y()
    assert table.height() > summary.height()


def test_long_rename_summary_stays_bounded_above_actions(apply_scene):
    main, host = apply_scene
    first = host.session_state.groups[0].group.files[0]
    host.selectFile(first.file_id, False)
    assert host.applyUi.beginRename()
    window = _window(main, "renameFilesWindow")
    window.resize(640, 480)
    host.applyUi._rename_summary = "Long validation detail.\n" * 100
    host.applyUi.changed.emit()
    QTest.qWait(80)
    summary = window.findChild(QQuickItem, "renameSummaryText")
    table = window.findChild(QQuickItem, "renameFilesTable")
    accept = window.findChild(QQuickItem, "acceptRenameButton")
    assert summary is not None and table is not None and accept is not None

    assert 0 < summary.height() <= 192
    assert table.height() >= 192
    assert accept.mapToScene(QPointF(0, accept.height())).y() <= window.height()


@pytest.mark.parametrize("kind", ("summary", "rename", "previews", "results"))
def test_apply_tables_expose_full_row_details_through_keyboard(apply_scene, qtbot, kind):
    main, host = apply_scene
    first, second = host.session_state.groups[0].group.files
    host.selectFile(first.file_id, False)
    host.selectFile(second.file_id, True)
    original = host.session_state
    included = host.includedFileIds

    if kind == "summary":
        assert host.applyUi.beginApply()
        name = "applySummaryWindow"
    elif kind == "rename":
        assert host.applyUi.beginRename()
        name = "renameFilesWindow"
    elif kind == "previews":
        host.applyUi.showPreviews()
        name = "renamePreviewsWindow"
    else:
        host.applyUi._service = ResultService()
        assert host.applyUi.beginApply() and host.applyUi.confirmApply()
        host.executor.run_next()
        qtbot.waitUntil(lambda: host.applyUi.resultsVisible)
        name = "applyResultsWindow"

    window = _window(main, name)
    view = _row_view(window)
    view.forceActiveFocus()
    QTest.keyClick(window, Qt.Key.Key_Home)
    QTest.keyClick(window, Qt.Key.Key_Down)
    QTest.qWait(30)
    details = [
        item.property("text") for item in window.findChildren(QQuickItem)
        if item.isVisible() and item.property("readOnly") is True
    ]
    assert any("second.flac" in str(text) for text in details)

    if kind == "rename":
        QTest.keyClick(window, Qt.Key.Key_Space)
        rows = {row["id"]: row for row in host.applyUi.renameRows}
        assert rows[first.file_id]["included"] is True
        assert rows[second.file_id]["included"] is False

    if kind != "results":
        assert host.session_state is original
        assert host.includedFileIds == included
        assert not host.executor.pending


def test_apply_summary_restores_the_existing_widgets_window_size(apply_scene):
    main, host = apply_scene
    assert host.applyUi.beginApply()
    window = _window(main, "applySummaryWindow")
    assert (window.width(), window.height()) == (820, 560)


def test_confirmation_column_can_be_resized_with_its_header(apply_scene):
    main, host = apply_scene
    assert host.applyUi.beginApply()
    window = _window(main, "applySummaryWindow")
    header = next(item for item in _visual_items(window.contentItem()) if item.property("text") == "File")
    before = header.width()
    edge = header.mapToScene(QPointF(header.width() - 1, header.height() / 2)).toPoint()
    QTest.mousePress(window, Qt.MouseButton.LeftButton, pos=edge)
    QTest.mouseMove(window, edge + QPointF(60, 0).toPoint(), delay=30)
    QTest.mouseRelease(window, Qt.MouseButton.LeftButton, pos=edge + QPointF(60, 0).toPoint())
    QTest.qWait(30)
    assert header.width() >= before + 45
