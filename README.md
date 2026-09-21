# Metadata Polisher

Metadata Polisher is a portable Windows desktop application for reviewing and repairing metadata in an existing music library. It scans local files, offers metadata from the provider selected in Settings, explains its matching decisions, and writes only the changes you approve.

V1.1 targets Windows. Source development requires Python 3.14 or newer and uses PySide6, Mutagen, RapidFuzz and httpx. Development, tests and packaging target conventional, GIL-enabled CPython 3.14. Later interpreters require separate verification.

Original project code is available under the [MIT licence](LICENSE), copyright 2026 zzvsjs. Dependencies retain their own licences, including Mutagen's GPL licence; see [third-party notices](THIRD_PARTY_NOTICES.md). The initial public release is prepared as source. Frozen Windows binaries require the additional redistribution work described there.

## Portable use

Keep the complete `MetadataPolisher/` build folder together, including its bundled dependency files, and run `MetadataPolisher.exe` from a writable location. Copy the whole folder when moving the application to another computer. The packaged application includes its Python runtime.

Application-owned files live beside the executable:

| Relative path | Purpose |
| --- | --- |
| `settings.json` | Saved preferences and the last library folder |
| `logs/` | Rotating application logs and optional detailed traces |
| `reports/` | Optional JSON Apply reports when no custom report directory is selected |

The application checks that it can save settings and logs at start-up. If that folder is not writable, it explains the problem and exits; move the complete folder to a writable location. It does not save these files in AppData or request administrator access.

Scanned groups, candidates, review choices, undo history, inclusion choices and the provider cache exist only for the current session. Application credentials also stay in RAM. Saved preferences, window layout and completed file changes remain after closing the application.

Rescan, browsing to another folder and exiting now ask before discarding pending decisions. **Cancel** is the default. Failed or cancelled scans retain the current review; a successful replacement scan starts a fresh review and write selection.

## Review and apply

Manual field editing is available after scanning, even without an online lookup.

**Files / tracks** uses the full height of the right-hand workspace. Select **Metadata review…** to open a separate, resizable review window; you can keep selecting files in the main window while reviewing them. The review window starts closed, remembers its size, and follows the selected files without opening automatically. **Find Metadata** opens it automatically. Close it with **X** or **Escape**; pending review choices remain available when you reopen it. **Group tools → Reset layout** restores the default layout.

Use **Select all** or press **Ctrl+A** in the files table to highlight every file in the displayed group. **Clear selection** or **Ctrl+Shift+A** clears that highlighting. Selection, rename choices and inclusion in the next write batch are separate actions. The review window's scope selector offers **Selected files**, **Current group**, **Included files** and **All library files**, so you can also review across groups without selecting each one in turn.

Double-click a file or press **Enter** in Files to review the highlighted tracks. **F2** or a double-click on a field's **Final** value opens its manual editor. **Full values** expands read-only, copyable details for the selected fields. Field actions follow highlighted fields; removing a field from the selection removes it from the action. **Previous** and **Next** in individual-file review retain your selected fields. Right-click files, fields or albums for contextual commands. Press **F1**, or choose **Group tools → Help and shortcuts**, for the workflow and keyboard reference.

| Shortcut | Action |
| --- | --- |
| Ctrl+O | Browse for a music folder |
| Enter in the folder path / F5 | Scan / rescan, with pending-work protection |
| Ctrl+E | Open metadata review |
| Ctrl+L | Find metadata for all highlighted searchable albums |
| F2 in the field table | Edit the highlighted field(s) |
| Ctrl+Z in review | Undo the last available pending review action, even without file highlighting |
| Alt+Left / Alt+Right in review | Previous / next file in the current album |
| Ctrl+Enter in either workspace | Open the final Review changes summary |
| Escape in review | Close the review window while retaining its decisions |
| F1 | Open workflow help and shortcuts |

Shortcuts respect their window and editor contexts. Opening the final summary does not start writing. Multi-album lookup runs serially and reports completed/skipped groups; select an album and choose a candidate to inspect its results.

