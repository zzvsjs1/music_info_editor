"""The review work area stays useful without authorising any disk writes."""

import pytest
from PySide6.QtCore import QPointF
from PySide6.QtGui import QFont
from PySide6.QtQuick import QQuickItem

from tests.ui.test_quick_window import backend, click_item, open_review, scene  # noqa: F401


def item(window, name):
    result = window.findChild(QQuickItem, name)
    assert result is not None, name
    return result


def inside(window, control):
    origin = control.mapToScene(QPointF())
    assert origin.x() >= 0 and origin.y() >= 0
    assert origin.x() + control.width() <= window.width() + 1
    assert origin.y() + control.height() <= window.height() + 1


def test_collapsed_sections_give_metadata_the_main_work_area(scene, qtbot):  # noqa: F811
    window, interface = scene
    interface.selectFile("first", False)
    review = open_review(window, interface)
    review.resize(1080, 760)
    qtbot.wait(80)

    assert item(review, "reviewTable").height() >= review.height() / 2
    assert not item(review, "filenameDetails").isVisible()
    assert not item(review, "reviewDiagnostics").isVisible()
    assert item(review, "reviewFileProgress").property("text") == "1 of 2"
    safe = item(review, "acceptSafeAdditionsButton")
    assert safe.mapToScene(QPointF()).x() > review.width() / 2
    assert not interface.executor.pending


def test_table_receives_extra_height_and_footer_remains_fixed(scene, qtbot):  # noqa: F811
    window, interface = scene
    interface.selectFile("first", False)
    review = open_review(window, interface)
    review.resize(960, 650)
    qtbot.wait(80)
    previous_height = item(review, "reviewTable").height()
    review.resize(960, 850)
    qtbot.wait(80)

    assert item(review, "reviewTable").height() >= previous_height + 190
    inside(review, item(review, "reviewApplyButton"))
    # Qt buttons escape a literal ampersand so it is not consumed as a mnemonic.
    assert item(review, "reviewApplyButton").property("text").replace("&&", "&") == "Review & Apply…"
    assert not item(review, "reviewApplyButton").isEnabled()


def test_large_text_actions_do_not_slide_under_the_vertical_scrollbar(scene, qtbot):  # noqa: F811
    window, interface = scene
    interface.selectFile("first", False)
    review = open_review(window, interface)
    review.setProperty("font", QFont("Segoe UI", 14))
    review.resize(600, 360)
    qtbot.wait(80)
    body = item(review, "reviewScrollArea")
    safe = item(review, "acceptSafeAdditionsButton")
    edge = safe.mapToItem(body, QPointF(safe.width(), 0)).x()

    assert edge <= body.width() - body.property("effectiveScrollBarWidth") + 1


def test_expanding_details_does_not_change_review_or_write_membership(scene, qtbot):  # noqa: F811
    window, interface = scene
    interface.selectFile("first", False)
    interface.selectField("title")
    interface.set_status("Scanned 2 supported files in 1 albums.")
    review = open_review(window, interface)
    snapshot = interface.session_state
    click_item(review, "filenameSectionButton")
    assert item(review, "filenameDetails").isVisible()
    click_item(review, "scanDetailsButton")
    assert item(review, "reviewDiagnostics").isVisible()
    click_item(review, "fieldDetailsButton")

    assert item(review, "fieldDetailsButton").property("text") == "Hide full values"
    assert interface.session_state is snapshot
    assert interface.includedFileIds == []
    assert not interface.executor.pending


def test_footer_opens_final_review_without_submitting_a_write(scene, qtbot):  # noqa: F811
    window, interface = scene
    interface.selectFile("first", False)
    interface.selectField("title")
    review = open_review(window, interface)
    assert interface.beginEdit()
    assert interface.commitEdit("Reviewed title")
    click_item(review, "includeReviewScopeButton")

    assert interface.includedFileIds == ["first"]
    assert item(review, "reviewApplyButton").isEnabled()
    assert not interface.executor.pending
    click_item(review, "reviewApplyButton")

    assert interface.applyUi.summaryVisible
    assert not interface.executor.pending
    interface.applyUi.cancelApply()
    assert interface.canUndo
    assert interface.includedFileIds == ["first"]


def test_new_rename_suggestions_and_scan_warnings_open_their_details(scene, qtbot):  # noqa: F811
    from dataclasses import replace

    from metadata_polisher.domain.errors import Issue, MediaErrorCode

    window, interface = scene
    interface.selectFile("first", False)
    interface.selectField("title")
    review = open_review(window, interface)
    assert not item(review, "filenameDetails").isVisible()
    assert interface.beginEdit()
    assert interface.commitEdit("Reviewed title")
    qtbot.wait(50)
    assert item(review, "filenameDetails").isVisible()

    warning = Issue(MediaErrorCode.TAG_READ_FAILED, "One file needs attention", "Full diagnostic")
    interface.set_state(replace(interface.session_state, scan_issues=(warning,)))
    qtbot.wait(50)
    assert item(review, "reviewDiagnostics").isVisible()
    assert "Full diagnostic" in item(review, "reviewMessageLabel").property("text")
    inside(review, item(review, "reviewApplyButton"))


def test_failed_filename_preview_keeps_its_validation_visible(scene, qtbot):  # noqa: F811
    window, interface = scene
    interface.selectFile("first", False)
    interface.selectField("title")
    review = open_review(window, interface)
    assert interface.beginEdit()
    assert interface.commitEdit("Reviewed title")
    qtbot.wait(40)
    interface.reviewAction("clear")
    qtbot.wait(40)

    # A missing required template value removes the preview. It must expose
    # the reason instead of looking like an ordinary file with no suggestion.
    assert not interface.hasFilenameSuggestion
    assert "needs attention" in interface.filenameSummary
    assert item(review, "filenameDetails").isVisible()
    assert "Warning:" in interface.renameValidation


def test_failed_scan_exposes_diagnostics_and_preserves_review(scene, qtbot, tmp_path):  # noqa: F811
    window, interface = scene
    interface.selectFile("first", False)
    review = open_review(window, interface)
    groups = interface.session_state.groups
    assert interface.scan(str(tmp_path), False)
    operation = interface.session_state.active_operation
    interface._on_failed(operation.operation_id, RuntimeError("Synthetic scan failure"))
    qtbot.wait(50)

    assert interface.session_state.groups == groups
    assert item(review, "reviewDiagnostics").isVisible()
    assert "Synthetic scan failure" in item(review, "reviewMessageLabel").property("text")
    inside(review, item(review, "reviewApplyButton"))


@pytest.mark.parametrize("size", [(600, 360), (800, 480), (960, 650)])
def test_actions_remain_available_in_small_windows(scene, qtbot, size):  # noqa: F811
    window, interface = scene
    interface.selectFile("first", False)
    review = open_review(window, interface)
    review.resize(*size)
    qtbot.wait(80)

    for name in ("reviewScopeCombo", "previousReviewFileButton", "nextReviewFileButton",
                 "includeReviewScopeButton", "reviewApplyButton"):
        inside(review, item(review, name))

    assert item(review, "reviewScrollArea").height() >= item(review, "reviewTable").property("minimumTableHeight")
    assert not item(review, "keepExistingButton").isEnabled()
    assert not item(review, "useProposedButton").isEnabled()
    assert not item(review, "manualValueButton").isEnabled()
