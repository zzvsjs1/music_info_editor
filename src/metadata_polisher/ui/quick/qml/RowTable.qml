import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// Small immutable row projections share one interaction surface. Selection is
// identified by the supplied stable id; checkbox intent remains in the backend.
Control {
    id: root

    property var rows: []
    property var columns: []
    property QtObject layout: null
    property string preferenceKey: ""
    property var columnWidths: []
    property var hiddenColumns: []
    property int protectedColumn: -1
    property bool stretchLastColumn: true
    property bool scaleColumnWidths: false
    property bool sortable: false
    property bool showDetails: true
    property bool gridLines: false
    property Component cellDelegate: null
    property string currentId: ""

    // A ListView resets its native index when its model binding is replaced.
    // Keep the semantic selection here so that a sort cannot retarget Enter,
    // full details or checkboxes between model notification and the next frame.
    property int currentIndex: -1
    readonly property alias count: list.count
    readonly property var currentRow: currentIndex >= 0 && currentIndex < rows.length ? rows[currentIndex] : null
    readonly property string details: rowDetails(currentRow)
    property string sortKey: ""
    property bool sortDescending: false
    property bool preferencesReady: false
    property var defaultHiddenColumns: []

    signal rowSelected(string id)
    signal activated(string id)
    signal chosen(string id, bool included)
    signal sortRequested(string key, bool descending)

    activeFocusOnTab: true
    focusPolicy: Qt.StrongFocus
    implicitWidth: 400
    implicitHeight: 260

    UiMetrics { id: metrics }
    FontMetrics { id: textMetrics; font: root.font }

    readonly property real rowHeight: Math.max(30, textMetrics.height + metrics.spacingLarge)
    readonly property real headerHeight: Math.max(26, textMetrics.height + metrics.spacing)
    readonly property real horizontalScrollBarHeight: horizontalScrollBar.implicitHeight
    readonly property color gridColour: Qt.tint(palette.base,
        Qt.rgba(palette.text.r, palette.text.g, palette.text.b, 0.15))

    function restoreColumns() {
        // Widgets configure_columns scales its starting widths to the font;
        // direct setColumnWidth dialogues use logical pixels. Persisted user
        // widths already describe the visible layout and must not scale twice.
        const scale = scaleColumnWidths ? textMetrics.advanceWidth("M") / 10 : 1;
        const defaults = columns.map(function(column) { return Math.round((column.width || 160) * scale); });
        columnWidths = layout && preferenceKey ? layout.columnWidths(preferenceKey, defaults) : defaults;
        hiddenColumns = layout && preferenceKey
            ? layout.hiddenColumns(preferenceKey, defaultHiddenColumns) : defaultHiddenColumns.slice();
    }

    function saveColumns() {
        if (preferencesReady && layout && preferenceKey) {
            layout.saveColumns(preferenceKey, columnWidths, hiddenColumns);
        }
    }

    function visibleWidth(index) {
        if (hiddenColumns.indexOf(index) >= 0) {
            return 0;
        }

        const base = columnWidths[index] || columns[index].width || 160;
        let last = columns.length - 1;

        while (last >= 0 && hiddenColumns.indexOf(last) >= 0) {
            last--;
        }

        if (stretchLastColumn && index === last) {
            let preceding = 0;

            for (let other = 0; other < last; other++) {
                if (hiddenColumns.indexOf(other) < 0) {
                    preceding += columnWidths[other] || columns[other].width || 160;
                }
            }

            return Math.max(base, list.width - preceding);
        }

        return base;
    }

    readonly property real totalWidth: {
        let total = 0;

        for (let index = 0; index < columns.length; index++) {
            total += visibleWidth(index);
        }

        return total;
    }

    function rowDetails(row) {
        if (!row) {
            return "";
        }

        return columns.map(function(column) {
            const value = row[column.detailKey || column.key];
            return column.label + ": " + (value === undefined || value === null ? "" : String(value));
        }).join("\n");
    }

    function restoreSelection() {
        const index = rows.findIndex(function(row) { return String(row.id) === currentId; });
        currentIndex = index;

        if (index >= 0) {
            list.positionViewAtIndex(index, ListView.Contain);
        }

        // The bound ListView model may settle after this property's handler.
        // Only viewport positioning waits; the command target is already valid.
        Qt.callLater(positionCurrentRow);
    }

    function positionCurrentRow() {
        if (currentIndex >= 0 && currentIndex < list.count) {
            list.positionViewAtIndex(currentIndex, ListView.Contain);
        }
    }

    function scrollHorizontally(delta, verticalDelta) {
        // A wheel can change axis while the preceding vertical flick is still
        // decelerating. Apply the horizontal distance explicitly so that both
        // the body and its bound header continue moving together.
        list.cancelFlick();
        const first = list.originX;
        const last = first + Math.max(0, list.contentWidth - list.width);
        list.contentX = Math.max(first, Math.min(last, list.contentX + delta));

        // The accepted event can contain a simultaneous vertical touchpad
        // distance. Keep both parts within this viewport's scrollable bounds.
        if (verticalDelta) {
            const top = list.originY;
            const bottom = top + Math.max(0, list.contentHeight - list.height);
            list.contentY = Math.max(top, Math.min(bottom, list.contentY + verticalDelta));
        }
    }

    function selectRow(index) {
        if (index < 0 || index >= rows.length) {
            return;
        }

        const id = String(rows[index].id);
        currentIndex = index;
        rowSelected(id);

        // A parent may synchronously update its authoritative currentId binding
        // in response. Preserve that binding when it already contains this id.
        if (currentId !== id) {
            currentId = id;
        }

        list.positionViewAtIndex(index, ListView.Contain);
    }

    function handleKey(event) {
        const page = Math.max(1, Math.floor(list.height / rowHeight));
        let target = currentIndex;

        if (event.key === Qt.Key_Home) {
            target = 0;
        } else if (event.key === Qt.Key_End) {
            target = rows.length - 1;
        } else if (event.key === Qt.Key_Up) {
            target = Math.max(0, target - 1);
        } else if (event.key === Qt.Key_Down) {
            target = Math.min(rows.length - 1, target + 1);
        } else if (event.key === Qt.Key_PageUp) {
            target = Math.max(0, target - page);
        } else if (event.key === Qt.Key_PageDown) {
            target = Math.min(rows.length - 1, target + page);
        } else if (event.key === Qt.Key_Space && currentRow) {
            const column = columns.find(function(item) { return item.checkable === true; });

            if (!column) {
                return;
            }

            chosen(String(currentRow.id), currentRow[column.key] !== true);
            event.accepted = true;
            return;
        } else if ((event.key === Qt.Key_Return || event.key === Qt.Key_Enter) && currentRow) {
            activated(String(currentRow.id));
            event.accepted = true;
            return;
        } else if (event.key === Qt.Key_C && (event.modifiers & Qt.ControlModifier)) {
            fullDetails.selectAll();
            fullDetails.copy();
            fullDetails.deselect();
            event.accepted = true;
            return;
        } else {
            return;
        }

        selectRow(target);
        event.accepted = true;
    }

    Keys.onPressed: function(event) { handleKey(event); }
    onCurrentIdChanged: {
        if (preferencesReady) {
            restoreSelection();
        }
    }

    onRowsChanged: {
        if (preferencesReady) {
            restoreSelection();
        }
    }

    Component.onCompleted: {
        defaultHiddenColumns = hiddenColumns.slice();
        restoreColumns();
        preferencesReady = true;
        restoreSelection();
    }

    Connections {
        target: root.layout
        function onChanged() { root.restoreColumns(); }
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: metrics.spacingSmall

        Rectangle {
            id: tableFrame
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.minimumHeight: root.headerHeight + root.rowHeight + root.horizontalScrollBarHeight + 2
            // Leave unused viewport space in the window surface colour. Data
            // rows keep palette.base so a small result set stays legible.
            color: root.palette.window
            border.color: root.activeFocus || list.activeFocus ? root.palette.highlight : root.palette.mid
            clip: true

            Item {
                id: header
                anchors.top: parent.top
                anchors.left: parent.left
                anchors.right: parent.right
                height: root.headerHeight
                clip: true

                // Keep column recovery available across the whole header,
                // including empty space after every column has been hidden.
                MouseArea {
                    anchors.fill: parent
                    acceptedButtons: Qt.RightButton
                    onClicked: columnMenu.popup()
                }

                Row {
                    x: -list.contentX
                    height: parent.height

                    Repeater {
                        model: root.columns

                        delegate: Button {
                            id: heading
                            required property var modelData
                            required property int index
                            width: root.visibleWidth(index)
                            height: header.height
                            visible: width > 0
                            text: modelData.label
                            flat: true
                            padding: metrics.spacingSmall

                            Rectangle {
                                anchors.top: parent.top
                                anchors.bottom: parent.bottom
                                anchors.right: parent.right
                                width: 1
                                color: root.gridColour
                                visible: root.gridLines
                            }

                            Label {
                                anchors.right: parent.right
                                anchors.rightMargin: metrics.spacing
                                anchors.verticalCenter: parent.verticalCenter
                                visible: root.sortable && root.sortKey === heading.modelData.key
                                text: root.sortDescending ? "▾" : "▴"
                            }

                            onClicked: {
                                if (root.sortable) {
                                    root.sortDescending = root.sortKey === modelData.key ? !root.sortDescending : false;
                                    root.sortKey = modelData.key;
                                    root.sortRequested(root.sortKey, root.sortDescending);
                                }
                            }

                            // Drag from the edge, including over the header text.
                            // Coordinates are measured in the fixed root so the
                            // moving edge cannot compound the requested distance.
                            MouseArea {
                                anchors.right: parent.right
                                anchors.top: parent.top
                                anchors.bottom: parent.bottom
                                width: metrics.spacing
                                cursorShape: Qt.SplitHCursor
                                property real pressX: 0
                                property real initialWidth: 0

                                onPressed: function(mouse) {
                                    pressX = mapToItem(root, mouse.x, mouse.y).x;
                                    initialWidth = heading.width;
                                }

                                onPositionChanged: function(mouse) {
                                    if (!pressed) {
                                        return;
                                    }

                                    const widths = root.columnWidths.slice();
                                    widths[heading.index] = Math.max(36,
                                        Math.round(initialWidth + mapToItem(root, mouse.x, mouse.y).x - pressX));
                                    root.columnWidths = widths;
                                }

                                onReleased: root.saveColumns()
                            }
                        }
                    }
                }

                // The header rule also covers space after the final column.
                Rectangle {
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.bottom: parent.bottom
                    height: 1
                    color: root.gridColour
                    visible: root.gridLines
                }
            }

            ListView {
                id: list
                anchors.top: header.bottom
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.bottom: parent.bottom
                anchors.margins: 1
                anchors.bottomMargin: root.horizontalScrollBarHeight + 1
                clip: true
                model: root.rows
                currentIndex: root.currentIndex
                contentWidth: root.totalWidth
                flickableDirection: Flickable.AutoFlickDirection
                boundsBehavior: Flickable.StopAtBounds
                keyNavigationEnabled: false
                activeFocusOnTab: true
                Keys.onPressed: function(event) { root.handleKey(event); }
                ScrollBar.vertical: AppScrollBar { active: true }

                // Reserve a gutter inside the table frame, above the separate
                // details panel, so the last row is never covered by the bar.
                ScrollBar.horizontal: AppScrollBar {
                    id: horizontalScrollBar
                    parent: tableFrame
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.bottom: parent.bottom
                    anchors.margins: 1
                    height: implicitHeight
                    policy: ScrollBar.AsNeeded
                    active: true
                }

                WheelHandler {
                    orientation: Qt.Horizontal
                    acceptedDevices: PointerDevice.Mouse | PointerDevice.TouchPad
                    target: null
                    onWheel: function(event) {
                        const distance = event.pixelDelta.x !== 0 ? event.pixelDelta.x
                            : event.angleDelta.x / 120 * root.rowHeight * 3;
                        const vertical = event.pixelDelta.y !== 0 ? event.pixelDelta.y
                            : event.angleDelta.y / 120 * root.rowHeight * 3;
                        root.scrollHorizontally(-distance, -vertical);
                    }
                }

                delegate: Rectangle {
                    id: row
                    required property var modelData
                    required property int index
                    width: root.totalWidth
                    height: root.rowHeight
                    color: root.currentIndex === index ? root.palette.highlight : root.palette.base

                    MouseArea {
                        anchors.fill: parent
                        onClicked: {
                            list.forceActiveFocus();
                            root.selectRow(row.index);
                        }
                        onDoubleClicked: root.activated(String(row.modelData.id))
                    }

                    Row {
                        anchors.fill: parent

                        Repeater {
                            model: root.columns

                            delegate: Item {
                                id: cell
                                required property var modelData
                                required property int index
                                width: root.visibleWidth(index)
                                height: row.height
                                visible: width > 0

                                // Draw inside each cell so scrolling and column
                                // resizing keep both dividers aligned with text.
                                Rectangle {
                                    anchors.left: parent.left
                                    anchors.right: parent.right
                                    anchors.bottom: parent.bottom
                                    height: 1
                                    color: root.gridColour
                                    visible: root.gridLines
                                }

                                Rectangle {
                                    anchors.top: parent.top
                                    anchors.bottom: parent.bottom
                                    anchors.right: parent.right
                                    width: 1
                                    color: root.gridColour
                                    visible: root.gridLines
                                }

                                Label {
                                    anchors.fill: parent
                                    anchors.margins: metrics.spacingSmall
                                    visible: !cell.modelData.checkable && !cell.modelData.custom
                                    text: String(row.modelData[cell.modelData.key] ?? "")
                                    textFormat: Text.PlainText
                                    elide: Text.ElideMiddle
                                    verticalAlignment: Text.AlignVCenter
                                    color: root.currentIndex === row.index
                                        ? root.palette.highlightedText : root.palette.text
                                    ToolTip.visible: hover.hovered
                                    ToolTip.text: String(row.modelData[cell.modelData.detailKey || cell.modelData.key] ?? "")
                                    HoverHandler { id: hover }
                                }

                                CheckBox {
                                    anchors.centerIn: parent
                                    visible: cell.modelData.checkable === true
                                    checked: row.modelData[cell.modelData.key] === true
                                    Accessible.name: cell.modelData.label + " " + String(row.modelData.current || row.modelData.id)
                                    onClicked: {
                                        root.selectRow(row.index);
                                        root.chosen(String(row.modelData.id), checked);
                                    }
                                }

                                Loader {
                                    anchors.fill: parent
                                    anchors.margins: metrics.spacingSmall
                                    active: cell.modelData.custom === true && root.cellDelegate !== null
                                    sourceComponent: root.cellDelegate
                                    property var rowData: row.modelData
                                    property var column: cell.modelData
                                    property int columnIndex: cell.index
                                    property bool selected: root.currentIndex === row.index
                                }
                            }
                        }
                    }
                }
            }
        }

        AppScrollView {
            Layout.fillWidth: true
            Layout.preferredHeight: metrics.diagnosticHeight
            Layout.minimumHeight: metrics.diagnosticHeight / 2
            Layout.maximumHeight: metrics.diagnosticHeight
            visible: root.showDetails
            clip: true
            contentWidth: availableWidth

            TextArea {
                id: fullDetails
                text: root.details
                placeholderText: "Select a row to read and copy its full details."
                readOnly: true
                selectByMouse: true
                wrapMode: TextEdit.WrapAnywhere
                textFormat: TextEdit.PlainText
            }
        }
    }

    Menu {
        id: columnMenu

        Instantiator {
            model: root.columns

            delegate: MenuItem {
                required property var modelData
                required property int index
                text: modelData.label
                checkable: true
                checked: root.hiddenColumns.indexOf(index) < 0
                enabled: index !== root.protectedColumn

                onTriggered: {
                    let hidden = root.hiddenColumns.slice();

                    if (checked) {
                        hidden = hidden.filter(function(column) { return column !== index; });
                    } else if (hidden.indexOf(index) < 0) {
                        hidden.push(index);
                    }

                    root.hiddenColumns = hidden;
                    root.saveColumns();
                }
            }

            onObjectAdded: function(index, object) { columnMenu.insertItem(index, object); }
            onObjectRemoved: function(index, object) { columnMenu.removeItem(object); }
        }
    }
}
