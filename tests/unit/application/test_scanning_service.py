# Local placeholder files exercise discovery while injected adapters supply tags.
# This isolates scan/group/progress composition from real audio decoding.

import errno
import os
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

from metadata_polisher.application.scanning import (
    ScanLibraryResult,
    ScanLibraryService,
    ScanLibraryStage,
)
from metadata_polisher.domain.media import LocalMediaFile, MediaReadResult, StreamInfo
from metadata_polisher.domain.metadata import FieldReadState, MetadataField, MetadataSnapshot
from metadata_polisher.execution.cancellation import (
    MutableCancellationToken,
    NeverCancelledToken,
    OperationCancelledError,
)
from metadata_polisher.execution.events import (
    OperationEvent,
    OperationProgress,
    OperationStageChanged,
)
from metadata_polisher.scanner.grouping import (
    AlbumGroup,
    GroupingReason,
    GroupingResult,
    GroupingWarning,
    GroupingWarningCode,
    GroupingWarningReason,
)
from metadata_polisher.scanner.scanner import ScanResult
from metadata_polisher.session.state import (
    OperationKind,
    ResultApplicationStatus,
    ScanResultEnvelope,
    SessionState,
    StaleResultReason,
    apply_scan_library_result,
    apply_scan_result,
    begin_operation,
)


def make_read_result(title: str, album: str) -> MediaReadResult:
    states = {field_name: FieldReadState.MISSING for field_name in MetadataField}
    states[MetadataField.TITLE] = FieldReadState.PRESENT
    states[MetadataField.ALBUM] = FieldReadState.PRESENT

    return MediaReadResult(
        metadata=MetadataSnapshot(title=title, album=album),
        field_states=states,
        stream_info=StreamInfo(
            duration_seconds=180.0,
            sample_rate=48_000,
            channels=2,
            bit_depth=24,
            codec="FLAC",
        ),
    )


@dataclass
class FakeReadAdapter:
    format_id: str
    result: MediaReadResult
    read_paths: list[Path] = field(default_factory=list)
    after_read: Callable[[], None] | None = None

    def read(self, path: Path) -> MediaReadResult:
        self.read_paths.append(path)

        if self.after_read is not None:
            self.after_read()

        return self.result


class FakeRegistry:
    def __init__(self, routes: dict[Path, FakeReadAdapter]) -> None:
        self.routes = routes
        self.supported_extensions = frozenset({".flac"})
        self.detected_paths: list[Path] = []

    def detect(self, path: Path) -> FakeReadAdapter | None:
        self.detected_paths.append(path)

        return self.routes.get(path)


@dataclass
class RecordingEventSink:
    events: list[OperationEvent] = field(default_factory=list)

    def emit(self, event: OperationEvent) -> None:
        self.events.append(event)


def touch(path: Path) -> None:
    # The injected adapter supplies metadata, so these bytes only make discovery
    # see a local file; they are deliberately not a real audio fixture.
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"local fixture")


def test_scan_library_composes_local_scan_grouping_and_count_progress(tmp_path: Path) -> None:
    first = tmp_path / "Album" / "01 - Opening.flac"
    second = tmp_path / "Album" / "02 - Finale.flac"
    unsupported = tmp_path / "Album" / "bonus.opus"
    ignored = tmp_path / "cover.jpg"

    for path in (second, ignored, unsupported, first):
        touch(path)

    first_adapter = FakeReadAdapter("flac", make_read_result("Opening", "Album"))
    second_adapter = FakeReadAdapter("flac", make_read_result("Finale", "Album"))
    registry = FakeRegistry({first: first_adapter, second: second_adapter})
    events = RecordingEventSink()
    service = ScanLibraryService(registry)

    result = service.scan_library(
        operation_id="SCAN-0007",
        base_session_revision=5,
        base_library_revision=3,
        root=tmp_path,
        cancellation=NeverCancelledToken(),
        events=events,
    )

    assert result.operation_id == "SCAN-0007"
    assert result.base_session_revision == 5
    assert result.base_library_revision == 3
    assert result.root == tmp_path
    assert tuple(file.path for file in result.scan_result.supported_files) == (first, second)
    assert tuple(file.path for file in result.scan_result.unsupported_files) == (unsupported,)
    assert len(result.grouping_result.groups) == 1
    assert result.grouping_result.groups[0].files == result.scan_result.supported_files
    assert events.events == [
        OperationStageChanged("SCAN-0007", ScanLibraryStage.SCANNING_FILES),
        OperationProgress("SCAN-0007", ScanLibraryStage.SCANNING_FILES, 0, 4),
        OperationProgress("SCAN-0007", ScanLibraryStage.SCANNING_FILES, 1, 4),
        OperationProgress("SCAN-0007", ScanLibraryStage.SCANNING_FILES, 2, 4),
        OperationProgress("SCAN-0007", ScanLibraryStage.SCANNING_FILES, 3, 4),
        OperationProgress("SCAN-0007", ScanLibraryStage.SCANNING_FILES, 4, 4),
        OperationStageChanged("SCAN-0007", ScanLibraryStage.GROUPING_FILES),
        OperationProgress("SCAN-0007", ScanLibraryStage.GROUPING_FILES, 0, 1),
        OperationProgress("SCAN-0007", ScanLibraryStage.GROUPING_FILES, 1, 1),
    ]


