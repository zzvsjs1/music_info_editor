from dataclasses import dataclass
from pathlib import Path

import pytest
from mutagen.mp4 import MP4Tags

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
from metadata_polisher.formats.mp4 import Mp4Adapter


@dataclass
class FakeMp4Info:
    length: float = 245.75
    sample_rate: int = 48_000
    channels: int = 2
    bits_per_sample: int = 24
    codec: str = "mp4a.40.2"


class FakeMp4File:
    def __init__(self, tags: dict[str, object] | None, *, info: FakeMp4Info | None = None) -> None:
        self.tags = tags
        self.info = info or FakeMp4Info()
        self.add_tags_calls = 0
        self.save_calls = 0

    def add_tags(self) -> None:
        self.add_tags_calls += 1
        self.tags = {}

    def save(self) -> None:
        self.save_calls += 1


# Keep the exact case-sensitive atom names and integer-pair shapes visible
# here, so the fixture does not copy them from the adapter's own lookup tables.
def make_complete_mp4_tags() -> dict[str, object]:
    return {
        "\xa9nam": ["決戦"],
        "\xa9ART": ["Artist One", "Artist Two"],
        "\xa9alb": ["Soundtrack"],
        "aART": ["Album Artist"],
        "\xa9wrt": ["Composer One", "Composer Two"],
        "trkn": [(3, 12)],
        "disk": [(2, 4)],
        "\xa9day": ["2024-01-30"],
        "\xa9gen": ["Game", "Soundtrack"],
    }


def test_mp4_reads_all_managed_atoms_multi_values_and_stream_identity(tmp_path: Path) -> None:
    tags = make_complete_mp4_tags()
    artwork = object()
    tags["covr"] = [artwork]
    audio = FakeMp4File(tags)
    adapter = Mp4Adapter(loader=lambda _: audio)

    result = adapter.read(tmp_path / "track.m4a")

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
        duration_seconds=245.75,
        sample_rate=48_000,
        channels=2,
        bit_depth=24,
        codec="mp4a.40.2",
    )
    assert tags["covr"] == [artwork]


def test_mp4_marks_malformed_present_atoms_unreadable_with_typed_issues(tmp_path: Path) -> None:
    audio = FakeMp4File(
        {
            "\xa9nam": [object()],
            "trkn": [("three", 12)],
        }
    )
    adapter = Mp4Adapter(loader=lambda _: audio)

    result = adapter.read(tmp_path / "broken.m4a")

    assert result.metadata.title is None
    assert result.metadata.track == Position()
    assert result.field_states[MetadataField.TITLE] is FieldReadState.UNREADABLE
    assert result.field_states[MetadataField.TRACK] is FieldReadState.UNREADABLE
    assert result.field_states[MetadataField.ALBUM] is FieldReadState.MISSING
    assert set(result.field_states) == set(MetadataField)
    assert {issue.code for issue in result.issues} == {MediaErrorCode.TAG_READ_FAILED}


def test_mp4_writes_only_changed_canonical_atoms_and_preserves_artwork(tmp_path: Path) -> None:
    tags = make_complete_mp4_tags()
    artwork = object()
    freeform = object()
    tags["covr"] = [artwork]
    tags["----:example:unmanaged"] = [freeform]
    original_album = tags["\xa9alb"]
    audio = FakeMp4File(tags)
    adapter = Mp4Adapter(loader=lambda _: audio)

    adapter.write_changes(
        tmp_path / "track.m4a",
        (
            MetadataChange(
                field=MetadataField.COMPOSERS,
                old_value=("Composer One", "Composer Two"),
                new_value=("Revised Composer", "Guest Composer"),
            ),
            MetadataChange(
                field=MetadataField.TRACK,
                old_value=Position(number=3, total=12),
                new_value=Position(number=4, total=13),
            ),
        ),
    )

    assert tags["\xa9wrt"] == ["Revised Composer", "Guest Composer"]
    assert tags["trkn"] == [(4, 13)]
    assert tags["\xa9alb"] is original_album
    assert tags["covr"] == [artwork]
    assert tags["----:example:unmanaged"] == [freeform]
    assert audio.add_tags_calls == 0
    assert audio.save_calls == 1


@pytest.mark.parametrize(
    ("field", "new_value", "atom", "expected_raw"),
    [
        (MetadataField.TITLE, "Title", "©nam", ["Title"]),
        (MetadataField.ARTISTS, ("Artist One", "Artist Two"), "©ART", ["Artist One", "Artist Two"]),
        (MetadataField.ALBUM, "Album", "©alb", ["Album"]),
        (MetadataField.ALBUM_ARTISTS, ("Album Artist",), "aART", ["Album Artist"]),
        (MetadataField.COMPOSERS, ("Composer",), "©wrt", ["Composer"]),
        (MetadataField.TRACK, Position(number=3, total=12), "trkn", [(3, 12)]),
        (MetadataField.DISC, Position(number=2, total=4), "disk", [(2, 4)]),
        (MetadataField.DATE, "2024-01-30", "©day", ["2024-01-30"]),
        (MetadataField.GENRES, ("Game", "Soundtrack"), "©gen", ["Game", "Soundtrack"]),
    ],
)
def test_mp4_writes_each_managed_field_to_its_canonical_atom(
    field: MetadataField,
    new_value: object,
    atom: str,
    expected_raw: object,
    tmp_path: Path,
) -> None:
    tags: dict[str, object] = {}
    audio = FakeMp4File(tags)
    adapter = Mp4Adapter(loader=lambda _: audio)

    adapter.write_changes(
        tmp_path / "track.m4a",
        (MetadataChange(field=field, old_value=None, new_value=new_value),),
    )

    assert tags == {atom: expected_raw}


