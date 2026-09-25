"""Keep desktop action targets and form selectors readable across windows."""

from dataclasses import replace

import pytest
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QFont, QFontMetricsF
from PySide6.QtQuick import QQuickItem, QQuickWindow
from PySide6.QtTest import QTest

from tests.ui.test_quick_window import backend, open_review, scene  # noqa: F401


def control(window, name):
    result = window.findChild(QQuickItem, name)
    assert result is not None, name
    return result


@pytest.mark.parametrize("point_size", [9, 14])
def test_main_and_review_selectors_share_action_height_and_text_clearance(scene, qtbot, point_size):  # noqa: F811
    window, interface = scene
    interface.selectFile("first", False)
    review = open_review(window, interface)
    font = QFont("Segoe UI", point_size)
    window.setProperty("font", font)
    review.setProperty("font", font)
    qtbot.wait(80)
    language = control(window, "languageCombo")
    scope = control(review, "reviewScopeCombo")
    button = control(review, "nextReviewFileButton")

    assert scope.height() >= 36
    assert scope.height() == pytest.approx(language.height(), abs=1)
    assert scope.height() == pytest.approx(button.height(), abs=1)

    for selector in (language, scope):
        text = selector.property("displayText")
        content = selector.property("contentItem")
        metrics = QFontMetricsF(selector.property("font"))
        assert content.width() >= metrics.horizontalAdvance(text), text
        assert selector.height() >= metrics.height() + 12


def test_review_action_targets_and_gaps_are_not_compressed(scene, qtbot):  # noqa: F811
    window, interface = scene
    interface.selectFile("first", False)
    interface.selectField("title")
    review = open_review(window, interface)
    qtbot.wait(80)
    names = ("keepExistingButton", "useProposedButton", "manualValueButton", "moreFieldActionsButton",
             "undoReviewButton", "fieldDetailsButton")
    actions = [control(review, name) for name in names]

    for action in actions:
        assert action.height() >= 36, action.objectName()
        assert action.property("leftPadding") >= 12
        assert action.property("rightPadding") >= 12

    for previous, following in zip(actions, actions[1:], strict=False):
        right = previous.mapToScene(QPointF(previous.width(), 0))
        left = following.mapToScene(QPointF())

        if abs(right.y() - left.y()) <= 1:
            assert left.x() - right.x() >= 8


def test_scope_selector_keeps_native_keyboard_behaviour_without_writing(scene, qtbot):  # noqa: F811
    window, interface = scene
    interface.selectFile("first", False)
    review = open_review(window, interface)
    scope = control(review, "reviewScopeCombo")
    original = interface.session_state
    scope.forceActiveFocus()
    QTest.keyClick(review, Qt.Key.Key_Space)
    qtbot.wait(50)
    QTest.keyClick(review, Qt.Key.Key_Down)
    QTest.keyClick(review, Qt.Key.Key_Return)
    qtbot.waitUntil(lambda: interface.reviewScope == "group")
    assert interface.session_state is original
    assert not interface.executor.pending
    assert not interface.includedFileIds


def test_small_position_editor_keeps_spin_arrows_clear_of_its_scrollbar(scene, qtbot):  # noqa: F811
    window, interface = scene
    interface.selectFile("first", False)
    interface.selectField("track")
    assert interface.beginEdit()
    editor = window.findChild(QQuickWindow, "manualEditWindow")
    assert editor is not None
    editor.setProperty("font", QFont("Segoe UI", 14))
    editor.resize(280, 160)
    qtbot.wait(80)
    number = control(editor, "manualEditNumber")
    scroll = number.parentItem()

    while scroll is not None and scroll.property("effectiveScrollBarWidth") is None:
        scroll = scroll.parentItem()

    assert scroll is not None
    gutter = scroll.property("effectiveScrollBarWidth")
    assert gutter > 0
    right = number.mapToScene(QPointF(number.width(), 0)).x()
    scrollbar_left = scroll.mapToScene(QPointF(scroll.width() - gutter, 0)).x()
    assert right <= scrollbar_left
    assert not interface.executor.pending


@pytest.mark.parametrize("point_size", [9, 14])
def test_search_error_preserves_readable_inputs_and_visible_footer_at_minimum_size(scene, qtbot, point_size):  # noqa: F811
    window, interface = scene
    lookup = interface.lookupUi
    assert lookup.beginSearch()
    search = window.findChild(QQuickWindow, "quickSearchWindow")
    assert search is not None
    search.setProperty("font", QFont("Segoe UI", point_size))
    search.resize(440, 230)
    search.requestActivate()
    assert QTest.qWaitForWindowActive(search, 2000)

    # A stale session is a real error path with a valid form draft. It must
    # leave a readable explanation and a reachable way to close the window.
    current = replace(interface.session_state, revision=interface.session_state.revision + 1)
    interface.set_state(current)
    assert not lookup.commitSearch("Edited album", "Artist", 2024)
    assert lookup.searchError
    qtbot.wait(80)

    accept = control(search, "quickSearchAccept")
    accept_bottom = accept.mapToScene(QPointF(accept.width(), accept.height()))
    assert accept_bottom.y() <= search.height() - 12
    assert accept_bottom.x() <= search.width() - 12

    for name in ("quickSearchAlbum", "quickSearchArtists", "quickSearchYear"):
        editor = control(search, name)
        metrics = QFontMetricsF(editor.property("font"))
        assert editor.height() >= max(36, metrics.height() + 14) - 1, name

    # Preserve wrappers until scene teardown: QML owns these visual items.
    search._error_items = search.findChildren(QQuickItem)
    error_panel = next(item for item in search._error_items
                       if "ErrorPanel" in item.metaObject().className())
    error_top = error_panel.mapToScene(QPointF())
    error_bottom = error_panel.mapToScene(QPointF(0, error_panel.height()))
    accept_top = accept.mapToScene(QPointF())
    assert error_panel.isVisible()
    assert error_panel.height() >= 60
    assert error_top.y() >= 12
    assert error_bottom.y() + 6 <= accept_top.y()
    assert interface.session_state is current
    assert not interface.executor.pending

    QTest.keyClick(search, Qt.Key.Key_Escape)
    qtbot.waitUntil(lambda: not lookup.searchVisible)
