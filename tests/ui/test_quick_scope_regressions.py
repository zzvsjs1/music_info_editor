"""QtQuick review gestures preserve the existing selected-scope safety contract."""

from dataclasses import replace
from pathlib import Path

import pytest

from metadata_polisher.domain.media import UnsupportedMediaFile
from metadata_polisher.domain.metadata import FieldReadState, MetadataField
from metadata_polisher.domain.review import DecisionOrigin
from metadata_polisher.ui.quick.backend import QuickBackend
from tests.ui.helpers import ControlledExecutor, blocked_first_session
from tests.unit.session.test_batch_review import make_batch_session
from tests.unit.session.test_review_editing import make_local_session, make_source


@pytest.fixture
def make_backend(qapp):
    instances = []

    def create(state):
        backend = QuickBackend(state=state, executor=ControlledExecutor())
        instances.append(backend)
        return backend

    yield create
    for backend in instances:
        backend.shutdown()


@pytest.mark.parametrize("action", ("clear", "keep_existing", "use_candidate"))
def test_healthy_scope_members_remain_actionable_after_a_stale_album(make_backend, action):
    state = blocked_first_session()
    backend = make_backend(state)
    backend.setReviewScope("library")
    backend.selectField("title")
    assert backend.canEdit
    backend.reviewAction(action)
    assert backend.session_state.groups[0] == state.groups[0]
    assert "3 affected" in backend.status
    assert "1 blocked" in backend.status
    assert all(next(field for field in item.reviews if field.field is MetadataField.TITLE).decision_origin
               is DecisionOrigin.USER for item in backend.session_state.groups[1].reviewed_files)


def test_manual_common_value_applies_healthy_members_and_reports_blocked_members(make_backend):
    state = blocked_first_session()
    backend = make_backend(state)
    backend.setReviewScope("library")
    backend.selectField("title")
    assert backend.beginEdit()
    assert backend.commitEdit("Common correction")
    assert backend.session_state.groups[0] == state.groups[0]
    assert all(item.change_set.final_metadata.title == "Common correction"
               for item in backend.session_state.groups[1].reviewed_files)
    assert "blocked" in backend.status


def test_unreadable_selected_field_does_not_block_readable_selected_field(make_backend):
    state = make_local_session(make_source())
    group = state.groups[0]
    source = group.group.files[0]
    source = replace(source, read_result=replace(source.read_result, field_states={
        **source.read_result.field_states, MetadataField.TITLE: FieldReadState.UNREADABLE,
    }))
    state = replace(state, groups=(replace(group, group=replace(group.group, files=(source,))),))
    backend = make_backend(state)
    backend.selectFile(source.file_id, False)
    backend.selectField("title")
    backend.selectFieldExtended("album", True, False)
    assert backend.canEdit
    backend.reviewAction("clear")
    assert "1 affected" in backend.status
    assert "1 blocked" in backend.status
    assert backend.session_state.groups[0].reviewed_files[0].change_set.final_metadata.album is None


@pytest.mark.parametrize("scope", ("library", "included"))
@pytest.mark.parametrize("action", ("select_all", "clear"))
def test_table_selection_commands_return_review_to_highlighted_scope(make_backend, scope, action):
    state = make_batch_session()
    backend = make_backend(state)
    file_ids = tuple(source.file_id for group in state.groups for source in group.group.files)
    backend.set_included_file_ids(frozenset(file_ids))
    backend.setReviewScope(scope)
    if action == "select_all":
        backend.selectAllFiles()
    else:
        backend.clearSelection()
    assert backend.reviewScope == "selected"
    assert backend.scopeFileIds == (list(file_ids) if action == "select_all" else [])
    assert frozenset(backend.includedFileIds) == frozenset(file_ids)


def test_plain_unsupported_group_selection_clears_old_lookup_targets(make_backend):
    state = make_batch_session()
    state = replace(state, unsupported_files=(UnsupportedMediaFile(Path("library/extra.opus")),))
    backend = make_backend(state)
    assert backend.selectedGroupIds
    backend.selectGroupExtended("@unsupported", False, False)
    assert backend.selectedGroupIds == []
    assert not backend.lookupUi.canFind
    assert backend.scopeFileIds == []


def test_deselected_focused_field_cannot_supply_manual_seed_or_candidate(make_backend):
    backend = make_backend(make_batch_session())
    file_id = backend.session_state.groups[0].group.files[0].file_id
    backend.selectFile(file_id, False)
    backend.selectField("title")
    backend.selectFieldExtended("album", True, False)
    backend.selectFieldExtended("album", True, False)
    assert backend.selectedFields == ["title"]
    assert backend.canUseCandidate
    assert backend.beginEdit()
    assert backend.editLabel == "Title"
    assert backend.editValue == "Local first"


def test_safe_additions_do_not_require_highlighted_fields(make_backend):
    backend = make_backend(make_batch_session())
    file_id = backend.session_state.groups[0].group.files[0].file_id
    backend.selectFile(file_id, False)
    backend.selectField("title")
    backend.selectFieldExtended("title", True, False)
    assert backend.selectedFields == []
    backend.reviewAction("accept_safe_additions")
    reviewed = backend.session_state.groups[0].reviewed_files[0]
    assert (next(field for field in reviewed.reviews if field.field is MetadataField.GENRES).decision_origin
            is DecisionOrigin.USER)


def test_filename_actions_reach_healthy_members_of_mixed_scope(make_backend):
    state = blocked_first_session()
    backend = make_backend(state)
    backend.setReviewScope("library")
    assert backend.canRename
    backend.renameAction(False)
    assert backend.session_state.groups[0] == state.groups[0]
    # Healthy files already keep their filenames; the service reports these as
    # valid no-op members while retaining the separate stale-album blocker.
    assert "3 skipped" in backend.status
    assert "1 blocked" in backend.status