def test_scan_library_stops_between_files_when_cancellation_is_requested(tmp_path: Path) -> None:
    first = tmp_path / "01 - Opening.flac"
    second = tmp_path / "02 - Finale.flac"
    touch(first)
    touch(second)
    cancellation = MutableCancellationToken()
    first_adapter = FakeReadAdapter(
        "flac",
        make_read_result("Opening", "Album"),
        after_read=cancellation.cancel,
    )
    second_adapter = FakeReadAdapter("flac", make_read_result("Finale", "Album"))
    registry = FakeRegistry({first: first_adapter, second: second_adapter})
    events = RecordingEventSink()

    with pytest.raises(OperationCancelledError, match="operation was cancelled"):
        ScanLibraryService(registry).scan_library(
            operation_id="SCAN-0008",
            base_session_revision=0,
            base_library_revision=0,
            root=tmp_path,
            cancellation=cancellation,
            events=events,
        )

    assert registry.detected_paths == [first]
    assert second_adapter.read_paths == []
    assert events.events == [
        OperationStageChanged("SCAN-0008", ScanLibraryStage.SCANNING_FILES),
        OperationProgress("SCAN-0008", ScanLibraryStage.SCANNING_FILES, 0, 2),
        OperationProgress("SCAN-0008", ScanLibraryStage.SCANNING_FILES, 1, 2),
    ]


def test_scan_result_converts_explicitly_for_the_central_state_reducer(tmp_path: Path) -> None:
    supported = tmp_path / "Album" / "01 - Opening.flac"
    unsupported = tmp_path / "Album" / "bonus.opus"
    touch(supported)
    touch(unsupported)
    adapter = FakeReadAdapter("flac", make_read_result("Opening", "Album"))
    scanned = ScanLibraryService(FakeRegistry({supported: adapter})).scan_library(
        operation_id="SCAN-0009",
        base_session_revision=0,
        base_library_revision=0,
        root=tmp_path,
    )
    group = scanned.grouping_result.groups[0]
    warning = GroupingWarning(
        code=GroupingWarningCode.POSSIBLE_MULTIPLE_ALBUMS,
        reason=GroupingWarningReason.TRACK_NUMBER_RESTART_WITHOUT_STRONG_TAG_EVIDENCE,
        group_id=group.group_id,
        affected_file_ids=(group.files[0].file_id,),
        message="Review the uncertain grouping.",
    )
    result = replace(
        scanned,
        grouping_result=GroupingResult(groups=(group,), warnings=(warning,)),
    )
    started = begin_operation(
        SessionState(root=None),
        "SCAN-0009",
        OperationKind.SCAN,
        (),
    )

    envelope = ScanResultEnvelope.from_scan_library_result(result)
    applied = apply_scan_result(started, envelope)

    assert envelope.operation_id == result.operation_id
    assert envelope.base_session_revision == result.base_session_revision
    assert envelope.base_library_revision == result.base_library_revision
    assert applied.status is ResultApplicationStatus.APPLIED
    assert applied.state.root == tmp_path
    assert applied.state.groups[0].group is group
    assert applied.state.groups[0].warnings == (warning,)
    assert applied.state.unsupported_files == result.scan_result.unsupported_files
    assert applied.state.scan_issues == result.scan_result.issues


