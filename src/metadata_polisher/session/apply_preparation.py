"""Capture reviewed session decisions as a state-independent Apply request."""

from collections.abc import Sequence
from pathlib import Path

from metadata_polisher.application.apply import ApplyBatchRequest, ApplyFileRequest, ApplyGroupRequest
from metadata_polisher.application.changes import RenameDecision
from metadata_polisher.infrastructure.reporting import ReportOutputPolicy, SelectedReleaseReference
from metadata_polisher.infrastructure.settings import AppSettings
from metadata_polisher.infrastructure.transaction import BackupPolicy
from metadata_polisher.matching.release_scoring import MatchReasonCode
from metadata_polisher.rename.template import FilenameRenderPolicy
from metadata_polisher.session.state import SessionState


def reviewed_file_ids(state: SessionState) -> tuple[str, ...]:
    """List current reviewed files in visible group/file order, including blockers."""
    return tuple(
        source.file_id for group in state.groups if not group.requires_rescan
        for source in group.group.files
        if any(review.file_id == source.file_id and review.change_set is not None for review in group.reviewed_files)
    )


def prepare_apply_request(
    state: SessionState,
    file_ids: Sequence[str],
    settings: AppSettings,
    operation_id: str,
) -> ApplyBatchRequest:
    """Freeze the chosen reviews; actual filesystem facts are rechecked by the worker."""
    if state.active_operation is not None or state.root is None:
        raise ValueError("Apply requires an idle, scanned library.")

    if isinstance(file_ids, (str, bytes)) or not file_ids or len(file_ids) != len(set(file_ids)):
        raise ValueError("Select a non-empty set of unique reviewed files.")

    selected_ids = set(file_ids)

    if not selected_ids.issubset(reviewed_file_ids(state)):
        raise ValueError("Every selected file needs current reviewed metadata; rescan changed files first.")

    # Membership comes from captured IDs, but execution order comes from the
    # session. Clicking rows in a different order must not reorder transactions.
    groups = []

    for group in state.groups:
        reviews_by_id = {review.file_id: review for review in group.reviewed_files}
        files = []
        mapping = group.effective_track_mapping

        for source in group.group.files:
            if source.file_id not in selected_ids:
                continue

            reviewed = reviews_by_id[source.file_id]
            assert reviewed.change_set is not None
            # Include group-wide reasons plus this file's pair evidence; another
            # track's score must not appear as justification for the current file.
            evidence = (() if mapping is None else mapping.evidence) + tuple(
                item for pair in (() if mapping is None else mapping.mappings)
                if pair.local_file_id == source.file_id for item in pair.evidence
            )
            reasons = tuple(dict.fromkeys(
                MatchReasonCode(item.code) for item in evidence if item.code in MatchReasonCode
            ))
            files.append(ApplyFileRequest(
                source, reviewed.reviews,
                reviewed.change_set.rename_decision if settings.rename.enabled else RenameDecision.KEEP_FILENAME,
                reviewed.track_mapping_resolved, reasons,
            ))

        if files:
            release = SelectedReleaseReference(*group.selected_release.identity) if group.selected_release else None
            groups.append(ApplyGroupRequest(group.group.group_id, group.revision, release, tuple(files)))

    # Capture revisions and settings with the decisions so a later widget change
    # cannot alter the meaning of the worker's already-confirmed batch.
    return ApplyBatchRequest(
        operation_id, state.revision, state.library_revision, state.root, tuple(groups),
        BackupPolicy(settings.backup.enabled, Path(settings.backup.directory) if settings.backup.directory else None,
                     state.root, operation_id),
        ReportOutputPolicy(settings.reports.enabled, settings.reports.directory),
        settings.rename.template,
        FilenameRenderPolicy(settings.rename.minimum_track_digits, settings.rename.minimum_disc_digits),
    )
