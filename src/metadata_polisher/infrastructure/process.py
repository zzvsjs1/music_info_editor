"""Explicit subprocess boundary for manually selected external executables."""

import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class ProcessResult:
    """Captured outcome from a completed non-shell process."""

    return_code: int
    stdout: str
    stderr: str


class ProcessRunner:
    """Run an explicit executable and argument list without shell expansion."""

    def run(
        self,
        arguments: Sequence[str],
        *,
        timeout_seconds: float,
    ) -> ProcessResult:
        # Pass arguments as separate values with shell expansion disabled so
        # spaces and metacharacters in a selected path remain ordinary text.
        completed = subprocess.run(
            list(arguments),
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )

        return ProcessResult(
            return_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )


@dataclass(frozen=True)
class ExternalToolProbe:
    """Tool-specific arguments used to confirm a selected executable responds."""

    arguments: tuple[str, ...]
    timeout_seconds: float = 5.0

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("Probe timeout must be greater than zero")


class ExternalToolStatus(StrEnum):
    """Resolution states consumed by the settings/application boundary."""

    READY = "ready"
    NEEDS_USER_SELECTION = "needs_user_selection"


@dataclass(frozen=True)
class ExternalToolResolution:
    """Validated executable path, or a reason the UI must request a selection."""

    status: ExternalToolStatus
    path: Path | None = None
    detail: str | None = None


class _Runner(Protocol):
    def run(
        self,
        arguments: Sequence[str],
        *,
        timeout_seconds: float,
    ) -> ProcessResult: ...


class ExternalToolResolver:
    """Validate configured or newly browsed paths without automatic discovery."""

    def __init__(self, runner: _Runner) -> None:
        self._runner = runner

    def resolve_configured(
        self,
        configured_path: str | None,
        probe: ExternalToolProbe,
    ) -> ExternalToolResolution:
        """Validate a configured path or tell the UI to request one from the user."""
        if not configured_path:
            return ExternalToolResolution(status=ExternalToolStatus.NEEDS_USER_SELECTION)

        return self._validate(Path(configured_path), probe)

    def validate_selected(
        self,
        selected_path: str | Path,
        probe: ExternalToolProbe,
    ) -> ExternalToolResolution:
        """Validate a path returned by an explicit file-browse interaction."""
        return self._validate(Path(selected_path), probe)

    def _validate(
        self,
        candidate: Path,
        probe: ExternalToolProbe,
    ) -> ExternalToolResolution:
        # Requiring a complete path prevents an innocent-looking relative value from
        # turning into implicit PATH or current-directory discovery.
        try:
            is_explicit_file = candidate.is_absolute() and candidate.is_file()
        except OSError as error:
            return ExternalToolResolution(
                status=ExternalToolStatus.NEEDS_USER_SELECTION,
                detail=str(error),
            )

        if not is_explicit_file:
            return ExternalToolResolution(status=ExternalToolStatus.NEEDS_USER_SELECTION)

        try:
            result = self._runner.run(
                (str(candidate), *probe.arguments),
                timeout_seconds=probe.timeout_seconds,
            )
        except (OSError, subprocess.SubprocessError) as error:
            return ExternalToolResolution(
                status=ExternalToolStatus.NEEDS_USER_SELECTION,
                detail=str(error),
            )

        # A process starting successfully does not mean the selected tool is
        # usable. Its probe must also finish with the expected success status.
        if result.return_code != 0:
            detail = result.stderr.strip() or result.stdout.strip()

            if not detail:
                detail = f"Probe exited with code {result.return_code}."

            return ExternalToolResolution(
                status=ExternalToolStatus.NEEDS_USER_SELECTION,
                detail=detail,
            )

        return ExternalToolResolution(
            status=ExternalToolStatus.READY,
            path=candidate,
        )
