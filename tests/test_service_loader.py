import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("quickshell"), "Quickshell is required for the real loader probe")
class LoaderTests(unittest.TestCase):
    def run_probe(self, custom=None):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            config = home / "probe"
            config.mkdir()
            shutil.copy2(ROOT / "Service.qml", config / "PluginService.qml")
            stock = home / "omarchy/shell/plugins/lock"
            stock.mkdir(parents=True)
            (stock / "Service.qml").write_text('import QtQuick\nItem { property var shell: null; property string omarchyPath: ""; Component.onCompleted: console.log("STOCK-FIXTURE-LOADED") }\n')
            if custom is not None:
                runtime = home / "runtime.qml"
                runtime.write_text(custom)
                state = home / "state/omarchy-lock-screensaver"
                state.mkdir(parents=True)
                (state / "active.json").write_text(json.dumps({"serviceUrl": runtime.as_uri()}))
            (config / "shell.qml").write_text('''import QtQuick
import Quickshell
ShellRoot {
  PluginService { id: fixture }
  Timer { running: true; interval: 600; onTriggered: console.log("LOADER-PROBE", JSON.stringify({kind:fixture.sourceKind,error:fixture.loadError})) }
}
''')
            env = {**os.environ, "HOME": str(home), "XDG_STATE_HOME": str(home / "state"),
                   "OMARCHY_PATH": str(home / "omarchy"), "QT_QPA_PLATFORM": "offscreen",
                   "QT_FORCE_STDERR_LOGGING": "1"}
            process = subprocess.Popen(["quickshell", "-p", str(config)], stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, text=True, env=env)
            try:
                output, _ = process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                process.terminate()
                output, _ = process.communicate(timeout=3)
            records = [json.loads(line[line.index('{'):]) for line in output.splitlines()
                       if "LOADER-PROBE" in line]
            self.assertEqual(len(records), 1, output)
            return records[0], output

    def test_missing_runtime_uses_installed_stock_lock(self):
        record, output = self.run_probe()
        self.assertEqual(record["kind"], "stock")
        self.assertIn("STOCK-FIXTURE-LOADED", output)

    def test_configured_runtime_is_selected_once(self):
        record, output = self.run_probe('import QtQuick\nItem { property var shell: null; property string omarchyPath: ""; Component.onCompleted: console.log("CUSTOM-FIXTURE-LOADED") }\n')
        self.assertEqual(record["kind"], "custom")
        self.assertIn("CUSTOM-FIXTURE-LOADED", output)
        self.assertNotIn("STOCK-FIXTURE-LOADED", output)

    def test_component_load_failure_falls_back_before_any_lock_is_taken(self):
        record, output = self.run_probe('import QtQuick\nThisTypeDoesNotExist {}\n')
        self.assertEqual(record["kind"], "stock")
        self.assertTrue(record["error"])
        self.assertIn("STOCK-FIXTURE-LOADED", output)


if __name__ == "__main__":
    unittest.main()
