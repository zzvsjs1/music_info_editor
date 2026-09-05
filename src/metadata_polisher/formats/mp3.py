"""MP3 container adapter using the shared ID3 semantic codec."""

from collections.abc import Callable
from pathlib import Path
from typing import Protocol, cast

from mutagen.id3 import ID3, ID3Tags
from mutagen.mp3 import MP3

from metadata_polisher.domain.errors import Issue, MediaErrorCode
from metadata_polisher.domain.media import MediaReadResult, StreamInfo
from metadata_polisher.domain.metadata import MetadataChange, MetadataField, MetadataSnapshot
from metadata_polisher.formats.base import MediaFormatError, VerificationResult
from metadata_polisher.formats.id3_codec import Id3TagCodec
from metadata_polisher.formats.id3_policy import (
    read_id3v1_tail,
    restore_id3v1_tail,
    snapshot_id3,
    validate_loaded_frames,
    verify_preserved_frames,
)


class _Mp3Info(Protocol):
    length: float
    sample_rate: int
    channels: int


class _Mp3File(Protocol):
    tags: ID3Tags | None
    info: _Mp3Info

    def add_tags(self) -> None: ...

    def save(self, **kwargs: object) -> None: ...


type Mp3Loader = Callable[[Path], _Mp3File]


def _load_mp3(path: Path) -> _Mp3File:
    # Suppress automatic version translation and ID3v1 promotion so reading
    # cannot hide the original representation that the preservation checks need.
    return cast(_Mp3File, MP3(path, translate=False, load_v1=False))  # type: ignore[no-untyped-call]


def _empty_id3() -> ID3Tags:
    return cast(ID3Tags, ID3())  # type: ignore[no-untyped-call]


def _stream_info(audio: _Mp3File) -> StreamInfo:
    return StreamInfo(
        duration_seconds=audio.info.length,
        sample_rate=audio.info.sample_rate,
        channels=audio.info.channels,
        bit_depth=None,
        codec="mp3",
    )


class Mp3Adapter:
    """Read, write, and verify managed ID3v2 metadata in MP3 files."""

    format_id = "mp3"
    extensions = frozenset({".mp3"})

    def __init__(self, loader: Mp3Loader = _load_mp3, codec: Id3TagCodec | None = None) -> None:
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
                message="Could not read MP3 metadata.",
                cause=error,
            ) from error

        # Reading must never mutate a tagless user file merely to obtain a complete
        # set of MISSING field states.
        tags = audio.tags if audio.tags is not None else _empty_id3()
        decoded = self._codec.read(tags)

        return MediaReadResult(
            metadata=decoded.metadata,
            field_states=decoded.field_states,
            stream_info=_stream_info(audio),
            issues=decoded.issues,
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
                message="Could not open the MP3 file for metadata writing.",
                cause=error,
            ) from error

        if audio.tags is None:
            if not needs_tag_block:
                return

            try:
                audio.add_tags()
            except Exception as error:
                raise MediaFormatError.from_cause(
                    path=path,
                    code=MediaErrorCode.TAG_WRITE_FAILED,
                    message="Could not create an MP3 ID3 tag block.",
                    cause=error,
                ) from error

        if audio.tags is None:
            raise MediaFormatError(
                path=path,
                issue=Issue(
                    code=MediaErrorCode.TAG_WRITE_FAILED,
                    message="MP3 ID3 tag block creation did not produce a writable tag object.",
                ),
            )

        try:
            # Compare the raw tag with the loaded object before editing. This
            # catches frames the library silently discarded during parsing.
            original = snapshot_id3(path)
            validate_loaded_frames(original, audio.tags)
            version = self._codec.validate_preserved_version(audio.tags, changes)
            original_v1 = read_id3v1_tail(path)
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
                message="Could not save MP3 metadata.",
                cause=error,
            ) from error

        try:
            # Mutagen may rewrite the legacy tail while saving v2 tags. Restore
            # those unmanaged bytes on the temporary file before allowing commit.
            restore_id3v1_tail(path, original_v1)
            verify_preserved_frames(original, snapshot_id3(path), changes)
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
        actual_stream = _stream_info(audio)

        if actual_stream != baseline_stream:
            issues.append(
                Issue(
                    code=MediaErrorCode.VERIFICATION_FAILED,
                    message="Stable MP3 stream properties changed during metadata writing.",
                )
            )

        return VerificationResult(ok=not issues, issues=tuple(issues))
