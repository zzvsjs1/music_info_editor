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

## Qt Quick interface

Qt Quick/QML is the application interface. The library window and separate metadata review window use the shared application services for lookup, review, grouping, renaming and Apply:

```powershell
& .\.venv\Scripts\python.exe -m metadata_polisher
# Or, from the complete portable build folder:
.\MetadataPolisher.exe
```

Choose a folder and select **Rescan**, then choose an album. Select files and open **Metadata review…** to inspect their existing, proposed and final values. Ctrl-click toggles selections; Shift-click extends them. In the review window, select fields and press **F2** to edit, **Escape** to close an editor, or **Ctrl+Z** to undo a review action. In Files, arrow keys move selection, **Space** toggles inclusion, and **Ctrl+A** selects the current album. **Show full values** displays complete, copyable metadata for every selected field. Manual editing opens a compact separate window; Track and Disc have independent Number and Total inputs. Enter accepts a position, Ctrl+Enter accepts a text edit, and Escape cancels it. Page Up/Down scroll through rows; Ctrl+Home/End moves to the first/last row, and Shift extends selection. Right-click table headers, including an empty header, to choose visible columns.

Provider lookup, candidate explanations, search terms, track mapping, settings, diagnostics, grouping, filename previews and Apply are available in QML. Candidate and per-file tables support keyboard navigation, resizable columns and copyable details; secondary windows retain their sizes. The QML launcher respects the platform control style and explicit Qt style overrides. Settings uses compact Qt Fusion tabs with previous/next controls when the strip overflows; Ctrl+Tab and Ctrl+Shift+Tab switch tabs from an input. Rows and action layouts adapt to larger fonts. Review decisions remain in memory until **Apply changes** is explicitly selected in the final confirmation. Existing portable settings and window preferences are retained. The old Widgets interface has been removed; existing shortcuts containing `--qml` still launch the same QML application.

Click a column header to sort albums, files and secondary tables; click again to reverse the order. Arrows show the active direction. Files initially follow **Disc → Track → File**, using numeric positions rather than filename order. A track without a disc number belongs to disc 1 for this display; missing tracks follow numbered tracks on their disc, and files without either number follow the numbered files in filename order. Right-click a file header and choose **Disc / track order** to restore this default. Selection, write inclusion and review navigation follow the same files when sorting changes. Metadata review fields retain their fixed order.

## Review and apply

Manual field editing is available after scanning, even without an online lookup.

The review window follows **Metadata review → Filename review → Review & Apply**. The metadata table receives spare window space and displays **Field, Existing, Proposed, Final, Status**. **Empty** means a readable field has no value; **No suggestion** means no proposed value is available. Unreadable and unsupported fields remain explicitly labelled. Filename review opens when a rename is suggested, while successful scan details remain collapsed. Both can be expanded; the final action stays fixed below the scrolling content. Accept safe suggestions only accepts confident, non-conflicting additions. It does not include files or write them.

**Files / tracks** uses the full height of the right-hand workspace. Select **Metadata review…** to open a separate, resizable review window; you can keep selecting files in the main window while reviewing them. The review window starts closed, remembers its size, and follows the selected files without opening automatically. **Find Metadata** opens it automatically. Close it with **X** or **Escape**; pending review choices remain available when you reopen it. **Group tools → Reset layout** restores the default layout.

Use **Select all** or press **Ctrl+A** in the files table to highlight every file in the displayed group. **Clear selection** or **Ctrl+Shift+A** clears that highlighting. Selection, rename choices and inclusion in the next write batch are separate actions. The review window's scope selector offers **Selected files**, **Current group**, **Included files** and **All library files**, so you can also review across groups without selecting each one in turn.

Double-click a file or press **Enter** in Files to review the highlighted tracks. **F2** or a double-click on a field's **Final** value opens its manual editor. **Show full values** expands read-only, copyable details for the selected fields. Field actions follow highlighted fields; removing a field from the selection removes it from the action. **Previous** and **Next** in individual-file review retain your selected fields. Right-click files, fields or albums for contextual commands. Press **F1**, or choose **Group tools → Help and shortcuts**, for the workflow and keyboard reference.

| Shortcut | Action |
| --- | --- |
| Ctrl+O | Browse for a music folder |
| Enter in the folder path / F5 | Scan / rescan, with pending-work protection |
| Ctrl+E | Open metadata review |
| Ctrl+L | Find metadata for all highlighted searchable albums |
| F2 in the field table | Edit the highlighted field(s) |
| Ctrl+Z in review | Undo the last available pending review action, even without file highlighting |
| Alt+Left / Alt+Right in review | Previous / next file in the current album |
| Ctrl+Enter in either workspace | Open the final Review & Apply summary |
| Escape in review | Close the review window while retaining its decisions |
| F1 | Open workflow help and shortcuts |

Shortcuts respect their window and editor contexts. Opening the final summary does not start writing. Multi-album lookup runs serially and reports completed/skipped groups; select an album and choose a candidate to inspect its results.

