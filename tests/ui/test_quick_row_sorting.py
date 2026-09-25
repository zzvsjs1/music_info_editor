"""Keep secondary table ordering separate from immutable rows and command IDs."""

from dataclasses import replace
from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPointF, Qt, QUrl
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuick import QQuickItem
from PySide6.QtTest import QSignalSpy, QTest

from tests.ui.test_quick_lookup_port import activate, lookup_scene
from tests.ui.test_quick_table_interaction import variant, visual_items
from tests.unit.session.test_mapping_editing import partial_session


@pytest.fixture
def row_scene(qapp, qtbot, tmp_path):
    """Load the real shared table with stable identities and numeric text."""
    scenes = []

    def create(*, external=False):
        engine = QQmlApplicationEngine()
        warnings = []
        engine.warnings.connect(lambda messages: warnings.extend(message.toString() for message in messages))
        directory = Path(__file__).parents[2] / "src/metadata_polisher/ui/quick/qml"
        external_configuration = "externalSorting: true" if external else ""
        source = f"""
            import QtQuick
            import QtQuick.Controls
            import "{directory.as_uri()}"

            ApplicationWindow {{
                width: 720
                height: 400
                visible: true

                RowTable {{
                    objectName: "sortedRows"
                    anchors.fill: parent
                    currentId: "two"
                    {external_configuration}
                    columns: [
                        {{key: "file", label: "File", width: 170}},
                        {{key: "value", label: "Count", width: 100, sortType: "number"}},
                        {{key: "duration", label: "Duration", width: 130, sortType: "duration"}},
                        {{key: "included", label: "Include", width: 100, checkable: true}}
                    ]
                    rows: [
                        {{id: "ten", file: "zeta", value: "10", duration: "10:01", included: false}},
                        {{id: "two", file: "Alpha", value: "2", duration: "2:59", included: true}},
                        {{id: "tie", file: "alpha", value: "2", duration: "2:09", included: false}},
                        {{id: "missing", file: "beta", value: "", duration: "—", included: false}}
                    ]
                }}
            }}
        """
        engine.loadData(source.encode(), QUrl.fromLocalFile(str(tmp_path / "RowSortHarness.qml")))
        assert engine.rootObjects(), warnings
        window = engine.rootObjects()[0]
        window.requestActivate()
        assert QTest.qWaitForWindowActive(window, 2000)
        table = window.findChild(QQuickItem, "sortedRows")
        qtbot.wait(80)
        scenes.append((engine, window, warnings))

        return window, table

    yield create

    for engine, window, warnings in reversed(scenes):
        window.hide()
        engine.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        assert warnings == []


def click_heading(window, table, label):
    # Loader delegates can have visual ownership without a QObject parent.
    # Retain traversal wrappers, as the main table gesture helpers do, so Python
    # garbage collection cannot destroy a still-live QML control during a test.
    items = list(visual_items(table))
    window._table_items = getattr(window, "_table_items", []) + items
    heading = next(
        item for item in items
        if "Button" in item.metaObject().className() and item.property("text") == label
    )
    position = heading.mapToScene(QPointF(heading.width() / 2, heading.height() / 2)).toPoint()
    QTest.mouseClick(window, Qt.MouseButton.LeftButton, pos=position)


def displayed_ids(table):
    return [row["id"] for row in variant(table.property("displayRows"))]


def test_header_sorts_numbers_stably_and_keeps_missing_values_last(row_scene):
    window, table = row_scene()
    original = variant(table.property("rows"))
    selected = QSignalSpy(table.rowSelected)
    chosen = QSignalSpy(table.chosen)
    assert table.property("sortable") is True

    click_heading(window, table, "Count")
    assert displayed_ids(table) == ["two", "tie", "ten", "missing"]
    assert table.property("sortKey") == "value"
    assert table.property("sortDescending") is False

    click_heading(window, table, "Count")
    assert displayed_ids(table) == ["ten", "two", "tie", "missing"]
    assert table.property("sortDescending") is True
    assert variant(table.property("rows")) == original
    assert selected.count() == chosen.count() == 0


def test_sort_keeps_command_identity_and_keyboard_uses_visible_order(row_scene):
    window, table = row_scene()
    activated = QSignalSpy(table.activated)
    chosen = QSignalSpy(table.chosen)
    click_heading(window, table, "Count")

    # Sorting moves the selected row without changing the details, Enter target
    # or inclusion intent. Arrow navigation then follows the visible neighbour.
    assert table.property("currentId") == "two"
    assert table.property("currentIndex") == 0
    assert "File: Alpha" in table.property("details")
    table.forceActiveFocus()
    QTest.keyClick(window, Qt.Key.Key_Return)
    QTest.keyClick(window, Qt.Key.Key_Space)
    assert activated.at(0) == ["two"]
    assert chosen.at(0) == ["two", False]

    QTest.keyClick(window, Qt.Key.Key_Down)
    assert table.property("currentId") == "tie"
    QTest.keyClick(window, Qt.Key.Key_Space)
    assert chosen.at(1) == ["tie", True]


def test_text_and_duration_sorting_handle_case_and_numeric_minutes(row_scene):
    window, table = row_scene()
    click_heading(window, table, "File")
    assert displayed_ids(table) == ["two", "tie", "missing", "ten"]

    click_heading(window, table, "Duration")
    assert displayed_ids(table) == ["tie", "two", "ten", "missing"]
    click_heading(window, table, "Duration")
    assert displayed_ids(table) == ["ten", "two", "tie", "missing"]


