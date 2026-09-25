import QtQuick
import QtQuick.Controls

// Keep the active Qt style's arrow, popup and keyboard behaviour. Only the
// geometry is shared with neighbouring form fields and ordinary actions.
ComboBox {
    id: control

    UiMetrics { id: metrics }
    TextMetrics { id: selectedText; font: control.font; text: control.displayText }

    // Windows paints its arrow in the native background; other styles expose
    // an indicator item. Reserve either arrow before measuring the text width.
    readonly property real arrowWidth: indicator ? indicator.width : 20
    leftPadding: metrics.controlHorizontalPadding + (mirrored ? arrowWidth : 0)
    rightPadding: metrics.controlHorizontalPadding + (mirrored ? 0 : arrowWidth)
    topPadding: metrics.controlVerticalPadding
    bottomPadding: metrics.controlVerticalPadding
    implicitContentWidthPolicy: ComboBox.WidestText

    implicitWidth: Math.max(implicitBackgroundWidth + leftInset + rightInset,
        Math.max(implicitContentWidth, selectedText.advanceWidth) + leftPadding + rightPadding)
    implicitHeight: Math.max(metrics.controlMinimumHeight,
        implicitBackgroundHeight + topInset + bottomInset,
        implicitContentHeight + topPadding + bottomPadding)

    // Some styles add padding inside their embedded text editor as well. Use
    // one padding layer so the same font produces the same control height on
    // Windows and Fusion, while retaining each style's native content item.
    Binding {
        target: control.contentItem
        property: "topPadding"
        value: 0
        when: control.contentItem && control.contentItem.hasOwnProperty("topPadding")
    }

    Binding {
        target: control.contentItem
        property: "bottomPadding"
        value: 0
        when: control.contentItem && control.contentItem.hasOwnProperty("bottomPadding")
    }

    Binding {
        target: control.contentItem
        property: "leftPadding"
        value: 0
        when: control.contentItem && control.contentItem.hasOwnProperty("leftPadding")
    }

    Binding {
        target: control.contentItem
        property: "rightPadding"
        value: 0
        when: control.contentItem && control.contentItem.hasOwnProperty("rightPadding")
    }
}
