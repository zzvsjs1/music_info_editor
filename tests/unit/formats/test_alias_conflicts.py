"""Contradictory physical aliases remain visible until that field is reviewed."""

from copy import deepcopy

import pytest
from mutagen.apev2 import APEv2

from metadata_polisher.domain.metadata import FieldReadState, MetadataChange, MetadataField, Position
from metadata_polisher.formats.flac import FlacAdapter
from metadata_polisher.formats.mp4 import Mp4Adapter
from metadata_polisher.formats.tak import TakAdapter
from tests.unit.formats.test_flac import FakeFlacFile
from tests.unit.formats.test_mp4 import FakeMp4File
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


@pytest.mark.parametrize("format_id", ("flac", "tak", "mp4"))
def test_reading_keeps_each_fields_value_state_and_diagnostic_together(tmp_path, format_id) -> None:
    # Mixed states in one read expose accidental reuse of another field's
    # diagnostic. The public snapshot also distinguishes unreadable positions
    # from genuinely missing ones, even though both have empty domain values.
    if format_id == "flac":
        tags = {
            "TITLE": ["Visible title"],
            "ARTIST": ["", "Artist One", "Artist Two"],
            "TRACKNUMBER": ["1/2/3"],
            "DATE": [9],
        }
        adapter = FlacAdapter(loader=lambda _: FakeFlacFile(tags))
        suffix = ".flac"
        track_detail = "TRACKNUMBER"
        date_detail = "non-string"
    elif format_id == "tak":
        tags = APEv2()
        tags["Title"] = "Visible title"
        tags["Artist"] = ["", "Artist One", "Artist Two"]
        tags["Track"] = "1/2/3"
        tags["Year"] = b"\x01\x02"
        adapter = TakAdapter(loader=lambda _: FakeTakFile(tags))
        suffix = ".tak"
        track_detail = "more than one slash"
        date_detail = "UTF-8 text"
    else:
        tags = {
            "©nam": ["Visible title"],
            "©ART": ["", "Artist One", "Artist Two"],
            "trkn": [(1, "bad total")],
            "©day": [9],
        }
        adapter = Mp4Adapter(loader=lambda _: FakeMp4File(tags))
        suffix = ".m4a"
        track_detail = "non-integer position"
        date_detail = "non-string"

    before = deepcopy(dict(tags.items()))
    result = adapter.read(tmp_path / f"fixture{suffix}")

    assert result.metadata.title == "Visible title"
    assert result.metadata.artists == ("Artist One", "Artist Two")
    assert result.field_states[MetadataField.TITLE] is FieldReadState.PRESENT
    assert result.field_states[MetadataField.ARTISTS] is FieldReadState.PRESENT
    assert result.metadata.track == result.metadata.disc == Position()
    assert result.field_states[MetadataField.TRACK] is FieldReadState.UNREADABLE
    assert result.field_states[MetadataField.DISC] is FieldReadState.MISSING
    assert result.metadata.date is None
    assert result.field_states[MetadataField.DATE] is FieldReadState.UNREADABLE
    assert len(result.issues) == 2
    track_issue = next(issue for issue in result.issues if "track metadata" in issue.message)
    date_issue = next(issue for issue in result.issues if "date metadata" in issue.message)
    assert track_detail in track_issue.technical_detail
    assert date_detail in date_issue.technical_detail
    assert dict(tags.items()) == before
