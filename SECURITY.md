# Security policy

Metadata Polisher reads local media and can write approved metadata and filenames. A security report is useful for issues such as unintended file access or writes, credential disclosure, unsafe handling of media or provider responses, or a way to bypass the final Apply review boundary.

## Reporting a vulnerability

Please do not disclose exploitable details, personal files or credentials in a public issue or pull request.

If this repository offers GitHub private vulnerability reporting, use that private reporting option. Its availability depends on repository settings. If no private reporting option is available, open a public issue containing only a request for a private security contact, with no vulnerability details, and wait for a maintainer to establish a private channel.

Once a private channel is available, include:

- The application version or source revision, Windows version and whether you used source or a packaged build.
- The affected component and the impact you observed.
- Minimal reproduction steps using generated or sanitised data.
- Any relevant configuration, with secrets and personal paths removed.
- A proposed fix or workaround, if you have one.

Reports are handled on a best-effort basis; there is no guaranteed response time. Include the version you tested so maintainers can assess affected releases and the current source.

## Protecting diagnostic information

Inspect logs, diagnostic summaries, Apply reports and screenshots before sharing them. They can contain filenames, library paths, metadata and provider query details even when credential redaction works correctly. Use placeholders for identifying information and remove tokens, cookies, passwords, contact addresses and proxy credentials.

If a secret has been disclosed, revoke or rotate it with its provider. Removing it from a visible issue or the latest commit does not invalidate it or remove copies from Git history.

Test suspicious media or write behaviour only with disposable files in a controlled location. Keep original music and personal configuration out of public reports and repository fixtures.
