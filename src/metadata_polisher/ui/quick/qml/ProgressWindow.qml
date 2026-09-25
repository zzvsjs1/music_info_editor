import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Window

ApplicationWindow {
    id: progressWindow
    required property QtObject backend
    property bool wasBusy: false
    property bool wasFailed: false
    property double startedAt: 0
    property int elapsedSeconds: 0
    objectName: "operationProgressWindow"
    title: backend.progressTitle
    width: Math.min(650, Screen.desktopAvailableWidth - 32)
    height: Math.min(330, Screen.desktopAvailableHeight - 64)
    minimumWidth: 460
    minimumHeight: 300
    modality: Qt.NonModal
    visible: backend.progressVisible

    UiMetrics {
        id: metrics
    }

    WindowPreferences {
        layout: backend.layoutUi
        window: progressWindow
        key: "OperationProgressDialog"
    }

    // A failed or cancelled operation keeps its complete diagnostics visible.
    onClosing: function(event) {
        if (backend.busy && backend.progressKind === "apply") {
            event.accepted = false;
            backend.cancelScan();
        } else {
            backend.hideProgress();
        }
    }

    Connections {
        target: backend
        function onChanged() {
            if (backend.busy && !progressWindow.wasBusy) {
                progressWindow.startedAt = Date.now();
                progressWindow.elapsedSeconds = 0;
                detailsButton.checked = false;
                detailsScroll.followTail = true;
                detailsScroll.readPosition = 0;
            }

            if (backend.progressFailed && !progressWindow.wasFailed) {
                // A concise stage keeps the controls reachable. Present the
                // copyable diagnostic document immediately when work fails.
                detailsButton.checked = true;
            }

            detailsScroll.updateLog();
            progressWindow.wasBusy = backend.busy;
            progressWindow.wasFailed = backend.progressFailed;
        }
    }

    Timer {
        interval: 1000
        repeat: true
        running: backend.busy
        onTriggered: progressWindow.elapsedSeconds = Math.floor((Date.now() - progressWindow.startedAt) / 1000)
    }

    Shortcut {
        sequence: "Escape"
        enabled: progressWindow.visible && progressWindow.active
        onActivated: backend.hideProgress()
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: metrics.windowMargin
        spacing: metrics.spacing
        Label {
            Layout.fillWidth: true
            text: backend.progressKind === "apply"
                  ? "Only the confirmed changes are being applied. Cancellation waits for a safe file boundary."
                  : "No files are being changed."
            wrapMode: Text.WordWrap
        }
        Label {
            objectName: "progressStage"
            Layout.fillWidth: true
            text: backend.progressStage
            textFormat: Text.PlainText
            elide: Text.ElideRight
        }

        Label {
            objectName: "progressContext"
            Layout.fillWidth: true
            text: backend.progressContext
            textFormat: Text.PlainText
            // The tooltip retains the complete context. Keep the current item
            // to one line so a long filename cannot push Cancel out of view.
            elide: Text.ElideMiddle
            ToolTip.visible: contextHover.hovered
            ToolTip.text: text
            HoverHandler {
                id: contextHover
            }
            visible: text.length > 0
        }

        ProgressBar {
            Layout.fillWidth: true
            indeterminate: backend.busy && backend.progress < 0
            value: Math.max(0, backend.progress)
        }

        Label {
            objectName: "progressCount"
            text: backend.progressCount
            visible: text.length > 0
        }
        Label {
            text: "Elapsed: " + Math.floor(progressWindow.elapsedSeconds / 60) + ":"
                  + String(progressWindow.elapsedSeconds % 60).padStart(2, "0")
        }
        ActionButton {
            id: detailsButton
            text: checked ? "Hide details" : "Show details"
            checkable: true
        }
        AppScrollView {
            id: detailsScroll
            objectName: "progressDetailsScroll"
            property bool followTail: true
            property bool updatingLog: false
            property real readPosition: 0
            Layout.fillWidth: true
            Layout.fillHeight: true
            visible: detailsButton.checked
            clip: true
            contentWidth: availableWidth
            Component.onCompleted: updateLog()

            function settleLogScroll() {
                const view = contentItem;
                const bottom = Math.max(0, view.contentHeight - view.height);
                view.contentY = followTail ? bottom : Math.min(readPosition, bottom);
                updatingLog = false;
            }

            function updateLog() {
                if (progressLog.text === backend.progressLog) {
                    return;
                }

                // Replacing a TextArea document can move its cursor/viewport.
                // Capture the user's reading position before publishing text,
                // then restore it once Qt has measured the updated document.
                updatingLog = true;
                progressLog.text = backend.progressLog;
                Qt.callLater(settleLogScroll);
            }

            onVisibleChanged: {
                if (visible) {
                    Qt.callLater(settleLogScroll);
                }
            }

            Connections {
                target: detailsScroll.contentItem

                function onContentYChanged() {
                    if (!detailsScroll.updatingLog) {
                        const view = detailsScroll.contentItem;
                        const bottom = Math.max(0, view.contentHeight - view.height);
                        detailsScroll.followTail = view.contentY >= bottom - 2;
                        detailsScroll.readPosition = Math.max(0, view.contentY);
                    }
                }
            }

            TextArea {
                id: progressLog
                objectName: "progressLog"
                readOnly: true
                selectByMouse: true
                wrapMode: TextEdit.WrapAnywhere
                textFormat: TextEdit.PlainText
                Accessible.name: "Operation details"
            }
        }
        Item {
            Layout.fillHeight: true
            visible: !detailsButton.checked
        }
        RowLayout {
            Layout.fillWidth: true
            ActionButton {
                text: "Copy details"
                onClicked: {
                    progressLog.selectAll();
                    progressLog.copy();
                    progressLog.deselect();
                }
            }

            Item {
                Layout.fillWidth: true
            }
            ActionButton {
                objectName: "progressCancelButton"
                text: backend.cancelling ? "Cancelling…" : "Cancel"
                enabled: backend.busy && !backend.cancelling
                visible: backend.busy
                onClicked: backend.cancelScan()
            }
            ActionButton {
                text: backend.busy ? "Hide" : "Close"
                visible: !backend.busy || backend.progressKind !== "apply"
                onClicked: backend.hideProgress()
            }
        }
    }
}
