"""Transactional deployment; no installed authentication source is vendored."""
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import time
import uuid

from installer import (PLUGIN_ID, IdlePause, InstallError, entry_id,
                       enable_configuration, restore_configuration, lock_subset,
                       read_command_json, run_checked, ensure_unlocked)


@contextmanager
def _parent_directory(path, *, create=False):
    """Open every parent without following links, retaining the directory fd.

    Linked configuration/XDG directories are deliberately unsupported. Do not
    resolve them: that would silently authorize editing a different location.
    """
    path = Path(path).absolute()
    if ".." in path.parts:
        raise InstallError(f"Refusing a configuration path containing '..': {path}")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory = os.open(path.anchor, flags)
    try:
        for part in path.parts[1:-1]:
            try:
                child = os.open(part, flags, dir_fd=directory)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(part, dir_fd=directory)
                except FileExistsError:
                    pass
                child = os.open(part, flags, dir_fd=directory)
            os.close(directory)
            directory = child
        yield directory, path.name
    finally:
        os.close(directory)


def _regular_file(directory, name, path):
    try:
        state = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(state.st_mode):
        raise InstallError(f"Refusing symbolic-link configuration/state file: {path}")
    if not stat.S_ISREG(state.st_mode):
        raise InstallError(f"Refusing non-regular configuration/state file: {path}")
    if state.st_nlink != 1:
        raise InstallError(f"Refusing hard-linked configuration/state file: {path}")
    return state


def _identity(state):
    return None if state is None else (state.st_dev, state.st_ino, state.st_size,
                                       state.st_mtime_ns, state.st_ctime_ns, state.st_mode,
                                       state.st_nlink)


def _verify_parent(path, directory):
    with _parent_directory(path) as (current, _):
        before, after = os.fstat(directory), os.fstat(current)
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise InstallError(f"Configuration parent changed; preserving your edit: {path}")


def read_bytes(path):
    """Read a single-link regular file without following leaf or parent links."""
    path = Path(path)
    try:
        with _parent_directory(path) as (directory, name):
            before = _regular_file(directory, name, path)
            if before is None:
                return None
            # NONBLOCK also prevents a concurrent FIFO substitution from hanging.
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                 dir_fd=directory)
            with os.fdopen(descriptor, "rb") as stream:
                opened = os.fstat(stream.fileno())
                if _identity(opened) != _identity(before):
                    raise InstallError(f"Configuration changed while opening; preserving your edit: {path}")
                payload = stream.read()
                after = os.fstat(stream.fileno())
            _verify_parent(path, directory)
            current = _regular_file(directory, name, path)
            if _identity(after) != _identity(before) or _identity(current) != _identity(before):
                raise InstallError(f"Configuration changed while reading; preserving your edit: {path}")
            return payload
    except FileNotFoundError:
        return None
    except OSError as error:
        raise InstallError(f"Cannot safely read {path}; linked, non-directory, or inaccessible "
                           "parents are unsupported") from error


def atomic_bytes(path, payload):
    """Replace only an unchanged regular file, never a detected link/special file.

    Directory-relative operations keep a parent-path substitution from redirecting
    writes. The final check/rename is not a filesystem compare-and-swap: other
    editors must still avoid changing configuration concurrently.
    """
    path = Path(path)
    try:
        with _parent_directory(path, create=True) as (directory, name):
            before = _regular_file(directory, name, path)
            temporary = "." + name + "." + uuid.uuid4().hex
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600, dir_fd=directory)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(payload)
                    if before is not None:
                        os.fchmod(stream.fileno(), stat.S_IMODE(before.st_mode))
                    stream.flush()
                    os.fsync(stream.fileno())
                _verify_parent(path, directory)
                if _identity(_regular_file(directory, name, path)) != _identity(before):
                    raise InstallError(f"Configuration changed before write; preserving your edit: {path}")
                os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
            finally:
                try:
                    os.unlink(temporary, dir_fd=directory)
                except FileNotFoundError:
                    pass
    except OSError as error:
        raise InstallError(f"Cannot safely write {path}; linked, non-directory, or inaccessible "
                           "parents are unsupported") from error


def remove_file(path):
    """Remove only an unchanged regular file; preserve substituted links."""
    path = Path(path)
    try:
        with _parent_directory(path) as (directory, name):
            before = _regular_file(directory, name, path)
            if before is None:
                return
            _verify_parent(path, directory)
            if _identity(_regular_file(directory, name, path)) != _identity(before):
                raise InstallError(f"Configuration changed before removal; preserving your edit: {path}")
            os.unlink(name, dir_fd=directory)
    except FileNotFoundError:
        return
    except OSError as error:
        raise InstallError(f"Cannot safely remove {path}; linked, non-directory, or inaccessible "
                           "parents are unsupported") from error



