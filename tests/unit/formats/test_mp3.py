from dataclasses import dataclass
from pathlib import Path

import pytest
from mutagen.id3 import COMM, ID3, TIT2, TRCK

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
from metadata_polisher.formats.mp3 import Mp3Adapter


@dataclass
class FakeMp3Info:
    length: float = 201.5
    sample_rate: int = 48_000
    channels: int = 2


# Record native tag creation and save options while reusing genuine ID3
# objects; these fixtures cover adapter orchestration without MPEG decoding.
class FakeMp3File:
    def __init__(self, tags: ID3 | None) -> None:
        self.tags = tags
        self.info = FakeMp3Info()
        self.add_tags_calls = 0
        self.save_calls: list[dict[str, object]] = []

    def add_tags(self) -> None:
        self.add_tags_calls += 1
        self.tags = ID3()

    def save(self, **kwargs: object) -> None:
        self.save_calls.append(kwargs)


def test_mp3_read_composes_id3_metadata_and_stream_information(tmp_path: Path) -> None:
    tags = ID3()
    tags.add(TIT2(encoding=3, text=["Track Title"]))
    tags.add(TRCK(encoding=3, text=["2/11"]))
    audio = FakeMp3File(tags)
    adapter = Mp3Adapter(loader=lambda _: audio)

    result = adapter.read(tmp_path / "track.mp3")

    assert result.metadata.title == "Track Title"
    assert result.metadata.track == Position(number=2, total=11)
    assert result.field_states[MetadataField.TITLE] is FieldReadState.PRESENT
    assert result.field_states[MetadataField.COMPOSERS] is FieldReadState.MISSING
    assert set(result.field_states) == set(MetadataField)
    assert result.stream_info == StreamInfo(
        duration_seconds=201.5,
        sample_rate=48_000,
        channels=2,
        bit_depth=None,
        codec="mp3",
    )


def test_mp3_tagless_read_does_not_create_or_write_tags(tmp_path: Path) -> None:
    audio = FakeMp3File(tags=None)
    adapter = Mp3Adapter(loader=lambda _: audio)

    result = adapter.read(tmp_path / "untagged.mp3")

    assert all(state is FieldReadState.MISSING for state in result.field_states.values())
    assert audio.tags is None
    assert audio.add_tags_calls == 0
    assert audio.save_calls == []


def test_mp3_read_wraps_loader_failure_with_typed_issue_and_preserves_cause(
    tmp_path: Path,
) -> None:
    failure = OSError("cannot open media")

    def failing_loader(_: Path) -> FakeMp3File:
        raise failure

    path = tmp_path / "unreadable.mp3"
    adapter = Mp3Adapter(loader=failing_loader)

    with pytest.raises(MediaFormatError) as caught:
        adapter.read(path)

    assert caught.value.path == path
    assert caught.value.issue.code is MediaErrorCode.TAG_READ_FAILED
    assert caught.value.__cause__ is failure


def test_mp3_write_uses_native_tag_creation_v24_and_preserves_unmanaged_frame(
    tmp_path: Path,
) -> None:
    tags = ID3()
    tags.add(COMM(encoding=3, lang="eng", desc="review", text=["preserve me"]))
    audio = FakeMp3File(tags)
    adapter = Mp3Adapter(loader=lambda _: audio)

    adapter.write_changes(
        tmp_path / "track.mp3",
        (
            MetadataChange(
                field=MetadataField.TITLE,
                old_value=None,
                new_value="New Title",
            ),
        ),
    )

    assert audio.tags is not None
    title = audio.tags.get("TIT2")
    assert title is not None
    assert tuple(str(value) for value in title.text) == ("New Title",)
    assert tuple(str(value) for value in audio.tags.getall("COMM")[0].text) == ("preserve me",)
    assert audio.add_tags_calls == 0
    assert audio.save_calls == [{"v2_version": 4}]


def test_mp3_write_calls_native_add_tags_for_tagless_file(tmp_path: Path) -> None:
    audio = FakeMp3File(tags=None)
    adapter = Mp3Adapter(loader=lambda _: audio)

    adapter.write_changes(
        tmp_path / "untagged.mp3",
        (
            MetadataChange(
                field=MetadataField.TITLE,
                old_value=None,
                new_value="New Title",
            ),
        ),
    )

    assert audio.add_tags_calls == 1
    assert audio.tags is not None
    assert audio.tags.get("TIT2") is not None
    assert audio.save_calls == [{"v2_version": 4}]


def test_mp3_clear_only_change_does_not_create_empty_tag_block(tmp_path: Path) -> None:
    audio = FakeMp3File(tags=None)
    adapter = Mp3Adapter(loader=lambda _: audio)

    adapter.write_changes(
        tmp_path / "untagged.mp3",
        (
            MetadataChange(
                field=MetadataField.TITLE,
                old_value=None,
                new_value=None,
            ),
        ),
    )

    assert audio.tags is None
    assert audio.add_tags_calls == 0
    assert audio.save_calls == []


def test_mp3_write_wraps_save_failure_with_typed_issue_and_preserves_cause(
    tmp_path: Path,
) -> None:
    failure = OSError("disk write failed")

    class SaveFailingMp3File(FakeMp3File):
        def save(self, **kwargs: object) -> None:
            raise failure

    audio = SaveFailingMp3File(tags=ID3())
    path = tmp_path / "track.mp3"
    adapter = Mp3Adapter(loader=lambda _: audio)

    with pytest.raises(MediaFormatError) as caught:
        adapter.write_changes(
            path,
            (
                MetadataChange(
                    field=MetadataField.TITLE,
                    old_value=None,
                    new_value="New Title",
                ),
            ),
        )

    assert caught.value.path == path
    assert caught.value.issue.code is MediaErrorCode.TAG_WRITE_FAILED
    assert caught.value.__cause__ is failure


# A malformed tag may decode to the same empty value as a requested clear.
# Verification must reject its read state as well as changed stream facts.
def test_mp3_verify_rejects_unreadable_clear_and_changed_stream(tmp_path: Path) -> None:
    tags = ID3()
    tags.add(TRCK(encoding=3, text=["broken-position"]))
    audio = FakeMp3File(tags)
    adapter = Mp3Adapter(loader=lambda _: audio)

    result = adapter.verify(
        tmp_path / "track.mp3",
        expected=MetadataSnapshot(track=Position()),
        changed_fields=frozenset({MetadataField.TRACK}),
        baseline_stream=StreamInfo(
            duration_seconds=999.0,
            sample_rate=48_000,
            channels=2,
            bit_depth=None,
            codec="mp3",
        ),
    )

    assert not result.ok
    assert len(result.issues) == 2
    assert {issue.code for issue in result.issues} == {MediaErrorCode.VERIFICATION_FAILED}
