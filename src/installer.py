#!/usr/bin/env python3
"""Install and restore the user-owned Omarchy secure screensaver plugin."""
from __future__ import annotations

import argparse
import copy
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import uuid

PLUGIN_ID = "ericdlaspe.lock-screensaver"
CONFIG_KEYS = ("plugins", "disabledPlugins", "cloneSourceRestores")


class InstallError(RuntimeError):
    """An unsafe or unsupported operation, with no silent fallback."""


def read_command_json(command, runner=subprocess.run):
    try:
        result = runner(command, capture_output=True, text=True, timeout=15, check=False)
        if result.returncode:
            raise InstallError(f"Command failed: {' '.join(command)}")
        return json.loads(result.stdout)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise InstallError(f"Could not read {' '.join(command)}: {type(error).__name__}") from error


def ensure_unlocked(runner=subprocess.run):
    """Require positive unlocked evidence; never unload a live lock client."""
    state = read_command_json(["omarchy-shell", "lock", "status"], runner)
    if (not isinstance(state, dict) or not all(state.get(key) is False
                                              for key in ("locked", "requested", "secure"))
            or state.get("pending", False) is not False):
        raise InstallError("Unlock the computer locally before changing the lock plugin")
    try:
        compositor = runner(["omarchy-hyprland-session-locked"], capture_output=True,
                            text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        raise InstallError("Could not verify the compositor lock state") from error
    if compositor.returncode != 1:
        raise InstallError("The compositor is locked or its state is unknown")
    return state


class IdlePause:
    """Restore the exact prior enabled/disabled idle state on every exit path."""

    def __init__(self, runner=subprocess.run):
        self.runner = runner
        self.changed = False

    def __enter__(self):
        state = read_command_json(["omarchy-shell", "idle", "status"], self.runner)
        if not isinstance(state, dict) or not isinstance(state.get("enabled"), bool):
            raise InstallError("Could not verify idle state; leaving it untouched")
        if state["enabled"]:
            self.changed = True
            try:
                run_checked(["omarchy-shell", "idle", "disable"], self.runner)
            except Exception:
                self.restore()
                raise
        return self

    def restore(self):
        if self.changed:
            run_checked(["omarchy-shell", "idle", "enable"], self.runner)
            state = read_command_json(["omarchy-shell", "idle", "status"], self.runner)
            if not isinstance(state, dict) or state.get("enabled") is not True:
                raise InstallError("Automatic idle was NOT restored. Run: omarchy-shell idle enable")
            self.changed = False

    def __exit__(self, *_error):
        self.restore()
        return False


def run_checked(command, runner=subprocess.run, timeout=120):
    try:
        result = runner([str(part) for part in command], capture_output=True, text=True,
                        timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        raise InstallError(f"Could not run {command[0]}: {type(error).__name__}") from error
    if result.returncode:
        raise InstallError(f"{' '.join(map(str, command))} failed: {result.stderr.strip()[:1000]}")
    return result


def entry_id(value):
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return value.get("id")
    raise InstallError("Plugin entries must be strings or objects with an id")


def lock_subset(config, ids):
    return {key: [entry for entry in config.get(key, []) if entry_id(entry) in ids]
            for key in CONFIG_KEYS}


def enable_configuration(config, active_lock_ids):
    """Change only the lock selection; record a mergeable uninstall snapshot."""
    result = copy.deepcopy(config)
    ids = list(dict.fromkeys([PLUGIN_ID, "omarchy.lock", *active_lock_ids]))
    snapshot = {"version": 1, "ids": ids, "before": {}}
    for key in CONFIG_KEYS:
        current = result.get(key, [])
        if not isinstance(current, list):
            raise InstallError(f"shell.json {key} must be a list")
        snapshot["before"][key] = {
            "present": key in config,
            "entries": [[index, copy.deepcopy(entry)] for index, entry in enumerate(current)
                        if entry_id(entry) in ids],
        }
    result["plugins"] = [entry for entry in result.get("plugins", [])
                         if entry_id(entry) not in ids] + [{"id": PLUGIN_ID}]
    disabled = [entry for entry in result.get("disabledPlugins", []) if entry_id(entry) != PLUGIN_ID]
    for lock_id in ["omarchy.lock", *active_lock_ids]:
        if lock_id not in [entry_id(entry) for entry in disabled]:
            disabled.append(lock_id)
    result["disabledPlugins"] = disabled
    result["cloneSourceRestores"] = [entry for entry in result.get("cloneSourceRestores", [])
                                     if entry_id(entry) not in ids] + [PLUGIN_ID]
    snapshot["expected"] = lock_subset(result, ids)
    return result, snapshot


def restore_configuration(config, snapshot):
    """Undo only our lock-selection edits; never overwrite later bar/idle edits."""
    ids = snapshot["ids"]
    if lock_subset(config, ids) != snapshot["expected"]:
        raise InstallError("Lock selection changed since installation; refusing to overwrite it")
    result = copy.deepcopy(config)
    for key in CONFIG_KEYS:
        prior = snapshot["before"][key]
        values = [entry for entry in result.get(key, []) if entry_id(entry) not in ids]
        for index, entry in prior["entries"]:
            values.insert(min(index, len(values)), copy.deepcopy(entry))
        if values or prior["present"]:
            result[key] = values
        else:
            result.pop(key, None)
    return result


@dataclass(frozen=True)
class Paths:
    home: Path
    data: Path
    state: Path
    plugins: Path
    shell_config: Path
    omarchy: Path

    @classmethod
    def from_environment(cls, home=None, env=None):
        env = os.environ if env is None else env
        home = Path(home or env.get("HOME") or Path.home()).expanduser().resolve()
        return cls(home,
                   Path(env.get("XDG_DATA_HOME") or home / ".local/share") / "omarchy-lock-screensaver",
                   Path(env.get("XDG_STATE_HOME") or home / ".local/state") / "omarchy-lock-screensaver",
                   home / ".config/omarchy/plugins",
                   home / ".config/omarchy/shell.json",
                   Path(env.get("OMARCHY_PATH") or "/usr/share/omarchy"))

    @property
    def active(self):
        return self.state / "active.json"

    @property
    def snapshot(self):
        return self.state / "uninstall.json"

    @property
    def plugin(self):
        return self.plugins / PLUGIN_ID


def install(*args, **kwargs):
    from deployment import install as deploy
    return deploy(*args, **kwargs)


def uninstall(*args, **kwargs):
    from deployment import uninstall as restore
    return restore(*args, **kwargs)
