"""Rebuild immutable review state after an explicit local-to-provider mapping."""

from dataclasses import replace

from metadata_polisher.application.changes import RenameDecision
from metadata_polisher.application.review import build_selected_file_results
from metadata_polisher.domain.metadata import MetadataField
from metadata_polisher.infrastructure.settings import RenameSettings
from metadata_polisher.matching.release_scoring import MatchClassification
from metadata_polisher.matching.track_mapping import TrackMappingResult
from metadata_polisher.session.review_editing import rebuild_reviewed_files, retain_user_decision
from metadata_polisher.session.state import ReviewedFileState, SessionState


def apply_manual_track_mapping(
    state: SessionState,
    group_id: str,
    mapping: TrackMappingResult,
    rename_settings: RenameSettings,
    *,
    preferred_language: str = "auto",
) -> SessionState:
    """Replace the effective mapping and derive proposals, reviews and filenames."""
    if state.active_operation is not None:
        raise ValueError("Wait for the current operation before mapping tracks.")

    group = next((item for item in state.groups if item.group.group_id == group_id), None)

    if group is None or group.requires_rescan or group.selected_release is None:
        raise ValueError("Select a release for a current scanned group before mapping tracks.")

    selection = group.selected_release
    classification = next(
        (entry.result.classification for entry in group.release_ranking.entries
         if entry.identity == selection.identity),
        MatchClassification.REVIEW,
    ) if group.release_ranking is not None else MatchClassification.REVIEW
    # Rebuild proposals from the selected provider track first. Reusing old
    # proposals after remapping would attach another track's metadata to this file.
    rebuilt = build_selected_file_results(
        group.group.files, selection.candidate,
        medium_index=selection.medium_index,
        mapping_result=mapping,
        release_classification=classification,
        preferred_language=group.language_override or preferred_language,
    )
    previous = {item.file_id: {review.field: review for review in item.reviews} for item in group.reviewed_files}
    old_mapping = group.effective_track_mapping
    previous_tracks = {
        item.local_file_id: item.provider_track_index for item in old_mapping.mappings
    } if old_mapping else {}
    current_tracks = {item.local_file_id: item.provider_track_index for item in mapping.mappings}
    # Release-level choices survive a track reassignment. Track-level candidate
    # choices survive only when that file still points to the same provider index.
    track_fields = {MetadataField.TITLE, MetadataField.ARTISTS, MetadataField.COMPOSERS, MetadataField.TRACK}
    reviews = tuple(
        ReviewedFileState(
            result.file_id, result.proposals,
            tuple(
                retain_user_decision(
                    previous[result.file_id][fresh.field], fresh,
                    candidate_dependency_unchanged=(
                        fresh.field not in track_fields
                        or previous_tracks.get(result.file_id) == current_tracks.get(result.file_id)
                    ),
                )
                if fresh.field in previous.get(result.file_id, {}) else fresh
                for fresh in result.reviews
            ),
            result.track_mapping_resolved,
        )
        for result in rebuilt
    )
    rename_decisions = {
        item.file_id: item.change_set.rename_decision if item.change_set is not None else RenameDecision.KEEP_FILENAME
        for item in group.reviewed_files
    }
    final_reviews = rebuild_reviewed_files(state, group, reviews, rename_settings, rename_decisions=rename_decisions)
    # Retain the automatic mapping as evidence and install the human mapping as
    # the effective override; the UI can still explain how the original match arose.
    replacement = replace(
        group, manual_track_mapping=mapping, reviewed_files=final_reviews,
        selected_metadata=None, revision=group.revision + 1,
    )

    return replace(
        state, groups=tuple(replacement if item is group else item for item in state.groups),
        revision=state.revision + 1,
    )
