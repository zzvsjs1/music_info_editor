"""Keep the loaded library and useful review content visible in compact windows."""

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QMetaObject, QPointF, Qt
from PySide6.QtGui import QFont
from PySide6.QtQuick import QQuickItem, QQuickWindow
from PySide6.QtTest import QTest

from metadata_polisher.session.state import OperationKind
from metadata_polisher.ui.quick.application import create_quick_engine
from metadata_polisher.ui.quick.backend import QuickBackend
from tests.ui.helpers import ControlledExecutor, changed_local_session
from tests.ui.test_quick_table_interaction import visual_items


@pytest.fixture
def audit_scene(qapp, qtbot):
    original_font = QFont(qapp.font())
    qapp.setFont(QFont("Segoe UI", 14))
    backend = QuickBackend(state=changed_local_session(), executor=ControlledExecutor())
    warnings = []
    engine = create_quick_engine(backend, warnings=warnings)
    window = engine.rootObjects()[0]
    window.resize(840, 580)
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
        qapp.setFont(original_font)
        assert not warnings, warnings


def item(window, name):
    control = window.findChild(QQuickItem, name)
    assert control is not None, name
    return control


@pytest.mark.parametrize("cancel", ["button", "close", "escape"])
def test_cancel_folder_replacement_restores_loaded_path_and_pending_review(audit_scene, qtbot, cancel):
    window, backend = audit_scene
    original = backend.session_state
    path = item(window, "folderPath")
    path.setProperty("text", "another-library")
    assert QMetaObject.invokeMethod(window, "requestScan")
    dialog = window.findChild(QQuickWindow, "discardDialog")
    assert dialog is not None
    qtbot.waitUntil(dialog.isVisible)

    if cancel == "button":
        assert QMetaObject.invokeMethod(item(dialog, "cancelDiscardButton"), "clicked")
    elif cancel == "close":
        dialog.close()
    else:
        dialog.requestActivate()
        assert QTest.qWaitForWindowActive(dialog, 2000)
        qtbot.wait(60)
        item(dialog, "cancelDiscardButton").forceActiveFocus()
        QTest.keyClick(dialog, Qt.Key.Key_Escape)

    qtbot.waitUntil(lambda: not dialog.isVisible())
    assert path.property("text") == backend.rootPath
    assert backend.session_state is original
    assert backend.canUndo
    assert not backend.executor.pending


def test_unscanned_path_is_explicitly_distinguished_from_loaded_library(audit_scene, qtbot):
    window, backend = audit_scene
    item(window, "folderPath").setProperty("text", "another-library")
    context = item(window, "loadedLibraryLabel")
    qtbot.waitUntil(context.isVisible)

    assert backend.rootPath in context.property("text")
    assert "Loaded library" in context.property("text")
    assert "another-library" not in context.property("text")


def test_compact_main_retains_four_readable_rows_and_access_to_secondary_actions(audit_scene, qtbot):
    window, backend = audit_scene
    table = item(window, "fileTable")
    viewport = next(child for child in table.findChildren(QQuickItem)
                    if child.inherits("QQuickTableView") and child.property("rows") == backend.files.rowCount())

    # Count only the usable body: the header and horizontal scrollbar must not
    # be mistaken for additional readable rows in a short large-font window.
    assert viewport.height() >= 4 * table.property("rowHeight")
    assert item(window, "compactFileActionsButton").isVisible()
    assert QMetaObject.invokeMethod(item(window, "groupToolsButton"), "clicked")
    qtbot.wait(40)
    controls = list(visual_items(window.contentItem()))
    window._audit_items = controls
    settings = next(control for control in controls
                    if "MenuItem" in control.metaObject().className()
                    and control.property("text") == "Settings" and control.isVisible())
    assert QMetaObject.invokeMethod(settings, "triggered")
    qtbot.waitUntil(lambda: backend.settingsUi.opened)
    backend.settingsUi.reject()
    assert not backend.executor.pending


