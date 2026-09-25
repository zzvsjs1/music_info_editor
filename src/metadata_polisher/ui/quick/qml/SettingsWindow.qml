import QtQuick
import QtQuick.Controls
import QtQuick.Dialogs
import QtQuick.Layouts

ApplicationWindow {
    id: root
    required property QtObject settings
    property QtObject layout: null
    objectName: "settingsWindow"
    title: "Settings"
    width: Math.min(760, Screen.width - 32)
    height: Math.min(490, Screen.height - 64)
    minimumWidth: 600
    minimumHeight: 360
    visible: settings.opened
    modality: Qt.WindowModal
    flags: Qt.Dialog

    UiMetrics {
        id: metrics
    }

    // The native Windows SpinBox already reserves space for its arrows. Only
    // plain text fields need the extra inset seen in the Widgets dialogue.
    component SettingsTextField: TextField {
        leftPadding: 5
        rightPadding: 5
    }

    // Qt Quick's Windows style does not supply a TabButton. Keep the tab
    // outline and colours tied to the platform palette, including dark mode.
    component SettingsTabButton: TabButton {
        id: tab
        width: implicitWidth
        leftPadding: 9
        rightPadding: 9
        topPadding: 5
        bottomPadding: 5

        contentItem: Text {
            text: tab.text
            font: tab.font
            color: tab.enabled ? root.palette.windowText : root.palette.placeholderText
            horizontalAlignment: Text.AlignHCenter
            verticalAlignment: Text.AlignVCenter
        }

        background: Item {
            Rectangle {
                anchors.fill: parent
                color: tab.checked ? root.palette.base
                    : tab.hovered ? root.palette.button : root.palette.window
                border.color: root.palette.mid
            }

            // The active tab opens onto the page instead of closing its lower
            // edge with another line.
            Rectangle {
                x: 1
                width: Math.max(0, parent.width - 2)
                height: 1
                anchors.bottom: parent.bottom
                color: root.palette.base
                visible: tab.checked
            }

            Rectangle {
                anchors.fill: parent
                anchors.margins: 3
                color: "transparent"
                border.color: root.palette.highlight
                visible: tab.visualFocus
            }
        }
    }

    WindowPreferences {
        layout: root.layout
        window: root
        key: "SettingsDialog"
    }

    // A test owns captured credentials until its terminal event. The native
    // close button requests cancellation, keeping that lifetime visible.
    onClosing: function(close) {
        close.accepted = false
        settings.reject()
    }

    Shortcut {
        sequence: "Escape"
        enabled: root.active && !suggestions.opened
        onActivated: settings.reject()
    }

    // These shortcuts work from page inputs as well as the tab strip. Changing
    // the current index also reveals an overflowed tab through TabBar's view.
    Shortcut {
        sequence: "Ctrl+Tab"
        enabled: root.visible && root.active && !suggestions.opened
        onActivated: tabs.currentIndex = (tabs.currentIndex + 1) % tabs.count
    }

    Shortcut {
        sequence: "Ctrl+Shift+Tab"
        enabled: root.visible && root.active && !suggestions.opened
        onActivated: tabs.currentIndex = (tabs.currentIndex + tabs.count - 1) % tabs.count
    }

    function optionIndex(options, selected) {
        for (let index = 0; index < options.length; ++index) {
            if (options[index].id === selected)
                return index
        }

        return -1
    }

    FolderDialog {
        id: directoryPicker
        property string targetField: ""
        onAccepted: settings.setField(targetField, settings.pathFromUrl(selectedFolder))
    }

    FileDialog {
        id: executablePicker
        title: "Choose executable"
        nameFilters: ["Executables (*.exe)", "All files (*)"]
        fileMode: FileDialog.OpenFile
        property string toolName: ""
        onAccepted: settings.addTool(toolName, settings.pathFromUrl(selectedFile))
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: metrics.windowMargin
        spacing: metrics.spacing

        RowLayout {
            id: tabStrip
            Layout.fillWidth: true
            spacing: metrics.spacingSmall
            readonly property bool overflowing: tabs.contentWidth > width

            ToolButton {
                objectName: "previousSettingsTab"
                visible: tabStrip.overflowing
                enabled: tabs.currentIndex > 0
                text: "‹"
                Accessible.name: "Previous settings tab"
                ToolTip.visible: hovered
                ToolTip.text: "Previous settings tab (Ctrl+Shift+Tab)"
                onClicked: tabs.decrementCurrentIndex()
            }

            TabBar {
                id: tabs
                objectName: "settingsTabs"
                Layout.fillWidth: true
                Layout.minimumWidth: 0
                clip: true

                background: Item {
                    Rectangle {
                        anchors.bottom: parent.bottom
                        width: parent.width
                        height: 1
                        color: root.palette.mid
                    }
                }

                onCurrentIndexChanged: Qt.callLater(function() {
                    // Layout polish can follow the click/key event. Reveal the
                    // whole current tab after the viewport has its final width.
                    if (contentItem && currentIndex >= 0)
                        contentItem.positionViewAtIndex(currentIndex, ListView.Contain)
                })

                // Overflow stays inside this viewport while keyboard tab
                // switching keeps the active label visible.
                SettingsTabButton {
                    objectName: "renamingTab"
                    text: "Renaming"
                }

                SettingsTabButton {
                    objectName: "providersTab"
                    text: "Providers and language"
                }

                SettingsTabButton {
                    objectName: "networkTab"
                    text: "Network and session login"
                }

                SettingsTabButton {
                    objectName: "outputTab"
                    text: "Backups, reports and diagnostics"
                }

                SettingsTabButton {
                    objectName: "externalToolsTab"
                    text: "External tools"
                }
            }

            ToolButton {
                objectName: "nextSettingsTab"
                visible: tabStrip.overflowing
                enabled: tabs.currentIndex < tabs.count - 1
                text: "›"
                Accessible.name: "Next settings tab"
                ToolTip.visible: hovered
                ToolTip.text: "Next settings tab (Ctrl+Tab)"
                onClicked: tabs.incrementCurrentIndex()
            }
        }

        StackLayout {
            objectName: "settingsPages"
            currentIndex: tabs.currentIndex
            Layout.fillWidth: true
            Layout.fillHeight: true
            // Every page starts below the shared tab baseline. Keep this inset
            // on the stack so pages with different scroll containers align.
            Layout.topMargin: 5

            AppScrollView {
                id: renameScroll
                clip: true
                contentWidth: availableWidth

                ColumnLayout {
                    width: renameScroll.availableWidth
                    spacing: 10

                    CheckBox {
                        text: "Enable filename renaming"
                        checked: settings.draft.renameEnabled
                        enabled: !settings.testRunning
                        onToggled: settings.setField("renameEnabled", checked)
                    }

                    GridLayout {
                        columns: 2
                        Layout.fillWidth: true
                        enabled: !settings.testRunning

                        Label {
                            text: "Filename template"
                        }

                        SettingsTextField {
                            id: templateEdit
                            objectName: "settingsTemplate"
                            Layout.fillWidth: true
                            text: settings.draft.template
                            selectByMouse: true
                            property var completion: ({})

                            function refreshCompletions() {
                                completion = selectionStart === selectionEnd
                                    ? settings.templateCompletion(text, cursorPosition) : ({})

                                if (completion.options && completion.options.length) {
                                    suggestionList.currentIndex = 0
                                    suggestions.open()
                                } else {
                                    suggestions.close()
                                }
                            }

                            function acceptCompletion(token) {
                                const start = completion.start
                                const end = completion.end
                                suggestions.close()
                                remove(start, end)
                                insert(start, token)
                                cursorPosition = start + token.length
                                settings.setField("template", text)
                            }

                            onTextEdited: {
                                settings.setField("template", text)
                                refreshCompletions()
                            }

                            onCursorPositionChanged: {
                                if (suggestions.opened)
                                    refreshCompletions()
                            }

                            Keys.onPressed: function(event) {
                                if (event.key === Qt.Key_Space && event.modifiers === Qt.ControlModifier) {
                                    refreshCompletions()
                                    event.accepted = true
                                } else if (suggestions.opened) {
                                    if (event.key === Qt.Key_Escape) {
                                        suggestions.close()
                                        event.accepted = true
                                    } else if (event.key === Qt.Key_Up || event.key === Qt.Key_Down) {
                                        const step = event.key === Qt.Key_Down ? 1 : -1
                                        suggestionList.currentIndex = (suggestionList.currentIndex + step
                                            + suggestionList.count) % suggestionList.count
                                        event.accepted = true
                                    } else if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter
                                               || event.key === Qt.Key_Tab) {
                                        acceptCompletion(completion.options[suggestionList.currentIndex])
                                        event.accepted = true
                                    }
                                }
                            }

                            Popup {
                                id: suggestions
                                objectName: "templateSuggestions"
                                y: templateEdit.height
                                width: Math.min(280, templateEdit.width)
                                height: Math.min(220, suggestionList.contentHeight + 12)
                                padding: 6
                                focus: false
                                closePolicy: Popup.CloseOnPressOutside

                                ListView {
                                    id: suggestionList
                                    anchors.fill: parent
                                    clip: true
                                    model: templateEdit.completion.options || []
                                    delegate: ItemDelegate {
                                        required property int index
                                        required property string modelData
                                        width: ListView.view.width
                                        text: modelData
                                        highlighted: ListView.isCurrentItem
                                        onClicked: templateEdit.acceptCompletion(modelData)
                                    }
                                }
                            }
                        }

                        Label {
                            text: "Minimum track digits"
                        }

                        SpinBox {
                            Layout.fillWidth: true
                            from: 1; to: 10; editable: true
                            value: settings.draft.trackDigits
                            onValueModified: settings.setField("trackDigits", value)
                        }

                        Label {
                            text: "Minimum disc digits"
                        }

                        SpinBox {
                            Layout.fillWidth: true
                            from: 1; to: 10; editable: true
                            value: settings.draft.discDigits
                            onValueModified: settings.setField("discDigits", value)
                        }
                    }

                    Label {
                        Layout.fillWidth: true
                        wrapMode: Text.WordWrap
                        text: "Type % to choose a field, then press Enter or Tab to insert it. "
                              + "Press Escape to close suggestions, or Ctrl+Space to reopen them inside a field. "
                              + "Put optional content in brackets, for example [%discnumber%.]. "
                              + "The file extension is retained automatically. Minimum digit widths are limited to 1–10: "
                              + "ten digits cover the largest editable track or disc number without excessive zero padding."
                    }
                }
            }

            AppScrollView {
                id: providerScroll
                clip: true
                contentWidth: availableWidth

                ColumnLayout {
                    width: providerScroll.availableWidth
                    spacing: 10

                    GridLayout {
                        columns: 2
                        Layout.fillWidth: true

                        Label {
                            text: "Preferred language (auto, ja, en…)"
                        }

                        SettingsTextField {
                            Layout.fillWidth: true
                            text: settings.draft.preferredLanguage
                            enabled: !settings.testRunning
                            selectByMouse: true
                            onTextEdited: settings.setField("preferredLanguage", text)
                        }

                        Label {
                            text: "Lookup provider"
                        }

                        ComboBox {
                            Layout.fillWidth: true
                            model: settings.providerOptions
                            textRole: "label"
                            currentIndex: root.optionIndex(settings.providerOptions, settings.draft.providerId)
                            enabled: !settings.testRunning
                            onActivated: settings.setField("providerId", settings.providerOptions[index].id)
                        }
                    }

                    Label {
                        Layout.fillWidth: true
                        text: settings.providerSummary
                        textFormat: Text.PlainText
                        wrapMode: Text.WordWrap
                    }

                    RowLayout {
                        ActionButton {
                            text: "Test selected provider"
                            enabled: settings.testEnabled
                            onClicked: settings.testProvider()
                        }

                        ActionButton {
                            text: "Cancel test"
                            enabled: settings.testRunning
                            onClicked: settings.cancelTest()
                        }
                    }

                    AppScrollView {
                        Layout.fillWidth: true
                        Layout.preferredHeight: Math.min(90, testResult.implicitHeight)
                        clip: true

                        TextArea {
                            id: testResult
                            text: settings.testStatus
                            readOnly: true
                            selectByMouse: true
                            wrapMode: TextEdit.Wrap
                            textFormat: TextEdit.PlainText
                        }
                    }

                    ProgressBar {
                        Layout.fillWidth: true
                        visible: settings.testRunning
                        indeterminate: true
                    }

                    Label {
                        Layout.fillWidth: true
                        wrapMode: Text.WordWrap
                        text: "Provider changes apply to the next operation. Existing candidates and review decisions are retained."
                    }
                }
            }

            AppScrollView {
                id: networkScroll
                clip: true
                contentWidth: availableWidth

                ColumnLayout {
                    width: networkScroll.availableWidth
                    spacing: 10
                    property bool manual: settings.draft.networkMode === "manual_proxy" && !settings.testRunning

                    GridLayout {
                        columns: 2
                        Layout.fillWidth: true

                        Label {
                            text: "External services route"
                        }

                        ComboBox {
                            Layout.fillWidth: true
                            model: settings.routeOptions
                            textRole: "label"
                            currentIndex: root.optionIndex(settings.routeOptions, settings.draft.networkMode)
                            enabled: !settings.testRunning
                            onActivated: settings.setField("networkMode", settings.routeOptions[index].id)
                        }

                        Label {
                            text: "HTTP proxy host"
                        }

                        SettingsTextField {
                            Layout.fillWidth: true
                            text: settings.draft.proxyHost
                            enabled: settings.draft.networkMode === "manual_proxy" && !settings.testRunning
                            placeholderText: "Hostname or IP address, without http://"
                            selectByMouse: true
                            onTextEdited: settings.setField("proxyHost", text)
                        }

                        Label {
                            text: "Port"
                        }

                        SpinBox {
                            from: 0; to: 2147483647; editable: true
                            value: settings.draft.proxyPort
                            enabled: settings.draft.networkMode === "manual_proxy" && !settings.testRunning
                            onValueModified: settings.setField("proxyPort", value)
                        }
                    }

                    Label {
                        Layout.fillWidth: true
                        text: settings.routeDescription
                        textFormat: Text.PlainText
                        wrapMode: Text.WordWrap
                    }

                    GridLayout {
                        columns: 2
                        Layout.fillWidth: true
                        enabled: settings.draft.networkMode === "manual_proxy" && !settings.testRunning

                        Label {
                            text: "Session proxy username"
                        }

                        SettingsTextField {
                            id: usernameEdit
                            Layout.fillWidth: true
                            text: settings.username
                            selectByMouse: true
                            onTextEdited: settings.setCredentials(text, passwordEdit.text)
                        }

                        Label {
                            text: "Session proxy password"
                        }

                        SettingsTextField {
                            id: passwordEdit
                            Layout.fillWidth: true
                            text: settings.password
                            echoMode: TextInput.Password
                            selectByMouse: true
                            onTextEdited: settings.setCredentials(usernameEdit.text, text)
                        }
                    }

                    RowLayout {
                        ActionButton {
                            text: "Save for this session"
                            enabled: !settings.testRunning
                            onClicked: settings.saveSessionLogin()
                        }

                        ActionButton {
                            text: "Forget session login"
                            enabled: !settings.testRunning
                            onClicked: settings.forgetSessionLogin()
                        }
                    }

                    Label {
                        Layout.fillWidth: true
                        text: settings.credentialsStatus
                        wrapMode: Text.WordWrap
                    }

                    Label {
                        Layout.fillWidth: true
                        wrapMode: Text.WordWrap
                        text: "Proxy credentials stay in memory and are lost when the application closes. "
                              + "Test selected provider uses the draft credentials. Save for this session activates them; "
                              + "closing Settings discards unsaved credential edits. Route preferences use the main Save button. "
                              + "Direct and Manual HTTP proxy ignore environment proxy settings. "
                              + "HTTPS certificate checks stay enabled."
                    }
                }
            }

            AppScrollView {
                id: outputScroll
                clip: true
                contentWidth: availableWidth

                ColumnLayout {
                    width: outputScroll.availableWidth
                    spacing: 10
                    enabled: !settings.testRunning

                    CheckBox {
                        text: "Keep permanent backups"
                        checked: settings.draft.backupEnabled
                        onToggled: settings.setField("backupEnabled", checked)
                    }

                    Label {
                        text: "Backup directory"
                    }

                    RowLayout {
                        Layout.fillWidth: true
                        SettingsTextField {
                            Layout.fillWidth: true
                            text: settings.draft.backupDirectory
                            selectByMouse: true
                            onTextEdited: settings.setField("backupDirectory", text)
                        }

                        ActionButton {
                            text: "Browse…"
                            onClicked: {
                                directoryPicker.title = "Choose backup directory"
                                directoryPicker.targetField = "backupDirectory"
                                directoryPicker.open()
                            }
                        }
                    }

                    CheckBox {
                        text: "Save processing reports"
                        checked: settings.draft.reportsEnabled
                        onToggled: settings.setField("reportsEnabled", checked)
                    }

                    Label {
                        text: "Report directory"
                    }

                    RowLayout {
                        Layout.fillWidth: true
                        SettingsTextField {
                            Layout.fillWidth: true
                            text: settings.draft.reportsDirectory
                            placeholderText: "Blank uses the application reports directory"
                            selectByMouse: true
                            onTextEdited: settings.setField("reportsDirectory", text)
                        }

                        ActionButton {
                            text: "Browse…"
                            onClicked: {
                                directoryPicker.title = "Choose report directory"
                                directoryPicker.targetField = "reportsDirectory"
                                directoryPicker.open()
                            }
                        }
                    }

                    CheckBox {
                        text: "Enable detailed diagnostic tracing"
                        checked: settings.draft.detailedTracing
                        onToggled: settings.setField("detailedTracing", checked)
                    }
                }
            }

            ColumnLayout {
                enabled: !settings.testRunning

                RowLayout {
                    Layout.fillWidth: true
                    Label {
                        text: "Tool name"
                        Layout.preferredWidth: 160
                        font.bold: true
                    }

                    Label {
                        text: "Executable path"
                        Layout.fillWidth: true
                        font.bold: true
                    }
                }

                Rectangle {
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    color: root.palette.base
                    border.color: root.palette.mid

                    // A plain item accepts a custom white viewport under the
                    // native Windows style, which disallows replacing Frame's
                    // background and content item.
                    ListView {
                        id: toolsList
                        objectName: "settingsToolsTable"
                        anchors.fill: parent
                        anchors.margins: 1
                        clip: true
                        model: settings.tools
                        property string selectedName: ""
                        activeFocusOnTab: true
                        keyNavigationEnabled: false
                        currentIndex: {
                            for (let index = 0; index < model.length; ++index) {
                                if (model[index].name === selectedName)
                                    return index
                            }

                            return -1
                        }

                        // The saved name owns selection. A sorted model refresh
                        // must not retarget Remove selected to the old row number.
                        function selectIndex(index) {
                            if (index >= 0 && index < count) {
                                selectedName = model[index].name
                                positionViewAtIndex(index, ListView.Contain)
                            }
                        }

                        Keys.onUpPressed: selectIndex(Math.max(0, currentIndex - 1))
                        Keys.onDownPressed: selectIndex(Math.min(count - 1, currentIndex + 1))
                        Keys.onPressed: function(event) {
                            if (event.key === Qt.Key_Home || event.key === Qt.Key_End) {
                                selectIndex(event.key === Qt.Key_Home ? 0 : count - 1)
                                event.accepted = true
                            }
                        }
                        ScrollBar.vertical: AppScrollBar {}

                        delegate: ItemDelegate {
                            id: toolDelegate
                            required property var modelData
                            required property int index
                            width: ListView.view.width
                            highlighted: ListView.isCurrentItem
                            contentItem: RowLayout {
                                Label {
                                    text: modelData.name
                                    color: toolDelegate.highlighted ? palette.highlightedText : palette.text
                                    Layout.preferredWidth: 160
                                    elide: Text.ElideRight
                                }

                                Label {
                                    text: modelData.path
                                    color: toolDelegate.highlighted ? palette.highlightedText : palette.text
                                    Layout.fillWidth: true
                                    elide: Text.ElideMiddle
                                }
                            }
                            ToolTip.visible: hovered
                            ToolTip.text: modelData.path

                            onClicked: {
                                toolsList.selectedName = modelData.name
                                toolsList.forceActiveFocus()
                            }
                        }
                    }
                }

                RowLayout {
                    Layout.fillWidth: true
                    SettingsTextField {
                        id: toolName
                        Layout.fillWidth: true
                        placeholderText: "Tool name"
                        selectByMouse: true
                    }

                    ActionButton {
                        text: "Browse executable…"
                        onClicked: {
                            if (!toolName.text.trim()) {
                                settings.addTool("", "")
                                return
                            }

                            executablePicker.toolName = toolName.text
                            executablePicker.open()
                        }
                    }

                    ActionButton {
                        objectName: "removeExternalTool"
                        text: "Remove selected"
                        enabled: toolsList.currentIndex >= 0
                        onClicked: {
                            settings.removeTool(toolsList.selectedName)
                            toolsList.selectedName = ""
                        }
                    }
                }

                Label {
                    Layout.fillWidth: true
                    wrapMode: Text.WordWrap
                    text: "Enter a tool name and choose its executable. Reusing a name replaces its saved path."
                }
            }
        }

        // Diagnostics remain bounded and copyable; actions never move inside
        // this scrolling region when an error contains a long filesystem path.
        AppScrollView {
            visible: settings.error.length > 0
            Layout.fillWidth: true
            Layout.preferredHeight: Math.min(72, errorText.implicitHeight)
            clip: true

            TextArea {
                id: errorText
                text: settings.error
                readOnly: true
                selectByMouse: true
                wrapMode: TextEdit.Wrap
                textFormat: TextEdit.PlainText
            }
        }

        RowLayout {
            Layout.fillWidth: true
            Item {
                Layout.fillWidth: true
            }

            ActionButton {
                objectName: "saveSettingsButton"
                text: "Save"
                enabled: !settings.testRunning
                onClicked: settings.save()
            }

            ActionButton {
                objectName: "cancelSettingsButton"
                text: "Cancel"
                enabled: !settings.testRunning
                onClicked: settings.reject()
            }
        }
    }
}
