import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// Pair native text editing with a shared frame, label and compact validation
// message. Other forms can supply their own editor without duplicating the
// normal, focused and invalid borders.
ColumnLayout {
    id: root

    property string labelText: ""
    property string errorText: ""
    property string accessibleLabel: labelText
    // A specialised input can keep its existing completion/key handlers while
    // sharing this field's invalid border and bounded, copyable diagnostic.
    property Item editor: null
    property alias text: input.text
    readonly property Item inputControl: editor || input
    property alias inputObjectName: input.objectName
    property alias placeholderText: input.placeholderText
    property alias selectByMouse: input.selectByMouse
    readonly property string effectiveObjectName: inputControl.objectName
    readonly property bool invalid: errorText.length > 0
    readonly property color errorColour: inputControl.palette.window.hslLightness < 0.5
        ? "#ff8078" : "#c32f25"

    signal accepted()
    signal edited()
    signal finishedEditing()

    spacing: 4

    Label {
        Layout.fillWidth: true
        visible: root.labelText.length > 0
        text: root.labelText
        wrapMode: Text.WordWrap
    }

    Item {
        id: inputFrame

        Layout.fillWidth: true
        implicitHeight: root.inputControl.implicitHeight

        // Reparent only the optional editor; the ordinary input keeps its
        // aliases and behaviour for forms already using this component.
        Binding { target: root.editor; property: "parent"; value: inputFrame; when: root.editor !== null }
        Binding {
            target: root.inputControl.background
            property: "visible"
            value: false
        }

        // Windows' native frame can lose edge pixels at fractional display
        // scales, even when its size matches the input. Paint one outline for
        // every state; retain the actual editor's input and keyboard handling.
        Rectangle {
            objectName: root.effectiveObjectName + "Border"
            anchors.fill: parent
            radius: 3
            color: root.inputControl.palette.base
            border.width: 1
            border.color: root.invalid ? root.errorColour
                : root.inputControl.activeFocus ? root.inputControl.palette.highlight
                : root.inputControl.palette.mid
        }

        AppTextField {
            id: input

            anchors.fill: parent
            visible: root.editor === null
            Accessible.name: root.accessibleLabel
            Accessible.description: root.invalid ? root.errorText : ""
            onAccepted: root.accepted()
            onTextEdited: root.edited()
            onEditingFinished: root.finishedEditing()
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

            objectName: root.effectiveObjectName + "Error"
            text: root.errorText
            color: root.errorColour
            font: root.inputControl.font
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
