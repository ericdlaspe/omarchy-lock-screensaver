"""Optional real Quickshell/ttfx acceptance (no live config or lock changes).

Set LOCKSAVER_MODULE_URL to a locally built module's file:// URL.
"""
import json
import os
from pathlib import Path
import re
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]


def descendants(pid):
    found = []
    try:
        children = Path(f"/proc/{pid}/task/{pid}/children").read_text().split()
    except FileNotFoundError:
        return found
    for child in map(int, children):
        found.append(child)
        found.extend(descendants(child))
    return found


@unittest.skipUnless(os.environ.get("LOCKSAVER_MODULE_URL") and shutil.which("quickshell")
                     and shutil.which("ttfx"), "Real Quickshell, ttfx and a built native module are required")
class QuickshellTests(unittest.TestCase):
    def exercise(self, metrics, destroy_active=False):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            template = (ROOT / "qml/SecureSaver.qml.in").read_text()
            replacements = {"PYTHON": json.dumps(sys.executable),
                            "BRIDGE": json.dumps(str(ROOT / "src/bridge.py")),
                            "RENDERER_URL": json.dumps(os.environ["LOCKSAVER_MODULE_URL"]),
                            "FPS": "60", "METRICS": str(metrics).lower()}
            for key, value in replacements.items():
                template = template.replace("@@" + key + "@@", value)
            (temp / "SecureSaver.qml").write_text(template)
            (temp / "shell.qml").write_text('''import QtQuick
import Quickshell
ShellRoot {
  Window {
    id: window
    visible: true
    width: 2048
    height: 1152
    SecureSaver { id: saver; anchors.fill: parent; active: true }
    Timer {
      interval: %d; running: true
      onTriggered: {
        console.log("acceptance-grid", saver.columns, saver.rows)
        for (let child of saver.children) {
          if (child.cellWidth !== undefined)
            console.log("acceptance-cells", child.cellWidth, child.cellHeight)
        }
        if (%s) { Qt.quit(); return }
        saver.active = false
        console.log("acceptance-stopped", saver.runs.length)
        quitTimer.start()
      }
    }
    Timer { id: quitTimer; interval: 700; onTriggered: Qt.quit() }
  }
}
''' % ((6200 if metrics else 2200), str(destroy_active).lower()))
            env = {**os.environ, "QT_QPA_PLATFORM": "offscreen", "QT_QPA_PLATFORMTHEME": "",
                   "QSG_RHI_BACKEND": "software", "QSG_RENDER_LOOP": "basic",
                   "XDG_CACHE_HOME": str(temp / "cache")}
            proc = subprocess.Popen(["quickshell", "--no-color", "-p", str(temp)],
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
            try:
                time.sleep(1.5)
                pids = descendants(proc.pid)
                commands = []
                for pid in pids:
                    try:
                        commands.append(Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" "))
                    except FileNotFoundError:
                        pass
                try:
                    output = proc.communicate(timeout=12)[0].decode(errors="replace")
                except subprocess.TimeoutExpired:
                    proc.terminate()
                    output = proc.communicate(timeout=4)[0].decode(errors="replace")
                    self.fail("isolated Quickshell timed out:\n" + output)
                self.assertEqual(proc.returncode, 0, output)
                self.assertTrue(any(b"bridge.py" in cmd for cmd in commands), output)
                self.assertTrue(any(b"ttfx" in cmd for cmd in commands), output)
                if not destroy_active:
                    self.assertIn("acceptance-stopped 0", output)
                grid = re.search(r"acceptance-grid (\d+) (\d+)", output)
                cells = re.search(r"acceptance-cells ([0-9.]+) ([0-9.]+)", output)
                self.assertIsNotNone(grid, output)
                self.assertIsNotNone(cells, output)
                columns, rows = map(int, grid.groups())
                cell_width, cell_height = map(float, cells.groups())
                self.assertEqual(columns, max(40, min(300, int((2048 - 4) / cell_width))))
                self.assertEqual(rows, max(16, min(120, int((1152 - 4) / cell_height))))
                self.assertNotIn("ReferenceError", output)
                self.assertNotIn("TypeError", output)
                self.assertNotIn("Failed to load", output)
                # Process shutdown is asynchronous; allow the bridge to reap
                # its effect after the QML engine itself has exited.
                deadline = time.monotonic() + 3
                remaining = list(pids)
                while remaining and time.monotonic() < deadline:
                    remaining = [pid for pid in remaining if Path(f"/proc/{pid}").exists()]
                    if remaining:
                        time.sleep(.02)
                self.assertEqual(remaining, [], f"children survived close: {remaining}")
                samples = re.findall(r'lock-saver-fps (\{[^\n]+\})', output)
                if metrics:
                    self.assertTrue(samples, output)
                    sample = json.loads(samples[0])
                    self.assertGreater(sample["received"], 0)
                    self.assertGreater(sample["painted"], 0)
                    print("offscreen native metrics:", json.dumps(sample, sort_keys=True))
                else:
                    self.assertEqual(samples, [])
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    proc.communicate(timeout=4)

    def test_fullscreen_geometry_real_ttfx_and_metrics(self):
        self.exercise(True)

    def test_destroying_active_component_stops_children(self):
        self.exercise(False, destroy_active=True)

    def test_metrics_off_and_children_stop_on_close(self):
        self.exercise(False)