def make_directory(path, *, mode=0o700, exist_ok=False):
    """Create a directory without following an existing leaf or parent link."""
    path = Path(path)
    try:
        with _parent_directory(path, create=True) as (directory, name):
            _verify_parent(path, directory)
            try:
                os.mkdir(name, mode=mode, dir_fd=directory)
            except FileExistsError:
                current = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if not exist_ok or not stat.S_ISDIR(current.st_mode):
                    raise InstallError(f"Refusing existing linked/non-directory destination: {path}")
    except OSError as error:
        raise InstallError(f"Cannot safely create directory {path}; linked parents are unsupported") from error


@contextmanager
def operation_lock(path):
    """Never follow a substituted install.lock, including a dangling link/FIFO."""
    path = Path(path)
    acquired = False
    try:
        with _parent_directory(path, create=True) as (directory, name):
            before = _regular_file(directory, name, path)
            flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
            if before is None:
                flags |= os.O_CREAT | os.O_EXCL
            descriptor = os.open(name, flags, 0o600, dir_fd=directory)
            with os.fdopen(descriptor, "a") as lock:
                opened = os.fstat(lock.fileno())
                current = _regular_file(directory, name, path)
                if (_identity(opened) != _identity(current)
                        or (before is not None and _identity(opened) != _identity(before))):
                    raise InstallError(f"Operation lock changed; preserving your edit: {path}")
                _verify_parent(path, directory)
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                yield lock
    except OSError as error:
        if acquired:
            raise
        raise InstallError(f"Cannot safely acquire operation lock {path}; "
                           "another operation or an unsupported path may be present") from error


