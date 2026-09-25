import QtQuick
import QtQuick.Controls

// Preserve native width and horizontal text scrolling: a long path must never
// enlarge its form. Height and interior clearance follow the shared controls.
TextField {
    id: control

    UiMetrics { id: metrics }
    FontMetrics { id: textMetrics; font: control.font }

    leftPadding: metrics.controlHorizontalPadding
    rightPadding: metrics.controlHorizontalPadding
    topPadding: metrics.controlVerticalPadding
    bottomPadding: metrics.controlVerticalPadding

    implicitHeight: Math.max(metrics.controlMinimumHeight,
        implicitBackgroundHeight + topInset + bottomInset,
        Math.max(contentHeight, textMetrics.height) + topPadding + bottomPadding)
}
