from pathlib import Path

from PySide6.QtCore import QObject, Qt
from PySide6.QtWidgets import (
    QDialog,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTableView,
    QTableWidget,
    QTreeView,
)

from metadata_polisher.bootstrap import create_application
from metadata_polisher.domain.media import LocalMediaFile, MediaReadResult, StreamInfo
from metadata_polisher.domain.metadata import (
    FieldReadState,
    MetadataField,
    MetadataSnapshot,
    Position,
)
from metadata_polisher.scanner.grouping import AlbumGroup, GroupingReason
from metadata_polisher.session.state import GroupSelection, GroupState, SessionState
from metadata_polisher.ui.main_window import MainWindow
from metadata_polisher.ui.models import FileTableModel, GroupListModel, MetadataDiffModel


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


def test_main_window_builds_named_model_views_with_a_separate_review_window(qapp, qtbot, tmp_path) -> None:
    application, window = create_application([], settings_file=tmp_path / "settings.json")
    qtbot.addWidget(window)

    assert application is qapp
    assert isinstance(window, MainWindow)
    assert window.windowTitle() == "Metadata Polisher"
    assert window.processing_executor is not None
    assert window.operation_bridge is not None
    assert window.operation_controller is not None

    expected_types = {
        "rootPathEdit": QLineEdit,
        "browseButton": QPushButton,
        "rescanButton": QPushButton,
        "findSelectedButton": QPushButton,
        "findAllIncompleteButton": QPushButton,
        "applySelectedButton": QPushButton,
        "applyAllButton": QPushButton,
        "settingsButton": QPushButton,
        "openReviewButton": QPushButton,
        "mainSplitter": QSplitter,
        "groupView": QTreeView,
        "fileTableView": QTableView,
        "diffTableView": QTableView,
        "renameCurrentLabel": QLabel,
        "renameProposedLabel": QLabel,
        "renameTemplateLabel": QLabel,
        "summaryCountsLabel": QLabel,
        "operationStageLabel": QLabel,
        "operationProgressBar": QProgressBar,
        "applySafetyLabel": QLabel,
    }

    for object_name, widget_type in expected_types.items():
        assert isinstance(window.findChild(widget_type, object_name), widget_type)

    splitter = window.findChild(QSplitter, "mainSplitter")
    group_view = window.findChild(QTreeView, "groupView")
    file_view = window.findChild(QTableView, "fileTableView")
    diff_view = window.findChild(QTableView, "diffTableView")

    assert splitter.orientation() is Qt.Orientation.Horizontal
    assert splitter.count() == 2
    assert all(splitter.isAncestorOf(view) for view in (group_view, file_view))
    assert not splitter.isAncestorOf(diff_view)
    assert isinstance(window.review_window, QDialog)
    assert window.review_window.isWindow()
    assert window.review_window.windowModality() is Qt.WindowModality.NonModal
    assert window.review_window.isAncestorOf(diff_view)
    assert isinstance(group_view.model(), GroupListModel)
    assert isinstance(file_view.model(), FileTableModel)
    assert isinstance(diff_view.model(), MetadataDiffModel)
    assert window.findChildren(QTableWidget) == []

    assert window.findChild(QLabel, "operationStageLabel").text() == "Idle"
    assert window.findChild(QProgressBar, "operationProgressBar").value() == 0
    assert "Only Apply changes" in window.findChild(QLabel, "applySafetyLabel").text()
    assert window.findChildren(QObject, "rootPathEdit") == [
        window.findChild(QLineEdit, "rootPathEdit")
    ]


def test_group_navigation_updates_session_selection_and_replacement_projection(
    qapp,
    qtbot,
) -> None:
    del qapp
    window = MainWindow()
    qtbot.addWidget(window)
    original = make_group("group-1", "old-file", "Original")
    window.set_session_state(SessionState(root=Path("library"), groups=(original,)))

    with qtbot.waitSignal(window.group_selection_requested) as selection_requested:
        window.group_view.setCurrentIndex(window.group_model.index(0, 0))

    assert selection_requested.args == ["group-1"]
    assert window.session_state.selection is None
    assert window.file_model.data(
        window.file_model.index(0, 0),
        Qt.ItemDataRole.UserRole,
    ) == "old-file"
    assert window.summary_counts_label.text() == "0 ready · 1 review · 0 unsupported"

    replacement = make_group("group-1", "new-file", "Replacement")
    window.set_session_state(
        SessionState(
            root=Path("library"),
            groups=(replacement,),
            selection=GroupSelection("group-1"),
        )
    )

    assert window.session_state.groups[0] is replacement
    assert window.file_model.data(
        window.file_model.index(0, 0),
        Qt.ItemDataRole.UserRole,
    ) == "new-file"
