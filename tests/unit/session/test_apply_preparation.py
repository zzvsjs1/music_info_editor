# A prepared request freezes review choices, revisions and output settings.
# The worker still owns the later checks of actual paths, adapters and free space.

from pathlib import Path

import pytest

from metadata_polisher.application.changes import RenameDecision
from metadata_polisher.infrastructure.settings import AppSettings, BackupSettings, RenameSettings, ReportsSettings
from metadata_polisher.session.apply_preparation import prepare_apply_request
from metadata_polisher.session.review_editing import refresh_rename_previews, set_rename_decision
from tests.unit.session.test_lookup_editing import make_selected_session


def test_prepare_apply_captures_review_lineage_and_output_policies():
    state = make_selected_session()
    source = state.groups[0].group.files[0]
    settings = AppSettings(backup=BackupSettings(True, "backups"), reports=ReportsSettings(True, ""))
    request = prepare_apply_request(state, (source.file_id,), settings, "APPLY-0001")
    assert request.base_session_revision == state.revision
    assert request.base_library_revision == state.library_revision
    assert request.groups[0].base_group_revision == state.groups[0].revision
    assert request.groups[0].files[0].reviews == state.groups[0].reviewed_files[0].reviews
    assert request.groups[0].selected_release.release_id == "123"
    assert request.backup.enabled
    assert request.backup.root == Path("backups")
    assert request.report.enabled and request.report.directory == ""
    assert request.rename_template == settings.rename.template


@pytest.mark.parametrize("ids", [(), ("unknown",), ("same", "same")])
def test_prepare_apply_rejects_invalid_selection(ids):
    with pytest.raises(ValueError):
        prepare_apply_request(make_selected_session(), ids, AppSettings(), "APPLY-0001")


def test_settings_refresh_rebuilds_filenames_without_replacing_provider_choices():
    state = make_selected_session()
    source = state.groups[0].group.files[0]
    state = set_rename_decision(state, "album", (source.file_id,), RenameDecision.APPLY_RENAME, RenameSettings())
    original = state.groups[0]
    changed = refresh_rename_previews(state, RenameSettings(template="%title%"))
    reviewed = changed.groups[0].reviewed_files[0]
    assert reviewed.change_set.rename_preview.new_path.name == "序曲.flac"
    assert reviewed.change_set.rename_decision is RenameDecision.APPLY_RENAME
    assert reviewed.reviews == original.reviewed_files[0].reviews
    assert reviewed.proposals == original.reviewed_files[0].proposals
    assert changed.groups[0].selected_release is original.selected_release

    disabled = refresh_rename_previews(changed, RenameSettings(enabled=False))
    assert disabled.groups[0].reviewed_files[0].change_set.rename_change is None
