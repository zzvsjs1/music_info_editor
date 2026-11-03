# Metadata Polisher

Metadata Polisher is a Windows desktop app for editing music tags and renaming tracks. You can fix a misspelt title, update the artist across an album, or look up a soundtrack on MusicBrainz or VGMdb. You preview the changes before writing them to your files.

It supports **FLAC, MP3, WAV, TAK and MPEG-4 audio** (`.m4a` and audio `.mp4`).

## Run on Windows

If you have a portable build, keep its folder together and open `MetadataPolisher.exe`. Choose a writable location so the app can save its settings and logs.

To run from source, install Python 3.14 and run these commands in PowerShell from the project folder.

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install .
.\.venv\Scripts\python.exe -m metadata_polisher
```

## Edit your first track

If you want a backup of the original, enable **Keep permanent backups** in **Settings** and choose a backup folder before you start.

1. Choose **Browse** and select your music folder. Click an album to see its tracks, then double-click the track you want to edit.
2. In **Metadata review**, double-click the **Final** value beside **Title**. Enter the corrected title and choose **Use value**.
3. Choose **Add selected files**, then **Review & Apply…**. Select the file in the summary to compare its old and new tags.
4. Choose **Apply changes** to write the edit.

You can edit tags offline. For suggestions, select an album and choose **Find Metadata for Selected**. For filenames, select the tracks and choose **Rename files…** to preview their new names. Press **F1** in the library for the full workflow and keyboard shortcuts.

## Contributing

The [contributor guide](CONTRIBUTING.md) covers development setup, tests and portable builds. For security issues, see [security reporting](SECURITY.md).

Metadata Polisher is licensed under the [MIT licence](LICENSE). Bundled dependencies have their own [licence notices](THIRD_PARTY_NOTICES.md).
