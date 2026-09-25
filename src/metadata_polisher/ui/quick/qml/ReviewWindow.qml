import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Window

ApplicationWindow {
    id: reviewWindow

    required property QtObject backend
    readonly property bool interactionEnabled: !backend.busy && !backend.editing
                                               && !backend.confirmationVisible && !fullValueDialog.visible
    readonly property bool multiple: backend.scopeFileIds.length > 1 || backend.selectedFields.length > 1
    property bool layoutReady: false
    property int retainedWidth: metrics.reviewWidth
    property int retainedHeight: metrics.reviewHeight
    signal helpRequested()

    UiMetrics {
        id: metrics
    }

    objectName: "metadataReviewWindow"
    title: "Metadata review — Metadata Polisher"
    width: metrics.reviewWidth
    height: metrics.reviewHeight
    minimumWidth: 600
    minimumHeight: 360
    modality: Qt.NonModal
    visible: backend.reviewVisible
    color: palette.window

    Component.onCompleted: {
        // Apply screen bounds once. A live Screen binding would reset a user's
        // size when the platform detaches and reattaches a hidden window.
        width = Math.min(metrics.reviewWidth, Screen.desktopAvailableWidth - metrics.screenHorizontalMargin);
        height = Math.min(metrics.reviewHeight, Screen.desktopAvailableHeight - metrics.screenVerticalMargin);
        backend.layoutUi.restoreWindow(reviewWindow, "MetadataReviewWindow");
        const widths = backend.layoutUi.columnWidths(
            "MainWindow/diffTableView", reviewTable.scaledWidths(metrics.reviewColumns));
        const hidden = backend.layoutUi.hiddenColumns(
            "MainWindow/diffTableView", metrics.hiddenReviewColumns).filter(function(column) {
                return column !== 0;
            });
        reviewTable.resetColumns(widths, hidden);
        layoutReady = true;
        retainedWidth = width;
        retainedHeight = height;
    }

    function captureLayout() {
        backend.layoutUi.captureWindow(reviewWindow, "MetadataReviewWindow");
        backend.layoutUi.saveColumns("MainWindow/diffTableView", reviewTable.storedWidths(), reviewTable.hiddenColumns);
    }

    onWidthChanged: {
        if (layoutReady && visible) {
            retainedWidth = width;
        }
    }

    onHeightChanged: {
        if (layoutReady && visible) {
            retainedHeight = height;
        }
    }

    onClosing: {
        retainedWidth = width;
        retainedHeight = height;
        captureLayout();
        backend.closeReview();
    }
    onVisibleChanged: {
        if (visible) {
            width = retainedWidth;
            height = retainedHeight;
            raise();
            requestActivate();
        } else if (layoutReady) {
            captureLayout();
        }
    }

    function resetLayout() {
        visibility = visible ? Window.Windowed : Window.Hidden;
        width = Math.min(metrics.reviewWidth, Screen.desktopAvailableWidth - metrics.screenHorizontalMargin);
        height = Math.min(metrics.reviewHeight, Screen.desktopAvailableHeight - metrics.screenVerticalMargin);
        retainedWidth = width;
        retainedHeight = height;
        reviewTable.resetColumns(reviewTable.scaledWidths(metrics.reviewColumns), metrics.hiddenReviewColumns);
    }

    function openDetails(value) {
        fullValueText.text = value;
        fullValueDialog.open();
    }

    Shortcut {
        sequence: "Escape"
        enabled: reviewWindow.visible && reviewWindow.active && !backend.editing && !fullValueDialog.visible
        onActivated: backend.closeReview()
    }

    Shortcut {
        sequence: "F1"
        enabled: reviewWindow.active && !backend.editing
        onActivated: reviewWindow.helpRequested()
    }

    Shortcut {
        sequences: [StandardKey.Undo]
        enabled: reviewWindow.active && reviewWindow.interactionEnabled && backend.canUndo
        onActivated: backend.undo()
    }

    Shortcut {
        sequences: ["Ctrl+Return", "Ctrl+Enter"]
        enabled: reviewWindow.active && reviewWindow.interactionEnabled && backend.applyUi.canApply
        onActivated: backend.applyUi.beginApply()
    }

    Shortcut {
        sequence: "Alt+Left"
        enabled: reviewWindow.active && reviewWindow.interactionEnabled && backend.canNavigate
        onActivated: backend.moveFile(-1)
    }

    Shortcut {
        sequence: "Alt+Right"
        enabled: reviewWindow.active && reviewWindow.interactionEnabled && backend.canNavigate
        onActivated: backend.moveFile(1)
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 9
        spacing: 6

        RowLayout {
            Layout.fillWidth: true

            Label {
                objectName: "reviewTargetLabel"
                Layout.fillWidth: true
                text: "Metadata review · " + (backend.reviewTargetLabel.length ? backend.reviewTargetLabel
                      : "0 selected — Select tracks or change the review scope")
                textFormat: Text.PlainText
                elide: Text.ElideMiddle
                ToolTip.visible: targetHover.hovered
                ToolTip.text: text
                HoverHandler { id: targetHover }
            }

            ComboBox {
                objectName: "reviewScopeCombo"
                model: ["Selected files", "Current group", "Included files", "All library files"]
                property var keys: ["selected", "group", "included", "library"]
                currentIndex: keys.indexOf(backend.reviewScope)
                enabled: reviewWindow.interactionEnabled
                Accessible.name: "Review scope"
                onActivated: backend.setReviewScope(keys[currentIndex])
            }
        }

        AppScrollView {
            id: reviewScroll

            objectName: "reviewScrollArea"
            Layout.fillWidth: true
            Layout.fillHeight: true
            clip: true
            contentWidth: availableWidth

            ColumnLayout {
                width: reviewScroll.availableWidth
                spacing: metrics.spacingSmall

                RowLayout {
                    Layout.fillWidth: true
                    Label { text: "Metadata ·" }
                    Label {
                        Layout.fillWidth: true
                        text: backend.reviewScopeLabel
                        wrapMode: Text.WordWrap
                    }
                    ActionButton {
                        objectName: "previousReviewFileButton"
                        text: "Previous"
                        enabled: reviewWindow.interactionEnabled && backend.canNavigate && backend.currentFileRow > 0
                        onClicked: backend.moveFile(-1)
                    }
                    ActionButton {
                        objectName: "nextReviewFileButton"
                        text: "Next"
                        enabled: reviewWindow.interactionEnabled && backend.canNavigate
                                 && backend.currentFileRow + 1 < backend.files.rowCount()
                        onClicked: backend.moveFile(1)
                    }
                    ActionButton {
                        objectName: "undoReviewButton"
                        text: "Undo review"
                        enabled: reviewWindow.interactionEnabled && backend.canUndo
                        onClicked: backend.undo()
                    }
                }

                RowLayout {
                    Layout.fillWidth: true
                    Label {
                        Layout.fillWidth: true
                        visible: backend.selectedFields.length === 0
                        text: backend.selectedFields.length ? "" : "Select a field, or double-click its Final value to edit."
                        wrapMode: Text.WordWrap
                    }
                    ActionButton {
                        id: detailsButton
                        Layout.fillWidth: true
                        objectName: "fieldDetailsButton"
                        text: checked ? "Full values ▾" : "Full values ▸"
                        checkable: true
                        enabled: backend.selectedFields.length > 0
                    }
                }

                DataTable {
                    id: reviewTable

                    objectName: "reviewTable"
                    Layout.fillWidth: true
                    Layout.preferredHeight: Math.max(195, reviewScroll.availableHeight - 278)
                    model: backend.review
                    enabled: reviewWindow.interactionEnabled
                    currentRow: backend.currentFieldRow
                    columnWidths: scaledWidths(metrics.reviewColumns)
                    hiddenColumns: metrics.hiddenReviewColumns
                    protectedColumn: 0
                    stretchLastColumn: true
                    emptyText: "Select one or more files to inspect their metadata."

                    onRowSelected: function(stableId, toggle, extend) {
                        backend.selectFieldExtended(stableId, toggle, extend);
                    }

                    onCellActivated: function(stableId, value, column) {
                        if (column === 4) {
                            backend.beginEdit();
                        }
                    }

                    onContextRequested: function(stableId, highlighted, x, y) {
                        if (!highlighted) {
                            backend.selectFieldExtended(stableId, false, false);
                        }

                        fieldMenu.popup(reviewTable, x, y);
                    }

                    onMoveExtendedRequested: function(delta, extend) {
                        backend.moveFieldExtended(delta, extend);
                    }

                    onSelectAllRequested: backend.selectAllFields()
                    onClearSelectionRequested: backend.clearFieldSelection()
                    onEditRequested: backend.beginEdit()

                    onValueRequested: function(value) {
                        reviewWindow.openDetails(value);
                    }
                }

                AppScrollView {
                    objectName: "fieldDetailsScroll"
                    implicitHeight: metrics.diagnosticHeight
                    height: metrics.diagnosticHeight
                    Layout.fillWidth: true
                    Layout.preferredHeight: metrics.diagnosticHeight
                    Layout.minimumHeight: metrics.diagnosticHeight
                    Layout.maximumHeight: metrics.diagnosticHeight
                    visible: detailsButton.checked && backend.fieldDetails.length > 0
                    clip: true
                    contentWidth: availableWidth
                    TextArea {
                        objectName: "fieldDetails"
                        text: backend.fieldDetails
                        readOnly: true
                        selectByMouse: true
                        wrapMode: TextEdit.WrapAnywhere
                        Accessible.name: "Full metadata values and review details"
                    }
                }

                ComboBox {
                    objectName: "proposalCombo"
                    Layout.fillWidth: true
                    model: backend.proposalOptions
                    textRole: "label"
                    valueRole: "key"
                    visible: !reviewWindow.multiple
                    enabled: reviewWindow.interactionEnabled && backend.canUseCandidate
                    currentIndex: indexOfValue(backend.proposalKey)
                    onActivated: backend.setProposal(currentValue)
                }

                GridLayout {
                    id: fieldActions
                    Layout.fillWidth: true
                    readonly property real widestAction: Math.max(keepAction.implicitWidth,
                        candidateAction.implicitWidth, manualAction.implicitWidth, clearAction.implicitWidth + 16)
                    columns: width >= 4 * widestAction + 3 * columnSpacing ? 4
                             : width >= 2 * widestAction + columnSpacing ? 2 : 1

                    // Keep complete action labels at larger text sizes. A narrow
                    // review uses additional rows inside its existing scroll area.
                    ActionButton {
                        id: keepAction
                        objectName: "keepExistingButton"
                        Layout.fillWidth: true
                        Layout.minimumWidth: implicitWidth
                        Layout.preferredWidth: 1
                        text: "Keep Existing"
                        enabled: reviewWindow.interactionEnabled && backend.canEdit
                        onClicked: backend.reviewAction("keep_existing")
                    }
                    ActionButton {
                        id: candidateAction
                        objectName: "useProposedButton"
                        Layout.fillWidth: true
                        Layout.minimumWidth: implicitWidth
                        Layout.preferredWidth: 1
                        text: reviewWindow.multiple ? "Use each file's candidate" : "Use candidate"
                        enabled: reviewWindow.interactionEnabled && backend.canUseCandidate
                        onClicked: backend.reviewAction("use_candidate")
                    }
                    ActionButton {
                        id: manualAction
                        objectName: "manualValueButton"
                        Layout.fillWidth: true
                        Layout.minimumWidth: implicitWidth
                        Layout.preferredWidth: 1
                        text: reviewWindow.multiple ? "Set common value…" : "Manual…"
                        enabled: reviewWindow.interactionEnabled && backend.canEdit
                        onClicked: backend.beginEdit()
                    }
                    ActionButton {
                        id: clearAction
                        objectName: "clearValueButton"
                        Layout.fillWidth: true
                        Layout.minimumWidth: implicitWidth
                        Layout.leftMargin: fieldActions.columns === 4 ? 16 : 0
                        Layout.preferredWidth: 1
                        text: "Clear selected fields"
                        enabled: reviewWindow.interactionEnabled && backend.canEdit
                        onClicked: backend.reviewAction("clear")
                    }
                }

                ActionButton {
                    objectName: "acceptSafeAdditionsButton"
                    text: "Accept Safe Additions"
                    enabled: reviewWindow.interactionEnabled && backend.canAcceptSafe
                    onClicked: backend.reviewAction("accept_safe_additions")
                }

                Label { text: "Filenames" }

                Repeater {
                    model: [backend.scopeFileIds.length > 1 ? "Current filenames: " + backend.currentFilename
                                                           : "Current filename: " + (backend.currentFilename || "—"),
                            backend.scopeFileIds.length > 1 ? "Proposed filenames: each file has its own reviewed preview"
                                                           : "Proposed filename: " + (backend.proposedFilename || "—"),
                            "Template: " + backend.renameTemplate]
                    delegate: Label {
                        required property string modelData
                        Layout.fillWidth: true
                        text: modelData
                        textFormat: Text.PlainText
                        elide: Text.ElideMiddle
                        ToolTip.visible: filenameHover.hovered
                        ToolTip.text: text
                        HoverHandler { id: filenameHover }
                    }
                }

                GridLayout {
                    id: filenameActions
                    Layout.fillWidth: true
                    readonly property real widestAction: Math.max(keepFilenameAction.implicitWidth,
                        proposedFilenameAction.implicitWidth, previewsAction.implicitWidth)
                    columns: width >= 3 * widestAction + 2 * columnSpacing ? 3
                             : width >= 2 * widestAction + columnSpacing ? 2 : 1

                    ActionButton {
                        id: keepFilenameAction
                        objectName: "keepFilenameButton"
                        Layout.fillWidth: true
                        Layout.minimumWidth: implicitWidth
                        Layout.preferredWidth: 1
                        text: "Keep filename"
                        enabled: reviewWindow.interactionEnabled && backend.canKeepFilename
                        onClicked: backend.renameAction(false)
                    }
                    ActionButton {
                        id: proposedFilenameAction
                        objectName: "applyRenameButton"
                        Layout.fillWidth: true
                        Layout.minimumWidth: implicitWidth
                        Layout.preferredWidth: 1
                        text: "Use proposed filename"
                        enabled: reviewWindow.interactionEnabled && backend.canRename
                        onClicked: backend.renameAction(true)
                    }
                    ActionButton {
                        id: previewsAction
                        objectName: "renamePreviewsButton"
                        Layout.fillWidth: true
                        Layout.minimumWidth: implicitWidth
                        Layout.preferredWidth: 1
                        text: "Filename previews…"
                        enabled: reviewWindow.interactionEnabled && backend.scopeFileIds.length > 0
                        onClicked: backend.applyUi.showPreviews()
                    }
                }

                Label {
                    Layout.fillWidth: true
                    text: backend.renameValidation
                    textFormat: Text.PlainText
                    wrapMode: Text.WrapAnywhere
                }
            }
        }

        ColumnLayout {
            objectName: "reviewFooter"
            Layout.fillWidth: true
            spacing: metrics.spacingSmall

            Label { text: "Write batch · " + backend.includedFileIds.length + " included for writing" }
            Label {
                Layout.fillWidth: true
                text: "Review decisions stay in memory. Only Apply changes in the final confirmation writes files."
                wrapMode: Text.WordWrap
            }
            AppScrollView {
                Layout.fillWidth: true
                Layout.preferredHeight: 60
                Layout.maximumHeight: 60
                implicitHeight: 60
                visible: backend.status.length > 0
                clip: true
                contentWidth: availableWidth
                TextArea {
                    objectName: "reviewMessageLabel"
                    text: backend.status
                    readOnly: true
                    selectByMouse: true
                    wrapMode: TextEdit.WrapAnywhere
                }
            }
            Label {
                Layout.fillWidth: true
                text: backend.applyUi.guidance
                wrapMode: Text.WordWrap
            }
            Flow {
                Layout.fillWidth: true
                spacing: metrics.spacing
                ActionButton {
                    objectName: "includeReviewScopeButton"
                    text: "Add scope to write batch"
                    enabled: reviewWindow.interactionEnabled && backend.scopeFileIds.length > 0
                    onClicked: backend.includeScope()
                }

                ActionButton {
                    objectName: "reviewApplyButton"
                    text: "Review changes…"
                    enabled: reviewWindow.interactionEnabled && backend.applyUi.canApply
                    onClicked: backend.applyUi.beginApply()
                }
            }
        }
    }

    Menu {
        id: fieldMenu
        property int snapshotRevision: backend.revision
        onAboutToShow: snapshotRevision = backend.revision
        enabled: snapshotRevision === backend.revision && reviewWindow.interactionEnabled

        MenuItem {
            text: backend.selectedFields.length + " fields · " + backend.scopeFileIds.length + " files"
            enabled: false
        }

        MenuSeparator { }
        MenuItem {
            text: reviewWindow.multiple ? "Set common value…\tF2" : "Manual…\tF2"
            enabled: backend.canEdit
            onTriggered: backend.beginEdit()
        }
        MenuItem {
            text: "Keep Existing"
            enabled: backend.canEdit
            onTriggered: backend.reviewAction("keep_existing")
        }

        MenuItem {
            objectName: "useCandidateContextAction"
            text: reviewWindow.multiple ? "Use each file's candidate" : "Use candidate"
            enabled: backend.canUseCandidate
            onTriggered: backend.reviewAction("use_candidate")
        }
        MenuItem {
            text: "Clear selected fields"
            enabled: backend.canEdit
            onTriggered: backend.reviewAction("clear")
        }

        MenuItem {
            text: "Undo review\tCtrl+Z"
            enabled: backend.canUndo
            onTriggered: backend.undo()
        }
    }

    ManualEditWindow {
        backend: reviewWindow.backend
        transientParent: reviewWindow
    }

    Dialog {
        id: fullValueDialog
        parent: Overlay.overlay
        x: (parent.width - width) / 2
        y: (parent.height - height) / 2
        width: Math.min(800, reviewWindow.width - 48)
        height: Math.min(520, reviewWindow.height - 48)
        modal: true
        title: "Full value and details"
        standardButtons: Dialog.Close
        contentItem: AppScrollView {
            clip: true
            TextArea {
                id: fullValueText
                readOnly: true
                selectByMouse: true
                wrapMode: TextEdit.WrapAnywhere
            }
        }
    }
}
