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
    // These lists contain model column indices. Moving their visual positions
    // must not change persisted widths or the field identified by a command.
    property var columnOrder: []
    property var stretchColumns: []
    property var appliedColumnOrder: []
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
    // Sorting is opt-in: metadata fields keep their deliberate review order.
    // These indices are logical model columns, just like persisted widths.
    property bool sortable: false
    property int sortColumn: -1
    property bool sortDescending: false
    property var defaultSortColumns: []
    property string defaultSortLabel: ""
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
    signal sortRequested(int column, bool descending)
    signal restoreDefaultSortRequested()

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
    onColumnOrderChanged: Qt.callLater(applyColumnOrder)
    onStretchColumnsChanged: { if (table) table.forceLayout(); }
    onWidthChanged: { if (table && (stretchLastColumn || stretchColumns.length)) table.forceLayout(); }
    onRowHeightChanged: { if (table) table.forceLayout(); }

    Component.onCompleted: Qt.callLater(applyColumnOrder)

    function applyColumnOrder() {
        if (!table || table.columns === 0) {
            return;
        }

        let order = [];

        // Ignore invalid or duplicated entries and append omitted columns.
        // This also lets a table restore optional detail columns later without
        // changing which logical columns its header menu and callbacks mean.
        for (let column of columnOrder) {
            if (Number.isInteger(column) && column >= 0 && column < table.columns
                    && order.indexOf(column) < 0) {
                order.push(column);
            }
        }

        for (let column = 0; column < table.columns; column++) {
            if (order.indexOf(column) < 0) {
                order.push(column);
            }
        }

        if (JSON.stringify(order) === JSON.stringify(appliedColumnOrder)) {
            return;
        }

        table.clearColumnReordering();
        let current = Array.from({length: table.columns}, function(_, column) { return column; });

        for (let destination = 0; destination < order.length; destination++) {
            const source = current.indexOf(order[destination]);

            if (source !== destination) {
                table.moveColumn(source, destination);
                current.splice(destination, 0, current.splice(source, 1)[0]);
            }
        }

        appliedColumnOrder = order;
        table.forceLayout();
    }

    function requestSort(column) {
        if (!sortable) {
            return;
        }

        sortRequested(column, column === sortColumn ? !sortDescending : false);
        root.forceActiveFocus();
        Qt.callLater(revealCurrentRow);
    }

    function preferredColumnWidth(column) {
        const explicit = table.explicitColumnWidth(visualColumn(column));
        return explicit >= 36 ? explicit : (columnWidths[column] || 160);
    }

    function logicalColumn(visual) {
        return visual < appliedColumnOrder.length ? appliedColumnOrder[visual] : visual;
    }

    function visualColumn(logical) {
        const visual = appliedColumnOrder.indexOf(logical);
        return visual >= 0 ? visual : logical;
    }

    function stretchedValueWidth(column) {
        const explicit = table.explicitColumnWidth(visualColumn(column));

        if (explicit >= 36) {
            return explicit;
        }

        let preferredTotal = 0;
        let flexible = [];

        for (let other = 0; other < columnTitles.length; other++) {
            if (hiddenColumns.indexOf(other) >= 0) {
                continue;
            }

            preferredTotal += preferredColumnWidth(other);

            if (stretchColumns.indexOf(other) >= 0 && table.explicitColumnWidth(visualColumn(other)) < 36) {
                flexible.push(other);
            }
        }

        // Reserve the compact fixed columns first, then share spare space
        // between values. User-resized columns retain their exact widths;
        // narrower windows keep the readable defaults and scroll horizontally.
        const spare = Math.max(0, table.width - preferredTotal);
        const position = flexible.indexOf(column);
        const share = position < 0 ? 0
            : Math.floor(spare * (position + 1) / flexible.length)
              - Math.floor(spare * position / flexible.length);
        return preferredColumnWidth(column) + share;
    }

    function storedWidths() {
        let widths = [];

        for (let column = 0; column < columnTitles.length; column++) {
            let resized = table.explicitColumnWidth(visualColumn(column));
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
                id: headerCell
                required property var display
                required property int column
                readonly property int defaultPriority: root.defaultSortColumns.indexOf(column)
                readonly property bool sorted: root.sortable && (column === root.sortColumn
                    || (root.sortColumn < 0 && defaultPriority >= 0))
                objectName: "sortHeader" + column

                implicitHeight: root.headerHeight
                color: palette.button
                border.color: root.gridColour
                activeFocusOnTab: root.sortable
                Accessible.role: root.sortable ? Accessible.Button : Accessible.ColumnHeader
                Accessible.name: display + (sorted
                    ? (root.sortDescending ? ", descending" : ", ascending")
                      + (root.sortColumn < 0 ? ", priority " + (defaultPriority + 1) : "") : "")
                Accessible.description: root.sortable ? "Activate to sort by this column" : ""
                Accessible.onPressAction: root.requestSort(column)
                Keys.onSpacePressed: root.requestSort(column)
                Keys.onReturnPressed: root.requestSort(column)

                // Passive tap handling leaves the header's existing drag
                // gesture available for column resizing. A drag never sorts.
                TapHandler {
                    enabled: root.sortable
                    acceptedButtons: Qt.LeftButton
                    gesturePolicy: TapHandler.DragThreshold
                    onTapped: root.requestSort(headerCell.column)
                }

                HoverHandler { id: headerHover }
                ToolTip.visible: root.sortable && headerHover.hovered
                ToolTip.delay: 900
                ToolTip.text: "Sort by " + display + (sorted && root.sortColumn < 0
                    ? " · " + root.defaultSortLabel : "")

                Label {
                    anchors.fill: parent
                    anchors.leftMargin: 8
                    anchors.rightMargin: headerCell.sorted ? sortIndicator.width + 12 : 8
                    text: headerCell.display
                    textFormat: Text.PlainText
                    verticalAlignment: Text.AlignVCenter
                    horizontalAlignment: root.headerAlignment
                    font.bold: root.headerBold
                    elide: Text.ElideRight
                }

                Label {
                    id: sortIndicator
                    anchors.right: parent.right
                    anchors.rightMargin: 5
                    anchors.verticalCenter: parent.verticalCenter
                    visible: headerCell.sorted
                    text: (root.sortDescending ? "↓" : "↑")
                        + (root.sortColumn < 0 ? headerCell.defaultPriority + 1 : "")
                    font.pixelSize: Math.max(9, textMetrics.height * 0.8)
                }

                Rectangle {
                    anchors.fill: parent
                    visible: headerCell.activeFocus
                    color: "transparent"
                    border.color: palette.highlight
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

            columnWidthProvider: function(visual) {
                // Qt's sizing APIs use visual positions, while delegate roles
                // still expose model indices. Convert here so the compact
                // Status column cannot inherit a metadata value's saved width.
                const column = root.logicalColumn(visual);

                if (root.hiddenColumns.indexOf(column) >= 0) {
                    return 0;
                }

                if (root.stretchColumns.indexOf(column) >= 0) {
                    return root.stretchedValueWidth(column);
                }

                let lastVisual = root.columnTitles.length - 1;

                while (lastVisual >= 0 && root.hiddenColumns.indexOf(root.logicalColumn(lastVisual)) >= 0) {
                    lastVisual--;
                }

                if (root.stretchLastColumn && visual === lastVisual) {
                    let previousWidth = 0;

                    for (let position = 0; position < lastVisual; position++) {
                        const other = root.logicalColumn(position);

                        if (root.hiddenColumns.indexOf(other) < 0) {
                            previousWidth += root.preferredColumnWidth(other);
                        }
                    }

                    // Stretch supplies a minimum useful width, but it must
                    // not discard a user's explicit resize when Qt evaluates
                    // the provider again after a model or layout update.
                    const preferred = root.preferredColumnWidth(column);
                    return Math.max(preferred, table.width - previousWidth);
                }

                let resized = table.explicitColumnWidth(visual);
                if (resized >= 36) {
                    return resized;
                }

                return root.columnWidths[column] || 160;
            }

            rowHeightProvider: function(row) {
                return root.rowHeight;
            }

            onRowsChanged: Qt.callLater(root.revealCurrentRow)
            onColumnsChanged: Qt.callLater(root.applyColumnOrder)

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
                required property var model
                // Optional roles keep the generic table compatible with small
                // standalone models while the application supplies richer
                // explanations and semantic empty-value formatting.
                readonly property bool placeholder: Boolean(model.placeholder)
                readonly property string tooltip: model.tooltip === undefined ? display : model.tooltip

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
                    color: cell.highlighted ? palette.highlightedText
                           : cell.placeholder ? palette.placeholderText : palette.text
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

                    visible: cellMouse.containsMouse && cell.tooltip.length > 0
                    delay: 900
                    width: Math.min(360, implicitWidth)
                    text: cell.tooltip.length > 300
                          ? cell.tooltip.slice(0, 300) + "…"
                          : cell.tooltip

                    // ToolTip is a popup; wrapping belongs to its text item.
                    contentItem: Label {
                        text: valueTip.text
                        textFormat: Text.PlainText
                        wrapMode: Text.WrapAnywhere
                        // Embedded newlines can make even a short string tall.
                        // Full values remain available in the details surface.
                        maximumLineCount: 12
                        elide: Text.ElideRight
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

        MenuItem {
            objectName: "restoreDefaultSortAction"
            text: root.defaultSortLabel
            visible: root.sortable && root.defaultSortLabel.length > 0
            height: visible ? implicitHeight : 0
            onTriggered: {
                root.restoreDefaultSortRequested();
                root.forceActiveFocus();
                Qt.callLater(root.revealCurrentRow);
            }
        }

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

            onObjectAdded: function(index, object) { headerMenu.insertItem(index + 1, object); }
            onObjectRemoved: function(index, object) { headerMenu.removeItem(object); }
        }
    }
}
