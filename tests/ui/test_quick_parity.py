"""Exercise the native two-window workflow through its actual Qt Quick scene."""

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPointF, Qt
from PySide6.QtQuick import QQuickItem, QQuickWindow
from PySide6.QtTest import QTest

from metadata_polisher.domain.metadata import MetadataField, MetadataSnapshot, Position
from metadata_polisher.domain.review import DecisionOrigin, FieldConfidence, FieldDecisionKind
from tests.ui.test_quick_backend import backend as backend_fixture
from tests.ui.test_quick_window import click_item
from tests.unit.session.test_review_editing import field_review, make_selected_session, proposal

backend = pytest.fixture(backend_fixture.__wrapped__)


@pytest.fixture
def parity_scene(backend, qtbot):  # noqa: F811
    from metadata_polisher.ui.quick.application import create_quick_engine

    warnings = []
    engine = create_quick_engine(backend, warnings=warnings)
    window = engine.rootObjects()[0]
    window.show()
    qtbot.wait(80)

    try:
        yield window, backend
    finally:
        # Hide every owned window before deferred engine destruction; this also
        # catches bindings that refer to a disposed backend during teardown.
        for child in window.findChildren(QQuickWindow):
            child.hide()

        window.hide()
        engine.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        assert not warnings, warnings


def item(window, name):
    result = window.findChild(QQuickItem, name)
    assert result is not None, name
    return result


def test_main_has_album_navigation_and_full_height_files_with_review_initially_closed(parity_scene):
    window, _backend = parity_scene
    albums = item(window, "groupTable")
    files = item(window, "fileTable")
    review = window.findChild(QQuickWindow, "metadataReviewWindow")

    assert review is not None
    assert not review.isVisible()
    assert review.modality() == Qt.WindowModality.NonModal
    assert window.title() == "Metadata Polisher"
    assert albums.mapToScene(QPointF()).x() < files.mapToScene(QPointF()).x()
    assert 280 <= albums.width() <= 310
    assert files.height() >= 8 * 30
    assert item(review, "reviewTable").window() is review


def test_review_reuses_its_window_and_retains_decisions_without_reducing_files(parity_scene, qtbot):
    window, backend = parity_scene
    backend.selectFile("first", False)
    backend.selectField("title")
    backend.setIncluded("second", True)
    backend.reviewAction("clear")
    snapshot = backend.session_state
    file_height = item(window, "fileTable").height()
    backend.openReview()
    review = window.findChild(QQuickWindow, "metadataReviewWindow")
    qtbot.waitUntil(review.isVisible)
    review.resize(860, 600)
    qtbot.waitUntil(lambda: (review.width(), review.height()) == (860, 600))
    qtbot.wait(30)
    review.close()

    assert backend.session_state is snapshot
    assert backend.includedFileIds == ["second"]
    assert not review.isVisible()
    backend.openReview()
    qtbot.waitUntil(review.isVisible)
    qtbot.waitUntil(lambda: (review.width(), review.height()) == (860, 600))
    assert (review.width(), review.height()) == (860, 600)
    assert item(window, "fileTable").height() == file_height
    assert backend.selectedFileIds == ["first"]


def test_enter_on_file_opens_selected_scope_and_keeps_inclusion_separate(parity_scene, qtbot):
    window, backend = parity_scene
    backend.selectFile("first", False)
    backend.setReviewScope("library")
    table = item(window, "fileTable")
    window.requestActivate()
    table.forceActiveFocus()
    QTest.keyClick(window, Qt.Key.Key_Return)
    review = window.findChild(QQuickWindow, "metadataReviewWindow")
    qtbot.waitUntil(review.isVisible)

    assert backend.reviewScope == "selected"
    assert backend.selectedFileIds == ["first"]
    assert backend.includedFileIds == []
    assert not backend.editing


def test_scope_and_write_actions_remain_outside_review_scroll_on_small_window(parity_scene, qtbot):
    window, backend = parity_scene
    backend.selectFile("first", False)
    backend.openReview()
    review = window.findChild(QQuickWindow, "metadataReviewWindow")
    review.resize(800, 480)
    qtbot.wait(80)
    scope = item(review, "reviewScopeCombo")
    apply = item(review, "reviewApplyButton")
    body = item(review, "reviewScrollArea")

    assert scope.mapToScene(QPointF()).y() < body.mapToScene(QPointF()).y()
    assert apply.mapToScene(QPointF()).y() >= body.mapToScene(QPointF(0, body.height())).y()
    assert apply.mapToScene(QPointF(0, apply.height())).y() <= review.height()
    assert review.width() == 800 and review.height() == 480


