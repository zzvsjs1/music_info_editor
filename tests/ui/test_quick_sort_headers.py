"""Exercise sorting through real library headers without changing review order."""

from PySide6.QtCore import QPointF, Qt
from PySide6.QtQuick import QQuickItem
from PySide6.QtTest import QTest

from tests.ui.test_quick_table_interaction import visual_items
from tests.ui.test_quick_window import backend, open_review, scene  # noqa: F401


def _header(window, table, column):
    # Keep wrappers alive for visually parented delegates until scene teardown.
    items = list(visual_items(table))
    window._sort_items = getattr(window, "_sort_items", []) + items
    return next(item for item in items if item.objectName() == f"sortHeader{column}")


def _click(window, item, button=Qt.MouseButton.LeftButton):
    point = item.mapToScene(QPointF(item.width() / 2, item.height() / 2)).toPoint()
    QTest.mouseClick(window, button, pos=point)
    QTest.qWait(60)


def _ids(model):
    roles = {bytes(name): role for role, name in model.roleNames().items()}
    return [model.index(row, 0).data(roles[b"stableId"]) for row in range(model.rowCount())]


def test_file_header_toggles_order_and_keyboard_keeps_selected_identity(scene, qtbot):  # noqa: F811
    window, interface = scene
    table = window.findChild(QQuickItem, "fileTable")
    assert table.property("sortable") is True
    interface.selectFile("first", False)
    interface.setIncluded("first", True)
    qtbot.wait(80)

    _click(window, _header(window, table, 2))
    assert _ids(interface.files) == ["first", "second"]
    _click(window, _header(window, table, 2))
    assert _ids(interface.files) == ["second", "first"]
    assert interface.currentFileRow == 1
    assert interface.selectedFileIds == ["first"]
    assert interface.includedFileIds == ["first"]
    assert table.property("sortDescending") is True

    # Sorting gives focus back to the table, so the next arrow moves through
    # the displayed neighbours without selecting an unrelated source index.
    QTest.keyClick(window, Qt.Key.Key_Up)
    assert interface.selectedFileIds == ["second"]
    assert interface.includedFileIds == ["first"]
    assert interface.session_state.revision == 0
    assert not interface.executor.pending


def test_album_headers_sort_but_metadata_headers_keep_the_field_order(scene, qtbot):  # noqa: F811
    window, interface = scene
    groups = window.findChild(QQuickItem, "groupTable")
    assert groups.property("sortable") is True
    _click(window, _header(window, groups, 0))
    assert interface.albumModel.sortColumnIndex == 0

    interface.selectFile("first", False)
    review = open_review(window, interface)
    fields = review.findChild(QQuickItem, "reviewTable")
    assert fields.property("sortable") is False
    before = _ids(interface.review)
    _click(review, _header(review, fields, 0))
    assert _ids(interface.review) == before
    assert interface.review.sortingEnabled is False


def test_sorted_header_can_resize_without_sorting_again(scene, qtbot):  # noqa: F811
    window, interface = scene
    table = window.findChild(QQuickItem, "fileTable")
    qtbot.wait(80)
    header = _header(window, table, 2)
    _click(window, header)
    width = header.width()
    start = header.mapToScene(QPointF(width - 1, header.height() / 2)).toPoint()
    end = start + QPointF(60, 0).toPoint()
    QTest.mouseMove(window, start, 50)
    QTest.mousePress(window, Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(window, start + QPointF(20, 0).toPoint(), 50)
    QTest.mouseMove(window, end, 80)
    QTest.mouseRelease(window, Qt.MouseButton.LeftButton, pos=end)
    qtbot.wait(80)
    assert header.width() > width + 30
    assert interface.files.sortColumnIndex == 2
    assert interface.files.sortDescending is False


def test_header_menu_restores_disc_track_order_without_losing_inclusion(scene, qtbot):  # noqa: F811
    window, interface = scene
    table = window.findChild(QQuickItem, "fileTable")
    interface.files.sortByColumn(2, True)
    interface.setIncluded("second", True)
    qtbot.wait(80)
    _click(window, _header(window, table, 2), Qt.MouseButton.RightButton)
    action = table.findChild(QQuickItem, "restoreDefaultSortAction")
    assert action is not None
    qtbot.waitUntil(action.isVisible)
    _click(window, action)
    assert interface.files.sortColumnIndex == -1
    assert interface.files.sortDescending is False
    assert interface.includedFileIds == ["second"]