1. Choose a music folder with **Browse**, or enter its path and select **Rescan**. Scanning is local and makes no provider requests.
2. Inspect the detected album/disc groups. The **Group tools** menu provides split, merge and disc-number corrections. Groups sit on the left and files on the right. Resize panes or columns as needed; header menus expose optional columns.
3. Select **Find Metadata for Selected** or **Find All Incomplete** to start an online lookup. Choose a release and its disc, and inspect the **Why?** evidence. Scores are deterministic rankings, not probabilities.
4. Check the track assignments. **Map tracks…** lets you assign an unused provider track or leave a local file unmapped. The group language selector chooses among supplied variants. **Use Settings** inherits the displayed preference; **Auto** explicitly uses existing library text as evidence, even when Settings prefers a language.
5. Open **Metadata review…**, select files and fields, then check the displayed review scope. **Keep existing** preserves each file's own value; **Use proposed** uses its own mapped proposal. **Edit…**, **More… → Clear selected fields** and **Accept safe suggestions** operate on that declared scope. Missing suggestions are skipped. Undo reverses pending review edits only.
6. Review filenames separately with **Keep current** or **Use proposed** in **Filename review**. **Filename previews…** shows each file's result. Resolve blocking filename or mapping problems before applying.
7. Tick **Include**, or choose **Add selected files**, for files to write. **Add selected files** adds the highlighted files; with another Reviewing option, **Add reviewed files** adds the files in that option. Highlighting rows and accepting metadata do not include files automatically. Select **Review & Apply…** in either window, check the same immutable summary, then select **Apply changes** to start writing.
8. The results view opens when writing finishes and remains reachable through **Apply results…**. Successfully reread files show **Written**, and the album list refreshes from their saved tags. Completed and unchanged files leave the next write batch; unfinished files stay included and unrelated pending reviews survive. You can review and include another album straight away. A failed reread or uncertain filesystem outcome explicitly requires a rescan.

Splitting or merging groups retains manual values, Clear, Keep existing and filename choices. It resets release selection and track mapping; a warning identifies candidate choices that need review again and allows cancellation. Independent pending review choices remain undoable after regrouping.

The default filename template is `[%discnumber%.]%tracknumber%. %title%`. Brackets make the enclosed content optional: disc 1, track 1 becomes `1.01. Title.flac`; without a disc number it becomes `01. Title.flac`. Settings provides the template and minimum digit widths from **1 to 10**, covering editable track/disc numbers without excessive padding. Invalid saved widths are reported and replaced with safe defaults. The original extension is retained, Windows filename problems are reported, and collisions require correction.

In **Settings → Renaming**, type `%` to see all supported fields and keep typing to narrow the suggestions. Choose a field with the arrow keys and **Enter** or **Tab**, or click it. **Escape** closes the suggestions; **Ctrl+Space** reopens them inside a field. Completion replaces only that field, preserving the surrounding template. The saved template is used by every rename workflow.

To rename every file shown in the current group:

1. Click **Select all**, then **Rename files…**.
2. Inspect the template and each file's preview; tick the filenames to rename. Unticking a previously queued rename keeps that filename. The template is configured in **Settings**.
3. Choose **Include chosen files and review changes…**. Check the final file list, any previously included files and pending metadata changes, then confirm **Apply changes**.

Cancelling Rename files leaves the session untouched. Cancelling the final Apply summary retains prepared choices for further review. Filename choices from one Rename files action are undone together; write inclusion remains a separate choice.

To rename across the entire scanned library, open **Metadata review…**, choose **All library files**, then **Filename review → Use proposed** and **Filename previews…**. Choose **Add reviewed files**, then **Review & Apply…**. No online lookup is required when existing metadata supplies the filename values. Resolve blocking preview problems before writing.

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
& .\.venv\Scripts\python.exe scripts/test_offscreen.py -q
& .\.venv\Scripts\python.exe -m ruff check .
& .\.venv\Scripts\python.exe -m mypy src scripts
```

The offscreen runner provides a fixed 1920×1080 virtual desktop, Segoe UI and software rendering so layout checks do not depend on your display size. Native Windows painting and process lifecycle checks run separately in CI. Focus, DPI scaling and packaged application behaviour still need manual Windows checks.

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

The Windows [CI workflow](.github/workflows/ci.yml) runs the offline suite on a fixed virtual desktop, separate native Windows painting and process lifecycle checks, static checks, Python package builds and release-content checks. It does not upload or publish releases.

Build source and wheel archives after installing the development dependencies:

```powershell
& .\.venv\Scripts\python.exe -m build --outdir dist/open-source
& .\.venv\Scripts\python.exe scripts/check_release.py dist/open-source
```

The `.tar.gz` contains application source, tests, synthetic fixtures and public project guidance. The `.whl` contains the Python application and licence notices; dependencies and a Python runtime must be installed separately. Package version `0.1.0` is independent of the V1.1 feature-iteration name used above.

Source archives exclude Git history, private `docs/`, local plans, music and runtime state. The checker verifies that boundary and licence metadata; it is not a comprehensive secret scanner or licence audit. Review the contents before publication. For a first public repository, start from the reviewed source archive without copying an existing `.git` directory. Ignore rules do not remove private files from old commits or local snapshot refs; review existing history separately before pushing it, and avoid mirror pushes of local refs.

Once a public repository is chosen, add its URL to the package metadata, enable private vulnerability reporting, and require the Windows CI check before merging changes. Frozen executable distribution requires the additional [dependency licence work](THIRD_PARTY_NOTICES.md#requirements-before-distributing-a-frozen-application).
