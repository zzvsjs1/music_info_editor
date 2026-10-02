"""Responsive ordering keeps an explicit width attached to its metadata field."""

from PySide6.QtCore import Q_ARG, QMetaObject, QPointF, Qt

from tests.ui.test_quick_review_columns import _cells, backend, review_columns  # noqa: F401
from tests.ui.test_quick_table_interaction import variant, viewport


def test_explicit_final_width_survives_responsive_column_order_in_both_directions(review_columns, qtbot):  # noqa: F811
    window, table, _backend = review_columns
    view = viewport(table)

    # The ordinary review places Final at visual position three. Resize that
    # value before moving it to the front, as a saved desktop layout would do.
    assert QMetaObject.invokeMethod(view, "setColumnWidth", Q_ARG(int, 3), Q_ARG(float, 330.0))
    assert QMetaObject.invokeMethod(view, "forceLayout", Qt.ConnectionType.DirectConnection)
    qtbot.waitUntil(lambda: _cells(window, table)[4].width() == 330)

    for order in ([0, 4, 1, 2, 3, 5], [0, 2, 3, 4, 1, 5]):
        table.setProperty("columnOrder", order)
        qtbot.wait(80)
        assert _cells(window, table)[4].width() == 330

        # Preference slots use logical model columns. Both directions must
        # retain Final's width without moving it onto Existing or Proposed.
        assert QMetaObject.invokeMethod(window, "captureWidths", Qt.ConnectionType.DirectConnection)
        stored = variant(window.property("savedWidths"))
        assert stored[4] == 330
        assert stored[1] == 90
        assert stored[2] == 140
        assert stored[3] == 140


def test_responsive_reordering_reveals_leading_columns_after_horizontal_scroll(review_columns, qtbot):  # noqa: F811
    window, table, _backend = review_columns
    view = viewport(table)
    assert QMetaObject.invokeMethod(view, "setColumnWidth", Q_ARG(int, 3), Q_ARG(float, 330.0))
    window.resize(640, window.height())
    qtbot.waitUntil(lambda: view.property("contentWidth") > view.width() + 50)
    view.setProperty("contentX", view.property("contentWidth") - view.width())
    assert view.property("contentX") > 0

    # A responsive order promises the leading Field, Final and Status columns.
    # Keeping the previous order's scroll offset would hide that new context.
    table.setProperty("columnOrder", [0, 4, 1, 2, 3, 5])
    qtbot.waitUntil(lambda: abs(view.property("contentX") - view.property("originX")) < 1, timeout=700)
    # Native TableView applies delegate positions during the next polish pass;
    # the content offset changes earlier, before those pixels have settled.
    qtbot.wait(80)
    cells = _cells(window, table)

    for column in (0, 4, 1):
        assert column in cells
        start = cells[column].mapToItem(table, QPointF()).x()
        assert start >= 0
        assert start + cells[column].width() <= table.width()


def test_preferred_visible_width_follows_explicit_resize_and_hidden_columns(review_columns, qtbot):  # noqa: F811
    _window, table, _backend = review_columns
    view = viewport(table)

    # Extra viewport space stretches metadata values, but does not change the
    # readable preferred widths used to decide when a compact order is needed.
    assert table.property("preferredVisibleColumnsWidth") == 610
    assert QMetaObject.invokeMethod(view, "setColumnWidth", Q_ARG(int, 3), Q_ARG(float, 530.0))
    assert QMetaObject.invokeMethod(view, "forceLayout", Qt.ConnectionType.DirectConnection)
    qtbot.waitUntil(lambda: table.property("preferredVisibleColumnsWidth") == 1000, timeout=700)

    # Shrinking that explicit value and hiding Proposed must release space in
    # the breakpoint calculation, without counting the viewport's stretch.
    assert QMetaObject.invokeMethod(view, "setColumnWidth", Q_ARG(int, 3), Q_ARG(float, 230.0))
    assert QMetaObject.invokeMethod(view, "forceLayout", Qt.ConnectionType.DirectConnection)
    qtbot.waitUntil(lambda: table.property("preferredVisibleColumnsWidth") == 700, timeout=700)
    table.setProperty("hiddenColumns", [3, 5])
    qtbot.waitUntil(lambda: table.property("preferredVisibleColumnsWidth") == 560, timeout=700)
