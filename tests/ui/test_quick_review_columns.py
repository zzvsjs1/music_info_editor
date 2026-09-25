"""Keep review columns visual while stable model indices own their commands."""

from pathlib import Path

import pytest
from PySide6.QtCore import Q_ARG, QCoreApplication, QEvent, QMetaObject, QPointF, Qt, QUrl
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuick import QQuickItem
from PySide6.QtTest import QTest

from tests.ui.test_quick_backend import backend as backend_fixture
from tests.ui.test_quick_table_interaction import variant, viewport, visual_items

backend = pytest.fixture(backend_fixture.__wrapped__)


@pytest.fixture
def review_columns(backend, qtbot, tmp_path):  # noqa: F811
    """Exercise the production table without depending on the surrounding layout."""
    backend.selectFile("first", False)
    engine = QQmlApplicationEngine()
    warnings = []
    engine.warnings.connect(lambda messages: warnings.extend(message.toString() for message in messages))
    engine.rootContext().setContextProperty("sampleModel", backend.review)
    engine.rootContext().setContextProperty("sampleBackend", backend)
    directory = Path(__file__).parents[2] / "src/metadata_polisher/ui/quick/qml"
    source = f"""
        import QtQuick
        import QtQuick.Controls
        import "{directory.as_uri()}"

        ApplicationWindow {{
            width: 960
            height: 440
            visible: true
            property var savedWidths: []

            function captureWidths() {{
                savedWidths = reviewColumns.storedWidths();
            }}

            DataTable {{
                id: reviewColumns
                objectName: "reviewColumns"
                anchors.fill: parent
                model: sampleModel
                columnWidths: [100, 90, 140, 140, 140, 160]
                columnOrder: [0, 2, 3, 4, 1, 5]
                stretchColumns: [2, 3, 4]
                hiddenColumns: [5]

                onRowSelected: function(stableId, toggle, extend) {{
                    sampleBackend.selectFieldExtended(stableId, toggle, extend);
                }}

                onCellActivated: function(stableId, value, column) {{
                    if (column === 4) {{
                        sampleBackend.beginEdit();
                    }}
                }}
            }}
        }}
    """
    engine.loadData(source.encode(), QUrl.fromLocalFile(str(tmp_path / "ReviewColumnsHarness.qml")))
    assert engine.rootObjects(), warnings
    window = engine.rootObjects()[0]
    table = window.findChild(QQuickItem, "reviewColumns")
    assert table is not None
    window.requestActivate()
    assert QTest.qWaitForWindowActive(window, 2000)
    qtbot.wait(80)

    try:
        yield window, table, backend
    finally:
        window.hide()
        engine.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        assert not warnings, warnings


def _cells(window, table, stable_id="title"):
    # Retain wrappers for delegates with only a visual parent, as the shared
    # interaction helpers do; Python collection must not delete QML children.
    items = list(visual_items(table))
    window._column_items = getattr(window, "_column_items", []) + items
    return {item.property("column"): item for item in items
            if item.property("stableId") == stable_id and item.width() > 0 and item.isVisible()}


def test_visual_order_keeps_final_model_index_and_edit_target(review_columns, qtbot):
    window, table, backend = review_columns
    cells = _cells(window, table)
    assert sorted(cells, key=lambda column: cells[column].mapToScene(QPointF()).x()) == [0, 2, 3, 4, 1]

    # Actual pointer interaction must reach Final even though it is now the
    # fourth displayed column and remains the fifth column in the model.
    final = cells[4]
    point = final.mapToScene(QPointF(final.width() / 2, final.height() / 2)).toPoint()
    QTest.mouseClick(window, Qt.MouseButton.LeftButton, pos=point)
    QTest.mouseDClick(window, Qt.MouseButton.LeftButton, pos=point)
    qtbot.waitUntil(lambda: backend.editing)
    assert backend.selectedFields == ["title"]
    assert backend.editValue == "First title"
    assert not backend.executor.pending


def test_window_growth_is_shared_by_value_columns_without_widening_status(review_columns, qtbot):
    window, table, _backend = review_columns
    original = {column: cell.width() for column, cell in _cells(window, table).items()}
    window.resize(window.width() + 240, window.height())
    qtbot.wait(80)
    grown = _cells(window, table)

    for column in (2, 3, 4):
        assert grown[column].width() == pytest.approx(original[column] + 80, abs=1)

    assert grown[0].width() == original[0]
    assert grown[1].width() == original[1]
    assert grown[1].width() < grown[4].width()


def test_resized_value_width_and_hidden_sources_survive_layout_and_model_refresh(review_columns, qtbot):
    window, table, backend = review_columns
    view = viewport(table)
    # Qt's width API takes a visual position; its fourth visible column is
    # Final, whose delegate and persisted preference still use model index 4.
    assert QMetaObject.invokeMethod(view, "setColumnWidth", Q_ARG(int, 3), Q_ARG(float, 330.0))
    assert QMetaObject.invokeMethod(view, "forceLayout", Qt.ConnectionType.DirectConnection)
    qtbot.wait(80)
    assert _cells(window, table)[4].width() == 330

    backend.selectFile("second", False)
    table.setProperty("hiddenColumns", [])
    qtbot.wait(80)
    table.setProperty("hiddenColumns", [5])
    window.resize(window.width() + 90, window.height())
    qtbot.wait(80)
    cells = _cells(window, table)
    assert cells[4].width() == 330
    assert sorted(cells, key=lambda column: cells[column].mapToScene(QPointF()).x()) == [0, 2, 3, 4, 1]
    assert variant(table.property("hiddenColumns")) == [5]

    # Settings retain the source model's order across sessions. Saving the
    # fourth visible width into slot 3 would otherwise resize Proposed on the
    # next launch and silently lose the user's Final-column preference.
    assert QMetaObject.invokeMethod(window, "captureWidths", Qt.ConnectionType.DirectConnection)
    stored = variant(window.property("savedWidths"))
    assert stored[4] == 330
    assert stored[1] == 90
    assert stored[3] == 140


def test_placeholder_cells_retain_selection_contrast_and_full_tooltip(review_columns, qtbot):
    window, table, backend = review_columns
    cells = _cells(window, table, "composers")
    existing = cells[2]
    assert existing.property("display") == "Empty"
    assert existing.property("placeholder") is True
    assert existing.property("tooltip")

    label = next(item for item in visual_items(existing) if item.property("text") == "Empty")
    muted = label.property("color")
    backend.selectField("composers")
    qtbot.wait(80)
    assert label.property("color") != muted

    # The body text remains the literal model value: tooltip metadata must not
    # leak into the cell or become rich text merely because it contains tags.
    assert label.property("text") == "Empty"
