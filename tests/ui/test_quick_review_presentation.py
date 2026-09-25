"""Review copy describes typed state without changing decisions or write consent."""

from dataclasses import replace
from pathlib import Path

import pytest
from PySide6.QtCore import QObject, Qt

from metadata_polisher.application.changes import ChangeValidationFacts, RenameDecision, build_change_set
from metadata_polisher.domain.errors import Issue, MediaErrorCode
from metadata_polisher.domain.metadata import FieldReadState, MetadataField
from metadata_polisher.session.state import GroupSelection, SessionState, mark_groups_requires_rescan
from metadata_polisher.ui.models import MetadataDiffModel
from metadata_polisher.ui.quick.backend import QuickBackend
from metadata_polisher.ui.quick.models import QuickTableModel
from tests.ui.helpers import ControlledExecutor, make_group
from tests.unit.session.test_batch_review import make_batch_session


@pytest.fixture
def presentation_backend(qapp):
    first = make_group("album", "first", "Empty")
    second = make_group("album", "second", "Other title").group.files[0]
    group = replace(first, group=replace(first.group, files=(*first.group.files, second)))
    state = SessionState(root=Path("library"), groups=(group,), selection=GroupSelection("album"))
    backend = QuickBackend(state=state, executor=ControlledExecutor())
    yield backend
    backend.shutdown()


@pytest.mark.parametrize(("model_name", "column"), (("files", 2), ("albumModel", 0)))
def test_library_tooltips_retain_the_full_displayed_value(presentation_backend, model_name, column):
    model = getattr(presentation_backend, model_name)
    roles = {bytes(name): role for role, name in model.roleNames().items()}
    index = model.index(0, column)

    assert str(index.data()) in str(index.data(roles[b"tooltip"]))


def test_placeholders_distinguish_absence_from_literal_metadata(presentation_backend):
    backend = presentation_backend
    backend.selectFile("first", False)
    roles = {bytes(name): role for role, name in backend.review.roleNames().items()}
    placeholder = roles[b"placeholder"]
    artists = list(MetadataField).index(MetadataField.ARTISTS)

    # A genuine title named Empty remains normal metadata, even though its text
    # happens to match the placeholder used for a missing artists field.
    assert backend.review.index(0, 2).data() == "Empty"
    assert backend.review.index(0, 2).data(placeholder) is False
    assert backend.review.index(artists, 2).data() == "Empty"
    assert backend.review.index(artists, 2).data(placeholder) is True
    assert backend.review.index(artists, 3).data() == "No suggestion"
    assert backend.review.index(artists, 3).data(placeholder) is True
    assert backend.review.index(artists, 4).data() == "Empty"
    assert backend.review.index(artists, 4).data(placeholder) is True
    assert backend.review.index(0, 1).data() == "Needs review"
    assert backend.review.index(0, 2).data(roles[b"tooltip"])


def test_aggregate_absence_has_the_same_labels_and_typed_roles(presentation_backend):
    backend = presentation_backend
    backend.selectAllFiles()
    roles = {bytes(name): role for role, name in backend.review.roleNames().items()}
    artists = list(MetadataField).index(MetadataField.ARTISTS)

    assert backend.review.index(0, 2).data() == "Mixed values"
    assert backend.review.index(0, 2).data(roles[b"placeholder"]) is False
    assert backend.review.index(artists, 2).data() == "Empty"
    assert backend.review.index(artists, 3).data() == "No suggestion"
    assert backend.review.index(artists, 4).data() == "Empty"
    assert backend.review.index(artists, 3).data(roles[b"placeholder"]) is True


@pytest.mark.parametrize("read_state", (FieldReadState.UNREADABLE, FieldReadState.UNSUPPORTED))
def test_unavailable_values_never_look_like_empty_metadata(qapp, read_state):
    source = make_group("album", "first", "A latent title").group.files[0]
    states = dict(source.read_result.field_states)
    states[MetadataField.TITLE] = read_state
    source = replace(source, read_result=replace(source.read_result, field_states=states))
    # Match the application's ownership: source and proxy are siblings under
    # an independent owner. Parenting this PySide sorting proxy to its source
    # can invoke native mapping callbacks during source destruction and crash
    # a later cyclic collection, even when neither model has sorted its rows.
    owner = QObject()
    model = MetadataDiffModel(source, parent=owner)
    proxy = QuickTableModel(model, owner)
    roles = {bytes(name): role for role, name in proxy.roleNames().items()}

    assert model.index(0, 2).data() == read_state.value.capitalize()
    assert model.index(0, 4).data() == read_state.value.capitalize()
    assert proxy.index(0, 2).data(roles[b"placeholder"]) is False


def test_status_names_follow_explicit_review_decisions_and_undo(presentation_backend):
    backend = presentation_backend
    backend.selectFile("first", False)
    backend.selectField("title")
    backend.reviewAction("keep_existing")
    assert backend.review.index(0, 1).data() == "Kept"

    assert backend.beginEdit()
    assert backend.commitEdit("Edited title")
    assert backend.review.index(0, 1).data() == "Edited"
    backend.reviewAction("clear")
    assert backend.review.index(0, 1).data() == "Cleared"
    assert backend.review.index(0, 4).data() == "Empty"
    backend.undo()
    assert backend.review.index(0, 1).data() == "Edited"
    assert backend.review.index(0, 4).data() == "Edited title"
    assert backend.includedFileIds == []


