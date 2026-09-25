import QtQuick
import QtQuick.Templates as T

// Paint both scroll directions from the same palette surfaces. The Windows
// native groove contains transparent edge pixels at fractional display scales;
// a complete template keeps those pixels inside an owned, opaque track.
T.ScrollBar {
    id: bar

    UiMetrics { id: metrics }

    implicitWidth: metrics.scrollBarThickness
    implicitHeight: metrics.scrollBarThickness
    readonly property bool highContrast: Qt.styleHints.accessibility.contrastPreference === Qt.HighContrast
    readonly property int stepButtonLength: metrics.scrollBarThickness
    readonly property bool stepButtonsVisible: (policy === T.ScrollBar.AlwaysOn || size < 1)
        && (orientation === Qt.Horizontal ? width : height) >= 4 * stepButtonLength

    minimumSize: orientation === Qt.Horizontal ? height / width : width / height
    padding: 0
    leftPadding: orientation === Qt.Horizontal && stepButtonsVisible ? stepButtonLength : 0
    rightPadding: leftPadding
    topPadding: orientation === Qt.Vertical && stepButtonsVisible ? stepButtonLength : 0
    bottomPadding: topPadding
    policy: T.ScrollBar.AsNeeded
    visible: policy !== T.ScrollBar.AlwaysOff

    background: Rectangle {
        visible: bar.policy === T.ScrollBar.AlwaysOn || bar.size < 1
        color: bar.palette.window
    }

    contentItem: Item {
        visible: bar.policy === T.ScrollBar.AlwaysOn || bar.size < 1

        Rectangle {
            anchors.centerIn: parent
            width: bar.orientation === Qt.Vertical
                ? metrics.scrollBarThumbThickness
                : Math.max(0, parent.width - 2 * metrics.scrollBarThumbInset)
            height: bar.orientation === Qt.Horizontal
                ? metrics.scrollBarThumbThickness
                : Math.max(0, parent.height - 2 * metrics.scrollBarThumbInset)
            radius: metrics.scrollBarThumbThickness / 2
            // Windows high contrast themes can make palette.mid blend into the
            // track. The text colour remains distinct from palette.window.
            color: bar.highContrast ? bar.palette.text
                : (bar.pressed ? bar.palette.highlight : bar.palette.mid)
        }
    }

    // Native Windows scrollbars offer a one-step action at both ends. Keep
    // those hit targets separate from the draggable thumb's padded track.
    component StepButton: Item {
        property bool forward: false

        width: bar.orientation === Qt.Horizontal ? bar.stepButtonLength : bar.width
        height: bar.orientation === Qt.Vertical ? bar.stepButtonLength : bar.height
        x: forward && bar.orientation === Qt.Horizontal ? bar.width - width : 0
        y: forward && bar.orientation === Qt.Vertical ? bar.height - height : 0
        z: 2
        visible: bar.stepButtonsVisible

        Text {
            anchors.centerIn: parent
            text: bar.orientation === Qt.Vertical
                ? (forward ? "\u25BC" : "\u25B2")
                : (forward ? "\u25B6" : "\u25C0")
            font.pixelSize: Math.max(8, bar.stepButtonLength - 4)
            color: bar.palette.text
            visible: stepMouse.containsMouse
        }

        MouseArea {
            id: stepMouse
            anchors.fill: parent
            hoverEnabled: true
            onClicked: {
                if (forward) {
                    bar.increase();
                } else {
                    bar.decrease();
                }
            }
        }
    }

    StepButton { forward: false }
    StepButton { forward: true }
}
