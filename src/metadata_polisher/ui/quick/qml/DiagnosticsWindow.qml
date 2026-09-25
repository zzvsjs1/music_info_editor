import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ApplicationWindow {
    id: root
    required property QtObject backend
    objectName: "diagnosticsWindow"
    title: "Diagnostic summary"
    width: Math.min(950, Screen.width - 32)
    height: Math.min(650, Screen.height - 64)
    minimumWidth: 600
    minimumHeight: 360
    visible: false
    flags: Qt.Dialog

    WindowPreferences {
        layout: backend.layoutUi
        window: root
        key: "DiagnosticsDialog"
    }

    Shortcut {
        sequence: "Escape"
        enabled: root.visible && root.active
        onActivated: root.close()
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 12

        AppScrollView {
            Layout.fillWidth: true
            Layout.fillHeight: true
            clip: true

            TextArea {
                text: root.backend.diagnosticSummary
                textFormat: TextEdit.PlainText
                readOnly: true
                selectByMouse: true
                wrapMode: TextEdit.Wrap
                Accessible.name: "Diagnostic summary"
            }
        }

        Flow {
            Layout.fillWidth: true
            spacing: 8
            ActionButton {
                text: "Copy diagnostic summary"
                onClicked: root.backend.copyDiagnostics()
            }

            ActionButton {
                text: "Open log folder"
                onClicked: root.backend.openLogs()
            }

            ActionButton {
                text: "Why this match?"
                enabled: root.backend.releaseEvidence.length > 0
                onClicked: {
                    explanation.title = text
                    explanation.rows = root.backend.releaseEvidence
                    explanation.show()
                    explanation.raise()
                    explanation.requestActivate()
                }
            }

            ActionButton {
                text: "Why this track mapping?"
                enabled: root.backend.trackEvidence.length > 0
                onClicked: {
                    explanation.title = text
                    explanation.rows = root.backend.trackEvidence
                    explanation.show()
                    explanation.raise()
                    explanation.requestActivate()
                }
            }
        }

        RowLayout {
            Layout.fillWidth: true
            Item {
                Layout.fillWidth: true
            }

            ActionButton {
                text: "Close"
                onClicked: root.close()
            }
        }
    }

    EvidenceWindow {
        id: explanation
        transientParent: root
        layout: root.backend.layoutUi
    }
}
