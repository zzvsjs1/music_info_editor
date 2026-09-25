import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ApplicationWindow {
    id: root
    required property QtObject backend
    objectName: "helpWindow"
    title: "How to review metadata"
    width: Math.min(700, Screen.width - 32)
    height: Math.min(570, Screen.height - 64)
    minimumWidth: 420
    minimumHeight: 320
    visible: false
    flags: Qt.Dialog

    WindowPreferences {
        layout: backend.layoutUi
        window: root
        key: "WorkflowHelpDialog"
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
                text: root.backend.helpText
                textFormat: TextEdit.RichText
                readOnly: true
                selectByMouse: true
                wrapMode: TextEdit.Wrap
                Accessible.name: "Workflow and keyboard help"
            }
        }

        ActionButton {
            text: "Close"
            Layout.alignment: Qt.AlignRight
            onClicked: root.close()
        }
    }
}
