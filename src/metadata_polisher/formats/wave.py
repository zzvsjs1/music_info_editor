"""RIFF/WAVE container adapter reusing the shared ID3 semantic codec."""

from collections.abc import Callable
from pathlib import Path
from typing import Protocol, cast

from mutagen.id3 import ID3, ID3Tags
from mutagen.wave import WAVE

from metadata_polisher.domain.errors import Issue, MediaErrorCode
from metadata_polisher.domain.media import MediaReadResult, StreamInfo
from metadata_polisher.domain.metadata import MetadataChange, MetadataField, MetadataSnapshot
from metadata_polisher.formats.base import MediaFormatError, VerificationResult
from metadata_polisher.formats.id3_codec import Id3TagCodec
from metadata_polisher.formats.id3_policy import snapshot_id3, validate_loaded_frames, verify_preserved_frames


class _WaveInfo(Protocol):
    length: float
    sample_rate: int
    channels: int
    bits_per_sample: int
    audio_format: int


class _WaveFile(Protocol):
    tags: ID3Tags | None
    info: _WaveInfo

    def add_tags(self) -> None: ...

    def save(self, **kwargs: object) -> None: ...


type WaveLoader = Callable[[Path], _WaveFile]


def _load_wave(path: Path) -> _WaveFile:
    return cast(_WaveFile, WAVE(path, translate=False, load_v1=False))  # type: ignore[no-untyped-call]


def _empty_id3() -> ID3Tags:
    return cast(ID3Tags, ID3())  # type: ignore[no-untyped-call]


def _stream_info(audio: _WaveFile) -> StreamInfo:
    return StreamInfo(
        duration_seconds=audio.info.length,
        sample_rate=audio.info.sample_rate,
        channels=audio.info.channels,
        bit_depth=audio.info.bits_per_sample,
        # RIFF's audio-format code distinguishes, for example, integer PCM from
        # IEEE float while keeping the domain independent of Mutagen classes.
        codec=f"wave:{audio.info.audio_format}",
    )


def _additional_metadata_issues(path: Path) -> tuple[Issue, ...]:
    """Surface other real metadata systems without importing or synchronising them."""
    if not path.is_file():
        return ()

    systems: list[str] = []

    with path.open("rb") as source:
        header = source.read(12)

        if header[:4] != b"RIFF" or header[8:] != b"WAVE":
            return ()

        end = min(path.stat().st_size, int.from_bytes(header[4:8], "little") + 8)

        while source.tell() + 8 <= end:
            chunk = source.read(8)
            size = int.from_bytes(chunk[4:], "little")
            payload_start = source.tell()

            if size > end - payload_start:
                break

            if chunk[:4] == b"bext":
                systems.append("Broadcast Wave (bext)")
            elif chunk[:4] == b"LIST" and size >= 4 and source.read(4) == b"INFO":
                systems.append("RIFF INFO")

            # Seek from the payload start because inspecting LIST consumed four
            # bytes; also account for RIFF padding after odd-length chunks.
            source.seek(payload_start + size + size % 2)

    return tuple(Issue(
        code=MediaErrorCode.ADDITIONAL_METADATA,
        message=(f"Additional {system} metadata is preserved and may differ from the reviewed ID3 fields. "
                 "This application does not synchronise that metadata system."),
    ) for system in dict.fromkeys(systems))


