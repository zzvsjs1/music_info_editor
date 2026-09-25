"""Library sorting changes presentation order without changing review identity."""

from dataclasses import replace
from pathlib import Path

import pytest

from metadata_polisher.domain.media import UnsupportedMediaFile
from metadata_polisher.domain.metadata import FieldReadState, MetadataField, Position
from metadata_polisher.session.state import GroupSelection, SessionState
from metadata_polisher.ui.quick.backend import QuickBackend
from metadata_polisher.ui.quick.models import QuickTableModel
from tests.ui.helpers import ControlledExecutor, make_group


@pytest.fixture
def sorted_library(qapp):
    # Names intentionally disagree with numeric order, and 2 versus 10 catches
    # lexicographic sorting of the strings shown in Track and Disc cells.
    base = make_group("album", "disc-ten", "Album")
    specifications = (
        ("disc-ten", "A.flac", 10, 1, 120.0),
        ("track-ten", "B.flac", 1, 10, 10.0),
        ("no-number-z", "Z.flac", None, None, None),
        ("disc-two", "C.flac", 2, 1, 20.0),
        ("track-two", "Y.flac", 1, 2, 2.0),
        ("implicit-disc", "X.flac", None, 3, 30.0),
        ("disc-only", "D.flac", 1, None, 40.0),
        ("no-number-e", "E.flac", None, None, None),
    )
    files = []

    for identity, filename, disc, track, duration in specifications:
        source = base.group.files[0]
        metadata = replace(source.read_result.metadata, disc=Position(disc), track=Position(track))
        states = dict(source.read_result.field_states)
        states[MetadataField.DISC] = FieldReadState.MISSING if disc is None else FieldReadState.PRESENT
        states[MetadataField.TRACK] = FieldReadState.MISSING if track is None else FieldReadState.PRESENT
        result = replace(
            source.read_result,
            metadata=metadata,
            field_states=states,
            stream_info=replace(source.read_result.stream_info, duration_seconds=duration),
        )
        files.append(replace(source, file_id=identity, path=Path("library") / filename, read_result=result))

    group = replace(base, group=replace(base.group, files=tuple(files)))
    state = SessionState(root=Path("library"), groups=(group,), selection=GroupSelection("album"))
    backend = QuickBackend(state=state, executor=ControlledExecutor())

    try:
        yield backend
    finally:
        backend.shutdown()


def visible_ids(model):
    return [model.index(row, 0).data(QuickTableModel.IDENTITY) for row in range(model.rowCount())]


def test_default_file_order_uses_disc_then_numeric_track_and_missing_fallback(sorted_library):
    original = sorted_library.session_state

    assert visible_ids(sorted_library.files) == [
        "track-two", "implicit-disc", "track-ten", "disc-only", "disc-two", "disc-ten",
        "no-number-e", "no-number-z",
    ]
    assert sorted_library.files.sortColumnIndex == -1
    assert sorted_library.files.sortLabel == "Disc → Track → File"
    assert sorted_library.session_state is original
    assert [source.file_id for source in original.groups[0].group.files] == [
        "disc-ten", "track-ten", "no-number-z", "disc-two", "track-two", "implicit-disc",
        "disc-only", "no-number-e",
    ]


@pytest.mark.parametrize(("column", "expected"), [
    (3, ["disc-ten", "disc-two", "track-two", "implicit-disc", "track-ten"]),
    (4, ["track-ten", "disc-only", "track-two", "disc-two", "disc-ten"]),
    (8, ["track-two", "track-ten", "disc-two", "implicit-disc", "disc-only", "disc-ten"]),
])
def test_numeric_columns_sort_typed_values_with_missing_values_after_known_values(sorted_library, column, expected):
    sorted_library.files.sortByColumn(column, False)

    assert visible_ids(sorted_library.files)[:len(expected)] == expected
    assert sorted_library.files.sortColumnIndex == column
    assert sorted_library.files.sortDescending is False


@pytest.mark.parametrize(("column", "expected"), [
    (3, ["track-ten", "implicit-disc", "track-two", "disc-two", "disc-ten"]),
    (4, ["disc-ten", "disc-two", "track-two", "disc-only", "track-ten"]),
    (8, ["disc-ten", "disc-only", "implicit-disc", "disc-two", "track-ten", "track-two"]),
])
def test_descending_numeric_sorts_keep_missing_values_last(sorted_library, column, expected):
    sorted_library.files.sortByColumn(column, True)

    assert visible_ids(sorted_library.files)[:len(expected)] == expected
    assert sorted_library.files.sortDescending is True