1. Choose a music folder with **Browse**, or enter its path and select **Rescan**. Scanning is local and makes no provider requests.
2. Inspect the detected album/disc groups. The **Group tools** menu provides split, merge and disc-number corrections. Groups sit on the left and files on the right. Resize panes or columns as needed; header menus expose optional columns.
3. Select **Find Metadata for Selected** or **Find All Incomplete** to start an online lookup. Choose a release and its disc, and inspect the **Why?** evidence. Scores are deterministic rankings, not probabilities.
4. Check the track assignments. **Map tracks…** lets you assign an unused provider track or leave a local file unmapped. The group language selector chooses among supplied variants. **Use Settings** inherits the displayed preference; **Auto** explicitly uses existing library text as evidence, even when Settings prefers a language.
5. Open **Metadata review…**, select files and fields, then check the displayed review scope. **Keep existing** preserves each file's own value; **Use each file's candidate** uses its own mapped proposal. **Set common value…**, **Clear** and safe additions operate on that declared scope. Missing candidates are skipped. Undo reverses pending review edits only.
6. Review filenames separately with **Keep filename** or **Use proposed filename**. **Filename previews…** shows each file's result. Resolve blocking filename or mapping problems before applying.
7. Tick **Include**, or choose **Add to write batch**, for files to write. **Add scope to write batch** adds the review window's declared scope. Highlighting rows and accepting metadata do not include files automatically. Select **Review changes…** in either window, check the same immutable summary, then select **Apply changes** to start writing.
8. The results view opens when writing finishes and remains reachable through **Apply results…**. Successfully reread files show **Written**, and the album list refreshes from their saved tags. Completed and unchanged files leave the next write batch; unfinished files stay included and unrelated pending reviews survive. You can review and include another album straight away. A failed reread or uncertain filesystem outcome explicitly requires a rescan.

Splitting or merging groups retains manual values, Clear, Keep existing and filename choices. It resets release selection and track mapping; a warning identifies candidate choices that need review again and allows cancellation. Independent pending review choices remain undoable after regrouping.

The default filename template is `[%discnumber%.]%tracknumber%. %title%`. Brackets make the enclosed content optional: disc 1, track 1 becomes `1.01. Title.flac`; without a disc number it becomes `01. Title.flac`. Settings provides the template and minimum digit widths from **1 to 10**, covering editable track/disc numbers without excessive padding. Invalid saved widths are reported and replaced with safe defaults. The original extension is retained, Windows filename problems are reported, and collisions require correction.

To rename every file shown in the current group:

1. Click **Select all**, then **Rename files…**.
2. Inspect the template and each file's preview; tick the filenames to rename. Unticking a previously queued rename keeps that filename. The template is configured in **Settings**.
3. Choose **Include chosen files and review changes…**. Check the final file list, any previously included files and pending metadata changes, then confirm **Apply changes**.

Cancelling Rename files leaves the session untouched. Cancelling the final Apply summary retains prepared choices for further review. Filename choices from one Rename files action are undone together; write inclusion remains a separate choice.

To rename across the entire scanned library, open **Metadata review…**, choose **All library files**, then **Use proposed filename** and **Filename previews…**. Choose **Add scope to write batch**, then **Review changes…**. No online lookup is required when existing metadata supplies the filename values. Resolve blocking preview problems before writing.

## Formats and metadata

| Format | Extensions | Tag representation |
| --- | --- | --- |
| FLAC | `.flac` | Vorbis comments |
| MP3 | `.mp3` | Existing supported ID3v2.3/v2.4 retained; new tags use v2.4 |
| MPEG-4 audio | `.m4a`, audio `.mp4` | MP4/iTunes atoms |
| WAV | `.wav` | Existing supported ID3v2.3/v2.4 inside RIFF/WAVE; new tags use v2.4 |
| TAK | `.tak` | APEv2 |

Managed fields are title, artists, album, album artists, composers, track number/total, disc number/total, date/year and genres. Multiple artists, composers and genres remain separate values where the format supports them; unsafe v2.3 representations are blocked. Contradictory aliases require explicit review. WAV INFO/BWF presence is reported separately. Adapters preserve unmanaged metadata rather than normalising it incidentally.

