# Contributing

This beta runs in a security-sensitive shell. Keep animation changes separate from authentication, prefer small reviewable patches, and read [SECURITY.md](SECURITY.md) before testing or reporting lock-related behavior.

## Development setup

On an existing Omarchy installation, install build/test dependencies explicitly:

```bash
omarchy pkg add base-devel qt6-base qt6-declarative python-pyte pkgconf
```

Python, `qmake6`, `make`, and a C++ compiler must be available. An alternative interpreter must already have `pyte`; do not add a network-driven dependency installer. Quickshell and `ttfx` come from the Omarchy environment for real integration tests. Do not modify the system's `/usr/share/omarchy/` files.

From the repository root:

```bash
./bin/test
```

This is the shared test entry point used by CI: Python unit tests, native build, and offscreen native renderer tests. Do not claim success from a command that was not run. Include the command, revision, environment, and actual result in a pull request; distinguish skipped or unavailable tests from passing tests. Do not run `bin/install` in CI.

The workflow uses an Arch Linux container, installs build/test packages before checkout, and gives the repository token only `contents: read`. It does not publish artifacts, change repository settings, or access repository secrets. A local passing test is not evidence of a successful remote run.

## Review invariants

- Authentication stays with the currently installed Omarchy stock lock and PAM. Never send keystrokes, password text, or authentication messages to the animation bridge/native renderer or its PTY.
- Animation dismissal, animation failure, and display wake must never be treated as authentication success.
- Generate production runtimes from installed stock sources. Refuse unsupported or ambiguous patch shapes. Compatibility fixtures are test inputs only, not a production authentication bundle.
- Keep startup stock fallback explicit; do not claim it handles arbitrary shell/compositor crashes.
- Check positive unlocked state and working local shell/compositor IPC before changing the live lock. Preserve temporary idle state with failure-safe cleanup.
- Touch only the intended lock selection. Preserve later unrelated bar/idle edits on uninstall/rollback and refuse conflicting lock-selection changes.
- Honor `HOME`/XDG paths, safely handle spaces, and do not hard-code a developer's home directory, interpreter path, font, or monitor geometry.
- Bound external frame data, parser sizes, and rendering work. Treat terminal output as untrusted data, never shell commands or QML markup.
- Keep diagnostics opt-in and free of passwords, key events, tokens, and unnecessary personal paths.
- Preserve upstream copyright/license notices in copied/generated sources. See [NOTICE](NOTICE).

## Local integration testing

**Never update, reload, kill, or edit a live lock plugin while the session is locked.** Plugin files can trigger hot reload. Work in a local unlocked session, keep an independent recovery path, and use a disposable test session for risky changes. Do not type passwords into logs or tool prompts.

For changes affecting lock integration, document each check separately:

1. Installer dry-run, actual installation, and status in an unlocked local GUI session.
2. Preview opens, produces distinct frames, closes cleanly, and stops bridge/effect processes. Preview is visual testing, not secure lock validation.
3. Real manual and automatic secure lock; wake/dismiss animation; normal local password unlock. Test the installed authentication methods without exposing their secrets.
4. Monitor layout/scaling and shell responsiveness under actual full-screen effects.
5. Locked-screen display blank/wake behavior. A configured 30-minute interval is not an elapsed-time result: record duration actually observed.
6. Merge-based uninstall/rollback preserves unrelated configuration and restores the previous lock selection.

Do not infer real-lock FPS from preview. Report target and measured delivered FPS separately, with hardware/resolution/scaling/effect and sampling method. Do not extrapolate a limited central grid measurement to full-screen performance or promise an FPS floor.

Headless/offscreen tests cannot reproduce the real session-lock/PAM/compositor boundary, physical display blanking, or all rendering backends. List these limits explicitly instead of replacing missing integration evidence with simulated results.

## Issues and pull requests

Use the bug or feature forms for non-sensitive reports. Provide a minimal reproduction and redact usernames, private paths, monitor identifiers if sensitive, screenshots, and logs. For security issues, use the private-reporting guidance in [SECURITY.md](SECURITY.md), not a public reproduction.

For a pull request, include motivation, changed behavior, actual test results, known untested cases, and any migration/recovery implications. Add regression tests for behavior changes and update the README/changelog when user-facing behavior changes. Do not include generated runtimes, local configuration, credentials, binaries, or desktop captures containing personal information.

Contributions are offered under this project's MIT license, with upstream notices retained. No separate contributor license agreement is required by this document.
