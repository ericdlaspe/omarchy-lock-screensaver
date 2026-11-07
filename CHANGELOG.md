# Changelog

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
