"""Exercise lookup parity through real QML controls and immutable session state."""

import json
from contextlib import contextmanager
from dataclasses import replace

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPointF, Qt
from PySide6.QtQuick import QQuickItem, QQuickWindow
from PySide6.QtTest import QTest

from metadata_polisher.application.lookup import LookupService
from metadata_polisher.infrastructure.settings import AppSettings, UiSettings, load_settings, save_settings
from metadata_polisher.providers.base import RequestContext
from metadata_polisher.providers.coordinator import ProviderCoordinator
from metadata_polisher.session.state import GroupSelection, GroupState, SessionState
from metadata_polisher.ui.quick.application import create_quick_engine
from metadata_polisher.ui.quick.backend import QuickBackend
from tests.ui.helpers import ControlledExecutor
from tests.ui.test_quick_window import click_item
from tests.unit.application.test_lookup_service import (
    LookupFakeProvider,
    make_candidate,
    make_group,
    make_media_file,
    make_medium,
)
from tests.unit.session.test_mapping_editing import partial_session


def candidate_state():
    """Use two media from one release so a release-only selection would be wrong."""
    group = make_group(make_media_file("01.flac", title="Opening"))
    release = make_candidate(
        "catalogue", "same-release", title="Album Evidence",
        media=(make_medium("Opening"), make_medium("Second disc", medium_number=2)),
    )
    service = LookupService(ProviderCoordinator((LookupFakeProvider("catalogue", (release,)),)))
    result = service.search_and_rank_group(group, RequestContext("offline-parity", "eng"))
    state = SessionState(
        root=group.files[0].path.parent,
        groups=(GroupState(
            group=group, candidate_lookup=result,
            lookup_result=result.lookup_result, release_ranking=result.release_ranking,
        ),),
        selection=GroupSelection(group.group_id),
    )
    return state, service


@contextmanager
def lookup_scene(qtbot, *, state=None, settings=None, settings_file=None):
    backend = QuickBackend(
        state=state or partial_session(), executor=ControlledExecutor(),
        settings=settings, settings_file=settings_file,
    )
    warnings = []
    engine = create_quick_engine(backend, warnings=warnings)
    root = engine.rootObjects()[0]

    try:
        root.show()
        yield root, backend
    finally:
        for window in root.findChildren(QQuickWindow):
            window.hide()

        root.hide()
        backend.shutdown()
        engine.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        assert not warnings, warnings


def activate(root, name):
    window = root.findChild(QQuickWindow, name)
    assert window is not None
    window.requestActivate()
    assert QTest.qWaitForWindowActive(window, 2000)
    return window


def visual_items(window):
    """Repeater delegates belong to the visual tree, not necessarily QObject's tree."""
    pending = [window.contentItem()]
    items = []

    while pending:
        item = pending.pop()
        items.append(item)
        pending.extend(item.childItems())

    return items


def text_item(window, text):
    matches = [item for item in visual_items(window) if item.property("text") == text and item.isVisible()]
    assert matches, text
    return max(matches, key=lambda item: item.width())


@pytest.mark.parametrize("field", ["quickSearchAlbum", "quickSearchArtists"])
@pytest.mark.parametrize("key", [Qt.Key.Key_Return, Qt.Key.Key_Enter])
def test_search_enter_accepts_the_current_draft(qtbot, field, key):
    with lookup_scene(qtbot) as (root, backend):
        lookup = backend.lookupUi
        assert lookup.beginSearch()
        search = activate(root, "quickSearchWindow")
        search.findChild(QQuickItem, "quickSearchAlbum").setProperty("text", "Edited album")
        search.findChild(QQuickItem, "quickSearchArtists").setProperty("text", "One; Two")
        click_item(search, field)
        QTest.keyClick(search, key)

        qtbot.waitUntil(lambda: not lookup.searchVisible, timeout=700)
        query = backend.session_state.groups[0].search_query_override
        assert query.album == "Edited album"
        assert query.artists == ("One", "Two")
        assert not backend.executor.pending


def test_search_enter_commits_the_year_text_still_being_edited(qtbot):
    with lookup_scene(qtbot) as (root, backend):
        lookup = backend.lookupUi
        assert lookup.beginSearch()
        search = activate(root, "quickSearchWindow")
        year = search.findChild(QQuickItem, "quickSearchYear")
        editor = year.property("contentItem")
        editor.forceActiveFocus()
        QTest.keyClick(search, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)

        for digit in (Qt.Key.Key_1, Qt.Key.Key_9, Qt.Key.Key_9, Qt.Key.Key_8):
            QTest.keyClick(search, digit)

        QTest.keyClick(search, Qt.Key.Key_Return)
        qtbot.waitUntil(lambda: not lookup.searchVisible, timeout=700)
        assert backend.session_state.groups[0].search_query_override.year == 1998


