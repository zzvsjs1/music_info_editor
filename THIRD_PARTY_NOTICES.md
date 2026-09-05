# Third-party software and data

Metadata Polisher's original project material is offered under the [MIT licence](LICENSE).
That permission does not replace the licences of dependencies, provider data, music,
artwork or other third-party material. The MIT notice must accompany copies or
substantial portions of the original project material. See the
[standard MIT terms](https://opensource.org/license/mit).

The first public release is intended to contain project source only. Dependencies
are installed separately. **A frozen Windows application is not an MIT-only
distribution.** The binary release requirements below remain unresolved until
they have been completed for the exact files being distributed. This document
records an inventory and release requirements; it does not certify legal compliance.

## Runtime dependency inventory

The versions and licence identifiers below were read from the existing development
environment's installed distribution metadata on 2026-09-05. They are an observed
snapshot, not a dependency lock or a complete inventory of a future executable.
Recheck them after dependency changes. Full licence texts and copyright notices
in each installed distribution take precedence over these short identifiers.

| Distribution | Observed version | Declared licence | Role / primary source |
| --- | --- | --- | --- |
| Mutagen | 1.48.1 | GPL-2.0-or-later | Audio tag reading and writing; [Mutagen documentation](https://mutagen.readthedocs.io/en/latest/) |
| PySide6 | 6.11.2 | LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only | Python GUI bindings; [Qt licensing](https://doc.qt.io/qt-6/licensing.html) |
| PySide6_Essentials | 6.11.2 | LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only | Required PySide6 component; [Qt licensing](https://doc.qt.io/qt-6/licensing.html) |
| PySide6_Addons | 6.11.2 | LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only | Installed by PySide6; individual Qt modules may have narrower licence choices; [Qt licensing](https://doc.qt.io/qt-6/licensing.html) |
| shiboken6 | 6.11.2 | LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only | Binding runtime; [Qt for Python third-party licences](https://doc.qt.io/qtforpython-6/licenses.html) |
| HTTPX | 0.28.1 | BSD-3-Clause | Provider HTTP requests; [upstream licence](https://github.com/encode/httpx/blob/master/LICENSE.md) |
| RapidFuzz | 3.14.6 | MIT | Text matching; [upstream licence](https://github.com/rapidfuzz/RapidFuzz/blob/main/LICENSE) |
| AnyIO | 4.15.0 | MIT | HTTPX dependency |
| certifi | 2026.7.22 | MPL-2.0 | Certificate bundle; [upstream notice](https://github.com/certifi/python-certifi/blob/master/LICENSE) |
| httpcore | 1.0.9 | BSD-3-Clause | HTTPX dependency |
| h11 | 0.16.0 | MIT | HTTP protocol dependency |
| idna | 3.19 | BSD-3-Clause | Internationalised domain names |
| typing_extensions | 4.16.0 | PSF-2.0 | AnyIO dependency for the observed Python version |

The Qt package-level expressions are not a licence clearance for every DLL,
plugin, example or add-on in those packages. Qt and Qt for Python include
third-party components with their own notices. Inspect what the build actually
collects, including libraries pulled in indirectly. See
[Qt licensing](https://doc.qt.io/qt-6/licensing.html) and
[Qt for Python third-party licences](https://doc.qt.io/qtforpython-6/licenses.html).

## Mutagen and the MIT source licence

Mutagen is a required library imported by the audio adapters. Its upstream
licence is GPL version 2 or later. MIT permission for Metadata Polisher's original
code does not make Mutagen permissively licensed. The GNU project's guidance says
that modules in a combined program may use GPL-compatible licences, while the
combination using a GPL library must satisfy the GPL. This is the basis for the
project's conservative binary release policy. See
[Mutagen's licence statement](https://mutagen.readthedocs.io/en/latest/) and
[GNU's library guidance](https://www.gnu.org/licenses/gpl-faq.html.en#IfLibraryIsGPL).

Publishing the original source under MIT preserves that permission for the
original code. It is not permission to redistribute a complete application with
Mutagen under MIT alone. If the project later requires a wholly permissive runtime
distribution, the Mutagen dependency will need a separate implementation and
licensing decision; it has not been replaced as part of this source release.

## Requirements before distributing a frozen application

The current source release does not include dependency source archives or a
complete set of binary redistribution notices. Adding this file to a build is
useful attribution, but does not complete these requirements.

1. Record the exact application revision, Python version, build instructions,
   dependency versions and hashes, and every collected library, DLL, plugin and
   runtime hook. Distinguish build-only tools from code shipped in the output.
2. Determine a GPL-compatible distribution basis for the combined application
   containing Mutagen. Preserve the MIT notice on original project code and all
   upstream notices. Supply the applicable GPL text and fulfil its corresponding
   source requirements for the combined work. Retain the actual source and build
   materials for the released versions; a link to a changing upstream branch is
   not a substitute for an assessed source-delivery arrangement. See
   [GNU's library guidance](https://www.gnu.org/licenses/gpl-faq.html.en#IfLibraryIsGPL).
3. Identify the licence option for each shipped Qt component. Under the LGPL
   option, provide the required notices, licence texts, corresponding library
   source and a usable mechanism and instructions for replacing the libraries.
   An onedir layout alone does not demonstrate that these conditions are met.
   Qt's guidance also says the source-delivery arrangement must be under the
   distributor's control. See [Qt's LGPL obligations](https://www.qt.io/development/open-source-lgpl-obligations)
   and [Qt's source distribution guidance](https://www.qt.io/development/download-open-source).
4. Include full licence texts and copyright notices for all other shipped
   components, including Python, certifi, Qt's third-party libraries and the
   PyInstaller runtime components. Satisfy applicable source and attribution
   requirements for their exact versions. See [Python's copyright information](https://www.python.org/doc/copyright/),
   [certifi's notice](https://github.com/certifi/python-certifi/blob/master/LICENSE)
   and [Qt for Python's third-party notices](https://doc.qt.io/qtforpython-6/licenses.html).
5. Review the finished release folder and accompanying source/notice materials
   together before uploading it. Do not describe the complete bundle as MIT-only
   or treat a successful local build as proof of licence compliance.

## Build and development tools

PyInstaller 6.22.2 was installed at the time of this inventory. Its GPL licence
includes a bootloader exception permitting distribution of the compiled
bootloader and related embedded files in other programs. Its runtime hooks use
Apache-2.0. The exception concerns PyInstaller's files; it does not waive the
licences of libraries it bundles. See [PyInstaller's licence](https://github.com/pyinstaller/pyinstaller/blob/develop/COPYING.txt).

The installed `pyinstaller-hooks-contrib` 2026.7 licence file distinguishes
GPL-2.0-or-later standard hooks from Apache-2.0 runtime hooks. Other observed build
dependencies were setuptools 84.0.0 (MIT), packaging 26.3 (Apache-2.0 OR
BSD-2-Clause), altgraph 0.17.5 (MIT), pefile 2024.8.26 (MIT), and pywin32-ctypes
0.2.3 (BSD-3-Clause). These entries are a build-environment snapshot, not a claim
that all of these packages are present in the executable.

The development extra also installs build, mypy, pytest, pytest-cov, pytest-qt and Ruff.
Their installation does not transfer their licences to original project code.
If redistributing a development environment or any tool's files, inventory its
dependencies and retain its own notices as well.

## Provider data and test fixtures

MusicBrainz and VGMdb are external services. The project's MIT licence grants no
additional rights over their returned metadata, site content, logos or artwork.
MusicBrainz distinguishes CC0 core database data from supplementary data under
CC BY-NC-SA 3.0; consult its [data licence](https://musicbrainz.org/doc/About/Data_License)
before redistributing retrieved data. Do not assume VGMdb responses are MIT
licensed merely because the application can read them.

The checked-in MusicBrainz JSON fixtures use small, constructed records with
conspicuous sample entity identifiers and example album/track values; they are
not full database exports. Relationship-type identifiers model the provider
schema. Some artist names are public names used as test values. Preserve this
distinction when adding new fixtures: use minimal synthetic examples and record
the source and redistribution basis for anything copied from a provider.

The [VGMdb fixture notes](tests/fixtures/providers/vgmdb/README.md) record the
observed HTML shapes and describe shortened or invented values, without artwork,
cookies or challenge tokens. The [audio fixture notes](tests/fixtures/audio/README.md)
describe generated PCM data and artwork sentinels. Private recordings belong in
the ignored `musics/` directory and must not be added to public fixtures.

The [loopback TLS fixture notes](tests/fixtures/network/README.md) explain why a
public test private key is checked in: it belongs only to a synthetic local
HTTPS test service. It is not an application credential and must never be used
for a real service.
