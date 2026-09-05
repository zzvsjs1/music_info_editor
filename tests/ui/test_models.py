from dataclasses import replace
from pathlib import Path

import pytest
from PySide6.QtCore import Qt

from metadata_polisher.application.changes import RenameDecision, build_change_set
from metadata_polisher.application.review import build_field_review_state, set_manual_decision
from metadata_polisher.domain.errors import Issue, MediaErrorCode
from metadata_polisher.domain.matching import MetadataProvenance
from metadata_polisher.domain.media import LocalMediaFile, MediaReadResult, StreamInfo
from metadata_polisher.domain.metadata import (
    FieldReadState,
    MetadataField,
    MetadataSnapshot,
    Position,
)
from metadata_polisher.domain.review import (
    FieldConfidence,
    FieldProposal,
    FieldReviewState,
    ReviewReasonCode,
)
from metadata_polisher.scanner.grouping import AlbumGroup, GroupingReason
from metadata_polisher.session.state import GroupState, ReviewedFileState, SessionState
from metadata_polisher.ui.models.diff_model import DIFF_HEADERS, MetadataDiffModel
from metadata_polisher.ui.models.file_table_model import FILE_TABLE_HEADERS, FileTableModel
from metadata_polisher.ui.models.group_model import GroupListModel


def make_source(
    file_id: str = "file-1",
    path: str = "library/Album/01.flac",
    *,
    title: str | None = "Opening",
) -> LocalMediaFile:
    metadata = MetadataSnapshot(
        title=title,
        artists=("Local Artist",),
        album="Local Album",
        album_artists=("Local Artist",),
        composers=(),
        track=Position(1, 10),
        disc=Position(1, 1),
        date="2024",
        genres=("Soundtrack",),
    )
    states = {
        # Read states are part of the fixture's evidence, not inferred from text.
        # Missing composers intentionally differ from present multi-value fields.
        MetadataField.TITLE: (
            FieldReadState.PRESENT if title is not None else FieldReadState.MISSING
        ),
        MetadataField.ARTISTS: FieldReadState.PRESENT,
        MetadataField.ALBUM: FieldReadState.PRESENT,
        MetadataField.ALBUM_ARTISTS: FieldReadState.PRESENT,
        MetadataField.COMPOSERS: FieldReadState.MISSING,
        MetadataField.TRACK: FieldReadState.PRESENT,
        MetadataField.DISC: FieldReadState.PRESENT,
        MetadataField.DATE: FieldReadState.PRESENT,
        MetadataField.GENRES: FieldReadState.PRESENT,
    }

    return LocalMediaFile(
        path=Path(path),
        format_id="flac",
        read_result=MediaReadResult(
            metadata=metadata,
            field_states=states,
            stream_info=StreamInfo(185.2, 48_000, 2, 24, "FLAC"),
        ),
        file_id=file_id,
    )


def existing_value(source: LocalMediaFile, field: MetadataField) -> object | None:
    metadata = source.read_result.metadata

    return {
        MetadataField.TITLE: metadata.title,
        MetadataField.ARTISTS: metadata.artists,
        MetadataField.ALBUM: metadata.album,
        MetadataField.ALBUM_ARTISTS: metadata.album_artists,
        MetadataField.COMPOSERS: None,
        MetadataField.TRACK: metadata.track,
        MetadataField.DISC: metadata.disc,
        MetadataField.DATE: metadata.date,
        MetadataField.GENRES: metadata.genres,
    }[field]


def make_reviews(
    source: LocalMediaFile,
    proposals: tuple[FieldProposal, ...] = (),
) -> tuple[FieldReviewState, ...]:
    return tuple(
        build_field_review_state(
            field=field,
            read_state=source.read_result.field_states[field],
            existing_value=existing_value(source, field),  # type: ignore[arg-type]
            proposals=tuple(proposal for proposal in proposals if proposal.field is field),
        )
        for field in MetadataField
    )


def make_group_state(
    group_id: str,
    source: LocalMediaFile,
    reviewed: ReviewedFileState | None = None,
) -> GroupState:
    return GroupState(
        group=AlbumGroup(
            group_id=group_id,
            files=(source,),
            album_title="Local Album",
            reason=GroupingReason.DIRECTORY_ALBUM_CONSISTENT,
        ),
        reviewed_files=() if reviewed is None else (reviewed,),
    )


