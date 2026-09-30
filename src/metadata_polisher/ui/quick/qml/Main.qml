import QtQuick
import QtQuick.Controls
import QtQuick.Dialogs
import QtQuick.Layouts
import QtQuick.Window

ApplicationWindow {
    id: window
    required property QtObject backend
    readonly property bool interactionEnabled: !backend.busy && !backend.editing
                                               && !discardDialog.visible && !backend.confirmationVisible
                                               && !backend.libraryUi.discVisible
    readonly property bool compactHeight: height < metrics.mainCompactHeight
    property bool discardCloseConfirmed: false
    property string pendingScanPath: ""
    objectName: "quickWindow"
    visible: true
    width: metrics.mainWidth
    height: metrics.mainHeight
    minimumWidth: Math.min(840, Screen.desktopAvailableWidth - metrics.screenHorizontalMargin)
    minimumHeight: Math.min(580, Screen.desktopAvailableHeight - metrics.screenVerticalMargin)
    title: "Metadata Polisher"
    color: palette.window

    UiMetrics {
        id: metrics
    }

    Component.onCompleted: {
        width = Math.min(metrics.mainWidth, Screen.desktopAvailableWidth - metrics.screenHorizontalMargin);
        height = Math.min(metrics.mainHeight, Screen.desktopAvailableHeight - metrics.screenVerticalMargin);
        backend.layoutUi.watchWindow(window, "MainWindow");
        albumsPane.SplitView.preferredWidth = backend.layoutUi.albumWidth;
        groupTable.resetColumns(backend.layoutUi.columnWidths("MainWindow/groupView", groupTable.scaledWidths(metrics.albumColumns)),
                                backend.layoutUi.hiddenColumns("MainWindow/groupView", []));
        fileTable.resetColumns(backend.layoutUi.columnWidths("MainWindow/fileTableView", fileTable.scaledWidths(metrics.fileColumns)),
                               backend.layoutUi.hiddenColumns("MainWindow/fileTableView", metrics.hiddenFileColumns));
    }

    function captureLayout() {
        backend.layoutUi.captureWindow(window, "MainWindow");
        backend.layoutUi.setAlbumWidth(Math.round(albumsPane.width));
        backend.layoutUi.saveColumns("MainWindow/groupView", groupTable.storedWidths(), groupTable.hiddenColumns);
        backend.layoutUi.saveColumns("MainWindow/fileTableView", fileTable.storedWidths(), fileTable.hiddenColumns);
        reviewWindow.captureLayout();
    }

    function openHelp() {
        helpWindow.show();
        helpWindow.raise();
        helpWindow.requestActivate();
    }

    function requestScan() {
        pendingScanPath = folderPath.text;

        if (backend.hasPendingWork) {
            discardDialog.purpose = "scan";
            discardDialog.show();
        } else {
            backend.scan(pendingScanPath, false);
        }
    }

    function openSelectedReview() {
        if (backend.selectedFileIds.length) {
            backend.setReviewScope("selected");
            backend.openReview();
        }
    }

    function resetLayout() {
        backend.layoutUi.reset();
        width = Math.min(metrics.mainWidth, Screen.desktopAvailableWidth - metrics.screenHorizontalMargin);
        height = Math.min(metrics.mainHeight, Screen.desktopAvailableHeight - metrics.screenVerticalMargin);
        albumsPane.SplitView.preferredWidth = metrics.albumPaneWidth;
        groupTable.resetColumns(groupTable.scaledWidths(metrics.albumColumns), []);
        fileTable.resetColumns(fileTable.scaledWidths(metrics.fileColumns), metrics.hiddenFileColumns);
        reviewWindow.resetLayout();
    }

    onClosing: function(event) {
        if (discardCloseConfirmed) {
            captureLayout();
            backend.closeReview();
            return;
        }

        if (backend.busy) {
            event.accepted = false;
            backend.cancelScan();
        } else if (backend.hasPendingWork || backend.editing) {
            event.accepted = false;
            discardDialog.purpose = "close";
            discardDialog.show();
        } else {
            captureLayout();
            backend.closeReview();
        }
    }

    Shortcut {
        sequence: "Ctrl+O"
        enabled: window.active && window.interactionEnabled
        onActivated: folderDialog.open()
    }

    Shortcut {
        sequence: "F5"
        enabled: window.active && window.interactionEnabled
        onActivated: window.requestScan()
    }

    Shortcut {
        sequence: "Ctrl+E"
        enabled: window.active && window.interactionEnabled
        onActivated: backend.openReview()
    }

    Shortcut {
        sequence: "F1"
        enabled: window.active && window.interactionEnabled
        onActivated: window.openHelp()
    }

    Shortcut {
        sequence: "Ctrl+L"
        enabled: window.active && window.interactionEnabled && backend.lookupUi.canFind
        onActivated: backend.lookupUi.findSelected()
    }

    Shortcut {
        sequences: ["Ctrl+Return", "Ctrl+Enter"]
        enabled: window.active && window.interactionEnabled && backend.applyUi.canApply
        onActivated: backend.applyUi.beginApply()
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: window.compactHeight ? metrics.spacingSmall : 9
        // At minimum height, decorative margins and section gaps give space to
        // readable controls. Action groups keep their full shared spacing; the
        // diagnostic viewport and complete first table row remain available.
        spacing: window.compactHeight ? metrics.spacingCompact : metrics.spacing
        RowLayout {
            spacing: metrics.controlSpacing
            Layout.fillWidth: true
            AppTextField {
                id: folderPath
                objectName: "folderPath"
                Layout.fillWidth: true

                text: backend.rootPath
                enabled: window.interactionEnabled
                placeholderText: "Choose a music library folder"
                selectByMouse: true
                Accessible.name: "Music folder"
                onAccepted: window.requestScan()
            }

            ActionButton {
                objectName: "browseButton"
                text: "Browse"
                enabled: window.interactionEnabled
                onClicked: folderDialog.open()
            }

            ActionButton {
                objectName: "scanButton"
                text: "Rescan"
                enabled: window.interactionEnabled
                onClicked: window.requestScan()
            }
        }

        // Wrapping actions retain their full labels when larger desktop fonts
        // no longer fit a single row. The workspace receives the remaining height.
        Flow {
            Layout.fillWidth: true
            spacing: metrics.controlSpacing
            ActionButton {
                objectName: "findSelectedButton"
                text: "Find Metadata for Selected"
                enabled: window.interactionEnabled && backend.lookupUi.canFind
                onClicked: backend.lookupUi.findSelected()
            }

            ActionButton {
                objectName: "findAllIncompleteButton"
                text: "Find All Incomplete"
                enabled: window.interactionEnabled && backend.lookupUi.canFindAll
                onClicked: backend.lookupUi.findAll()
            }

            ActionButton {
                objectName: "groupToolsButton"
                text: "Group tools"
                onClicked: groupMenu.popup()
            }

            RowLayout {
                spacing: metrics.controlSpacing
                Label {
                    text: "Language:"
                }

                AppComboBox {
                    objectName: "languageCombo"
                    Layout.preferredWidth: Math.max(125, implicitWidth)
                    model: backend.lookupUi.languageChoices
                    textRole: "label"
                    valueRole: "value"
                    currentIndex: indexOfValue(backend.lookupUi.language)
                    enabled: window.interactionEnabled && backend.groupId.length > 0
                    Accessible.name: "Metadata language preference"
                    onActivated: backend.lookupUi.setLanguage(currentValue)
                }
            }

            ActionButton {
                objectName: "settingsButton"
                text: "Settings"
                enabled: window.interactionEnabled
                onClicked: backend.settingsUi.open()
            }

            ActionButton {
                objectName: "diagnosticsButton"
                text: "Diagnostics…"
                onClicked: { diagnosticsWindow.show(); diagnosticsWindow.raise(); diagnosticsWindow.requestActivate(); }
            }
        }

        Label {
            Layout.fillWidth: true
            text: backend.providerLabel
            textFormat: Text.PlainText
            wrapMode: Text.WordWrap
        }

        SplitView {
            id: mainSplitter
            objectName: "mainSplitter"
            Layout.fillWidth: true
            Layout.fillHeight: true
            orientation: Qt.Horizontal
            handle: Rectangle { implicitWidth: 7; color: SplitHandle.hovered ? palette.mid : palette.midlight }
            ColumnLayout {
                id: albumsPane
                objectName: "albumsPane"
                SplitView.preferredWidth: metrics.albumPaneWidth
                SplitView.minimumWidth: 150
                spacing: window.compactHeight ? metrics.spacingCompact : metrics.spacing
                Label {
                    text: "Albums / groups"
                    Layout.leftMargin: 9
                    Layout.topMargin: window.compactHeight ? metrics.spacingCompact : 9
                }

                DataTable {
                    id: groupTable
                    objectName: "groupTable"
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    Layout.leftMargin: 9
                    Layout.rightMargin: 9
                    Layout.bottomMargin: window.compactHeight ? metrics.spacingCompact : 9
                    model: backend.albumModel
                    sortable: backend.albumModel.sortingEnabled
                    sortColumn: backend.albumModel.sortColumnIndex
                    sortDescending: backend.albumModel.sortDescending
                    onSortRequested: function(column, descending) {
                        backend.albumModel.sortByColumn(column, descending);
                    }
                    enabled: window.interactionEnabled
                    currentRow: backend.currentGroupRow
                    columnWidths: scaledWidths(metrics.albumColumns)
                    compactRows: true
                    gridLines: false
                    headerBold: false
                    headerAlignment: Qt.AlignLeft
                    emptyText: ""
                    onRowSelected: function(id, toggle, extend) { backend.selectGroupExtended(id, toggle, extend); }
                    onMoveExtendedRequested: function(delta, extend) { backend.moveGroup(delta, extend); }
                    onSelectAllRequested: backend.selectAllGroups()
                    onClearSelectionRequested: backend.clearGroupSelection()
                    onContextRequested: function(id, highlighted, x, y) {
                        if (!highlighted) {
                            backend.selectGroupExtended(id, false, false);
                        }

                        albumMenu.popup(groupTable, x, y);
                    }
                }
            }

            ColumnLayout {
                objectName: "filesPane"
                SplitView.fillWidth: true
                SplitView.minimumWidth: 430
                spacing: window.compactHeight ? metrics.spacingCompact : metrics.spacing
                Label {
                    text: "Files / tracks"
                    Layout.leftMargin: 9
                    Layout.topMargin: window.compactHeight ? metrics.spacingCompact : 9
                }

                Flow {
                    Layout.fillWidth: true
                    Layout.leftMargin: 9
                    Layout.rightMargin: 9
                    spacing: metrics.controlSpacing
                    ActionButton {
                        objectName: "selectAllFilesButton"
                        text: "Select all"
                        enabled: window.interactionEnabled && backend.files.rowCount() > 0
                        onClicked: backend.selectAllFiles()
                    }

                    ActionButton {
                        objectName: "clearFileSelectionButton"
                        text: "Clear selection"
                        enabled: window.interactionEnabled && backend.selectedFileIds.length > 0
                        onClicked: backend.clearSelection()
                    }

                    ActionButton {
                        objectName: "renameFilesButton"
                        text: "Rename files…"
                        enabled: window.interactionEnabled && backend.applyUi.canRename
                        onClicked: backend.applyUi.beginRename()
                    }

                    ActionButton {
                        objectName: "openReviewButton"
                        text: "Metadata review…"
                        onClicked: backend.openReview()
                    }
                }

                Label {
                    Layout.fillWidth: true
                    visible: backend.groupId.length === 0
                    text: backend.groups.length ? "Choose an album to see its tracks." : "Choose a folder to scan your music library."
                    wrapMode: Text.WordWrap
                }

                DataTable {
                    id: fileTable
                    objectName: "fileTable"
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    Layout.leftMargin: 9
                    Layout.rightMargin: 9
                    // Keep one complete row available at the supported minimum
                    // window size. Larger windows still give this table all spare
                    // space, with eight rows as the initial preferred viewport.
                    Layout.minimumHeight: minimumTableHeight
                    Layout.preferredHeight: 8 * rowHeight + headerHeight + horizontalScrollBarHeight + 2
                    enabled: window.interactionEnabled
                    model: backend.files
                    sortable: backend.files.sortingEnabled
                    sortColumn: backend.files.sortColumnIndex
                    sortDescending: backend.files.sortDescending
                    defaultSortColumns: [4, 3]
                    defaultSortLabel: "Disc / track order"
                    onSortRequested: function(column, descending) {
                        backend.files.sortByColumn(column, descending);
                    }
                    onRestoreDefaultSortRequested: backend.files.restoreDefaultSort()
                    currentRow: backend.currentFileRow
                    inclusionColumn: true
                    columnWidths: scaledWidths(metrics.fileColumns)
                    hiddenColumns: metrics.hiddenFileColumns
                    emptyText: ""
                    onRowSelected: function(id, toggle, extend) { backend.selectFileExtended(id, toggle, extend); }
                    onCellActivated: function(id, value, column) {
                        if (column !== 0) {
                            window.openSelectedReview();
                        }
                    }
                    onContextRequested: function(id, highlighted, x, y) {
                        if (!highlighted) {
                            backend.selectFileExtended(id, false, false);
                        }

                        fileMenu.popup(fileTable, x, y);
                    }
                    onInclusionRequested: function(id, included) { backend.setIncluded(id, included); }
                    onMoveExtendedRequested: function(delta, extend) { backend.moveFileExtended(delta, extend); }
                    onToggleInclusionRequested: backend.toggleSelectedIncluded()
                    onSelectAllRequested: backend.selectAllFiles()
                    onClearSelectionRequested: backend.clearSelection()
                    onActivateRequested: window.openSelectedReview()
                }

                Flow {
                    id: inclusionActions
                    Layout.fillWidth: true
                    Layout.leftMargin: 9
                    Layout.rightMargin: 9
                    Layout.bottomMargin: window.compactHeight ? metrics.spacingCompact : 9
                    spacing: metrics.controlSpacing

                    Label {
                        id: selectionScopeLabel
                        objectName: "selectionScopeLabel"
                        // At a comfortable window height, reserve the first
                        // row for the full count when the actions need more
                        // width. The supported short window keeps one footer
                        // row so the table still has a complete visible row;
                        // its tooltip and accessible name retain the count.
                        readonly property bool actionsFit: implicitWidth + includeSelectedButton.implicitWidth
                            + excludeSelectedButton.implicitWidth + 2 * inclusionActions.spacing <= inclusionActions.width
                        width: actionsFit || window.compactHeight
                            ? Math.max(0, inclusionActions.width - includeSelectedButton.implicitWidth
                                - excludeSelectedButton.implicitWidth - 2 * inclusionActions.spacing)
                            : inclusionActions.width
                        height: Math.max(includeSelectedButton.implicitHeight, excludeSelectedButton.implicitHeight)
                        verticalAlignment: Text.AlignVCenter
                        text: backend.selectedFileIds.length + " highlighted · " + backend.includedFileIds.length + " included"
                        elide: Text.ElideRight
                        Accessible.name: text
                        ToolTip.visible: scopeHover.hovered
                        ToolTip.text: text
                        HoverHandler { id: scopeHover }
                    }

                    ActionButton {
                        id: includeSelectedButton
                        objectName: "includeSelectedButton"
                        text: "Add selected files"
                        enabled: window.interactionEnabled && backend.selectedFileIds.length > 0
                        onClicked: backend.includeSelection(true)
                    }

                    ActionButton {
                        id: excludeSelectedButton
                        objectName: "excludeSelectedButton"
                        text: "Remove from batch"
                        enabled: window.interactionEnabled && backend.selectedFileIds.length > 0
                        onClicked: backend.includeSelection(false)
                    }
                }
            }
        }

        RowLayout {
            spacing: metrics.controlSpacing
            Layout.fillWidth: true
            Label {
                objectName: "summaryCountsLabel"
                Layout.fillWidth: true
                Layout.minimumWidth: 0
                text: backend.summary
                elide: Text.ElideRight
                ToolTip.visible: summaryHover.hovered
                ToolTip.text: text
                HoverHandler { id: summaryHover }
            }

            ToolButton {
                id: progressButton
                objectName: "operationStageButton"
                Layout.preferredWidth: Math.min(180, implicitWidth)
                Layout.minimumWidth: 40
                Layout.maximumWidth: 180
                text: backend.progressStage
                Accessible.name: "Show operation progress: " + text
                contentItem: Label {
                    text: progressButton.text
                    font: progressButton.font
                    elide: Text.ElideRight
                    verticalAlignment: Text.AlignVCenter
                    horizontalAlignment: Text.AlignHCenter
                }
                onClicked: backend.showProgress()
            }

            ProgressBar {
                visible: backend.busy
                Layout.preferredWidth: 140
                indeterminate: backend.progress < 0
                value: Math.max(0, backend.progress)
            }

            ActionButton {
                objectName: "cancelButton"
                text: backend.cancelling ? "Cancelling…" : "Cancel operation"
                enabled: backend.busy && !backend.cancelling
                onClicked: backend.cancelScan()
            }
        }

        Label {
            Layout.fillWidth: true
            text: "Only Apply changes in the final confirmation writes files."
            wrapMode: Text.WordWrap
        }

        AppScrollView {
            Layout.fillWidth: true
            Layout.minimumHeight: implicitHeight
            Layout.preferredHeight: implicitHeight
            Layout.maximumHeight: implicitHeight
            // At the minimum window height, keep one complete diagnostic line
            // and its editor padding. The remaining text stays scrollable and
            // copyable while the file table retains a row above its scrollbar.
            implicitHeight: window.compactHeight
                ? Math.max(32, Math.ceil(statusMetrics.height) + statusText.topPadding + statusText.bottomPadding + 2)
                : 60
            visible: backend.status.length > 0
            clip: true
            contentWidth: availableWidth
            FontMetrics {
                id: statusMetrics
                font: statusText.font
            }

            TextArea {
                id: statusText
                objectName: "statusText"
                text: backend.status
                readOnly: true
                selectByMouse: true
                wrapMode: TextEdit.WrapAnywhere
                Accessible.name: "Workflow messages"
            }
        }

        RowLayout {
            spacing: metrics.controlSpacing
            Layout.fillWidth: true
            Label {
                Layout.fillWidth: true
                Layout.minimumWidth: 0
                text: backend.applyUi.guidance
                wrapMode: Text.WordWrap
            }

            ActionButton {
                objectName: "applyResultsButton"
                text: "Apply results…"
                enabled: backend.applyUi.hasResults
                onClicked: backend.applyUi.showResults()
            }

            ActionButton {
                objectName: "applySelectedButton"
                text: "Review && Apply…"
                Accessible.name: "Review & Apply"
                enabled: window.interactionEnabled && backend.applyUi.canApply
                onClicked: backend.applyUi.beginApply()
            }
        }
    }

    Menu {
        id: groupMenu
        property int snapshotRevision: backend.revision
        onAboutToShow: snapshotRevision = backend.revision
        enabled: snapshotRevision === backend.revision && window.interactionEnabled
        MenuItem {
            text: "Split selected files"
            enabled: backend.libraryUi.canSplit
            onTriggered: backend.libraryUi.split()
        }

        MenuItem {
            text: "Merge groups"
            enabled: backend.libraryUi.canMerge
            onTriggered: backend.libraryUi.merge()
        }

        MenuItem {
            text: "Disc number…"
            enabled: backend.libraryUi.canDisc
            onTriggered: backend.libraryUi.beginDisc()
        }

        MenuItem {
            text: "Choose candidate…"
            enabled: backend.lookupUi.canChoose
            onTriggered: backend.lookupUi.showCandidates()
        }

        MenuItem {
            text: "Map tracks…"
            enabled: backend.lookupUi.canMap
            onTriggered: backend.lookupUi.beginMapping()
        }

        MenuItem {
            text: "Edit search terms…"
            enabled: window.interactionEnabled && backend.groupId.length > 0
            onTriggered: backend.lookupUi.beginSearch()
        }

        MenuSeparator {
        }

        MenuItem {
            text: "Reset layout"
            onTriggered: window.resetLayout()
        }

        MenuItem {
            text: "Help and shortcuts (F1)"
            onTriggered: window.openHelp()
        }
    }

    Menu {
        id: fileMenu
        property int snapshotRevision: backend.revision
        onAboutToShow: snapshotRevision = backend.revision
        enabled: snapshotRevision === backend.revision && window.interactionEnabled
        MenuItem {
            text: backend.selectedFileIds.length + " selected files"
            enabled: false
        }

        MenuSeparator {
        }

        MenuItem {
            text: "Metadata review…\tCtrl+E"
            onTriggered: window.openSelectedReview()
        }

        MenuItem {
            text: "Rename files…"
            enabled: backend.applyUi.canRename
            onTriggered: backend.applyUi.beginRename()
        }

        MenuItem {
            text: "Add selected files"
            onTriggered: backend.includeSelection(true)
        }

        MenuItem {
            text: "Remove from batch"
            onTriggered: backend.includeSelection(false)
        }
    }

    Menu {
        id: albumMenu
        property int snapshotRevision: backend.revision
        onAboutToShow: snapshotRevision = backend.revision
        enabled: snapshotRevision === backend.revision && window.interactionEnabled
        MenuItem {
            text: backend.selectedGroupIds.length + " selected albums"
            enabled: false
        }

        MenuSeparator {
        }

        MenuItem {
            text: "Find Metadata for Selected\tCtrl+L"
            enabled: backend.lookupUi.canFind
            onTriggered: backend.lookupUi.findSelected()
        }

        MenuItem {
            text: "Choose candidate…"
            enabled: backend.lookupUi.canChoose
            onTriggered: backend.lookupUi.showCandidates()
        }

        MenuItem {
            text: "Map tracks…"
            enabled: backend.lookupUi.canMap
            onTriggered: backend.lookupUi.beginMapping()
        }

        MenuItem {
            text: "Edit search terms…"
            enabled: window.interactionEnabled
            onTriggered: backend.lookupUi.beginSearch()
        }

        MenuItem {
            text: "Split selected files"
            enabled: backend.libraryUi.canSplit
            onTriggered: backend.libraryUi.split()
        }

        MenuItem {
            text: "Merge groups"
            enabled: backend.libraryUi.canMerge
            onTriggered: backend.libraryUi.merge()
        }

        MenuItem {
            text: "Disc number…"
            enabled: backend.libraryUi.canDisc
            onTriggered: backend.libraryUi.beginDisc()
        }
    }

    FolderDialog {
        id: folderDialog
        title: "Choose a music folder"
        onAccepted: {
            folderPath.text = backend.pathFromUrl(selectedFolder);
            window.requestScan();
        }
    }

    ReviewWindow {
        id: reviewWindow
        backend: window.backend
        onHelpRequested: window.openHelp()
    }

    ProgressWindow {
        backend: window.backend
    }

    ApplyWindows {
        backend: window.backend
    }

    CandidateWindow {
        lookup: backend.lookupUi
    }

    SearchWindow {
        lookup: backend.lookupUi
    }

    MappingWindow {
        lookup: backend.lookupUi
    }

    SettingsWindow {
        settings: backend.settingsUi
        layout: backend.layoutUi
    }

    DiagnosticsWindow {
        id: diagnosticsWindow
        backend: window.backend
    }

    HelpWindow {
        id: helpWindow
        backend: window.backend
    }

    ApplicationWindow {
        id: discardDialog
        property string purpose: "scan"
        objectName: "discardDialog"
        transientParent: window
        x: window.x + (window.width - width) / 2
        y: window.y + (window.height - height) / 2
        width: Math.min(640, Screen.desktopAvailableWidth - metrics.screenHorizontalMargin)
        height: 240
        minimumWidth: 460
        minimumHeight: 220
        modality: Qt.WindowModal
        flags: Qt.Dialog | Qt.WindowTitleHint | Qt.WindowSystemMenuHint | Qt.WindowCloseButtonHint
        visible: false
        title: "Discard pending review work?"
        color: palette.window

        // A real transient window gives the warning a native title bar, close
        // button and keyboard focus. Its native close button is a safe cancel.
        onVisibleChanged: {
            if (visible) {
                // Closing the owner can leave a review window active until the
                // next event turn. Activate after the native child is exposed
                // so Escape reaches this confirmation instead of that review.
                Qt.callLater(function() {
                    if (!discardDialog.visible) {
                        return;
                    }

                    discardDialog.raise();
                    discardDialog.requestActivate();
                    cancelDiscardButton.forceActiveFocus();
                });
            }
        }

        Shortcut {
            sequences: ["Escape", "Return", "Enter"]
            // This QML object is owned by Main, while the confirmation has
            // its own native window. Scope the shortcut to the application,
            // then gate it to this active child so keyboard cancel still works.
            context: Qt.ApplicationShortcut
            enabled: discardDialog.visible && discardDialog.active
            onActivated: discardDialog.close()
        }

        ColumnLayout {
            anchors.fill: parent
            anchors.margins: metrics.windowMargin
            spacing: metrics.spacingLarge

            Label {
                Layout.fillWidth: true
                text: discardDialog.purpose === "scan"
                      ? "A successful scan will replace the entire library session, including review edits and inclusion choices across all albums."
                      : "Closing will discard pending review decisions and inclusion choices across the entire library."
                wrapMode: Text.WordWrap
            }

            AppScrollView {
                objectName: "discardWorkScroll"
                Layout.fillWidth: true
                Layout.minimumHeight: metrics.diagnosticHeight
                Layout.preferredHeight: metrics.diagnosticHeight
                Layout.maximumHeight: metrics.diagnosticHeight
                contentWidth: availableWidth
                clip: true

                // Counts describe the whole session even when the visible album
                // has no edits. The text remains copyable without enlarging the
                // confirmation or moving its explicit choices out of reach.
                TextArea {
                    objectName: "discardWorkDescription"
                    text: backend.pendingWorkDescription
                    readOnly: true
                    selectByMouse: true
                    wrapMode: TextEdit.WrapAnywhere
                    textFormat: TextEdit.PlainText
                    Accessible.name: "Pending work across the entire library"
                }
            }

            Item {
                Layout.fillHeight: true
            }

            RowLayout {
                spacing: metrics.controlSpacing
                Layout.fillWidth: true
                Layout.alignment: Qt.AlignRight
                ActionButton {
                    id: cancelDiscardButton
                    objectName: "cancelDiscardButton"
                    text: "Cancel"
                    Keys.onEscapePressed: discardDialog.close()
                    Keys.onReturnPressed: discardDialog.close()
                    Keys.onEnterPressed: discardDialog.close()
                    onClicked: discardDialog.close()
                }

                ActionButton {
                    objectName: "confirmDiscardButton"
                    text: discardDialog.purpose === "scan" ? "Discard and scan" : "Discard and close"
                    onClicked: {
                        discardDialog.close();

                        if (discardDialog.purpose === "scan") {
                            backend.scan(window.pendingScanPath, true);
                        } else {
                            window.discardCloseConfirmed = true;
                            window.close();
                        }
                    }
                }
            }
        }
    }

    Dialog {
        id: confirmationDialog
        parent: Overlay.overlay
        x: (parent.width - width) / 2
        y: (parent.height - height) / 2
        width: Math.min(620, window.width - 48)
        modal: true
        visible: backend.confirmationVisible
        title: backend.confirmationTitle
        closePolicy: Popup.NoAutoClose
        onOpened: {
            window.raise();
            window.requestActivate();
            cancelConfirmationButton.forceActiveFocus();
        }
        contentItem: ColumnLayout {
            AppScrollView {
                Layout.fillWidth: true
                Layout.preferredHeight: Math.min(300, confirmationText.implicitHeight + 12)
                Layout.maximumHeight: 300
                implicitHeight: Math.min(300, confirmationText.implicitHeight + 12)
                clip: true
                contentWidth: availableWidth
                TextArea {
                    id: confirmationText
                    text: backend.confirmationText
                    readOnly: true
                    selectByMouse: true
                    wrapMode: TextEdit.WrapAnywhere
                }
            }

            RowLayout {
                spacing: metrics.controlSpacing
                Layout.alignment: Qt.AlignRight
                ActionButton {
                    id: cancelConfirmationButton
                    text: "Cancel"
                    onClicked: backend.cancelConfirmation()
                }

                ActionButton {
                    text: "Continue"
                    onClicked: backend.confirmAction()
                }
            }
        }

        Shortcut {
            sequence: "Escape"
            enabled: confirmationDialog.visible
            onActivated: backend.cancelConfirmation()
        }
    }

    Dialog {
        id: discDialog
        parent: Overlay.overlay
        x: (parent.width - width) / 2
        y: (parent.height - height) / 2
        width: 380
        title: "Disc number for lookup"
        modal: true
        visible: backend.libraryUi.discVisible
        closePolicy: Popup.NoAutoClose
        onOpened: discNumber.value = backend.libraryUi.discValue
        contentItem: ColumnLayout {
            Label {
                text: "Disc number for this group:"
            }

            Label {
                Layout.fillWidth: true
                text: "Changing the disc number clears this group's candidate selection and track mapping. "
                      + "Candidate choices require review again. Manual values, Clear, Keep existing and filename choices are retained."
                wrapMode: Text.WordWrap
            }

            AppSpinBox {
                id: discNumber
                from: 0
                to: 9999
                editable: true
                textFromValue: function(value, locale) { return value === 0 ? "Auto" : String(value); }
                valueFromText: function(text, locale) { return text.toLowerCase() === "auto" ? 0 : Number(text); }
            }

            Label {
                Layout.fillWidth: true
                text: backend.libraryUi.discError
                wrapMode: Text.WordWrap
            }

            RowLayout {
                spacing: metrics.controlSpacing
                Layout.alignment: Qt.AlignRight
                ActionButton {
                    text: "Cancel"
                    onClicked: backend.libraryUi.cancelDisc()
                }

                ActionButton {
                    text: "OK"
                    onClicked: backend.libraryUi.commitDisc(discNumber.value)
                }
            }
        }

        Shortcut {
            sequence: "Escape"
            enabled: discDialog.visible
            onActivated: backend.libraryUi.cancelDisc()
        }
    }
}