# An invalid field later in the request must not replace an earlier atom.
# The loaded mapping is inspected directly to expose partial in-memory edits.
def test_mp4_validates_entire_batch_before_mutating_atoms(tmp_path: Path) -> None:
    tags = make_complete_mp4_tags()
    original_title = tags["\xa9nam"]
    original_track = tags["trkn"]
    audio = FakeMp4File(tags)
    adapter = Mp4Adapter(loader=lambda _: audio)

    with pytest.raises(TypeError):
        adapter.write_changes(
            tmp_path / "track.m4a",
            (
                MetadataChange(MetadataField.TITLE, "決戦", "Revised"),
                MetadataChange(MetadataField.TRACK, Position(number=3, total=12), "invalid"),
            ),
        )

    assert tags["\xa9nam"] is original_title
    assert tags["trkn"] is original_track
    assert audio.save_calls == 0


def test_mp4_validates_utf8_before_removing_existing_atom(tmp_path: Path) -> None:
    tags = MP4Tags()
    tags["©nam"] = ["Original"]
    original_title = tags["©nam"]
    audio = FakeMp4File(tags)  # type: ignore[arg-type]
    adapter = Mp4Adapter(loader=lambda _: audio)

    with pytest.raises(UnicodeEncodeError):
        adapter.write_changes(
            tmp_path / "track.m4a",
            (MetadataChange(MetadataField.TITLE, "Original", "\ud800"),),
        )

    assert tags["©nam"] is original_title
    assert audio.save_calls == 0


def test_mp4_tagless_clear_is_noop_but_nonempty_change_uses_native_tag_creation(
    tmp_path: Path,
) -> None:
    audio = FakeMp4File(tags=None)
    adapter = Mp4Adapter(loader=lambda _: audio)

    adapter.write_changes(
        tmp_path / "track.m4a",
        (MetadataChange(MetadataField.GENRES, (), ()),),
    )

    assert audio.tags is None
    assert audio.add_tags_calls == 0
    assert audio.save_calls == 0

    adapter.write_changes(
        tmp_path / "track.m4a",
        (MetadataChange(MetadataField.GENRES, (), ("Game", "Soundtrack")),),
    )

    assert audio.tags == {"\xa9gen": ["Game", "Soundtrack"]}
    assert audio.add_tags_calls == 1
    assert audio.save_calls == 1


def test_mp4_probe_rejects_container_without_audio_codec(tmp_path: Path) -> None:
    video_only = FakeMp4File(tags={}, info=FakeMp4Info(codec=""))

    assert not Mp4Adapter(loader=lambda _: video_only).can_handle(tmp_path / "video.mp4")


def test_mp4_verify_rejects_unreadable_clear_and_changed_stream(tmp_path: Path) -> None:
    audio = FakeMp4File({"disk": [("broken", 0)]})
    adapter = Mp4Adapter(loader=lambda _: audio)

    result = adapter.verify(
        tmp_path / "track.m4a",
        expected=MetadataSnapshot(disc=Position()),
        changed_fields=frozenset({MetadataField.DISC}),
        baseline_stream=StreamInfo(
            duration_seconds=999.0,
            sample_rate=48_000,
            channels=2,
            bit_depth=24,
            codec="mp4a.40.2",
        ),
    )

    assert not result.ok
    assert len(result.issues) == 2
    assert {issue.code for issue in result.issues} == {MediaErrorCode.VERIFICATION_FAILED}


def test_mp4_wraps_loader_and_save_failures_with_typed_chained_errors(tmp_path: Path) -> None:
    load_failure = OSError("cannot open media")

    def failing_loader(_: Path) -> FakeMp4File:
        raise load_failure

    path = tmp_path / "track.m4a"

    with pytest.raises(MediaFormatError) as load_error:
        Mp4Adapter(loader=failing_loader).read(path)

    assert load_error.value.issue.code is MediaErrorCode.TAG_READ_FAILED
    assert load_error.value.__cause__ is load_failure

    save_failure = OSError("disk write failed")

    class SaveFailingMp4File(FakeMp4File):
        def save(self) -> None:
            raise save_failure

    with pytest.raises(MediaFormatError) as save_error:
        Mp4Adapter(loader=lambda _: SaveFailingMp4File(tags={})).write_changes(
            path,
            (MetadataChange(MetadataField.TITLE, None, "Title"),),
        )

    assert save_error.value.issue.code is MediaErrorCode.TAG_WRITE_FAILED
    assert save_error.value.__cause__ is save_failure
