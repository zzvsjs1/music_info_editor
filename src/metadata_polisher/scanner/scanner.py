"""Recursive local-media discovery with per-file failure isolation."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from metadata_polisher.domain.errors import Issue, MediaErrorCode
from metadata_polisher.domain.media import LocalMediaFile, UnsupportedMediaFile
from metadata_polisher.execution.cancellation import CancellationToken, NeverCancelledToken
from metadata_polisher.formats.base import MediaFormatAdapter, MediaFormatError
from metadata_polisher.scanner.filename_hints import extract_filename_hints


# Depend on the registry contract so discovery can be exercised with small
# fake adapters while real content probing remains owned by format adapters.
class FormatRegistry(Protocol):
    @property
    def supported_extensions(self) -> frozenset[str]: ...

    def detect(self, path: Path) -> MediaFormatAdapter | None: ...


@dataclass(frozen=True)
class ScanResult:
    """Supported files, visible future formats, and isolated scan problems."""

    supported_files: tuple[LocalMediaFile, ...]
    unsupported_files: tuple[UnsupportedMediaFile, ...]
    issues: tuple[Issue, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "supported_files", tuple(self.supported_files))
        object.__setattr__(self, "unsupported_files", tuple(self.unsupported_files))
        object.__setattr__(self, "issues", tuple(self.issues))


_KNOWN_UNSUPPORTED_EXTENSIONS = frozenset({".ape", ".ogg", ".opus", ".wma", ".wv"})


def _path_sort_key(path: Path, root: Path) -> tuple[str, str]:
    relative_path = path.relative_to(root).as_posix()

    return relative_path.casefold(), relative_path


def _contextualise_issue(issue: Issue, path: Path) -> Issue:
    return Issue(
        code=issue.code,
        message=f"{issue.message} File: {path}",
        technical_detail=issue.technical_detail,
    )


def _probe_issue(path: Path, extension: str, error: Exception | None = None) -> Issue:
    if error is None:
        detail = f"No registered adapter accepted content with extension {extension}."
    else:
        detail = f"{type(error).__name__}: {error}"

    return Issue(
        code=MediaErrorCode.CORRUPT_FILE,
        message=f"Could not identify readable media content for {path}.",
        technical_detail=detail,
    )


def _unexpected_read_issue(path: Path, error: Exception) -> Issue:
    code = MediaErrorCode.PERMISSION_DENIED if isinstance(error, PermissionError) else MediaErrorCode.TAG_READ_FAILED

    return Issue(
        code=code,
        message=f"Could not read metadata from {path}.",
        technical_detail=f"{type(error).__name__}: {error}",
    )


def scan_media(
    root: Path,
    registry: FormatRegistry,
    *,
    cancellation: CancellationToken | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> ScanResult:
    """Scan only local files, retaining deterministic order and partial success."""
    active_cancellation = cancellation if cancellation is not None else NeverCancelledToken()
    active_cancellation.raise_if_cancelled()
    supported_files: list[LocalMediaFile] = []
    unsupported_files: list[UnsupportedMediaFile] = []
    issues: list[Issue] = []
    supported_extensions = frozenset(extension.casefold() for extension in registry.supported_extensions)
    discovered_files: list[Path] = []

    for path in root.rglob("*"):
        active_cancellation.raise_if_cancelled()

        if path.is_file():
            discovered_files.append(path)

    active_cancellation.raise_if_cancelled()
    # Filesystem enumeration order is not stable. Sort before reading so
    # result order, progress and subsequent grouping are reproducible.
    files = sorted(discovered_files, key=lambda path: _path_sort_key(path, root))
    total = len(files)

    if on_progress is not None:
        # Current always means the number of discovered filesystem files that
        # have been completely inspected, including ignored non-audio files.
        on_progress(0, total)

    active_cancellation.raise_if_cancelled()

    for current, path in enumerate(files, start=1):
        active_cancellation.raise_if_cancelled()
        extension = path.suffix.casefold()

        try:
            # Registered formats take precedence so adding a future adapter upgrades
            # an extension without requiring a second scanner policy change.
            if extension in supported_extensions:
                try:
                    adapter = registry.detect(path)
                except Exception as error:
                    issues.append(_probe_issue(path, extension, error))
                    continue

                if adapter is None:
                    issues.append(_probe_issue(path, extension))
                    continue

                # Probe success identifies a supported container, but metadata
                # reading can still fail independently. Isolate each failure so
                # one damaged file does not discard the rest of the scan.
                try:
                    read_result = adapter.read(path)
                except MediaFormatError as error:
                    issues.append(_contextualise_issue(error.issue, path))
                    continue
                except Exception as error:
                    issues.append(_unexpected_read_issue(path, error))
                    continue

                supported_files.append(
                    LocalMediaFile(
                        path=path,
                        format_id=adapter.format_id,
                        read_result=read_result,
                        filename_hints=extract_filename_hints(path),
                    )
                )
                issues.extend(_contextualise_issue(issue, path) for issue in read_result.issues)
            elif extension in _KNOWN_UNSUPPORTED_EXTENSIONS:
                unsupported_files.append(UnsupportedMediaFile(path=path))
        # Advance progress for every inspected file, including early continues
        # from probe/read failures, so progress does not stall on bad files.
        finally:
            if on_progress is not None:
                on_progress(current, total)

    active_cancellation.raise_if_cancelled()

    return ScanResult(
        supported_files=tuple(supported_files),
        unsupported_files=tuple(unsupported_files),
        issues=tuple(issues),
    )
