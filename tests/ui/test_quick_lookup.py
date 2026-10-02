"""QML lookup commands retain service validation and immutable dialogue targets."""

from dataclasses import replace
from pathlib import Path

import pytest
from PySide6.QtCore import QObject, Signal

from metadata_polisher.application.lookup import LookupService
from metadata_polisher.infrastructure.diagnostics import ThreadSafeOperationIds
from metadata_polisher.infrastructure.session_credentials import SessionCredentials
from metadata_polisher.infrastructure.settings import AppSettings
from metadata_polisher.providers.coordinator import ProviderCoordinator
from metadata_polisher.session.state import GroupSelection, GroupState, SessionState, begin_operation, finish_operation
from metadata_polisher.ui.qt_bridge import QtOperationBridge
from metadata_polisher.ui.quick.lookup import QuickLookup
from tests.ui.helpers import ControlledExecutor
from tests.ui.rendering import window_image_scale
from tests.unit.application.test_lookup_service import (
    LookupFakeProvider,
    make_candidate,
    make_group,
    make_media_file,
    make_medium,
)
from tests.unit.session.test_mapping_editing import partial_session


class LookupHost(QObject):
    changed = Signal()
    cancellation_requested = Signal(str)

    def __init__(self, state):
        super().__init__()
        self.session_state = state
        self.app_settings = AppSettings()
        self.credentials = SessionCredentials()
        self._ids = ThreadSafeOperationIds()
        self.selected_group_ids = (state.groups[0].group.group_id,)
        self.executor = ControlledExecutor()
        self.bridge = QtOperationBridge(self.executor, self)
        self.bridge.completed.connect(self._complete)
        self.bridge.failed.connect(self._failed)
        self.bridge.cancelled.connect(lambda operation_id: self._failed(operation_id, None))
        self.status = ""
        self.reducers = {}
        self.review_opened = False

    def _group(self):
        return next((group for group in self.session_state.groups
                     if group.group.group_id == self.session_state.selection.group_id), None)

    def set_state(self, state):
        self.session_state = state
        self.changed.emit()

    def set_status(self, message):
        self.status = message

    def openReview(self):
        self.review_opened = True

    def submit_operation(self, operation_id, kind, targets, work, reducer):
        self.reducers[operation_id] = reducer
        self.set_state(begin_operation(self.session_state, operation_id, kind, targets))
        self.handle = self.bridge.submit(operation_id, work)
        return True

    def _complete(self, operation_id, result):
        reduced = self.reducers.pop(operation_id)(self.session_state, result)
        self.set_state(finish_operation(reduced.state, operation_id))

    def _failed(self, operation_id, error):
        self.set_state(finish_operation(self.session_state, operation_id))

    def cancelScan(self):
        self.handle.cancel()
        self.cancellation_requested.emit(self.handle.operation_id)


@pytest.fixture
def lookup(qapp):
    group = GroupState(group=make_group(make_media_file("01.flac", title="Opening")))
    candidate = make_candidate("catalogue", "one", title="Album Evidence", media=(make_medium("Opening"),))
    provider = LookupFakeProvider("catalogue", (candidate, replace(candidate, release_id="two")))
    state = SessionState(root=Path("library"), groups=(group,), selection=GroupSelection(group.group.group_id))
    host = LookupHost(state)
    facade = QuickLookup(host, service=LookupService(ProviderCoordinator((provider,))))
    yield host, facade, provider
    facade.close()


def complete(qtbot, host):
    with qtbot.waitSignal(host.bridge.completed):
        host.executor.run_next()


def test_qml_lookup_requires_explicit_candidate_and_exposes_full_evidence(lookup, qtbot):
    host, facade, provider = lookup
    facade.findSelected()
    assert host.review_opened
    assert len(host.executor.pending) == 1
    complete(qtbot, host)
    assert facade.candidateVisible
    assert len(facade.candidates) == 2
    assert not provider.enrich_calls
    assert host.session_state.groups[0].selected_release is None

    facade.selectCandidate(facade.candidates[0]["key"])
    facade.showWhy()
    evidence = host.session_state.groups[0].release_ranking.entries[0].result.evidence
    assert all(item.code in facade.whyText and item.detail in facade.whyText for item in evidence)
    assert facade.whyRows == [
        {"reason": item.code, "contribution": f"{item.contribution:g}", "detail": item.detail}
        for item in evidence
    ]
    assert facade.chooseCandidate()
    assert not facade.chooseCandidate()
    complete(qtbot, host)
    assert len(provider.enrich_calls) == 1
    assert host.session_state.groups[0].selected_release is not None
    facade.showCandidates()
    assert facade.candidates[0]["coverage"] == "1 / 1 local tracks"


