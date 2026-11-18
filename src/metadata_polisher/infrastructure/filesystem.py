"""Minimal filesystem boundary used by safe media-file transactions."""

import os
import shutil
import stat
import sys
import tempfile
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Protocol

from metadata_polisher.domain.media import FileVersion
from metadata_polisher.rename.windows import STAGING_FILE_PREFIX

RECOVERY_DIRECTORY_PREFIX = ".metadata-polisher-recovery-"

if sys.platform == "win32":
    from metadata_polisher.infrastructure.windows_file_guard import (
        delete_on_close,
        open_file_identity,
        open_guard,
        open_write_probe,
        replace_with_recovery,
    )
    from metadata_polisher.infrastructure.windows_file_version import change_time_ns


def read_descriptor_version(descriptor: int) -> FileVersion:
    """Read the version of this open file, even if another path now refers to it."""
    stat = os.fstat(descriptor)
    # On Windows CPython 3.14 maps st_ctime_ns to creation time. A tagger can
    # preserve size and mtime, so use the native change time from the same handle.
    changed_ns = change_time_ns(descriptor) if sys.platform == "win32" else stat.st_ctime_ns
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, changed_ns


def read_file_version(path: Path) -> FileVersion:
    """Capture identity and modification facts without reading the media bytes."""
    with path.open("rb") as source:
        return read_descriptor_version(source.fileno())


def source_is_writable(path: Path) -> bool:
    """Require data-write access to the source, even when its directory allows replacement."""
    try:
        if sys.platform == "win32":
            # os.access(W_OK) only checks the DOS read-only attribute on Windows.
            with open_write_probe(path):
                return True
        else:
            write_bits = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH
            return bool(path.stat().st_mode & write_bits) and os.access(path, os.W_OK)
    except OSError:
        return False


class ReplacementRecoveryError(OSError):
    """A commit or cleanup may have changed the published file. Retain recovery evidence."""


class FileSystem(Protocol):
    """Only the filesystem operations whose failures affect transaction safety."""

    def is_writable_file(self, path: Path) -> bool: ...

    def create_temporary_sibling(self, source: Path) -> Path: ...

    def create_directory(self, path: Path) -> None: ...

    def copy_file(self, source: Path, destination: Path, *, overwrite: bool) -> None: ...

    def replace_file(self, source: Path, destination: Path, *, recovery: Path | None = None) -> None: ...

    def replacement_recovery_path(self, destination: Path) -> AbstractContextManager[Path]: ...

    def pin_replacement_identity(self, path: Path) -> AbstractContextManager[FileVersion]: ...

    def move_file_no_replace(self, source: Path, destination: Path) -> None: ...

    def pin_file(self, path: Path, *, allow_rename: bool = False) -> AbstractContextManager[FileVersion]: ...

    def remove_file(self, path: Path, *, expected_version: FileVersion | None = None) -> None: ...

    def exists(self, path: Path) -> bool: ...


