import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ApplicationWindow {
    id: window

    required property QtObject lookup

    signal restoreEditorFocus(string fileId)

    function assignFromEditor(fileId, providerIndex, hadFocus) {
        lookup.assignTrack(fileId, providerIndex)

        if (hadFocus) {
            // Publishing the immutable draft replaces ListView delegates. Send
            // one focus request to the replacement editor after that refresh;
            // unrelated facade notifications never trigger this request.
            Qt.callLater(function() {
                if (window.active && lookup.mappingVisible) {
                    window.restoreEditorFocus(fileId)
                }
            })
        }
    }

    objectName: "quickMappingWindow"
    title: "Review track mapping"
    width: Math.min(1100, Screen.desktopAvailableWidth - metrics.screenHorizontalMargin)
    height: Math.min(600, Screen.desktopAvailableHeight - metrics.screenVerticalMargin)
    minimumWidth: 720
    minimumHeight: 400
    modality: Qt.WindowModal
    visible: lookup.mappingVisible
    onClosing: lookup.cancelMapping()

    UiMetrics {
        id: metrics
    }

    WindowPreferences {
        layout: lookup.layoutUi
        window: window
        key: "TrackMappingDialog"
    }

    Shortcut {
        sequence: "Escape"
        enabled: window.visible && window.active
        onActivated: lookup.cancelMapping()
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: metrics.windowMargin
        spacing: metrics.spacing

        Label {
            Layout.fillWidth: true
            wrapMode: Text.WordWrap
            text: lookup.mappingInstructions
        }

        RowTable {
            id: table
            objectName: "quickMappingTable"
            Layout.fillWidth: true
            Layout.fillHeight: true
            rows: lookup.mappingRows
            layout: lookup.layoutUi
            preferenceKey: "TrackMappingDialog/trackMappingTable"
            scaleColumnWidths: true
            columns: [
                { key: "file", label: "Local file", width: 230 },
                { key: "title", label: "Local title", width: 180 },
                { key: "duration", label: "Duration", width: 70 },
                { key: "assignment", label: "Provider track", width: 280, custom: true },
                { key: "evidence", label: "Assignment evidence", width: 220 }
            ]

            // Only the assignment cell is editable. The immutable facade owns
            // the draft and one-to-one checks; row positions never identify files.
            cellDelegate: Component {
                ComboBox {
                    id: selector

                    objectName: "mappingTrackSelector_" + rowData.id
                    model: lookup.mappingTracks
                    textRole: "label"
                    valueRole: "value"
                    currentIndex: rowData.track + 1
                    Accessible.name: "Provider track for " + rowData.file

                    onActiveFocusChanged: {
                        if (activeFocus) {
                            // Keyboard editing and the full-evidence pane follow
                            // the same local file, even after the draft refreshes.
                            table.currentId = rowData.id
                        }
                    }

                    Connections {
                        target: window

                        function onRestoreEditorFocus(fileId) {
                            if (fileId === rowData.id) {
                                selector.forceActiveFocus()
                            }
                        }
                    }

                    onActivated: {
                        window.assignFromEditor(rowData.id, currentValue, activeFocus)

                        // A rejected duplicate keeps the last valid draft. Bind
                        // back to it without dispatching a second edit request.
                        currentIndex = Qt.binding(function() { return rowData.track + 1 })
                    }
                }
            }
        }

        ErrorPanel {
            text: lookup.mappingEvidence + (lookup.mappingError ? "\n" + lookup.mappingError : "")
        }

        RowLayout {
            Layout.alignment: Qt.AlignRight
            spacing: metrics.spacing

            ActionButton {
                objectName: "quickMappingAccept"
                text: "OK"
                onClicked: lookup.commitMapping()
            }

            ActionButton {
                text: "Cancel"
                onClicked: lookup.cancelMapping()
            }
        }
    }
}
