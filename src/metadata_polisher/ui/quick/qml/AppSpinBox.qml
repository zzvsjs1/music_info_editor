import QtQuick
import QtQuick.Controls

SpinBox {
    id: control

    UiMetrics { id: metrics }
    FontMetrics { id: textMetrics; font: control.font }

    // Windows draws the frame through an embedded TextField. Pad that editor
    // internally, keeping its frame aligned with the native arrow buttons.
    // Styles with a plain TextInput instead reserve space on the SpinBox.
    readonly property bool framedEditor: contentItem && contentItem.hasOwnProperty("background")
    readonly property real arrowWidth: Math.max(up.indicator ? up.indicator.width : 0,
        down.indicator ? down.indicator.width : 0)
    // Let each style retain its left/right arrow allocation. Some place both
    // arrows on the right; others put one button on either side of the editor.
    padding: framedEditor ? 0 : metrics.controlHorizontalPadding
    topPadding: framedEditor ? 0 : metrics.controlVerticalPadding
    bottomPadding: framedEditor ? 0 : metrics.controlVerticalPadding

    implicitHeight: Math.max(metrics.controlMinimumHeight,
        textMetrics.height + 2 * metrics.controlVerticalPadding,
        implicitBackgroundHeight + topInset + bottomInset,
        implicitContentHeight + topPadding + bottomPadding,
        up.implicitIndicatorHeight, down.implicitIndicatorHeight)

    Binding {
        target: control.contentItem
        property: "topPadding"
        value: control.framedEditor ? metrics.controlVerticalPadding : 0
        when: control.contentItem && control.contentItem.hasOwnProperty("topPadding")
    }

    Binding {
        target: control.contentItem
        property: "bottomPadding"
        value: control.framedEditor ? metrics.controlVerticalPadding : 0
        when: control.contentItem && control.contentItem.hasOwnProperty("bottomPadding")
    }

    Binding {
        target: control.contentItem
        property: "leftPadding"
        value: control.framedEditor ? metrics.controlHorizontalPadding : 0
        when: control.contentItem && control.contentItem.hasOwnProperty("leftPadding")
    }

    Binding {
        target: control.contentItem
        property: "rightPadding"
        value: control.framedEditor ? metrics.controlHorizontalPadding + control.arrowWidth : 0
        when: control.contentItem && control.contentItem.hasOwnProperty("rightPadding")
    }
}
