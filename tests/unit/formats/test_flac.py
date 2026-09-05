from dataclasses import dataclass
from pathlib import Path

import pytest
from mutagen.flac import VCFLACDict

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
from metadata_polisher.formats.flac import FlacAdapter


@dataclass
class FakeFlacInfo:
    length: float = 185.25
    sample_rate: int = 44_100
    channels: int = 2
    bits_per_sample: int = 16


# Counters expose unwanted saves or tag creation without needing audio bytes.
# Separate real-container tests establish preservation on physical FLAC files.
class FakeFlacFile:
    def __init__(self, tags: dict[str, list[object]] | None) -> None:
        self.tags = tags
        self.info = FakeFlacInfo()
        self.save_calls = 0
        self.add_tags_calls = 0

    def add_tags(self) -> None:
        self.add_tags_calls += 1
        self.tags = {}

    def save(self) -> None:
        self.save_calls += 1


def test_flac_reads_alias_totals_multi_values_and_stream_properties(tmp_path: Path) -> None:
    path = tmp_path / "track.flac"
    audio = FakeFlacFile(
        {
            "TITLE": ["決戦"],
            "ARTIST": ["Artist One", "Artist Two"],
            "ALBUM": ["Soundtrack"],
            "ALBUMARTIST": ["Album Artist"],
            "COMPOSER": ["Composer One", "Composer Two"],
            "TRACKNUMBER": ["3"],
            "TOTALTRACKS": ["12"],
            "DISCNUMBER": ["2"],
            "TOTALDISCS": ["4"],
            "DATE": ["2024-01-30"],
            "GENRE": ["Game", "Soundtrack"],
            "UNMANAGED": ["preserve me"],
        }
    )
    adapter = FlacAdapter(loader=lambda _: audio)

    result = adapter.read(path)

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
        duration_seconds=185.25,
        sample_rate=44_100,
        channels=2,
        bit_depth=16,
        codec="flac",
    )
    assert result.issues == ()


def test_flac_surfaces_conflicting_total_keys_instead_of_preferring_an_alias(tmp_path: Path) -> None:
    audio = FakeFlacFile(
        {
            "TRACKNUMBER": ["1"],
            "TRACKTOTAL": ["9"],
            "TOTALTRACKS": ["99"],
            "DISCNUMBER": ["1"],
            "DISCTOTAL": ["2"],
            "TOTALDISCS": ["22"],
        }
    )
    adapter = FlacAdapter(loader=lambda _: audio)

    result = adapter.read(tmp_path / "track.flac")

    assert result.field_states[MetadataField.TRACK] is FieldReadState.UNREADABLE
    assert result.field_states[MetadataField.DISC] is FieldReadState.UNREADABLE


# A native Vorbis dictionary iterates pairs, unlike dict; this case catches
# adapters that accidentally assume all tag mappings iterate keys.
def test_flac_supports_real_vorbis_mapping_pair_iteration(tmp_path: Path) -> None:
    tags = VCFLACDict()
    tags["TITLE"] = ["Original"]
    tags["UNMANAGED"] = ["preserve me"]
    audio = FakeFlacFile(tags)  # type: ignore[arg-type]
    adapter = FlacAdapter(loader=lambda _: audio)

    result = adapter.read(tmp_path / "track.flac")
    adapter.write_changes(
        tmp_path / "track.flac",
        (
            MetadataChange(
                field=MetadataField.TITLE,
                old_value="Original",
                new_value="Revised",
            ),
        ),
    )

    assert result.metadata.title == "Original"
    assert tags["title"] == ["Revised"]
    assert tags["unmanaged"] == ["preserve me"]


def test_flac_marks_malformed_present_values_unreadable_without_hiding_issue(
    tmp_path: Path,
) -> None:
    audio = FakeFlacFile(
        {
            "TITLE": [object()],
            "TRACKNUMBER": ["not-a-number"],
        }
    )
    adapter = FlacAdapter(loader=lambda _: audio)

    result = adapter.read(tmp_path / "broken.flac")

    assert result.metadata.title is None
    assert result.metadata.track == Position()
    assert result.field_states[MetadataField.TITLE] is FieldReadState.UNREADABLE
    assert result.field_states[MetadataField.TRACK] is FieldReadState.UNREADABLE
    assert result.field_states[MetadataField.ALBUM] is FieldReadState.MISSING
    assert {issue.code for issue in result.issues} == {MediaErrorCode.TAG_READ_FAILED}
    assert any("title" in issue.message.casefold() for issue in result.issues)
    assert any("track" in issue.message.casefold() for issue in result.issues)