Album composer credits remain album evidence. A track Composer proposal needs explicit track, work or all-tracks evidence; a deliberate common manual edit remains available. Partial provider lists do not invent track or disc totals. CUE sheets and EAC logs are ignored.

Recognised formats outside this list, such as Ogg, Opus, WMA, APE and WavPack, appear as unsupported. V1 does not translate or invent romanisation, fingerprint audio, or require external encoder/tagging executables.

## Providers

Choose **MusicBrainz**, **VGMdb** or **None** in Settings. Opening Settings, scanning and changing the selection make no requests. Every lookup and explicit connection test uses only the selected provider, with no other-provider fallback. Existing candidates retain provenance and can still be reviewed offline after a switch.

**MusicBrainz** provides general release data and composer relationships. Before its first lookup, enter your contact email or public project URL when prompted. The entry is used in the request User-Agent and retained for that session. Alternatively, set it in PowerShell before launching:

```powershell
$env:METADATA_POLISHER_MUSICBRAINZ_CONTACT = "you@example.org"
& .\dist\MetadataPolisher\MetadataPolisher.exe
```

Replace the example with your own contact. For development, set the same variable before the Python launch command below.

MusicBrainz searches both release titles and catalogue aliases. This can find a Japanese title even when the release's main title is English; it does not translate the returned metadata. Searches retain the original album text and add a clean-title fallback for recognised catalogue prefixes and terminal disc annotations. Existing tags remain unchanged.

**VGMdb** provides specialist soundtrack metadata. Its parser is isolated from matching. Album-level credits are not propagated to every track, and unproven totals remain unknown.

VGMdb can block automated access with HTTP 403 or a browser challenge. This appears as `ACCESS_DENIED`; changing the album name will not resolve that block. The release dialogue distinguishes no matches from failures and keeps long diagnostics scrollable. Failed repeat searches retain the previous release, mapping and review choices alongside the new failure. If a completed search finds nothing, **Edit search terms…** accepts another known title or artist without discarding independent manual decisions. Accepting unchanged terms retains the current review.

Online lookup uses outbound HTTPS with bounded retries, timeouts and provider rate limits. Settings offers **Direct** or **Manual HTTP proxy**; ambient proxy variables are ignored and TLS verification remains enabled. Proxy credentials are masked, staged separately from active credentials, and saved only with **Save for this session**. **Forget** and application exit remove their usability. Connection testing does not activate an unsaved draft; editing credentials marks the new draft untested. Neither current adapter requires an account login. There is no local HTTP service or persistent provider cache.

## Write safety and diagnostics

Editing the review and preview does not modify music files. Apply checks the selected batch, then copies each source to a temporary sibling, writes the approved metadata there, reopens it, and verifies the changed fields and stable media properties before committing. Runtime verification checks metadata, duration and available stream properties; it does not compute audio-payload checksums.

A normal failure before commit leaves the original intact. A rename keeps the original until the final output has been verified. If deleting the original then fails, both files are retained and the cleanup error is reported. A write failure stops the remaining files in that album. Cancellation takes effect at a safe file boundary, and files already completed remain completed; there is no whole-album rollback.

Permanent backups and JSON reports are both off by default. Enable them in **Settings** if wanted. Backups require a selected directory and mirror the source paths beneath an operation directory. Reports record actual file outcomes, field decisions and provenance. A report-writing error does not undo completed audio changes.

Use **Diagnostics…** to inspect matching evidence, copy a diagnostic summary or open the log folder. Detailed tracing is optional. Credential, cookie and token values are redacted from diagnostic output.

## Development and portable build

For a fresh checkout, install conventional CPython 3.14 and create a virtual environment once, from the repository root:

```powershell
py -3.14 -m venv .venv
```

If the checkout already has a working `.venv`, reuse it. Install the dependencies and launch with:

