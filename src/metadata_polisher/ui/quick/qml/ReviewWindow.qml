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
    readonly property bool scanFailed: backend.progressFailed && backend.progressKind === "scan"
    property bool layoutReady: false
    property int retainedWidth: metrics.reviewWidth
    property int retainedHeight: metrics.reviewHeight
    property string filenameContext: ""
    property string diagnosticContext: ""
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

    Connections {
        target: backend

        function onChanged() {
            // Reset disclosures when their subject changes, while retaining a
            // user's expansion choice during ordinary edits of the same file.
            const filenames = JSON.stringify(backend.scopeFileIds) + backend.hasFilenameSuggestion
                              + (backend.filenameNeedsAttention ? backend.renameValidation : "");

            if (filenames !== reviewWindow.filenameContext) {
                reviewWindow.filenameContext = filenames;
                filenameToggle.checked = backend.hasFilenameSuggestion || backend.filenameNeedsAttention;
            }

            const diagnostics = backend.scanSummary + backend.scanDetails
                                + (reviewWindow.scanFailed ? backend.status : "");

            if (diagnostics !== reviewWindow.diagnosticContext) {
                reviewWindow.diagnosticContext = diagnostics;
                diagnosticsToggle.checked = backend.scanHasWarnings || reviewWindow.scanFailed;
            }
        }
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
        enabled: reviewWindow.active && reviewWindow.interactionEnabled && backend.applyUi.canApply && backend.hasChangesToApply
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
        spacing: metrics.spacing

        GridLayout {
            Layout.fillWidth: true
            columns: width >= navigation.implicitWidth + scopeControls.implicitWidth + 20 ? 2 : 1

            RowLayout {
                spacing: metrics.controlSpacing
                id: navigation
                Layout.fillWidth: true

                ActionButton {
                    objectName: "previousReviewFileButton"
                    text: "← Previous"
                    enabled: reviewWindow.interactionEnabled && backend.canNavigate && backend.currentFileRow > 0
                    onClicked: backend.moveFile(-1)
                }
                Label {
                    objectName: "reviewFileProgress"
                    text: backend.fileProgress
                    Accessible.name: "File position: " + text
                }
                ActionButton {
                    objectName: "nextReviewFileButton"
                    text: "Next →"
                    enabled: reviewWindow.interactionEnabled && backend.canNavigate
                             && backend.currentFileRow + 1 < backend.files.rowCount()
                    onClicked: backend.moveFile(1)
                }
                Item { Layout.fillWidth: true }
            }

            RowLayout {
                spacing: metrics.controlSpacing
                id: scopeControls
                Label { text: "Reviewing:" }
                AppComboBox {
                    objectName: "reviewScopeCombo"
                    implicitContentWidthPolicy: ComboBox.WidestText
                    Layout.minimumWidth: implicitWidth
                    model: ["Selected files", "Current group", "Included files", "All library files"]
                    property var keys: ["selected", "group", "included", "library"]
                    currentIndex: keys.indexOf(backend.reviewScope)
                    enabled: reviewWindow.interactionEnabled
                    Accessible.name: "Files to review"
                    onActivated: backend.setReviewScope(keys[currentIndex])
                }
            }
        }

        Label {
            objectName: "reviewTargetLabel"
            Layout.fillWidth: true
            text: backend.reviewTargetLabel || "Select files to review their metadata."
            textFormat: Text.PlainText
            font.bold: true
            elide: Text.ElideMiddle
            ToolTip.visible: targetHover.hovered
            ToolTip.text: text
            HoverHandler { id: targetHover }
        }

        AppScrollView {
            id: reviewScroll
            objectName: "reviewScrollArea"
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.minimumHeight: reviewTable.minimumTableHeight
            clip: true
            // The desktop scrollbar occupies real horizontal space. Reserve
            // it so wrapped actions never sit underneath its input surface.
            rightPadding: effectiveScrollBarWidth
            contentWidth: availableWidth

            ColumnLayout {
                width: reviewScroll.availableWidth
                // The table takes spare height. Only genuinely small windows or
                // expanded details need to scroll the surrounding review content.
                height: Math.max(reviewScroll.availableHeight, implicitHeight)
                spacing: metrics.spacingSmall

                GridLayout {
                    Layout.fillWidth: true
                    columns: reviewScroll.availableWidth >= metadataTitle.implicitWidth
                             + Math.max(safeButton.implicitWidth, safeExplanation.implicitWidth) + 24 ? 2 : 1

                    ColumnLayout {
                        id: metadataHeading
                        Layout.fillWidth: true
                        spacing: 2
                        Label {
                            id: metadataTitle
                            Layout.fillWidth: true
                            text: "1  Metadata review"
                            font.bold: true
                        }
                        Label {
                            Layout.fillWidth: true
                            text: backend.reviewScopeLabel
                                  + (reviewTable.compactColumns ? " · Final values first" : "")
                            textFormat: Text.PlainText
                            elide: Text.ElideRight
                            color: palette.placeholderText
                        }
                    }
                    ColumnLayout {
                        id: safeSuggestions
                        spacing: 2
                        ActionButton {
                            id: safeButton
                            objectName: "acceptSafeAdditionsButton"
                            Layout.alignment: Qt.AlignRight
                            text: "Accept safe suggestions"
                            enabled: reviewWindow.interactionEnabled && backend.canAcceptSafe
                            onClicked: backend.reviewAction("accept_safe_additions")
                        }
                        Label {
                            id: safeExplanation
                            text: "Only confident, non-conflicting additions."
                            color: palette.placeholderText
                        }
                    }
                }

                DataTable {
                    id: reviewTable
                    objectName: "reviewTable"
                    // Preserve readable value widths. On a narrow viewport,
                    // bring the write result and its status next to the field;
                    // Existing and Proposed remain reachable by scrolling.
                    readonly property bool compactColumns: width < preferredVisibleColumnsWidth + 2
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    Layout.minimumHeight: 195
                    Layout.preferredHeight: 195
                    model: backend.review
                    enabled: reviewWindow.interactionEnabled
                    currentRow: backend.currentFieldRow
                    columnWidths: scaledWidths(metrics.reviewColumns)
                    columnOrder: compactColumns ? [0, 4, 1, 2, 3, 5] : [0, 2, 3, 4, 1, 5]
                    stretchColumns: [2, 3, 4]
                    hiddenColumns: metrics.hiddenReviewColumns
                    protectedColumn: 0
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
                    onValueRequested: function(value) { reviewWindow.openDetails(value); }
                }

                AppComboBox {
                    objectName: "proposalCombo"
                    Layout.fillWidth: true
                    model: backend.proposalOptions
                    textRole: "label"
                    valueRole: "key"
                    // A single suggestion is already visible in the table.
                    visible: !reviewWindow.multiple && count > 1
                    enabled: reviewWindow.interactionEnabled && backend.canUseCandidate
                    currentIndex: indexOfValue(backend.proposalKey)
                    Accessible.name: "Proposed value to use"
                    onActivated: backend.setProposal(currentValue)
                }

                Flow {
                    Layout.fillWidth: true
                    spacing: metrics.controlSpacing
                    ActionButton {
                        objectName: "keepExistingButton"
                        text: "Keep existing"
                        enabled: reviewWindow.interactionEnabled && backend.canEdit
                        onClicked: backend.reviewAction("keep_existing")
                    }
                    ActionButton {
                        objectName: "useProposedButton"
                        text: "Use proposed"
                        highlighted: true
                        enabled: reviewWindow.interactionEnabled && backend.canUseCandidate
                        ToolTip.visible: hovered
                        ToolTip.text: "Use each selected field's proposed value for the files being reviewed."
                        onClicked: backend.reviewAction("use_candidate")
                    }
                    ActionButton {
                        objectName: "manualValueButton"
                        text: "Edit…"
                        enabled: reviewWindow.interactionEnabled && backend.canEdit
                        ToolTip.visible: hovered
                        ToolTip.text: reviewWindow.multiple ? "Set a common value for the selected fields and files."
                                                          : "Edit the Final value (F2)."
                        onClicked: backend.beginEdit()
                    }
                    ActionButton {
                        objectName: "moreFieldActionsButton"
                        text: "More…"
                        flat: true
                        enabled: reviewWindow.interactionEnabled && backend.canEdit
                        onClicked: fieldMenu.popup()
                    }
                    ActionButton {
                        objectName: "undoReviewButton"
                        text: "Undo review"
                        flat: true
                        enabled: reviewWindow.interactionEnabled && backend.canUndo
                        onClicked: backend.undo()
                    }
                    ActionButton {
                        id: detailsButton
                        objectName: "fieldDetailsButton"
                        text: checked ? "Hide full values" : "Show full values"
                        flat: true
                        checkable: true
                        enabled: backend.selectedFields.length > 0
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

                RowLayout {
                    spacing: metrics.controlSpacing
                    Layout.fillWidth: true
                    ToolButton {
                        id: filenameToggle
                        objectName: "filenameSectionButton"
                        text: (checked ? "▾" : "▸") + "  2  Filename review"
                        checkable: true
                        checked: backend.hasFilenameSuggestion || backend.filenameNeedsAttention
                        Accessible.name: (checked ? "Collapse" : "Expand") + " filename review"
                    }
                    Label {
                        Layout.fillWidth: true
                        text: backend.filenameSummary
                        elide: Text.ElideRight
                        textFormat: Text.PlainText
                        ToolTip.visible: filenameSummaryHover.hovered
                        ToolTip.text: text
                        HoverHandler { id: filenameSummaryHover }
                    }
                }

                ColumnLayout {
                    objectName: "filenameDetails"
                    Layout.fillWidth: true
                    visible: filenameToggle.checked
                    spacing: metrics.spacingSmall

                    Repeater {
                        model: ["Current: " + (backend.currentFilename || "No files selected"),
                                "Proposed: " + (backend.proposedFilename || "No suggestion")]
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

                    Flow {
                        Layout.fillWidth: true
                        spacing: metrics.controlSpacing
                        ActionButton {
                            objectName: "keepFilenameButton"
                            text: "Keep current"
                            enabled: reviewWindow.interactionEnabled && backend.canKeepFilename
                            onClicked: backend.renameAction(false)
                        }
                        ActionButton {
                            objectName: "applyRenameButton"
                            text: "Use proposed"
                            enabled: reviewWindow.interactionEnabled && backend.canRename
                            onClicked: backend.renameAction(true)
                        }
                        ActionButton {
                            objectName: "renamePreviewsButton"
                            text: "Filename previews…"
                            flat: true
                            enabled: reviewWindow.interactionEnabled && backend.scopeFileIds.length > 0
                            onClicked: backend.applyUi.showPreviews()
                        }
                        ActionButton {
                            id: templateToggle
                            text: "Filename details"
                            flat: true
                            checkable: true
                        }
                    }
                    Label {
                        Layout.fillWidth: true
                        text: "Template: " + backend.renameTemplate
                        visible: templateToggle.checked
                        textFormat: Text.PlainText
                        wrapMode: Text.WrapAnywhere
                    }
                    Label {
                        Layout.fillWidth: true
                        text: backend.renameValidation
                        visible: text.length > 0
                        textFormat: Text.PlainText
                        wrapMode: Text.WrapAnywhere
                    }
                }

                RowLayout {
                    spacing: metrics.controlSpacing
                    Layout.fillWidth: true
                    ToolButton {
                        id: diagnosticsToggle
                        objectName: "scanDetailsButton"
                        text: (checked ? "▾" : "▸") + "  Scan details"
                        checkable: true
                        checked: backend.scanHasWarnings || reviewWindow.scanFailed
                        Accessible.name: (checked ? "Hide" : "View") + " scan details"
                    }
                    Label {
                        Layout.fillWidth: true
                        text: reviewWindow.scanFailed ? "Scan failed · Previous review retained"
                              : (backend.scanHasWarnings ? "Warning · " : "") + backend.scanSummary
                        visible: backend.scanSummary.length > 0
                        textFormat: Text.PlainText
                        elide: Text.ElideRight
                        ToolTip.visible: scanHover.hovered
                        ToolTip.text: text
                        HoverHandler { id: scanHover }
                    }
                }
                AppScrollView {
                    objectName: "reviewDiagnostics"
                    implicitHeight: metrics.diagnosticHeight
                    height: metrics.diagnosticHeight
                    Layout.fillWidth: true
                    Layout.preferredHeight: metrics.diagnosticHeight
                    Layout.minimumHeight: metrics.diagnosticHeight
                    Layout.maximumHeight: metrics.diagnosticHeight
                    visible: diagnosticsToggle.checked
                    clip: true
                    contentWidth: availableWidth
                    TextArea {
                        objectName: "reviewMessageLabel"
                        text: backend.scanDetails + (backend.status.length ? "\n" + backend.status : "")
                        readOnly: true
                        selectByMouse: true
                        wrapMode: TextEdit.WrapAnywhere
                        Accessible.name: "Scan and review diagnostics"
                    }
                }
            }
        }

        // Keep the final action outside the scrolling body at every size.
        ColumnLayout {
            objectName: "reviewFooter"
            Layout.fillWidth: true
            spacing: metrics.spacingSmall

            Label {
                Layout.fillWidth: true
                text: backend.reviewMessage
                visible: text.length > 0
                textFormat: Text.PlainText
                elide: Text.ElideRight
                maximumLineCount: 1
                ToolTip.visible: statusHover.hovered
                ToolTip.text: text
                HoverHandler { id: statusHover }
            }
            RowLayout {
                spacing: metrics.controlSpacing
                Layout.fillWidth: true
                Label {
                    Layout.fillWidth: true
                    text: "Changes to apply · " + backend.changesToApplySummary
                    textFormat: Text.PlainText
                    elide: Text.ElideRight
                    ToolTip.visible: applySummaryHover.hovered
                    ToolTip.text: text + "\nReview decisions stay in memory until final confirmation."
                    HoverHandler { id: applySummaryHover }
                }
                ActionButton {
                    objectName: "includeReviewScopeButton"
                    text: backend.reviewScope === "selected" ? "Add selected files" : "Add reviewed files"
                    enabled: reviewWindow.interactionEnabled && backend.scopeFileIds.length > 0
                    ToolTip.visible: hovered
                    ToolTip.text: "Add the files being reviewed to Changes to apply. This does not write files."
                    onClicked: backend.includeScope()
                }
                ActionButton {
                    objectName: "reviewApplyButton"
                    text: "Review && Apply…"
                    Accessible.name: "Review & Apply"
                    highlighted: true
                    enabled: reviewWindow.interactionEnabled && backend.applyUi.canApply && backend.hasChangesToApply
                    ToolTip.visible: hovered
                    ToolTip.text: "Inspect the changes before the final write confirmation (Ctrl+Enter)."
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
            text: "Edit…\tF2"
            enabled: backend.canEdit
            onTriggered: backend.beginEdit()
        }
        MenuItem {
            text: "Keep existing"
            enabled: backend.canEdit
            onTriggered: backend.reviewAction("keep_existing")
        }

        MenuItem {
            objectName: "useCandidateContextAction"
            text: "Use proposed"
            enabled: backend.canUseCandidate
            onTriggered: backend.reviewAction("use_candidate")
        }
        MenuSeparator { }
        MenuItem {
            objectName: "clearValueButton"
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
