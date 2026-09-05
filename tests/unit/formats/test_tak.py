from dataclasses import dataclass
from pathlib import Path

import pytest
from mutagen.apev2 import APEv2

from metadata_polisher.domain.errors import MediaErrorCode
from metadata_polisher.domain.media import StreamInfo
from metadata_polisher.domain.metadata import (
    FieldReadState,
    MetadataChange,
    MetadataField,
    MetadataSnapshot,
    Position,
)
from metadata_polisher.formats.base import MediaFormatError
from metadata_polisher.formats.tak import TakAdapter


@dataclass
class FakeTakInfo:
    length: float = 301.25
    sample_rate: int = 96_000
    channels: int = 2
    bits_per_sample: int = 24


# Only container I/O is replaced: writes still interact with the supplied
# APEv2 tag object, while counters expose unnecessary creation or saving.
class FakeTakFile:
    def __init__(self, tags: object | None) -> None:
        self.tags = tags
        self.info = FakeTakInfo()
        self.add_tags_calls = 0
        self.save_calls = 0

    def add_tags(self) -> None:
        self.add_tags_calls += 1
        self.tags = APEv2()

    def save(self) -> None:
        self.save_calls += 1


# Native APEv2 values retain their text/binary types. A plain string dictionary
# would miss mistakes in NUL-separated multi-values and binary artwork handling.
def make_complete_apev2_tags() -> APEv2:
    tags = APEv2()
    tags["Title"] = "決戦"
    tags["Artist"] = ["Artist One", "Artist Two"]
    tags["Album"] = "Soundtrack"
    tags["Album Artist"] = ["Album Artist"]
    tags["Composer"] = ["Composer One", "Composer Two"]
    tags["Track"] = "3/12"
    tags["Disc"] = "2/4"
    tags["Year"] = "2024-01-30"
    tags["Genre"] = ["Game", "Soundtrack"]

    return tags


def test_tak_reads_picard_compatible_apev2_fields_and_stream_properties(
    tmp_path: Path,
) -> None:
    tags = make_complete_apev2_tags()
    audio = FakeTakFile(tags)
    adapter = TakAdapter(loader=lambda _: audio)

    result = adapter.read(tmp_path / "track.tak")

    assert result.metadata == MetadataSnapshot(
        title="決戦",
        artists=("Artist One", "Artist Two"),
        album="Soundtrack",
        album_artists=("Album Artist",),
        composers=("Composer One", "Composer Two"),
        track=Position(number=3, total=12),
        disc=Position(number=2, total=4),
        date="2024-01-30",
        genres=("Game", "Soundtrack"),
    )
    assert all(state is FieldReadState.PRESENT for state in result.field_states.values())
    assert result.stream_info == StreamInfo(
        duration_seconds=301.25,
        sample_rate=96_000,
        channels=2,
        bit_depth=24,
        codec="tak",
    )


def test_tak_writes_selected_raw_apev2_convention_and_preserves_unmanaged_data(
    tmp_path: Path,
) -> None:
    tags = APEv2()
    tags["Cover Art (Front)"] = b"\x00image-data"
    tags["Comment"] = "preserve me"
    artwork = tags["Cover Art (Front)"]
    comment = tags["Comment"]
    audio = FakeTakFile(tags)
    adapter = TakAdapter(loader=lambda _: audio)
    changes = (
        MetadataChange(MetadataField.TITLE, None, "Title"),
        MetadataChange(MetadataField.ARTISTS, (), ("Artist One", "Artist Two")),
        MetadataChange(MetadataField.ALBUM, None, "Album"),
        MetadataChange(MetadataField.ALBUM_ARTISTS, (), ("Album Artist",)),
        MetadataChange(MetadataField.COMPOSERS, (), ("Composer",)),
        MetadataChange(MetadataField.TRACK, Position(), Position(number=3, total=12)),
        MetadataChange(MetadataField.DISC, Position(), Position(number=2, total=4)),
        MetadataChange(MetadataField.DATE, None, "2024-01-30"),
        MetadataChange(MetadataField.GENRES, (), ("Game", "Soundtrack")),
    )

    adapter.write_changes(tmp_path / "track.tak", changes)

    assert str(tags["Title"]) == "Title"
    assert tuple(tags["Artist"]) == ("Artist One", "Artist Two")
    assert str(tags["Album"]) == "Album"
    assert tuple(tags["Album Artist"]) == ("Album Artist",)
    assert tuple(tags["Composer"]) == ("Composer",)
    assert str(tags["Track"]) == "3/12"
    assert str(tags["Disc"]) == "2/4"
    assert "Year" in tuple(tags.keys())
    assert str(tags["Year"]) == "2024-01-30"
    assert tuple(tags["Genre"]) == ("Game", "Soundtrack")
    assert bytes(tags["Artist"]) == b"Artist One\x00Artist Two"
    assert bytes(tags["Genre"]) == b"Game\x00Soundtrack"
    assert tags["Cover Art (Front)"] is artwork
    assert tags["Comment"] is comment
    assert audio.save_calls == 1


