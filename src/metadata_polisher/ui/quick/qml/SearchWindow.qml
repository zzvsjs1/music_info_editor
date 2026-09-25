import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ApplicationWindow {
    id: window

    required property QtObject lookup

    objectName: "quickSearchWindow"
    title: "Edit search terms"
    width: 600
    height: 310
    minimumWidth: 440
    minimumHeight: 230
    modality: Qt.WindowModal
    visible: lookup.searchVisible
    onClosing: lookup.cancelSearch()
    onVisibleChanged: {
        if (visible) {
            album.text = lookup.searchAlbum
            artists.text = lookup.searchArtists
            year.value = lookup.searchYear
            album.forceActiveFocus()
        }
    }

    UiMetrics {
        id: metrics
    }

    WindowPreferences {
        layout: lookup.layoutUi
        window: window
        key: "SearchTermsDialog"
    }

    // Enter and the standard acceptance button publish the same captured draft.
    // The facade rejects stale session state and keeps any error visible here.
    function acceptDraft() {
        lookup.commitSearch(album.text, artists.text, year.value)
    }

    Shortcut {
        sequence: "Escape"
        enabled: window.visible && window.active
        onActivated: lookup.cancelSearch()
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: metrics.windowMargin
        spacing: metrics.spacing

        GridLayout {
            Layout.fillWidth: true
            columns: 2
            columnSpacing: metrics.spacing
            rowSpacing: metrics.spacing

            Label {
                text: "Album"
            }

            TextField {
                id: album
                objectName: "quickSearchAlbum"
                Layout.fillWidth: true
                leftPadding: 5
                rightPadding: 5
                onAccepted: window.acceptDraft()
            }

            Label {
                text: "Artists (separate with ;)"
            }

            TextField {
                id: artists
                objectName: "quickSearchArtists"
                Layout.fillWidth: true
                leftPadding: 5
                rightPadding: 5
                onAccepted: window.acceptDraft()
            }

            Label {
                text: "Year"
            }

            SpinBox {
                id: year
                objectName: "quickSearchYear"
                from: 0
                to: 9999
                editable: true
                textFromValue: function(value) {
                    return value === 0 ? "Any year" : String(value)
                }

                valueFromText: function(text) {
                    return text === "Any year" ? 0 : Number(text)
                }

                // Commit the editable text before accepting. The number may
                // otherwise still contain its previous value while it has focus.
                Keys.onReturnPressed: {
                    value = valueFromText(contentItem.text)
                    window.acceptDraft()
                }

                Keys.onEnterPressed: {
                    value = valueFromText(contentItem.text)
                    window.acceptDraft()
                }
            }
        }

        ErrorPanel {
            text: lookup.searchError
        }

        Item {
            Layout.fillHeight: true
        }

        RowLayout {
            Layout.alignment: Qt.AlignRight
            spacing: metrics.spacing

            ActionButton {
                objectName: "quickSearchAccept"
                text: "OK"
                onClicked: window.acceptDraft()
            }

            ActionButton {
                text: "Cancel"
                onClicked: lookup.cancelSearch()
            }
        }
    }
}
