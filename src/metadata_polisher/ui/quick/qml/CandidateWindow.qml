import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ApplicationWindow {
    id: window

    required property QtObject lookup

    objectName: "quickCandidateWindow"
    title: "Choose metadata release"
    width: Math.min(1200, Screen.desktopAvailableWidth - metrics.screenHorizontalMargin)
    height: Math.min(650, Screen.desktopAvailableHeight - metrics.screenVerticalMargin)
    minimumWidth: 720
    minimumHeight: 420
    visible: lookup.candidateVisible
    color: palette.window
    onClosing: lookup.closeCandidates()

    UiMetrics {
        id: metrics
    }

    WindowPreferences {
        layout: lookup.layoutUi
        window: window
        key: "CandidateDialog"
    }

    Shortcut {
        sequence: "Escape"
        enabled: window.visible && window.active
        onActivated: lookup.closeCandidates()
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: metrics.windowMargin
        spacing: metrics.spacing

        Label {
            Layout.fillWidth: true
            text: "Select a release and disc to build metadata proposals."
            wrapMode: Text.WordWrap
        }

        // Sorting and column preferences affect presentation only. Every action
        // carries the complete release-and-medium identity back to the facade.
        RowTable {
            id: table
            objectName: "quickCandidateTable"
            Layout.fillWidth: true
            Layout.fillHeight: true
            rows: lookup.candidates
            currentId: lookup.candidateKey
            layout: lookup.layoutUi
            preferenceKey: "CandidateDialog/candidateTable"
            scaleColumnWidths: true
            sortable: true
            showDetails: false
            hiddenColumns: [1, 5]
            columns: [
                { key: "engine", label: "Engine", width: 100 },
                { key: "source", label: "Source", width: 100 },
                { key: "album", label: "Album title", width: 340 },
                { key: "date", label: "Date", width: 85 },
                { key: "disc", label: "Disc / tracks", width: 160 },
                { key: "languages", label: "Languages", width: 100 },
                { key: "score", label: "Match score", width: 145 },
                { key: "coverage", label: "Mapping coverage", width: 150 }
            ]

            onRowSelected: function(id) {
                lookup.selectCandidate(id)
            }

            onActivated: function(id) {
                lookup.selectCandidate(id)
                lookup.chooseCandidate()
            }

            onSortRequested: function(key, descending) {
                lookup.sortCandidates(key, descending)
            }
        }

        // Empty feedback returns its space to the table. Long receipts remain
        // selectable and scroll inside a fixed allocation above the actions.
        AppScrollView {
            Layout.fillWidth: true
            Layout.preferredHeight: metrics.diagnosticHeight
            Layout.maximumHeight: metrics.diagnosticHeight
            visible: lookup.candidateNotices.length > 0
            contentWidth: availableWidth
            clip: true

            TextArea {
                objectName: "quickCandidateNotices"
                text: lookup.candidateNotices
                readOnly: true
                selectByMouse: true
                wrapMode: TextEdit.WrapAnywhere
                textFormat: TextEdit.PlainText
                Accessible.name: "Candidate search messages and details"
            }
        }

        RowLayout {
            Layout.fillWidth: true
            spacing: metrics.spacing

            ActionButton {
                text: "Why?"
                enabled: lookup.canAcceptCandidate
                onClicked: lookup.showWhy()
            }

            Item {
                Layout.fillWidth: true
            }

            ActionButton {
                objectName: "quickChooseCandidate"
                text: "Choose"
                enabled: lookup.canAcceptCandidate
                onClicked: lookup.chooseCandidate()
            }

            ActionButton {
                text: "Close"
                onClicked: lookup.closeCandidates()
            }
        }
    }

    EvidenceWindow {
        objectName: "quickMatchExplanationWindow"
        title: "Why this match?"
        transientParent: window.transientParent
        rows: lookup.whyRows
        layout: lookup.layoutUi
        visible: lookup.whyVisible
        onClosing: lookup.closeWhy()
    }

    ApplicationWindow {
        id: contactWindow

        objectName: "quickMusicBrainzContactWindow"
        title: "MusicBrainz contact"
        width: Math.min(500, Screen.desktopAvailableWidth - metrics.screenHorizontalMargin)
        height: Math.min(contactForm.implicitHeight + contactActions.implicitHeight
                         + metrics.spacing + 2 * metrics.windowMargin,
                         Screen.desktopAvailableHeight - metrics.screenVerticalMargin)
        minimumWidth: Math.min(380, Screen.desktopAvailableWidth - metrics.screenHorizontalMargin)
        minimumHeight: Math.min(contactActions.implicitHeight + 2 * metrics.windowMargin + 40,
                                Screen.desktopAvailableHeight - metrics.screenVerticalMargin)

        // The contact may be needed before candidates have ever been shown.
        // Its transient owner must therefore be the visible main window.
        transientParent: window.transientParent
        modality: Qt.ApplicationModal
        visible: lookup.contactVisible
        onClosing: lookup.cancelContact()

        onVisibleChanged: {
            if (visible) {
                contactField.text = ""
                contactField.inputControl.forceActiveFocus()
            }
        }

        Shortcut {
            sequence: "Escape"
            enabled: contactWindow.visible && contactWindow.active
            onActivated: lookup.cancelContact()
        }

        ColumnLayout {
            id: contactContent

            anchors.fill: parent
            anchors.margins: metrics.windowMargin
            spacing: metrics.spacing

            // Only the form scrolls on a short display. Acceptance and
            // cancellation stay visible in the separate footer below it.
            AppScrollView {
                id: contactBody

                Layout.fillWidth: true
                Layout.fillHeight: true
                Layout.minimumHeight: 0
                Layout.preferredHeight: contactForm.implicitHeight
                contentWidth: availableWidth
                background: null
                padding: 0
                clip: true

                ColumnLayout {
                    id: contactForm

                    width: contactBody.availableWidth
                    spacing: metrics.spacing

                    Label {
                        Layout.fillWidth: true
                        wrapMode: Text.WordWrap
                        text: "MusicBrainz requires a public project URL or contact email with requests. "
                              + "Enter the contact to use for this session:"
                    }

                    ValidatedTextField {
                        id: contactField

                        objectName: "quickMusicBrainzContactField"
                        Layout.fillWidth: true
                        labelText: "Contact email or project URL"
                        inputObjectName: "quickMusicBrainzContact"
                        errorText: lookup.contactError
                        onAccepted: lookup.setContact(text)
                        onEdited: lookup.clearContactError()
                    }
                }
            }

            RowLayout {
                id: contactActions

                Layout.fillWidth: true
                spacing: metrics.spacing

                Item {
                    Layout.fillWidth: true
                }

                ActionButton {
                    text: "OK"
                    onClicked: lookup.setContact(contactField.text)
                }

                ActionButton {
                    text: "Cancel"
                    onClicked: lookup.cancelContact()
                }
            }
        }
    }
}
