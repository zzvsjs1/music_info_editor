import QtQuick
import QtQuick.Controls

// Own the completion interaction independently of the Settings page layout.
// The facade supplies token/UTF-16 boundaries; this control owns popup focus,
// keyboard navigation and replacement of the user's current text selection.
AppTextField {
    id: editor

    required property QtObject settings
    property int completionStart: 0
    property int completionEnd: 0
    property list<string> completionOptions: []
    readonly property bool suggestionsOpen: suggestions.opened

    text: settings.draft.template
    selectByMouse: true
    Accessible.name: "Filename template"
    Accessible.description: settings.fieldErrors.template || ""

    function refreshCompletions() {
        const result = selectionStart === selectionEnd
            ? settings.templateCompletion(text, cursorPosition) : ({});
        completionOptions = result.options || [];
        completionStart = result.start || 0;
        completionEnd = result.end || 0;

        if (completionOptions.length === 0) {
            suggestions.close();
            return;
        }

        suggestionList.currentIndex = 0;
        suggestions.open();
    }

    function acceptCompletion(token: string) {
        if (completionOptions.indexOf(token) < 0) {
            return;
        }

        // Close before editing: cursor notifications must not calculate a new
        // replacement span halfway through removing and inserting this token.
        const start = completionStart;
        const end = completionEnd;
        suggestions.close();
        remove(start, end);
        insert(start, token);
        cursorPosition = start + token.length;
        settings.setField("template", text);
    }

    function moveSuggestion(step: int) {
        const count = completionOptions.length;
        suggestionList.currentIndex = (suggestionList.currentIndex + step + count) % count;
    }

    onTextEdited: {
        settings.setField("template", text);
        refreshCompletions();
    }

    onEditingFinished: settings.validateField("template")

    onCursorPositionChanged: {
        if (suggestions.opened) {
            refreshCompletions();
        }
    }

    Keys.onPressed: function(event) {
        if (event.key === Qt.Key_Space && event.modifiers === Qt.ControlModifier) {
            refreshCompletions();
            event.accepted = true;
            return;
        }

        if (!suggestions.opened) {
            return;
        }

        switch (event.key) {
        case Qt.Key_Escape:
            suggestions.close();
            break;
        case Qt.Key_Up:
            moveSuggestion(-1);
            break;
        case Qt.Key_Down:
            moveSuggestion(1);
            break;
        case Qt.Key_Return:
        case Qt.Key_Enter:
        case Qt.Key_Tab:
            acceptCompletion(completionOptions[suggestionList.currentIndex]);
            break;
        default:
            return;
        }

        event.accepted = true;
    }

    Popup {
        id: suggestions
        objectName: "templateSuggestions"
        y: editor.height
        width: Math.min(280, editor.width)
        height: Math.min(220, suggestionList.contentHeight + 12)
        padding: 6
        focus: false
        closePolicy: Popup.CloseOnPressOutside

        ListView {
            id: suggestionList
            anchors.fill: parent
            clip: true
            model: editor.completionOptions

            delegate: ItemDelegate {
                required property int index
                required property string modelData
                width: ListView.view.width
                text: modelData
                highlighted: ListView.isCurrentItem
                onClicked: editor.acceptCompletion(modelData)
            }
        }
    }
}