def test_table_default_visibility_matches_widgets_and_field_cannot_be_hidden(parity_scene):
    window, _backend = parity_scene
    files = item(window, "fileTable")
    review = window.findChild(QQuickWindow, "metadataReviewWindow")
    fields = item(review, "reviewTable")

    hidden_files = files.property("hiddenColumns")
    hidden_fields = fields.property("hiddenColumns")
    hidden_files = hidden_files.toVariant() if hasattr(hidden_files, "toVariant") else hidden_files
    hidden_fields = hidden_fields.toVariant() if hasattr(hidden_fields, "toVariant") else hidden_fields
    assert hidden_files == [6, 7, 10, 11]
    assert hidden_fields == [5]
    assert fields.property("protectedColumn") == 0


def test_enter_in_review_cannot_choose_metadata_or_start_apply(parity_scene, qtbot):
    window, backend = parity_scene
    backend.selectFile("first", False)
    backend.setIncluded("first", True)
    backend.openReview()
    review = window.findChild(QQuickWindow, "metadataReviewWindow")
    qtbot.waitUntil(review.isVisible)
    review.requestActivate()
    item(review, "reviewTable").forceActiveFocus()
    snapshot = backend.session_state
    QTest.keyClick(review, Qt.Key.Key_Return)

    assert backend.session_state is snapshot
    assert not backend.editing
    assert not backend.applyUi.summaryVisible
    assert not backend.executor.pending


@pytest.mark.parametrize("entry_point", ["button", "context_menu"])
def test_use_candidate_ui_applies_the_selected_provider_value(parity_scene, qtbot, entry_point):
    window, backend = parity_scene
    state = make_selected_session((proposal(MetadataField.TITLE, "Provider title"),))
    backend.set_state(state)
    file_id = state.groups[0].group.files[0].file_id
    backend.selectFile(file_id, False)
    backend.selectField("title")
    backend.openReview()
    review = window.findChild(QQuickWindow, "metadataReviewWindow")
    qtbot.waitUntil(review.isVisible)
    review.requestActivate()
    assert QTest.qWaitForWindowActive(review, 2000)
    assert state.groups[0].reviewed_files[0].change_set.final_metadata.title == "Overture"

    if entry_point == "button":
        click_item(review, "useProposedButton")
    else:
        # Showing a QQuickWindow exposes it before its next scene-graph frame;
        # allow the new review model to lay out its real delegates first.
        qtbot.wait(50)
        table = item(review, "reviewTable")
        row_centre = table.property("headerHeight") + table.property("rowHeight") / 2 + 1
        point = table.mapToScene(QPointF(40, row_centre)).toPoint()
        QTest.mouseClick(review, Qt.MouseButton.RightButton, pos=point)
        command = item(review, "useCandidateContextAction")
        qtbot.waitUntil(command.isVisible)
        click_item(review, "useCandidateContextAction")

    changed = backend.session_state
    assert field_review(changed, MetadataField.TITLE).decision is FieldDecisionKind.USE_PROPOSAL
    assert changed.groups[0].reviewed_files[0].change_set.final_metadata.title == "Provider title"
    assert backend.includedFileIds == []
    assert not backend.executor.pending


def test_safe_additions_button_changes_missing_values_and_preserves_existing_values(parity_scene, qtbot):
    window, backend = parity_scene
    state = make_selected_session((
        proposal(MetadataField.ARTISTS, ("Provider artist",)),
        proposal(MetadataField.GENRES, ("Different genre",)),
        proposal(MetadataField.DATE, "2026", FieldConfidence.LOW),
    ), metadata=MetadataSnapshot(title="Existing title", album="Album", track=Position(1), genres=("My genre",)))
    backend.set_state(state)
    backend.selectFile(state.groups[0].group.files[0].file_id, False)
    backend.openReview()
    review = window.findChild(QQuickWindow, "metadataReviewWindow")
    qtbot.waitUntil(review.isVisible)
    review.requestActivate()
    assert QTest.qWaitForWindowActive(review, 2000)
    assert backend.canAcceptSafe
    click_item(review, "acceptSafeAdditionsButton")

    changed = backend.session_state
    assert field_review(changed, MetadataField.ARTISTS).decision_origin is DecisionOrigin.USER
    final = changed.groups[0].reviewed_files[0].change_set.final_metadata
    assert final.artists == ("Provider artist",)
    assert final.title == "Existing title"
    assert final.genres == ("My genre",)
    assert final.date is None
    assert backend.includedFileIds == []
    assert not backend.executor.pending
