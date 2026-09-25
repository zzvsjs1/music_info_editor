"""Session decisions survive cancelled replacement and exit operations."""

from dataclasses import replace
from pathlib import Path

import pytest

from metadata_polisher.application.changes import RenameDecision, build_change_set
from metadata_polisher.domain.metadata import MetadataField
from metadata_polisher.domain.review import FieldDecisionKind
from metadata_polisher.infrastructure.settings import RenameSettings
from metadata_polisher.scanner.grouping import GroupingReason
from metadata_polisher.session.review_editing import apply_field_decision, set_rename_decision, undo_last_review_action
from metadata_polisher.session.state import GroupSelection, ReviewedFileState, SessionState
from metadata_polisher.ui.pending_work import pending_work_summary
from tests.ui.test_models import make_group_state, make_reviews, make_source
from tests.unit.session.test_lookup_editing import make_selected_session


def local_session(root: Path = Path("library")) -> SessionState:
    source = make_source(path=str(root / "Album" / "01.flac"))
    reviews = make_reviews(source)
    reviewed = ReviewedFileState(
        source.file_id, reviews=reviews,
        change_set=build_change_set(source, reviews, RenameDecision.KEEP_FILENAME),
    )

    return SessionState(
        root=root, groups=(make_group_state("album", source, reviewed),), selection=GroupSelection("album"),
    )


def edit_title(state: SessionState, decision=FieldDecisionKind.USE_MANUAL) -> SessionState:
    return apply_field_decision(
        state, "album", "file-1", MetadataField.TITLE, decision, RenameSettings(),
        manual_value="My correction" if decision is FieldDecisionKind.USE_MANUAL else None,
    )


def test_default_reviews_and_filename_previews_are_not_pending_user_work():
    state = local_session()
    summary = pending_work_summary(state)

    assert state.groups[0].reviewed_files
    assert not summary.has_pending_work
    assert summary.description() == ""


@pytest.mark.parametrize("decision", [
    FieldDecisionKind.KEEP_EXISTING, FieldDecisionKind.USE_MANUAL, FieldDecisionKind.CLEAR,
])
# A resolved Keep existing decision has value even when there is no tag delta.
# Discard protection therefore follows explicit intent, not just write counts.
def test_explicit_field_choices_are_counted_even_when_the_value_is_unchanged(decision):
    state = edit_title(local_session(), decision)
    summary = pending_work_summary(state)

    assert summary.has_pending_work
    assert summary.field_decision_count == 1
    assert summary.reviewed_file_count == 1
    assert summary.rename_file_count == 0
    assert summary.undo_action_count == 1
    assert "1 metadata decision in 1 file" in summary.description()


def test_rename_intent_is_protected_and_undo_can_return_to_an_unmodified_session():
    state = set_rename_decision(
        local_session(), "album", ("file-1",), RenameDecision.APPLY_RENAME, RenameSettings(),
    )
    summary = pending_work_summary(state)

    assert summary.field_decision_count == 0
    assert summary.rename_file_count == 1
    assert summary.undo_action_count == 1
    assert not pending_work_summary(undo_last_review_action(state, RenameSettings())).has_pending_work


@pytest.mark.parametrize("choice", ["split", "merge", "language", "disc"])
def test_session_only_grouping_and_lookup_hints_are_protected(choice):
    state = local_session()
    group = state.groups[0]

    if choice == "split":
        group = replace(group, group=replace(group.group, reason=GroupingReason.MANUAL_SPLIT))
    elif choice == "merge":
        group = replace(group, group=replace(group.group, reason=GroupingReason.MANUAL_MERGE))
    elif choice == "language":
        group = replace(group, language_override="en")
    else:
        group = replace(group, disc_number_override=2)

    summary = pending_work_summary(replace(state, groups=(group,)))

    assert summary.has_pending_work
    assert summary.edited_group_count == 1
    assert summary.field_decision_count == 0


def test_selected_release_is_a_choice_but_default_provider_fields_are_not_user_edits():
    summary = pending_work_summary(make_selected_session())

    assert summary.edited_group_count == 1
    assert summary.field_decision_count == 0
    assert summary.undo_action_count == 0


def test_write_inclusion_counts_only_files_still_in_the_library():
    summary = pending_work_summary(local_session(), frozenset({"file-1", "missing-file"}))

    assert summary.has_pending_work
    assert summary.included_file_count == 1
    assert summary.field_decision_count == 0
