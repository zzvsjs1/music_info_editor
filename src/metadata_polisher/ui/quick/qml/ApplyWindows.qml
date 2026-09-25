import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Window

Item {
    id: owner
    required property QtObject backend
    readonly property QtObject apply: backend.applyUi

    UiMetrics { id: metrics }

    // Long summaries scroll within their own document. The layout reserves
    // space for the file table and keeps confirmation actions outside it.
    component SummaryText: AppScrollView {
        id: summaryText
        property string text: ""
        property bool fitContent: false
        readonly property real fittedHeight: Math.min(metrics.diagnosticHeight * 2,
            Math.max(metrics.diagnosticHeight / 2,
                     summaryArea.contentHeight + summaryArea.topPadding + summaryArea.bottomPadding))
        Layout.fillWidth: true
        Layout.fillHeight: !fitContent
        Layout.minimumHeight: metrics.diagnosticHeight / 2
        Layout.preferredHeight: fitContent ? fittedHeight : metrics.diagnosticHeight
        Layout.maximumHeight: metrics.diagnosticHeight * 2
        clip: true
        contentWidth: availableWidth

        TextArea {
            id: summaryArea
            text: summaryText.text
            readOnly: true
            selectByMouse: true
            wrapMode: TextEdit.WrapAnywhere
            textFormat: TextEdit.PlainText
        }
    }

    ApplicationWindow {
        id: summaryWindow
        objectName: "applySummaryWindow"
        title: "Review Apply changes"
        width: Math.min(700, Screen.desktopAvailableWidth - metrics.screenHorizontalMargin)
        height: Math.min(620, Screen.desktopAvailableHeight - metrics.screenVerticalMargin)
        minimumWidth: 600
        minimumHeight: 420
        modality: Qt.ApplicationModal
        visible: owner.apply.summaryVisible
        onClosing: owner.apply.cancelApply()

        WindowPreferences {
            layout: backend.layoutUi
            window: summaryWindow
            key: "ApplySummaryDialog"
        }

        onVisibleChanged: {
            if (visible) {
                requestActivate();
                cancelApplyButton.forceActiveFocus();
            }
        }

        Shortcut {
            sequence: "Escape"
            // A hidden transient window can retain its active flag while the
            // next operation takes focus. It must release Escape immediately
            // so cancellation reaches the visible writing progress window.
            enabled: summaryWindow.visible && summaryWindow.active
            onActivated: owner.apply.cancelApply()
        }

        ColumnLayout {
            anchors.fill: parent
            anchors.margins: metrics.windowMargin
            spacing: metrics.spacing

            SummaryText {
                text: owner.apply.summaryText
                Layout.preferredHeight: summaryWindow.height / 3
            }

            RowTable {
                objectName: "applySummaryFilesTable"
                Layout.fillWidth: true
                Layout.fillHeight: true
                Layout.minimumHeight: metrics.diagnosticHeight * 2
                rows: owner.apply.summaryRows
                layout: backend.layoutUi
                preferenceKey: "ApplySummaryDialog/applySummaryFilesTable"
                gridLines: true
                columns: [
                    {key: "file", label: "File", width: 230},
                    {key: "fields", label: "Tag fields", width: 70, detailKey: "fieldNames", sortType: "number"},
                    {key: "decision", label: "Filename decision", width: 130},
                    {key: "final", label: "Final filename", width: 230}
                ]
            }

            ErrorPanel { text: owner.apply.summaryError }

            Label {
                Layout.fillWidth: true
                text: "Only Apply changes starts writing the files in this summary."
                wrapMode: Text.WordWrap
            }

            RowLayout {
                Layout.alignment: Qt.AlignRight

                ActionButton {
                    id: cancelApplyButton
                    objectName: "cancelApplyButton"
                    text: "Cancel"
                    onClicked: owner.apply.cancelApply()
                }

                ActionButton {
                    objectName: "confirmApplyButton"
                    text: "Apply changes"
                    enabled: owner.apply.summaryCanApply
                    onClicked: owner.apply.confirmApply()
                }
            }
        }
    }

    ApplicationWindow {
        id: resultsWindow
        objectName: "applyResultsWindow"
        title: "Apply results"
        width: Math.min(1080, Screen.desktopAvailableWidth - metrics.screenHorizontalMargin)
        height: Math.min(560, Screen.desktopAvailableHeight - metrics.screenVerticalMargin)
        minimumWidth: 600
        minimumHeight: 420
        visible: owner.apply.resultsVisible
        onClosing: owner.apply.closeResults()

        WindowPreferences {
            layout: backend.layoutUi
            window: resultsWindow
            // The original results window was a generic QDialog.
            key: "QDialog"
        }

        Shortcut {
            sequence: "Escape"
            enabled: resultsWindow.visible && resultsWindow.active
            onActivated: owner.apply.closeResults()
        }

        ColumnLayout {
            anchors.fill: parent
            anchors.margins: metrics.windowMargin
            spacing: metrics.spacing

            SummaryText { text: owner.apply.resultsText }

            RowTable {
                objectName: "applyResultsTable"
                Layout.fillWidth: true
                Layout.fillHeight: true
                Layout.minimumHeight: metrics.diagnosticHeight * 2
                rows: owner.apply.resultsRows
                layout: backend.layoutUi
                preferenceKey: "QDialog/applyResultsTable"
                stretchLastColumn: false
                scaleColumnWidths: true
                columns: [
                    {key: "file", label: "File", width: 230},
                    {key: "status", label: "Result", width: 140},
                    {key: "final", label: "Final path", width: 300},
                    {key: "details", label: "Details", width: 400}
                ]
            }

            ActionButton {
                Layout.alignment: Qt.AlignRight
                text: "Close"
                onClicked: owner.apply.closeResults()
            }
        }
    }

    ApplicationWindow {
        id: renameWindow
        objectName: "renameFilesWindow"
        title: "Rename files"
        width: Math.min(1050, Screen.desktopAvailableWidth - metrics.screenHorizontalMargin)
        height: Math.min(600, Screen.desktopAvailableHeight - metrics.screenVerticalMargin)
        minimumWidth: 600
        minimumHeight: 420
        modality: Qt.ApplicationModal
        visible: owner.apply.renameVisible
        onClosing: owner.apply.cancelRename()

        WindowPreferences {
            layout: backend.layoutUi
            window: renameWindow
            key: "RenameFilesDialog"
        }

        Shortcut {
            sequence: "Escape"
            enabled: renameWindow.visible && renameWindow.active
            onActivated: owner.apply.cancelRename()
        }

        ColumnLayout {
            anchors.fill: parent
            anchors.margins: metrics.windowMargin
            spacing: metrics.spacing

            Label {
                Layout.fillWidth: true
                text: "Choose filenames to prepare. The next screen reviews all changes to apply, including metadata."
                wrapMode: Text.WordWrap
            }

            SummaryText {
                objectName: "renameSummaryText"
                text: owner.apply.renameSummary
                // A short status should occupy only its text height. Longer
                // diagnostics retain the shared capped, scrollable area.
                fitContent: true
            }

            RowTable {
                objectName: "renameFilesTable"
                Layout.fillWidth: true
                Layout.fillHeight: true
                Layout.minimumHeight: metrics.diagnosticHeight * 2
                rows: owner.apply.renameRows
                layout: backend.layoutUi
                preferenceKey: "RenameFilesDialog/renameFilesTable"
                gridLines: true
                columns: [
                    {key: "included", label: "Rename", width: 65, checkable: true},
                    {key: "current", label: "Current filename", width: 230},
                    {key: "preview", label: "Preview", width: 290},
                    {key: "validation", label: "Validation", width: 300}
                ]
                onChosen: function(id, included) { owner.apply.setRenameIncluded(id, included); }
            }

            ErrorPanel { text: owner.apply.renameError }

            RowLayout {
                Layout.alignment: Qt.AlignRight

                ActionButton {
                    text: "Cancel"
                    onClicked: owner.apply.cancelRename()
                }

                ActionButton {
                    objectName: "acceptRenameButton"
                    text: "Include chosen files and review changes…"
                    enabled: owner.apply.renameCanAccept
                    onClicked: owner.apply.acceptRename()
                }
            }
        }
    }

    ApplicationWindow {
        id: previewsWindow
        objectName: "renamePreviewsWindow"
        title: "Filename previews"
        width: Math.min(1000, Screen.desktopAvailableWidth - metrics.screenHorizontalMargin)
        height: Math.min(600, Screen.desktopAvailableHeight - metrics.screenVerticalMargin)
        minimumWidth: 600
        minimumHeight: 400
        visible: owner.apply.previewsVisible
        onClosing: owner.apply.closePreviews()

        WindowPreferences {
            layout: backend.layoutUi
            window: previewsWindow
            key: "RenamePreviewDialog"
        }

        Shortcut {
            sequence: "Escape"
            enabled: previewsWindow.visible && previewsWindow.active
            onActivated: owner.apply.closePreviews()
        }

        ColumnLayout {
            anchors.fill: parent
            anchors.margins: metrics.windowMargin
            spacing: metrics.spacing

            Label {
                Layout.fillWidth: true
                text: owner.apply.previewRows.length + " files being reviewed. No files are being changed."
                wrapMode: Text.WordWrap
            }

            RowTable {
                objectName: "renamePreviewTable"
                Layout.fillWidth: true
                Layout.fillHeight: true
                rows: owner.apply.previewRows
                layout: backend.layoutUi
                preferenceKey: "RenamePreviewDialog/renamePreviewTable"
                columns: [
                    {key: "current", label: "Current filename", width: 240},
                    {key: "preview", label: "Proposed filename", width: 300},
                    {key: "decision", label: "Decision", width: 130},
                    {key: "validation", label: "Validation", width: 300}
                ]
            }

            ActionButton {
                Layout.alignment: Qt.AlignRight
                text: "Close"
                onClicked: owner.apply.closePreviews()
            }
        }
    }
}
