import copy
import importlib.util
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
try:
    import installer
except ModuleNotFoundError:
    installer = None


class ConfigurationTests(unittest.TestCase):
    def test_enabling_plugin_preserves_unrelated_configuration(self):
        self.assertIsNotNone(installer, "The portable installer has not been implemented")
        before = {"idle": {"screensaver": 150, "lock": 300},
                  "bar": {"layout": {"right": [{"id": "demo.weather"}]}},
                  "plugins": [{"id": "demo.other", "enabled": True}],
                  "disabledPlugins": ["demo.hidden"]}
        saved = copy.deepcopy(before)
        after, snapshot = installer.enable_configuration(before, [])
        self.assertEqual(before, saved)
        self.assertEqual(after["idle"], before["idle"])
        self.assertEqual(after["bar"], before["bar"])
        self.assertIn({"id": "demo.other", "enabled": True}, after["plugins"])
        self.assertIn({"id": installer.PLUGIN_ID}, after["plugins"])
        self.assertIn("omarchy.lock", after["disabledPlugins"])
        self.assertIn("demo.hidden", after["disabledPlugins"])
        self.assertEqual(installer.restore_configuration(after, snapshot), before)


class GuardTests(unittest.TestCase):
    def test_locked_or_unknown_session_refuses_installation(self):
        guard = getattr(installer, "ensure_unlocked", None)
        self.assertTrue(callable(guard), "No fail-closed lock guard exists")
        from subprocess import CompletedProcess
        for state in ({"locked": True, "requested": True, "secure": True},
                      {"locked": False, "requested": True, "secure": False}, {}, None):
            def runner(command, **kwargs):
                return CompletedProcess(command, 0, json.dumps(state), "")
            with self.assertRaises(installer.InstallError):
                guard(runner)


    def test_missing_id_in_lock_status_is_not_unlocked_evidence(self):
        from subprocess import CompletedProcess
        def runner(command, **kwargs):
            return CompletedProcess(command, 0, '{"locked": false}', "")
        with self.assertRaises(installer.InstallError):
            installer.ensure_unlocked(runner)

    def test_rollback_requires_positive_unlocked_ipc_not_only_compositor(self):
        import deployment
        from subprocess import CompletedProcess
        unlocked = {"locked": False, "requested": False, "secure": False}
        for state in ({**unlocked, "requested": True}, {**unlocked, "pending": True},
                      {**unlocked, "locked": True}, {**unlocked, "secure": True},
                      {}, None, "malformed", "missing"):
            with self.subTest(state=state):
                def runner(command, **kwargs):
                    if command == ["omarchy-hyprland-session-locked"]:
                        return CompletedProcess(command, 1, "", "")
                    if state == "missing":
                        raise FileNotFoundError("no IPC")
                    return CompletedProcess(command, 0,
                                            "{" if state == "malformed" else json.dumps(state), "")
                self.assertFalse(deployment.safe_to_rollback(runner))
        def unlocked_runner(command, **kwargs):
            return CompletedProcess(command, 1 if len(command) == 1 else 0,
                                    json.dumps(unlocked), "")
        self.assertTrue(deployment.safe_to_rollback(unlocked_runner))

    def test_already_disabled_idle_remains_disabled(self):
        from subprocess import CompletedProcess
        calls = []
        def runner(command, **kwargs):
            calls.append(command)
            return CompletedProcess(command, 0, '{"enabled": false}', "")
        with installer.IdlePause(runner):
            pass
        self.assertNotIn(["omarchy-shell", "idle", "enable"], calls)
        self.assertNotIn(["omarchy-shell", "idle", "disable"], calls)