def test_candidate_confirmation_rejects_changed_group_evidence(lookup, qtbot):
    host, facade, _ = lookup
    facade.findSelected()
    complete(qtbot, host)
    facade.selectCandidate(facade.candidates[0]["key"])
    old = host.session_state.groups[0]
    host.set_state(replace(host.session_state, groups=(replace(old, revision=old.revision + 1),),
                           revision=host.session_state.revision + 1))
    assert not facade.chooseCandidate()
    assert not host.executor.pending
    assert "changed" in host.status.lower()


def test_cancel_after_worker_completion_does_not_restart_bulk_queue(lookup, qapp):
    host, facade, _ = lookup
    first = host.session_state.groups[0]
    second_source = make_media_file("02.flac", title="Opening")
    second = GroupState(group=replace(make_group(second_source), group_id="second"))
    host.set_state(replace(host.session_state, groups=(first, second)))
    host.selected_group_ids = (first.group.group_id, "second")
    facade.findSelected()
    host.executor.run_next()
    host.cancelScan()
    qapp.processEvents()
    assert not host.executor.pending
    assert host.session_state.groups[0].candidate_lookup is not None
    assert host.session_state.groups[1].candidate_lookup is None
    assert not facade.candidateVisible
    assert "stopped" in host.status.lower()


def test_search_draft_rejects_stale_session_and_preserves_other_query_hints(lookup):
    host, facade, _ = lookup
    assert facade.beginSearch()
    host.set_state(replace(host.session_state, revision=host.session_state.revision + 1))
    assert not facade.commitSearch("New album", "Artist One; Artist Two", 2024)
    assert host.session_state.groups[0].search_query_override is None
    facade.cancelSearch()
    assert facade.beginSearch()
    assert facade.commitSearch("New album", "Artist One; Artist Two", 2024)
    query = host.session_state.groups[0].search_query_override
    assert query.album == "New album"
    assert query.artists == ("Artist One", "Artist Two")
    assert query.year == 2024
    assert query.distinctive_titles


def test_mapping_draft_only_publishes_after_acceptance(qapp):
    host = LookupHost(partial_session())
    facade = QuickLookup(host)
    original = host.session_state
    file_id = original.groups[0].group.files[0].file_id
    assert facade.beginMapping()
    assert facade.assignTrack(file_id, 0)
    assert host.session_state is original
    assert facade.commitMapping()
    group = host.session_state.groups[0]
    assert group.reviewed_files[0].track_mapping_resolved
    assert group.automatic_track_mapping is original.groups[0].automatic_track_mapping
    assert group.manual_track_mapping is not None
    facade.close()


def test_language_inheritance_and_invalid_search_input_are_explicit(lookup):
    host, facade, _ = lookup
    facade.setLanguage("auto")
    assert host.session_state.groups[0].language_override == "auto"
    facade.setLanguage("")
    assert host.session_state.groups[0].language_override is None
    assert facade.beginSearch()
    assert not facade.commitSearch("Album", "", 10000)
    assert facade.searchVisible
    assert facade.searchError


def test_connection_test_uses_explicit_injected_service_without_live_runtime(lookup, monkeypatch):
    from metadata_polisher.infrastructure.settings import ProvidersSettings

    _host, facade, _provider = lookup

    def unexpected_runtime(*args, **kwargs):
        pytest.fail("An injected provider boundary must never create live provider clients")

    monkeypatch.setattr("metadata_polisher.ui.quick.lookup.ProviderRuntime", unexpected_runtime)
    assert facade.connection_service(ProvidersSettings("vgmdb")) is facade._service


def test_find_all_requires_an_idle_searchable_incomplete_group(lookup):
    from metadata_polisher.session.state import OperationKind, mark_groups_requires_rescan

    host, facade, _provider = lookup
    assert facade.canFindAll
    original = host.session_state
    host.set_state(begin_operation(original, "TEST-0001", OperationKind.LOOKUP,
                                   (original.groups[0].group.group_id,)))
    assert not facade.canFindAll
    host.set_state(mark_groups_requires_rescan(original, (original.groups[0].group.group_id,)))
    assert not facade.canFindAll