@pytest.mark.parametrize("name, preference, opener", [
    ("quickSearchWindow", "SearchTermsDialog", "beginSearch"),
    ("quickCandidateWindow", "CandidateDialog", "showCandidates"),
    ("quickMappingWindow", "TrackMappingDialog", "beginMapping"),
    ("quickMatchExplanationWindow", "MatchExplanationDialog", "showWhy"),
])
def test_lookup_windows_restore_and_capture_original_preference_keys(qtbot, tmp_path, name, preference, opener):
    path = tmp_path / "settings.json"
    settings = AppSettings(ui=UiSettings(dialog_sizes={preference: (750, 480)}))
    save_settings(path, settings)

    with lookup_scene(qtbot, settings=settings, settings_file=path) as (root, backend):
        lookup = backend.lookupUi

        if opener == "showWhy":
            lookup.showCandidates()
            lookup.selectCandidate(lookup.candidates[0]["key"])

        getattr(lookup, opener)()
        window = activate(root, name)
        assert (window.width(), window.height()) == (750, 480)
        window.resize(780, 500)
        window.close()
        assert backend.layoutUi.persist()
        assert load_settings(path).settings.ui.dialog_sizes[preference] == (780, 500)


@pytest.mark.parametrize("name, preference, opener, column, widths", [
    ("quickCandidateWindow", "CandidateDialog/candidateTable", "showCandidates", "Engine",
     (177, 100, 340, 85, 160, 100, 145, 150)),
    ("quickMappingWindow", "TrackMappingDialog/trackMappingTable", "beginMapping", "Local file",
     (277, 180, 70, 280, 220)),
    ("quickMatchExplanationWindow", "MatchExplanationDialog/matchEvidenceTable", "showWhy", "Reason",
     (277, 95, 420)),
])
def test_lookup_tables_restore_original_column_preferences(qtbot, name, preference, opener, column, widths):
    settings = AppSettings(ui=UiSettings(column_widths={preference: widths}))

    with lookup_scene(qtbot, settings=settings) as (root, backend):
        lookup = backend.lookupUi

        if opener == "showWhy":
            lookup.showCandidates()
            lookup.selectCandidate(lookup.candidates[0]["key"])

        getattr(lookup, opener)()
        window = activate(root, name)
        header = text_item(window, column)
        assert header.width() == widths[0]