def test_narrow_review_keeps_field_final_and_status_in_view_without_retargeting(audit_scene, qtbot):
    window, backend = audit_scene
    file_id = backend.session_state.groups[0].group.files[0].file_id
    backend.selectFile(file_id, False)
    backend.selectField("title")
    backend.openReview()
    review = window.findChild(QQuickWindow, "metadataReviewWindow")
    assert review is not None
    review.resize(840, 580)
    qtbot.wait(100)
    table = item(review, "reviewTable")
    controls = list(visual_items(table))
    review._audit_items = controls
    cells = {control.property("column"): control for control in controls
             if control.property("stableId") == "title" and control.width() > 0 and control.isVisible()}

    for column in (0, 4, 1):
        assert column in cells, f"Column {column} must be visible before horizontal scrolling."
        origin = cells[column].mapToItem(table, QPointF())
        assert origin.x() >= 0
        assert origin.x() + cells[column].width() <= table.width()

    # Reordering is presentation only: activating the visible Final column
    # must still edit Title on the selected file, with no inclusion side effect.
    final = cells[4]
    point = final.mapToScene(QPointF(final.width() / 2, final.height() / 2)).toPoint()
    review.requestActivate()
    assert QTest.qWaitForWindowActive(review, 2000)
    QTest.mouseDClick(review, Qt.MouseButton.LeftButton, pos=point)
    qtbot.waitUntil(lambda: backend.editing)
    assert backend.selectedFields == ["title"]
    assert backend.selectedFileIds == [file_id]
    assert backend.includedFileIds == []
    backend.cancelEdit()


def test_saved_wide_value_columns_also_prioritise_final_and_status(audit_scene, qtbot):
    window, backend = audit_scene
    backend.selectFile(backend.session_state.groups[0].group.files[0].file_id, False)
    backend.openReview()
    review = window.findChild(QQuickWindow, "metadataReviewWindow")
    table = item(review, "reviewTable")
    table.setProperty("columnWidths", [100, 100, 600, 600, 240, 180])
    review.resize(1400, 760)
    qtbot.wait(80)
    controls = list(visual_items(table))
    review._audit_items = controls
    cells = {control.property("column"): control for control in controls
             if control.property("stableId") == "title" and control.width() > 0 and control.isVisible()}

    assert 4 in cells
    assert cells[4].mapToItem(table, QPointF()).x() < cells[2].mapToItem(table, QPointF()).x()


def test_compact_window_does_not_open_a_submenu_without_user_input(audit_scene):
    window, _backend = audit_scene
    menus = [obj for obj in window.findChildren(QQuickItem) if obj.property("title") == "Language"]

    assert all(not obj.isVisible() for obj in menus)


@pytest.mark.parametrize("height", [580, 840])
def test_diagnostics_remains_available_during_an_operation_in_both_layouts(audit_scene, qtbot, height):
    window, backend = audit_scene
    window.resize(840, height)
    assert backend.submit_operation(
        "diagnostics-access", OperationKind.PROVIDER_TEST, (), lambda *_: None, lambda state, _result: state,
    )
    qtbot.wait(60)
    assert backend.busy

    if window.property("compactHeight"):
        assert QMetaObject.invokeMethod(item(window, "groupToolsButton"), "clicked")
        qtbot.wait(40)
        controls = list(visual_items(window.contentItem()))
        window._audit_items = controls
        command = next(control for control in controls
                       if "MenuItem" in control.metaObject().className()
                       and control.property("text") == "Diagnostics…" and control.isVisible())
        signal = "triggered"
    else:
        command = item(window, "diagnosticsButton")
        signal = "clicked"

    # Diagnostics is a read-only view of the running operation. Moving its
    # entry into the compact menu must not inherit editing-command blockers.
    assert command.isEnabled()
    assert QMetaObject.invokeMethod(command, signal)
    diagnostics = window.findChild(QQuickWindow, "diagnosticsWindow")
    assert diagnostics is not None
    qtbot.waitUntil(diagnostics.isVisible)
    diagnostics.close()

