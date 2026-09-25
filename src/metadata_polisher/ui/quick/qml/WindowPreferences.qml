import QtQuick

// Register once with the Python preference owner. It observes native show/hide
// events and also captures still-open dialogues when the application exits.
QtObject {
    required property QtObject layout
    required property QtObject window
    required property string key

    Component.onCompleted: {
        // Evidence can also be opened without a settings owner in an embedded
        // view. That use remains transient and does not invent a settings file.
        if (layout) {
            layout.watchWindow(window, key);
        }
    }
}
