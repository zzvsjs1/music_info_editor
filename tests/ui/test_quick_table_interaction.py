"""Exercise table navigation, scrolling and recovery in the actual QML controls."""

from pathlib import Path

import pytest
from PySide6.QtCore import (
    Q_ARG,
    Property,
    QAbstractTableModel,
    QByteArray,
    QCoreApplication,
    QEvent,
    QMetaObject,
    QPoint,
    QPointF,
    Qt,
    QUrl,
)
from PySide6.QtGui import QFont, QFontMetrics, QGuiApplication, QWheelEvent
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuick import QQuickItem
from PySide6.QtTest import QSignalSpy, QTest

from tests.ui.rendering import window_image_scale


class TableRows(QAbstractTableModel):
    """Supply enough synthetic rows to require both scrollbar directions."""

    columnTitles = Property(list, lambda self: ["File", "Title", "Details"], constant=True)

    def rowCount(self, parent=None):
        return 0 if parent is not None and parent.isValid() else 80

    def columnCount(self, parent=None):
        return 0 if parent is not None and parent.isValid() else 3

    def roleNames(self):
        return {
            Qt.ItemDataRole.DisplayRole: QByteArray(b"display"),
            Qt.ItemDataRole.UserRole + 1: QByteArray(b"stableId"),
            Qt.ItemDataRole.UserRole + 2: QByteArray(b"highlighted"),
            Qt.ItemDataRole.UserRole + 3: QByteArray(b"included"),
        }

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None

        if role == Qt.ItemDataRole.DisplayRole:
            return f"Row {index.row():02d}, column {index.column()}"

        if role == Qt.ItemDataRole.UserRole + 1:
            return f"file-{index.row():03d}"

        if role in (Qt.ItemDataRole.UserRole + 2, Qt.ItemDataRole.UserRole + 3):
            return False

        return None

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.columnTitles[section]

        return None


@pytest.fixture
def table_scene(qapp, qtbot, tmp_path):
    """Load the real components without the retired Widgets test fixtures."""
    scenes = []

    def create(kind="DataTable", *, right_margin=0):
        engine = QQmlApplicationEngine()
        model = TableRows(engine)
        engine.rootContext().setContextProperty("sampleModel", model)
        warnings = []
        engine.warnings.connect(lambda messages: warnings.extend(message.toString() for message in messages))
        directory = Path(__file__).parents[2] / "src/metadata_polisher/ui/quick/qml"

        if kind == "DataTable":
            configuration = """
                model: sampleModel
                columnWidths: [180, 260, 500]
                currentRow: 3
                onMoveExtendedRequested: function(delta, extend) {
                    currentRow = Math.max(0, Math.min(sampleModel.rowCount() - 1, currentRow + delta));
                }
            """
        else:
            configuration = """
                showDetails: false
                columns: [{key: "file", label: "File", width: 180},
                          {key: "title", label: "Title", width: 260},
                          {key: "detail", label: "Details", width: 500}]
                rows: Array.from({length: 80}, function(_, index) {
                    return {id: "file-" + index, file: "File " + index,
                            title: "Title " + index, detail: "Detail " + index};
                })
            """

        source = f"""
            import QtQuick
            import QtQuick.Controls
            import "{directory.as_uri()}"

            ApplicationWindow {{
                width: 640
                height: 360
                visible: true

                {kind} {{
                    objectName: "testedTable"
                    anchors.fill: parent
                    anchors.rightMargin: {right_margin}
                    {configuration}
                }}
            }}
        """
        engine.loadData(source.encode(), QUrl.fromLocalFile(str(tmp_path / f"{kind}Harness.qml")))
        assert engine.rootObjects(), warnings
        window = engine.rootObjects()[0]
        window.requestActivate()
        assert QTest.qWaitForWindowActive(window, 2000)
        table = window.findChild(QQuickItem, "testedTable")
        qtbot.wait(80)
        scenes.append((engine, window, model, warnings))

        return window, table

    yield create

    for engine, window, _model, warnings in reversed(scenes):
        window.hide()
        engine.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        assert warnings == []