```powershell
& .\.venv\Scripts\python.exe -m pip install -e ".[dev]"
& .\.venv\Scripts\python.exe -m metadata_polisher
```

In source mode the application directory is the current working directory, so the commands above keep `settings.json`, `logs/` and default `reports/` in the repository root. These runtime files are ignored by Git.

Build the portable Windows folder with:

```powershell
& .\.venv\Scripts\python.exe scripts\build_windows.py
```

The PyInstaller specification is `packaging/metadata-polisher.spec`. Output is `dist/MetadataPolisher/MetadataPolisher.exe` with its companion files in the same build folder. Local builds include the project's licence and dependency guidance under `_internal/`. Publishing a binary requires the [third-party redistribution requirements](THIRD_PARTY_NOTICES.md#requirements-before-distributing-a-frozen-application), complete regression checks and packaged-application smoke checks.

Close the packaged application before rebuilding. Builds use a separate staging folder and preserve the existing `settings.json`, `logs/` and `reports/` when replacing the portable output. A failed build leaves the previous application folder intact. A clean release folder must exclude your runtime settings, logs, reports and music while retaining the executable, complete `_internal/` directory and all required dependency notices and source materials.

## Tests

The default suite is offline and does not require `musics/`. Run it and the static checks with:

```powershell
& .\.venv\Scripts\python.exe -m pytest -q
& .\.venv\Scripts\python.exe -m ruff check .
& .\.venv\Scripts\python.exe -m mypy src scripts
```

Normal format integration tests generate disposable WAV fixtures. Other formats have native-tag and adapter tests; real container checks use the optional local-media suite. Keep private sample files under the ignored `musics/` directory and run:

```powershell
& .\.venv\Scripts\python.exe -m pytest -m localmedia -q -rs
```

This selects at most one representative per supported format, copies it into pytest's temporary directory and performs all writes on that copy. Source hashes, sizes and modification times are checked afterwards. Missing formats are skipped. Do not move private music into committed fixtures.

Live provider tests are also opt-in and require their environment switches:

```powershell
$env:METADATA_POLISHER_MUSICBRAINZ_CONTACT = "you@example.org"
$env:METADATA_POLISHER_MUSICBRAINZ_LIVE = "1"
& .\.venv\Scripts\python.exe -m pytest tests/live/test_musicbrainz_live.py -m live -q -rs
```

Run only the live test for the provider you have selected. These tests make real requests. A missing opt-in setting or unavailable optional sample can produce a skip; inspect the reasons separately from offline results. Do not enable an all-provider sweep.

## Contributing and releases

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, architecture and review expectations, and [SECURITY.md](SECURITY.md) for private vulnerability reporting guidance. Bug reports should contain minimal reproduction steps and sanitised diagnostics.

The Windows [CI workflow](.github/workflows/ci.yml) runs the offline suite, static checks, Python package builds and release-content checks. It uses Qt's native Windows backend for screen and font layout tests. It does not upload or publish releases.

Build source and wheel archives after installing the development dependencies:

```powershell
& .\.venv\Scripts\python.exe -m build --outdir dist/open-source
& .\.venv\Scripts\python.exe scripts/check_release.py dist/open-source
```

The `.tar.gz` contains application source, tests, synthetic fixtures and public project guidance. The `.whl` contains the Python application and licence notices; dependencies and a Python runtime must be installed separately. Package version `0.1.0` is independent of the V1.1 feature-iteration name used above.

Source archives exclude Git history, private `docs/`, local plans, music and runtime state. The checker verifies that boundary and licence metadata; it is not a comprehensive secret scanner or licence audit. Review the contents before publication. For a first public repository, start from the reviewed source archive without copying an existing `.git` directory. Ignore rules do not remove private files from old commits or local snapshot refs; review existing history separately before pushing it, and avoid mirror pushes of local refs.

Once a public repository is chosen, add its URL to the package metadata, enable private vulnerability reporting, and require the Windows CI check before merging changes. Frozen executable distribution requires the additional [dependency licence work](THIRD_PARTY_NOTICES.md#requirements-before-distributing-a-frozen-application).