class IdlePauseTests(unittest.TestCase):
    def test_idle_is_restored_when_installation_raises(self):
        pause = getattr(installer, "IdlePause", None)
        self.assertTrue(callable(pause), "Temporary idle suppression lacks cleanup")
        from subprocess import CompletedProcess
        calls = []
        def runner(command, **kwargs):
            calls.append(command)
            if command[-1] == "status":
                return CompletedProcess(command, 0, json.dumps({"enabled": True}), "")
            return CompletedProcess(command, 0, "enabled", "")
        with self.assertRaisesRegex(RuntimeError, "build failed"):
            with pause(runner):
                raise RuntimeError("build failed")
        self.assertIn(["omarchy-shell", "idle", "disable"], calls)
        self.assertIn(["omarchy-shell", "idle", "enable"], calls)


class TransactionTests(unittest.TestCase):
    def test_failed_activation_restores_config_and_idle(self):
        action = getattr(installer, "install", None)
        paths_type = getattr(installer, "Paths", None)
        self.assertTrue(callable(action) and callable(paths_type), "Transactional install is missing")
        import tempfile
        from subprocess import CompletedProcess
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            paths = paths_type.from_environment(home, {"OMARCHY_PATH": "/usr/share/omarchy"})
            paths.shell_config.parent.mkdir(parents=True)
            original = {"idle": {"screensaver": 150, "lock": 300}, "bar": {"theme": "keep"}}
            paths.shell_config.write_text(json.dumps(original))
            calls = []
            activations = 0
            def runner(command, **kwargs):
                nonlocal activations
                calls.append(command)
                if command == ["omarchy-hyprland-session-locked"]:
                    return CompletedProcess(command, 1, "", "")
                if command[:3] == ["omarchy-shell", "lock", "status"]:
                    output = {"locked": False, "requested": False, "secure": False, "passwordPam": True}
                elif command[:3] == ["omarchy-shell", "idle", "status"]:
                    output = {"enabled": True}
                elif command[:3] == ["omarchy", "plugin", "list"]:
                    output = []
                elif command[:3] == ["omarchy", "restart", "shell"]:
                    activations += 1
                    if activations == 1:
                        return CompletedProcess(command, 1, "", "activation failed")
                    output = None
                else:
                    output = None
                return CompletedProcess(command, 0, json.dumps(output) if output is not None else "ok", "")
            def build(*args, **kwargs):
                target = paths.data / "generations" / "test" / "runtime" / "Service.qml"
                target.parent.mkdir(parents=True)
                target.write_text("// test runtime")
                return {"serviceUrl": target.as_uri(), "fps": 60, "blankSeconds": 1800}
            options = SimpleNamespace(fps=60, blank_minutes=30, metrics=False,
                                      python=sys.executable, replace_existing_lock=False, dry_run=False)
            with self.assertRaises(installer.InstallError):
                action(paths, options, ROOT, runner=runner, build=build)
            self.assertEqual(json.loads(paths.shell_config.read_text()), original)
            self.assertIn(["omarchy-shell", "idle", "enable"], calls)
            self.assertGreaterEqual(activations, 2)
            self.assertFalse(paths.active.exists())

    def test_uninstall_restores_previous_lock_but_keeps_new_widgets(self):
        uninstall = getattr(installer, "uninstall", None)
        self.assertTrue(callable(uninstall), "Safe uninstall is missing")
        import tempfile
        from subprocess import CompletedProcess
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as directory:
            paths = installer.Paths.from_environment(Path(directory), {})
            paths.shell_config.parent.mkdir(parents=True)
            original = {"idle": {"lock": 300}, "plugins": [{"id": "user.lock"}],
                        "disabledPlugins": ["omarchy.lock"], "cloneSourceRestores": ["user.lock"]}
            desired, snapshot = installer.enable_configuration(original, ["user.lock"])
            desired["bar"] = {"changed": True}
            desired["plugins"].append({"id": "new.weather"})
            paths.shell_config.write_text(json.dumps(desired))
            paths.state.mkdir(parents=True)
            paths.snapshot.write_text(json.dumps(snapshot))
            paths.active.write_text(json.dumps({"serviceUrl": "file:///runtime/Service.qml"}))
            def runner(command, **kwargs):
                if command == ["omarchy-hyprland-session-locked"]:
                    return CompletedProcess(command, 1, "", "")
                if command[:3] == ["omarchy-shell", "lock", "status"]:
                    output = {"locked": False, "requested": False, "secure": False, "passwordPam": True}
                elif command[:3] == ["omarchy-shell", "idle", "status"]:
                    output = {"enabled": False}
                elif command[:3] == ["omarchy", "plugin", "list"]:
                    output = []
                else:
                    output = {}
                return CompletedProcess(command, 0, json.dumps(output), "")
            uninstall(paths, SimpleNamespace(dry_run=False), runner=runner)
            restored = json.loads(paths.shell_config.read_text())
            self.assertEqual(restored["bar"], {"changed": True})
            self.assertIn({"id": "new.weather"}, restored["plugins"])
            self.assertIn({"id": "user.lock"}, restored["plugins"])
            self.assertNotIn({"id": installer.PLUGIN_ID}, restored["plugins"])
            self.assertFalse(paths.active.exists())
            self.assertFalse(paths.snapshot.exists())


    def test_configuration_edit_during_build_is_not_overwritten(self):
        import tempfile
        from subprocess import CompletedProcess
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as directory:
            paths = installer.Paths.from_environment(Path(directory), {})
            paths.shell_config.parent.mkdir(parents=True)
            original = {"bar": {"value": "before"}}
            paths.shell_config.write_text(json.dumps(original))
            def runner(command, **kwargs):
                if command == ["omarchy-hyprland-session-locked"]:
                    return CompletedProcess(command, 1, "", "")
                if command[:3] == ["omarchy-shell", "lock", "status"]:
                    data = {"locked": False, "requested": False, "secure": False, "passwordPam": True}
                elif command[:3] == ["omarchy-shell", "idle", "status"]:
                    data = {"enabled": False}
                elif command[:3] == ["omarchy", "plugin", "list"]:
                    data = []
                elif command[:3] == ["omarchy-shell", "lock-screensaver", "status"]:
                    data = {"ready": True, "custom": True}
                else:
                    data = {}
                return CompletedProcess(command, 0, json.dumps(data), "")
            def build(*args):
                paths.shell_config.write_text(json.dumps({"bar": {"value": "edited while building"}}))
                return {"serviceUrl": "file:///test/Service.qml", "blankSeconds": 1800}
            options = SimpleNamespace(fps=60, blank_minutes=30, metrics=False, python=sys.executable,
                                      replace_existing_lock=False, dry_run=False)
            installer.install(paths, options, ROOT, runner=runner, build=build)
            installed = json.loads(paths.shell_config.read_text())
            self.assertEqual(installed["bar"]["value"], "edited while building")
            self.assertIn({"id": installer.PLUGIN_ID}, installed["plugins"])


