"""Contradictory physical aliases remain visible until that field is reviewed."""

from copy import deepcopy

import pytest
from mutagen.apev2 import APEv2

from metadata_polisher.domain.metadata import FieldReadState, MetadataChange, MetadataField, Position
from metadata_polisher.formats.flac import FlacAdapter
from metadata_polisher.formats.tak import TakAdapter
from tests.unit.formats.test_flac import FakeFlacFile
from tests.unit.formats.test_tak import FakeTakFile


@pytest.mark.parametrize("keys,field", [(("TRACKTOTAL", "TOTALTRACKS"), MetadataField.TRACK),
                                       (("DISCTOTAL", "TOTALDISCS"), MetadataField.DISC)])
@pytest.mark.parametrize("reverse", [False, True])
def test_conflicting_vorbis_aliases_are_unreadable_independently_of_key_order(tmp_path, keys, field, reverse) -> None:
    # Reverse insertion order to prove the adapter reports a conflict instead
    # of accepting whichever canonical/alias value it encounters first.
    items = [(keys[0], ["9"]), (keys[1], ["99"]), ("UNMANAGED", ["Keep"])]
    tags = dict(reversed(items) if reverse else items)
    before = deepcopy(tags)
    adapter = FlacAdapter(loader=lambda _: FakeFlacFile(tags))
    result = adapter.read(tmp_path / "fixture.flac")

    assert result.field_states[field] is FieldReadState.UNREADABLE
    assert any("conflict" in (issue.technical_detail or "").casefold() for issue in result.issues)
    assert tags == before


@pytest.mark.parametrize("values,state", [(["9", "99"], FieldReadState.UNREADABLE),
                                         (["09", "9"], FieldReadState.PRESENT)])
def test_duplicate_numeric_vorbis_values_are_compared_semantically(tmp_path, values, state) -> None:
    audio = FakeFlacFile({"TRACKTOTAL": values})
    result = FlacAdapter(loader=lambda _: audio).read(tmp_path / "fixture.flac")

    assert result.field_states[MetadataField.TRACK] is state


def test_explicit_vorbis_position_change_replaces_only_its_conflicting_aliases(tmp_path) -> None:
    tags = {"TRACKTOTAL": ["9"], "TOTALTRACKS": ["99"], "DISCTOTAL": ["2"], "TOTALDISCS": ["22"]}
    audio = FakeFlacFile(tags)
    FlacAdapter(loader=lambda _: audio).write_changes(tmp_path / "fixture.flac", (
        MetadataChange(MetadataField.TRACK, Position(), Position(1, 12)),
    ))

    assert tags == {"TRACKNUMBER": ["1"], "TRACKTOTAL": ["12"], "DISCTOTAL": ["2"], "TOTALDISCS": ["22"]}


def test_ape_year_date_conflict_is_surfaced_and_preserved_by_unrelated_edit(tmp_path) -> None:
    tags = APEv2()
    tags["Year"] = "2024"
    tags["Date"] = "1999"
    # Binary artwork acts as an unrelated sentinel while a Title edit leaves
    # the conflicting Year and Date values available for explicit review.
    tags["Cover Art (Front)"] = b"cover.png\x00binary\x01sentinel"
    audio = FakeTakFile(tags)
    adapter = TakAdapter(loader=lambda _: audio)
    result = adapter.read(tmp_path / "fixture.tak")

    assert result.field_states[MetadataField.DATE] is FieldReadState.UNREADABLE
    assert any("conflict" in (issue.technical_detail or "").casefold() for issue in result.issues)

    adapter.write_changes(tmp_path / "fixture.tak", (MetadataChange(MetadataField.TITLE, None, "New"),))
    assert str(tags["Year"]) == "2024" and str(tags["Date"]) == "1999"
    assert bytes(tags["Cover Art (Front)"]) == b"cover.png\x00binary\x01sentinel"


def test_equivalent_ape_date_aliases_remain_readable_and_explicit_change_uses_year(tmp_path) -> None:
    tags = APEv2()
    tags["Year"] = "2024-02"
    tags["Date"] = "2024-02"
    adapter = TakAdapter(loader=lambda _: FakeTakFile(tags))

    assert adapter.read(tmp_path / "fixture.tak").metadata.date == "2024-02"
    adapter.write_changes(tmp_path / "fixture.tak", (MetadataChange(MetadataField.DATE, "2024-02", "2025"),))
    assert str(tags["Year"]) == "2025" and "Date" not in tags
