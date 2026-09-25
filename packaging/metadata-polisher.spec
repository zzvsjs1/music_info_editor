# -*- mode: python ; coding: utf-8 -*-
"""Portable onedir build; application state belongs beside the executable."""

from pathlib import Path

from PyInstaller.building.api import COLLECT, EXE, PYZ
from PyInstaller.building.build_main import Analysis
from PyInstaller.utils.hooks import collect_submodules

# SPECPATH is supplied by PyInstaller. It anchors source discovery independently
# of the shell's working directory; the resulting executable has no source path
# dependency because the analysed modules are collected into the portable folder.
project_root = Path(SPECPATH).resolve().parent

analysis = Analysis(
    [str(project_root / "src" / "metadata_polisher" / "__main__.py")],
    pathex=[str(project_root / "src")],
    binaries=[],
    # Keep the project's licence and dependency guidance with every local build.
    # These two documents alone do not fulfil third-party binary redistribution.
    datas=[
        (str(project_root / "LICENSE"), "."),
        (str(project_root / "THIRD_PARTY_NOTICES.md"), "."),
        # Keep the desktop scene next to its Python loader in frozen builds.
        (str(project_root / "src" / "metadata_polisher" / "ui" / "quick" / "qml"),
         "metadata_polisher/ui/quick/qml"),
    ],
    # RapidFuzz chooses compiled CPU-specific modules dynamically. Its modules
    # must be present even when static analysis cannot follow that selection.
    # Its bundled PyInstaller hook/tests are build tooling, not runtime modules.
    hiddenimports=collect_submodules(
        "rapidfuzz",
        filter=lambda name: name != "rapidfuzz.__pyinstaller" and not name.startswith("rapidfuzz.__pyinstaller."),
    ),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

# The dependency scanner can pick up ICU DLLs from an unrelated Poppler
# installation on PATH. Those copies can prevent the frozen QtCore module from
# loading. Limit this exclusion to the observed Poppler directory and DLLs so
# that binaries supplied by PySide6 or another dependency are still collected.
def is_unrelated_poppler_icu(binary):
    destination, source, kind = binary
    source_directory = tuple(part.casefold() for part in Path(source).parent.parts[-3:])

    return (
        kind == "BINARY"
        and Path(destination).name.casefold() in {"icuuc.dll", "icudt78.dll"}
        and source_directory == ("poppler", "library", "bin")
    )


application_binaries = [
    binary for binary in analysis.binaries if not is_unrelated_poppler_icu(binary)
]

# Keep the launcher small and collect dependencies into the onedir distribution;
# users need the complete resulting folder, including its _internal directory.
archive = PYZ(analysis.pure)
executable = EXE(
    archive,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="MetadataPolisher",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    contents_directory="_internal",
)
collection = COLLECT(
    executable,
    application_binaries,
    analysis.datas,
    strip=False,
    upx=False,
    name="MetadataPolisher",
)
