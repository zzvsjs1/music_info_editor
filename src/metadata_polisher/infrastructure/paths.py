"""Centralised runtime paths for source and portable frozen builds."""

import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ApplicationPaths:
    """Locations for application-owned files beside the running application."""

    app_dir: Path
    settings_file: Path
    logs_dir: Path
    reports_dir: Path

    @classmethod
    def detect(cls) -> ApplicationPaths:
        """Derive all runtime paths from the single approved application directory."""
        # Frozen builds anchor storage beside their executable; source runs use
        # the selected working directory. No roaming-profile fallback is implied.
        app_dir = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path.cwd()

        return cls(
            app_dir=app_dir,
            settings_file=app_dir / "settings.json",
            logs_dir=app_dir / "logs",
            reports_dir=app_dir / "reports",
        )


@dataclass(frozen=True)
class WritableCheckResult:
    """Outcome suitable for presenting at the desktop start-up boundary."""

    ok: bool
    message: str | None = None


def _probe_directory(directory: Path) -> None:
    """Create and remove a uniquely named file to prove a directory is writable."""
    with tempfile.NamedTemporaryFile(
        dir=directory,
        prefix=".metadata-polisher-write-test-",
        delete=True,
    ):
        pass


def ensure_writable_application_dir(paths: ApplicationPaths) -> WritableCheckResult:
    """Check settings/log storage without changing an existing settings document."""
    try:
        if not paths.app_dir.is_dir():
            raise NotADirectoryError(paths.app_dir)

        # Atomic settings saves create a sibling temporary file even when the
        # existing JSON is writable, so directory create access is also needed.
        _probe_directory(paths.app_dir)

        if paths.settings_file.exists():
            # Opening read/write validates update access while deliberately leaving the
            # user's existing JSON byte-for-byte unchanged.
            with paths.settings_file.open("r+b"):
                pass
        paths.logs_dir.mkdir(exist_ok=True)
        _probe_directory(paths.logs_dir)
    except OSError as error:
        return WritableCheckResult(
            ok=False,
            message=(
                "Metadata Polisher cannot store settings and logs beside the executable. "
                f"Move the application to a writable directory and try again. Details: {error}"
            ),
        )

    return WritableCheckResult(ok=True)
