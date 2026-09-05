from dataclasses import dataclass
from pathlib import Path

import pytest
from mutagen.id3 import COMM, ID3, TCOM, TPOS

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
from metadata_polisher.formats.wave import WaveAdapter


@dataclass
class FakeWaveInfo:
    length: float = 90.25
    sample_rate: int = 96_000
    channels: int = 2
    bits_per_sample: int = 24
    audio_format: int = 1


# This fake isolates shared ID3 delegation and save options. RIFF chunk
# preservation needs the generated real-WAVE fixtures in integration tests.
class FakeWaveFile:
    def __init__(self, tags: ID3 | None) -> None:
        self.tags = tags
        self.info = FakeWaveInfo()
        self.add_tags_calls = 0
        self.save_calls: list[dict[str, object]] = []

    def add_tags(self) -> None:
        self.add_tags_calls += 1
        self.tags = ID3()

    def save(self, **kwargs: object) -> None:
        self.save_calls.append(kwargs)


def test_wave_read_reuses_id3_codec_and_retains_pcm_properties(tmp_path: Path) -> None:
    tags = ID3()
    tags.add(TCOM(encoding=3, text=["Composer One", "Composer Two"]))
    audio = FakeWaveFile(tags)
    adapter = WaveAdapter(loader=lambda _: audio)

    result = adapter.read(tmp_path / "track.wav")

    assert result.metadata.composers == ("Composer One", "Composer Two")
    assert result.field_states[MetadataField.COMPOSERS] is FieldReadState.PRESENT
    assert set(result.field_states) == set(MetadataField)
    assert result.stream_info == StreamInfo(
        duration_seconds=90.25,
        sample_rate=96_000,
        channels=2,
        bit_depth=24,
        codec="wave:1",
    )


def test_wave_write_uses_native_tag_creation_v24_and_preserves_unmanaged_frame(
    tmp_path: Path,
) -> None:
    tags = ID3()
    tags.add(COMM(encoding=3, lang="eng", desc="review", text=["preserve me"]))
    audio = FakeWaveFile(tags)
    adapter = WaveAdapter(loader=lambda _: audio)

    adapter.write_changes(
        tmp_path / "track.wav",
        (
            MetadataChange(
                field=MetadataField.DISC,
                old_value=Position(),
                new_value=Position(number=2, total=3),
            ),
        ),
    )

    assert audio.tags is not None
    disc = audio.tags.get("TPOS")
    assert disc is not None
    assert tuple(str(value) for value in disc.text) == ("2/3",)
    assert tuple(str(value) for value in audio.tags.getall("COMM")[0].text) == ("preserve me",)
    assert audio.add_tags_calls == 0
    assert audio.save_calls == [{"v2_version": 4}]


def test_wave_write_calls_native_add_tags_for_tagless_file(tmp_path: Path) -> None:
    audio = FakeWaveFile(tags=None)
    adapter = WaveAdapter(loader=lambda _: audio)

    adapter.write_changes(
        tmp_path / "untagged.wav",
        (
            MetadataChange(
                field=MetadataField.COMPOSERS,
                old_value=(),
                new_value=("Composer",),
            ),
        ),
    )

    assert audio.add_tags_calls == 1
    assert audio.tags is not None
    assert audio.tags.get("TCOM") is not None
    assert audio.save_calls == [{"v2_version": 4}]


def test_wave_clear_only_change_does_not_create_empty_tag_block(tmp_path: Path) -> None:
    audio = FakeWaveFile(tags=None)
    adapter = WaveAdapter(loader=lambda _: audio)

    adapter.write_changes(
        tmp_path / "untagged.wav",
        (
            MetadataChange(
                field=MetadataField.DISC,
                old_value=Position(),
                new_value=Position(),
            ),
        ),
    )

    assert audio.tags is None
    assert audio.add_tags_calls == 0
    assert audio.save_calls == []


def test_wave_write_wraps_save_failure_with_typed_issue_and_preserves_cause(
    tmp_path: Path,
) -> None:
    failure = OSError("disk write failed")

    class SaveFailingWaveFile(FakeWaveFile):
        def save(self, **kwargs: object) -> None:
            raise failure

    audio = SaveFailingWaveFile(tags=ID3())
    path = tmp_path / "track.wav"
    adapter = WaveAdapter(loader=lambda _: audio)

    with pytest.raises(MediaFormatError) as caught:
        adapter.write_changes(
            path,
            (
                MetadataChange(
                    field=MetadataField.COMPOSERS,
                    old_value=(),
                    new_value=("Composer",),
                ),
            ),
        )

    assert caught.value.path == path
    assert caught.value.issue.code is MediaErrorCode.TAG_WRITE_FAILED
    assert caught.value.__cause__ is failure


def test_wave_verify_rejects_unreadable_value_that_looks_like_clear(tmp_path: Path) -> None:
    tags = ID3()
    tags.add(TPOS(encoding=3, text=["broken-position"]))
    audio = FakeWaveFile(tags)
    adapter = WaveAdapter(loader=lambda _: audio)

    result = adapter.verify(
        tmp_path / "track.wav",
        expected=MetadataSnapshot(disc=Position()),
        changed_fields=frozenset({MetadataField.DISC}),
        baseline_stream=StreamInfo(
            duration_seconds=90.25,
            sample_rate=96_000,
            channels=2,
            bit_depth=24,
            codec="wave:1",
        ),
    )

    assert not result.ok
    assert {issue.code for issue in result.issues} == {MediaErrorCode.VERIFICATION_FAILED}
