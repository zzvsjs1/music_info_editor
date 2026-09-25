import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ApplicationWindow {
    required property string message
    visible: true
    width: 640
    height: 320
    title: "Metadata Polisher cannot start"
    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 12
        AppScrollView {
            Layout.fillWidth: true
            Layout.fillHeight: true
            TextArea {
                text: message
                readOnly: true
                selectByMouse: true
                wrapMode: TextEdit.Wrap
                textFormat: TextEdit.PlainText
            }
        }

        ActionButton {
            text: "Close"
            Layout.alignment: Qt.AlignRight
            onClicked: Qt.quit()
        }
    }
}
