"""Synthetic sessions and controllable workers shared by Qt Quick tests.

These helpers have no Widgets dependency. Keeping fixtures outside test modules
lets the QML and service suites survive retirement of the previous frontend.
"""

from concurrent.futures import Future
from dataclasses import replace
from pathlib import Path

from metadata_polisher.domain.media import LocalMediaFile, MediaReadResult, StreamInfo
from metadata_polisher.domain.metadata import FieldReadState, MetadataField, MetadataSnapshot, Position
from metadata_polisher.domain.review import FieldDecisionKind
from metadata_polisher.execution.cancellation import MutableCancellationToken
from metadata_polisher.infrastructure.settings import RenameSettings
from metadata_polisher.scanner.grouping import AlbumGroup, GroupingReason
from metadata_polisher.session.review_editing import apply_field_decision
from metadata_polisher.session.state import GroupState, mark_groups_requires_rescan
from tests.unit.session.test_batch_review import make_batch_session
from tests.unit.session.test_review_editing import make_local_session, make_source


class ControlledHandle:
    def __init__(self, operation_id):
        self.operation_id = operation_id
        self.future = Future()
        self.token = MutableCancellationToken()

    def cancel(self):
        self.token.cancel()

    def is_cancel_requested(self):
        return self.token.is_cancelled()

    def done(self):
        return self.future.done()

    def result(self, timeout=None):
        return self.future.result(timeout)

    def add_done_callback(self, callback):
        self.future.add_done_callback(lambda _: callback(self))


class ControlledExecutor:
    # Queue work until run_next so assertions can inspect the busy UI before
    # completion, while the real Qt bridge still controls signal delivery.
    def __init__(self):
        self.pending = []

    def submit(self, operation_id, work, events):
        handle = ControlledHandle(operation_id)
        self.pending.append((handle, work, events))
        return handle

    def run_next(self):
        handle, work, events = self.pending.pop(0)

        try:
            handle.future.set_result(work(handle.token, events))
        except Exception as error:
            handle.future.set_exception(error)

    def shutdown(self, wait=True):
        return


def make_group(group_id: str, file_id: str, title: str) -> GroupState:
    # Give groups distinct stable identities and visible values so navigation
    # can reveal a stale model even when the same row number is selected again.
    states = {field: FieldReadState.MISSING for field in MetadataField}
    states[MetadataField.TITLE] = FieldReadState.PRESENT
    states[MetadataField.TRACK] = FieldReadState.PRESENT
    source = LocalMediaFile(
        path=Path("library") / group_id / f"{file_id}.flac",
        format_id="flac",
        read_result=MediaReadResult(
            metadata=MetadataSnapshot(title=title, track=Position(number=1)),
            field_states=states,
            stream_info=StreamInfo(180.0, 48_000, 2, 24, "FLAC"),
        ),
        file_id=file_id,
    )

    return GroupState(
        group=AlbumGroup(
            group_id=group_id,
            files=(source,),
            album_title=title,
            reason=GroupingReason.DIRECTORY_ALBUM_CONSISTENT,
        )
    )


def changed_local_session():
    state = make_local_session(make_source("first.flac"), make_source("second.flac"))

    for index, source in enumerate(state.groups[0].group.files):
        state = apply_field_decision(state, "album", source.file_id, MetadataField.TITLE,
                                     FieldDecisionKind.USE_MANUAL, RenameSettings(), manual_value=f"Corrected {index}")

    return state


def blocked_first_session():
    """The first album is stale; later tracks retain distinct candidate titles."""
    state = make_batch_session()
    first = make_local_session(make_source("blocked.flac")).groups[0]
    first = replace(first, group=replace(first.group, group_id="blocked"))
    return mark_groups_requires_rescan(replace(state, groups=(first, *state.groups)), ("blocked",))
