# v0.1.1 — idle prompt return beta

The lock-screen animation now returns after 30 seconds without input. The timeout clears unfinished password and failure text, pauses during password authentication, and does not unlock or wake the display. Activity is shared across outputs so an inactive screen cannot clear the prompt while another screen is in use.

This release also adds fail-closed compatibility guards and isolated Qt QML regressions. The functional timer tests run through `bin/test` in CI without requiring Quickshell.

## Install or update

Use the documented dependency setup and installer from a **local, unlocked Omarchy session**. Do not update, reload, or edit the live plugin while locked. Cloning/enabling through the plugin manager is not enough: dependencies and the native renderer must be installed/built first.

```sh
git clone --branch v0.1.1 https://github.com/ericdlaspe/omarchy-lock-screensaver.git
cd omarchy-lock-screensaver
omarchy pkg add base-devel qt6-base qt6-declarative python-pyte pkgconf
./bin/install
./bin/status
```

For an existing checkout, unlock locally, select v0.1.1, then rerun the installer with any nondefault options you want to preserve. An existing custom lock is refused unless explicitly replaced. See the README for safe update, recovery, and uninstall instructions.

## Verification limits

The repository-only tests cover runtime generation, authentication-code preservation, native rendering, two simulated lock views sharing activity, repeated prompt timeout, disabled input, and authentication pause/resume. No live lock configuration was changed during release preparation.

This remains an unofficial, unaudited beta. A new physical multi-monitor session, real PAM/password unlock, and a full elapsed display-blank/wake cycle have not been verified for this release. Isolated tests and marketplace review are not security certification.

The display-off default remains 30 minutes; the new 30-second prompt timer is separate from locking and display power. Public packages do not alter the existing custom installation merely by being released.
