# Contributing to Metadata Polisher

Metadata Polisher is a Windows desktop application for reviewing music metadata before applying approved changes. Contributions should preserve that review boundary, deterministic matching and the file-level transactional writer.

For a substantial feature or a change to metadata behaviour, open an issue describing the problem and proposed behaviour before starting. For a focused bug fix, include a reproducible example and a regression test in the pull request. Report security concerns using [SECURITY.md](SECURITY.md).

## Development setup

Use conventional, GIL-enabled CPython 3.14 on Windows. Later Python versions are permitted by the package metadata but need their own verification. From the root of a fresh checkout or unpacked source archive, run the following in PowerShell.

```powershell
py -3.14 -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -e ".[dev]"
& .\.venv\Scripts\python.exe -m metadata_polisher
```

If the checkout already has a working `.venv`, reuse it and skip the creation command. Source launches save settings, logs and default reports in the current working directory; run from the repository root to keep those files in their ignored locations.

## Making a change

The desktop interface lives in `src/metadata_polisher/ui/quick/`. Its QML controls display state and send commands to the Python services. For changes to selection, matching, file writing or recovery, consult the [project notes for coding agents](AGENTS.md).

To preview a layout with sample data, run `scripts/render_qml.py` with the project’s Python interpreter. It saves screenshots under `build/qml-preview/`. Use `--font 14 --width 840` to check larger text in a narrow window, or `--style native --platform windows` to inspect Windows controls.

For a behaviour change, first add a focused test that demonstrates the bug or missing behaviour. Confirm that it fails for the expected reason, implement the change, then run the relevant checks. Use clear English comments for non-obvious logic and UK English in new documentation and user-facing text. Separate logical blocks with a blank line where it improves readability.

Changes to tag mappings need an authoritative format reference and a test of the raw representation. Reading an edit back through the same library alone does not establish format correctness. Preserve unmanaged tags and supported existing tag versions; report a limitation if an edit cannot be represented safely.

Use synthetic provider responses and generated disposable media in committed tests. Include only material you created or have permission to redistribute, and record its source and licence when relevant. Do not commit private music, copied album artwork, credentials, personal settings, logs or reports. Local planning and scratch files belong in the ignored `plans/` or `.plans/` directory.

## Verification

Run the offline suite and static checks from the repository root:

```powershell
& .\.venv\Scripts\python.exe scripts/test_offscreen.py -q
& .\.venv\Scripts\python.exe -m ruff check .
& .\.venv\Scripts\python.exe -m mypy src scripts
```

The offscreen runner keeps layout checks independent of the host display size. The [CI workflow](.github/workflows/ci.yml) also runs native Windows painting and process lifecycle checks separately, because offscreen rendering cannot validate native theme textures. Manual Windows checks are still required for focus, DPI scaling and packaged application behaviour.

The default pytest configuration excludes `live` and `localmedia` tests. It must work without a music library, provider credentials or network access. Use fake transports for provider behaviour tests.

### Optional checks

**Local media.** Place private samples under the ignored `musics/` directory. The format checks select one sample per available format, edit temporary copies, and verify the source hash, size and modification time afterwards. Missing samples are skipped. See the [audio fixture notes](tests/fixtures/audio/README.md) for what the offline suite covers.

```powershell
& .\.venv\Scripts\python.exe -m pytest -m localmedia -q -rs
```

**Live lookups.** Run only the block for the provider you intend to contact. For MusicBrainz, replace the example address with your contact email or public project URL.

```powershell
$env:METADATA_POLISHER_MUSICBRAINZ_CONTACT = "you@example.org"
$env:METADATA_POLISHER_MUSICBRAINZ_LIVE = "1"
& .\.venv\Scripts\python.exe -m pytest tests/live/test_musicbrainz_live.py -m live -q -rs
```

For VGMdb, run this block on its own.

```powershell
$env:METADATA_POLISHER_VGMDB_LIVE = "1"
& .\.venv\Scripts\python.exe -m pytest tests/live/test_vgmdb_live.py -m live -q -rs
```

Respect provider access restrictions. Report access denials, skips and failures separately from successful live requests and offline test results.

## Portable Windows build

Close any running copy of the packaged application. From the project root, use the virtual environment created during [development setup](#development-setup).

```powershell
& .\.venv\Scripts\python.exe scripts/build_windows.py
```

The [build script](scripts/build_windows.py) uses the [PyInstaller specification](packaging/metadata-polisher.spec) to stage a fresh build, then preserves `settings.json`, `logs/` and `reports/` when replacing the output folder. Keep the entire `dist/MetadataPolisher/` folder together, including `_internal/`. For QML or packaging changes, run `dist/MetadataPolisher/MetadataPolisher.exe`, scan disposable media, review a change, confirm **Apply changes**, and check the saved result.

The first public release is intended to ship as source. Before redistributing a frozen build, prepare a clean folder free of personal settings, logs, reports and music, and complete the [binary redistribution requirements](THIRD_PARTY_NOTICES.md#requirements-before-distributing-a-frozen-application).

## Source and wheel releases

With the development dependencies installed, build both Python archives and check their contents.

```powershell
& .\.venv\Scripts\python.exe -m build --outdir dist/open-source
& .\.venv\Scripts\python.exe scripts/check_release.py dist/open-source
```

The source archive includes application code, tests, synthetic fixtures and contributor guidance. The wheel contains the Python package and licence notices and requires a separately installed Python runtime and dependencies. The [release checker](scripts/check_release.py) checks archive paths, expected files and licence metadata. Review the actual contents for sensitive material before publishing.

For a first public repository, start from the reviewed source archive. Publishing existing Git history requires a separate review of commits and refs for private files. The [Windows CI workflow](.github/workflows/ci.yml) builds and checks the Python archives. Publication is a separate step.

## Submitting a pull request

Explain the user-visible problem, the resulting behaviour, and why the chosen approach fits the existing architecture. Include the commands and results from your checks, any optional checks you skipped, and remaining limitations. Use sanitised screenshots or minimal generated fixtures when they help reproduce a problem.

Review both tracked changes and new files before submitting:

```powershell
git status --short
git diff --check
git diff
```

Keep each pull request focused. Check new dependencies and redistributed assets for licence compatibility and update the dependency notices when needed. Contributions of original project code are made under the project's [MIT licence](LICENSE); separately identified third-party material retains its own licence. Please keep discussion respectful and focused on the work.