def test_candidate_resize_persists_without_retargeting_the_selected_medium(qtbot):
    state, service = candidate_state()

    with lookup_scene(qtbot, state=state) as (root, backend):
        lookup = backend.lookupUi
        lookup._service = service
        lookup.showCandidates()
        window = activate(root, "quickCandidateWindow")
        initial = lookup.candidates[0]["key"]
        lookup.selectCandidate(initial)
        header = text_item(window, "Engine")
        before = header.width()
        start = header.mapToScene(QPointF(before - 2, header.height() / 2)).toPoint()
        finish = start + QPointF(45, 0).toPoint()
        QTest.mousePress(window, Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(window, finish, delay=50)
        QTest.mouseRelease(window, Qt.MouseButton.LeftButton, pos=finish)

        widths = backend.layoutUi.columnWidths("CandidateDialog/candidateTable", [100] * 8)
        assert widths[0] >= before + 35
        assert lookup.candidateKey == initial

        # Changing order cannot change which disc Enter will submit to the service.
        lookup.sortCandidates("disc", True)
        assert lookup.candidateKey == initial
        table = window.findChild(QQuickItem, "quickCandidateTable")
        table.forceActiveFocus()
        QTest.keyClick(window, Qt.Key.Key_Return)
        assert len(backend.executor.pending) == 1

        with qtbot.waitSignal(backend.bridge.completed):
            backend.executor.run_next()

        assert backend.session_state.groups[0].selected_release.identity == tuple(json.loads(initial))


def test_candidate_column_menu_restores_optional_source_column(qtbot):
    state, _service = candidate_state()

    with lookup_scene(qtbot, state=state) as (root, backend):
        lookup = backend.lookupUi
        lookup.showCandidates()
        window = activate(root, "quickCandidateWindow")
        header = text_item(window, "Engine")
        position = header.mapToScene(QPointF(header.width() / 2, header.height() / 2)).toPoint()
        QTest.mouseClick(window, Qt.MouseButton.RightButton, pos=position)
        qtbot.wait(50)
        actions = [
            item for item in visual_items(window)
            if item.property("text") == "Source" and item.property("checkable") is True
            and item.isVisible() and item.property("checked") is False
            and "MenuItem" in item.metaObject().className()
        ]
        assert actions, "The header context menu must expose every column, including hidden Source."
        action = actions[0]
        position = action.mapToScene(QPointF(action.width() / 2, action.height() / 2)).toPoint()
        QTest.mouseClick(window, Qt.MouseButton.LeftButton, pos=position)
        assert 1 not in backend.layoutUi.hiddenColumns("CandidateDialog/candidateTable", [1, 5])


def test_hidden_candidate_columns_can_be_restored_from_the_blank_header(qtbot):
    state, _service = candidate_state()
    settings = AppSettings(ui=UiSettings(hidden_columns={"CandidateDialog/candidateTable": tuple(range(8))}))

    with lookup_scene(qtbot, state=state, settings=settings) as (root, backend):
        backend.lookupUi.showCandidates()
        window = activate(root, "quickCandidateWindow")
        table = window.findChild(QQuickItem, "quickCandidateTable")
        position = table.mapToScene(QPointF(40, 12)).toPoint()
        QTest.mouseClick(window, Qt.MouseButton.RightButton, pos=position)
        qtbot.wait(50)
        actions = [
            item for item in visual_items(window)
            if item.property("text") == "Engine" and item.property("checkable") is True
            and item.isVisible() and "MenuItem" in item.metaObject().className()
        ]
        assert actions, "A blank header must retain the column menu when every column is hidden."
        action = actions[0]
        position = action.mapToScene(QPointF(action.width() / 2, action.height() / 2)).toPoint()
        QTest.mouseClick(window, Qt.MouseButton.LeftButton, pos=position)

        assert 0 not in backend.layoutUi.hiddenColumns("CandidateDialog/candidateTable", list(range(8)))
        assert text_item(window, "Engine").width() > 0


def test_mapping_has_separate_assignment_and_evidence_columns_and_cancel_discards_draft(qtbot):
    state = partial_session()
    group = state.groups[0]
    first = group.group.files[0]
    second = replace(first, path=first.path.with_name("02.flac"), file_id="mapping-second")
    mapping = replace(group.automatic_track_mapping, unmatched_local_file_ids=(first.file_id, second.file_id))
    group = replace(group, group=replace(group.group, files=(first, second)), automatic_track_mapping=mapping)
    state = replace(state, groups=(group,))

    with lookup_scene(qtbot, state=state) as (root, backend):
        lookup = backend.lookupUi
        captured = backend.session_state
        assert lookup.beginMapping()
        window = activate(root, "quickMappingWindow")
        assert any(
            str(item.property("text") or "").startswith("Disc 1: assign")
            for item in visual_items(window)
        )
        provider = text_item(window, "Provider track")
        evidence = text_item(window, "Assignment evidence")
        assert provider.mapToScene(QPointF()).x() < evidence.mapToScene(QPointF()).x()
        assert lookup.assignTrack(first.file_id, 0)
        accepted_rows = lookup.mappingRows
        assert not lookup.assignTrack(second.file_id, 0)
        assert lookup.mappingRows == accepted_rows
        assert lookup.mappingError
        assert backend.session_state is captured
        lookup.cancelMapping()
        assert backend.session_state is captured


def test_mapping_selector_keyboard_edits_the_draft_and_cancel_preserves_session(qtbot):
    with lookup_scene(qtbot) as (root, backend):
        lookup = backend.lookupUi
        captured = backend.session_state
        assert lookup.beginMapping()
        window = activate(root, "quickMappingWindow")
        file_id = captured.groups[0].group.files[0].file_id
        selectors = [
            item for item in visual_items(window)
            if item.objectName() == "mappingTrackSelector_" + file_id
        ]
        assert selectors
        selector = selectors[0]
        selector.forceActiveFocus()
        QTest.keyClick(window, Qt.Key.Key_Down)

        qtbot.waitUntil(lambda: lookup.mappingRows[0]["track"] == 0)
        assert lookup.mappingRows[0]["assignment"] != "Unmapped"
        assert lookup.mappingRows[0]["evidence"].startswith("Manual assignment")
        table = window.findChild(QQuickItem, "quickMappingTable")
        assert "Manual assignment" in table.property("details")
        assert backend.session_state is captured
        lookup.cancelMapping()
        assert backend.session_state is captured


def test_mapping_selector_keeps_focus_across_successive_keyboard_edits(qtbot):
    with lookup_scene(qtbot) as (root, backend):
        lookup = backend.lookupUi
        captured = backend.session_state
        assert lookup.beginMapping()
        window = activate(root, "quickMappingWindow")
        file_id = captured.groups[0].group.files[0].file_id
        selector = next(
            item for item in visual_items(window)
            if item.objectName() == "mappingTrackSelector_" + file_id
        )
        selector.forceActiveFocus()
        QTest.keyClick(window, Qt.Key.Key_Down)
        qtbot.waitUntil(lambda: lookup.mappingRows[0]["track"] == 0)

        # Publishing the new draft projection must not steal focus from the
        # editor. A second key goes to the same control without another click.
        qtbot.wait(50)
        QTest.keyClick(window, Qt.Key.Key_Up)
        qtbot.waitUntil(lambda: lookup.mappingRows[0]["track"] == -1, timeout=700)
        assert backend.session_state is captured
