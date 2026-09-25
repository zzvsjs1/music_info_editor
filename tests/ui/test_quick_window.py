"""Load the actual QML scene to catch broken imports, bindings and packaging paths."""

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPointF, Qt
from PySide6.QtQuick import QQuickItem, QQuickWindow
from PySide6.QtTest import QTest

from tests.ui.test_quick_backend import backend as backend_fixture

backend = pytest.fixture(backend_fixture.__wrapped__)


@pytest.fixture
def scene(backend, qtbot):  # noqa: F811
    from metadata_polisher.ui.quick.application import create_quick_engine

    warnings = []
    engine = create_quick_engine(backend, warnings=warnings)
    window = engine.rootObjects()[0]
    try:
        window.show()
        window.requestActivate()
        assert QTest.qWaitForWindowActive(window, 2000)
        yield window, backend
    finally:
        for child in window.findChildren(QQuickWindow):
            child.hide()

        window.hide()
        engine.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        assert not warnings, warnings


def click_item(window, name, offset=None):
    # QML lays out popup children in its next frame after text/focus changes.
    # Let that frame finish before converting an item's position for a click.
    QTest.qWait(50)
    item = window.findChild(QQuickItem, name)
    assert item is not None, name
    point = offset if offset is not None else QPointF(item.width() / 2, item.height() / 2)
    QTest.mouseClick(item.window() or window, Qt.MouseButton.LeftButton, pos=item.mapToScene(point).toPoint())


def click_table_cell(window, name, stable_id, column, *, inclusion=False):
    """Hit the rendered delegate, independent of native checkbox/font metrics."""
    QTest.qWait(50)
    table = window.findChild(QQuickItem, name)
    assert table is not None

    def descendants(item):
        for child in item.childItems():
            yield child
            yield from descendants(child)

    # Keep the wrappers alive until scene teardown. Some delegates have only a
    # visual parent; releasing newly created Python wrappers during traversal
    # can destroy those QML-owned items in PySide.
    items = list(descendants(table))
    window._table_items = getattr(window, "_table_items", []) + items
    cell = next(item for item in items
                if item.property("stableId") == stable_id and item.property("column") == column)
    target = cell

    if inclusion:
        # Qt's Windows checkbox is shorter than Fusion's control. A coordinate
        # near the cell's bottom can select its row without hitting the checkbox.
        target = next(item for item in items
                      if item.parentItem() == cell
                      if "CheckBox" in item.metaObject().className() and item.isVisible())

    point = target.mapToScene(QPointF(target.width() / 2, target.height() / 2)).toPoint()
    QTest.mouseClick(window, Qt.MouseButton.LeftButton, pos=point)


def open_review(window, interface):
    interface.openReview()
    review = window.findChild(QQuickWindow, "metadataReviewWindow")
    assert review is not None
    review.requestActivate()
    assert QTest.qWaitForWindowActive(review, 2000)
    return review


def test_qml_window_loads_and_keyboard_edits_the_selected_file(scene, qtbot):
    window, interface = scene
    interface.selectFile("first", False)
    review = open_review(window, interface)
    click_item(review, "reviewTable", QPointF(50, 48))
    QTest.keyClick(review, Qt.Key.Key_F2)
    qtbot.waitUntil(lambda: interface.editing)
    editor = window.findChild(QQuickWindow, "manualEditWindow")
    editor.requestActivate()
    assert QTest.qWaitForWindowActive(editor, 2000)
    qtbot.waitUntil(lambda: editor.activeFocusItem() is not None)
    QTest.keyClick(editor, Qt.Key.Key_Escape)
    qtbot.waitUntil(lambda: not interface.editing)
    window.resize(960, 650)
    qtbot.wait(100)
    assert window.width() == 960


