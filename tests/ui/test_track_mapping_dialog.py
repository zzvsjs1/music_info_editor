"""Explicit track assignments stay local to the dialog until accepted."""

from PySide6.QtWidgets import QDialog

from metadata_polisher.matching.track_mapping import map_tracks
from metadata_polisher.ui.dialogs.track_mapping_dialog import TrackMappingDialog
from tests.unit.matching.test_track_mapping import make_local, make_provider, make_release, mapping_pairs


def mapping_dialog(qtbot):
    files = (
        make_local("opening", title="Opening", tagged_number=1, duration=181.0),
        make_local("finale", title="Finale", tagged_number=2, duration=241.0),
    )
    candidate = make_release(
        make_provider(1, "Opening", duration=181.0),
        make_provider(2, "Finale", duration=241.0),
        make_provider(3, "Bonus", duration=301.0),
        medium_number=2,
        leading_medium=True,
    )
    mapping = map_tracks(files, candidate, selected_medium_index=1)
    dialog = TrackMappingDialog(files, candidate, mapping)
    qtbot.addWidget(dialog)
    return dialog, mapping


def test_track_mapping_lists_local_and_selected_medium_context_without_changing_mapping(qtbot):
    dialog, mapping = mapping_dialog(qtbot)
    model = dialog.table.model()
    assert model.rowCount() == 2
    assert model.index(0, 0).data() == "opening.flac"
    assert model.index(0, 1).data() == "Opening"
    assert model.index(0, 2).data() == "3:01"
    assert model.index(1, 0).data() == "finale.flac"
    assert model.index(1, 2).data() == "4:01"

    selector = dialog.selectors["opening"]
    assert [selector.itemData(index) for index in range(selector.count())] == [None, 0, 1, 2]
    assert selector.currentData() == 0
    assert "Opening" in selector.itemText(1)
    assert "3:01" in selector.itemText(1)
    assert "Bonus" in selector.itemText(3)
    assert "Other disc" not in " ".join(selector.itemText(index) for index in range(selector.count()))

    evidence_text = model.index(0, 4).data()

    for evidence in mapping.mappings[0].evidence:
        assert evidence.code in evidence_text
        assert evidence.detail in evidence_text

    assert dialog.mapping() is mapping


# One provider track cannot be assigned twice. After rejecting that choice the
# widget must return to the last valid mapping without recursively editing it.
def test_duplicate_provider_selection_is_rejected_and_original_choice_restored(qtbot):
    dialog, mapping = mapping_dialog(qtbot)
    selector = dialog.selectors["finale"]

    selector.setCurrentIndex(selector.findData(0))

    assert dialog.mapping() is mapping
    assert selector.currentData() == 1
    assert dialog.error_label.text()
    assert mapping_pairs(mapping) == (("opening", 0), ("finale", 1))


# Deliberate unmapping releases a one-to-one reservation; moving a track between
# local files requires this explicit intermediate state rather than an implicit swap.
def test_clearing_an_assignment_frees_the_track_for_another_local_file(qtbot):
    dialog, original = mapping_dialog(qtbot)
    dialog.selectors["opening"].setCurrentIndex(0)
    selector = dialog.selectors["finale"]
    selector.setCurrentIndex(selector.findData(0))
    dialog.accept()

    assert dialog.result() == QDialog.DialogCode.Accepted
    changed = dialog.mapping()
    assert mapping_pairs(changed) == (("finale", 0),)
    assert changed.unmatched_local_file_ids == ("opening",)
    assert changed.unmatched_provider_indexes == (1, 2)
    assert changed.selected_medium_index == 1
    assert not dialog.error_label.text()
    assert mapping_pairs(original) == (("opening", 0), ("finale", 1))


def test_unused_provider_track_can_be_selected_without_changing_another_assignment(qtbot):
    dialog, original = mapping_dialog(qtbot)
    selector = dialog.selectors["finale"]
    selector.setCurrentIndex(selector.findData(2))
    dialog.accept()

    assert dialog.result() == QDialog.DialogCode.Accepted
    assert mapping_pairs(dialog.mapping()) == (("opening", 0), ("finale", 2))
    assert dialog.mapping().unmatched_provider_indexes == (1,)
    assert dialog.mapping().mappings[0] is original.mappings[0]
    assert mapping_pairs(original) == (("opening", 0), ("finale", 1))


def test_all_local_files_may_be_left_explicitly_unmapped(qtbot):
    dialog, original = mapping_dialog(qtbot)

    for selector in dialog.selectors.values():
        selector.setCurrentIndex(0)

    dialog.accept()

    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.mapping().mappings == ()
    assert set(dialog.mapping().unmatched_local_file_ids) == {"opening", "finale"}
    assert dialog.mapping().unmatched_provider_indexes == (0, 1, 2)
    assert mapping_pairs(original) == (("opening", 0), ("finale", 1))