def move_directory(source, destination):
    """Move an owned checkout/backup without following either parent chain."""
    source, destination = Path(source), Path(destination)
    with _parent_directory(source) as (source_dir, source_name):
        with _parent_directory(destination, create=True) as (destination_dir, destination_name):
            current = os.stat(source_name, dir_fd=source_dir, follow_symlinks=False)
            if not stat.S_ISDIR(current.st_mode):
                raise InstallError(f"Refusing linked/non-directory source: {source}")
            try:
                os.stat(destination_name, dir_fd=destination_dir, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise InstallError(f"Refusing to overwrite existing destination: {destination}")
            _verify_parent(source, source_dir)
            _verify_parent(destination, destination_dir)
            os.replace(source_name, destination_name,
                       src_dir_fd=source_dir, dst_dir_fd=destination_dir)


def atomic_json(path, payload):
    atomic_bytes(path, (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode())


def read_json(path, default=None):
    payload = read_bytes(path)
    if payload is None:
        return default
    try:
        return json.loads(payload)
    except ValueError as error:
        raise InstallError(f"Cannot read valid JSON from {path}") from error


def configuration_state(path):
    """Check and back up the same safe configuration bytes, not two racing reads."""
    payload = read_bytes(path)
    try:
        config = json.loads(payload) if payload is not None else {}
    except ValueError as error:
        raise InstallError(f"Cannot read valid JSON from {path}") from error
    if not isinstance(config, dict):
        raise InstallError("shell.json must contain an object")
    return config, payload


def preflight_paths(paths):
    """Reject unsupported config/state/data paths before temporary idle changes."""
    for path in (paths.shell_config, paths.active, paths.snapshot, paths.state / "install.lock"):
        read_bytes(path)
    # Check existing directory chains before builds, backups, or checkout copies.
    # Missing directories are allowed and will be created by the real operation.
    for path in (paths.data / "generations" / ".path-check",
                 paths.state / "backups" / ".path-check",
                 paths.plugin / "manifest.json"):
        try:
            with _parent_directory(path):
                pass
        except FileNotFoundError:
            pass
        except OSError as error:
            raise InstallError(f"Cannot safely use {path.parent}; linked, non-directory, or "
                               "inaccessible parents are unsupported") from error


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
    make_directory(paths.plugins, mode=0o755, exist_ok=True)
    temporary = paths.plugins / (".lock-screensaver-" + uuid.uuid4().hex)
    previous = None
    try:
        shutil.copytree(project, temporary, ignore=shutil.ignore_patterns(
            ".git", ".venv", "__pycache__", "build", "dist", "*.pyc", ".pytest_cache"))
        if runner is not None:
            ensure_unlocked(runner)
        if paths.plugin.exists():
            previous = paths.state / "backups" / ("source-" + uuid.uuid4().hex)
            make_directory(previous.parent, exist_ok=True)
            move_directory(paths.plugin, previous)
        move_directory(temporary, paths.plugin)
    except Exception as error:
        if previous is not None and previous.exists() and not paths.plugin.exists():
            if runner is None or not safe_to_rollback(runner):
                raise InstallError("Source deployment failed while locked/unknown; do not reload the lock. "
                                   f"Recovery backup: {previous}") from error
            move_directory(previous, paths.plugin)
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
        remove_file(path)
    else:
        atomic_bytes(path, old_bytes)


def persist_backup(paths, files, previous_checkout=None, *, backup=None):
    """Keep exact pre-write pointers and absence evidence for manual recovery."""
    if backup is None:
        backup = paths.state / "backups" / uuid.uuid4().hex
        make_directory(backup)
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
    preflight_paths(paths)
    original, _ = configuration_state(paths.shell_config)
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
    make_directory(paths.state, exist_ok=True)
    with operation_lock(paths.state / "install.lock"):
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
            _, old_config = configuration_state(paths.shell_config)
            old_active = read_bytes(paths.active)
            old_snapshot = read_bytes(paths.snapshot)
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
                try:
                    if config_committed:
                        restored = restore_configuration(read_json(paths.shell_config, {}), commit_snapshot)
                        if old_config is None and not restored:
                            remove_file(paths.shell_config)
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
                        move_directory(paths.plugin, failed)
                        move_directory(previous_checkout, paths.plugin)
                    if config_committed:
                        if not safe_to_rollback(runner):
                            raise InstallError("Rollback stopped while locked/unknown; do not reload the lock. "
                                               f"Recovery backup: {backup}") from error
                        run_checked(["omarchy", "restart", "shell"], runner, timeout=90)
                except (InstallError, OSError) as recovery_error:
                    raise InstallError(f"Installation failed; rollback stopped: {recovery_error}. "
                                       f"Recovery backup: {backup}") from error
                if not config_committed:
                    raise InstallError("Installation failed before configuration commit; edits preserved. "
                                       f"Recovery backup: {backup}") from error
                raise InstallError("Installation failed; prior configuration restored") from error
    return {"installed": True, "plugin": PLUGIN_ID, "fps": options.fps,
            "blankSeconds": metadata["blankSeconds"], "backup": str(backup)}


def uninstall(paths, options, *, runner=subprocess.run):
    ensure_unlocked(runner)
    preflight_paths(paths)
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
    with operation_lock(paths.state / "install.lock"):
        with IdlePause(runner):
            ensure_unlocked(runner)
            if set(active_locks(runner)) != previous:
                raise InstallError("Lock selection changed during preparation; preserving your edit")
            current, old_config = configuration_state(paths.shell_config)
            restore_configuration(current, snapshot)
            old_active = read_bytes(paths.active)
            old_snapshot = read_bytes(paths.snapshot)
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
                remove_file(paths.active)
                remove_file(paths.snapshot)
            except Exception as error:
                if not config_committed or undo is None:
                    raise InstallError("Uninstall failed before configuration commit; edits preserved. "
                                       f"Recovery backup: {backup}") from error
                if not safe_to_rollback(runner):
                    raise InstallError("Uninstall failed while locked/unknown; do not reload the lock. "
                                       f"Recovery backup: {backup}") from error
                try:
                    atomic_json(paths.shell_config, restore_configuration(read_json(paths.shell_config, {}), undo))
                    restore_file(paths.active, old_active)
                    restore_file(paths.snapshot, old_snapshot)
                    if not safe_to_rollback(runner):
                        raise InstallError("Rollback stopped while locked/unknown; do not reload the lock. "
                                           f"Recovery backup: {backup}") from error
                    run_checked(["omarchy", "restart", "shell"], runner, timeout=90)
                except (InstallError, OSError) as recovery_error:
                    raise InstallError(f"Uninstall failed; rollback stopped: {recovery_error}. "
                                       f"Recovery backup: {backup}") from error
                raise InstallError("Uninstall failed; screensaver configuration restored") from error
    return {"uninstalled": True, "keepsCheckoutAndBackups": True,
            "message": "Previous lock restored; checkout and backups retained for reinstall/recovery"}
