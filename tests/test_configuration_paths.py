"""Configuration-path regression tests; only temporary files and simulated IPC."""
import json
import os
from pathlib import Path
import stat
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


class ConfigurationPathTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="locksaver-paths-")
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.paths = installer.Paths.from_environment(self.home, {})
        self.paths.shell_config.parent.mkdir(parents=True)
        self.original = {"bar": {"position": "top"}, "idle": {"lock": 300}}
        deployment.atomic_json(self.paths.shell_config, self.original)
        self.project = self.home / "source"
        self.project.mkdir()
        deployment.atomic_json(self.project / "manifest.json", {"id": installer.PLUGIN_ID})
        (self.project / "Service.qml").write_text("fixture")
        self.options = SimpleNamespace(fps=60, blank_minutes=30, metrics=False,
                                       python=sys.executable, replace_existing_lock=False,
                                       dry_run=False)
        self.calls = []
        self.idle_enabled = True
        self.hook = lambda command: None

    def runner(self, command, **kwargs):
        command = list(map(str, command))
        self.calls.append(command)
        self.hook(command)
        if command == ["omarchy-hyprland-session-locked"]:
            return subprocess.CompletedProcess(command, 1, "", "")
        if command == ["omarchy-shell", "lock", "status"]:
            value = {"locked": False, "requested": False, "secure": False, "passwordPam": True}
        elif command == ["omarchy-shell", "idle", "status"]:
            value = {"enabled": self.idle_enabled}
        elif command == ["omarchy-shell", "idle", "disable"]:
            self.idle_enabled = False
            value = {}
        elif command == ["omarchy-shell", "idle", "enable"]:
            self.idle_enabled = True
            value = {}
        elif command == ["omarchy", "plugin", "list", "--json"]:
            value = []
        elif command == ["omarchy-shell", "lock-screensaver", "status"]:
            value = {"ready": True, "custom": True}
        else:
            value = {}
        return subprocess.CompletedProcess(command, 0, json.dumps(value), "")

    def install(self):
        return deployment.install(self.paths, self.options, self.project, runner=self.runner,
                                  build=lambda *_: {"serviceUrl": "file:///fixture", "blankSeconds": 1800})

    def uninstall(self):
        return deployment.uninstall(self.paths, self.options, runner=self.runner)

    def seed_installed(self):
        current, snapshot = installer.enable_configuration(self.original, [])
        deployment.atomic_json(self.paths.shell_config, current)
        deployment.atomic_json(self.paths.snapshot, snapshot)
        deployment.atomic_json(self.paths.active, {"old": "pointer"})

    def assert_no_mutating_desktop_calls(self):
        for command in self.calls:
            self.assertNotIn(command, (["omarchy-shell", "idle", "disable"],
                                       ["omarchy-shell", "idle", "enable"],
                                       ["omarchy", "restart", "shell"]))
        self.assertTrue(self.idle_enabled)

    def make_link(self, path, *, dangling=False):
        target = self.home / ("missing-target.json" if dangling else "dotfile-target.json")
        original = b'{"private": "unchanged"}\n'
        if not dangling:
            target.write_bytes(original)
        path.unlink(missing_ok=True)
        path.symlink_to(target)
        return target, original

    def test_existing_and_dangling_links_refused_before_install_update_uninstall(self):
        for action in ("install", "update", "uninstall"):
            for dangling in (False, True):
                for dry_run in (False, True):
                    with self.subTest(action=action, dangling=dangling, dry_run=dry_run):
                        # A new fixture per combination avoids carrying installation state.
                        case = ConfigurationPathTests()
                        case.setUp()
                        self.addCleanup(case.doCleanups)
                        if action != "install":
                            case.seed_installed()
                        old_state = {p: p.read_bytes() for p in
                                     (case.paths.active, case.paths.snapshot) if p.exists()}
                        target, original = case.make_link(case.paths.shell_config, dangling=dangling)
                        case.options.dry_run = dry_run
                        with self.assertRaisesRegex(installer.InstallError, "symbolic-link"):
                            (case.uninstall if action == "uninstall" else case.install)()
                        self.assertTrue(case.paths.shell_config.is_symlink())
                        if dangling:
                            self.assertFalse(target.exists())
                        else:
                            self.assertEqual(target.read_bytes(), original)
                        for path, payload in old_state.items():
                            self.assertEqual(path.read_bytes(), payload)
                        case.assert_no_mutating_desktop_calls()
                        self.assertFalse(case.paths.data.exists())
                        self.assertFalse(case.paths.plugin.exists())

    def test_hardlinks_and_special_files_are_refused_before_side_effects(self):
        for kind in ("hardlink", "directory", "fifo"):
            with self.subTest(kind=kind):
                case = ConfigurationPathTests()
                case.setUp()
                self.addCleanup(case.doCleanups)
                path = case.paths.shell_config
                target = case.home / "other.json"
                original = path.read_bytes()
                path.unlink()
                if kind == "hardlink":
                    target.write_bytes(original)
                    os.link(target, path)
                elif kind == "directory":
                    path.mkdir()
                else:
                    os.mkfifo(path)
                with self.assertRaisesRegex(installer.InstallError, "hard-linked|non-regular"):
                    case.install()
                case.assert_no_mutating_desktop_calls()
                if kind == "hardlink":
                    self.assertEqual(target.read_bytes(), original)
                    self.assertEqual(path.stat().st_nlink, 2)
                elif kind == "directory":
                    self.assertTrue(path.is_dir())
                else:
                    self.assertTrue(stat.S_ISFIFO(path.lstat().st_mode))

    def test_linked_config_parent_is_refused_before_idle(self):
        parent = self.paths.shell_config.parent
        target = self.home / "managed-config"
        parent.rename(target)
        parent.symlink_to(target, target_is_directory=True)
        original = (target / "shell.json").read_bytes()
        with self.assertRaisesRegex(installer.InstallError, "parents are unsupported"):
            self.install()
        self.assertTrue(parent.is_symlink())
        self.assertEqual((target / "shell.json").read_bytes(), original)
        self.assert_no_mutating_desktop_calls()

    def test_linked_xdg_state_and_data_roots_are_refused_before_idle(self):
        for variable in ("XDG_STATE_HOME", "XDG_DATA_HOME"):
            with self.subTest(variable=variable):
                target = self.home / variable.lower()
                target.mkdir()
                linked = target.with_name(target.name + "-link")
                linked.symlink_to(target, target_is_directory=True)
                self.paths = installer.Paths.from_environment(self.home, {variable: str(linked)})
                self.calls.clear()
                with self.assertRaisesRegex(installer.InstallError, "parents are unsupported"):
                    self.install()
                self.assertTrue(linked.is_symlink())
                self.assertEqual(list(target.iterdir()), [])
                self.assert_no_mutating_desktop_calls()

    def test_regular_configuration_preserves_mode_and_missing_config_still_works(self):
        self.paths.shell_config.chmod(0o640)
        deployment.atomic_json(self.paths.shell_config, {"bar": "new"})
        self.assertEqual(stat.S_IMODE(self.paths.shell_config.stat().st_mode), 0o640)
        self.paths.shell_config.unlink()
        self.assertEqual(deployment.configuration_state(self.paths.shell_config), ({}, None))
        self.assertTrue(self.install()["installed"])
        self.assertTrue(self.uninstall()["uninstalled"])
        self.assertTrue(self.idle_enabled)

    def test_read_does_not_follow_a_link_swapped_at_open(self):
        real_open = os.open
        swapped = False
        target = None
        original = None
        def swap_then_open(name, flags, *args, **kwargs):
            nonlocal swapped, target, original
            if name == "shell.json" and flags & os.O_NOFOLLOW and not swapped:
                swapped = True
                target, original = self.make_link(self.paths.shell_config)
            return real_open(name, flags, *args, **kwargs)
        with patch.object(deployment.os, "open", side_effect=swap_then_open):
            with self.assertRaises(installer.InstallError):
                deployment.configuration_state(self.paths.shell_config)
        self.assertTrue(swapped)
        self.assertTrue(self.paths.shell_config.is_symlink())
        self.assertEqual(target.read_bytes(), original)

    def test_late_leaf_link_during_atomic_write_is_preserved(self):
        real_fsync = os.fsync
        target = original = None
        def swap_then_sync(descriptor):
            nonlocal target, original
            target, original = self.make_link(self.paths.shell_config)
            return real_fsync(descriptor)
        with patch.object(deployment.os, "fsync", side_effect=swap_then_sync):
            with self.assertRaisesRegex(installer.InstallError, "symbolic-link"):
                deployment.atomic_json(self.paths.shell_config, {"new": "value"})
        self.assertTrue(self.paths.shell_config.is_symlink())
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(list(self.paths.shell_config.parent.glob(".shell.json.*")), [])

    def test_late_parent_link_during_atomic_write_is_preserved(self):
        real_fsync = os.fsync
        parent = self.paths.shell_config.parent
        moved = self.home / "old-config"
        other = self.home / "other-config"
        other.mkdir()
        external = other / "shell.json"
        external.write_bytes(b"external")
        before = self.paths.shell_config.read_bytes()
        def swap_then_sync(descriptor):
            parent.rename(moved)
            parent.symlink_to(other, target_is_directory=True)
            return real_fsync(descriptor)
        with patch.object(deployment.os, "fsync", side_effect=swap_then_sync):
            with self.assertRaises(installer.InstallError):
                deployment.atomic_json(self.paths.shell_config, {"new": "value"})
        self.assertTrue(parent.is_symlink())
        self.assertEqual(external.read_bytes(), b"external")
        self.assertEqual((moved / "shell.json").read_bytes(), before)
        self.assertEqual(list(moved.glob(".shell.json.*")), [])

    def test_restore_absence_never_unlinks_a_substituted_link(self):
        target, original = self.make_link(self.paths.shell_config)
        with self.assertRaisesRegex(installer.InstallError, "symbolic-link"):
            deployment.restore_file(self.paths.shell_config, None)
        self.assertTrue(self.paths.shell_config.is_symlink())
        self.assertEqual(target.read_bytes(), original)

    def test_late_link_before_install_or_uninstall_commit_is_preserved(self):
        for action in ("install", "uninstall"):
            with self.subTest(action=action):
                case = ConfigurationPathTests()
                case.setUp()
                self.addCleanup(case.doCleanups)
                if action == "uninstall":
                    case.seed_installed()
                writer = deployment.atomic_json
                swapped = False
                target = original = None
                def swap_then_write(path, payload):
                    nonlocal swapped, target, original
                    if path == case.paths.shell_config and not swapped:
                        swapped = True
                        target, original = case.make_link(path)
                    return writer(path, payload)
                with patch.object(deployment, "atomic_json", side_effect=swap_then_write):
                    with self.assertRaisesRegex(installer.InstallError,
                                                "before configuration commit; edits preserved"):
                        (case.uninstall if action == "uninstall" else case.install)()
                self.assertTrue(swapped)
                self.assertTrue(case.paths.shell_config.is_symlink())
                self.assertEqual(target.read_bytes(), original)
                self.assertTrue(case.idle_enabled)
                self.assertNotIn(["omarchy", "restart", "shell"], case.calls)

    def test_failed_activation_rollback_preserves_link_and_reports_backup(self):
        self.paths.shell_config.unlink()
        target = original = None
        def fail_restart(command):
            nonlocal target, original
            if command == ["omarchy", "restart", "shell"]:
                target, original = self.make_link(self.paths.shell_config)
                raise installer.InstallError("simulated activation failure")
        self.hook = fail_restart
        with self.assertRaisesRegex(installer.InstallError, "rollback stopped:.*Recovery backup:"):
            self.install()
        self.assertTrue(self.paths.shell_config.is_symlink())
        self.assertEqual(target.read_bytes(), original)
        self.assertTrue(self.idle_enabled)
        backups = list((self.paths.state / "backups").glob("*/recovery.json"))
        self.assertEqual(len(backups), 1)
        self.assertFalse(json.loads(backups[0].read_text())["files"]["shell.json"]["present"])

    def test_absent_original_rollback_preserves_link_swapped_just_before_removal(self):
        self.paths.shell_config.unlink()
        restore = installer.restore_configuration
        target = original = None
        def restore_then_swap(config, snapshot):
            nonlocal target, original
            result = restore(config, snapshot)
            self.assertEqual(result, {})
            target, original = self.make_link(self.paths.shell_config)
            return result
        def fail_restart(command):
            if command == ["omarchy", "restart", "shell"]:
                raise installer.InstallError("simulated activation failure")
        self.hook = fail_restart
        with patch.object(deployment, "restore_configuration", side_effect=restore_then_swap):
            with self.assertRaisesRegex(installer.InstallError, "rollback stopped:.*Recovery backup:"):
                self.install()
        self.assertTrue(self.paths.shell_config.is_symlink())
        self.assertEqual(target.read_bytes(), original)
        self.assertTrue(self.idle_enabled)

    def test_uninstall_pointer_cleanup_preserves_substituted_link(self):
        self.seed_installed()
        target = original = None
        def swap_at_restart(command):
            nonlocal target, original
            if command == ["omarchy", "restart", "shell"]:
                target, original = self.make_link(self.paths.active)
        self.hook = swap_at_restart
        with self.assertRaisesRegex(installer.InstallError, "rollback stopped:.*Recovery backup:"):
            self.uninstall()
        self.assertTrue(self.paths.active.is_symlink())
        self.assertEqual(target.read_bytes(), original)
        self.assertTrue(self.paths.snapshot.exists())
        self.assertTrue(self.idle_enabled)


    def test_unsafe_operation_lock_and_backup_paths_are_refused_before_side_effects(self):
        for action in ("install", "uninstall"):
            for dry_run in (False, True):
                for kind in ("dangling-lock", "linked-lock", "fifo-lock", "hardlinked-lock", "backups"):
                    with self.subTest(action=action, dry_run=dry_run, kind=kind):
                        case = ConfigurationPathTests()
                        case.setUp()
                        self.addCleanup(case.doCleanups)
                        if action == "uninstall":
                            case.seed_installed()
                        case.paths.state.mkdir(parents=True, exist_ok=True)
                        target = case.home / "outside-state"
                        link = case.paths.state / ("backups" if kind == "backups" else "install.lock")
                        original_config = case.paths.shell_config.read_bytes()
                        if kind == "backups":
                            target.mkdir()
                            link.symlink_to(target, target_is_directory=True)
                        elif kind == "fifo-lock":
                            os.mkfifo(link)
                        elif kind == "hardlinked-lock":
                            target.write_bytes(b"original")
                            os.link(target, link)
                        else:
                            if kind == "linked-lock":
                                target.write_bytes(b"original")
                            link.symlink_to(target)
                        case.options.dry_run = dry_run
                        with self.assertRaises(installer.InstallError):
                            (case.uninstall if action == "uninstall" else case.install)()
                        case.assert_no_mutating_desktop_calls()
                        self.assertEqual(case.paths.shell_config.read_bytes(), original_config)
                        if kind == "backups":
                            self.assertTrue(link.is_symlink())
                            self.assertEqual(list(target.iterdir()), [])
                        elif kind == "dangling-lock":
                            self.assertTrue(link.is_symlink())
                            self.assertFalse(target.exists())
                        elif kind == "fifo-lock":
                            self.assertTrue(stat.S_ISFIFO(link.lstat().st_mode))
                        else:
                            self.assertEqual(target.read_bytes(), b"original")

    def test_late_operation_lock_link_is_not_followed(self):
        real_open = os.open
        target = self.home / "not-created"
        swapped = False
        def swap_then_open(name, flags, *args, **kwargs):
            nonlocal swapped
            if name == "install.lock" and flags & os.O_RDWR and not swapped:
                swapped = True
                (self.paths.state / "install.lock").symlink_to(target)
            return real_open(name, flags, *args, **kwargs)
        with patch.object(deployment.os, "open", side_effect=swap_then_open):
            with self.assertRaises(installer.InstallError):
                self.install()
        self.assertTrue(swapped)
        self.assertTrue((self.paths.state / "install.lock").is_symlink())
        self.assertFalse(target.exists())
        self.assert_no_mutating_desktop_calls()

    def test_late_backup_directory_link_is_never_followed(self):
        for managed_checkout in (False, True):
            with self.subTest(managed_checkout=managed_checkout):
                case = ConfigurationPathTests()
                case.setUp()
                self.addCleanup(case.doCleanups)
                if managed_checkout:
                    deployment.prepare_plugin_checkout(case.paths, case.project)
                target = case.home / "outside-backups"
                target.mkdir()
                backup_path = case.paths.state / "backups"
                def swap_after_build(command):
                    if command[:3] == ["omarchy", "plugin", "validate"]:
                        backup_path.symlink_to(target, target_is_directory=True)
                case.hook = swap_after_build
                with self.assertRaises((installer.InstallError, OSError)):
                    case.install()
                self.assertTrue(backup_path.is_symlink())
                self.assertEqual(list(target.iterdir()), [])
                self.assertTrue(case.idle_enabled)
                self.assertNotIn(["omarchy", "restart", "shell"], case.calls)
                if managed_checkout:
                    self.assertEqual((case.paths.plugin / "Service.qml").read_text(), "fixture")

    def test_operation_lock_preserves_errors_from_the_operation_body(self):
        with self.assertRaisesRegex(OSError, "operation body failed"):
            with deployment.operation_lock(self.paths.state / "install.lock"):
                raise OSError("operation body failed")


if __name__ == "__main__":
    unittest.main()
