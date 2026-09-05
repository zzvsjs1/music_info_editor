"""Real containers and independent frame bytes verify the preserved ID3 policy."""

import os
import shutil
import struct
from pathlib import Path

import pytest
from mutagen.id3 import APIC, COMM, TALB, TCOM, TDAT, TDOR, TDRC, TDRL, TIME, TIT2, TORY, TPE1, TXXX, TYER
from mutagen.mp3 import MP3
from mutagen.wave import WAVE

from metadata_polisher.domain.metadata import MetadataChange, MetadataField, Position
from metadata_polisher.formats.base import MediaFormatError
from metadata_polisher.formats.mp3 import Mp3Adapter
from metadata_polisher.formats.wave import WaveAdapter
from tests.integration.formats.test_format_contract import make_wave_fixture, riff_chunks
from tests.support.format_contract import make_test_png


def _synchsafe(value: int) -> bytes:
    return bytes((value >> 21 & 127, value >> 14 & 127, value >> 7 & 127, value & 127))


def _read_synchsafe(value: bytes) -> int:
    assert len(value) == 4 and all(byte < 128 for byte in value)
    return sum(byte << shift for byte, shift in zip(value, (21, 14, 7, 0), strict=True))


def id3_bytes(path: Path) -> bytes:
    if path.suffix == ".wav":
        chunks = riff_chunks(path)
        return next((values[0] for key, values in chunks.items() if key.lower() == b"id3 "), b"")

    content = path.read_bytes()
    return content[:10 + _read_synchsafe(content[6:10])] if content.startswith(b"ID3") else b""


def raw_frames(path: Path) -> tuple[int, dict[bytes, bytes]]:
    """Read frame sizes/encodings directly; do not use Mutagen's semantic reader."""
    block = id3_bytes(path)
    assert block[:3] == b"ID3" and block[5] == 0
    version = block[3]
    offset = 10
    result = {}

    while offset + 10 <= len(block) and block[offset] != 0:
        identifier = block[offset:offset + 4]
        raw_size = block[offset + 4:offset + 8]
        # Interpret raw sizes independently: v2.4 packs seven bits per size
        # byte, while v2.3 stores a normal four-byte big-endian frame length.
        size = _read_synchsafe(raw_size) if version == 4 else int.from_bytes(raw_size, "big")
        result[identifier] = block[offset + 8:offset + 10 + size]
        offset += 10 + size

    return version, result


def make_encoded_fixture(tmp_path: Path, suffix: str) -> Path:
    source = tmp_path / "generated.wav"
    make_wave_fixture(source)

    if suffix == ".wav":
        return source

    configured = os.environ.get(f"METADATA_POLISHER_TEST_{'MP3' if suffix == '.mp3' else 'MP4'}_COPY")

    if configured is None:
        pytest.skip("No explicitly configured disposable encoded-media copy was supplied.")

    destination = tmp_path / f"generated{suffix}"
    shutil.copy2(Path(configured), destination)

    if suffix == ".mp3":
        # Clear tags only on this second disposable copy, providing a controlled
        # tagless baseline without redistributing private compressed audio.
        MP3(destination).delete()

    return destination


def seed_id3(path: Path, version: int) -> None:
    audio = WAVE(path) if path.suffix == ".wav" else MP3(path)

    if audio.tags is None:
        audio.add_tags()

    # Seed each version with its supported Unicode encoding, plus unmanaged
    # comments/artwork and date frames that a Title-only edit must preserve.
    encoding = 1 if version == 3 else 3
    audio.tags.add(TIT2(encoding=encoding, text=["Original title"]))
    audio.tags.add(TALB(encoding=encoding, text=["Album – アルバム"]))
    audio.tags.add(TPE1(encoding=encoding, text=["AC/DC & Artist; Name"]))
    audio.tags.add(COMM(encoding=encoding, lang="eng", desc="note", text=["User comment – 保持"]))
    audio.tags.add(TXXX(encoding=encoding, desc="UNMANAGED", text=["Keep exactly"]))
    audio.tags.add(APIC(encoding=encoding, mime="image/png", type=3, desc="cover", data=make_test_png()))

    if version == 3:
        audio.tags.add(TYER(encoding=0, text=["2024"]))
        audio.tags.add(TDAT(encoding=0, text=["2902"]))
        audio.tags.add(TIME(encoding=0, text=["1234"]))
        audio.tags.add(TORY(encoding=0, text=["1999"]))
    else:
        audio.tags.add(TDRC(encoding=3, text=["2024-02"]))
        audio.tags.add(TDRL(encoding=3, text=["2025-01"]))
        audio.tags.add(TDOR(encoding=3, text=["1999"]))

    audio.save(v2_version=version)