def test_sort_preserves_selection_inclusion_and_review_target_across_refresh(sorted_library):
    backend = sorted_library
    backend.selectFile("track-two", False)
    backend.setIncluded("disc-two", True)
    original = backend.session_state
    backend.files.sortByColumn(2, False)

    assert backend.selectedFileIds == ["track-two"]
    assert backend.currentFileRow == 6
    assert backend.fileProgress == "7 of 8"
    assert backend.includedFileIds == ["disc-two"]
    assert backend.files.index(2, 0).data(QuickTableModel.INCLUDED) is True
    assert backend.files.index(6, 0).data(QuickTableModel.HIGHLIGHTED) is True
    assert backend.session_state is original

    backend.set_state(original)

    assert visible_ids(backend.files) == [
        "disc-ten", "track-ten", "disc-two", "disc-only", "no-number-e", "implicit-disc",
        "track-two", "no-number-z",
    ]
    assert backend.currentFileRow == 6
    assert backend.selectedFileIds == ["track-two"]
    assert backend.includedFileIds == ["disc-two"]


def test_file_keyboard_ranges_and_review_navigation_follow_the_visible_order(sorted_library):
    backend = sorted_library
    backend.selectFile("track-two", False)
    backend.moveFileExtended(2, True)

    assert backend.selectedFileIds == ["track-two", "implicit-disc", "track-ten"]
    assert backend.currentFileRow == 2

    backend.files.sortByColumn(2, True)
    backend.selectFile("track-two", False)
    backend.moveFile(1)

    assert backend.selectedFileIds == ["implicit-disc"]
    assert backend.fileProgress == "3 of 8"

    backend.clearSelection()
    backend.openReview()

    assert backend.selectedFileIds == ["no-number-z"]
    assert backend.includedFileIds == []


def test_restore_default_sort_and_metadata_fixed_order(sorted_library):
    backend = sorted_library
    backend.files.sortByColumn(2, True)
    backend.files.restoreDefaultSort()

    assert visible_ids(backend.files)[:3] == ["track-two", "implicit-disc", "track-ten"]
    assert backend.files.sortColumnIndex == -1
    assert backend.files.sortDescending is False
    assert backend.files.sortingEnabled is True
    assert backend.review.sortingEnabled is False

    backend.selectFile("track-two", False)
    fields = visible_ids(backend.review)
    backend.review.sortByColumn(0, True)

    assert visible_ids(backend.review) == fields


def test_include_sort_does_not_move_the_clicked_row_until_sort_is_requested(sorted_library):
    backend = sorted_library
    backend.setIncluded("track-two", True)
    backend.files.sortByColumn(0, True)
    ids = visible_ids(backend.files)
    assert ids[0] == "track-two"

    backend.setIncluded("track-two", False)

    assert visible_ids(backend.files) == ids
    assert backend.includedFileIds == []


def test_album_sort_changes_keyboard_range_and_current_row_without_changing_source_groups(qapp):
    groups = tuple(make_group(key, f"file-{key}", key.title()) for key in ("zeta", "alpha", "middle"))
    state = SessionState(root=Path("library"), groups=groups, selection=GroupSelection("alpha"))
    backend = QuickBackend(state=state, executor=ControlledExecutor())

    try:
        backend.selectGroup("alpha")
        backend.albumModel.sortByColumn(0, False)

        assert visible_ids(backend.albumModel) == ["alpha", "middle", "zeta"]
        assert backend.currentGroupRow == 0
        assert backend.session_state.groups == groups

        backend.moveGroup(1, True)

        assert backend.groupId == "middle"
        assert backend.selectedGroupIds == ["alpha", "middle"]
        assert backend.currentGroupRow == 1
        assert visible_ids(backend.albumModel) == ["alpha", "middle", "zeta"]
        assert backend.includedFileIds == []
    finally:
        backend.shutdown()


def test_unsupported_collection_keeps_its_identity_and_visual_navigation_after_sort(qapp):
    groups = tuple(make_group(key, f"file-{key}", key.title()) for key in ("zeta", "alpha"))
    state = SessionState(
        root=Path("library"),
        groups=groups,
        selection=GroupSelection("alpha"),
        unsupported_files=(UnsupportedMediaFile(Path("library/extra.opus")),),
    )
    backend = QuickBackend(state=state, executor=ControlledExecutor())

    try:
        backend.albumModel.sortByColumn(0, False)
        assert visible_ids(backend.albumModel) == ["alpha", "@unsupported", "zeta"]

        backend.moveGroup(1, False)

        assert backend.currentGroupRow == 1
        assert backend.selectedGroupIds == []
        assert visible_ids(backend.files) == [str(Path("library/extra.opus"))]

        backend.moveGroup(1, False)

        assert backend.groupId == "zeta"
        assert backend.currentGroupRow == 2

        backend.selectAllGroups()

        assert backend.selectedGroupIds == ["alpha", "zeta"]
        assert backend.session_state.groups == groups
        assert backend.includedFileIds == []
    finally:
        backend.shutdown()