def test_group_model_preserves_session_order_status_text_and_stable_ids(qapp) -> None:
    del qapp
    not_searched = make_group_state("group-b", make_source("file-b", "library/B/01.flac"))
    rescan_source = make_source("file-a", "library/A/01.flac")
    rescan = GroupState(
        group=make_group_state("group-a", rescan_source).group,
        requires_rescan=True,
    )
    state = SessionState(root=Path("library"), groups=(not_searched, rescan))
    model = GroupListModel(state)

    assert model.rowCount() == 2
    assert model.columnCount() == 2
    assert model.data(model.index(0, 0), Qt.ItemDataRole.UserRole) == "group-b"
    assert model.data(model.index(1, 0), Qt.ItemDataRole.UserRole) == "group-a"
    assert model.data(model.index(0, 1), Qt.ItemDataRole.DisplayRole) == "○ Not searched"
    assert model.data(model.index(1, 1), Qt.ItemDataRole.DisplayRole) == "✖ Rescan required"
    assert "group-b" in model.data(model.index(0, 0), Qt.ItemDataRole.ToolTipRole)


def test_file_table_has_exact_columns_symbols_values_and_read_only_identity(qapp) -> None:
    del qapp
    source = make_source()
    reviews = make_reviews(source)
    reviews = (set_manual_decision(reviews[0], "Reviewed title"), *reviews[1:])
    reviewed = ReviewedFileState(
        file_id=source.file_id,
        reviews=reviews,
        change_set=build_change_set(source, reviews, RenameDecision.KEEP_FILENAME),
    )
    group = make_group_state("group-1", source, reviewed)
    model = FileTableModel(group)

    headers = tuple(
        model.headerData(column, Qt.Orientation.Horizontal, Qt.ItemDataRole.DisplayRole)
        for column in range(model.columnCount())
    )

    assert headers == FILE_TABLE_HEADERS == (
        "Include",
        "Status",
        "File",
        "Track",
        "Disc",
        "Title",
        "Artist",
        "Composer",
        "Duration",
        "Format",
        "Match",
        "Rename",
    )
    assert model.rowCount() == 1
    assert model.data(model.index(0, 0), Qt.ItemDataRole.DisplayRole) == ""
    assert model.data(model.index(0, 0), Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Unchecked
    assert model.data(model.index(0, 1), Qt.ItemDataRole.DisplayRole) == "+ Ready"
    assert model.data(model.index(0, 2), Qt.ItemDataRole.DisplayRole) == "01.flac"
    assert model.data(model.index(0, 3), Qt.ItemDataRole.DisplayRole) == "1/10"
    assert model.data(model.index(0, 8), Qt.ItemDataRole.DisplayRole) == "3:05"
    assert model.data(model.index(0, 9), Qt.ItemDataRole.DisplayRole) == "FLAC"
    assert model.data(model.index(0, 0), Qt.ItemDataRole.UserRole) == source.file_id
    assert not model.flags(model.index(0, 5)) & Qt.ItemFlag.ItemIsEditable

    original_group = group
    original_change_set = reviewed.change_set

    assert not model.setData(model.index(0, 5), "Mutated", Qt.ItemDataRole.EditRole)
    assert group is original_group
    assert reviewed.change_set is original_change_set
    assert source.read_result.metadata.title == "Opening"


def test_diff_model_uses_field_order_five_columns_sources_reasons_and_no_mutation(qapp) -> None:
    del qapp
    source = make_source(title=None)
    proposal = FieldProposal(
        field=MetadataField.TITLE,
        value="Provider opening",
        confidence=FieldConfidence.HIGH,
        provenance=MetadataProvenance(
            engine_id="musicbrainz",
            source_id="musicbrainz",
            record_id="record-1",
            source_url=None,
            language="en",
            operation_id="LOOKUP-1",
        ),
        language="en",
        script="Latn",
        reason_codes=(ReviewReasonCode.PROPOSAL_CONFIDENT,),
    )
    reviews = make_reviews(source, (proposal,))
    reviewed = ReviewedFileState(
        file_id=source.file_id,
        proposals=(proposal,),
        reviews=reviews,
        change_set=build_change_set(source, reviews, RenameDecision.KEEP_FILENAME),
    )
    model = MetadataDiffModel(source, reviewed)

    assert model.columnCount() == 6
    assert model.rowCount() == len(MetadataField)
    assert tuple(
        model.headerData(column, Qt.Orientation.Horizontal, Qt.ItemDataRole.DisplayRole)
        for column in range(model.columnCount())
    ) == DIFF_HEADERS == (
        "Field",
        "Status",
        "Existing",
        "Proposed",
        "Final",
        "Sources",
    )
    assert tuple(
        model.headerData(row, Qt.Orientation.Vertical, Qt.ItemDataRole.DisplayRole)
        for row in range(model.rowCount())
    ) == (
        "Title",
        "Artists",
        "Album",
        "Album artists",
        "Composers",
        "Track",
        "Disc",
        "Date",
        "Genres",
    )
    assert model.data(model.index(0, 0), Qt.ItemDataRole.DisplayRole) == "Title"
    assert model.data(model.index(0, 2), Qt.ItemDataRole.DisplayRole) == "—"
    assert model.data(model.index(0, 3), Qt.ItemDataRole.DisplayRole) == "Provider opening"
    assert model.data(model.index(0, 4), Qt.ItemDataRole.DisplayRole) == "Provider opening"
    assert model.data(model.index(0, 5), Qt.ItemDataRole.DisplayRole) == "musicbrainz / musicbrainz"
    assert "Add" in model.data(model.index(0, 1), Qt.ItemDataRole.DisplayRole)
    assert "PROPOSAL_CONFIDENT" in model.data(
        model.index(0, 4),
        Qt.ItemDataRole.ToolTipRole,
    )
    assert model.data(model.index(0, 0), Qt.ItemDataRole.UserRole) is MetadataField.TITLE
    assert not model.flags(model.index(0, 2)) & Qt.ItemFlag.ItemIsEditable

    assert not model.setData(model.index(0, 2), "Mutated", Qt.ItemDataRole.EditRole)
    assert reviewed.proposals == (proposal,)
    assert proposal.value == "Provider opening"


def test_diff_model_shows_existing_values_for_an_unreviewed_scanned_file(qapp) -> None:
    del qapp
    source = make_source()
    model = MetadataDiffModel(source)

    assert model.rowCount() == len(MetadataField)
    assert model.data(model.index(0, 2), Qt.ItemDataRole.DisplayRole) == "Opening"
    assert model.data(model.index(0, 3), Qt.ItemDataRole.DisplayRole) == "—"
    assert model.data(model.index(0, 4), Qt.ItemDataRole.DisplayRole) == "Opening"
    assert model.data(model.index(0, 1), Qt.ItemDataRole.DisplayRole) == "○ Not reviewed"
    assert model.data(model.index(4, 2), Qt.ItemDataRole.DisplayRole) == "—"
    assert model.data(model.index(4, 4), Qt.ItemDataRole.DisplayRole) == "—"


def test_final_position_shows_the_retained_component_from_the_actual_change_set(qapp) -> None:
    del qapp
    source = make_source()
    reviews = tuple(set_manual_decision(review, Position(None, 99)) if review.field is MetadataField.TRACK
                    else review for review in make_reviews(source))
    reviewed = ReviewedFileState(
        source.file_id, reviews=reviews,
        change_set=build_change_set(source, reviews, RenameDecision.KEEP_FILENAME),
    )
    model = MetadataDiffModel(source, reviewed)
    row = tuple(MetadataField).index(MetadataField.TRACK)

    assert reviewed.change_set.final_metadata.track == Position(1, 99)
    assert model.index(row, 4).data() == "1/99"


def test_models_reset_from_complete_immutable_replacements(qapp, qtbot) -> None:
    del qapp
    first_source = make_source("file-a", "library/A/01.flac")
    second_source = make_source("file-b", "library/B/02.flac", title="Second")
    first_group = make_group_state("group-a", first_source)
    second_group = make_group_state("group-b", second_source)
    group_model = GroupListModel(SessionState(root=Path("library"), groups=(first_group,)))
    file_model = FileTableModel(first_group)
    diff_model = MetadataDiffModel(first_source)

    with qtbot.waitSignal(group_model.modelReset):
        group_model.set_session_state(
            SessionState(root=Path("library"), groups=(second_group, first_group))
        )

    with qtbot.waitSignal(file_model.modelReset):
        file_model.set_group_state(second_group)

    with qtbot.waitSignal(diff_model.modelReset):
        diff_model.set_file(second_source, None)

    assert group_model.rowCount() == 2
    assert group_model.data(group_model.index(0, 0), Qt.ItemDataRole.UserRole) == "group-b"
    assert file_model.data(file_model.index(0, 0), Qt.ItemDataRole.UserRole) == "file-b"
    assert diff_model.data(diff_model.index(0, 2), Qt.ItemDataRole.DisplayRole) == "Second"

    with qtbot.waitSignal(file_model.modelReset):
        file_model.set_group_state(None)

    with qtbot.waitSignal(diff_model.modelReset):
        diff_model.set_file(None, None)

    assert file_model.rowCount() == 0
    assert diff_model.rowCount() == 0


# An index with plausible coordinates can still belong to another model; both
# identity and bounds must be checked before exposing this model's row contents.
def test_models_reject_foreign_and_out_of_bounds_indexes(qapp) -> None:
    del qapp
    source = make_source()
    group = make_group_state("group-1", source)
    group_model = GroupListModel(SessionState(root=Path("library"), groups=(group,)))
    file_model = FileTableModel(group)
    diff_model = MetadataDiffModel(source)
    models = (group_model, file_model, diff_model)

    for model in models:
        foreign_index = next(other.index(0, 0) for other in models if other is not model)
        too_wide = model.createIndex(0, model.columnCount())
        too_low = model.createIndex(model.rowCount(), 0)

        assert model.data(foreign_index, Qt.ItemDataRole.DisplayRole) is None
        assert model.flags(foreign_index) == Qt.ItemFlag.NoItemFlags
        assert model.data(too_wide, Qt.ItemDataRole.DisplayRole) is None
        assert model.flags(too_wide) == Qt.ItemFlag.NoItemFlags
        assert model.data(too_low, Qt.ItemDataRole.DisplayRole) is None
        assert model.flags(too_low) == Qt.ItemFlag.NoItemFlags


def test_diff_constructor_rejects_review_state_for_another_file(qapp) -> None:
    del qapp
    source = make_source("file-a", "library/A/01.flac")
    other = make_source("file-b", "library/B/01.flac")
    reviews = make_reviews(other)
    reviewed = ReviewedFileState(
        file_id=other.file_id,
        reviews=reviews,
        change_set=build_change_set(other, reviews, RenameDecision.KEEP_FILENAME),
    )

    with pytest.raises(ValueError, match="must belong"):
        MetadataDiffModel(source, reviewed)


@pytest.mark.parametrize(
    ("read_state", "expected_status"),
    (
        (FieldReadState.MISSING, "○ Not reviewed"),
        (FieldReadState.UNREADABLE, "✖ Unreadable"),
        (FieldReadState.UNSUPPORTED, "! Unsupported"),
    ),
)
def test_unreviewed_diff_does_not_expose_non_present_latent_values(
    qapp,
    read_state: FieldReadState,
    expected_status: str,
) -> None:
    del qapp
    source = make_source()
    states = dict(source.read_result.field_states)
    states[MetadataField.TITLE] = read_state
    source = replace(
        source,
        read_result=replace(source.read_result, field_states=states),
    )
    model = MetadataDiffModel(source)

    assert model.data(model.index(0, 2), Qt.ItemDataRole.DisplayRole) == "—"
    assert model.data(model.index(0, 4), Qt.ItemDataRole.DisplayRole) == "—"
    assert model.data(model.index(0, 1), Qt.ItemDataRole.DisplayRole) == expected_status
    assert read_state.value in model.data(model.index(0, 0), Qt.ItemDataRole.ToolTipRole)


def test_unresolved_mapping_and_media_read_failure_are_visible_in_statuses(qapp) -> None:
    del qapp
    source = make_source()
    reviews = make_reviews(source)
    unresolved = ReviewedFileState(
        file_id=source.file_id,
        reviews=reviews,
        track_mapping_resolved=False,
        change_set=build_change_set(source, reviews, RenameDecision.KEEP_FILENAME),
    )
    unresolved_group = make_group_state("group-unresolved", source, unresolved)
    unresolved_state = SessionState(root=Path("library"), groups=(unresolved_group,))
    unresolved_file_model = FileTableModel(unresolved_group)
    unresolved_group_model = GroupListModel(unresolved_state)

    assert unresolved_file_model.data(
        unresolved_file_model.index(0, 1),
        Qt.ItemDataRole.DisplayRole,
    ) == "! Needs review"
    assert unresolved_group_model.data(
        unresolved_group_model.index(0, 1),
        Qt.ItemDataRole.DisplayRole,
    ) == "! Needs review"

    states = dict(source.read_result.field_states)
    states[MetadataField.TITLE] = FieldReadState.UNREADABLE
    read_failure = replace(
        source,
        read_result=replace(
            source.read_result,
            field_states=states,
            issues=(Issue(MediaErrorCode.TAG_READ_FAILED, "Title tag could not be read."),),
        ),
    )
    failed_group = make_group_state("group-error", read_failure)
    failed_state = SessionState(root=Path("library"), groups=(failed_group,))
    file_model = FileTableModel(failed_group)
    group_model = GroupListModel(failed_state)

    assert file_model.data(file_model.index(0, 1), Qt.ItemDataRole.DisplayRole) == "✖ Read error"
    assert group_model.data(group_model.index(0, 1), Qt.ItemDataRole.DisplayRole) == "✖ Read error"
    assert "TAG_READ_FAILED" in file_model.data(
        file_model.index(0, 1),
        Qt.ItemDataRole.ToolTipRole,
    )
