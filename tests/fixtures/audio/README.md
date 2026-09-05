# Audio fixture provenance

The normal integration suite generates its own short PCM WAV files in pytest's
temporary directory using Python's standard `wave` module. The sample values are
simple integer patterns created by the test. They are not recorded or copied music.
The artwork sentinel is a one-pixel PNG generated with `struct` and `zlib`.
No audio or artwork is redistributed in this directory.

`tests/integration/formats/test_format_contract.py` exercises real WAV loading,
tag serialisation, reopening and verification. It checks Unicode, multiple values,
track/disc totals, explicit clearing, native ID3 representation, existing artwork,
an unmanaged comment, the PCM payload and an unrelated RIFF chunk.

The shared assertions in `tests/support/format_contract.py` also cover FLAC,
MP3, MP4 audio and TAK when the opt-in local-media tests supply temporary copies.
Normal tests for formats without a generated encoded fixture use the existing
injected-loader tests and native Mutagen tag containers under `tests/unit/formats/`.
Those tests check physical keys and multi-value representation; they do not prove
real audio-container round trips. The opt-in copy tests provide that additional
check when a representative source is available.

No encoder, downloaded media or private library file is needed by the normal
suite. The shared write assertions reject paths within protected `musics/` and
plan directories. Callers must always supply disposable fixtures or copies.
