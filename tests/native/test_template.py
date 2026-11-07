import re
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]


class TemplateTests(unittest.TestCase):
    def test_template_has_exact_literal_tokens_and_secure_lifecycle(self):
        template = ROOT / "qml/SecureSaver.qml.in"
        self.assertTrue(template.is_file(), "SecureSaver template is missing")
        text = template.read_text()
        self.assertEqual(set(re.findall(r"@@[A-Z_]+@@", text)),
                         {"@@PYTHON@@", "@@BRIDGE@@", "@@RENDERER_URL@@", "@@FPS@@", "@@METRICS@@"})
        self.assertIn("import @@RENDERER_URL@@ as Native", text)
        self.assertIn("command: [@@PYTHON@@, @@BRIDGE@@", text)
        self.assertIn('"--fps", String(root.targetFps)', text)
        self.assertIn("readonly property real targetFps: @@FPS@@", text)
        self.assertIn("readonly property bool metricsEnabled: @@METRICS@@", text)
        self.assertIn("root.metricsEnabled && root.active", text)
        self.assertIn("interval: 100", text)
        self.assertIn("Component.onDestruction:", text)
        self.assertIn("Text.PlainText", text)
        self.assertIn('Quickshell.env("HOME")', text)
        self.assertIn("Native.TerminalFrame {", text)
        self.assertNotIn("/home/", text)
        self.assertNotIn("RichText", text)
        self.assertNotIn("FrameAnimation", text)
        self.assertNotIn("PAM", text)