def test_row_refresh_retains_sort_and_selected_identity(row_scene):
    window, table = row_scene()
    click_heading(window, table, "Count")
    rows = variant(table.property("rows"))
    rows[1]["value"] = "12"
    rows[1]["included"] = False
    table.setProperty("rows", rows)

    assert displayed_ids(table) == ["tie", "ten", "two", "missing"]
    assert table.property("currentId") == "two"
    assert table.property("currentIndex") == 2
    assert variant(table.property("currentRow"))["included"] is False


def test_mapping_sort_uses_numeric_identity_and_keeps_unmapped_last(row_scene):
    window, table = row_scene()
    table.setProperty("columns", [
        {"key": "assignment", "label": "Provider track", "width": 300,
         "sortValueKey": "track", "sortType": "number", "sortMissingValue": -1},
    ])
    table.setProperty("rows", [
        {"id": "ten", "assignment": "10. Tenth track", "track": 9},
        {"id": "missing", "assignment": "Unmapped", "track": -1},
        {"id": "two", "assignment": "2. Second track", "track": 1},
    ])

    click_heading(window, table, "Provider track")
    assert displayed_ids(table) == ["two", "ten", "missing"]
    assert variant(table.property("currentRow"))["assignment"] == "2. Second track"

    click_heading(window, table, "Provider track")
    assert displayed_ids(table) == ["ten", "two", "missing"]
    assert table.property("currentId") == "two"


def test_external_sort_dispatches_without_reordering_provider_rows(row_scene):
    window, table = row_scene(external=True)
    requested = QSignalSpy(table.sortRequested)
    click_heading(window, table, "Count")
    assert requested.at(0) == ["value", False]
    assert displayed_ids(table) == ["ten", "two", "tie", "missing"]

    # Candidate rows already use a facade ranking and numeric score ordering;
    # retain that authority while still tracking selection after its refresh.
    table.setProperty("rows", list(reversed(variant(table.property("rows")))))
    assert displayed_ids(table) == ["missing", "tie", "two", "ten"]
    assert table.property("currentId") == "two"
    assert table.property("currentIndex") == 2


def test_sorted_mapping_edits_keep_focus_and_reject_duplicate_assignments(qtbot):
    state = partial_session()
    group = state.groups[0]
    first = group.group.files[0]
    second = replace(first, path=first.path.with_name("02.flac"), file_id="mapping-second")
    mapping = replace(group.automatic_track_mapping, unmatched_local_file_ids=(first.file_id, second.file_id))
    group = replace(group, group=replace(group.group, files=(first, second)), automatic_track_mapping=mapping)
    state = replace(state, groups=(group,))

    with lookup_scene(qtbot, state=state) as (root, backend):
        lookup = backend.lookupUi
        captured = backend.session_state
        assert lookup.beginMapping()
        window = activate(root, "quickMappingWindow")
        table = window.findChild(QQuickItem, "quickMappingTable")
        click_heading(window, table, "Provider track")
        # A header click replaces the immutable ListView projection. Wait for
        # its visual delegates to settle before grabbing an editor; the earlier
        # delegate can otherwise still exist while scheduled for destruction.
        qtbot.wait(80)

        def selector(file_id):
            items = list(visual_items(table))
            window._table_items = getattr(window, "_table_items", []) + items
            return next(
                item for item in items
                if item.objectName() == "mappingTrackSelector_" + file_id and item.isVisible()
            )

        def assignments():
            return {row["id"]: row["track"] for row in lookup.mappingRows}

        # Assigning the second file moves its sorted row from last to first.
        # Its replacement editor must receive focus so the next key still edits
        # that file, even though the visible row number has changed.
        selector(second.file_id).forceActiveFocus()
        assert selector(second.file_id).hasActiveFocus(), (
            window.isActive(),
            window.activeFocusItem().objectName() if window.activeFocusItem() is not None else None,
            table.property("currentId"),
        )
        QTest.keyClick(window, Qt.Key.Key_Down)
        qtbot.waitUntil(lambda: assignments()[second.file_id] == 0)
        qtbot.waitUntil(lambda: selector(second.file_id).hasActiveFocus())
        assert displayed_ids(table) == [second.file_id, first.file_id]
        assert table.property("currentId") == second.file_id
        assert table.property("currentIndex") == 0
        assert "Manual assignment" in table.property("details")

        QTest.keyClick(window, Qt.Key.Key_Up)
        qtbot.waitUntil(lambda: assignments()[second.file_id] == -1)
        qtbot.waitUntil(lambda: selector(second.file_id).hasActiveFocus())
        assert displayed_ids(table) == [first.file_id, second.file_id]
        assert table.property("currentIndex") == 1

        QTest.keyClick(window, Qt.Key.Key_Down)
        qtbot.waitUntil(lambda: assignments()[second.file_id] == 0)
        qtbot.waitUntil(lambda: selector(second.file_id).hasActiveFocus())
        selector(first.file_id).forceActiveFocus()
        QTest.keyClick(window, Qt.Key.Key_Down)
        qtbot.waitUntil(lambda: bool(lookup.mappingError))
        qtbot.waitUntil(lambda: selector(first.file_id).hasActiveFocus())
        assert assignments() == {first.file_id: -1, second.file_id: 0}
        assert table.property("currentId") == first.file_id
        assert table.property("currentIndex") == 1
        assert selector(first.file_id).property("currentIndex") == 0
        assert backend.session_state is captured

        lookup.cancelMapping()
        assert backend.session_state is captured
