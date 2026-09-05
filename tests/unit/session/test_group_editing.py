# Membership edits invalidate matching evidence, while unaffected groups and
# existing rescan requirements retain their meaning across split/merge operations.

from dataclasses import replace
from pathlib import Path

import pytest

from metadata_polisher.session.group_editing import merge_session_groups, set_disc_override, split_session_group
from metadata_polisher.session.state import GroupSelection, OperationKind, SessionState, begin_operation
from tests.unit.session.test_state import make_derived_group_state, make_file


def test_merge_disc_and_split_invalidate_derived_evidence_but_preserve_rescan():
    first = make_derived_group_state("first")
    second_file = replace(make_file(), path=Path("library/Album/other.flac"), file_id="other")
    second = make_derived_group_state("second", source=second_file)
    state = SessionState(root=Path("library"), groups=(first, second), selection=GroupSelection("first"))

    changed_disc = set_disc_override(state, "first", 2)
    assert changed_disc.groups[0].selected_release is None
    assert changed_disc.groups[0].automatic_track_mapping is None
    assert changed_disc.groups[0].reviewed_files == ()
    assert changed_disc.groups[1] is second
    assert first.reviewed_files

    merged = merge_session_groups(state, ("first", "second"))
    assert merged.groups[0].selected_release is None
    assert merged.groups[0].reviewed_files == ()
    assert merged.library_revision == 1

    needs_rescan = replace(merged, groups=(replace(merged.groups[0], requires_rescan=True),))
    split = split_session_group(needs_rescan, "first", (second_file.file_id,))
    assert all(group.requires_rescan for group in split.groups)
    assert split.provider_cache is state.provider_cache


def test_group_edits_reject_active_operations():
    group = make_derived_group_state()
    state = begin_operation(SessionState(root=Path("library"), groups=(group,)), "SCAN-1", OperationKind.SCAN, ())

    with pytest.raises(ValueError, match="current operation"):
        set_disc_override(state, group.group.group_id, 2)

    with pytest.raises(ValueError, match="current operation"):
        split_session_group(state, group.group.group_id, (group.group.files[0].file_id,))

    with pytest.raises(ValueError, match="current operation"):
        merge_session_groups(state, (group.group.group_id,))