class CliTests(unittest.TestCase):
    def test_install_cli_explains_dry_run_without_changing_desktop(self):
        import subprocess
        result = subprocess.run([sys.executable, str(ROOT / "src/cli.py"), "install", "--help"],
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--dry-run", result.stdout)
        self.assertIn("--replace-existing-lock", result.stdout)


    def test_ssh_install_is_refused_before_desktop_calls(self):
        import os
        import subprocess
        result = subprocess.run([sys.executable, str(ROOT / "src/cli.py"), "install", "--dry-run"],
                                env={**os.environ, "PATH": "", "SSH_CONNECTION": "test-only"},
                                text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SSH", result.stderr)


class CheckoutTests(unittest.TestCase):
    def test_reinstall_from_source_updates_only_our_managed_copy(self):
        import tempfile
        import deployment
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            project = home / "source"
            project.mkdir()
            (project / "manifest.json").write_text(json.dumps({"id": installer.PLUGIN_ID}))
            (project / "Service.qml").write_text("first")
            paths = installer.Paths.from_environment(home, {})
            deployment.prepare_plugin_checkout(paths, project)
            (project / "Service.qml").write_text("updated")
            deployment.prepare_plugin_checkout(paths, project)
            self.assertEqual((paths.plugin / "Service.qml").read_text(), "updated")


if __name__ == "__main__":
    unittest.main()
