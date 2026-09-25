import QtQuick

// Shared logical-pixel defaults for the desktop port. Qt applies display scaling;
// individual screens can still use content-driven sizes and user preferences.
QtObject {
    readonly property int spacingCompact: 3
    readonly property int spacingSmall: 4
    readonly property int spacing: 6
    readonly property int spacingLarge: 12
    readonly property int windowMargin: 12
    readonly property int diagnosticHeight: 96
    readonly property int buttonMinimumWidth: 75
    readonly property int buttonMinimumHeight: controlMinimumHeight
    readonly property int buttonHorizontalPadding: 12
    readonly property int buttonVerticalPadding: controlVerticalPadding
    // Ordinary actions and form controls share a readable desktop target. Table
    // headers and inline editors retain their separate, compact geometry.
    readonly property int controlMinimumHeight: 36
    readonly property int controlHorizontalPadding: 12
    readonly property int controlVerticalPadding: 7
    readonly property int controlSpacing: 8
    readonly property int formRowSpacing: 10
    readonly property int scrollBarThickness: 14
    readonly property int scrollBarThumbThickness: 5
    readonly property int scrollBarThumbInset: 2

    // Leave room for native window decorations and the desktop taskbar.
    readonly property int screenHorizontalMargin: 32
    readonly property int screenVerticalMargin: 64

    readonly property int mainWidth: 1180
    readonly property int mainHeight: 820
    readonly property int mainCompactHeight: 650
    readonly property int albumPaneWidth: 300
    readonly property int reviewWidth: 1080
    readonly property int reviewHeight: 760

    // Manual input is a compact owned window, matching the Widgets workflow.
    readonly property int manualWidth: 320
    readonly property int manualHeight: 310
    readonly property int manualPositionHeight: 180
    readonly property int manualMinimumWidth: 280
    readonly property int manualMinimumHeight: 160

    // Stable column order follows the shared Python models. Default widths are
    // adjusted by each table's font metrics before applying saved user widths.
    readonly property var albumColumns: [190, 100]
    readonly property var fileColumns: [56, 100, 260, 65, 65, 240, 160, 160, 70, 65, 90, 100]
    readonly property var hiddenFileColumns: [6, 7, 10, 11]
    readonly property var reviewColumns: [100, 108, 180, 180, 180, 180]
    readonly property var hiddenReviewColumns: [5]
}
