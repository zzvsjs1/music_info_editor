import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// Pair a native text input with its label and a compact validation message.
// Other forms can provide their own label, input name and error without
// replacing Qt's normal field appearance or duplicating the invalid state.
ColumnLayout {
    id: root

    property string labelText: ""
    property string errorText: ""
    property alias text: input.text
    property alias inputControl: input
    property alias inputObjectName: input.objectName
    readonly property bool invalid: errorText.length > 0
    readonly property color errorColour: input.palette.window.hslLightness < 0.5
        ? "#ff8078" : "#c32f25"

    signal accepted()
    signal edited()

    spacing: 4

    Label {
        Layout.fillWidth: true
        visible: root.labelText.length > 0
        text: root.labelText
        wrapMode: Text.WordWrap
    }

    Item {
        Layout.fillWidth: true
        implicitHeight: input.implicitHeight

        // The native Windows frame has a different corner shape. While the
        // field is invalid, paint one filled outline behind the editable text
        // so no grey pixel from that frame remains at the rounded corners.
        Rectangle {
            objectName: root.inputObjectName + "InvalidBorder"
            anchors.fill: parent
            radius: 3
            color: input.palette.base
            border.width: 1
            border.color: root.errorColour
            visible: root.invalid
        }

        TextField {
            id: input

            anchors.fill: parent
            leftPadding: 8
            rightPadding: 8
            topPadding: 4
            bottomPadding: 4
            background.visible: !root.invalid
            Accessible.name: root.labelText
            Accessible.description: root.invalid ? root.errorText : ""
            onAccepted: root.accepted()
            onTextEdited: root.edited()
        }
    }

    // A long future message remains copyable, with a visible scroll bar when
    // it exceeds three lines. A short error uses only its text height.
    AppScrollView {
        Layout.fillWidth: true
        Layout.preferredHeight: Math.min(48, message.implicitHeight)
        Layout.maximumHeight: 48
        visible: root.invalid
        contentWidth: availableWidth
        background: null
        padding: 0
        clip: true

        TextArea {
            id: message

            objectName: root.inputObjectName + "Error"
            text: root.errorText
            color: root.errorColour
            font: input.font
            background: null
            padding: 0
            leftPadding: 0
            rightPadding: 0
            readOnly: true
            selectByMouse: true
            wrapMode: TextEdit.WrapAnywhere
            textFormat: TextEdit.PlainText
            Accessible.name: root.errorText
        }
    }
}
