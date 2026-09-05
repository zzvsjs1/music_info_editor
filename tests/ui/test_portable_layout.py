"""Geometry remains portable and never replaces a corrupt settings document."""

import json
from pathlib import Path

import pytest
from PySide6.QtCore import QRect

from metadata_polisher.bootstrap import create_application
from metadata_polisher.infrastructure.settings import load_settings
from metadata_polisher.session.state import GroupSelection, SessionState
from tests.ui.test_main_window import make_group


def test_resized_splitters_and_columns_survive_a_portable_restart(qtbot, tmp_path):
    path = tmp_path / "settings.json"
    _, window = create_application([], settings_file=path)
    qtbot.addWidget(window)
    window.show()
    window.resize(940, 740)
    window.main_splitter.setSizes([230, 670])
    window.diff_table_view.setColumnWidth(2, 211)
    expected = window.main_splitter.sizes()
    window.close()

    assert json.loads(path.read_text(encoding="utf-8"))["ui"]["version"] == 1
    _, reopened = create_application([], settings_file=path)
    qtbot.addWidget(reopened)
    reopened.show()

    assert reopened.diff_table_view.columnWidth(2) == 211
    assert abs(reopened.main_splitter.sizes()[0] - expected[0]) < 20


# Persisted geometry can outlive a monitor arrangement. Restoration must keep
# the window reachable rather than blindly trusting previously valid coordinates.
def test_invalid_and_offscreen_layout_falls_back_inside_available_screen(qtbot, tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"schema_version": 1, "ui": {
        "version": 1, "geometry": [100000, -100000, 99999, 99999],
        "horizontal_sizes": [-1, 500], "column_widths": {"diffTableView": ["invalid"]},
    }}), encoding="utf-8")
    _, window = create_application([], settings_file=path)
    qtbot.addWidget(window)
    window.show()
    available = window.screen().availableGeometry()

    assert available.contains(QRect(window.pos(), window.size()).center())
    assert window.width() <= available.width()
    assert window.height() <= available.height()
    assert load_settings(path).warnings


@pytest.mark.parametrize("close_review_first", [False, True])
def test_review_window_size_and_columns_survive_a_portable_restart(qtbot, tmp_path, close_review_first):
    path = tmp_path / "settings.json"
    group = make_group("album", "track", "Album")
    state = SessionState(root=Path("library"), groups=(group,), selection=GroupSelection("album"))
    _, window = create_application([], settings_file=path)
    qtbot.addWidget(window)
    window.show()
    window.set_session_state(state)
    window.file_table_view.selectRow(0)
    window.open_metadata_review()
    window.review_window.resize(850, 590)
    window.diff_table_view.setColumnWidth(2, 211)
    expected = window.review_window.size()

    if close_review_first:
        window.review_window.close()

    window.close()

    saved = load_settings(path).settings.ui
    assert saved.dialog_sizes["MetadataReviewWindow"] == (expected.width(), expected.height())
    assert saved.column_widths["MainWindow/diffTableView"][2] == 211

    _, reopened = create_application([], settings_file=path)
    qtbot.addWidget(reopened)
    reopened.show()
    reopened.set_session_state(state)
    reopened.file_table_view.selectRow(0)
    assert not reopened.diff_table_view.isVisible()
    reopened.open_review_button.click()
    qtbot.wait(10)

    assert reopened.review_window.size() == expected
    assert reopened.diff_table_view.columnWidth(2) == 211


def test_old_review_splitter_sizes_do_not_reduce_the_file_workspace(qtbot, tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"schema_version": 1, "ui": {
        "version": 1, "vertical_sizes": [180, 490],
        "column_widths": {"MainWindow/diffTableView": [100, 108, 211, 180, 180, 180]},
        "hidden_columns": {"MainWindow/diffTableView": [5]},
    }}), encoding="utf-8")
    _, window = create_application([], settings_file=path)
    qtbot.addWidget(window)
    window.show()
    window.resize(960, 700)
    group = make_group("album", "track", "Album")
    window.set_session_state(SessionState(
        root=Path("library"), groups=(group,), selection=GroupSelection("album"),
    ))
    window.file_table_view.selectRow(0)
    height = window.file_table_view.viewport().height()
    window.open_metadata_review()

    assert window.main_splitter.widget(1).isAncestorOf(window.file_table_view)
    assert not window.main_splitter.isAncestorOf(window.diff_table_view)
    assert window.file_table_view.viewport().height() == height
    assert height >= 8 * window.file_table_view.rowHeight(0)
    assert window.diff_table_view.columnWidth(2) == 211
    assert window.diff_table_view.isColumnHidden(5)


def test_oversized_saved_review_window_is_bounded_to_the_available_screen(qtbot, tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"schema_version": 1, "ui": {
        "version": 1, "dialog_sizes": {"MetadataReviewWindow": [99999, 99999]},
    }}), encoding="utf-8")
    _, window = create_application([], settings_file=path)
    qtbot.addWidget(window)
    window.show()
    window.open_metadata_review()
    available = window.review_window.screen().availableGeometry()

    assert window.review_window.isVisible()
    assert window.review_window.width() <= available.width()
    assert window.review_window.height() <= available.height()


def test_reset_review_columns_and_size_persist_after_reopening(qtbot, tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"schema_version": 1, "ui": {
        "version": 1, "dialog_sizes": {"MetadataReviewWindow": [850, 590]},
        "column_widths": {"MainWindow/diffTableView": [100, 108, 211, 180, 180, 180]},
    }}), encoding="utf-8")
    _, window = create_application([], settings_file=path)
    qtbot.addWidget(window)
    window.show()
    window.open_metadata_review()
    window.review_window.close()
    window.reset_layout()
    default_size = window.review_window.size()
    default_width = window.diff_table_view.columnWidth(2)
    window.open_metadata_review()

    assert window.review_window.size() == default_size
    assert window.diff_table_view.columnWidth(2) == default_width
    window.close()

    _, reopened = create_application([], settings_file=path)
    qtbot.addWidget(reopened)
    reopened.show()
    reopened.open_metadata_review()
    assert reopened.review_window.size() == default_size
    assert reopened.diff_table_view.columnWidth(2) == default_width


def test_closing_window_does_not_overwrite_corrupt_settings(qtbot, tmp_path):
    path = tmp_path / "settings.json"
    original = '{"schema_version": 1, "unfinished":'
    path.write_text(original, encoding="utf-8")
    _, window = create_application([], settings_file=path)
    qtbot.addWidget(window)
    window.show()
    window.resize(900, 700)
    window.close()

    assert path.read_text(encoding="utf-8") == original


def test_closing_window_preserves_a_future_layout_version(qtbot, tmp_path):
    path = tmp_path / "settings.json"
    original = json.dumps({"schema_version": 1, "ui": {"version": 99, "future_layout": [1, 2]}})
    path.write_text(original, encoding="utf-8")
    _, window = create_application([], settings_file=path)
    qtbot.addWidget(window)
    window.show()
    window.close()

    assert path.read_text(encoding="utf-8") == original


# Closing the UI is an automatic convenience save, so it cannot take ownership
# of configuration that another editor changed after the window loaded it.
def test_closing_window_preserves_settings_edited_externally_after_launch(qtbot, tmp_path):
    path = tmp_path / "settings.json"
    _, window = create_application([], settings_file=path)
    qtbot.addWidget(window)
    window.show()
    external = json.dumps({"schema_version": 1, "general": {"last_root_folder": "new-user-folder"}})
    path.write_text(external, encoding="utf-8")
    window.close()

    assert path.read_text(encoding="utf-8") == external