def test_tak_tolerates_date_alias_but_rewrites_it_to_canonical_year(tmp_path: Path) -> None:
    tags = APEv2()
    tags["Date"] = "2020-05-06"
    audio = FakeTakFile(tags)
    adapter = TakAdapter(loader=lambda _: audio)

    result = adapter.read(tmp_path / "track.tak")
    adapter.write_changes(
        tmp_path / "track.tak",
        (MetadataChange(MetadataField.DATE, "2020-05-06", "2024-01-30"),),
    )

    assert result.metadata.date == "2020-05-06"
    raw_keys = tuple(tags.keys())
    assert "Year" in raw_keys
    assert "Date" not in raw_keys
    assert str(tags["Year"]) == "2024-01-30"


def test_tak_marks_binary_and_malformed_managed_values_unreadable(tmp_path: Path) -> None:
    tags = APEv2()
    tags["Title"] = b"binary-title"
    tags["Track"] = "not-a-position"
    audio = FakeTakFile(tags)
    adapter = TakAdapter(loader=lambda _: audio)

    result = adapter.read(tmp_path / "broken.tak")

    assert result.metadata.title is None
    assert result.metadata.track == Position()
    assert result.field_states[MetadataField.TITLE] is FieldReadState.UNREADABLE
    assert result.field_states[MetadataField.TRACK] is FieldReadState.UNREADABLE
    assert result.field_states[MetadataField.ALBUM] is FieldReadState.MISSING
    assert set(result.field_states) == set(MetadataField)
    assert {issue.code for issue in result.issues} == {MediaErrorCode.TAG_READ_FAILED}


def test_tak_validates_entire_batch_before_mutating_tags(tmp_path: Path) -> None:
    tags = make_complete_apev2_tags()
    original_title = tags["Title"]
    original_track = tags["Track"]
    audio = FakeTakFile(tags)
    adapter = TakAdapter(loader=lambda _: audio)

    with pytest.raises(TypeError):
        adapter.write_changes(
            tmp_path / "track.tak",
            (
                MetadataChange(MetadataField.TITLE, "決戦", "Revised"),
                MetadataChange(MetadataField.TRACK, Position(number=3, total=12), "invalid"),
            ),
        )

    assert tags["Title"] is original_title
    assert tags["Track"] is original_track
    assert audio.save_calls == 0


def test_tak_validates_utf8_before_mutating_tags(tmp_path: Path) -> None:
    tags = make_complete_apev2_tags()
    original_title = tags["Title"]
    audio = FakeTakFile(tags)
    adapter = TakAdapter(loader=lambda _: audio)

    with pytest.raises(UnicodeEncodeError):
        adapter.write_changes(
            tmp_path / "track.tak",
            (MetadataChange(MetadataField.TITLE, "決戦", "\ud800"),),
        )

    assert tags["Title"] is original_title
    assert audio.save_calls == 0


def test_tak_tagless_clear_is_noop_but_nonempty_change_uses_native_tag_creation(
    tmp_path: Path,
) -> None:
    audio = FakeTakFile(tags=None)
    adapter = TakAdapter(loader=lambda _: audio)

    adapter.write_changes(
        tmp_path / "track.tak",
        (MetadataChange(MetadataField.COMPOSERS, (), ()),),
    )

    assert audio.tags is None
    assert audio.add_tags_calls == 0
    assert audio.save_calls == 0

    adapter.write_changes(
        tmp_path / "track.tak",
        (MetadataChange(MetadataField.COMPOSERS, (), ("Composer One", "Composer Two")),),
    )

    assert isinstance(audio.tags, APEv2)
    assert tuple(audio.tags["Composer"]) == ("Composer One", "Composer Two")
    assert audio.add_tags_calls == 1
    assert audio.save_calls == 1


def test_tak_verify_rejects_unreadable_clear_and_changed_stream(tmp_path: Path) -> None:
    tags = APEv2()
    tags["Disc"] = "broken-position"
    audio = FakeTakFile(tags)
    adapter = TakAdapter(loader=lambda _: audio)

    result = adapter.verify(
        tmp_path / "track.tak",
        expected=MetadataSnapshot(disc=Position()),
        changed_fields=frozenset({MetadataField.DISC}),
        baseline_stream=StreamInfo(
            duration_seconds=999.0,
            sample_rate=96_000,
            channels=2,
            bit_depth=24,
            codec="tak",
        ),
    )

    assert not result.ok
    assert len(result.issues) == 2
    assert {issue.code for issue in result.issues} == {MediaErrorCode.VERIFICATION_FAILED}


def test_tak_wraps_loader_and_save_failures_with_typed_chained_errors(tmp_path: Path) -> None:
    load_failure = OSError("cannot open media")

    def failing_loader(_: Path) -> FakeTakFile:
        raise load_failure

    path = tmp_path / "track.tak"

    with pytest.raises(MediaFormatError) as load_error:
        TakAdapter(loader=failing_loader).read(path)

    assert load_error.value.issue.code is MediaErrorCode.TAG_READ_FAILED
    assert load_error.value.__cause__ is load_failure

    save_failure = OSError("disk write failed")

    class SaveFailingTakFile(FakeTakFile):
        def save(self) -> None:
            raise save_failure

    with pytest.raises(MediaFormatError) as save_error:
        TakAdapter(loader=lambda _: SaveFailingTakFile(tags=APEv2())).write_changes(
            path,
            (MetadataChange(MetadataField.TITLE, None, "Title"),),
        )

    assert save_error.value.issue.code is MediaErrorCode.TAG_WRITE_FAILED
    assert save_error.value.__cause__ is save_failure