def test_musicbrainz_contact_is_visible_before_any_candidate_window(qapp, qtbot, monkeypatch):
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtQuick import QQuickWindow

    from metadata_polisher.ui.quick.application import create_quick_engine
    from metadata_polisher.ui.quick.backend import QuickBackend

    monkeypatch.delenv("METADATA_POLISHER_MUSICBRAINZ_CONTACT", raising=False)
    group = GroupState(group=make_group(make_media_file("01.flac", title="Opening")))
    state = SessionState(root=Path("library"), groups=(group,), selection=GroupSelection(group.group.group_id))
    backend = QuickBackend(state=state, executor=ControlledExecutor())
    warnings = []
    engine = create_quick_engine(backend, warnings=warnings)
    window = engine.rootObjects()[0]
    try:
        facade = backend.lookupUi
        facade.findSelected()
        assert facade.contactVisible
        assert not facade.candidateVisible
        contact = window.findChild(QQuickWindow, "quickMusicBrainzContactWindow")
        assert contact is not None
        qtbot.waitUntil(contact.isVisible)
        assert contact.transientParent() is window
        assert not backend.executor.pending
        assert facade._runtime is None
        assert not warnings, warnings
        facade.cancelContact()
    finally:
        for child in window.findChildren(QQuickWindow):
            child.hide()
        window.hide()
        backend.shutdown()
        engine.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


@pytest.fixture
def musicbrainz_contact_scene(qapp, qtbot, monkeypatch):
    """Open the real QML contact window without performing a provider request."""
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtQuick import QQuickItem, QQuickWindow
    from PySide6.QtTest import QTest

    from metadata_polisher.ui.quick.application import create_quick_engine
    from metadata_polisher.ui.quick.backend import QuickBackend

    monkeypatch.delenv("METADATA_POLISHER_MUSICBRAINZ_CONTACT", raising=False)
    group = GroupState(group=make_group(make_media_file("01.flac", title="Opening")))
    state = SessionState(root=Path("library"), groups=(group,), selection=GroupSelection(group.group.group_id))
    backend = QuickBackend(state=state, executor=ControlledExecutor())
    warnings = []
    engine = create_quick_engine(backend, warnings=warnings)
    window = engine.rootObjects()[0]

    try:
        window.show()
        facade = backend.lookupUi
        facade.findSelected()
        contact = window.findChild(QQuickWindow, "quickMusicBrainzContactWindow")
        assert contact is not None
        qtbot.waitUntil(contact.isVisible)
        contact.requestActivate()
        assert QTest.qWaitForWindowActive(contact, 2000)
        field = contact.findChild(QQuickItem, "quickMusicBrainzContact")
        assert field is not None
        yield contact, field, backend, facade
    finally:
        if backend.lookupUi.contactVisible:
            backend.lookupUi.cancelContact()

        for child in window.findChildren(QQuickWindow):
            child.hide()

        window.hide()
        backend.shutdown()
        engine.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        assert not warnings, warnings


def test_musicbrainz_contact_is_compact_with_reachable_actions(musicbrainz_contact_scene, qtbot):
    from PySide6.QtCore import QPointF
    from PySide6.QtQuick import QQuickItem

    contact, field, _backend, _facade = musicbrainz_contact_scene
    qtbot.wait(50)

    # The previous large window left a broad blank band below the input. Check
    # the actual laid-out controls so a smaller nominal size alone cannot pass.
    actions = [
        item for item in contact.findChildren(QQuickItem)
        if item.property("text") in {"OK", "Cancel"} and item.isVisible() and item.width() >= 60
    ]
    assert len(actions) == 2
    button_top = min(button.mapToScene(QPointF(0, 0)).y() for button in actions)
    field_bottom = field.mapToScene(QPointF(0, field.height())).y()

    assert contact.width() < 560
    assert contact.height() < 230
    assert 0 <= button_top - field_bottom <= max(button.height() for button in actions)

    for button in actions:
        top = button.mapToScene(QPointF(0, 0)).y()
        bottom = button.mapToScene(QPointF(0, button.height())).y()
        assert 0 <= top < bottom <= contact.height()


