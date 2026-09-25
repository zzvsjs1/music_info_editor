import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Templates as T
import QtQuick.Window

// A table projects backend state. Recycled cells never own selection or inclusion.
Control {
    id: root

    property alias model: table.model
    property var columnWidths: []
    property var columnTitles: model ? model.columnTitles : []
    property var hiddenColumns: []
    property int protectedColumn: -1
    property bool compactRows: false
    property int rowHeight: Math.max(compactRows ? 16 : 29,
        Math.ceil(textMetrics.height) + (compactRows ? 2 : 8))
    property int headerHeight: Math.max(compactRows ? 20 : 22,
        Math.ceil(textMetrics.height) + 6)
    property bool gridLines: true
    property bool headerBold: false
    property int headerAlignment: Qt.AlignHCenter
    property bool stretchLastColumn: false
    property bool inclusionColumn: false
    property string emptyText: "No rows to display."
    property int currentRow: -1
    // Reserve the style's scrollbar thickness even before a model starts to
    // overflow. Changing the viewport height while dragging an AsNeeded bar
    // can otherwise make the footer and last table row jump against each other.
    readonly property real horizontalScrollBarHeight: horizontalScrollBar.implicitHeight
    readonly property real minimumTableHeight: headerHeight + rowHeight + horizontalScrollBarHeight + 2
    readonly property real deviceScale: Screen.devicePixelRatio || 1

    // The native Windows palette can make midlight identical to the base.
    // Blend the text colour instead so the legacy grid remains visible in
    // both light and dark palettes, without fixing it to a particular grey.
    readonly property color gridColour: Qt.tint(palette.base,
        Qt.rgba(palette.text.r, palette.text.g, palette.text.b, 0.15))

    signal rowSelected(string stableId, bool toggle, bool extend)
    signal cellActivated(string stableId, string value, int column)
    signal contextRequested(string stableId, bool highlighted, real x, real y)
    signal valueRequested(string value)
    signal inclusionRequested(string stableId, bool included)
    signal moveRequested(int delta)
    signal moveExtendedRequested(int delta, bool extend)
    signal toggleInclusionRequested()
    signal selectAllRequested()
    signal clearSelectionRequested()
    signal activateRequested()
    signal editRequested()

    activeFocusOnTab: true
    focusPolicy: Qt.StrongFocus

    FontMetrics {
        id: textMetrics
        font: root.font
    }

    function devicePixelLength(logicalLength) {
        return Math.round(logicalLength * deviceScale) / deviceScale;
    }

    function scaledWidths(values) {
        return values.map(function(value) { return Math.round(value * textMetrics.advanceWidth("M") / 10); });
    }

    // Row numbers are used only to position the viewport. Commands use stable IDs.
    function revealCurrentRow() {
        if (currentRow >= 0 && currentRow < table.rows) {
            table.positionViewAtRow(currentRow, TableView.Contain);
        }
    }

    onCurrentRowChanged: Qt.callLater(revealCurrentRow)
    onHiddenColumnsChanged: { if (table) table.forceLayout(); }
    onColumnWidthsChanged: { if (table) table.forceLayout(); }
    onWidthChanged: { if (table && stretchLastColumn) table.forceLayout(); }
    onRowHeightChanged: { if (table) table.forceLayout(); }

    function storedWidths() {
        let widths = [];

        for (let column = 0; column < columnTitles.length; column++) {
            let resized = table.explicitColumnWidth(column);
            widths.push(resized >= 36 ? resized : (columnWidths[column] || 160));
        }

        return widths;
    }

    function resetColumns(widths, hidden) {
        table.clearColumnWidths();
        columnWidths = widths;
        hiddenColumns = hidden;
        table.forceLayout();
    }

    function scrollHorizontally(delta, verticalDelta) {
        // Cancel the old flick before changing axes: otherwise a horizontal
        // wheel event during vertical deceleration can be silently discarded.
        table.cancelFlick();
        const first = table.originX;
        const last = first + Math.max(0, table.contentWidth - table.width);
        table.contentX = Math.max(first, Math.min(last, table.contentX + delta));

        // One touchpad event may carry both axes. Once this handler accepts
        // it, retain the accompanying vertical distance rather than dropping
        // half of a diagonal gesture. Keyboard calls supply no vertical part.
        if (verticalDelta) {
            const top = table.originY;
            const bottom = top + Math.max(0, table.contentHeight - table.height);
            table.contentY = Math.max(top, Math.min(bottom, table.contentY + verticalDelta));
        }
    }

    Keys.onPressed: function(event) {
        const control = Boolean(event.modifiers & Qt.ControlModifier);
        const extend = Boolean(event.modifiers & Qt.ShiftModifier);
        const page = Math.max(1, Math.floor(table.height / root.rowHeight));
        let delta = null;

        // Keep row movement in the existing stable-ID backend commands. The
        // viewport supplies only a distance, and Shift retains the same anchor
        // used for pointer selection; write-batch membership is unaffected.
        if (event.key === Qt.Key_Up || event.key === Qt.Key_Down) {
            delta = event.key === Qt.Key_Up ? -1 : 1;
        } else if (event.key === Qt.Key_PageUp || event.key === Qt.Key_PageDown) {
            delta = event.key === Qt.Key_PageUp ? -page : page;
        } else if (control && event.key === Qt.Key_Home) {
            delta = -root.currentRow;
        } else if (control && event.key === Qt.Key_End) {
            delta = table.rows - 1 - root.currentRow;
        }

        if (delta !== null) {
            root.moveRequested(delta);
            root.moveExtendedRequested(delta, extend);
            Qt.callLater(root.revealCurrentRow);
            event.accepted = true;
        } else if (event.key === Qt.Key_Left || event.key === Qt.Key_Right) {
            root.scrollHorizontally((event.key === Qt.Key_Left ? -1 : 1) * root.rowHeight * 3);
            event.accepted = true;
        } else if (event.key === Qt.Key_Home || event.key === Qt.Key_End) {
            // A plain boundary key follows the current row across its columns;
            // Ctrl supplies the separate first/last-row operation above.
            root.scrollHorizontally(event.key === Qt.Key_Home ? -table.contentWidth : table.contentWidth);
            event.accepted = true;
        } else if (event.key === Qt.Key_Space && root.inclusionColumn) {
            root.toggleInclusionRequested();
            event.accepted = true;
        } else if (event.key === Qt.Key_A && (event.modifiers & Qt.ControlModifier)) {
            if (event.modifiers & Qt.ShiftModifier) {
                root.clearSelectionRequested();
            } else {
                root.selectAllRequested();
            }
            event.accepted = true;
        } else if (event.key === Qt.Key_F2 && event.modifiers === Qt.NoModifier) {
            root.editRequested();
            event.accepted = true;
        } else if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter) {
            root.activateRequested();
            event.accepted = true;
        }
    }

    Rectangle {
        anchors.fill: parent
        color: palette.window
        border.color: root.activeFocus ? palette.highlight : palette.mid
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 1
        spacing: 0

        HorizontalHeaderView {
            id: header

            Layout.fillWidth: true
            Layout.preferredHeight: root.headerHeight
            syncView: table
            clip: true
            interactive: false
            resizableColumns: true

            // Attach to the viewport itself, not a delegate or the moving
            // content item. Empty space must recover even an all-hidden table.
            MouseArea {
                parent: header
                anchors.fill: parent
                acceptedButtons: Qt.RightButton
                onClicked: headerMenu.popup()
            }

            delegate: Rectangle {
                required property var display

                implicitHeight: root.headerHeight
                color: palette.button
                border.color: root.gridColour

                Label {
                    anchors.fill: parent
                    anchors.leftMargin: 8
                    anchors.rightMargin: 8
                    text: parent.display
                    textFormat: Text.PlainText
                    verticalAlignment: Text.AlignVCenter
                    horizontalAlignment: root.headerAlignment
                    font.bold: root.headerBold
                    elide: Text.ElideRight
                }

            }
        }

        TableView {
            id: table

            Layout.fillWidth: true
            Layout.fillHeight: true
            clip: true
            boundsBehavior: Flickable.StopAtBounds
            reuseItems: true
            pointerNavigationEnabled: false
            keyNavigationEnabled: false
            columnSpacing: 0
            rowSpacing: 0

            // Keep the readable viewport white while the frame and scrollbar
            // gutters use the window surface. An explicit parent prevents the
            // Flickable from moving this background with recycled rows.
            Rectangle {
                parent: table
                anchors.fill: parent
                z: -1
                color: root.palette.base
            }

            columnWidthProvider: function(column) {
                if (root.hiddenColumns.indexOf(column) >= 0) {
                    return 0;
                }

                let last = root.columnTitles.length - 1;

                while (last >= 0 && root.hiddenColumns.indexOf(last) >= 0) {
                    last--;
                }

                if (root.stretchLastColumn && column === last) {
                    let previousWidth = 0;

                    for (let other = 0; other < last; other++) {
                        if (root.hiddenColumns.indexOf(other) < 0) {
                            let explicit = table.explicitColumnWidth(other);
                            previousWidth += explicit >= 36 ? explicit : (root.columnWidths[other] || 160);
                        }
                    }

                    // Stretch supplies a minimum useful width, but it must
                    // not discard a user's explicit resize when Qt evaluates
                    // the provider again after a model or layout update.
                    const explicit = table.explicitColumnWidth(column);
                    const preferred = explicit >= 36 ? explicit : (root.columnWidths[column] || 160);
                    return Math.max(preferred, table.width - previousWidth);
                }

                let resized = table.explicitColumnWidth(column);
                if (resized >= 36) {
                    return resized;
                }

                return root.columnWidths[column] || 160;
            }

            rowHeightProvider: function(row) {
                return root.rowHeight;
            }

            onRowsChanged: Qt.callLater(root.revealCurrentRow)

            // The attached bar still follows TableView's content position,
            // while its visual parent is the dedicated gutter below the view.
            // Qt's style supplies the gutter's thickness.
            ScrollBar.horizontal: AppScrollBar {
                id: horizontalScrollBar
                parent: horizontalScrollGutter
                anchors.fill: parent
                policy: ScrollBar.AsNeeded
                active: true
            }
            ScrollBar.vertical: AppScrollBar { policy: ScrollBar.AsNeeded; active: true }

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
                id: cell

                required property int row
                required property int column
                required property string display
                required property string stableId
                required property bool highlighted
                required property bool included

                implicitWidth: 160
                implicitHeight: root.rowHeight
                color: highlighted ? palette.highlight : palette.base

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
                    anchors.leftMargin: 8
                    anchors.rightMargin: 8
                    visible: !(root.inclusionColumn && cell.column === 0)
                    text: cell.display
                    textFormat: Text.PlainText
                    color: cell.highlighted ? palette.highlightedText : palette.text
                    elide: Text.ElideRight
                    verticalAlignment: Text.AlignVCenter
                }

                MouseArea {
                    id: cellMouse

                    anchors.fill: parent
                    acceptedButtons: Qt.LeftButton | Qt.RightButton
                    hoverEnabled: true

                    onClicked: function(mouse) {
                        root.forceActiveFocus();

                        if (mouse.button === Qt.RightButton) {
                            let point = cellMouse.mapToItem(root, mouse.x, mouse.y);
                            root.contextRequested(cell.stableId, cell.highlighted, point.x, point.y);
                        } else {
                            root.rowSelected(cell.stableId, Boolean(mouse.modifiers & Qt.ControlModifier),
                                             Boolean(mouse.modifiers & Qt.ShiftModifier));
                        }
                    }

                    onDoubleClicked: function(mouse) {
                        if (mouse.button === Qt.LeftButton) {
                            root.cellActivated(cell.stableId, cell.display, cell.column);
                        }
                    }
                }

                // The Windows native indicator is 13 logical pixels wide. At
                // fractional display scale that becomes a fractional bitmap
                // size and its border can break. This template keeps the same
                // checkbox behaviour but draws a palette-aware vector square
                // with size and border rounded to whole device pixels.
                T.CheckBox {
                    id: inclusionToggle
                    implicitWidth: root.devicePixelLength(24)
                    implicitHeight: root.devicePixelLength(24)
                    anchors.centerIn: parent
                    visible: root.inclusionColumn && cell.column === 0
                    checked: cell.included
                    focusPolicy: Qt.NoFocus
                    Accessible.name: "Include this file"

                    indicator: Rectangle {
                        implicitWidth: root.devicePixelLength(18)
                        implicitHeight: implicitWidth
                        x: root.devicePixelLength((inclusionToggle.width - width) / 2)
                        y: root.devicePixelLength((inclusionToggle.height - height) / 2)
                        radius: root.devicePixelLength(2)
                        color: inclusionToggle.checked
                            ? inclusionToggle.palette.highlight : inclusionToggle.palette.base
                        // Fractional display scales can thin a one-device-pixel
                        // stroke unevenly. Use the nearest whole-device-pixel
                        // stroke at least one logical pixel wide on each screen.
                        border.width: Math.ceil(root.deviceScale) / root.deviceScale
                        border.color: inclusionToggle.checked || inclusionToggle.hovered
                            ? inclusionToggle.palette.highlight : inclusionToggle.palette.text
                        opacity: inclusionToggle.enabled ? 1 : 0.55

                        Text {
                            anchors.centerIn: parent
                            text: "\u2713"
                            color: inclusionToggle.palette.highlightedText
                            font.pixelSize: parent.height - root.devicePixelLength(3)
                            font.bold: true
                            visible: inclusionToggle.checked
                        }
                    }

                    // Reading the current role avoids retaining a toggled value in
                    // a delegate that TableView may reuse for another file later.
                    nextCheckState: function() {
                        return cell.included ? Qt.Unchecked : Qt.Checked;
                    }

                    onClicked: {
                        root.forceActiveFocus();
                        root.inclusionRequested(cell.stableId, !cell.included);
                    }
                }

                ToolTip {
                    id: valueTip

                    visible: cellMouse.containsMouse && cell.display.length > 0
                    delay: 900
                    width: Math.min(360, implicitWidth)
                    text: cell.display.length > 300
                          ? cell.display.slice(0, 300) + "…"
                          : cell.display

                    // ToolTip is a popup; wrapping belongs to its text item.
                    contentItem: Label {
                        text: valueTip.text
                        textFormat: Text.PlainText
                        wrapMode: Text.WrapAnywhere
                    }
                }
            }

            Label {
                // Flickable normally reparents children into its moving content
                // item, which has no height when empty. Anchor this hint to the
                // control's viewport so the header cannot clip it away.
                parent: root
                anchors.centerIn: parent
                width: Math.max(0, parent.width - 32)
                visible: table.rows === 0
                text: root.emptyText
                wrapMode: Text.WordWrap
                horizontalAlignment: Text.AlignHCenter
                color: palette.placeholderText
            }
        }

        Rectangle {
            id: horizontalScrollGutter
            Layout.fillWidth: true
            Layout.preferredHeight: root.horizontalScrollBarHeight
            Layout.minimumHeight: root.horizontalScrollBarHeight
            Layout.maximumHeight: root.horizontalScrollBarHeight
            color: horizontalScrollBar.size < 1 ? root.palette.window : root.palette.base
            clip: true
        }
    }

    Menu {
        id: headerMenu

        Instantiator {
            model: root.columnTitles

            delegate: MenuItem {
                required property int index
                required property string modelData

                text: modelData
                checkable: true
                checked: root.hiddenColumns.indexOf(index) < 0
                enabled: index !== root.protectedColumn
                onTriggered: {
                    let hidden = root.hiddenColumns.slice();
                    let position = hidden.indexOf(index);

                    if (position >= 0) {
                        hidden.splice(position, 1);
                    } else {
                        hidden.push(index);
                    }

                    root.hiddenColumns = hidden;
                }
            }

            onObjectAdded: function(index, object) { headerMenu.insertItem(index, object); }
            onObjectRemoved: function(index, object) { headerMenu.removeItem(object); }
        }
    }
}