class LocalFileSystem:
    """Synchronous Windows filesystem implementation for the serial worker."""

    is_writable_file = staticmethod(source_is_writable)

    def create_temporary_sibling(self, source: Path) -> Path:
        # Keeping the media suffix lets adapters which inspect extensions reopen
        # the temporary copy without weakening their normal format checks.
        # A sibling stays on the source volume, allowing the final replacement
        # to use the filesystem's atomic move rather than a cross-volume copy.
        descriptor, raw_path = tempfile.mkstemp(
            prefix=STAGING_FILE_PREFIX,
            suffix=source.suffix,
            dir=source.parent,
        )
        # Release the creation handle before a library reopens this path;
        # Windows sharing rules can otherwise prevent metadata access.
        os.close(descriptor)

        return Path(raw_path)

    def create_directory(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)

    def copy_file(self, source: Path, destination: Path, *, overwrite: bool) -> None:
        if overwrite:
            # This branch populates the writer's already-created private sibling.
            # An attribute change during CopyFile can make that copy read-only
            # even after preflight. Keep it removable on failure/cancellation.
            if sys.platform != "win32":
                shutil.copy2(source, destination)
                return

            with open_file_identity(destination) as owned:
                try:
                    shutil.copy2(source, destination)
                finally:
                    if not os.fstat(owned).st_mode & stat.S_IWRITE:
                        with open_guard(destination) as descriptor:
                            if not os.path.samestat(os.fstat(owned), os.fstat(descriptor)):
                                raise OSError("The temporary copy was replaced before its permissions were restored.")
                            # The guard denies deletion/rename while chmod opens
                            # the owned path for FILE_WRITE_ATTRIBUTES.
                            destination.chmod(stat.S_IWRITE)
            return

        destination_created = False

        try:
            # Exclusive creation means an operation ID can never overwrite an
            # earlier backup, even if the destination appeared after preflight.
            with source.open("rb") as source_file, destination.open("xb") as destination_file:
                destination_created = True
                shutil.copyfileobj(source_file, destination_file)

            shutil.copystat(source, destination)
        except Exception:
            if destination_created:
                destination.unlink(missing_ok=True)

            raise

    @contextmanager
    def replacement_recovery_path(self, destination: Path) -> Iterator[Path]:
        # Give ReplaceFileW a name in a newly created private directory so its
        # backup cannot collide with an earlier transaction or a library file.
        folder = Path(tempfile.mkdtemp(prefix=RECOVERY_DIRECTORY_PREFIX, dir=destination.parent))
        recovery = folder / destination.name
        retain_recovery = False
        try:
            yield recovery
        except ReplacementRecoveryError:
            retain_recovery = True
            raise
        finally:
            # Checked removal can stay pending until the last identity handle
            # is closed. The caller releases those handles before this point.
            if not retain_recovery:
                try:
                    folder.rmdir()
                except OSError as error:
                    raise ReplacementRecoveryError(
                        f"Could not clear the recovery directory. Check {recovery}.",
                    ) from error

    @contextmanager
    def pin_replacement_identity(self, path: Path) -> Iterator[FileVersion]:
        if sys.platform != "win32":
            raise OSError("Protected replacement requires Windows file sharing.")

        with open_file_identity(path) as descriptor:
            version = read_descriptor_version(descriptor)
            if not version[1]:
                raise OSError(f"The filesystem did not supply a stable file ID for {path}.")
            yield version

    def replace_file(self, source: Path, destination: Path, *, recovery: Path | None = None) -> None:
        if recovery is None:
            os.replace(source, destination)
            return

        if sys.platform != "win32":
            raise OSError("Protected replacement requires Windows file sharing.")

        try:
            replace_with_recovery(source, destination, recovery)
        except OSError as error:
            # ERROR_UNABLE_TO_MOVE_REPLACEMENT_2 leaves the original under its
            # recovery name and the edited file at its staging name.
            if getattr(error, "winerror", None) == 1177 or os.path.lexists(recovery):
                raise ReplacementRecoveryError(
                    f"Windows could not finish saving {destination}. Check {recovery} and {source} before rescanning."
                ) from error
            raise

    def move_file_no_replace(self, source: Path, destination: Path) -> None:
        if destination.exists():
            raise FileExistsError(destination)

        # Metadata Polisher is Windows-only. On Windows os.rename is an atomic
        # same-volume move and raises rather than replacing an existing file.
        os.rename(source, destination)

    @contextmanager
    def pin_file(self, path: Path, *, allow_rename: bool = False) -> Iterator[FileVersion]:
        """Keep the file identity alive and prevent external writes until released."""
        if sys.platform != "win32":
            raise OSError("Protected rename publication requires Windows file sharing.")

        with open_guard(path, allow_rename=allow_rename) as descriptor:
            yield read_descriptor_version(descriptor)

    def remove_file(self, path: Path, *, expected_version: FileVersion | None = None) -> None:
        if expected_version is None:
            path.unlink(missing_ok=True)
            return

        if sys.platform != "win32":
            raise OSError("Checked file removal requires Windows file sharing.")

        # Validate and delete through one handle: another process cannot replace
        # a checked name between comparison and the removal of its directory entry.
        with open_guard(path, for_deletion=True) as descriptor:
            if read_descriptor_version(descriptor) != expected_version:
                raise OSError(f"The file at {path} changed before cleanup.")

            delete_on_close(descriptor)

    def exists(self, path: Path) -> bool:
        return path.exists()