@pytest.mark.parametrize("field_state", ["normal", "focused", "invalid"])
def test_native_contact_top_border_is_continuous(musicbrainz_contact_scene, qtbot, field_state):
    from math import ceil

    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QGuiApplication

    if QGuiApplication.platformName() != "windows":
        pytest.skip("The reported border gap requires native Windows painting.")

    contact, field, _backend, facade = musicbrainz_contact_scene

    if field_state == "normal":
        field.setFocus(False)
    else:
        field.forceActiveFocus()

    if field_state == "invalid":
        field.setProperty("text", "bad contact")
        assert not facade.setContact("bad contact")

    qtbot.wait(100)
    assert field.hasActiveFocus() == (field_state != "normal")
    image = contact.grabWindow()
    scale_x, scale_y = window_image_scale(contact, image)
    origin = field.mapToScene(QPointF())
    y = ceil(origin.y() * scale_y)

    # Stay on the straight top edge, outside both rounded corners. At 125%
    # the native frame loses a strip near its right corner despite matching
    # the input's size. Compare actual painted pixels rather than item bounds.
    def edge_colour(fraction):
        return image.pixelColor(round((origin.x() + field.width() * fraction) * scale_x), y)

    expected = edge_colour(0.5)
    interior = image.pixelColor(round((origin.x() + field.width() / 2) * scale_x),
                                round((origin.y() + field.height() / 2) * scale_y))
    assert expected != interior, "The top border must be painted."

    for fraction in (0.1, 0.25, 0.75, 0.9, 0.94):
        assert edge_colour(fraction) == expected, f"Broken top border at {fraction:.0%} ({scale_x:g}x DPI)"


def test_musicbrainz_contact_error_is_inline_and_clears_on_edit(musicbrainz_contact_scene, qtbot):
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtQml import QQmlProperty
    from PySide6.QtQuick import QQuickItem
    from PySide6.QtTest import QTest

    contact, field, backend, facade = musicbrainz_contact_scene
    field.forceActiveFocus()
    field.setProperty("text", "not a contact")
    QTest.keyClick(contact, Qt.Key.Key_Return)
    qtbot.waitUntil(lambda: bool(facade.contactError))

    # Validation must preserve the draft and keep the window open for repair.
    assert facade.contactVisible
    assert field.property("text") == "not a contact"
    assert not backend.executor.pending

    wrapper = contact.findChild(QQuickItem, "quickMusicBrainzContactField")
    message = contact.findChild(QQuickItem, "quickMusicBrainzContactError")
    border = contact.findChild(QQuickItem, "quickMusicBrainzContactBorder")
    assert wrapper is not None and message is not None and border is not None
    description = next(item for item in contact.findChildren(QQuickItem)
                       if str(item.property("text") or "").startswith("MusicBrainz requires a public project URL"))
    native_background = field.property("background")
    assert native_background is not None

    # The binding changes immediately; Qt places the newly visible error in
    # the following layout pass.
    qtbot.waitUntil(lambda: message.width() > 0 and message.mapToScene(QPointF(0, 0)).y()
                    >= field.mapToScene(QPointF(0, field.height())).y())
    assert message.isVisible() and message.property("text") == facade.contactError
    assert border.isVisible() and border.opacity() > 0
    assert not native_background.isVisible()
    assert field.property("leftPadding") == 12 and field.property("rightPadding") == 12
    assert field.property("topPadding") == 7 and field.property("bottomPadding") == 7
    description_left = description.mapToScene(QPointF(description.property("leftPadding"), 0)).x()
    message_left = message.mapToScene(QPointF(message.property("leftPadding"), 0)).x()
    assert abs(message_left - description_left) <= 1

    error_colour = message.property("color")
    assert error_colour.red() > error_colour.green() and error_colour.red() > error_colour.blue()

    field_top = field.mapToScene(QPointF(0, 0)).y()
    field_bottom = field.mapToScene(QPointF(0, field.height())).y()
    field_left = field.mapToScene(QPointF(0, 0)).x()
    field_right = field.mapToScene(QPointF(field.width(), 0)).x()
    message_top = message.mapToScene(QPointF(0, 0)).y()
    border_top = border.mapToScene(QPointF(0, 0)).y()
    border_bottom = border.mapToScene(QPointF(0, border.height())).y()
    border_left = border.mapToScene(QPointF(0, 0)).x()
    border_right = border.mapToScene(QPointF(border.width(), 0)).x()
    assert 0 <= message_top - field_bottom <= 12
    assert abs(border_top - field_top) <= 0.5 and abs(border_bottom - field_bottom) <= 0.5
    assert abs(border_left - field_left) <= 0.5 and abs(border_right - field_right) <= 0.5
    assert message.mapToScene(QPointF(0, message.height())).y() <= wrapper.mapToScene(
        QPointF(0, wrapper.height())
    ).y()
    assert border.width() == field.width()

    # An edit retires the stale validation state while retaining the draft.
    QTest.keyClick(contact, Qt.Key.Key_X)
    qtbot.waitUntil(lambda: not facade.contactError)
    assert field.property("text") == "not a contactx"
    assert not native_background.isVisible()
    assert not message.isVisible() and border.isVisible()
    assert QQmlProperty(border, "border.color").read() != error_colour

    field.setProperty("text", "me@example.org")
    QTest.keyClick(contact, Qt.Key.Key_Return)
    qtbot.waitUntil(lambda: not facade.contactVisible)
    assert len(backend.executor.pending) == 1


