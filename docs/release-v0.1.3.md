# v0.1.3 — stop animation on powered-off displays

The lock screensaver now stops its animation bridge and `ttfx` effect when the corresponding Hyprland output is DPMS-off. It clears cached frames and pauses paint-rate sampling, then restarts the effect when the output wakes. Each lock surface follows its own monitor; preview windows without a matching output follow whether any enabled output is lit.

Output power is checked every two seconds while the screensaver is selected. This leaves a small periodic IPC check running while animation is stopped. If compositor IPC fails, the last known power state is retained. This change reduces unnecessary animation work; no measured wattage or fan-noise reduction is claimed.

Password authentication, the 30-second prompt timeout, and the configurable display-off delay are unchanged.

## Install or update

From a local, unlocked Omarchy session, select `v0.1.3` and rerun `./bin/install` with your desired options. See the README for dependencies, custom-lock replacement, and recovery. Publishing this release does not change any installed live lock.

## Validation

`./bin/test` passed locally: 107 Python tests with no skips, plus 7 native Qt checks.

The regression test runs the real bridge and `ttfx` under an isolated offscreen Quickshell with simulated compositor power replies. It checks that animation starts, both animation processes stop after power-off, and animation restarts on wake. This does not verify physical multi-monitor power transitions, PAM unlock, or electrical power consumption.
