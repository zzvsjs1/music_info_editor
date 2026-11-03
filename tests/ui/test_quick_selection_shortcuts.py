"""Album and field shortcuts change highlighting without authorising writes."""

from dataclasses import replace
from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPointF, Qt
from PySide6.QtQuick import QQuickItem, QQuickWindow
from PySide6.QtTest import QTest

from metadata_polisher.domain.media import UnsupportedMediaFile
from metadata_polisher.domain.metadata import MetadataField
from metadata_polisher.session.state import GroupSelection, SessionState, mark_groups_requires_rescan
from metadata_polisher.ui.quick.application import create_quick_engine
from metadata_polisher.ui.quick.backend import QuickBackend
from tests.ui.helpers import ControlledExecutor, make_group
from tests.ui.test_quick_table_interaction import visual_items


@pytest.fixture
def selection_scene(qapp, qtbot, tmp_path):
    # Deliberately use an order different from alphabetical sorting. Commands
    # must retain displayed stable identities, including their range anchor.
    groups = tuple(make_group(key, f"file-{key}", key.title()) for key in ("zeta", "alpha", "middle"))
    state = SessionState(root=Path("library"), groups=groups, selection=GroupSelection("alpha"))
    backend = QuickBackend(state=state, executor=ControlledExecutor(), settings_file=tmp_path / "settings.json")
    backend.selectGroup("alpha")
    backend.selectFile("file-alpha", False)
    backend.set_included_file_ids(frozenset({"file-zeta", "file-alpha"}))
    warnings = []
    engine = create_quick_engine(backend, warnings=warnings)
    window = engine.rootObjects()[0]
    window.requestActivate()
    assert QTest.qWaitForWindowActive(window, 2000)
    qtbot.wait(60)

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


def focus_table(window, name):
    table = window.findChild(QQuickItem, name)
    assert table is not None
    window.requestActivate()
    assert QTest.qWaitForWindowActive(window, 2000)
    table.forceActiveFocus()


def open_review(window, backend, qtbot):
    backend.openReview()
    review = window.findChild(QQuickWindow, "metadataReviewWindow")
    assert review is not None
    qtbot.waitUntil(review.isVisible)
    focus_table(review, "reviewTable")

    return review


