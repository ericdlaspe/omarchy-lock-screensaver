# Security policy

## Status and scope

This is an **unofficial, unaudited beta**. The initial beta release is v0.1.0. There is no security certification, guaranteed response time, or maintained backport branch. Report problems against the current code and identify the exact revision and installed Omarchy version. Do not assume an old generated runtime incorporates a newer Omarchy security fix.

The animation and the authenticating lock are different components:

- Omarchy's installed stock lock supplies session-lock surfaces, password entry, PAM, and the authentication decisions. The installer generates a local runtime from those installed sources and applies structurally guarded animation/display-timer edits.
- `ttfx`, the terminal bridge, and the native painter provide animation. They must never receive keyboard events, passwords, PAM responses, or authentication tokens. The PTY's input is not an authentication channel.
- Dismissing an animation or waking a blank display must never unlock the session. An animation-only failure should still allow waking/dismissing the visual and using the normal password interface.
- Loader fallback to the stock lock covers missing/unloadable runtime components at startup. It is not a general recovery guarantee for a crash of the shell, compositor, or session-lock client.

Plugins are unsandboxed code running as your user in a security-sensitive shell. The native module and Python bridge expand that trusted computing base even though neither is supposed to authenticate. Preserving stock PAM code is not proof that integration is secure.

## Report privately

For suspected lock bypass, password/key disclosure, unsafe fallback, command execution, or a security-relevant installer/rollback bug, **do not open a public issue with exploit details**.

If the repository has GitHub private vulnerability reporting enabled, use its **Security → Advisories → Report a vulnerability** flow:

https://github.com/ericdlaspe/omarchy-lock-screensaver/security/advisories/new

Availability is controlled by the repository settings; this document does not claim it is enabled. If unavailable, open a minimal public issue requesting a private reporting channel, with **no exploit, sensitive logs, or credentials**. Wait for a verified private channel before disclosing details. No email address is listed because no public security contact has been verified.

Useful private report details:

- Affected revision/release, Omarchy/Qt/Quickshell/ttfx versions, and whether the runtime was regenerated after an Omarchy update.
- The expected security property, observed behavior, and a minimal reproduction on a disposable test session.
- Whether the session was truly locked, only in preview, or merely showing an idle screensaver.
- Redacted logs and impact, with no password, tokens, private screen contents, or identifying configuration.

Coordinate disclosure before posting reproduction details publicly. Bugs in Omarchy/PAM/compositor code may also need the relevant upstream project's reporting process; avoid duplicating sensitive details in public upstream tickets.

## Safe operation and testing

- Install, update, rebuild, uninstall, and edit plugin files only from an unlocked local GUI session with working IPC. Never force past unknown lock state. SSH is not a supported installation path.
- Never reload or kill the session-lock client while locked. That can leave the compositor in an orphaned-lock failsafe.
- Review changes before running them. Run the installer as your user, not root. Package installation is the separately authorized privileged step; the installer does not fetch dependencies with pip.
- After an Omarchy update, regenerate the local runtime while unlocked. Unsupported stock source shapes should be refused, not silently patched.
- Offscreen/unit tests and preview are not security validation. Real local manual/automatic lock, password unlock, display-off/wake, and multi-monitor checks remain necessary.
- Do not expose a production session or sensitive data just to reproduce a beta lock bug. Keep an independent local recovery path.

For ordinary visual/performance bugs, use the public issue forms after removing sensitive information.
