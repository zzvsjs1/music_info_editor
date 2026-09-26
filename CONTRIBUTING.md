# Contributing to Metadata Polisher

Metadata Polisher is a Windows desktop application for reviewing music metadata before applying approved changes. Contributions should preserve that review boundary, deterministic matching and the file-level transactional writer.

For a substantial feature or a change to metadata behaviour, open an issue describing the problem and proposed behaviour before starting. For a focused bug fix, include a reproducible example and a regression test in the pull request. Report security concerns using [SECURITY.md](SECURITY.md).

## Development setup

Use conventional, GIL-enabled CPython 3.14 on Windows. Later Python versions are permitted by the package metadata but need their own verification. From a fresh clone, run these PowerShell commands in the repository root:

```powershell
py -3.14 -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -e ".[dev]"
& .\.venv\Scripts\python.exe -m metadata_polisher
```

If the checkout already has a working `.venv`, reuse it and skip the creation command. Source launches save settings, logs and default reports in the current working directory; run from the repository root to keep those files in their ignored locations.

## Making a change

Read the existing implementation and relevant tests before editing. Keep application rules in typed services and immutable state transformations; QML controls should present state and dispatch user actions. Keep matching decisions explainable, preserve field provenance, and require a validated `ChangeSet` before writing.

The sole interface lives in `src/metadata_polisher/ui/quick/`. `qml/Main.qml` composes the library and separate `ReviewWindow.qml`; `DataTable.qml` presents shared Qt models, while `RowTable.qml` handles smaller stable-ID projections. Keep file/field highlighting, review decisions and write inclusion separate. `UiMetrics.qml` holds presentation defaults. Font metrics set table heights, wrapping action rows retain readable labels, and diagnostics use bounded, selectable text with actions outside their scroll areas.

`backend.py` owns immutable session state and stable-ID review commands. Lookup, Apply, library, settings, diagnostics and preference facades coordinate the shared Python services. `ui/models/`, `ui/qt_bridge.py` and `ui/pending_work.py` remain shared presentation helpers; they do not depend on Qt Widgets. `WindowPreferences.qml` and `preferences.py` retain portable preference keys from earlier releases. Keep domain rules in shared services and presentation in QML.

Run `python scripts/test_offscreen.py -q` with the repository virtual environment for the offline suite. The runner creates a `QGuiApplication`, a 1920×1080 virtual screen and an installed Segoe UI font, using Fusion and software rendering. The `tests/ui/test_quick*.py` suites cover service boundaries, real scene interactions and disposable-media Apply verification; `tests/ui/helpers.py` supplies synthetic sessions and controllable workers. Use actual delegate geometry for clicks instead of fixed checkbox coordinates.

`scripts/render_qml.py` renders synthetic populated windows under `build/qml-preview/`. Its default Fusion/offscreen mode checks layout. Use `--style native --platform windows` for native appearance, and `--font 14 --width 840` to inspect enlarged text in a narrow window. Native theme textures do not render correctly on the offscreen platform, so that combination is rejected. Native focus, scaling and packaged rendering also need Windows checks. Bind secondary-window shortcuts to their visible, active window.

Include QML components in package data and the portable bundle. After QML or packaging changes, exercise `MetadataPolisher.exe` through scan, review and explicit disposable-media Apply; Python tests alone do not establish that frozen Qt modules and scenes are complete. The historical `--qml` switch is accepted only as a compatibility alias. Do not restore a parallel Widgets composition.

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

Optional local-media checks use files under the ignored `musics/` directory, copy selected samples into temporary storage and write only to those copies. See the [README test instructions](README.md#tests) before opting in. Keep your own backups when testing the application manually and use disposable copies, never your only music-library copy.

Live provider checks make real requests and require explicit opt-in. Test only the selected provider, respect its access restrictions, and never bypass a browser challenge. Distinguish offline fixture results, live success, access denial, failures and skipped checks in the pull request.

For a packaging change, also run the Windows build and exercise the resulting application using the [README build instructions](README.md#development-and-portable-build). Record what you actually checked; an executable starting successfully does not verify scanning, review or writing.

## Submitting a pull request

Explain the user-visible problem, the resulting behaviour, and why the chosen approach fits the existing architecture. Include the commands and results from your checks, any optional checks you skipped, and remaining limitations. Use sanitised screenshots or minimal generated fixtures when they help reproduce a problem.

Review both tracked changes and new files before submitting:

```powershell
git status --short
git diff --check
git diff
```

Keep each pull request focused. Check new dependencies and redistributed assets for licence compatibility and update the dependency notices when needed. Contributions of original project code are made under the project's [MIT licence](LICENSE); separately identified third-party material retains its own licence. Please keep discussion respectful and focused on the work.
