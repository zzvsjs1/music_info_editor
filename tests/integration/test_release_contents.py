"""Check real disclosure risks using small disposable release archives."""

import io
import tarfile
import zipfile
from pathlib import Path

import pytest

from scripts.check_release import check_archive


def write_source(path: Path, extra: dict[str, bytes], *, outside_root: str | None = None) -> None:
    files = {
        "LICENSE": b"MIT License\nCopyright (c) 2026 Example\n",
        "THIRD_PARTY_NOTICES.md": b"Dependency notices\n",
        "PKG-INFO": b"Metadata-Version: 2.4\nLicense-Expression: MIT\n",
        "pyproject.toml": b"[project]\n",
        "README.md": b"Public readme\n",
        "CONTRIBUTING.md": b"Contribution guide\n",
        "SECURITY.md": b"Security reporting\n",
        "MANIFEST.in": b"include LICENSE\n",
        ".github/workflows/ci.yml": b"name: Checks\n",
        "scripts/check_release.py": b"# Release checker\n",
        "src/metadata_polisher/__init__.py": b"",
        "tests/fixtures/network/loopback-test-key.pem": b"Synthetic fixture\n",
        "tests/fixtures/network/README.md": b"Synthetic fixture provenance\n",
    }
    files.update(extra)

    with tarfile.open(path, "w:gz") as archive:
        for name, content in files.items():
            member = tarfile.TarInfo(f"metadata_polisher-0.1.0/{name}")
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))

        if outside_root is not None:
            member = tarfile.TarInfo(outside_root)
            member.size = 7
            archive.addfile(member, io.BytesIO(b"Private"))


@pytest.mark.parametrize("private_path", [
    "src/metadata_polisher/settings.json",
    "musics/private.flac",
    "plans/private.md",
    "docs/private.md",
    ".git/config",
    ".env",
    "logs/run.log",
    "tests/fixtures/network/real-key.pem",
])
def test_source_check_rejects_private_data_even_when_nested(tmp_path: Path, private_path: str) -> None:
    archive = tmp_path / "source.tar.gz"
    write_source(archive, {private_path: b"Private marker"})

    assert any(private_path in error for error in check_archive(archive))


def test_source_check_accepts_documented_synthetic_key_and_complete_public_source(tmp_path: Path) -> None:
    archive = tmp_path / "source.tar.gz"
    write_source(archive, {})

    assert check_archive(archive) == ()


@pytest.mark.parametrize("outside_root", ["musics/private.flac", "settings.json"])
def test_source_check_requires_one_common_root(tmp_path: Path, outside_root: str) -> None:
    archive = tmp_path / "source.tar.gz"
    write_source(archive, {}, outside_root=outside_root)

    assert any("single" in error.lower() for error in check_archive(archive))


def test_source_check_rejects_escaping_archive_members(tmp_path: Path) -> None:
    archive = tmp_path / "source.tar.gz"
    write_source(archive, {"../outside.py": b"Escaping marker"})

    assert any("unsafe" in error.lower() for error in check_archive(archive))


def test_source_check_rejects_links_without_following_them(tmp_path: Path) -> None:
    archive = tmp_path / "source.tar.gz"

    with tarfile.open(archive, "w:gz") as stream:
        member = tarfile.TarInfo("metadata_polisher-0.1.0/linked.py")
        member.type = tarfile.SYMTYPE
        member.linkname = "../../private.py"
        stream.addfile(member)

    assert any("link" in error.lower() for error in check_archive(archive))


def test_wheel_requires_mit_metadata_and_both_notices(tmp_path: Path) -> None:
    archive = tmp_path / "example.whl"

    with zipfile.ZipFile(archive, "w") as stream:
        stream.writestr("metadata_polisher/__init__.py", "")
        stream.writestr("example.dist-info/METADATA", "License-Expression: MIT\n")
        stream.writestr("example.dist-info/licenses/LICENSE", "MIT License\n")

    assert any("THIRD_PARTY_NOTICES.md" in error for error in check_archive(archive))

    with zipfile.ZipFile(archive, "a") as stream:
        stream.writestr("example.dist-info/licenses/THIRD_PARTY_NOTICES.md", "Notices\n")

    assert check_archive(archive) == ()
