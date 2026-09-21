"""Long workflow feedback must stay readable without enlarging either window."""

import pytest
from PySide6.QtCore import Qt

from metadata_polisher.ui.main_window import MainWindow

SCAN_ERRORS = "\n".join(
    f"Could not read disc metadata. File: library/Sound selection/{number:02} 空と太陽.flac"
    for number in range(1, 50)
)


@pytest.mark.parametrize(
    "message", [SCAN_ERRORS, "Could not read: library/" + "長い曲名_" * 500 + ".flac"],
    ids=["49-errors", "long-path"],
)
@pytest.mark.parametrize("review_open_first", [False, True])
def test_long_workflow_messages_keep_both_windows_and_apply_actions_within_bounds(
    qtbot, message, review_open_first,
):
    window = MainWindow()
    qtbot.addWidget(window)
    window.resize(960, 700)
    window.show()
    review = window.review_window
    review.resize(800, 480)

    if review_open_first:
        window.open_metadata_review()

    # Exercise the same text delivery used by scan completion, both while
    # review is visible and before it opens. Layout requests are queued in Qt.
    window.workflow_message_label.setText(message)

    if not review_open_first:
        window.open_metadata_review()

    qtbot.wait(20)

    assert window.width() <= 960
    assert window.height() <= 700
    assert window.minimumSizeHint().height() <= 700
    assert review.width() <= 800
    assert review.height() <= 480
    row_height = window.file_table_view.verticalHeader().defaultSectionSize()
    assert window.file_table_view.viewport().height() >= 8 * row_height

    for owner, button in ((window, window.apply_selected_button), (review, window.review_apply_button)):
        assert button.isVisible()
        assert owner.rect().contains(button.mapTo(owner, button.rect().bottomRight()))


def test_workflow_message_details_are_plain_read_only_and_keyboard_accessible(qtbot):
    window = MainWindow()
    qtbot.addWidget(window)
    window.resize(960, 700)
    window.show()
    # Markup-like file names must remain literal text, and the final error
    # must still be reachable rather than silently discarded to cap the list.
    message = SCAN_ERRORS + "\nFinal issue: <b>not markup</b> & details"
    window.workflow_message_label.setText(message)
    window.open_metadata_review()
    qtbot.wait(20)

    for panel in (window.workflow_message_label, window.review_message_label):
        assert panel.isReadOnly()
        assert panel.toPlainText() == message
        assert panel.verticalScrollBar().maximum() > 0
        panel.setFocus()
        qtbot.keyClick(panel, Qt.Key.Key_End, Qt.KeyboardModifier.ControlModifier)
        assert panel.textCursor().atEnd()
        assert panel.verticalScrollBar().value() > 0
        assert panel.viewport().rect().contains(panel.cursorRect().center())
        qtbot.keyClick(panel, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
        assert panel.textCursor().selectedText().replace("\u2029", "\n") == message
        qtbot.keyClicks(panel, "accidental edit")
        assert panel.text() == message


def test_clearing_feedback_recovers_workspace_and_replaces_stale_details(qtbot):
    window = MainWindow()
    qtbot.addWidget(window)
    window.resize(960, 700)
    window.show()
    window.workflow_message_label.setText(SCAN_ERRORS)
    window.open_metadata_review()
    qtbot.wait(20)
    track_height = window.file_table_view.height()
    window.review_window.close()

    window.workflow_message_label.clear()
    window.open_metadata_review()
    qtbot.wait(20)

    assert window.file_table_view.height() > track_height
    assert not window.workflow_message_label.isVisible()
    assert not window.review_message_label.isVisible()

    window.workflow_message_label.setText("Scan complete.")
    qtbot.wait(20)

    for panel in (window.workflow_message_label, window.review_message_label):
        assert panel.isVisible()
        assert panel.text() == "Scan complete."
        assert panel.verticalScrollBar().maximum() == 0
