"""Keep the captured album and keyboard table commands clear during review."""

from dataclasses import replace
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtQuick import QQuickItem
from PySide6.QtTest import QTest

from metadata_polisher.session.state import GroupState
from tests.ui.test_quick_lookup_port import activate, candidate_state, lookup_scene
from tests.ui.test_quick_table_interaction import table_scene, variant, viewport, visual_items  # noqa: F401
from tests.unit.application.test_lookup_service import make_group, make_media_file


def test_candidate_window_names_its_captured_album_after_main_selection_changes(qtbot):
    state, service = candidate_state()
    first = state.groups[0]
    first_file = replace(first.group.files[0], path=Path("library/first-album/01.flac"))
    first = replace(first, group=replace(first.group, album_title="Local first album", files=(first_file,)))
    other_file = replace(make_media_file("02.flac", title="Other track"),
                         path=Path("library/second-album/02.flac"))
    other = GroupState(group=replace(make_group(other_file, album_title="Local second album"), group_id="second"))
    state = replace(state, root=Path("library"), groups=(first, other))

    with lookup_scene(qtbot, state=state) as (root, backend):
        lookup = backend.lookupUi
        lookup._service = service
        lookup.showCandidates()
        window = activate(root, "quickCandidateWindow")
        target = window.findChild(QQuickItem, "candidateLocalContext")
        notice = window.findChild(QQuickItem, "candidateSelectionNotice")
        assert target is not None, "The chooser must visibly identify its captured local album."
        assert notice is not None
        assert "Local first album" in target.property("text")
        assert str(first_file.path.parent) in target.property("text")
        assert target.property("readOnly") is True
        assert target.property("selectByMouse") is True
        assert not notice.isVisible()

        # A modeless chooser must retain the first album while the main window
        # moves elsewhere. Its warning explains which album Choose will affect.
        lookup.selectCandidate(lookup.candidates[0]["key"])
        backend.selectGroup("second")
        qtbot.waitUntil(notice.isVisible)
        assert "Local first album" in target.property("text")
        assert str(first_file.path.parent) in target.property("text")
        assert "Local second album" in notice.property("text")
        assert "still" in notice.property("text").lower()
        assert lookup.chooseCandidate()

        with qtbot.waitSignal(backend.bridge.completed):
            backend.executor.run_next()

        groups = backend.session_state.groups
        assert groups[0].selected_release is not None
        assert groups[1].selected_release is None


def test_secondary_table_horizontal_keys_keep_the_selected_row(table_scene, qtbot):  # noqa: F811
    window, table = table_scene("RowTable")
    table.setProperty("currentId", "file-3")
    table.forceActiveFocus()
    view = viewport(table)
    initial_id = table.property("currentId")
    initial_index = table.property("currentIndex")
    initial_y = view.property("contentY")

    QTest.keyClick(window, Qt.Key.Key_Right)
    qtbot.waitUntil(lambda: view.property("contentX") > 0, timeout=700)
    assert table.property("currentId") == initial_id
    assert table.property("currentIndex") == initial_index
    assert view.property("contentY") == pytest.approx(initial_y)

    QTest.keyClick(window, Qt.Key.Key_Left)
    qtbot.waitUntil(lambda: abs(view.property("contentX")) < 1, timeout=700)
    assert table.property("currentId") == initial_id


@pytest.mark.parametrize("kind", ["DataTable", "RowTable"])
@pytest.mark.parametrize("key, modifiers", [
    (Qt.Key.Key_Menu, Qt.KeyboardModifier.NoModifier),
    (Qt.Key.Key_F10, Qt.KeyboardModifier.ShiftModifier),
])
def test_keyboard_can_restore_a_hidden_table_column(table_scene, qtbot, kind, key, modifiers):  # noqa: F811
    window, table = table_scene(kind)
    table.setProperty("hiddenColumns", [0, 1, 2])
    table.forceActiveFocus()
    QTest.keyClick(window, key, modifiers)

    def file_choice():
        return next((item for item in visual_items(window.contentItem())
                     if "MenuItem" in item.metaObject().className()
                     and item.property("text") == "File" and item.isVisible()), None)

    qtbot.waitUntil(lambda: file_choice() is not None, timeout=700)

    # Opening the menu from a keyboard must place focus on an actionable entry;
    # restoring the column must not require a pointer click after the shortcut.
    QTest.keyClick(window, Qt.Key.Key_Space)
    qtbot.waitUntil(lambda: 0 not in variant(table.property("hiddenColumns")), timeout=700)
