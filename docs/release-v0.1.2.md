# v0.1.2 — configuration-path safety and documented preview

This beta fixes an installer bug that could replace a symlinked `shell.json` with a regular file. Linked and unusual configuration paths are now refused instead of silently converted. The installer does not follow the link to edit its target, remove the link, or repair an existing dotfile layout automatically.

The repository now has a fresh, documented preview rendered with the actual native painter in an isolated offscreen process. It is not a capture of a live secure-lock session. Its source, reproduction recipe, dependency versions, and attribution are recorded in [preview provenance](https://github.com/ericdlaspe/omarchy-lock-screensaver/blob/v0.1.2/docs/preview-provenance.md).

The lock animation, 30-second idle prompt return, authentication wiring, and 30-minute default locked-screen blanking interval are unchanged.

## Install or update

Use the documented dependency setup and installer from a **local, unlocked Omarchy session**. Do not update, reload, or edit the live plugin while locked. Cloning/enabling through the plugin manager is not enough: dependencies and the native renderer must be installed/built first.

```sh
git clone --branch v0.1.2 https://github.com/ericdlaspe/omarchy-lock-screensaver.git
cd omarchy-lock-screensaver
omarchy pkg add base-devel qt6-base qt6-declarative python-pyte pkgconf
./bin/install --dry-run
./bin/install
./bin/status
```

For an existing checkout, unlock locally, select v0.1.2, then rerun the installer with any nondefault options you want to preserve. An existing custom lock is refused unless explicitly replaced. See the README for the exact configuration changes, safe update, recovery, and uninstall instructions.

If your configuration is managed through links, leave it unchanged and review the refusal. This release intentionally does not offer a force option to break or follow those links. Do not change dotfile management solely to bypass a safety check without reviewing the consequences.

## Verification limits

Local validation passed 106 Python tests with no skips and 7 native Qt checks, including 17 new configuration-path regressions. Independent review reproduced the original link issues and verified the fixes.

Repository-only tests cover linked paths, guarded writes, install/uninstall and recovery using temporary filesystem fixtures and simulated desktop IPC. The existing runtime-generation, native-renderer, and prompt-timer tests remain part of the suite. No live lock configuration is changed by testing or publishing the release.

Path checks protect supported operations from detected changes; they are not a transaction with every other editor or a defense against hostile code running as the same user. Do not edit configuration or switch lock plugins concurrently with installation or removal.

This remains an unofficial, unaudited beta. A new physical multi-monitor session, real PAM/password unlock, and a full elapsed display-blank/wake cycle have not been verified for this release. The preview and marketplace validation are not security certification.