@pytest.mark.parametrize("suffix", [".wav", ".mp3"])
@pytest.mark.parametrize("version", [3, 4])
def test_title_edit_preserves_supported_version_and_every_unmanaged_frame(tmp_path, suffix, version) -> None:
    path = make_encoded_fixture(tmp_path, suffix)
    seed_id3(path, version)
    before_version, before_frames = raw_frames(path)
    before_chunks = riff_chunks(path) if suffix == ".wav" else None
    adapter = WaveAdapter() if suffix == ".wav" else Mp3Adapter()
    adapter.write_changes(path, (MetadataChange(MetadataField.TITLE, "Original title", "Revised – 改訂 🎵"),))
    after_version, after_frames = raw_frames(path)

    assert before_version == after_version == version
    # Exclude only the edited Title frame. Comparing every other raw payload
    # detects silent conversion that a semantic read/write round trip could miss.
    preserved = {key: value for key, value in after_frames.items() if key != b"TIT2"} == {
        key: value for key, value in before_frames.items() if key != b"TIT2"
    }
    assert preserved, "An unmanaged frame payload changed"
    assert after_frames[b"TIT2"][2] == (1 if version == 3 else 3)
    assert adapter.read(path).metadata.title == "Revised – 改訂 🎵"

    if before_chunks is not None:
        after_chunks = riff_chunks(path)
        assert after_chunks[b"data"] == before_chunks[b"data"]
        assert after_chunks[b"JUNK"] == before_chunks[b"JUNK"]


@pytest.mark.parametrize("suffix", [".wav", ".mp3"])
@pytest.mark.parametrize("version", [3, 4])
def test_noop_and_rename_only_never_rewrite_existing_id3(tmp_path, suffix, version) -> None:
    path = make_encoded_fixture(tmp_path, suffix)
    seed_id3(path, version)
    before = path.read_bytes()
    adapter = WaveAdapter() if suffix == ".wav" else Mp3Adapter()
    adapter.write_changes(path, ())
    destination = path.with_name(f"renamed{suffix}")
    path.rename(destination)

    unchanged = destination.read_bytes() == before
    assert unchanged, "No-op/rename-only file bytes changed"


@pytest.mark.parametrize("suffix", [".wav", ".mp3"])
def test_genuinely_new_tags_use_v24_and_native_null_separated_multivalues(tmp_path, suffix) -> None:
    path = make_encoded_fixture(tmp_path, suffix)
    assert id3_bytes(path) == b""
    adapter = WaveAdapter() if suffix == ".wav" else Mp3Adapter()
    adapter.write_changes(path, (MetadataChange(MetadataField.COMPOSERS, (), ("One/Name", "二人目")),))
    version, frames = raw_frames(path)

    assert version == 4
    assert frames[b"TCOM"][2:] == b"\x03One/Name\x00" + "二人目".encode() + b"\x00"
    assert adapter.read(path).metadata.composers == ("One/Name", "二人目")


@pytest.mark.parametrize("field,value", [
    (MetadataField.COMPOSERS, ("One", "Two")),
    (MetadataField.DATE, "2024-02"),
    (MetadataField.TRACK, Position(total=12)),
])
def test_unrepresentable_v23_edits_are_explicitly_blocked_before_any_write(tmp_path, field, value) -> None:
    path = make_encoded_fixture(tmp_path, ".wav")
    seed_id3(path, 3)
    before = path.read_bytes()

    with pytest.raises(MediaFormatError, match="(?i)(ID3|represent|supported)"):
        WaveAdapter().write_changes(path, (MetadataChange(field, None, value),))

    assert path.read_bytes() == before


@pytest.mark.parametrize("value,expected", [("2025", {b"TYER": b"2025"}),
                                            ("2025-06-07", {b"TYER": b"2025", b"TDAT": b"0706"})])
def test_v23_date_edit_uses_native_year_and_day_month_without_changing_original_year(tmp_path, value, expected) -> None:
    path = make_encoded_fixture(tmp_path, ".wav")
    seed_id3(path, 3)
    _, before = raw_frames(path)
    WaveAdapter().write_changes(path, (MetadataChange(MetadataField.DATE, "2024-02-29T12:34", value),))
    version, frames = raw_frames(path)

    assert version == 3 and b"TDRC" not in frames and b"TIME" not in frames
    assert frames[b"TORY"] == before[b"TORY"]

    for key, text in expected.items():
        assert frames[key][2:] == b"\x00" + text + b"\x00"

    assert WaveAdapter().read(path).metadata.date == value


def test_v22_edit_is_blocked_instead_of_silently_upgraded(tmp_path) -> None:
    path = make_encoded_fixture(tmp_path, ".wav")
    frame = b"TT2" + (4).to_bytes(3, "big") + b"\x00Old"
    block = b"ID3\x02\x00\x00" + _synchsafe(len(frame)) + frame
    content = path.read_bytes() + b"id3 " + struct.pack("<I", len(block)) + block
    path.write_bytes(content[:4] + struct.pack("<I", len(content) - 8) + content[8:])
    before = path.read_bytes()

    with pytest.raises(MediaFormatError, match="(?i)(ID3v2.2|supported)"):
        WaveAdapter().write_changes(path, (MetadataChange(MetadataField.TITLE, "Old", "New"),))

    assert path.read_bytes() == before