class WaveAdapter:
    """Read, write, and verify managed ID3 metadata in RIFF/WAVE files."""

    format_id = "wave"
    extensions = frozenset({".wav"})

    def __init__(self, loader: WaveLoader = _load_wave, codec: Id3TagCodec | None = None) -> None:
        self._loader = loader
        self._codec = codec or Id3TagCodec()

    def can_handle(self, path: Path) -> bool:
        self._loader(path)
        return True

    def read(self, path: Path) -> MediaReadResult:
        try:
            audio = self._loader(path)
        except Exception as error:
            raise MediaFormatError.from_cause(
                path=path,
                code=MediaErrorCode.TAG_READ_FAILED,
                message="Could not read WAVE metadata.",
                cause=error,
            ) from error

        # WAVE.add_tags() creates Mutagen's container-aware _WaveID3 object, so a
        # temporary plain ID3 is used only for non-mutating reads of tagless files.
        tags = audio.tags if audio.tags is not None else _empty_id3()
        decoded = self._codec.read(tags)

        return MediaReadResult(
            metadata=decoded.metadata,
            field_states=decoded.field_states,
            stream_info=_stream_info(audio),
            issues=decoded.issues + _additional_metadata_issues(path),
        )

    def write_changes(self, path: Path, changes: tuple[MetadataChange, ...]) -> None:
        if not changes:
            return

        needs_tag_block = self._codec.changes_require_tag_block(changes)
        try:
            audio = self._loader(path)
        except Exception as error:
            raise MediaFormatError.from_cause(
                path=path,
                code=MediaErrorCode.TAG_WRITE_FAILED,
                message="Could not open the WAVE file for metadata writing.",
                cause=error,
            ) from error

        if audio.tags is None:
            if not needs_tag_block:
                return

            # Never assign ID3() directly here: Mutagen's WAVE implementation must
            # create its private RIFF-aware tag object for correct on-disk writing.
            try:
                audio.add_tags()
            except Exception as error:
                raise MediaFormatError.from_cause(
                    path=path,
                    code=MediaErrorCode.TAG_WRITE_FAILED,
                    message="Could not create a WAVE ID3 tag block.",
                    cause=error,
                ) from error

        if audio.tags is None:
            raise MediaFormatError(
                path=path,
                issue=Issue(
                    code=MediaErrorCode.TAG_WRITE_FAILED,
                    message="WAVE ID3 tag block creation did not produce a writable tag object.",
                ),
            )

        try:
            # Locate ID3 inside the RIFF chunks before comparing raw frames.
            # Only this metadata system participates in the reviewed edit.
            original = snapshot_id3(path, wave=True)
            validate_loaded_frames(original, audio.tags)
            version = self._codec.validate_preserved_version(audio.tags, changes)
        except (OSError, ValueError) as error:
            raise MediaFormatError.from_cause(
                path=path, code=MediaErrorCode.TAG_WRITE_FAILED,
                message=f"Cannot safely edit preserved ID3 metadata: {error}", cause=error,
            ) from error

        self._codec.write_changes(audio.tags, changes)

        try:
            audio.save(v2_version=version)
        except Exception as error:
            raise MediaFormatError.from_cause(
                path=path,
                code=MediaErrorCode.TAG_WRITE_FAILED,
                message="Could not save WAVE metadata.",
                cause=error,
            ) from error

        try:
            verify_preserved_frames(original, snapshot_id3(path, wave=True), changes)
        except (OSError, ValueError) as error:
            raise MediaFormatError.from_cause(
                path=path, code=MediaErrorCode.VERIFICATION_FAILED,
                message=f"Could not preserve unchanged ID3 frame data: {error}", cause=error,
            ) from error

    def verify(
        self,
        path: Path,
        expected: MetadataSnapshot,
        changed_fields: frozenset[MetadataField],
        baseline_stream: StreamInfo,
    ) -> VerificationResult:
        audio = self._loader(path)
        tags = audio.tags if audio.tags is not None else _empty_id3()
        metadata_verification = self._codec.verify(tags, expected, changed_fields)
        issues = list(metadata_verification.issues)
        # Metadata verification and the stable audio-format check are separate:
        # correct tags alone do not show that the audio format is unchanged.
        actual_stream = _stream_info(audio)

        if actual_stream != baseline_stream:
            issues.append(
                Issue(
                    code=MediaErrorCode.VERIFICATION_FAILED,
                    message="Stable WAVE stream properties changed during metadata writing.",
                )
            )

        return VerificationResult(ok=not issues, issues=tuple(issues))