def test_flac_read_wraps_loader_failure_with_typed_issue_and_preserves_cause(
    tmp_path: Path,
) -> None:
    failure = OSError("cannot open media")

    def failing_loader(_: Path) -> FakeFlacFile:
        raise failure

    path = tmp_path / "unreadable.flac"
    adapter = FlacAdapter(loader=failing_loader)

    with pytest.raises(MediaFormatError) as caught:
        adapter.read(path)

    assert caught.value.path == path
    assert caught.value.issue.code is MediaErrorCode.TAG_READ_FAILED
    assert caught.value.__cause__ is failure


def test_flac_writes_only_changed_fields_with_canonical_keys(tmp_path: Path) -> None:
    path = tmp_path / "track.flac"
    audio = FakeFlacFile(
        {
            "TITLE": ["Keep Title"],
            "ALBUM": ["Keep Album"],
            "COMPOSER": ["Old Composer"],
            "TRACKNUMBER": ["1"],
            "TOTALTRACKS": ["8"],
            "UNMANAGED": ["preserve me"],
        }
    )
    adapter = FlacAdapter(loader=lambda _: audio)
    changes = (
        MetadataChange(
            field=MetadataField.COMPOSERS,
            old_value=("Old Composer",),
            new_value=("Composer One", "Composer Two"),
        ),
        MetadataChange(
            field=MetadataField.TRACK,
            old_value=Position(number=1, total=8),
            new_value=Position(number=2, total=10),
        ),
    )

    adapter.write_changes(path, changes)

    assert audio.tags == {
        "TITLE": ["Keep Title"],
        "ALBUM": ["Keep Album"],
        "COMPOSER": ["Composer One", "Composer Two"],
        "TRACKNUMBER": ["2"],
        "TRACKTOTAL": ["10"],
        "UNMANAGED": ["preserve me"],
    }
    assert audio.save_calls == 1


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        (MetadataField.TITLE, 42),
        (MetadataField.COMPOSERS, ("Composer", 42)),
    ],
)
def test_flac_validates_changes_before_removing_existing_values(
    field: MetadataField,
    invalid_value: object,
    tmp_path: Path,
) -> None:
    original_tags: dict[str, list[object]] = {
        "TITLE": ["Original Title"],
        "COMPOSER": ["Original Composer"],
    }
    audio = FakeFlacFile({key: list(value) for key, value in original_tags.items()})
    adapter = FlacAdapter(loader=lambda _: audio)

    with pytest.raises(TypeError):
        adapter.write_changes(
            tmp_path / "track.flac",
            (MetadataChange(field=field, old_value=None, new_value=invalid_value),),
        )

    assert audio.tags == original_tags
    assert audio.save_calls == 0


def test_flac_adds_tag_block_only_when_a_changed_field_requires_it(tmp_path: Path) -> None:
    audio = FakeFlacFile(tags=None)
    adapter = FlacAdapter(loader=lambda _: audio)

    adapter.write_changes(
        tmp_path / "untagged.flac",
        (
            MetadataChange(
                field=MetadataField.TITLE,
                old_value=None,
                new_value="New Title",
            ),
        ),
    )

    assert audio.add_tags_calls == 1
    assert audio.tags == {"TITLE": ["New Title"]}
    assert audio.save_calls == 1


def test_flac_clear_only_change_does_not_create_empty_tag_block(tmp_path: Path) -> None:
    audio = FakeFlacFile(tags=None)
    adapter = FlacAdapter(loader=lambda _: audio)

    adapter.write_changes(
        tmp_path / "untagged.flac",
        (
            MetadataChange(
                field=MetadataField.COMPOSERS,
                old_value=(),
                new_value=(),
            ),
        ),
    )

    assert audio.tags is None
    assert audio.add_tags_calls == 0
    assert audio.save_calls == 0


def test_flac_verify_rejects_unreadable_value_that_looks_like_requested_clear(
    tmp_path: Path,
) -> None:
    audio = FakeFlacFile(tags={"COMPOSER": [object()]})
    adapter = FlacAdapter(loader=lambda _: audio)

    result = adapter.verify(
        tmp_path / "malformed.flac",
        expected=MetadataSnapshot(composers=()),
        changed_fields=frozenset({MetadataField.COMPOSERS}),
        baseline_stream=StreamInfo(
            duration_seconds=185.25,
            sample_rate=44_100,
            channels=2,
            bit_depth=16,
            codec="flac",
        ),
    )

    assert not result.ok
    assert {issue.code for issue in result.issues} == {MediaErrorCode.VERIFICATION_FAILED}
