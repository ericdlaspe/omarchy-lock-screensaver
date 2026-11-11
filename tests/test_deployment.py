"""Exercise real compilation/generation and filesystem transactions in a fake desktop.

The compositor/IPC is explicitly simulated. No live lock or desktop is modified.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import deployment
import installer
from deployment import read_json


class TransactionRegressionTests(unittest.TestCase):
    """All desktop commands are fake; every filesystem target is temporary."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="locksaver-transaction-")
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.paths = installer.Paths.from_environment(self.home, {})
        self.paths.shell_config.parent.mkdir(parents=True)
        self.original = {"bar": {"value": "original"}, "idle": {"lock": 300}}
        deployment.atomic_json(self.paths.shell_config, self.original)
        self.project = self.home / "source"
        self.project.mkdir()
        deployment.atomic_json(self.project / "manifest.json", {"id": installer.PLUGIN_ID})
        (self.project / "Service.qml").write_text("new source")
        self.options = SimpleNamespace(fps=60, blank_minutes=30, metrics=False,
                                       python=sys.executable, replace_existing_lock=False, dry_run=False)
        self.calls = []
        self.restarts = 0
        self.hook = lambda command: None
        self.lock_state = {"locked": False, "requested": False, "secure": False, "passwordPam": True}
        self.plugin_state = {"ready": True, "custom": True}

    def runner(self, command, **kwargs):
        command = list(map(str, command))
        self.calls.append(command)
        self.hook(command)
        if command == ["omarchy-hyprland-session-locked"]:
            return subprocess.CompletedProcess(command, 1, "", "")
        if command == ["omarchy-shell", "lock", "status"]:
            value = self.lock_state
        elif command == ["omarchy-shell", "lock-screensaver", "status"]:
            value = self.plugin_state
        elif command == ["omarchy-shell", "idle", "status"]:
            value = {"enabled": False}
        elif command == ["omarchy", "plugin", "list", "--json"]:
            value = []
        elif command == ["omarchy", "restart", "shell"]:
            self.restarts += 1
            value = {}
        else:
            value = {}
        return subprocess.CompletedProcess(command, 0, json.dumps(value), "")

    def build(self, *args):
        return {"serviceUrl": "file:///fixture/Service.qml", "blankSeconds": 1800}

    def install(self):
        return installer.install(self.paths, self.options, self.project,
                                 runner=self.runner, build=self.build)

    def seed_installed(self):
        current, snapshot = installer.enable_configuration(self.original, [])
        deployment.atomic_json(self.paths.shell_config, current)
        deployment.atomic_json(self.paths.snapshot, snapshot)
        deployment.atomic_json(self.paths.active, {"old": "pointer"})
        return current

    def edit_bar(self, value):
        current = read_json(self.paths.shell_config)
        current["bar"] = {"value": value}
        deployment.atomic_json(self.paths.shell_config, current)

    def test_install_refuses_new_lock_clone_enabled_during_build(self):
        original = {"plugins": [{"id": "previous.lock"}],
                    "disabledPlugins": ["omarchy.lock"],
                    "cloneSourceRestores": ["previous.lock"]}
        deployment.atomic_json(self.paths.shell_config, original)
        deployment.atomic_json(self.paths.active, {"old": "pointer"})
        old_active = self.paths.active.read_bytes()
        self.options.replace_existing_lock = True
        catalog = [{"id": "previous.lock", "enabled": True, "clonedFrom": "omarchy.lock"}]
        edited = []
        def runner(command, **kwargs):
            if command == ["omarchy", "plugin", "list", "--json"]:
                return subprocess.CompletedProcess(command, 0, json.dumps(catalog), "")
            return self.runner(command, **kwargs)
        def build(*args):
            latest = read_json(self.paths.shell_config)
            latest["plugins"].append({"id": "new.lock"})
            deployment.atomic_json(self.paths.shell_config, latest)
            catalog.append({"id": "new.lock", "enabled": True, "clonedFrom": "omarchy.lock"})
            edited.append(latest)
            return self.build(*args)
        with self.assertRaises(installer.InstallError):
            installer.install(self.paths, self.options, self.project, runner=runner, build=build)
        self.assertEqual(read_json(self.paths.shell_config), edited[0])
        self.assertEqual(self.paths.active.read_bytes(), old_active)
        self.assertFalse(self.paths.snapshot.exists())
        self.assertEqual(self.restarts, 0)

    def test_install_refuses_new_lock_clone_after_source_copy_or_at_final_guard(self):
        for seam in ("source-copy", "final-guard"):
            with self.subTest(seam=seam):
                deployment.atomic_json(self.paths.shell_config, self.original)
                deployment.atomic_json(self.paths.active, {"old": "pointer"})
                self.paths.snapshot.unlink(missing_ok=True)
                old_active = self.paths.active.read_bytes()
                catalog = []
                edited = []
                def select_new_lock():
                    latest = read_json(self.paths.shell_config)
                    latest.setdefault("plugins", []).append({"id": "new.lock"})
                    deployment.atomic_json(self.paths.shell_config, latest)
                    catalog.append({"id": "new.lock", "enabled": True, "clonedFrom": "omarchy.lock"})
                    edited.append(latest)
                copytree = deployment.shutil.copytree
                def copy_and_select(*args, **kwargs):
                    result = copytree(*args, **kwargs)
                    if seam == "source-copy":
                        select_new_lock()
                    return result
                def runner(command, **kwargs):
                    if command == ["omarchy", "plugin", "list", "--json"]:
                        return subprocess.CompletedProcess(command, 0, json.dumps(catalog), "")
                    if (seam == "final-guard" and not edited
                            and command == ["omarchy-shell", "lock", "status"]
                            and "serviceUrl" in read_json(self.paths.active, {})):
                        select_new_lock()
                    return self.runner(command, **kwargs)
                with patch.object(deployment.shutil, "copytree", side_effect=copy_and_select):
                    with self.assertRaises(installer.InstallError):
                        installer.install(self.paths, self.options, self.project,
                                          runner=runner, build=self.build)
                self.assertEqual(read_json(self.paths.shell_config), edited[0])
                self.assertEqual(self.paths.active.read_bytes(), old_active)
                self.assertFalse(self.paths.snapshot.exists())
                self.assertEqual(self.restarts, 0)

    def test_uninstall_refuses_new_lock_clone_during_precommit_guards(self):
        for guard in (2, 3):
            with self.subTest(guard=guard):
                self.seed_installed()
                old_active = self.paths.active.read_bytes()
                old_snapshot = self.paths.snapshot.read_bytes()
                catalog = []
                edited = []
                checks = 0
                def runner(command, **kwargs):
                    nonlocal checks
                    if command == ["omarchy", "plugin", "list", "--json"]:
                        return subprocess.CompletedProcess(command, 0, json.dumps(catalog), "")
                    if command == ["omarchy-shell", "lock", "status"]:
                        checks += 1
                        if checks == guard:
                            latest = read_json(self.paths.shell_config)
                            latest["plugins"].append({"id": "new.lock"})
                            deployment.atomic_json(self.paths.shell_config, latest)
                            catalog.append({"id": "new.lock", "enabled": True,
                                            "clonedFrom": "omarchy.lock"})
                            edited.append(latest)
                    return self.runner(command, **kwargs)
                with self.assertRaises(installer.InstallError):
                    installer.uninstall(self.paths, self.options, runner=runner)
                self.assertEqual(read_json(self.paths.shell_config), edited[0])
                self.assertEqual(self.paths.active.read_bytes(), old_active)
                self.assertEqual(self.paths.snapshot.read_bytes(), old_snapshot)
                self.assertEqual(self.restarts, 0)

    def test_pending_lock_after_config_commit_blocks_activation_restart(self):
        for action in ("install", "uninstall"):
            with self.subTest(action=action):
                self.lock_state["requested"] = False
                self.seed_installed()
                before = {name: path.read_bytes() for name, path in (
                    ("shell.json", self.paths.shell_config), ("active.json", self.paths.active),
                    ("uninstall.json", self.paths.snapshot))}
                committed = []
                write_json = deployment.atomic_json
                def request_after_commit(path, value):
                    result = write_json(path, value)
                    if path == self.paths.shell_config:
                        committed.append(value)
                        self.lock_state["requested"] = True
                    return result
                idle_enabled = True
                def runner(command, **kwargs):
                    nonlocal idle_enabled
                    if command == ["omarchy-shell", "idle", "status"]:
                        return subprocess.CompletedProcess(command, 0,
                                                           json.dumps({"enabled": idle_enabled}), "")
                    if command == ["omarchy-shell", "idle", "disable"]:
                        idle_enabled = False
                    elif command == ["omarchy-shell", "idle", "enable"]:
                        idle_enabled = True
                    return self.runner(command, **kwargs)
                with patch.object(deployment, "atomic_json", side_effect=request_after_commit):
                    with self.assertRaisesRegex(installer.InstallError, "Recovery backup:") as raised:
                        if action == "install":
                            installer.install(self.paths, self.options, self.project,
                                              runner=runner, build=self.build)
                        else:
                            installer.uninstall(self.paths, self.options, runner=runner)
                self.assertEqual(self.restarts, 0)
                self.assertEqual(len(committed), 1)
                self.assertEqual(read_json(self.paths.shell_config), committed[0])
                self.assertTrue(self.paths.active.exists())
                self.assertTrue(self.paths.snapshot.exists())
                backup = Path(str(raised.exception).rsplit("Recovery backup: ", 1)[1])
                for name, payload in before.items():
                    self.assertEqual((backup / name).read_bytes(), payload)
                self.assertTrue(idle_enabled)
                self.assertIn(["omarchy-shell", "idle", "enable"], self.calls)

    def test_successive_backup_edits_are_merged_from_the_last_configuration(self):
        for action in ("install", "uninstall"):
            with self.subTest(action=action):
                if action == "uninstall":
                    self.seed_installed()
                else:
                    deployment.atomic_json(self.paths.shell_config, self.original)
                    deployment.atomic_json(self.paths.active, {"old": "pointer"})
                    self.paths.snapshot.unlink(missing_ok=True)
                persist_backup = deployment.persist_backup
                backups = []
                edits = []
                def edit_after_backup(*args, **kwargs):
                    backup = persist_backup(*args, **kwargs)
                    backups.append(backup)
                    if len(backups) <= 2:
                        latest = read_json(self.paths.shell_config)
                        latest["bar"] = {"value": f"distinct backup edit {len(backups)}"}
                        latest.setdefault("plugins", []).append({"id": f"weather.{len(backups)}"})
                        deployment.atomic_json(self.paths.shell_config, latest)
                        edits.append(latest)
                    return backup
                with patch.object(deployment, "persist_backup", side_effect=edit_after_backup):
                    if action == "install":
                        self.install()
                    else:
                        installer.uninstall(self.paths, self.options, runner=self.runner)
                final = read_json(self.paths.shell_config)
                self.assertEqual(final["bar"], edits[-1]["bar"])
                self.assertIn({"id": "weather.1"}, final["plugins"])
                self.assertIn({"id": "weather.2"}, final["plugins"])
                self.assertEqual(read_json(backups[-1] / "shell.json"), edits[-1])

    def test_uninstall_refuses_an_already_active_independent_lock_clone(self):
        current = self.seed_installed()
        current["plugins"].append({"id": "new.lock"})
        deployment.atomic_json(self.paths.shell_config, current)
        old_config = self.paths.shell_config.read_bytes()
        old_active = self.paths.active.read_bytes()
        old_snapshot = self.paths.snapshot.read_bytes()
        def runner(command, **kwargs):
            if command == ["omarchy", "plugin", "list", "--json"]:
                catalog = [{"id": installer.PLUGIN_ID, "enabled": True, "clonedFrom": "omarchy.lock"},
                           {"id": "new.lock", "enabled": True, "clonedFrom": "omarchy.lock"}]
                return subprocess.CompletedProcess(command, 0, json.dumps(catalog), "")
            return self.runner(command, **kwargs)
        with self.assertRaises(installer.InstallError):
            installer.uninstall(self.paths, self.options, runner=runner)
        self.assertEqual(self.paths.shell_config.read_bytes(), old_config)
        self.assertEqual(self.paths.active.read_bytes(), old_active)
        self.assertEqual(self.paths.snapshot.read_bytes(), old_snapshot)
        self.assertEqual(self.restarts, 0)

    def test_continuous_preparation_edits_refuse_commit_with_bounded_retries(self):
        for action, seam in (("install", "backup"), ("uninstall", "backup"),
                             ("install", "snapshot")):
            with self.subTest(action=action, seam=seam):
                if action == "uninstall":
                    self.seed_installed()
                else:
                    deployment.atomic_json(self.paths.shell_config, self.original)
                    deployment.atomic_json(self.paths.active, {"old": "pointer"})
                    self.paths.snapshot.unlink(missing_ok=True)
                old_active = self.paths.active.read_bytes()
                old_snapshot = self.paths.snapshot.read_bytes() if self.paths.snapshot.exists() else None
                persist_backup = deployment.persist_backup
                write_json = deployment.atomic_json
                edits = []
                config_writes = []
                def edit_configuration():
                    latest = read_json(self.paths.shell_config)
                    latest["bar"] = {"value": f"continuous edit {len(edits) + 1}"}
                    write_json(self.paths.shell_config, latest)
                    edits.append(latest)
                def edit_after_backup(*args, **kwargs):
                    backup = persist_backup(*args, **kwargs)
                    if seam == "backup":
                        edit_configuration()
                    return backup
                def edit_after_snapshot(path, value):
                    result = write_json(path, value)
                    if path == self.paths.shell_config:
                        config_writes.append(value)
                    elif seam == "snapshot" and path == self.paths.snapshot:
                        edit_configuration()
                    return result
                with (patch.object(deployment, "persist_backup", side_effect=edit_after_backup),
                      patch.object(deployment, "atomic_json", side_effect=edit_after_snapshot)):
                    with self.assertRaises(installer.InstallError) as raised:
                        if action == "install":
                            self.install()
                        else:
                            installer.uninstall(self.paths, self.options, runner=self.runner)
                self.assertIn("kept changing", str(raised.exception.__cause__))
                self.assertEqual(len(edits), 4, "One initial preparation and three bounded retries")
                self.assertEqual(config_writes, [])
                self.assertEqual(read_json(self.paths.shell_config), edits[-1])
                self.assertEqual(self.paths.active.read_bytes(), old_active)
                if old_snapshot is None:
                    self.assertFalse(self.paths.snapshot.exists())
                else:
                    self.assertEqual(self.paths.snapshot.read_bytes(), old_snapshot)
                self.assertEqual(self.restarts, 0)

    def test_install_refuses_disappearing_initial_lock_clone(self):
        original = {"plugins": [{"id": "previous.lock"}], "disabledPlugins": ["omarchy.lock"],
                    "cloneSourceRestores": ["previous.lock"]}
        deployment.atomic_json(self.paths.shell_config, original)
        deployment.atomic_json(self.paths.active, {"old": "pointer"})
        self.options.replace_existing_lock = True
        catalog = [{"id": "previous.lock", "enabled": True, "clonedFrom": "omarchy.lock"}]
        def runner(command, **kwargs):
            if command == ["omarchy", "plugin", "list", "--json"]:
                return subprocess.CompletedProcess(command, 0, json.dumps(catalog), "")
            return self.runner(command, **kwargs)
        def build(*args):
            catalog[0]["enabled"] = False
            return self.build(*args)
        with self.assertRaises(installer.InstallError):
            installer.install(self.paths, self.options, self.project, runner=runner, build=build)
        self.assertEqual(read_json(self.paths.shell_config), original)
        self.assertEqual(read_json(self.paths.active), {"old": "pointer"})
        self.assertFalse(self.paths.snapshot.exists())
        self.assertEqual(self.restarts, 0)

    def test_own_catalog_entry_does_not_block_update_or_uninstall(self):
        self.seed_installed()
        def runner(command, **kwargs):
            if command == ["omarchy", "plugin", "list", "--json"]:
                catalog = [{"id": installer.PLUGIN_ID, "enabled": True, "clonedFrom": "omarchy.lock"}]
                return subprocess.CompletedProcess(command, 0, json.dumps(catalog), "")
            return self.runner(command, **kwargs)
        result = installer.install(self.paths, self.options, self.project, runner=runner, build=self.build)
        self.assertTrue(result["installed"])
        result = installer.uninstall(self.paths, self.options, runner=runner)
        self.assertTrue(result["uninstalled"])
        self.assertEqual(read_json(self.paths.shell_config), self.original)
        self.assertEqual(self.restarts, 2)

    def test_uninstall_merges_edit_during_final_unlocked_guard(self):
        self.seed_installed()
        checks = 0
        def edit_during_guard(command):
            nonlocal checks
            if command == ["omarchy-shell", "lock", "status"]:
                checks += 1
                if checks == 2:
                    self.edit_bar("edited during uninstall")
        self.hook = edit_during_guard
        installer.uninstall(self.paths, self.options, runner=self.runner)
        restored = read_json(self.paths.shell_config)
        self.assertEqual(restored["bar"]["value"], "edited during uninstall")
        self.assertNotIn(installer.PLUGIN_ID, restored.get("plugins", []))

    def test_activation_rollback_merges_only_owned_selection(self):
        for action in ("install", "uninstall"):
            with self.subTest(action=action):
                current = self.seed_installed() if action == "uninstall" else self.original
                deployment.atomic_json(self.paths.shell_config, current)
                self.restarts = 0
                failed = False
                def fail_activation(command):
                    nonlocal failed
                    if command == ["omarchy", "restart", "shell"] and not failed:
                        failed = True
                        self.edit_bar("edited after configuration commit")
                        latest = read_json(self.paths.shell_config)
                        latest.setdefault("plugins", []).append({"id": "new.weather"})
                        deployment.atomic_json(self.paths.shell_config, latest)
                        raise installer.InstallError("activation failed")
                self.hook = fail_activation
                with self.assertRaises(installer.InstallError):
                    if action == "install":
                        self.install()
                    else:
                        installer.uninstall(self.paths, self.options, runner=self.runner)
                restored = read_json(self.paths.shell_config)
                self.assertEqual(restored["bar"]["value"], "edited after configuration commit")
                self.assertIn({"id": "new.weather"}, restored["plugins"])
                ids = [installer.PLUGIN_ID, "omarchy.lock"]
                self.assertEqual(installer.lock_subset(restored, ids), installer.lock_subset(current, ids))
                self.assertEqual(self.restarts, 1)

    def test_failure_before_configuration_commit_never_rewrites_concurrent_edit(self):
        write_json = deployment.atomic_json
        def fail_pointer(path, value):
            if path == self.paths.active:
                self.edit_bar("concurrent precommit edit")
                raise OSError("pointer write failed")
            return write_json(path, value)
        with patch.object(deployment, "atomic_json", side_effect=fail_pointer):
            with self.assertRaises(installer.InstallError):
                self.install()
        self.assertEqual(read_json(self.paths.shell_config)["bar"]["value"], "concurrent precommit edit")
        self.assertFalse(self.paths.snapshot.exists())
        self.assertFalse(self.paths.active.exists())
        self.assertEqual(self.restarts, 0)

    def assert_recovery_backup(self, expected):
        backups = [path for path in (self.paths.state / "backups").iterdir()
                   if (path / "shell.json").exists()]
        self.assertEqual(len(backups), 1)
        backup = backups[0]
        manifest = read_json(backup / "recovery.json")
        self.assertIsInstance(manifest, dict, "Missing persistent absence/recovery metadata")
        for name, payload in expected.items():
            self.assertEqual(manifest["files"][name]["present"], payload is not None)
            if payload is not None:
                self.assertEqual((backup / name).read_bytes(), payload)
            else:
                self.assertFalse((backup / name).exists())
        return backup

    def pending_activation_failure(self, command):
        if command == ["omarchy", "restart", "shell"]:
            self.lock_state["requested"] = True
            raise installer.InstallError("pending lock during activation")

    def check_persistent_backup_before_writes(self, action):
        expected = {name: path.read_bytes() if path.exists() else None
                    for name, path in (("shell.json", self.paths.shell_config),
                                       ("active.json", self.paths.active),
                                       ("uninstall.json", self.paths.snapshot))}
        write_json = deployment.atomic_json
        observed = []
        def verify_before_write(path, value):
            if path in (self.paths.active, self.paths.snapshot, self.paths.shell_config):
                self.assert_recovery_backup(expected)
                observed.append(path)
            return write_json(path, value)
        self.hook = self.pending_activation_failure
        with patch.object(deployment, "atomic_json", side_effect=verify_before_write):
            with self.assertRaisesRegex(installer.InstallError, "Recovery backup:"):
                if action == "install":
                    self.install()
                else:
                    installer.uninstall(self.paths, self.options, runner=self.runner)
        self.assertTrue(observed)
        self.assert_recovery_backup(expected)
        self.assertEqual(self.restarts, 0, "No second restart may replace a pending lock client")

    def test_first_install_persists_pointer_absence_before_writes(self):
        self.check_persistent_backup_before_writes("install")

    def test_update_persists_previous_pointers_and_retains_source_on_pending_lock(self):
        self.seed_installed()
        deployment.prepare_plugin_checkout(self.paths, self.project)
        (self.paths.plugin / "Service.qml").write_text("previous source")
        self.check_persistent_backup_before_writes("install")
        self.assertEqual((self.paths.plugin / "Service.qml").read_text(), "new source")
        source_backups = list((self.paths.state / "backups").glob("source-*/Service.qml"))
        self.assertEqual([path.read_text() for path in source_backups], ["previous source"])
        self.assertEqual(list((self.paths.state / "backups").glob("failed-source-*")), [])

    def test_uninstall_persists_previous_pointers_before_writes(self):
        self.seed_installed()
        self.check_persistent_backup_before_writes("uninstall")

    def test_install_waits_for_delayed_pam_and_custom_readiness(self):
        lock_reads = plugin_reads = 0
        def delayed_status(command):
            nonlocal lock_reads, plugin_reads
            if self.restarts:
                if command == ["omarchy-shell", "lock", "status"]:
                    lock_reads += 1
                    self.lock_state["passwordPam"] = lock_reads >= 3
                elif command == ["omarchy-shell", "lock-screensaver", "status"]:
                    plugin_reads += 1
                    self.plugin_state["ready"] = plugin_reads >= 2
        self.hook = delayed_status
        elapsed = [0.0]
        def advance(seconds):
            elapsed[0] += seconds
        with patch("time.monotonic", side_effect=lambda: elapsed[0]), patch("time.sleep", side_effect=advance):
            try:
                result = self.install()
            except installer.InstallError as error:
                self.fail(f"Asynchronous readiness was not awaited: {error}")
        self.assertTrue(result["installed"])
        self.assertGreaterEqual(lock_reads, 3)
        self.assertGreaterEqual(plugin_reads, 2)
        self.assertEqual(self.restarts, 1)
        self.assertGreater(elapsed[0], 0)
        self.assertLess(elapsed[0], 1)

    def test_uninstall_waits_for_delayed_pam_readiness(self):
        self.seed_installed()
        reads = 0
        def delayed_status(command):
            nonlocal reads
            if self.restarts and command == ["omarchy-shell", "lock", "status"]:
                reads += 1
                self.lock_state["passwordPam"] = reads >= 3
        self.hook = delayed_status
        elapsed = [0.0]
        def advance(seconds):
            elapsed[0] += seconds
        with patch("time.monotonic", side_effect=lambda: elapsed[0]), patch("time.sleep", side_effect=advance):
            try:
                result = installer.uninstall(self.paths, self.options, runner=self.runner)
            except installer.InstallError as error:
                self.fail(f"Asynchronous readiness was not awaited: {error}")
        self.assertTrue(result["uninstalled"])
        self.assertEqual(self.restarts, 1)
        self.assertGreater(elapsed[0], 0)
        self.assertFalse(self.paths.active.exists())

    def test_install_stock_fallback_waits_until_deadline_then_rolls_back(self):
        self.plugin_state = {"ready": False, "custom": True}
        reads = 0
        timeouts = []
        def fallback(command):
            nonlocal reads
            if self.restarts and command == ["omarchy-shell", "lock-screensaver", "status"]:
                reads += 1
                if reads >= 2:
                    self.plugin_state = {"ready": True, "custom": False}
        self.hook = fallback
        elapsed = [0.0]
        def advance(seconds):
            elapsed[0] += seconds
        def runner(command, **kwargs):
            if self.restarts and command[:1] == ["omarchy-shell"]:
                timeouts.append(kwargs["timeout"])
            return self.runner(command, **kwargs)
        with (patch("time.monotonic", side_effect=lambda: elapsed[0]),
              patch("time.sleep", side_effect=advance),
              patch.object(deployment, "READINESS_TIMEOUT", 0.3)):
            with self.assertRaisesRegex(installer.InstallError, "prior configuration restored"):
                installer.install(self.paths, self.options, self.project, runner=runner, build=self.build)
        self.assertEqual(read_json(self.paths.shell_config), self.original)
        self.assertEqual(self.restarts, 2)
        self.assertGreaterEqual(reads, 2)
        self.assertAlmostEqual(elapsed[0], 0.3)
        self.assertTrue(any(0 < timeout <= 0.3 for timeout in timeouts))
        self.assertFalse(self.paths.active.exists())

    def test_precommit_independent_lock_edits_are_refused(self):
        for action, final_check in (("install", 3), ("uninstall", 2)):
            with self.subTest(action=action):
                current = self.seed_installed() if action == "uninstall" else self.original
                deployment.atomic_json(self.paths.shell_config, current)
                self.restarts = checks = 0
                edited = []
                def change_selection(command):
                    nonlocal checks
                    if command == ["omarchy-shell", "lock", "status"]:
                        checks += 1
                        if checks == final_check:
                            latest = read_json(self.paths.shell_config)
                            latest["plugins"] = [{"id": "omarchy.lock"}]
                            deployment.atomic_json(self.paths.shell_config, latest)
                            edited.append(latest)
                self.hook = change_selection
                with self.assertRaisesRegex(installer.InstallError, "[Ll]ock selection changed"):
                    if action == "install":
                        self.install()
                    else:
                        installer.uninstall(self.paths, self.options, runner=self.runner)
                self.assertEqual(read_json(self.paths.shell_config), edited[0])
                self.assertEqual(self.restarts, 0)

    def test_activation_rollback_refuses_later_independent_lock_edits(self):
        for action in ("install", "uninstall"):
            with self.subTest(action=action):
                current = self.seed_installed() if action == "uninstall" else self.original
                deployment.atomic_json(self.paths.shell_config, current)
                self.restarts = 0
                edited = []
                def change_selection(command):
                    if command == ["omarchy", "restart", "shell"]:
                        latest = read_json(self.paths.shell_config)
                        latest["plugins"] = [{"id": "omarchy.lock"}]
                        deployment.atomic_json(self.paths.shell_config, latest)
                        edited.append(latest)
                        raise installer.InstallError("activation failed with concurrent lock edit")
                self.hook = change_selection
                with self.assertRaisesRegex(installer.InstallError, "[Ll]ock selection changed"):
                    if action == "install":
                        self.install()
                    else:
                        installer.uninstall(self.paths, self.options, runner=self.runner)
                self.assertEqual(read_json(self.paths.shell_config), edited[0])
                self.assertEqual(self.restarts, 0)

    def test_install_rechecks_configuration_after_pointer_preparation(self):
        write_json = deployment.atomic_json
        def edit_after_pointer(path, value):
            result = write_json(path, value)
            if path == self.paths.active:
                self.edit_bar("edited during pointer preparation")
            return result
        with patch.object(deployment, "atomic_json", side_effect=edit_after_pointer):
            self.install()
        self.assertEqual(read_json(self.paths.shell_config)["bar"]["value"],
                         "edited during pointer preparation")
        backup = Path(next((self.paths.state / "backups").glob("*/shell.json")))
        self.assertEqual(read_json(backup)["bar"]["value"], "edited during pointer preparation")

    def test_uninstall_rechecks_configuration_after_backup_preparation(self):
        self.seed_installed()
        persist_backup = deployment.persist_backup
        def edit_after_backup(*args, **kwargs):
            result = persist_backup(*args, **kwargs)
            self.edit_bar("edited during backup preparation")
            return result
        with patch.object(deployment, "persist_backup", side_effect=edit_after_backup):
            installer.uninstall(self.paths, self.options, runner=self.runner)
        self.assertEqual(read_json(self.paths.shell_config)["bar"]["value"],
                         "edited during backup preparation")

    def test_pending_lock_during_rollback_blocks_source_replacement_and_restart(self):
        self.seed_installed()
        deployment.prepare_plugin_checkout(self.paths, self.project)
        (self.paths.plugin / "Service.qml").write_text("previous source")
        write_json = deployment.atomic_json
        activation_failed = False
        def fail_first_restart(command):
            nonlocal activation_failed
            if command == ["omarchy", "restart", "shell"] and not activation_failed:
                activation_failed = True
                raise installer.InstallError("activation failed")
        def pending_after_undo(path, value):
            result = write_json(path, value)
            if activation_failed and path == self.paths.shell_config:
                self.lock_state["requested"] = True
            return result
        self.hook = fail_first_restart
        with patch.object(deployment, "atomic_json", side_effect=pending_after_undo):
            with self.assertRaisesRegex(installer.InstallError, "Recovery backup:"):
                self.install()
        self.assertEqual(self.restarts, 0)
        self.assertEqual((self.paths.plugin / "Service.qml").read_text(), "new source")
        self.assertEqual(list((self.paths.state / "backups").glob("failed-source-*")), [])

    def test_uninstall_pending_lock_during_rollback_blocks_restart(self):
        self.seed_installed()
        write_json = deployment.atomic_json
        activation_failed = False
        def fail_first_restart(command):
            nonlocal activation_failed
            if command == ["omarchy", "restart", "shell"] and not activation_failed:
                activation_failed = True
                raise installer.InstallError("activation failed")
        def pending_after_undo(path, value):
            result = write_json(path, value)
            if activation_failed and path == self.paths.shell_config:
                self.lock_state["requested"] = True
            return result
        self.hook = fail_first_restart
        with patch.object(deployment, "atomic_json", side_effect=pending_after_undo):
            with self.assertRaisesRegex(installer.InstallError, "Recovery backup:"):
                installer.uninstall(self.paths, self.options, runner=self.runner)
        self.assertEqual(self.restarts, 0)
        self.assertTrue(self.paths.active.exists())
        self.assertTrue(self.paths.snapshot.exists())

    def test_failed_source_swap_does_not_restore_checkout_while_lock_is_pending(self):
        deployment.prepare_plugin_checkout(self.paths, self.project)
        (self.paths.plugin / "Service.qml").write_text("previous source")
        replace = deployment.move_directory
        def fail_new_checkout(source, destination):
            if Path(source).name.startswith(".lock-screensaver-") and destination == self.paths.plugin:
                self.lock_state["requested"] = True
                raise OSError("source swap failed while lock was requested")
            return replace(source, destination)
        with patch.object(deployment, "move_directory", side_effect=fail_new_checkout):
            with self.assertRaises((OSError, installer.InstallError)):
                self.install()
        self.assertFalse(self.paths.plugin.exists(), "Unsafe source rollback replaced a pending lock client")
        source_backups = list((self.paths.state / "backups").glob("source-*/Service.qml"))
        self.assertEqual([path.read_text() for path in source_backups], ["previous source"])
        self.assertEqual(self.restarts, 0)

    def test_pending_lock_after_source_copy_blocks_checkout_swap(self):
        deployment.prepare_plugin_checkout(self.paths, self.project)
        (self.paths.plugin / "Service.qml").write_text("previous source")
        copytree = deployment.shutil.copytree
        def pending_after_copy(*args, **kwargs):
            result = copytree(*args, **kwargs)
            self.lock_state["requested"] = True
            return result
        with patch.object(deployment.shutil, "copytree", side_effect=pending_after_copy):
            with self.assertRaises(installer.InstallError):
                self.install()
        self.assertEqual((self.paths.plugin / "Service.qml").read_text(), "previous source")
        self.assertEqual(self.restarts, 0)

    def test_install_merges_edit_during_source_copy(self):
        copytree = deployment.shutil.copytree
        def copy_and_edit(*args, **kwargs):
            result = copytree(*args, **kwargs)
            self.edit_bar("edited during source copy")
            return result
        with patch.object(deployment.shutil, "copytree", side_effect=copy_and_edit):
            self.install()
        self.assertEqual(read_json(self.paths.shell_config)["bar"]["value"],
                         "edited during source copy")


