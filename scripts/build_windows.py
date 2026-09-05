"""Build the portable Windows folder with the repository's already-running venv."""

import os
import shutil
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

PROJECT_ROOT = Path(__file__).resolve().parents[1]
_RUNTIME_ITEMS = ("settings.json", "logs", "reports")


def _checked_child(path: Path, root: Path) -> Path:
    """Resolve a filesystem target before a move or recursive cleanup."""
    resolved = path.resolve()
    allowed_root = root.resolve()

    # Reject the root itself as well as escapes: a valid cleanup target must be
    # a disposable child, never the directory that contains the user's checkout.
    if resolved == allowed_root or not resolved.is_relative_to(allowed_root):
        raise OSError(f"Build output must stay inside {allowed_root}: {resolved}")

    return resolved


def _copy_runtime_items(deployed: Path, fresh: Path) -> None:
    """Copy user-owned runtime state before replacing any deployed application."""
    for name in _RUNTIME_ITEMS:
        source = deployed / name
        destination = _checked_child(fresh / name, fresh)

        if source.is_symlink():
            shutil.copy2(source, destination, follow_symlinks=False)
        elif source.is_dir():
            shutil.copytree(source, destination, symlinks=True)
        elif source.exists():
            # Copy bytes and timestamps without parsing or repairing settings.
            # A corrupt settings document is still the user's original file.
            shutil.copy2(source, destination)


def _publish_build(fresh: Path, deployed: Path, staging: Path) -> None:
    """Retain the previous folder until the complete replacement is installed."""
    fresh = _checked_child(fresh, staging)
    deployed = _checked_child(deployed, PROJECT_ROOT)
    previous = _checked_child(staging / "previous", staging)

    # Keep a recoverable complete folder until the replacement has moved into
    # place. Publishing individual files could mix two application versions.
    if deployed.exists():
        os.replace(deployed, previous)

    try:
        os.replace(fresh, deployed)
    except OSError as error:
        if previous.exists():
            try:
                os.replace(previous, deployed)
            except OSError as restore_error:
                raise OSError(
                    f"Could not publish the new build or restore the old folder. "
                    f"The previous application and runtime files remain at {previous}: {restore_error}"
                ) from error

        raise


def _remove_staging(staging: Path, *, published: bool) -> None:
    """Discard build scratch files, retaining a previous folder if rollback failed."""
    if (staging / "previous").exists() and not published:
        print(f"Previous portable folder retained for recovery: {staging / 'previous'}", file=sys.stderr)
        return

    try:
        checked = _checked_child(staging, PROJECT_ROOT)
        shutil.rmtree(checked)
    except OSError as error:
        # Cleanup follows publication and must not undo a usable build. A failed
        # cleanup leaves only local build files; report the path for inspection.
        print(f"Could not remove build staging directory {staging}: {error}", file=sys.stderr)


def main() -> int:
    """Build separately, then preserve runtime state and publish a portable folder."""
    if sys.platform != "win32":
        print("The portable Windows build must run on Windows.", file=sys.stderr)
        return 2

    interpreter = Path(sys.executable)
    expected_interpreter = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"

    if interpreter.absolute() != expected_interpreter.absolute():
        print(
            "Run the build with the existing repository interpreter: "
            r".\.venv\Scripts\python.exe scripts\build_windows.py",
            file=sys.stderr,
        )
        return 2

    spec = PROJECT_ROOT / "packaging" / "metadata-polisher.spec"

    if not spec.is_file():
        print(f"The PyInstaller specification is missing: {spec}", file=sys.stderr)
        return 2

    environment = os.environ.copy()
    environment["PYINSTALLER_CONFIG_DIR"] = str(PROJECT_ROOT / "build" / "pyinstaller-cache")

    # Dependency discovery must use this interpreter and Windows system DLLs.
    # Ambient PDF/encoder tools can expose an incompatible ICU DLL with the same
    # filename as the system library that Qt expects, breaking the frozen app.
    python_base = Path(sys.base_prefix)
    windows = Path(os.environ["SYSTEMROOT"])
    environment["PATH"] = os.pathsep.join(str(directory) for directory in (
        interpreter.parent,
        python_base,
        python_base / "DLLs",
        windows / "System32",
        windows,
    ))

    staging: Path | None = None
    published = False

    try:
        build_root = _checked_child(PROJECT_ROOT / "build", PROJECT_ROOT)
        dist_root = _checked_child(PROJECT_ROOT / "dist", PROJECT_ROOT)
        deployed = dist_root / "MetadataPolisher"

        if deployed.is_symlink() or deployed.is_junction():
            raise OSError(f"The portable output folder must not be a filesystem link: {deployed}")

        deployed = _checked_child(deployed, PROJECT_ROOT)

        if deployed.exists() and not deployed.is_dir():
            raise OSError(f"The portable output path is not a directory: {deployed}")

        build_root.mkdir(parents=True, exist_ok=True)
        # Use ordinary workspace inheritance. Windows tempfile directories use
        # an owner-only ACL which would move with the published app and prevent
        # the desktop user from launching a build made by a sandbox account.
        staging = _checked_child(build_root / f"portable-stage-{uuid4().hex}", PROJECT_ROOT)
        staging.mkdir()
        staged_dist = staging / "dist"
        command = [
            str(interpreter), "-m", "PyInstaller",
            "--noconfirm", "--clean",
            "--distpath", str(staged_dist),
            "--workpath", str(build_root / "pyinstaller"),
            str(spec),
        ]

        try:
            # PyInstaller may remove its output under --noconfirm. It receives a
            # unique scratch directory, never the user's deployed runtime folder.
            # Argument lists and inherited streams retain native build diagnostics.
            result = subprocess.run(command, cwd=PROJECT_ROOT, env=environment, check=False)
        except OSError as error:
            print(f"Could not start PyInstaller: {error}", file=sys.stderr)
            return 1

        if result.returncode != 0:
            print(f"PyInstaller failed with exit code {result.returncode}; review its output above.", file=sys.stderr)
            return result.returncode

        fresh = staged_dist / "MetadataPolisher"
        executable = fresh / "MetadataPolisher.exe"

        if not executable.is_file():
            print(f"PyInstaller did not produce the expected executable: {executable}", file=sys.stderr)
            return 1

        # Preserve runtime files only after a complete executable exists, but
        # before moving the old deployment, so a copy error leaves it usable.
        _copy_runtime_items(deployed, fresh)
        dist_root.mkdir(parents=True, exist_ok=True)
        _publish_build(fresh, deployed, staging)
        published = True
        print(f"Portable application built: {deployed / 'MetadataPolisher.exe'}")
        print("Existing settings, logs and reports were retained.")
        print("Copy the complete MetadataPolisher folder to a writable location before using it.")
        return 0
    except OSError as error:
        print(f"Could not preserve or publish the portable build: {error}", file=sys.stderr)
        return 1
    finally:
        if staging is not None:
            _remove_staging(staging, published=published)


if __name__ == "__main__":
    raise SystemExit(main())
