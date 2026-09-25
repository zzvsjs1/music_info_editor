import QtQuick
import QtQuick.Controls

// Give every action the same modest breathing room while leaving Qt's active
// Controls style to draw the background, focus ring and disabled state.
Button {
    UiMetrics {
        id: metrics
    }

    leftPadding: metrics.buttonHorizontalPadding
    rightPadding: metrics.buttonHorizontalPadding
    topPadding: metrics.buttonVerticalPadding
    bottomPadding: metrics.buttonVerticalPadding

    // Content can grow with larger desktop fonts or translated labels. The
    // minimum keeps short actions aligned with their neighbours.
    implicitWidth: Math.max(metrics.buttonMinimumWidth,
        implicitBackgroundWidth + leftInset + rightInset,
        implicitContentWidth + leftPadding + rightPadding)
    implicitHeight: Math.max(metrics.buttonMinimumHeight,
        implicitBackgroundHeight + topInset + bottomInset,
        implicitContentHeight + topPadding + bottomPadding)
}
