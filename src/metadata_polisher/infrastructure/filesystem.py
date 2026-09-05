"""Minimal filesystem boundary used by safe media-file transactions."""

import os
import shutil
import tempfile
from pathlib import Path
from typing import Protocol


class FileSystem(Protocol):
    """Only the filesystem operations whose failures affect transaction safety."""

    def create_temporary_sibling(self, source: Path) -> Path: ...

    def create_directory(self, path: Path) -> None: ...

    def copy_file(self, source: Path, destination: Path, *, overwrite: bool) -> None: ...

    def replace_file(self, source: Path, destination: Path) -> None: ...

    def move_file_no_replace(self, source: Path, destination: Path) -> None: ...

    def remove_file(self, path: Path) -> None: ...

    def exists(self, path: Path) -> bool: ...


class LocalFileSystem:
    """Synchronous Windows filesystem implementation for the serial worker."""

    def create_temporary_sibling(self, source: Path) -> Path:
        # Keeping the media suffix lets adapters which inspect extensions reopen
        # the temporary copy without weakening their normal format checks.
        # A sibling stays on the source volume, allowing the final replacement
        # to use the filesystem's atomic move rather than a cross-volume copy.
        descriptor, raw_path = tempfile.mkstemp(
            prefix=f".{source.stem}.metadata-polisher-",
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
            shutil.copy2(source, destination)
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

    def replace_file(self, source: Path, destination: Path) -> None:
        os.replace(source, destination)

    def move_file_no_replace(self, source: Path, destination: Path) -> None:
        if destination.exists():
            raise FileExistsError(destination)

        # Metadata Polisher is Windows-only. On Windows os.rename is an atomic
        # same-volume move and raises rather than replacing an existing file.
        os.rename(source, destination)

    def remove_file(self, path: Path) -> None:
        path.unlink(missing_ok=True)

    def exists(self, path: Path) -> bool:
        return path.exists()
