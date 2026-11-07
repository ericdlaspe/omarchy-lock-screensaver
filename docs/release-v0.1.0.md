# v0.1.0 — initial beta

The real Omarchy `ttfx` screensaver, rendered inside the secure lock surface rather than in a terminal above it.

- Full-screen effects sized from the actual display and native font metrics.
- Configurable 15–120 FPS target (60 default), with optional actual-paint diagnostics.
- Configurable display-off delay after lock (30 minutes default); existing idle-to-lock timing stays unchanged.
- Builds locally against installed Qt; generates the runtime from the installed Omarchy lock source rather than shipping an old production authentication implementation.
- Unlocked-only installer, explicit custom-lock replacement, backups, rollback, and merge-based uninstall.

## Install from this release

In a local, unlocked Omarchy session:

```sh
git clone --branch v0.1.0 https://github.com/ericdlaspe/omarchy-lock-screensaver.git
cd omarchy-lock-screensaver
omarchy pkg add base-devel qt6-base qt6-declarative python-pyte pkgconf
./bin/install
```

Read README.md and SECURITY.md before enabling a lock-related plugin. Do not run the installer as root. Never install, pull, rebuild, or reload while the computer is locked.

## Validation and scope

Python tests, native Qt tests, real offscreen Quickshell/ttfx rendering, and isolated filesystem install/uninstall transactions were exercised locally. The headless tests do not prove real PAM/compositor security or physical display blanking. The preceding local prototype was also manually checked for full-screen lock animation and successful password unlock.

Tested development host: Omarchy 4.5.0-3, Qt 6.12, Quickshell 0.3.2, ttfx 0.5.0. FPS is hardware/effect dependent; there is no universal minimum-FPS guarantee. The 30-minute timeout is configured, not verified by a full 30-minute elapsed-time test.

**Unofficial, unaudited beta.** Re-run the installer after Omarchy updates to regenerate the runtime from current stock sources. This is a community project, not a package in the official Omarchy repositories or the AUR.

Source archives are provided with SHA256SUMS. No machine-specific Qt binaries are distributed.
