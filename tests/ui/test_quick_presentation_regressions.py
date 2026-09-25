"""Exercise readable layouts and window preferences through the actual scene."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QMetaObject, QPoint, QPointF, QRect, Qt
from PySide6.QtGui import QFont, QFontMetricsF, QGuiApplication, QWindow
from PySide6.QtQuick import QQuickItem, QQuickWindow
from PySide6.QtTest import QTest

from metadata_polisher.infrastructure.settings import AppSettings, UiSettings, load_settings, save_settings
from metadata_polisher.ui.quick.application import create_quick_engine
from metadata_polisher.ui.quick.backend import QuickBackend
from tests.ui.helpers import ControlledExecutor, changed_local_session


@pytest.fixture
def presentation_scene(qapp, qtbot, tmp_path, request):
    original_font = QFont(qapp.font())
    backend = None
    engine = None
    window = None
    warnings = []

    try:
        if getattr(request, "param", None) is not None:
            qapp.setFont(QFont("Segoe UI", request.param))

        settings = AppSettings(external_tools={"Player": "player.exe"})
        backend = QuickBackend(
            settings=settings, settings_file=tmp_path / "settings.json", executor=ControlledExecutor(),
        )
        engine = create_quick_engine(backend, warnings=warnings)
        window = engine.rootObjects()[0]
        qtbot.wait(60)
        yield window, backend
    finally:
        if window is not None:
            for child in window.findChildren(QQuickWindow):
                child.hide()

            window.hide()

        if backend is not None:
            backend.shutdown()

        if engine is not None:
            engine.deleteLater()

        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        qapp.setFont(original_font)
        assert not warnings, warnings


def _item(window, name):
    result = window.findChild(QQuickItem, name)
    assert result is not None, name
    return result


def _click(window, item):
    point = item.mapToScene(QPointF(item.width() / 2, item.height() / 2)).toPoint()
    QTest.mouseClick(window, Qt.MouseButton.LeftButton, pos=point)


def _inside(window, item):
    top_left = item.mapToScene(QPointF())
    assert top_left.x() >= -1
    assert top_left.x() + item.width() <= window.width() + 1
    assert top_left.y() >= -1
    assert top_left.y() + item.height() <= window.height() + 1


def test_album_rows_and_headers_fit_a_larger_accessibility_font(presentation_scene, qtbot):
    window, _backend = presentation_scene
    window.setProperty("font", QFont("Segoe UI", 14))
    qtbot.wait(60)
    table = _item(window, "groupTable")
    metrics = QFontMetricsF(table.property("font"))

    assert table.property("rowHeight") >= metrics.height() + 2
    assert table.property("headerHeight") >= metrics.height() + 4


def test_main_actions_remain_inside_a_narrow_window_with_larger_text(presentation_scene, qtbot):
    window, _backend = presentation_scene
    window.setProperty("font", QFont("Segoe UI", 14))
    window.resize(840, 900)
    qtbot.wait(80)

    for name in (
        "findSelectedButton", "findAllIncompleteButton", "groupToolsButton", "settingsButton",
        "diagnosticsButton", "selectAllFilesButton", "clearFileSelectionButton", "renameFilesButton",
        "openReviewButton", "includeSelectedButton", "excludeSelectedButton", "applySelectedButton",
    ):
        _inside(window, _item(window, name))


def test_shared_buttons_have_readable_padding_and_batch_count_does_not_elide(presentation_scene, qtbot):
    window, backend = presentation_scene
    state = changed_local_session()
    backend.set_state(state)
    file_id = state.groups[0].group.files[0].file_id
    backend.selectFile(file_id, False)
    backend.setIncluded(file_id, True)
    window.setProperty("font", QFont("Segoe UI", 14))
    window.resize(840, 900)
    qtbot.wait(80)

    for name in ("findSelectedButton", "settingsButton", "includeSelectedButton", "applySelectedButton"):
        button = _item(window, name)
        assert button.property("leftPadding") >= 10, name
        assert button.property("rightPadding") >= 10, name
        assert button.property("topPadding") >= 4, name
        assert button.property("bottomPadding") >= 4, name
        _inside(window, button)

    scope = _item(window, "selectionScopeLabel")
    assert scope.width() >= scope.implicitWidth() - 1


def test_main_minimum_size_retains_usable_table_and_separate_footer(presentation_scene, qtbot):
    window, backend = presentation_scene
    backend.set_state(changed_local_session())
    backend.set_status("A complete diagnostic remains available. " * 80)
    window.setProperty("font", QFont("Segoe UI", 14))
    window.resize(840, 580)
    qtbot.wait(80)
    table = _item(window, "fileTable")
    workspace = _item(window, "mainSplitter")
    scope = _item(window, "selectionScopeLabel")

    assert (window.width(), window.height()) == (840, 580)
    assert table.height() >= table.property("headerHeight") + table.property("rowHeight") + 2
    assert table.mapToScene(QPointF(0, table.height())).y() <= scope.mapToScene(QPointF()).y()

    # Controls must stay inside their own pane, not merely somewhere within the
    # window where they could cover the progress strip or the final write action.
    for name in ("selectAllFilesButton", "clearFileSelectionButton", "renameFilesButton", "openReviewButton",
                 "includeSelectedButton", "excludeSelectedButton"):
        button = _item(window, name)
        position = button.mapToItem(workspace, QPointF())
        assert position.y() >= 0, name
        assert position.y() + button.height() <= workspace.height() + 1, name
        _inside(window, button)

    for name in ("settingsButton", "diagnosticsButton", "operationStageButton", "cancelButton",
                 "applyResultsButton", "applySelectedButton"):
        _inside(window, _item(window, name))

    progress = _item(window, "operationStageButton")
    assert progress.mapToScene(QPointF()).y() >= workspace.mapToScene(QPointF(0, workspace.height())).y()


@pytest.mark.parametrize("presentation_scene", [14], indirect=True)
def test_global_large_font_keeps_a_full_file_row_above_the_scrollbar(presentation_scene, qapp, qtbot):
    window, backend = presentation_scene
    state = changed_local_session()
    backend.set_state(state)
    file_id = state.groups[0].group.files[0].file_id
    backend.selectFile(file_id, False)
    backend.setIncluded(file_id, True)
    backend.set_status("A complete diagnostic remains available. " * 80)
    window.resize(840, 580)
    qtbot.wait(80)
    table = _item(window, "fileTable")
    workspace = _item(window, "mainSplitter")
    viewport = next(child for child in table.findChildren(QQuickItem)
                    if child.inherits("QQuickTableView") and child.property("rows") == backend.files.rowCount())
    bars = [child for child in table.findChildren(QQuickItem)
            if child.inherits("QQuickScrollBar") and child.property("orientation") == Qt.Orientation.Horizontal
            and child.isVisible()]

    assert qapp.font().pointSize() == 14
    assert bars, "The populated file table must actually overflow horizontally."
    # Any part covered by the scrollbar cannot count towards a readable row.
    # Measuring their actual overlap also accepts an external reserved gutter.
    viewport_top = viewport.mapToScene(QPointF()).y()
    viewport_bottom = viewport_top + viewport.height()
    scrollbar_top = bars[0].mapToScene(QPointF()).y()
    scrollbar_bottom = scrollbar_top + bars[0].height()
    overlap = max(0, min(viewport_bottom, scrollbar_bottom) - max(viewport_top, scrollbar_top))
    assert viewport.height() - overlap >= table.property("rowHeight")

    for name in ("includeSelectedButton", "excludeSelectedButton"):
        button = _item(window, name)
        position = button.mapToItem(workspace, QPointF())
        assert position.y() + button.height() <= workspace.height() + 1, name

    progress = _item(window, "operationStageButton")
    assert progress.mapToScene(QPointF()).y() >= workspace.mapToScene(QPointF(0, workspace.height())).y()

    for name in ("cancelButton", "applyResultsButton", "applySelectedButton"):
        _inside(window, _item(window, name))


def test_native_review_scrollbar_has_no_white_edge(presentation_scene, qtbot):
    if QGuiApplication.platformName() != "windows":
        pytest.skip("The reported scrollbar seam requires native Windows painting.")

    window, backend = presentation_scene
    state = changed_local_session()
    backend.set_state(state)
    backend.selectFile(state.groups[0].group.files[0].file_id, False)
    backend.openReview()
    review = window.findChild(QQuickWindow, "metadataReviewWindow")
    assert review is not None and review.isVisible()
    qtbot.wait(100)

    scroll = _item(review, "reviewScrollArea")
    bars = [item for item in scroll.findChildren(QQuickItem)
            if item.inherits("QQuickScrollBar")
            and item.property("orientation") == Qt.Orientation.Vertical
            and item.parentItem() is scroll]
    assert len(bars) == 1
    bar = bars[0]
    assert bar.property("size") < 1

    image = review.grabWindow()
    scale = image.devicePixelRatio()
    origin = bar.mapToScene(QPointF())
    y = round((origin.y() + bar.height() / 2) * scale)
    edge = round(origin.x() * scale)
    track = round((origin.x() + bar.width() * 0.2) * scale)

    # The white line in the report belongs to this outer ScrollView. Sample
    # outside the centred thumb so a legitimate thumb edge is not a failure.
    assert image.pixelColor(edge, y) == image.pixelColor(track, y)

    start_position = bar.property("position")
    thumb = bar.mapToScene(QPointF(bar.width() / 2, bar.height() * bar.property("size") / 2)).toPoint()
    QTest.mouseMove(review, thumb)
    QTest.mousePress(review, Qt.MouseButton.LeftButton, pos=thumb)
    QTest.mouseMove(review, thumb + QPoint(0, 30))
    QTest.mouseRelease(review, Qt.MouseButton.LeftButton, pos=thumb + QPoint(0, 30))
    qtbot.wait(80)
    assert bar.property("position") > start_position


def test_long_operation_stage_does_not_push_the_cancel_action_outside_the_window(presentation_scene, qtbot):
    window, backend = presentation_scene
    window.resize(840, 700)
    backend._progress_stage = "Long operation detail " * 200
    backend.changed.emit()
    qtbot.wait(60)

    _inside(window, _item(window, "cancelButton"))


def test_discard_confirmation_shows_copyable_whole_library_counts(presentation_scene, qtbot):
    window, backend = presentation_scene
    backend.set_state(changed_local_session())
    window.setProperty("font", QFont("Segoe UI", 14))
    window.resize(840, 580)
    QMetaObject.invokeMethod(window, "requestScan", Qt.ConnectionType.DirectConnection)
    qtbot.wait(60)
    dialog = window.findChild(QQuickWindow, "discardDialog")
    assert dialog is not None and dialog.isVisible()
    assert dialog.flags() & Qt.WindowType.Dialog
    assert dialog.transientParent() is window
    details = _item(dialog, "discardWorkDescription")
    scroll = _item(dialog, "discardWorkScroll")
    confirm = _item(dialog, "confirmDiscardButton")

    assert details.property("text") == backend.pendingWorkDescription
    assert details.property("readOnly") and details.property("selectByMouse")
    assert 0 < scroll.height() <= 96
    assert confirm.mapToScene(QPointF()).y() >= scroll.mapToScene(QPointF(0, scroll.height())).y()
    _inside(dialog, confirm)
    assert backend.executor.pending == []

    # Dismissing the native warning is a cancellation: the pending session and
    # its review decisions must remain intact, with no scan submitted.
    before = backend.session_state
    dialog.requestActivate()
    qtbot.waitUntil(dialog.isActive)
    QTest.keyClick(dialog, Qt.Key.Key_Escape)
    qtbot.waitUntil(lambda: not dialog.isVisible())
    assert backend.session_state is before
    assert backend.executor.pending == []


def test_review_action_labels_fit_and_footer_stays_inside_with_larger_text(presentation_scene, qtbot):
    window, backend = presentation_scene
    backend.openReview()
    review = window.findChild(QQuickWindow, "metadataReviewWindow")
    review.setProperty("font", QFont("Segoe UI", 14))
    review.resize(600, 760)
    qtbot.wait(80)

    for name in ("keepExistingButton", "useProposedButton", "manualValueButton", "clearValueButton",
                 "keepFilenameButton", "applyRenameButton", "renamePreviewsButton"):
        button = _item(review, name)
        assert button.width() >= button.implicitWidth() - 1, name

    _inside(review, _item(review, "includeReviewScopeButton"))
    _inside(review, _item(review, "reviewApplyButton"))


def test_review_minimum_size_keeps_scrolling_body_and_write_actions_visible(presentation_scene, qtbot):
    window, backend = presentation_scene
    state = changed_local_session()
    backend.set_state(state)
    backend.selectFile(state.groups[0].group.files[0].file_id, False)
    backend.set_status("A complete diagnostic remains available. " * 80)
    backend.openReview()
    review = window.findChild(QQuickWindow, "metadataReviewWindow")
    review.setProperty("font", QFont("Segoe UI", 14))
    review.resize(600, 360)
    qtbot.wait(80)
    body = _item(review, "reviewScrollArea")
    table = _item(review, "reviewTable")
    footer = _item(review, "reviewFooter")

    assert (review.width(), review.height()) == (600, 360)
    assert body.height() >= table.property("headerHeight") + table.property("rowHeight")
    assert body.mapToScene(QPointF(0, body.height())).y() <= footer.mapToScene(QPointF()).y()

    for name in ("reviewScopeCombo", "includeReviewScopeButton", "reviewApplyButton"):
        _inside(review, _item(review, name))


def test_settings_tabs_have_visible_overflow_controls_and_keyboard_cycling(presentation_scene, qtbot):
    window, backend = presentation_scene
    backend.settingsUi.open()
    settings = window.findChild(QQuickWindow, "settingsWindow")
    settings.resize(600, 460)
    settings.requestActivate()
    qtbot.wait(80)
    next_button = _item(settings, "nextSettingsTab")

    assert next_button.isVisible() and next_button.isEnabled()
    _click(settings, next_button)
    assert _item(settings, "providersTab").property("checked")

    # Keyboard tab cycling also works from an input, without requiring the user
    # to find and focus a partly visible tab in a horizontally scrolling strip.
    _click(settings, _item(settings, "previousSettingsTab"))
    _item(settings, "settingsTemplate").forceActiveFocus()
    QTest.keyClick(settings, Qt.Key.Key_Tab, Qt.KeyboardModifier.ControlModifier)
    assert _item(settings, "providersTab").property("checked")

    for _ in range(3):
        _click(settings, next_button)

    final_tab = _item(settings, "externalToolsTab")
    tab_strip = _item(settings, "settingsTabs")
    assert final_tab.property("checked")

    # Qt polishes a ListView after the input event. Wait for the selected tab to
    # be wholly inside its clipped viewport, including when reached rapidly.
    qtbot.waitUntil(lambda: final_tab.mapToItem(tab_strip, QPointF()).x() >= 0
                    and final_tab.mapToItem(tab_strip, QPointF()).x() + final_tab.width() <= tab_strip.width() + 1)
    _inside(settings, final_tab)


def test_all_settings_pages_start_clear_of_the_tab_border(presentation_scene, qtbot):
    window, backend = presentation_scene
    backend.settingsUi.open()
    settings = window.findChild(QQuickWindow, "settingsWindow")
    settings.resize(950, 650)
    qtbot.wait(60)
    tabs = _item(settings, "settingsTabs")
    pages = _item(settings, "settingsPages")

    # The shared page container owns the inset, so every tab keeps the same
    # first-row separation even though their inner layouts differ.
    for index in range(5):
        tabs.setProperty("currentIndex", index)
        qtbot.wait(20)
        gap = pages.mapToScene(QPointF()).y() - tabs.mapToScene(QPointF(0, tabs.height())).y()
        assert gap >= 10, index


def test_selected_external_tool_uses_the_highlighted_text_colour(presentation_scene, qapp, qtbot):
    window, backend = presentation_scene
    backend.settingsUi.open()
    settings = window.findChild(QQuickWindow, "settingsWindow")
    qtbot.wait(60)
    _click(settings, _item(settings, "externalToolsTab"))
    table = _item(settings, "settingsToolsTable")
    table.setProperty("selectedName", "Player")
    qtbot.wait(60)
    delegate = table.property("currentItem")
    assert delegate is not None
    labels = [child for child in delegate.findChildren(QQuickItem) if child.property("text") == "Player"]

    assert labels
    assert labels[0].property("color") == qapp.palette().highlightedText().color()


def test_first_maximised_launch_retains_normal_geometry(qapp, qtbot, tmp_path):
    path = tmp_path / "settings.json"
    settings = AppSettings()
    save_settings(path, settings)
    backend = QuickBackend(settings=settings, settings_file=path, executor=ControlledExecutor())
    window = QWindow()
    window.setGeometry(70, 80, 930, 640)
    backend.layoutUi.watchWindow(window, "MainWindow")

    try:
        window.show()
        window.resize(1030, 710)
        qtbot.wait(40)
        normal = window.geometry()
        window.showMaximized()
        qtbot.wait(40)
        backend.layoutUi.captureWindow(window, "MainWindow")
        assert backend.layoutUi.persist()
        retained = load_settings(path).settings.ui

        assert retained.maximised
        assert retained.geometry == (normal.x(), normal.y(), normal.width(), normal.height())
    finally:
        window.hide()
        backend.shutdown()


def test_maximised_preference_is_restored_without_saved_geometry(qapp, tmp_path):
    settings = replace(AppSettings(), ui=UiSettings(maximised=True))
    backend = QuickBackend(settings=settings, settings_file=tmp_path / "settings.json", executor=ControlledExecutor())
    window = QWindow()

    try:
        backend.layoutUi.restoreWindow(window, "MainWindow")
        assert window.visibility() == QWindow.Visibility.Maximized
    finally:
        window.hide()
        backend.shutdown()


def test_saved_geometry_uses_its_connected_monitor(qapp, tmp_path, monkeypatch):
    # Two synthetic monitor rectangles make the placement decision testable on
    # a single-monitor runner. Capture the requested native geometry rather than
    # asking the real desktop to move a window onto a monitor it does not have.
    first = SimpleNamespace(availableGeometry=lambda: QRect(0, 0, 1920, 1040))
    second = SimpleNamespace(availableGeometry=lambda: QRect(1920, 0, 1600, 1000))
    monkeypatch.setattr(QGuiApplication, "screens", lambda: [first, second])
    settings = replace(AppSettings(), ui=UiSettings(geometry=(2050, 100, 1000, 700)))
    backend = QuickBackend(settings=settings, settings_file=tmp_path / "settings.json", executor=ControlledExecutor())
    window = QWindow()
    requested = []
    monkeypatch.setattr(window, "setGeometry", requested.append)

    try:
        backend.layoutUi.restoreWindow(window, "MainWindow")
        assert requested == [QRect(2050, 100, 1000, 700)]
    finally:
        window.hide()
        backend.shutdown()