def viewport(table):
    return next(
        item for item in table.findChildren(QQuickItem)
        if item.metaObject().className() in ("QQuickTableView", "QQuickListView")
    )


def visual_items(item):
    yield item

    for child in item.childItems():
        yield from visual_items(child)


def variant(value):
    return value.toVariant() if hasattr(value, "toVariant") else value


def test_page_navigation_uses_visible_rows_and_shift_extension(table_scene, qtbot):
    window, table = table_scene()
    moved = QSignalSpy(table.moveExtendedRequested)
    included = QSignalSpy(table.inclusionRequested)
    table.forceActiveFocus()
    initial = table.property("currentRow")

    QTest.keyClick(window, Qt.Key.Key_PageDown, Qt.KeyboardModifier.ShiftModifier)
    qtbot.waitUntil(lambda: table.property("currentRow") > initial)
    delta, extended = moved.at(0)
    assert extended is True
    assert delta == max(1, int(viewport(table).height() / table.property("rowHeight")))

    QTest.keyClick(window, Qt.Key.Key_PageUp)
    qtbot.waitUntil(lambda: table.property("currentRow") == initial)
    assert moved.at(1) == [-delta, False]
    assert included.count() == 0


def test_control_boundaries_reveal_the_target_and_keep_shift_intent(table_scene, qtbot):
    window, table = table_scene()
    moved = QSignalSpy(table.moveExtendedRequested)
    table.forceActiveFocus()

    QTest.keyClick(window, Qt.Key.Key_End, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
    qtbot.waitUntil(lambda: table.property("currentRow") == 79)
    assert moved.at(0) == [76, True]
    qtbot.waitUntil(lambda: viewport(table).property("contentY") > 0)

    QTest.keyClick(window, Qt.Key.Key_Home, Qt.KeyboardModifier.ControlModifier)
    qtbot.waitUntil(lambda: table.property("currentRow") == 0)
    assert moved.at(1) == [-79, False]
    qtbot.waitUntil(lambda: abs(viewport(table).property("contentY")) < 1)


def test_blank_header_can_restore_every_hidden_column(table_scene, qtbot):
    window, table = table_scene()
    table.setProperty("hiddenColumns", [0, 1, 2])
    qtbot.wait(80)
    QTest.mouseClick(window, Qt.MouseButton.RightButton, pos=QPoint(20, table.property("headerHeight") // 2))
    qtbot.wait(80)
    choices = [
        item for item in visual_items(window.contentItem())
        if "MenuItem" in item.metaObject().className()
        and item.property("text") == "File" and item.isVisible()
    ]
    assert choices, "An empty header must still expose its column recovery menu."
    choice = choices[0]
    position = choice.mapToScene(QPointF(choice.width() / 2, choice.height() / 2)).toPoint()
    QTest.mouseClick(window, Qt.MouseButton.LeftButton, pos=position)
    qtbot.waitUntil(lambda: 0 not in variant(table.property("hiddenColumns")))


def test_last_stretched_column_accepts_an_explicit_header_resize(table_scene, qtbot):
    # Leave space beyond the table edge so the native test pointer remains in
    # its window. QTest events outside the window are platform-dependent and
    # cannot reliably establish whether the header retained its mouse grab.
    window, table = table_scene(right_margin=96)
    table.setProperty("columnWidths", [90, 90, 120])
    table.setProperty("stretchLastColumn", True)
    assert QMetaObject.invokeMethod(viewport(table), "forceLayout", Qt.ConnectionType.DirectConnection)
    qtbot.wait(80)
    label = next(item for item in visual_items(table) if item.property("text") == "Details")
    heading = label.parentItem()
    qtbot.waitUntil(lambda: heading.width() >= table.width() - 182)
    before = heading.width()
    edge = heading.mapToScene(QPointF(before - 2, heading.height() / 2)).toPoint()

    # Continue beyond the table edge while the pressed header keeps the grab;
    # the resulting content must overflow and become horizontally scrollable.
    QTest.mouseMove(window, edge)
    qtbot.wait(40)
    QTest.mousePress(window, Qt.MouseButton.LeftButton, pos=edge)
    # The first movement crosses Qt's drag threshold. Subsequent movements
    # exercise the resize itself before releasing, as a real pointer does.
    QTest.mouseMove(window, edge + QPoint(12, 0), delay=30)
    QTest.mouseMove(window, edge + QPoint(35, 0), delay=30)
    QTest.mouseMove(window, edge + QPoint(70, 0), delay=50)
    QTest.mouseRelease(window, Qt.MouseButton.LeftButton, pos=edge + QPoint(70, 0))
    qtbot.waitUntil(lambda: heading.width() >= before + 50)

    # A normal layout invalidation (for example showing another column) must
    # retain the user's explicit size after the resize gesture has completed.
    assert QMetaObject.invokeMethod(viewport(table), "forceLayout", Qt.ConnectionType.DirectConnection)
    qtbot.wait(80)
    assert heading.width() >= before + 50


def test_stretched_column_body_keeps_an_explicit_width_after_layout(table_scene, qtbot):
    _window, table = table_scene()
    table.setProperty("columnWidths", [90, 90, 120])
    table.setProperty("stretchLastColumn", True)
    qtbot.wait(80)
    view = viewport(table)

    # The header and programmatic preference restore both use Qt's explicit
    # column widths. A later provider evaluation must honour that same width
    # in the body instead of reporting it as saved but painting a narrow cell.
    assert QMetaObject.invokeMethod(view, "setColumnWidth", Q_ARG(int, 2), Q_ARG(float, 700.0))
    assert QMetaObject.invokeMethod(view, "forceLayout", Qt.ConnectionType.DirectConnection)
    qtbot.wait(80)
    cell = next(item for item in visual_items(table) if item.property("text") == "Row 04, column 2")
    assert cell.parentItem().width() == 700


def test_table_rows_and_headers_grow_with_the_control_font(table_scene, qtbot):
    _window, table = table_scene()
    enlarged = QFont(table.property("font"))
    enlarged.setPointSize(24)
    table.setProperty("font", enlarged)
    qtbot.wait(80)
    height = QFontMetrics(enlarged).height()

    assert table.property("rowHeight") >= height + 8
    assert table.property("headerHeight") >= height + 6


@pytest.mark.parametrize("kind", ["DataTable", "RowTable"])
def test_wheel_scrolls_both_axes_without_retargeting_the_row(table_scene, qtbot, kind):
    window, table = table_scene(kind)
    view = viewport(table)
    selection_property = "currentRow" if kind == "DataTable" else "currentId"
    initial = table.property(selection_property)
    position = view.mapToScene(QPointF(view.width() / 2, view.height() / 2))

    for angle in (QPoint(0, -120), QPoint(-120, 0)):
        event = QWheelEvent(
            position, QPointF(window.mapToGlobal(position.toPoint())), QPoint(), angle,
            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.NoScrollPhase, False,
        )
        QCoreApplication.sendEvent(window, event)
        qtbot.wait(80)

    assert view.property("contentY") > 0
    assert view.property("contentX") > 0
    assert table.property(selection_property) == initial

    # Matching header and cell positions proves that horizontal scrolling has
    # moved their shared columns together, including the inset table border.
    header = next(item for item in visual_items(table) if item.property("text") == "Title")
    cell_text = "Row 04, column 1" if kind == "DataTable" else "Title 4"
    cell = next(item for item in visual_items(table) if item.property("text") == cell_text)
    heading = header.parentItem() if kind == "DataTable" else header
    header_x = heading.mapToScene(QPointF()).x()
    cell_x = cell.parentItem().mapToScene(QPointF()).x()
    assert abs(header_x - cell_x) <= 1


@pytest.mark.parametrize("kind", ["DataTable", "RowTable"])
def test_diagonal_pixel_scroll_preserves_both_distances(table_scene, qtbot, kind):
    window, table = table_scene(kind)
    view = viewport(table)
    position = view.mapToScene(QPointF(view.width() / 2, view.height() / 2))
    before_x = view.property("contentX")
    before_y = view.property("contentY")
    event = QWheelEvent(
        position, QPointF(window.mapToGlobal(position.toPoint())), QPoint(-45, -65), QPoint(-120, -120),
        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.ScrollUpdate, False,
    )
    QCoreApplication.sendEvent(window, event)
    qtbot.wait(80)

    # Smooth-scroll pixel distances take priority over the legacy angle ticks;
    # accepting a horizontal gesture must retain its simultaneous vertical part.
    assert view.property("contentX") == pytest.approx(before_x + 45, abs=1)
    assert view.property("contentY") == pytest.approx(before_y + 65, abs=1)


def horizontal_bar(table):
    return next(
        item for item in visual_items(table)
        if "ScrollBar" in item.metaObject().className()
        and item.property("orientation") == Qt.Orientation.Horizontal
    )


@pytest.mark.parametrize("kind", ["DataTable", "RowTable"])
def test_native_horizontal_scrollbar_has_a_continuous_track(table_scene, qtbot, kind):
    if QGuiApplication.platformName() != "windows":
        pytest.skip("Native Windows scrollbar painting requires the Windows QPA plugin.")

    window, table = table_scene(kind)
    bar = horizontal_bar(table)
    assert bar.isVisible() and bar.property("size") < 0.8

    image = window.grabWindow()
    scale_x, scale_y = window_image_scale(window, image)
    origin = bar.mapToScene(QPointF())

    def colour(x_fraction, y_fraction):
        x = round((origin.x() + bar.width() * x_fraction) * scale_x)
        y = round((origin.y() + bar.height() * y_fraction) * scale_y)
        return image.pixelColor(x, y)

    # The original groove defect exposed white content pixels at its top edge.
    # Compare that boundary with the empty track beyond the thumb, and verify
    # the reference itself lands on the track rather than blank table content.
    track = colour(0.9, 0.5)
    assert track == bar.property("background").property("color")
    assert colour(0.1, 0.001) == track
    assert colour(0.9, 0.001) == track


@pytest.mark.parametrize("kind", ["DataTable", "RowTable"])
def test_horizontal_scrollbar_end_button_moves_one_step(table_scene, qtbot, kind):
    window, table = table_scene(kind)
    bar = horizontal_bar(table)
    assert bar.isVisible() and bar.property("size") < 0.8

    bar.setProperty("stepSize", 0.02)
    end_button = bar.mapToScene(QPointF(bar.width() - bar.height() / 2, bar.height() / 2)).toPoint()
    QTest.mouseClick(window, Qt.MouseButton.LeftButton, pos=end_button)
    qtbot.wait(50)

    assert bar.property("position") == pytest.approx(0.02, abs=0.002)


def test_include_checkbox_keeps_the_visible_row_id_after_delegate_reuse(table_scene, qtbot):
    window, table = table_scene()
    table.setProperty("inclusionColumn", True)
    view = viewport(table)
    requested = QSignalSpy(table.inclusionRequested)

    def visible_toggle():
        return next(
            item for item in visual_items(table)
            if item.metaObject().className() == "QQuickCheckBox" and item.isVisible()
            and item.parentItem().property("column") == 0
            and 0 <= item.mapToItem(view, QPointF()).y() < view.height() - item.height()
        )

    first = visible_toggle()
    first_id = first.parentItem().property("stableId")
    QTest.mouseClick(window, Qt.MouseButton.LeftButton,
                     pos=first.mapToScene(QPointF(first.width() / 2, first.height() / 2)).toPoint())
    assert requested.at(0) == [first_id, True]

    # TableView recycles its delegates while scrolling. The checkbox must read
    # the new row role when clicked rather than retaining the earlier file ID.
    view.setProperty("contentY", table.property("rowHeight") * 50)
    qtbot.wait(80)
    later = visible_toggle()
    later_id = later.parentItem().property("stableId")
    assert later_id != first_id
    QTest.mouseClick(window, Qt.MouseButton.LeftButton,
                     pos=later.mapToScene(QPointF(later.width() / 2, later.height() / 2)).toPoint())
    assert requested.at(1) == [later_id, True]


@pytest.mark.parametrize("kind", ["DataTable", "RowTable"])
def test_dragging_the_horizontal_bar_keeps_its_gutter_stable(table_scene, qtbot, kind):
    window, table = table_scene(kind)
    view = viewport(table)
    bar = horizontal_bar(table)
    assert bar.isVisible() and bar.property("size") < 1
    initial_view_height = view.height()
    initial_bar_top = bar.mapToScene(QPointF()).y()
    initial_content_x = view.property("contentX")

    # Drag the actual thumb rather than setting contentX directly. A gutter
    # whose height follows scrollbar visibility can jump during this gesture.
    thumb_centre = bar.width() * bar.property("size") / 2
    start = bar.mapToScene(QPointF(thumb_centre, bar.height() / 2)).toPoint()
    middle = start + QPoint(12, 0)
    end = start + QPoint(100, 0)
    QTest.mouseMove(window, start)
    QTest.mousePress(window, Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(window, middle)
    QTest.mouseMove(window, end)
    QTest.mouseRelease(window, Qt.MouseButton.LeftButton, pos=end)
    qtbot.wait(80)

    assert view.property("contentX") > initial_content_x
    assert view.height() == pytest.approx(initial_view_height)
    assert bar.mapToScene(QPointF()).y() == pytest.approx(initial_bar_top)
    assert bar.mapToScene(QPointF()).y() >= view.mapToScene(QPointF(0, view.height())).y() - 1


@pytest.mark.parametrize("kind", ["DataTable", "RowTable"])
def test_last_row_is_fully_visible_above_the_horizontal_scrollbar(table_scene, qtbot, kind):
    window, table = table_scene(kind)
    table.forceActiveFocus()
    modifiers = Qt.KeyboardModifier.ControlModifier if kind == "DataTable" else Qt.KeyboardModifier.NoModifier
    QTest.keyClick(window, Qt.Key.Key_End, modifiers)
    qtbot.waitUntil(lambda: viewport(table).property("contentY") > 0)
    qtbot.wait(80)
    view = viewport(table)
    bar = horizontal_bar(table)
    assert bar.isVisible() and bar.height() > 0
    bar_top = bar.mapToScene(QPointF()).y()
    view_bottom = view.mapToScene(QPointF(0, view.height())).y()

    # A selected last row must remain legible. Geometry checks cover the whole
    # painted cell, not merely a text baseline that can sit behind the thumb.
    cell_text = "Row 79, column 0" if kind == "DataTable" else "File 79"
    label = next(item for item in visual_items(table) if item.property("text") == cell_text)
    cell = label.parentItem()
    cell_top = cell.mapToScene(QPointF()).y()
    cell_bottom = cell.mapToScene(QPointF(0, cell.height())).y()

    assert bar_top >= view_bottom - 1, "The horizontal scrollbar must have its own space below the viewport."
    assert cell_top >= view.mapToScene(QPointF()).y() - 1
    assert cell_bottom <= min(view_bottom, bar_top) + 1


def test_minimum_table_height_reserves_one_complete_row_and_scrollbar(table_scene, qtbot):
    window, table = table_scene()
    bar = horizontal_bar(table)
    assert bar.isVisible()
    minimum = table.property("minimumTableHeight")
    expected = table.property("rowHeight") + table.property("headerHeight") + bar.height() + 2
    assert minimum == pytest.approx(expected)

    # Consumers can use this minimum without knowing the active Controls
    # style's scrollbar thickness or the user's current text size.
    window.resize(window.width(), int(minimum))
    table.forceActiveFocus()
    QTest.keyClick(window, Qt.Key.Key_Home, Qt.KeyboardModifier.ControlModifier)
    qtbot.wait(80)
    view = viewport(table)
    assert view.height() >= table.property("rowHeight")
    assert bar.mapToScene(QPointF()).y() >= view.mapToScene(QPointF(0, view.height())).y() - 1
