#!/usr/bin/env python3
"""Non-root install, rollback and status commands for the Omarchy extension."""
import argparse
import json
import os
from pathlib import Path
import sys

import installer
from deployment import read_json


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="command", required=True)
    install = actions.add_parser("install", help="Build and enable while the computer is unlocked")
    install.add_argument("--fps", type=int, default=60, help="Animation target, 15–120 (default: 60)")
    install.add_argument("--blank-minutes", type=float, default=30,
                         help="Locked display-off delay, 0.5–1440 minutes (default: 30)")
    install.add_argument("--metrics", action="store_true", help="Log three short paint-rate samples")
    install.add_argument("--python", default=sys.executable,
                         help="Python interpreter with pyte installed; no downloads are performed")
    install.add_argument("--replace-existing-lock", action="store_true",
                         help="Explicitly replace an existing user lock clone (backed up for uninstall)")
    install.add_argument("--dry-run", action="store_true", help="Show planned settings without writing files")
    uninstall = actions.add_parser("uninstall", help="Restore previous lock selection; retain checkout/backups")
    uninstall.add_argument("--dry-run", action="store_true")
    actions.add_parser("status", help="Read installation state as JSON (no desktop changes)")
    options = parser.parse_args(argv)
    paths = installer.Paths.from_environment()
    project = Path(__file__).resolve().parents[1]
    try:
        if options.command != "status":
            if any(os.environ.get(name) for name in ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY")):
                raise installer.InstallError("SSH installation is unsupported; use an unlocked local graphical terminal")
            if os.geteuid() == 0:
                raise installer.InstallError("Run this installer as your desktop user, never with sudo/root")
        if options.command == "install":
            result = installer.install(paths, options, project)
        elif options.command == "uninstall":
            result = installer.uninstall(paths, options)
        else:
            config = read_json(paths.shell_config, {})
            active = read_json(paths.active, {})
            enabled = installer.PLUGIN_ID in [installer.entry_id(entry)
                                              for entry in config.get("plugins", [])]
            result = {"plugin": installer.PLUGIN_ID, "enabled": enabled,
                      "configured": bool(active), "fps": active.get("fps"),
                      "blankSeconds": active.get("blankSeconds"),
                      "idle": config.get("idle", "stock defaults")}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (installer.InstallError, OSError, ValueError) as error:
        print(f"lock-screensaver: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
