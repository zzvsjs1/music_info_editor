import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Window

ApplicationWindow {
    id: root

    property var rows: []
    property QtObject layout: null

    objectName: "matchExplanationWindow"
    title: "Why this match?"
    width: Math.min(880, Screen.desktopAvailableWidth - metrics.screenHorizontalMargin)
    height: Math.min(600, Screen.desktopAvailableHeight - metrics.screenVerticalMargin)
    minimumWidth: 460
    minimumHeight: 320
    visible: false
    flags: Qt.Dialog

    onRowsChanged: {
        if (evidenceTable) {
            evidenceTable.currentId = ""
            evidenceTable.currentIndex = -1
        }
    }

    onVisibleChanged: {
        if (visible) {
            evidenceTable.forceActiveFocus()
        }
    }

    UiMetrics {
        id: metrics
    }

    WindowPreferences {
        layout: root.layout
        window: root
        key: "MatchExplanationDialog"
    }

    Shortcut {
        sequence: "Escape"
        enabled: root.active
        onActivated: root.close()
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: metrics.windowMargin
        spacing: metrics.spacing

        Label {
            Layout.fillWidth: true
            text: "The score is a deterministic ranking, not a probability."
            wrapMode: Text.WordWrap
        }

        RowTable {
            id: evidenceTable
            objectName: "matchEvidenceTable"
            Layout.fillWidth: true
            Layout.fillHeight: true
            layout: root.layout
            preferenceKey: "MatchExplanationDialog/matchEvidenceTable"
            scaleColumnWidths: true
            showDetails: false
            columns: [
                { key: "reason", label: "Reason", width: 210 },
                { key: "contribution", label: "Contribution", width: 95, sortType: "number" },
                { key: "detail", label: "Detail", width: 420 }
            ]

            // This read-only snapshot starts in supplied order and retains
            // zero-valued reasons. Its ordinal identity survives visual sorting
            // and safely distinguishes repeated reason codes.
            rows: root.rows.map(function(row, index) {
                return { id: String(index), reason: row.reason,
                         contribution: row.contribution, detail: row.detail }
            })
        }

        // Full evidence remains copyable when the table elides a long cell.
        // Its fixed allocation leaves Close reachable at constrained sizes.
        AppScrollView {
            Layout.fillWidth: true
            Layout.preferredHeight: metrics.diagnosticHeight
            Layout.maximumHeight: metrics.diagnosticHeight
            clip: true
            contentWidth: availableWidth

            TextArea {
                objectName: "matchEvidenceDetails"
                text: evidenceTable.currentRow ? evidenceTable.currentRow.detail : ""
                placeholderText: "Select a reason to read its full explanation."
                textFormat: TextEdit.PlainText
                readOnly: true
                selectByMouse: true
                wrapMode: TextEdit.WrapAnywhere
            }
        }

        ActionButton {
            text: "Close"
            Layout.alignment: Qt.AlignRight
            onClicked: root.close()
        }
    }
}
