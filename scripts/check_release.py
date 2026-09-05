"""Inspect Python release archives without extracting or executing their contents."""

import argparse
import tarfile
import zipfile
from email.parser import BytesParser
from pathlib import Path, PurePosixPath

_PRIVATE_PARTS = frozenset({
    ".git", ".venv", ".codex", ".agents", "musics", "plans", ".plans", "docs",
    "build", "dist", "logs", "reports", "__pycache__",
})
_TEST_PEMS = frozenset({
    "tests/fixtures/network/loopback-test-cert.pem",
    "tests/fixtures/network/loopback-test-key.pem",
})
_SOURCE_REQUIRED = frozenset({
    "LICENSE", "THIRD_PARTY_NOTICES.md", "README.md", "CONTRIBUTING.md",
    "SECURITY.md", "pyproject.toml", "MANIFEST.in", ".github/workflows/ci.yml",
    "scripts/check_release.py", "src/metadata_polisher/__init__.py",
})


def _read_archive(path: Path) -> tuple[dict[str, bytes], list[str]]:
    """Read regular members only; links cannot hide a private file behind a name."""
    files: dict[str, bytes] = {}
    errors: list[str] = []

    if path.name.endswith(".tar.gz"):
        with tarfile.open(path, "r:gz") as source:
            for member in source.getmembers():
                if member.isdir():
                    continue

                if not member.isfile():
                    errors.append(f"Non-regular member (including links): {member.name}")
                    continue

                stream = source.extractfile(member)

                if stream is not None:
                    with stream:
                        files[member.name] = stream.read()

    elif path.suffix == ".whl":
        with zipfile.ZipFile(path) as wheel:
            for entry in wheel.infolist():
                if entry.is_dir():
                    continue

                if (entry.external_attr >> 16) & 0o170000 == 0o120000:
                    errors.append(f"Symbolic link: {entry.filename}")
                    continue

                files[entry.filename] = wheel.read(entry)

    else:
        errors.append("Expected a .tar.gz source distribution or .whl wheel")

    return files, errors


def check_archive(path: Path) -> tuple[str, ...]:
    """Reject local data and missing licence/development material in a release."""
    files, errors = _read_archive(path)
    source_release = path.name.endswith(".tar.gz")
    relative_files: dict[str, bytes] = {}

    # A source distribution has one wrapper directory. Check that invariant
    # before stripping it, so a second root cannot disguise a private directory.
    if source_release:
        paths = [PurePosixPath(name) for name in files]
        roots = {member.parts[0] for member in paths if member.parts}

        if len(roots) != 1 or any(len(member.parts) < 2 for member in paths):
            errors.append("Source members must share a single wrapper directory")

    for name, content in files.items():
        member = PurePosixPath(name)

        # Archive paths use forward slashes regardless of the build platform.
        # Reject Windows drive paths and traversal before removing the sdist root.
        if member.is_absolute() or ".." in member.parts or "\\" in name or ":" in name:
            errors.append(f"Unsafe member path: {name}")
            continue

        relative = PurePosixPath(*member.parts[1:]) if source_release else member
        relative_name = relative.as_posix()
        relative_files[relative_name] = content
        lower_parts = {part.lower() for part in relative.parts}
        filename = relative.name.lower()

        if (
            lower_parts & _PRIVATE_PARTS
            or filename == "settings.json"
            or filename == ".env"
            or filename.startswith(".env.")
            or relative.suffix.lower() in {".log", ".pyc", ".pyo", ".pfx", ".p12"}
            or (relative.suffix.lower() == ".pem" and relative_name not in _TEST_PEMS)
        ):
            errors.append(f"Private or generated material: {relative_name}")

    if source_release:
        for required in sorted(_SOURCE_REQUIRED - relative_files.keys()):
            errors.append(f"Missing source file: {required}")

        if (
            any(name in relative_files for name in _TEST_PEMS)
            and "tests/fixtures/network/README.md" not in relative_files
        ):
            errors.append("Synthetic TLS fixtures require their provenance README")

        licence = relative_files.get("LICENSE", b"")
        metadata = relative_files.get("PKG-INFO", b"")

    else:
        metadata_names = [name for name in relative_files if name.endswith(".dist-info/METADATA")]

        if len(metadata_names) != 1:
            errors.append("Wheel must contain exactly one distribution METADATA file")
            metadata = b""
            licence = b""

        else:
            metadata_name = metadata_names[0]
            prefix = metadata_name.removesuffix("METADATA") + "licenses/"
            metadata = relative_files[metadata_name]
            licence = relative_files.get(prefix + "LICENSE", b"")

            if prefix + "THIRD_PARTY_NOTICES.md" not in relative_files:
                errors.append("Wheel is missing THIRD_PARTY_NOTICES.md")

    if not licence.splitlines() or licence.splitlines()[0] != b"MIT License":
        errors.append("Missing MIT licence text")

    if BytesParser().parsebytes(metadata).get("License-Expression") != "MIT":
        errors.append("Distribution metadata must declare License-Expression: MIT")

    return tuple(errors)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="Folder containing source and wheel distributions")
    arguments = parser.parse_args()
    directory: Path = arguments.directory
    archives = sorted([*directory.glob("*.tar.gz"), *directory.glob("*.whl")])

    if not any(path.suffix == ".whl" for path in archives) or not any(
        path.name.endswith(".tar.gz") for path in archives
    ):
        parser.error("Build both a source distribution and wheel before checking the release")

    failed = False

    for archive in archives:
        try:
            errors = check_archive(archive)
        except (OSError, tarfile.TarError, zipfile.BadZipFile) as error:
            errors = (f"Cannot read archive: {error}",)

        for problem in errors:
            print(f"{archive.name}: {problem}")

        if errors:
            failed = True
        else:
            print(f"{archive.name}: release contents checked")

    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