@unittest.skipUnless(shutil.which("qmake6") and shutil.which("make"), "Qt build tools required")
class IsolatedDeploymentTests(unittest.TestCase):
    def test_real_build_install_and_restore_preserves_later_user_configuration(self):
        with tempfile.TemporaryDirectory(prefix="locksaver-install-") as directory:
            home = Path(directory) / "user with spaces"
            stock_root = home / "installed omarchy"
            stock = stock_root / "shell/plugins/lock"
            stock.parent.mkdir(parents=True)
            shutil.copytree(ROOT / "tests/fixtures/current-stock", stock)
            paths = installer.Paths.from_environment(home, {"OMARCHY_PATH": str(stock_root)})
            paths.shell_config.parent.mkdir(parents=True)
            original = {"idle": {"screensaver": 150, "lock": 300}, "bar": {"test": "original"}}
            paths.shell_config.write_text(json.dumps(original))
            idle_enabled = True
            calls = []
            def desktop_fixture(command, **kwargs):
                nonlocal idle_enabled
                command = [str(value) for value in command]
                calls.append(command)
                if command == ["omarchy-hyprland-session-locked"]:
                    return subprocess.CompletedProcess(command, 1, "", "")
                if command[:3] == ["omarchy-shell", "lock", "status"]:
                    output = {"locked": False, "secure": False, "requested": False, "passwordPam": True}
                elif command[:3] == ["omarchy-shell", "idle", "status"]:
                    output = {"enabled": idle_enabled}
                elif command[:3] == ["omarchy-shell", "idle", "disable"]:
                    idle_enabled = False
                    output = "disabled"
                elif command[:3] == ["omarchy-shell", "idle", "enable"]:
                    idle_enabled = True
                    output = "enabled"
                elif command[:3] == ["omarchy", "plugin", "list"]:
                    output = []
                elif command[:3] == ["omarchy", "plugin", "validate"]:
                    manifest = json.loads((Path(command[3]) / "manifest.json").read_text())
                    self.assertEqual(manifest["id"], installer.PLUGIN_ID)
                    output = "ok"
                elif command[:3] == ["omarchy", "restart", "shell"]:
                    output = "ok"
                elif command[:3] == ["omarchy-shell", "lock-screensaver", "status"]:
                    output = {"ready": True, "custom": True}
                else:
                    # Compiler, Python import, qmake and QML parsing are real.
                    return subprocess.run(command, **kwargs)
                text = json.dumps(output) if not isinstance(output, str) else output
                return subprocess.CompletedProcess(command, 0, text, "")
            options = SimpleNamespace(fps=60, blank_minutes=30, metrics=False, python=sys.executable,
                                      replace_existing_lock=False, dry_run=False)
            result = installer.install(paths, options, ROOT, runner=desktop_fixture)
            self.assertTrue(result["installed"])
            pointer = read_json(paths.active)
            self.assertEqual(pointer["fps"], 60)
            self.assertEqual(pointer["blankSeconds"], 1800)
            native = list(paths.data.glob("generations/*/renderer/liblocksaver.so"))
            self.assertEqual(len(native), 1)
            self.assertEqual(native[0].read_bytes()[:4], b"\x7fELF")
            service = next(paths.data.glob("generations/*/runtime/Service.qml"))
            self.assertIn("1800000", service.read_text())
            saver = service.with_name("SecureSaver.qml").read_text()
            self.assertNotIn("@@", saver)
            self.assertIn("%20", saver)
            installed = read_json(paths.shell_config)
            self.assertEqual(installed["idle"], original["idle"])
            self.assertTrue(idle_enabled)
            installed["bar"]["test"] = "changed after install"
            installed["plugins"].append({"id": "fixture.weather"})
            paths.shell_config.write_text(json.dumps(installed))
            installer.uninstall(paths, SimpleNamespace(dry_run=False), runner=desktop_fixture)
            final = read_json(paths.shell_config)
            self.assertEqual(final["bar"]["test"], "changed after install")
            self.assertEqual(final["idle"], original["idle"])
            self.assertIn({"id": "fixture.weather"}, final["plugins"])
            self.assertNotIn(installer.PLUGIN_ID, [installer.entry_id(x) for x in final["plugins"]])
            self.assertTrue(idle_enabled)
            self.assertFalse(paths.active.exists())


if __name__ == "__main__":
    unittest.main()
