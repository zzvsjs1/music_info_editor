"""Grouping dialogues only publish a validated, explicitly confirmed snapshot."""

from dataclasses import replace
from pathlib import Path

from metadata_polisher.session.state import GroupSelection, SessionState
from metadata_polisher.ui.quick.backend import QuickBackend
from metadata_polisher.ui.quick.library import QuickLibrary
from tests.ui.helpers import ControlledExecutor, make_group


def test_merge_requires_confirmation_and_rejects_stale_state(qapp):
    state = SessionState(
        root=Path("library"),
        groups=(make_group("a", "one", "One"), make_group("b", "two", "Two")),
        selection=GroupSelection("a"),
    )
    backend = QuickBackend(state=state, executor=ControlledExecutor())
    try:
        facade = QuickLibrary(backend)
        backend.selectGroupExtended("b", True, False)
        facade.merge()
        assert backend.confirmationVisible
        assert len(backend.session_state.groups) == 2
        backend.set_state(replace(backend.session_state, revision=1))
        assert not backend.confirmAction()
        assert len(backend.session_state.groups) == 2
        facade.merge()
        assert backend.confirmAction()
        assert len(backend.session_state.groups) == 1
    finally:
        backend.shutdown()


def test_disc_editor_cancel_and_stale_target_preserve_choices(qapp):
    state = SessionState(root=Path("library"), groups=(make_group("a", "one", "One"),), selection=GroupSelection("a"))
    backend = QuickBackend(state=state, executor=ControlledExecutor())
    try:
        facade = QuickLibrary(backend)
        facade.beginDisc()
        facade.cancelDisc()
        assert backend.session_state is state
        facade.beginDisc()
        backend.set_state(replace(state, revision=1))
        assert not facade.commitDisc(2)
        assert backend.session_state.groups[0].disc_number_override is None
        facade.cancelDisc()
        facade.beginDisc()
        assert facade.commitDisc(2)
        assert backend.session_state.groups[0].disc_number_override == 2
    finally:
        backend.shutdown()
