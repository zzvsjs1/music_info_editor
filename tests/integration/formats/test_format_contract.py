"""Real WAV serialisation/reopening without an encoder or redistributed audio."""

import struct
import wave
from pathlib import Path

import pytest
from mutagen.id3 import APIC, COMM
from mutagen.wave import WAVE

from metadata_polisher.formats.wave import WaveAdapter
from tests.support.format_contract import assert_format_contract, make_test_png


def make_wave_fixture(path: Path) -> None:
    """Generate a short PCM waveform and one legal, unmanaged RIFF chunk."""
    # Generate stereo 16-bit PCM locally: the byte pattern can be compared
    # exactly after tag edits without redistributing a recording or using an encoder.
    samples = b"".join(struct.pack("<hh", index % 100 - 50, 50 - index % 100) for index in range(800))

    with wave.open(str(path), "wb") as output:
        output.setnchannels(2)
        output.setsampwidth(2)
        output.setframerate(8_000)
        output.writeframes(samples)

    sentinel = b"Metadata Polisher unmanaged RIFF sentinel"
    content = path.read_bytes()
    junk_chunk = b"JUNK" + struct.pack("<I", len(sentinel)) + sentinel + (b"\x00" if len(sentinel) % 2 else b"")
    content += junk_chunk
    path.write_bytes(content[:4] + struct.pack("<I", len(content) - 8) + content[8:])


def riff_chunks(path: Path) -> dict[bytes, list[bytes]]:
    """Read exact chunk payloads independently of Mutagen's tag reader."""
    content = path.read_bytes()
    assert content[:4] == b"RIFF"
    assert content[8:12] == b"WAVE"
    assert struct.unpack_from("<I", content, 4)[0] + 8 == len(content)
    position = 12
    chunks: dict[bytes, list[bytes]] = {}

    while position + 8 <= len(content):
        kind = content[position:position + 4]
        size = struct.unpack_from("<I", content, position + 4)[0]
        assert position + 8 + size <= len(content)
        chunks.setdefault(kind, []).append(content[position + 8:position + 8 + size])
        # Each chunk has an eight-byte header and optional even-byte padding;
        # payload equality excludes those structural bytes from the comparison.
        position += 8 + size + size % 2

    return chunks


def test_generated_wave_round_trips_managed_metadata_and_preserves_pcm_and_riff_chunks(tmp_path: Path) -> None:
    path = tmp_path / "generated.wav"
    make_wave_fixture(path)
    before = riff_chunks(path)

    assert_format_contract(path, WaveAdapter())

    after = riff_chunks(path)
    assert after[b"data"] == before[b"data"]
    assert after[b"fmt "] == before[b"fmt "]
    assert after[b"JUNK"] == before[b"JUNK"]
    assert any(key.lower() == b"id3 " for key in after)


def test_existing_wave_artwork_and_comment_survive_the_shared_write_and_clear_contract(tmp_path: Path) -> None:
    path = tmp_path / "with-artwork.wav"
    make_wave_fixture(path)
    native = WAVE(path)
    native.add_tags()
    native.tags.add(APIC(encoding=3, mime="image/png", type=3, desc="Existing artwork", data=make_test_png()))
    native.tags.add(COMM(encoding=3, lang="eng", desc="User note", text=["Keep my note – 保持"]))
    native.save(v2_version=4)

    assert_format_contract(path, WaveAdapter())

    reopened = WAVE(path)
    existing_artwork = next(frame for frame in reopened.tags.getall("APIC") if frame.desc == "Existing artwork")
    assert existing_artwork.data == make_test_png()
    assert tuple(str(value) for value in reopened.tags.getall("COMM:User note:eng")[0].text) == (
        "Keep my note – 保持",
    )


def test_shared_contract_rejects_protected_paths_before_opening_any_media(tmp_path: Path, monkeypatch) -> None:
    def unexpected_probe(*_args):
        pytest.fail("Protected source paths must be rejected before opening media")

    monkeypatch.setattr(WaveAdapter, "can_handle", unexpected_probe)

    with pytest.raises(ValueError, match="temporary copy"):
        assert_format_contract(tmp_path / "musics" / "source.wav", WaveAdapter())
