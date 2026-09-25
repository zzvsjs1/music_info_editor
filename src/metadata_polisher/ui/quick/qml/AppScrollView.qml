import QtQuick
import QtQuick.Controls

// Keep Qt's ScrollView viewport, padding and horizontal overflow handling.
// Its vertical bar uses the same painted track as the table controls.
ScrollView {
    id: root

    ScrollBar.vertical: AppScrollBar {
        parent: root
        x: root.mirrored ? 0 : root.width - width
        y: 0
        height: root.height - (root.ScrollBar.horizontal.visible
            ? root.ScrollBar.horizontal.height : 0)
        active: root.ScrollBar.horizontal.active
    }
}
