import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Window

ApplicationWindow {
    id: editor

    required property QtObject backend
    readonly property bool positionValue: backend.editType === "position"
    property string inputError: ""

    objectName: "manualEditWindow"
    title: backend.editTitle
    width: metrics.manualWidth
    height: positionValue ? metrics.manualPositionHeight : metrics.manualHeight
    minimumWidth: metrics.manualMinimumWidth
    minimumHeight: metrics.manualMinimumHeight
    flags: Qt.Dialog | Qt.WindowTitleHint | Qt.WindowSystemMenuHint | Qt.WindowCloseButtonHint
    modality: Qt.WindowModal
    visible: backend.editing
    color: palette.window

    UiMetrics {
        id: metrics
    }

    WindowPreferences {
        layout: backend.layoutUi
        window: editor
        key: "ManualValueDialog"
    }

    // Seed controls only when opening. Validation publishes backend changes,
    // but must leave the user's invalid draft available for correction.
    onVisibleChanged: {
        if (visible) {
            inputError = "";
            editValue.text = backend.editValue;
            numberSpin.value = backend.editNumber;
            totalSpin.value = backend.editTotal;
            raise();
            requestActivate();
            Qt.callLater(function() {
                if (editor.positionValue) {
                    numberSpin.forceActiveFocus();
                    numberSpin.contentItem.selectAll();
                } else {
                    editValue.forceActiveFocus();
                    editValue.selectAll();
                }
            });
        }
    }

    onClosing: backend.cancelEdit()

    function acceptValue() {
        if (positionValue) {
            // A window shortcut runs before SpinBox commits its focused text.
            // Read both drafts explicitly so Return and clicking Use value save the
            // same number, including the zero sentinel for a preserved component.
            const number = numberSpin.valueFromText(numberSpin.contentItem.text, numberSpin.locale);
            const total = totalSpin.valueFromText(totalSpin.contentItem.text, totalSpin.locale);

            if (!Number.isInteger(number) || !Number.isInteger(total)
                    || number < numberSpin.from || number > numberSpin.to
                    || total < totalSpin.from || total > totalSpin.to) {
                inputError = "Enter whole numbers. Use 0 to keep the existing number or total.";
                return;
            }

            inputError = "";
            numberSpin.value = number;
            totalSpin.value = total;
            backend.commitPositionEdit(numberSpin.value, totalSpin.value);
        } else {
            backend.commitEdit(editValue.text);
        }
    }

    Shortcut {
        objectName: "manualCancelShortcut"
        sequence: "Escape"
        // The modal editor is the only active editing surface. Other windows
        // explicitly disable their shortcuts while hidden: native Windows can
        // otherwise match their Escape within the same transient-window group.
        enabled: editor.visible
        onActivated: backend.cancelEdit()
    }

    // Text areas retain Return for line breaks. Limit ordinary Return to the
    // position inputs so it cannot accept an edit while Cancel has focus.
    Shortcut {
        sequences: ["Return", "Enter"]
        enabled: editor.visible && editor.positionValue && (numberSpin.activeFocus || totalSpin.activeFocus)
        onActivated: editor.acceptValue()
    }

    Shortcut {
        sequences: ["Ctrl+Return", "Ctrl+Enter"]
        enabled: editor.visible
        onActivated: editor.acceptValue()
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: metrics.windowMargin
        spacing: metrics.spacing

        AppScrollView {
            id: bodyScroll

            Layout.fillWidth: true
            Layout.fillHeight: true
            contentWidth: availableWidth
            rightPadding: effectiveScrollBarWidth
            clip: true

            ColumnLayout {
                width: bodyScroll.availableWidth
                spacing: metrics.spacing

                GridLayout {
                    visible: editor.positionValue
                    Layout.fillWidth: true
                    columns: 2
                    columnSpacing: metrics.spacingLarge
                    rowSpacing: metrics.formRowSpacing

                    Label {
                        text: "Number"
                    }

                    AppSpinBox {
                        id: numberSpin

                        objectName: "manualEditNumber"
                        Layout.fillWidth: true
                        from: 0
                        to: backend.maximumPositionComponent
                        editable: true
                        Accessible.name: "Number"
                        textFromValue: function(value, locale) {
                            return value === 0 ? "Keep existing" : value.toLocaleString(locale, "f", 0);
                        }
                        valueFromText: function(text, locale) {
                            return text === "Keep existing" ? 0 : Number.fromLocaleString(locale, text);
                        }
                    }

                    Label {
                        text: "Total"
                    }

                    AppSpinBox {
                        id: totalSpin

                        objectName: "manualEditTotal"
                        Layout.fillWidth: true
                        from: 0
                        to: backend.maximumPositionComponent
                        editable: true
                        Accessible.name: "Total"
                        textFromValue: function(value, locale) {
                            return value === 0 ? "Keep existing" : value.toLocaleString(locale, "f", 0);
                        }
                        valueFromText: function(text, locale) {
                            return text === "Keep existing" ? 0 : Number.fromLocaleString(locale, text);
                        }
                    }
                }

                RowLayout {
                    visible: !editor.positionValue
                    Layout.fillWidth: true
                    spacing: metrics.controlSpacing

                    Label {
                        Layout.alignment: Qt.AlignTop
                        text: backend.editType === "list" ? "Values" : "Value"
                    }

                    AppScrollView {
                        Layout.fillWidth: true
                        Layout.preferredHeight: metrics.manualHeight / 2
                        contentWidth: availableWidth
                        clip: true

                        TextArea {
                            id: editValue

                            objectName: "manualEditValue"
                            leftPadding: 5
                            rightPadding: 5
                            selectByMouse: true
                            textFormat: TextEdit.PlainText
                            wrapMode: TextEdit.WrapAnywhere
                            Accessible.name: "New value for " + backend.editLabel
                        }
                    }
                }

                Label {
                    Layout.fillWidth: true
                    text: backend.editHint
                    textFormat: Text.PlainText
                    wrapMode: Text.WordWrap
                }

                AppScrollView {
                    Layout.fillWidth: true
                    Layout.preferredHeight: metrics.diagnosticHeight
                    Layout.maximumHeight: metrics.diagnosticHeight
                    visible: backend.editError.length > 0 || editor.inputError.length > 0
                    contentWidth: availableWidth
                    clip: true

                    TextArea {
                        objectName: "editError"
                        text: editor.inputError || backend.editError
                        readOnly: true
                        selectByMouse: true
                        wrapMode: TextEdit.WrapAnywhere
                    }
                }
            }
        }

        // Keep acceptance and cancellation outside scrolling input/errors.
        DialogButtonBox {
            Layout.fillWidth: true
            alignment: Qt.AlignRight
            spacing: metrics.controlSpacing
            onAccepted: editor.acceptValue()
            onRejected: backend.cancelEdit()

            ActionButton {
                objectName: "saveEditButton"
                text: "Use value"
                ToolTip.visible: hovered
                ToolTip.text: "Use this value in review (Ctrl+Enter)."
                DialogButtonBox.buttonRole: DialogButtonBox.AcceptRole
            }

            ActionButton {
                objectName: "cancelEditButton"
                text: "Cancel"
                DialogButtonBox.buttonRole: DialogButtonBox.RejectRole
            }
        }
    }
}