def test_accepted_proposal_has_concise_status_and_scope_count(presentation_backend):
    backend = presentation_backend
    backend.set_state(make_batch_session())
    source = backend.session_state.groups[0].group.files[0]
    backend.selectFile(source.file_id, False)
    backend.selectField("title")

    assert backend.reviewScopeLabel == "1 file · 3 suggestions"
    assert backend.review.index(0, 1).data() == "Needs review"
    backend.reviewAction("use_candidate")
    assert backend.review.index(0, 1).data() == "Accepted"
    assert backend.review.index(0, 4).data() == "Candidate 1"


def test_progress_scope_and_scan_labels_are_read_only(presentation_backend):
    backend = presentation_backend
    original = backend.session_state
    backend.selectFile("second", False)

    assert backend.fileProgress == "2 of 2"
    assert backend.reviewScopeLabel == "1 file · No suggestions"
    assert backend.filenameSummary == "No rename suggested"
    assert backend.hasFilenameSuggestion is False
    assert backend.proposedFilename == "No suggestion"
    assert backend.scanSummary == "Scanned 2 files in 1 album"
    assert backend.scanHasWarnings is False
    assert backend.scanDetails == ""
    assert backend.session_state is original
    assert backend.includedFileIds == []


def test_batch_progress_does_not_describe_a_single_focused_file(presentation_backend):
    backend = presentation_backend
    backend.selectAllFiles()
    assert backend.fileProgress == "2 files"
    backend.clearSelection()
    assert backend.fileProgress == "No files selected"


def test_review_feedback_omits_duplicate_scan_success_but_keeps_warnings(presentation_backend):
    backend = presentation_backend
    backend.set_status("Scanned 2 supported files in 1 albums.")
    assert backend.reviewMessage == ""
    backend.set_status("Scanned 2 supported files in 1 albums.\nSettings could not be saved.")
    assert backend.reviewMessage == "Settings could not be saved."
    backend.set_status("A review change was undone.")
    assert backend.reviewMessage == "A review change was undone."


def test_scan_warning_summary_keeps_full_details(presentation_backend):
    backend = presentation_backend
    issue = Issue(MediaErrorCode.TAG_READ_FAILED, "Cannot read one tag", "Parser context")
    backend.set_state(replace(backend.session_state, scan_issues=(issue,)))

    assert backend.scanHasWarnings is True
    assert backend.scanSummary == "Scanned 2 files in 1 album · 1 warning"
    assert "Cannot read one tag" in backend.scanDetails
    assert "Parser context" in backend.scanDetails


def test_apply_summary_never_claims_unreviewed_or_stale_inclusion_is_ready(presentation_backend):
    backend = presentation_backend
    backend.selectFile("first", False)
    assert backend.changesToApplySummary == "No changes selected"
    assert backend.hasChangesToApply is False
    backend.setIncluded("first", True)
    assert backend.changesToApplySummary == "1 selected file needs review"

    backend.selectField("title")
    assert backend.beginEdit()
    assert backend.commitEdit("Ready title")
    assert backend.changesToApplySummary == "1 file ready to apply"
    assert backend.hasChangesToApply is True
    assert backend.applyUi.canApply
    backend.set_state(mark_groups_requires_rescan(backend.session_state, ("album",)))
    assert backend.changesToApplySummary == "1 selected file needs review"
    assert not backend.applyUi.canApply


def test_unchanged_reviewed_file_does_not_count_as_ready(presentation_backend):
    backend = presentation_backend
    backend.selectFile("first", False)
    backend.selectField("title")
    backend.reviewAction("keep_existing")
    backend.setIncluded("first", True)

    assert backend.changesToApplySummary == "No changes selected"
    assert backend.hasChangesToApply is False
    assert backend.review.index(0, 2).data(Qt.ItemDataRole.DisplayRole) == "Empty"


@pytest.mark.parametrize("blocked", (False, True))
def test_actual_preflight_blockers_control_readiness_without_hiding_review(presentation_backend, blocked):
    backend = presentation_backend
    state = make_batch_session()
    group = state.groups[0]
    source = group.group.files[0]
    reviewed = group.reviewed_files[0]
    changes = build_change_set(
        source, reviewed.reviews, RenameDecision.KEEP_FILENAME,
        validation=ChangeValidationFacts(directory_writable=not blocked),
    )
    reviewed = replace(reviewed, change_set=changes)
    group = replace(group, reviewed_files=(reviewed, *group.reviewed_files[1:]))
    backend.set_state(replace(state, groups=(group,)))
    backend.setIncluded(source.file_id, True)

    # Keeping an unaccepted title proposal does not block a safe genre addition.
    # A real preflight blocker does, but the final explanation remains reachable.
    assert backend.hasChangesToApply is True
    assert backend.applyUi.canApply
    assert backend.changesToApplySummary == (
        "1 selected file needs review" if blocked else "1 file ready to apply"
    )


def test_rename_summary_distinguishes_suggestion_from_consent(presentation_backend):
    backend = presentation_backend
    backend.selectFile("first", False)
    backend.selectField("title")
    assert backend.beginEdit()
    assert backend.commitEdit("New title")

    assert backend.hasFilenameSuggestion is True
    assert backend.filenameSummary == "Rename suggested"
    assert backend.proposedFilename != backend.currentFilename
    reviewed = backend.session_state.groups[0].reviewed_files[0]
    assert reviewed.change_set.rename_change is None
    assert backend.includedFileIds == []