def test_musicbrainz_contact_actions_survive_a_short_viewport(musicbrainz_contact_scene, qtbot):
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtQuick import QQuickItem
    from PySide6.QtTest import QTest

    contact, field, _backend, facade = musicbrainz_contact_scene
    field.forceActiveFocus()
    field.setProperty("text", "bad contact")
    QTest.keyClick(contact, Qt.Key.Key_Return)
    qtbot.waitUntil(lambda: bool(facade.contactError))
    qtbot.wait(50)

    # Force the available client height below the form's natural height, as
    # can happen on a short desktop or with enlarged system text.
    contact.setMinimumHeight(1)
    contact.resize(contact.width(), 135)
    qtbot.waitUntil(lambda: contact.height() <= 140)
    qtbot.wait(50)
    assert contact.height() <= 140

    actions = [
        item for item in contact.findChildren(QQuickItem)
        if item.property("text") in {"OK", "Cancel"} and item.isVisible() and item.width() >= 60
    ]
    assert len(actions) == 2
    button_top = min(action.mapToScene(QPointF(0, 0)).y() for action in actions)

    for action in actions:
        top_left = action.mapToScene(QPointF(0, 0))
        bottom_right = action.mapToScene(QPointF(action.width(), action.height()))
        assert 0 <= top_left.x() < bottom_right.x() <= contact.width()
        assert 0 <= top_left.y() < bottom_right.y() <= contact.height()

    message = contact.findChild(QQuickItem, "quickMusicBrainzContactError")
    assert message is not None and message.isVisible()
    field_top = field.mapToScene(QPointF(0, 0)).y()
    message_bottom = message.mapToScene(QPointF(0, message.height())).y()
    body_fits = field_top >= 0 and message_bottom <= button_top

    # If the full form cannot fit, a clipped scroll viewport must end above
    # the actions and contain both the input and its error message.
    field_ancestors = []
    ancestor = field.parentItem()
    while ancestor is not None:
        field_ancestors.append(ancestor)
        ancestor = ancestor.parentItem()

    message_ancestors = []
    ancestor = message.parentItem()
    while ancestor is not None:
        message_ancestors.append(ancestor)
        ancestor = ancestor.parentItem()

    scrollable_body = any(
        ancestor in message_ancestors
        and bool(ancestor.property("clip"))
        and isinstance(ancestor.property("contentHeight"), (int, float))
        and ancestor.property("contentHeight") > ancestor.height()
        and ancestor.mapToScene(QPointF(0, 0)).y() >= 0
        and ancestor.mapToScene(QPointF(0, ancestor.height())).y() <= button_top
        for ancestor in field_ancestors
    )
    assert body_fits or scrollable_body


def test_candidate_keyboard_navigation_tracks_selected_identity_after_sort(lookup, qtbot):
    from PySide6.QtCore import QPointF, Qt, QUrl
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtQuick import QQuickItem
    from PySide6.QtQuickControls2 import QQuickStyle
    from PySide6.QtTest import QTest

    from tests.ui.test_quick_window import click_item

    host, facade, provider = lookup
    provider.candidates = tuple(replace(candidate, date=str(2000 + index))
                                for index, candidate in enumerate(provider.candidates))
    facade.findSelected()
    complete(qtbot, host)
    facade.sortCandidates("date", False)
    initial_key = facade.candidates[0]["key"]

    if QQuickStyle.name() != "Fusion":
        QQuickStyle.setStyle("Fusion")

    engine = QQmlApplicationEngine()
    engine.setInitialProperties({"lookup": facade})
    qml = Path(__file__).parents[2] / "src/metadata_polisher/ui/quick/qml/CandidateWindow.qml"
    engine.load(QUrl.fromLocalFile(str(qml)))

    try:
        window = engine.rootObjects()[0]
        window.requestActivate()
        qtbot.waitUntil(window.isActive)
        click_item(window, "quickCandidateTable", QPointF(240, 50))
        assert facade.candidateKey == initial_key
        table = window.findChild(QQuickItem, "quickCandidateTable")
        facade.sortCandidates("date", True)
        assert facade.candidates[1]["key"] == initial_key
        assert table.property("currentIndex") == 1
        table.forceActiveFocus()
        QTest.keyClick(window, Qt.Key.Key_Up)
        assert facade.candidateKey == facade.candidates[0]["key"]
    finally:
        facade.closeCandidates()
        window.hide()
        engine.deleteLater()
