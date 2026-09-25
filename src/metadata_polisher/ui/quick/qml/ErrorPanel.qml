import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

AppScrollView {
    id: root
    property string text: ""
    visible: text.length > 0
    implicitHeight: 60
    Layout.fillWidth: true
    Layout.minimumHeight: 60
    Layout.preferredHeight: 60
    Layout.maximumHeight: 60
    contentWidth: availableWidth
    clip: true
    TextArea {
        text: root.text
        readOnly: true
        selectByMouse: true
        wrapMode: TextEdit.WrapAnywhere
        textFormat: TextEdit.PlainText
    }
}