def test_real_delegate_clicks_keep_highlighting_separate_from_inclusion(scene, qtbot):
    window, interface = scene
    click_table_cell(window, "fileTable", "first", 2)
    assert interface.selectedFileIds == ["first"]
    assert interface.includedFileIds == []
    click_table_cell(window, "fileTable", "first", 0, inclusion=True)
    assert interface.includedFileIds == ["first"]
    click_table_cell(window, "fileTable", "second", 2)
    assert interface.selectedFileIds == ["second"]
    assert interface.includedFileIds == ["first"]

    review = open_review(window, interface)
    click_item(review, "reviewTable", QPointF(50, 48))
    QTest.keyClick(review, Qt.Key.Key_F2)
    qtbot.waitUntil(lambda: interface.editing)
    editor = window.findChild(QQuickWindow, "manualEditWindow")
    editor.requestActivate()
    assert QTest.qWaitForWindowActive(editor, 2000)
    qtbot.waitUntil(lambda: editor.activeFocusItem().objectName() == "manualEditValue")
    QTest.keyClick(editor, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    QTest.keyClick(editor, Qt.Key.Key_Z)
    assert editor.findChild(QQuickItem, "manualEditValue").property("text") == "z"
    click_item(editor, "saveEditButton")
    assert not interface.editError, interface.editError
    qtbot.waitUntil(lambda: not interface.editing)
    assert interface.review.index(0, 4).data() == "z"
    assert interface.selectedFileIds == ["second"]
    assert interface.includedFileIds == ["first"]


def test_file_keyboard_navigation_and_undo_route_to_stable_targets(scene, qtbot):
    window, interface = scene
    interface.selectField("title")
    click_table_cell(window, "fileTable", "first", 2)
    QTest.keyClick(window, Qt.Key.Key_Down)
    assert interface.selectedFileIds == ["second"]
    QTest.keyClick(window, Qt.Key.Key_Space)
    assert interface.includedFileIds == ["second"]
    QTest.keyClick(window, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    assert interface.selectedFileIds == ["first", "second"]
    interface.reviewAction("clear")
    assert interface.review.index(0, 4).data() == "Empty"
    review = open_review(window, interface)
    QTest.keyClick(review, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    qtbot.waitUntil(lambda: interface.review.index(0, 4).data() == "Mixed values")
    assert interface.includedFileIds == ["second"]


def test_long_values_stay_bounded_and_cancel_keeps_close_pending(scene, qtbot):
    window, interface = scene
    window.resize(960, 650)
    interface.selectFile("first", False)
    interface.selectField("title")
    review = open_review(window, interface)
    assert interface.beginEdit()
    assert interface.commitEdit("<b>Literal metadata</b> " + "Long text " * 1000)
    qtbot.wait(100)
    click_item(review, "fieldDetailsButton")
    details = review.findChild(QQuickItem, "fieldDetails")
    assert "<b>Literal metadata</b>" in details.property("text")
    assert window.width() == 960 and window.height() == 650
    assert review.findChild(QQuickItem, "fieldDetailsScroll").height() <= 100

    window.requestActivate()
    assert QTest.qWaitForWindowActive(window, 2000)
    window.close()
    dialog = window.findChild(QQuickWindow, "discardDialog")
    assert dialog is not None and dialog.isVisible()
    dialog.requestActivate()
    assert QTest.qWaitForWindowActive(dialog, 2000)
    qtbot.waitUntil(lambda: dialog.activeFocusItem() is not None
                   and dialog.activeFocusItem().objectName() == "cancelDiscardButton")
    click_item(dialog, "cancelDiscardButton")
    qtbot.waitUntil(lambda: not dialog.isVisible())
    assert window.isVisible()
    assert interface.hasPendingWork


def test_close_during_scan_requests_cancellation_and_retains_terminal_details(scene, qtbot, tmp_path):
    window, interface = scene
    assert interface.scan(str(tmp_path), False)
    window.close()
    assert window.isVisible()
    assert interface.busy
    assert interface.executor.pending[0][0].is_cancel_requested()
    interface.executor.run_next()
    qtbot.waitUntil(lambda: not interface.busy)
    assert window.isVisible()
    progress = window.findChild(QQuickWindow, "operationProgressWindow")
    assert progress is not None and progress.isVisible()


def test_empty_review_hint_is_inside_the_visible_table(scene, qtbot):
    window, interface = scene
    review = open_review(window, interface)
    interface.clearSelection()
    qtbot.wait(80)
    table = review.findChild(QQuickItem, "reviewTable")
    hint = next(item for item in table.findChildren(QQuickItem)
                if item.property("text") == "Select one or more files to inspect their metadata.")
    top_left = hint.mapToItem(table, QPointF(0, 0))
    assert top_left.x() >= 0
    assert top_left.y() >= 30
    assert top_left.x() + hint.width() <= table.width()
    assert top_left.y() + hint.height() <= table.height()