def test_v23_nonstandard_existing_multivalue_is_not_flattened_by_unrelated_title_edit(tmp_path) -> None:
    path = make_encoded_fixture(tmp_path, ".wav")
    seed_id3(path, 3)
    native = WAVE(path, translate=False)
    native.tags.add(TCOM(encoding=1, text=["First", "Second"]))
    native.save(v2_version=3, v23_sep=None)
    before = path.read_bytes()

    with pytest.raises(MediaFormatError, match="(?i)(multiple|multi.value|represent)"):
        WaveAdapter().write_changes(path, (MetadataChange(MetadataField.TITLE, "Original title", "New"),))

    assert path.read_bytes() == before


def test_mp3_id3v1_payload_is_retained_without_being_imported_into_new_id3v2_fields(tmp_path) -> None:
    path = make_encoded_fixture(tmp_path, ".mp3")
    seed_id3(path, 3)
    tail = b"TAG" + b"Legacy title".ljust(30, b"\x00") + b"Legacy artist".ljust(30, b"\x00")
    tail += b"Legacy album".ljust(30, b"\x00") + b"1998" + b"Legacy note".ljust(30, b"\x00") + b"\x0c"
    assert len(tail) == 128
    path.write_bytes(path.read_bytes() + tail)
    Mp3Adapter().write_changes(path, (MetadataChange(MetadataField.TITLE, "Original title", "New"),))

    assert path.read_bytes()[-128:] == tail
    assert b"TCON" not in raw_frames(path)[1]


@pytest.mark.parametrize("kind,payload,label", [
    (b"LIST", b"INFOINAM\x0c\x00\x00\x00Other title\x00", "INFO"),
    (b"bext", b"Broadcast description".ljust(602, b"\x00"), "Broadcast"),
], ids=["info", "broadcast"])
def test_additional_wave_metadata_is_surfaced_and_preserved_without_synchronising(
    tmp_path, kind, payload, label,
) -> None:
    path = make_encoded_fixture(tmp_path, ".wav")
    seed_id3(path, 4)
    content = path.read_bytes() + kind + struct.pack("<I", len(payload)) + payload
    content += b"\x00" if len(payload) % 2 else b""
    path.write_bytes(content[:4] + struct.pack("<I", len(content) - 8) + content[8:])
    adapter = WaveAdapter()
    result = adapter.read(path)

    assert result.metadata.title == "Original title"
    assert any(issue.code.value == "ADDITIONAL_METADATA" and label in issue.message for issue in result.issues)
    adapter.write_changes(path, (MetadataChange(MetadataField.TITLE, "Original title", "Reviewed ID3 title"),))
    assert riff_chunks(path)[kind] == [payload]


def test_unmanaged_frame_preservation_failure_has_a_precise_user_facing_reason(tmp_path) -> None:
    class UnexpectedNormalisingWave(WAVE):
        def save(self, **kwargs):
            self.tags["TALB"].text = ["Unexpected normalisation"]
            super().save(**kwargs)

    path = make_encoded_fixture(tmp_path, ".wav")
    seed_id3(path, 4)
    adapter = WaveAdapter(loader=lambda item: UnexpectedNormalisingWave(item, translate=False, load_v1=False))

    with pytest.raises(MediaFormatError) as caught:
        adapter.write_changes(path, (MetadataChange(MetadataField.TITLE, "Original title", "Reviewed title"),))

    assert "unchanged ID3 frame" in caught.value.issue.message


@pytest.mark.parametrize("version", [3, 4])
def test_generated_mpeg_frame_container_exercises_mp3_id3_without_an_encoder(tmp_path, version) -> None:
    # MPEG-1 Layer III, 128 kbit/s, 44.1 kHz, mono, no CRC. Each frame occupies
    # floor(144 * 128000 / 44100) = 417 bytes. Zero side information requests no
    # coded spectral values; the remainder is ancillary padding. This fixture
    # verifies container/tag handling, not an independent audio decoder/player.
    audio_payload = (b"\xff\xfb\x90\xc0" + bytes(413)) * 6
    path = tmp_path / "synthetic-frames.mp3"
    path.write_bytes(audio_payload)
    seed_id3(path, version)
    _, before = raw_frames(path)
    adapter = Mp3Adapter()
    adapter.write_changes(path, (MetadataChange(MetadataField.TITLE, "Original title", "Updated – 更新"),))
    after_version, after = raw_frames(path)

    assert after_version == version
    preserved = {key: value for key, value in after.items() if key != b"TIT2"} == {
        key: value for key, value in before.items() if key != b"TIT2"
    }
    assert preserved
    assert adapter.read(path).metadata.title == "Updated – 更新"
    assert adapter.read(path).stream_info.sample_rate == 44_100
    unchanged_audio = path.read_bytes()[len(id3_bytes(path)):] == audio_payload
    assert unchanged_audio
