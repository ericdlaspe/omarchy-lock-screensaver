# Changelog

## v0.1.2 — configuration-path safety and documented preview

- Refuse unsupported linked or nonregular configuration paths rather than silently replacing links or losing their relationship to managed dotfiles.
- Recheck configuration paths before writes and recovery, with regression tests for link changes during installation and uninstall.
- Replace the undocumented preview with a fresh isolated renderer capture, reproducible source, and explicit upstream attribution.
- Document the installer’s configuration changes and consent boundary more clearly.
- Release preparation does not update or reload the live lock. This remains an unofficial, unaudited beta.

## v0.1.1 — idle prompt return

- Return to the animation after 30 seconds without lock-screen input, clearing unfinished password and failure text without unlocking or waking the display.
- Share activity across lock surfaces so a quiet output cannot clear another output's active password prompt. Pause the countdown during password authentication.
- Refuse conflicting prompt identifiers and unsupported activity-wiring shapes before generating a runtime.
- Run functional Qt QML timer tests in the existing native test harness, including CI without Quickshell.
- The live lock is not updated by publishing a release. Install/update locally while unlocked and repeat real lock/unlock/wake verification. Physical multi-monitor and PAM behavior remain outside the isolated test evidence.

## v0.1.0 — initial beta

### Added

- Actual `ttfx` effects on Omarchy's secure lock surfaces, with screen-sized rendering through a Python terminal bridge and native Qt painter.
- Configurable 60 FPS target and a default 30-minute locked-screen display-blanking interval.
- Local build/install, status, dry-run, and merge-based uninstall entry points.
- Local generation from the installed stock lock, guarded compatibility checks, and startup stock-lock fallback.
- Explicit custom-lock replacement opt-in, unlocked-session checks, and restoration state without changing existing idle-to-lock timings.
- Python/native test entry point, Arch Linux CI configuration, issue forms, and beta/security documentation.

### Limits

- Unofficial and not security-audited; stock authentication is retained, not replaced with an animation-specific unlock path.
- Delivered frame rate depends on hardware, resolution, scaling, and effects. No universal minimum FPS is promised.
- The 30-minute blanking setting is not a claim that a full elapsed-time blank/wake test has passed.
- Offscreen tests cannot validate session-lock/PAM/compositor behavior. Omarchy updates require rerunning the installer while unlocked.
- A configured CI workflow is not evidence of a successful run; check the actual commit's checks on GitHub.
