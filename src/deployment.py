"""Transactional deployment; no installed authentication source is vendored."""
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import uuid

from installer import (PLUGIN_ID, IdlePause, InstallError, entry_id,
                       enable_configuration, restore_configuration, lock_subset,
                       read_command_json, run_checked, ensure_unlocked)


def atomic_bytes(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def atomic_json(path, payload):
    atomic_bytes(path, (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode())


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as error:
        raise InstallError(f"Cannot read valid JSON from {path}") from error


def configuration_state(path):
    """Check and back up the same configuration bytes, not two racing reads."""
    try:
        payload = Path(path).read_bytes()
    except FileNotFoundError:
        payload = None
    except OSError as error:
        raise InstallError(f"Cannot read configuration from {path}") from error
    try:
        config = json.loads(payload) if payload is not None else {}
    except ValueError as error:
        raise InstallError(f"Cannot read valid JSON from {path}") from error
    if not isinstance(config, dict):
        raise InstallError("shell.json must contain an object")
    return config, payload


def active_locks(runner):
    catalog = read_command_json(["omarchy", "plugin", "list", "--json"], runner)
    if not isinstance(catalog, list):
        raise InstallError("Could not inspect installed lock plugins")
    return [entry["id"] for entry in catalog if isinstance(entry, dict)
            and entry.get("enabled") is True and entry.get("clonedFrom") == "omarchy.lock"
            and entry.get("id") != PLUGIN_ID]


def prepare_plugin_checkout(paths, project, *, runner=None):
    if paths.plugin.resolve() == project.resolve():
        return None
    if paths.plugin.exists():
        manifest = read_json(paths.plugin / "manifest.json", {})
        if manifest.get("id") != PLUGIN_ID or (paths.plugin / ".git").exists():
            raise InstallError(f"{paths.plugin} is not our managed copy. Run its own bin/install instead")
    paths.plugins.mkdir(parents=True, exist_ok=True)
    temporary = paths.plugins / (".lock-screensaver-" + uuid.uuid4().hex)
    previous = None
    try:
        shutil.copytree(project, temporary, ignore=shutil.ignore_patterns(
            ".git", ".venv", "__pycache__", "build", "dist", "*.pyc", ".pytest_cache"))
        if runner is not None:
            ensure_unlocked(runner)
        if paths.plugin.exists():
            previous = paths.state / "backups" / ("source-" + uuid.uuid4().hex)
            previous.parent.mkdir(parents=True, exist_ok=True)
            os.replace(paths.plugin, previous)
        os.replace(temporary, paths.plugin)
    except Exception as error:
        if previous is not None and previous.exists() and not paths.plugin.exists():
            if runner is None or not safe_to_rollback(runner):
                raise InstallError("Source deployment failed while locked/unknown; do not reload the lock. "
                                   f"Recovery backup: {previous}") from error
            os.replace(previous, paths.plugin)
        raise
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return previous


def build_generation(paths, options, project, runner):
    from runtime import generate_runtime
    interpreter = shutil.which(str(options.python))
    if not interpreter:
        raise InstallError(f"Python interpreter not found: {options.python}")
    run_checked([interpreter, "-c", "import pyte"], runner)
    generation = paths.data / "generations" / uuid.uuid4().hex
    generation.mkdir(parents=True)
    try:
        renderer = generation / "renderer"
        run_checked([project / "scripts/build-native", renderer], runner, timeout=180)
        bridge = generation / "bridge.py"
        shutil.copy2(project / "src/bridge.py", bridge)
        metadata = generate_runtime(paths.omarchy / "shell/plugins/lock", generation / "runtime",
                                    project / "qml/SecureSaver.qml.in",
                                    python_executable=Path(interpreter), bridge_script=bridge,
                                    renderer_dir=renderer, fps=options.fps,
                                    blank_seconds=int(options.blank_minutes * 60), metrics=options.metrics)
        formatter = shutil.which("qmlformat") or "/usr/lib/qt6/bin/qmlformat"
        for name in ("Service.qml", "LockView.qml", "SecureSaver.qml"):
            run_checked([formatter, generation / "runtime" / name], runner)
        metadata.update(serviceUrl=(generation / "runtime/Service.qml").as_uri(),
                        fps=options.fps, blankSeconds=int(options.blank_minutes * 60),
                        installedAt=datetime.now(timezone.utc).isoformat())
        atomic_json(generation / "metadata.json", metadata)
        return metadata
    except Exception:
        shutil.rmtree(generation)
        raise


def restore_file(path, old_bytes):
    if old_bytes is None:
        Path(path).unlink(missing_ok=True)
    else:
        atomic_bytes(path, old_bytes)


def persist_backup(paths, files, previous_checkout=None, *, backup=None):
    """Keep exact pre-write pointers and absence evidence for manual recovery."""
    if backup is None:
        backup = paths.state / "backups" / uuid.uuid4().hex
        backup.mkdir(parents=True, mode=0o700)
    for name, payload in files.items():
        restore_file(backup / name, payload)
    atomic_json(backup / "recovery.json", {
        "version": 1,
        "files": {name: {"present": payload is not None} for name, payload in files.items()},
        "sourceBackup": str(previous_checkout) if previous_checkout is not None else None,
    })
    return backup


def safe_to_rollback(runner):
    try:
        ensure_unlocked(runner)
        return True
    except (InstallError, OSError, subprocess.SubprocessError):
        return False


READINESS_TIMEOUT = 15.0
READINESS_INTERVAL = 0.1


def wait_for_readiness(runner, *, custom=False, timeout=None, interval=None):
    """Poll read-only IPC within one deadline, including each command's timeout."""
    timeout = READINESS_TIMEOUT if timeout is None else timeout
    interval = READINESS_INTERVAL if interval is None else interval
    deadline = time.monotonic() + timeout

    def bounded_runner(command, **kwargs):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise InstallError("Lock readiness deadline expired")
        kwargs["timeout"] = min(kwargs.get("timeout", remaining), remaining)
        return runner(command, **kwargs)

    while True:
        try:
            lock_state = read_command_json(["omarchy-shell", "lock", "status"], bounded_runner)
            plugin_state = (read_command_json(["omarchy-shell", "lock-screensaver", "status"], bounded_runner)
                            if custom else None)
            if (isinstance(lock_state, dict) and lock_state.get("passwordPam") is True
                    and (not custom or (isinstance(plugin_state, dict)
                                        and plugin_state.get("ready") is True
                                        and plugin_state.get("custom") is True))):
                return
        except InstallError:
            pass  # Startup can briefly have no IPC, or incomplete status JSON.
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise InstallError("Lock screen readiness deadline expired")
        time.sleep(min(interval, remaining))


def install(paths, options, project, *, runner=subprocess.run, build=None):
    ensure_unlocked(runner)
    if not 15 <= options.fps <= 120 or not 30 <= options.blank_minutes * 60 <= 86400:
        raise InstallError("FPS must be 15–120 and display-off delay 0.5–1440 minutes")
    original = read_json(paths.shell_config, {})
    if not isinstance(original, dict):
        raise InstallError("shell.json must contain an object")
    previous = active_locks(runner)
    if previous and not options.replace_existing_lock:
        raise InstallError("An existing lock clone is active (" + ", ".join(previous)
                           + "). Review it, then use --replace-existing-lock to replace it")
    desired, snapshot = enable_configuration(original, previous)
    prior_snapshot = read_json(paths.snapshot)
    if prior_snapshot and PLUGIN_ID in [entry_id(entry) for entry in original.get("plugins", [])]:
        if lock_subset(original, prior_snapshot["ids"]) != prior_snapshot["expected"]:
            raise InstallError("The installed lock configuration changed; review before updating")
        snapshot = prior_snapshot
    if options.dry_run:
        return {"dryRun": True, "plugin": PLUGIN_ID, "replaces": previous,
                "fps": options.fps, "blankSeconds": int(options.blank_minutes * 60),
                "idle": original.get("idle", "unchanged")}
    if paths.plugin.exists() and paths.plugin.resolve() != project.resolve():
        manifest = read_json(paths.plugin / "manifest.json", {})
        if manifest.get("id") != PLUGIN_ID or (paths.plugin / ".git").exists():
            raise InstallError(f"Use the installed checkout: {paths.plugin}/bin/install")
    paths.state.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (paths.state / "install.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with IdlePause(runner):
            metadata = (build or build_generation)(paths, options, project, runner)
            run_checked(["omarchy", "plugin", "validate", project], runner)
            ensure_unlocked(runner)
            previous_checkout = prepare_plugin_checkout(paths, project, runner=runner)
            ensure_unlocked(runner)
            current = read_json(paths.shell_config, {})
            if (set(active_locks(runner)) != set(previous) or not isinstance(current, dict)
                    or lock_subset(current, snapshot["ids"]) != lock_subset(original, snapshot["ids"])):
                raise InstallError("Lock selection changed during preparation; preserving your edit")
            desired, commit_snapshot = enable_configuration(current, previous)
            if snapshot is not prior_snapshot:
                snapshot = commit_snapshot
            old_config = paths.shell_config.read_bytes() if paths.shell_config.exists() else None
            old_active = paths.active.read_bytes() if paths.active.exists() else None
            old_snapshot = paths.snapshot.read_bytes() if paths.snapshot.exists() else None
            backup = persist_backup(paths, {"shell.json": old_config, "active.json": old_active,
                                            "uninstall.json": old_snapshot}, previous_checkout)
            config_committed = active_committed = snapshot_committed = False
            try:
                atomic_json(paths.snapshot, snapshot)
                snapshot_committed = True
                atomic_json(paths.active, metadata)
                active_committed = True
                for attempt in range(3):
                    ensure_unlocked(runner)
                    if set(active_locks(runner)) != set(previous):
                        raise InstallError("Lock selection changed during preparation; preserving your edit")
                    latest, latest_bytes = configuration_state(paths.shell_config)
                    if lock_subset(latest, snapshot["ids"]) != lock_subset(original, snapshot["ids"]):
                        raise InstallError("Lock selection changed during preparation; preserving your edit")
                    desired, commit_snapshot = enable_configuration(latest, previous)
                    if snapshot is not prior_snapshot:
                        snapshot = commit_snapshot
                        atomic_json(paths.snapshot, snapshot)
                    if latest_bytes != old_config:
                        old_config = latest_bytes
                        persist_backup(paths, {"shell.json": old_config, "active.json": old_active,
                                               "uninstall.json": old_snapshot}, previous_checkout, backup=backup)
                    ensure_unlocked(runner)
                    if set(active_locks(runner)) != set(previous):
                        raise InstallError("Lock selection changed during preparation; preserving your edit")
                    _, final_bytes = configuration_state(paths.shell_config)
                    if final_bytes == latest_bytes:
                        break
                else:
                    raise InstallError("Configuration kept changing during preparation; preserving your edits")
                atomic_json(paths.shell_config, desired)
                config_committed = True
                ensure_unlocked(runner)
                run_checked(["omarchy", "restart", "shell"], runner, timeout=90)
                wait_for_readiness(runner, custom=True)
            except Exception as error:
                if not safe_to_rollback(runner):
                    raise InstallError("Activation failed while compositor state is locked/unknown. "
                                       f"Do not reload the lock. Recovery backup: {backup}") from error
                if config_committed:
                    restored = restore_configuration(read_json(paths.shell_config, {}), commit_snapshot)
                    if old_config is None and not restored:
                        paths.shell_config.unlink(missing_ok=True)
                    else:
                        atomic_json(paths.shell_config, restored)
                if active_committed:
                    restore_file(paths.active, old_active)
                if snapshot_committed:
                    restore_file(paths.snapshot, old_snapshot)
                if previous_checkout is not None:
                    if not safe_to_rollback(runner):
                        raise InstallError("Rollback stopped while locked/unknown; do not replace the lock. "
                                           f"Recovery backup: {backup}") from error
                    failed = paths.state / "backups" / ("failed-source-" + uuid.uuid4().hex)
                    os.replace(paths.plugin, failed)
                    os.replace(previous_checkout, paths.plugin)
                if config_committed:
                    if not safe_to_rollback(runner):
                        raise InstallError("Rollback stopped while locked/unknown; do not reload the lock. "
                                           f"Recovery backup: {backup}") from error
                    run_checked(["omarchy", "restart", "shell"], runner, timeout=90)
                raise InstallError("Installation failed; prior configuration restored") from error
    return {"installed": True, "plugin": PLUGIN_ID, "fps": options.fps,
            "blankSeconds": metadata["blankSeconds"], "backup": str(backup)}


def uninstall(paths, options, *, runner=subprocess.run):
    ensure_unlocked(runner)
    snapshot = read_json(paths.snapshot)
    if not snapshot:
        raise InstallError("No installation snapshot exists; refusing to guess your previous lock setup")
    current = read_json(paths.shell_config, {})
    desired = restore_configuration(current, snapshot)
    previous = set(active_locks(runner))
    if previous:
        raise InstallError("Lock selection changed since installation; another lock clone is active")
    if options.dry_run:
        return {"dryRun": True, "restores": snapshot["ids"], "keepsCheckoutAndBackups": True}
    with (paths.state / "install.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with IdlePause(runner):
            ensure_unlocked(runner)
            if set(active_locks(runner)) != previous:
                raise InstallError("Lock selection changed during preparation; preserving your edit")
            current, old_config = configuration_state(paths.shell_config)
            restore_configuration(current, snapshot)
            old_active = paths.active.read_bytes() if paths.active.exists() else None
            old_snapshot = paths.snapshot.read_bytes() if paths.snapshot.exists() else None
            backup = persist_backup(paths, {"shell.json": old_config, "active.json": old_active,
                                            "uninstall.json": old_snapshot})
            config_committed = False
            undo = None
            try:
                for attempt in range(3):
                    ensure_unlocked(runner)
                    if set(active_locks(runner)) != previous:
                        raise InstallError("Lock selection changed during preparation; preserving your edit")
                    current, latest_bytes = configuration_state(paths.shell_config)
                    desired = restore_configuration(current, snapshot)
                    undo = {"ids": snapshot["ids"], "expected": lock_subset(desired, snapshot["ids"]),
                            "before": {key: {"present": key in current,
                                             "entries": [[index, entry] for index, entry in enumerate(current.get(key, []))
                                                         if entry_id(entry) in snapshot["ids"]]}
                                       for key in snapshot["before"]}}
                    if latest_bytes != old_config:
                        old_config = latest_bytes
                        persist_backup(paths, {"shell.json": old_config, "active.json": old_active,
                                               "uninstall.json": old_snapshot}, backup=backup)
                    ensure_unlocked(runner)
                    if set(active_locks(runner)) != previous:
                        raise InstallError("Lock selection changed during preparation; preserving your edit")
                    _, final_bytes = configuration_state(paths.shell_config)
                    if final_bytes == latest_bytes:
                        break
                else:
                    raise InstallError("Configuration kept changing during preparation; preserving your edits")
                atomic_json(paths.shell_config, desired)
                config_committed = True
                ensure_unlocked(runner)
                run_checked(["omarchy", "restart", "shell"], runner, timeout=90)
                wait_for_readiness(runner)
                paths.active.unlink(missing_ok=True)
                paths.snapshot.unlink()
            except Exception as error:
                if not config_committed or undo is None:
                    raise InstallError("Uninstall failed before configuration commit; edits preserved. "
                                       f"Recovery backup: {backup}") from error
                if not safe_to_rollback(runner):
                    raise InstallError("Uninstall failed while locked/unknown; do not reload the lock. "
                                       f"Recovery backup: {backup}") from error
                atomic_json(paths.shell_config, restore_configuration(read_json(paths.shell_config, {}), undo))
                restore_file(paths.active, old_active)
                restore_file(paths.snapshot, old_snapshot)
                if not safe_to_rollback(runner):
                    raise InstallError("Rollback stopped while locked/unknown; do not reload the lock. "
                                       f"Recovery backup: {backup}") from error
                run_checked(["omarchy", "restart", "shell"], runner, timeout=90)
                raise InstallError("Uninstall failed; screensaver configuration restored") from error
    return {"uninstalled": True, "keepsCheckoutAndBackups": True,
            "message": "Previous lock restored; checkout and backups retained for reinstall/recovery"}
