from dataclasses import dataclass, field
from pathlib import Path

from metadata_polisher.domain.errors import Issue, MediaErrorCode
from metadata_polisher.domain.media import (
    FilenameHintReason,
    MediaReadResult,
    StreamInfo,
    UnsupportedMediaStatus,
)
from metadata_polisher.domain.metadata import FieldReadState, MetadataField, MetadataSnapshot
from metadata_polisher.formats.base import MediaFormatError
from metadata_polisher.scanner.scanner import ScanResult, scan_media


def make_read_result(
    title: str | None,
    *,
    issues: tuple[Issue, ...] = (),
) -> MediaReadResult:
    states = {field: FieldReadState.MISSING for field in MetadataField}

    if title is not None:
        states[MetadataField.TITLE] = FieldReadState.PRESENT

    return MediaReadResult(
        metadata=MetadataSnapshot(title=title),
        field_states=states,
        stream_info=StreamInfo(
            duration_seconds=120.0,
            sample_rate=48_000,
            channels=2,
            bit_depth=24,
            codec="fake",
        ),
        issues=issues,
    )


# Route reads through an explicit fixture result or error, and record calls.
# The temporary files exercise discovery without pretending to be valid audio.
@dataclass
class FakeReadAdapter:
    format_id: str
    result: MediaReadResult | None = None
    error: Exception | None = None
    read_paths: list[Path] = field(default_factory=list)

    def read(self, path: Path) -> MediaReadResult:
        self.read_paths.append(path)

        if self.error is not None:
            raise self.error

        assert self.result is not None
        return self.result


class FakeRegistry:
    def __init__(
        self,
        routes: dict[Path, FakeReadAdapter | Exception | None],
        supported_extensions: frozenset[str],
    ) -> None:
        self.routes = routes
        self.supported_extensions = supported_extensions
        self.detected_paths: list[Path] = []

    def detect(self, path: Path) -> FakeReadAdapter | None:
        self.detected_paths.append(path)
        route = self.routes.get(path)

        if isinstance(route, Exception):
            raise route

        return route


def touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"local fixture")


# Create files out of order and include ignored formats. The expected order
# and probe calls demonstrate deterministic discovery and local-only scope.
def test_scan_recurses_orders_files_and_keeps_filename_hints_non_authoritative(
    tmp_path: Path,
) -> None:
    later = tmp_path / "z-disc" / "02 - Filename Two.FLAC"
    first = tmp_path / "A-disc" / "01 - Filename One.flac"
    unsupported = tmp_path / "A-disc" / "bonus.OPUS"
    ignored_text = tmp_path / "notes.txt"
    ignored_image = tmp_path / "cover.jpg"

    for path in (later, ignored_text, unsupported, first, ignored_image):
        touch(path)

    first_adapter = FakeReadAdapter("flac", make_read_result("Embedded One"))
    later_adapter = FakeReadAdapter("flac", make_read_result("Embedded Two"))
    registry = FakeRegistry(
        routes={first: first_adapter, later: later_adapter},
        supported_extensions=frozenset({".flac"}),
    )

    result = scan_media(tmp_path, registry)

    assert tuple(file.path for file in result.supported_files) == (first, later)
    assert tuple(file.read_result.metadata.title for file in result.supported_files) == (
        "Embedded One",
        "Embedded Two",
    )
    assert tuple(file.filename_hints.probable_title for file in result.supported_files) == (
        "Filename One",
        "Filename Two",
    )
    assert all(
        file.filename_hints.reason is FilenameHintReason.TRACK_DASH_PREFIX
        for file in result.supported_files
    )
    assert result.unsupported_files[0].path == unsupported
    assert result.unsupported_files[0].status is UnsupportedMediaStatus.NOT_SUPPORTED_YET
    assert registry.detected_paths == [first, later]
    assert result.issues == ()