def test_album_select_all_keeps_current_group_anchor_and_write_membership(selection_scene, qtbot):
    window, backend = selection_scene
    original = backend.session_state
    included = backend.includedFileIds
    focus_table(window, "groupTable")
    QTest.keyClick(window, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    qtbot.waitUntil(lambda: backend.selectedGroupIds == ["zeta", "alpha", "middle"])

    assert backend.groupId == "alpha"
    assert backend.currentGroupRow == 1
    assert backend.selectedFileIds == ["file-alpha"]
    assert backend.session_state is original
    assert backend.includedFileIds == included

    # Ctrl+A must not silently move the anchor to the first or last album.
    # Extending one row from the retained current album yields this exact range.
    QTest.keyClick(window, Qt.Key.Key_Down, Qt.KeyboardModifier.ShiftModifier)
    assert backend.selectedGroupIds == ["alpha", "middle"]
    assert backend.groupId == "middle"
    assert backend.includedFileIds == included


def test_album_clear_selection_keeps_current_files_and_disables_group_scope(selection_scene, qtbot):
    window, backend = selection_scene
    original = backend.session_state
    included = backend.includedFileIds
    focus_table(window, "groupTable")
    QTest.keyClick(window, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
    qtbot.waitUntil(lambda: backend.selectedGroupIds == [])

    assert backend.groupId == "alpha"
    assert backend.currentGroupRow == 1
    assert backend.selectedFileIds == ["file-alpha"]
    assert not backend.lookupUi.canFind
    assert backend.includedFileIds == included
    assert backend.session_state is original


@pytest.mark.parametrize("gesture", ["double-click", "enter"])
def test_activating_an_album_focuses_its_tracks(selection_scene, qtbot, gesture):
    window, backend = selection_scene
    included = backend.includedFileIds
    albums = window.findChild(QQuickItem, "groupTable")
    files = window.findChild(QQuickItem, "fileTable")
    if gesture == "double-click":
        cells = list(visual_items(albums))
        target = next(cell for cell in cells if cell.property("stableId") == "middle" and cell.property("column") == 0)
        point = target.mapToScene(QPointF(target.width() / 2, target.height() / 2)).toPoint()
        QTest.mouseDClick(window, Qt.MouseButton.LeftButton, pos=point)
    else:
        backend.selectGroup("middle")
        albums.forceActiveFocus()
        QTest.keyClick(window, Qt.Key.Key_Return)

    assert backend.groupId == "middle"
    assert files.hasActiveFocus()
    assert backend.selectedFileIds == ["file-middle"]
    assert not backend.reviewVisible
    assert backend.includedFileIds == included


@pytest.mark.parametrize("collection", ["stale", "unsupported"])
def test_library_only_offers_inclusion_for_checkable_rows(selection_scene, qtbot, collection):
    window, backend = selection_scene
    if collection == "unsupported":
        state = replace(backend.session_state, unsupported_files=(UnsupportedMediaFile(Path("library/extra.opus")),))
        backend.set_state(state)
        backend.selectGroup("@unsupported")
    else:
        backend.set_state(mark_groups_requires_rescan(backend.session_state, ("alpha",)))
        backend.set_included_file_ids(frozenset())

    table = window.findChild(QQuickItem, "fileTable")
    view = next(child for child in table.findChildren(QQuickItem)
                if child.metaObject().className() == "QQuickTableView")
    qtbot.waitUntil(lambda: view.property("rows") > 0)
    assert not any(item.inherits("QQuickCheckBox") and item.isVisible() for item in visual_items(table))
    assert not window.findChild(QQuickItem, "includeSelectedButton").isEnabled()
    before = backend.includedFileIds
    backend.includeSelection(True)
    assert backend.includedFileIds == before


def test_review_select_all_keeps_current_field_anchor_and_write_membership(selection_scene, qtbot):
    window, backend = selection_scene
    backend.selectField("artists")
    review = open_review(window, backend, qtbot)
    original = backend.session_state
    included = backend.includedFileIds
    QTest.keyClick(review, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    qtbot.waitUntil(lambda: backend.selectedFields == [field.value for field in MetadataField])

    assert backend.currentFieldRow == 1
    assert backend.selectedField == "artists"
    assert backend.selectedFileIds == ["file-alpha"]
    assert backend.includedFileIds == included
    assert backend.session_state is original

    QTest.keyClick(review, Qt.Key.Key_Down, Qt.KeyboardModifier.ShiftModifier)
    assert backend.selectedFields == ["artists", "album"]
    assert backend.currentFieldRow == 2
    assert backend.includedFileIds == included


def test_review_clear_selection_removes_edit_scope_without_changing_metadata(selection_scene, qtbot):
    window, backend = selection_scene
    backend.selectField("artists")
    backend.selectFieldExtended("album", True, False)
    review = open_review(window, backend, qtbot)
    original = backend.session_state
    included = backend.includedFileIds
    QTest.keyClick(review, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
    qtbot.waitUntil(lambda: backend.selectedFields == [])

    assert backend.currentFieldRow == -1
    assert not backend.canEdit
    assert backend.selectedFileIds == ["file-alpha"]
    assert backend.includedFileIds == included
    assert backend.session_state is original

    # An empty highlight has no actionable field. The established Down
    # behaviour starts again at Title, irrespective of the previous anchor.
    QTest.keyClick(review, Qt.Key.Key_Down)
    assert backend.selectedFields == ["title"]
    assert backend.currentFieldRow == 0
    assert backend.includedFileIds == included
