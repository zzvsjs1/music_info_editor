from dataclasses import replace

from PySide6.QtCore import QItemSelectionModel, Qt, QTimer
from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox, QListWidget, QSpinBox

from metadata_polisher.bootstrap import create_application
from metadata_polisher.session.state import GroupSelection, SessionState
from tests.ui.test_main_window import make_group
from tests.ui.test_scan_workflow import ControlledExecutor


def group_window(qtbot, tmp_path):
    _, window = create_application([], executor=ControlledExecutor(), settings_file=tmp_path / "settings.json")
    qtbot.addWidget(window)
    first = make_group("one", "a", "Album")
    extra = replace(first.group.files[0], file_id="b", path=first.group.files[0].path.with_name("b.flac"))
    first = replace(first, group=replace(first.group, files=(*first.group.files, extra)))
    second = make_group("two", "c", "Elsewhere")
    state = SessionState(
        root=first.group.files[0].path.parents[1], groups=(first, second), selection=GroupSelection("one")
    )
    window.set_session_state(state)
    return window, state


# Session regrouping changes ownership, not the files themselves. Checking
# identities after both operations catches dropped or duplicated membership.
def test_split_and_merge_keep_unique_files_in_session(qtbot, tmp_path):
    window, original = group_window(qtbot, tmp_path)
    window.file_table_view.selectRow(0)
    assert window.split_group_button.isEnabled()
    qtbot.mouseClick(window.split_group_button, Qt.MouseButton.LeftButton)
    split = window.session_state
    assert len(split.groups) == 3
    assert [len(group.group.files) for group in split.groups] == [1, 1, 1]
    assert original.groups[0].group.files[0].file_id == "a"
    assert len(original.groups[0].group.files) == 2

    selection = window.group_view.selectionModel()
    selection.select(
        window.group_model.index(0, 0),
        QItemSelectionModel.SelectionFlag.ClearAndSelect | QItemSelectionModel.SelectionFlag.Rows,
    )
    selection.select(
        window.group_model.index(1, 0),
        QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows,
    )

    def confirm_merge():
        dialog = QApplication.activeModalWidget()
        assert isinstance(dialog, QDialog)
        assert dialog.findChild(QListWidget).count() == 2
        dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok).click()

    QTimer.singleShot(0, confirm_merge)
    qtbot.mouseClick(window.merge_groups_button, Qt.MouseButton.LeftButton)
    assert len(window.session_state.groups) == 2
    files = [file.file_id for group in window.session_state.groups for file in group.group.files]
    assert sorted(files) == ["a", "b", "c"]
    assert window.session_state.groups[-1] is original.groups[-1]
    assert window.session_state.revision == 2


def test_manual_disc_override_changes_evidence_without_changing_tags(qtbot, tmp_path):
    window, original = group_window(qtbot, tmp_path)

    def choose_disc():
        dialog = QApplication.activeModalWidget()
        assert isinstance(dialog, QDialog)
        dialog.findChild(QSpinBox).setValue(3)
        dialog.accept()

    QTimer.singleShot(0, choose_disc)
    qtbot.mouseClick(window.disc_override_button, Qt.MouseButton.LeftButton)
    assert window.session_state.groups[0].disc_number_override == 3
    assert window.session_state.groups[0].group.files == original.groups[0].group.files
    assert window.session_state.groups[0].reviewed_files == ()
    assert window.session_state.groups[0].revision == 1
    assert not (tmp_path / "settings.json").exists()


def test_group_navigation_retains_extended_selection(qtbot, tmp_path):
    window, _ = group_window(qtbot, tmp_path)
    selection = window.group_view.selectionModel()
    selection.select(
        window.group_model.index(0, 0),
        QItemSelectionModel.SelectionFlag.ClearAndSelect | QItemSelectionModel.SelectionFlag.Rows,
    )
    selection.setCurrentIndex(
        window.group_model.index(1, 0),
        QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows,
    )

    assert window.session_state.selection == GroupSelection("two")
    assert set(window.selected_group_ids()) == {"one", "two"}
    assert window.merge_groups_button.isEnabled()