# A failed supported-format probe, a failed tag read and a future format
# are different outcomes. A later valid file must survive all three cases.
def test_scan_distinguishes_corrupt_supported_files_from_known_unsupported_files(
    tmp_path: Path,
) -> None:
    corrupt = tmp_path / "01-corrupt.flac"
    unreadable = tmp_path / "02-unreadable.mp3"
    valid = tmp_path / "03-valid.wav"
    unsupported = tmp_path / "04-future.ape"

    for path in (valid, unsupported, unreadable, corrupt):
        touch(path)

    read_issue = Issue(
        code=MediaErrorCode.TAG_READ_FAILED,
        message="Could not decode ID3 metadata.",
        technical_detail="invalid frame size",
    )
    unreadable_adapter = FakeReadAdapter(
        "mp3",
        error=MediaFormatError(path=unreadable, issue=read_issue),
    )
    valid_adapter = FakeReadAdapter("wave", make_read_result("Valid"))
    registry = FakeRegistry(
        routes={
            corrupt: None,
            unreadable: unreadable_adapter,
            valid: valid_adapter,
        },
        supported_extensions=frozenset({".flac", ".mp3", ".wav"}),
    )

    result = scan_media(tmp_path, registry)

    assert tuple(file.path for file in result.supported_files) == (valid,)
    assert tuple(file.path for file in result.unsupported_files) == (unsupported,)
    assert [issue.code for issue in result.issues] == [
        MediaErrorCode.CORRUPT_FILE,
        MediaErrorCode.TAG_READ_FAILED,
    ]
    assert str(corrupt) in result.issues[0].message
    assert str(unreadable) in result.issues[1].message
    assert "invalid frame size" in (result.issues[1].technical_detail or "")


def test_scan_keeps_file_with_field_level_read_issue_and_contextualises_warning(
    tmp_path: Path,
) -> None:
    path = tmp_path / "01 - Damaged Title.flac"
    touch(path)
    read_issue = Issue(
        code=MediaErrorCode.TAG_READ_FAILED,
        message="Could not read title metadata.",
        technical_detail="invalid UTF-8",
    )
    adapter = FakeReadAdapter("flac", make_read_result(None, issues=(read_issue,)))
    registry = FakeRegistry(
        routes={path: adapter},
        supported_extensions=frozenset({".flac"}),
    )

    result = scan_media(tmp_path, registry)

    assert tuple(file.path for file in result.supported_files) == (path,)
    assert result.supported_files[0].read_result.issues == (read_issue,)
    assert result.supported_files[0].filename_hints.probable_title == "Damaged Title"
    assert len(result.issues) == 1
    assert result.issues[0].code is MediaErrorCode.TAG_READ_FAILED
    assert str(path) in result.issues[0].message


def test_scan_converts_unexpected_probe_failure_and_continues(tmp_path: Path) -> None:
    broken = tmp_path / "01-broken.flac"
    valid = tmp_path / "02-valid.flac"
    touch(broken)
    touch(valid)
    adapter = FakeReadAdapter("flac", make_read_result("Valid"))
    registry = FakeRegistry(
        routes={broken: ValueError("invalid FLAC marker"), valid: adapter},
        supported_extensions=frozenset({".flac"}),
    )

    result = scan_media(tmp_path, registry)

    assert tuple(file.path for file in result.supported_files) == (valid,)
    assert len(result.issues) == 1
    assert result.issues[0].code is MediaErrorCode.CORRUPT_FILE
    assert "ValueError: invalid FLAC marker" in (result.issues[0].technical_detail or "")


def test_scan_result_defensively_normalises_sequences_to_tuples() -> None:
    supported: list = []
    unsupported: list = []
    issues: list = []

    result = ScanResult(  # type: ignore[arg-type]
        supported_files=supported,
        unsupported_files=unsupported,
        issues=issues,
    )
    supported.append(object())
    unsupported.append(object())
    issues.append(object())

    assert result.supported_files == ()
    assert result.unsupported_files == ()
    assert result.issues == ()