def test_direct_scan_result_application_retains_existing_stale_result_rules(tmp_path: Path) -> None:
    supported = tmp_path / "01 - Opening.flac"
    touch(supported)
    adapter = FakeReadAdapter("flac", make_read_result("Opening", "Album"))
    result = ScanLibraryService(FakeRegistry({supported: adapter})).scan_library(
        operation_id="SCAN-0010",
        base_session_revision=0,
        base_library_revision=0,
        root=tmp_path,
    )
    started = begin_operation(
        SessionState(root=None),
        "SCAN-0010",
        OperationKind.SCAN,
        (),
    )
    changed_while_scanning = replace(started, revision=1)

    rejected = apply_scan_library_result(changed_while_scanning, result)

    assert rejected.status is ResultApplicationStatus.STALE
    assert rejected.reason is StaleResultReason.SESSION_REVISION_CHANGED
    assert rejected.state is changed_while_scanning


def test_scan_library_result_rejects_duplicate_scanned_file_id_hidden_by_mapping() -> None:
    source_path = Path("library/01 - Opening.flac")
    source = LocalMediaFile(
        path=source_path,
        format_id="flac",
        read_result=make_read_result("Opening", "Album"),
    )
    group = AlbumGroup(
        group_id="group-0001",
        files=(source,),
        album_title="Album",
        reason=GroupingReason.DIRECTORY_ALBUM_CONSISTENT,
    )

    with pytest.raises(ValueError, match="unique"):
        ScanLibraryResult(
            operation_id="SCAN-0011",
            base_session_revision=0,
            base_library_revision=0,
            root=Path("library"),
            scan_result=ScanResult(
                supported_files=(source, source),
                unsupported_files=(),
                issues=(),
            ),
            grouping_result=GroupingResult(groups=(group,), warnings=()),
        )


def test_scan_library_checks_cancellation_between_scanning_and_grouping() -> None:
    cancellation = MutableCancellationToken()
    calls: list[str] = []

    def scanner(
        root: Path,
        registry: FakeRegistry,
        *,
        cancellation: MutableCancellationToken,
        on_progress: Callable[[int, int], None],
    ) -> ScanResult:
        assert root == Path("library")
        assert registry is fake_registry
        calls.append("scan")
        on_progress(0, 0)
        cancellation.cancel()

        return ScanResult(supported_files=(), unsupported_files=(), issues=())

    def grouper(_files: tuple[LocalMediaFile, ...]) -> GroupingResult:
        calls.append("group")

        return GroupingResult(groups=(), warnings=())

    fake_registry = FakeRegistry({})
    service = ScanLibraryService(
        fake_registry,
        scanner=scanner,
        grouper=grouper,
    )

    with pytest.raises(OperationCancelledError, match="operation was cancelled"):
        service.scan_library(
            operation_id="SCAN-0012",
            base_session_revision=0,
            base_library_revision=0,
            root=Path("library"),
            cancellation=cancellation,
        )

    assert calls == ["scan"]


def test_scan_library_fails_before_grouping_when_directory_enumeration_is_incomplete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scandir = os.scandir
    grouped: list[LocalMediaFile] = []
    events = RecordingEventSink()

    def deny_root(path: str | os.PathLike[str]) -> object:
        if Path(path) == tmp_path:
            raise PermissionError(errno.EACCES, "directory access denied", str(tmp_path))

        return scandir(path)

    def grouper(files: tuple[LocalMediaFile, ...]) -> GroupingResult:
        grouped.extend(files)
        pytest.fail("An incomplete scan must not produce a replacement library result.")

    monkeypatch.setattr(os, "scandir", deny_root)
    service = ScanLibraryService(FakeRegistry({}), grouper=grouper)

    # A worker failure uses the existing terminal path, which preserves the
    # previous session and keeps its failed scan diagnostics visible.
    with pytest.raises(OSError, match="directory access denied"):
        service.scan_library(
            operation_id="SCAN-0013",
            base_session_revision=0,
            base_library_revision=0,
            root=tmp_path,
            events=events,
        )

    assert grouped == []
    assert all(
        event.stage is ScanLibraryStage.SCANNING_FILES
        for event in events.events
    )
