"""Runtime generation tests; no shell, PAM, or live configuration writes."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest

MODULE_PATH = Path(__file__).resolve().parents[1] / "src" / "runtime.py"
FIXTURE = Path(__file__).parent / "fixtures" / "current-stock"
TEMPLATE = '''import @@RENDERER_URL@@ as Renderer
import QtQuick
Item {
  property var command: [@@PYTHON@@, @@BRIDGE@@]
  property int fps: @@FPS@@
  property bool metrics: @@METRICS@@
}
'''


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.stock = self.root / "stock"
        shutil.copytree(FIXTURE, self.stock)
        (self.stock / "UPSTREAM.txt").unlink()
        self.output = self.root / "generated"
        self.template = self.root / "SecureSaver.qml.in"
        self.template.write_text(TEMPLATE, encoding="utf-8")
        self.options = dict(python_executable=self.root / "python", bridge_script=self.root / "bridge.py", renderer_dir=self.root / "renderer")

    def generate(self, **options):
        self.assertTrue(MODULE_PATH.exists(), "runtime generator is not implemented")
        spec = importlib.util.spec_from_file_location("runtime_under_test", MODULE_PATH)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.generate_runtime(self.stock, self.output, self.template, **(self.options | options))

    def test_patches_current_idle_timer_and_preview_only_preserving_authentication(self):
        original = (self.stock / "Service.qml").read_bytes()
        self.generate(blank_seconds=90)
        expected = original.replace(b"readonly property int idleMs: 5000", b"readonly property int idleMs: 90000").replace(
            b"      inputEnabled: false\n      loadBackground: root.previewVisible",
            b"      inputEnabled: false\n      previewSaver: true\n      loadBackground: root.previewVisible")
        expected = expected.replace(b"  function runWake() {\n",
            b"  property int promptActivitySerial: 0\n\n  function runWake() {\n    promptActivitySerial += 1\n").replace(
            b"        passwordText: root.enteredPassword\n",
            b"        passwordText: root.enteredPassword\n        promptActivitySerial: root.promptActivitySerial\n")
        self.assertEqual((self.output / "Service.qml").read_bytes(), expected)
        # Every byte outside the visual/timing/activity edits, including all PAM,
        # fingerprint, lock, unlock, and password functions, is unchanged.

    def test_lock_view_saver_retains_focused_password_input_and_original_handlers(self):
        original = (self.stock / "LockView.qml").read_text(encoding="utf-8")
        self.generate()
        view = (self.output / "LockView.qml").read_text(encoding="utf-8")
        self.assertIn("property bool previewSaver: false", view)
        self.assertIn("property bool saverDismissed: false", view)
        self.assertIn("readonly property bool saverActive: loadBackground && (previewSaver ||", view)
        self.assertIn("(inputEnabled && !saverDismissed && passwordText.length === 0 &&", view)
        self.assertIn("failureMessage.length === 0 && !authenticatingPassword))", view)
        self.assertIn("function dismissSaver() {\n    if (!inputEnabled || saverDismissed) return\n    saverDismissed = true\n    wakeRequested()\n    Qt.callLater(forcePasswordFocus)\n  }", view)
        self.assertIn("onInputEnabledChanged: {\n    if (inputEnabled) {\n      saverDismissed = false\n      Qt.callLater(forcePasswordFocus)\n    }\n  }", view)
        self.assertIn("onPasswordTextChanged: {\n    syncPasswordText()\n    if (passwordText.length > 0) dismissSaver()\n  }", view)
        self.assertIn("id: inputField\n      opacity: root.saverActive ? 0 : 1", view)
        self.assertNotIn("visible: !root.saverActive", view)
        original_keys = original.split("        Keys.onPressed: function(event) {", 1)[1].split("\n        }", 1)[0]
        self.assertIn("        Keys.onPressed: function(event) {\n          root.dismissSaver()" + original_keys + "\n        }", view)
        self.assertTrue(view.endswith("    SecureSaver {\n      anchors.fill: parent\n      active: root.saverActive\n      onDismissed: root.dismissSaver()\n    }\n  }\n}\n"))
        for handler in ("function restorePasswordFocus()", "function forcePasswordFocus()", "function syncPasswordText()", "        onAccepted:", "        onTextChanged:"):
            original_block = original.split(handler, 1)[1].split("\n" + ("  }" if handler.startswith("function") else "        }"), 1)[0]
            self.assertIn(handler + original_block, view)
        self.assertIn("enabled: root.inputEnabled && !root.authenticatingPassword", view)
        self.assertIn("Window.onActiveFocusItemChanged: Qt.callLater(restorePasswordFocus)", view)

    def test_invalid_options_fail_before_creating_output(self):
        for options in ({"fps": 14}, {"fps": 121}, {"fps": True}, {"fps": 60.0}, {"fps": "60"}, {"blank_seconds": 29}, {"blank_seconds": 86401}, {"blank_seconds": False}, {"blank_seconds": 90.0}, {"metrics": "false"}):
            with self.subTest(options=options):
                with self.assertRaises(ValueError):
                    self.generate(**options)
                self.assertFalse(self.output.exists())

    def test_supports_older_idle_interval_shape_without_changing_other_timers(self):
        service = self.stock / "Service.qml"
        original = service.read_bytes().replace(b"    readonly property int idleMs: 5000\n", b"").replace(b"    interval: idleMs\n", b"    interval: 5000\n")
        service.write_bytes(original)
        metadata = self.generate(blank_seconds=60)
        expected = original.replace(b"    interval: 5000\n", b"    interval: 60000\n").replace(
            b"      inputEnabled: false\n      loadBackground: root.previewVisible",
            b"      inputEnabled: false\n      previewSaver: true\n      loadBackground: root.previewVisible")
        expected = expected.replace(b"  function runWake() {\n",
            b"  property int promptActivitySerial: 0\n\n  function runWake() {\n    promptActivitySerial += 1\n").replace(
            b"        passwordText: root.enteredPassword\n",
            b"        passwordText: root.enteredPassword\n        promptActivitySerial: root.promptActivitySerial\n")
        self.assertEqual((self.output / "Service.qml").read_bytes(), expected)
        self.assertEqual(metadata["compatibility"], "interval")

    def test_missing_or_ambiguous_patch_markers_fail_closed(self):
        markers = {
            "Service.qml": ["    id: idleBlankTimer\n", "    readonly property int idleMs: 5000\n", "    interval: idleMs\n", "      inputEnabled: false\n      loadBackground: root.previewVisible\n", "  function runWake() {\n", "        passwordText: root.enteredPassword\n"],
            "LockView.qml": ["  property bool loadBackground: true\n", "  property bool syncingPasswordText: false\n", "  onPasswordTextChanged: syncPasswordText()\n", "  onInputEnabledChanged: {\n    if (inputEnabled) Qt.callLater(forcePasswordFocus)\n  }", "      id: inputField\n", "        Keys.onPressed: function(event) {\n"],
        }
        originals = {name: (self.stock / name).read_bytes() for name in markers}
        for name, values in markers.items():
            for index, marker in enumerate(values):
                for variant in ("missing", "ambiguous"):
                    with self.subTest(name=name, marker=marker, variant=variant):
                        self.output = self.root / f"bad-{name}-{index}-{variant}"
                        altered = originals[name].replace(marker.encode(), b"" if variant == "missing" else marker.encode() * 2)
                        (self.stock / name).write_bytes(altered)
                        with self.assertRaises(ValueError):
                            self.generate()
                        self.assertFalse(self.output.exists())
                        (self.stock / name).write_bytes(originals[name])

    def test_prompt_timeout_conflicts_fail_before_writing_output(self):
        cases = (
            '  onPromptActivitySerialChanged: console.log("upstream handler")\n',
            '  property int promptActivitySerial: 0\n',
            '  Timer { id: promptIdleTimer; interval: 1000 }\n',
        )
        path = self.stock / "LockView.qml"
        original = path.read_text(encoding="utf-8")
        for index, addition in enumerate(cases):
            with self.subTest(addition=addition):
                self.output = self.root / f"prompt-conflict-{index}"
                path.write_text(original.replace("  function forcePasswordFocus() {\n",
                    addition + "  function forcePasswordFocus() {\n"), encoding="utf-8")
                try:
                    with self.assertRaisesRegex(ValueError, "Unsupported stock|conflicting saver"):
                        self.generate()
                    self.assertFalse(self.output.exists())
                finally:
                    path.write_text(original, encoding="utf-8")

    def test_generated_prompt_timeout_is_unique_and_does_not_authenticate(self):
        self.generate()
        view = (self.output / "LockView.qml").read_text(encoding="utf-8")
        self.assertEqual(view.count("id: promptIdleTimer"), 1)
        self.assertEqual(view.count("onPromptActivitySerialChanged:"), 1)
        timer = view.split("    id: promptIdleTimer", 1)[1].split("  function forcePasswordFocus", 1)[0]
        self.assertIn("interval: 30000", timer)
        self.assertIn("!root.authenticatingPassword", timer)
        self.assertIn('root.passwordTextEdited("")', timer)
        self.assertIn("root.clearFailureRequested()", timer)
        self.assertNotIn("wakeRequested()", timer)
        self.assertNotIn("submitPassword", timer)
        self.assertNotIn("unlock", timer.lower())

    def test_template_missing_ambiguous_unknown_or_prequoted_tokens_fail_closed(self):
        cases = [TEMPLATE + "\n@@UNKNOWN@@\n", TEMPLATE.replace("@@PYTHON@@", '"prefix @@PYTHON@@ suffix"'), TEMPLATE.replace("@@BRIDGE@@", "/* @@BRIDGE@@ */")]
        for token in ("@@PYTHON@@", "@@BRIDGE@@", "@@RENDERER_URL@@", "@@FPS@@", "@@METRICS@@"):
            cases.extend((TEMPLATE.replace(token, ""), TEMPLATE.replace(token, token * 2), TEMPLATE.replace(token, '"' + token + '"')))
        for index, text in enumerate(cases):
            with self.subTest(index=index):
                self.output = self.root / f"template-bad-{index}"
                self.template.write_text(text, encoding="utf-8")
                with self.assertRaises(ValueError):
                    self.generate()
                self.assertFalse(self.output.exists())

    def test_json_template_quoting_unicode_quotes_spaces_and_token_like_paths(self):
        # Token-looking path text must not be interpreted as another substitution.
        self.options = dict(python_executable=self.root / '字 "python" @@FPS@@', bridge_script=self.root / "bridge\\name 'é'.py", renderer_dir=self.root / 'render "字" space')
        self.generate(fps=120, blank_seconds=86400, metrics=True)
        saver = (self.output / "SecureSaver.qml").read_text(encoding="utf-8")
        command = json.loads(saver.split("property var command: ")[1].split("\n")[0])
        self.assertEqual(command, [str(self.options["python_executable"]), str(self.options["bridge_script"])])
        imported = json.loads(saver.split("import ", 1)[1].split(" as Renderer", 1)[0])
        self.assertEqual(imported, self.options["renderer_dir"].resolve().as_uri())
        self.assertIn("property int fps: 120", saver)
        self.assertIn("property bool metrics: true", saver)

    def test_unsupported_upstream_structures_fail_before_writing_output(self):
        cases = [
            ("Service.qml", "  Timer {\n    id: idleBlankTimer", "  Item {\n    id: idleBlankTimer"),
            ("Service.qml", "      inputEnabled: false\n", "      inputEnabled: true\n"),
            ("Service.qml", "    LockView {\n      anchors.fill: parent", "    Item {\n      anchors.fill: parent"),
            ("Service.qml", "    interval: idleMs", "    interval: 7000"),
            ("LockView.qml", "  Rectangle {\n    anchors.fill: parent", "  Item {\n    anchors.fill: parent"),
            ("LockView.qml", "    BorderSurface {\n      id: inputField", "    Item {\n      id: inputField"),
            ("LockView.qml", "      TextInput {\n        id: passwordInput", "      TextEdit {\n        id: passwordInput"),
            ("LockView.qml", "  id: root", "  id: changedRoot"),
            ("LockView.qml", "enabled: root.inputEnabled && !root.authenticatingPassword", "enabled: false"),
            ("LockView.qml", "  property bool loadBackground: true\n", "/*\n  property bool loadBackground: true\n*/\n"),
            ("LockView.qml", "  }\n}\n", "  }\n  Item {\n  }\n}\n"),
            ("LockView.qml", "import QtQuick\n", "import QtQuick\n  property bool saverActive: false\n"),
            ("LockView.qml", "import QtQuick\n", "import QtQuick\n  property bool previewSaver: false\n"),
            ("LockView.qml", "import QtQuick\n", "import QtQuick\n  property bool saverDismissed: false\n"),
            ("LockView.qml", "import QtQuick\n", "import QtQuick\n  function dismissSaver() {}\n"),
            ("Service.qml", "import QtQuick\n", "import QtQuick\n// SecureSaver custom patched source\n"),
            ("Service.qml", "  id: root", "  id: root\n  property int promptActivitySerial: 0"),
            ("LockView.qml", "  id: root", "  id: root\n  property string bad: `unfamiliar ${template}`"),
            ("LockView.qml", "import QtQuick\n", "import QtQuick\n}\n"),
        ]
        for index, (name, marker, replacement) in enumerate(cases):
            with self.subTest(index=index, name=name):
                path = self.stock / name
                original = path.read_text(encoding="utf-8")
                self.output = self.root / f"unsupported-{index}"
                try:
                    path.write_text(original.replace(marker, replacement), encoding="utf-8")
                    with self.assertRaises(ValueError):
                        self.generate()
                    self.assertFalse(self.output.exists())
                finally:
                    path.write_text(original, encoding="utf-8")

    def test_missing_sources_or_symlinked_support_files_fail_closed(self):
        for name in ("Service.qml", "LockView.qml"):
            with self.subTest(missing=name):
                path = self.stock / name
                contents = path.read_bytes()
                path.unlink()
                try:
                    with self.assertRaises(ValueError):
                        self.generate()
                    self.assertFalse(self.output.exists())
                finally:
                    path.write_bytes(contents)
        outside = self.root / "outside.txt"
        outside.write_text("must not be copied")
        (self.stock / "linked.txt").symlink_to(outside)
        with self.assertRaises(ValueError):
            self.generate()
        self.assertFalse(self.output.exists())

    def test_io_failure_never_publishes_partial_output_or_leaves_staging_files(self):
        from unittest import mock
        original_write = Path.write_bytes
        count = 0

        def fail_later(path, data):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError("injected disk failure")
            return original_write(path, data)

        before = set(self.root.iterdir())
        with mock.patch.object(Path, "write_bytes", fail_later):
            with self.assertRaises(OSError):
                self.generate()
        self.assertFalse(self.output.exists())
        self.assertEqual(set(self.root.iterdir()), before)

    def test_output_must_be_new_and_separate_from_stock(self):
        for index, target in enumerate((self.output, self.stock / "generated", self.stock)):
            with self.subTest(target=target.name, index=index):
                self.output = target
                if index == 0:
                    target.mkdir()
                before = {path.relative_to(self.root): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
                with self.assertRaises((ValueError, FileExistsError)):
                    self.generate()
                after = {path.relative_to(self.root): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
                self.assertEqual(after, before)

    def test_existing_secure_saver_support_file_is_not_silently_overwritten(self):
        (self.stock / "SecureSaver.qml").write_text("Item {}\n")
        with self.assertRaises(ValueError):
            self.generate()
        self.assertFalse(self.output.exists())

    def test_duplicate_or_conflicting_property_shapes_are_rejected(self):
        cases = [
            ("Service.qml", "    interval: idleMs\n", "    interval: idleMs\n    interval: 5000\n"),
            ("Service.qml", "    readonly property int idleMs: 5000\n", "    readonly property int idleMs: 5000\n    readonly property int idleMs: 7000\n"),
            ("LockView.qml", "      id: inputField\n", "      id: inputField\n      opacity: 0.5\n"),
            ("Service.qml", "  WlSessionLock {", "  Item {"),
            ("Service.qml", "  PamContext {\n    id: passwordPam", "  Item {\n    id: passwordPam"),
        ]
        for index, (name, marker, replacement) in enumerate(cases):
            with self.subTest(index=index):
                path = self.stock / name
                original = path.read_text(encoding="utf-8")
                self.output = self.root / f"conflict-{index}"
                try:
                    path.write_text(original.replace(marker, replacement), encoding="utf-8")
                    with self.assertRaises(ValueError):
                        self.generate()
                    self.assertFalse(self.output.exists())
                finally:
                    path.write_text(original, encoding="utf-8")

    @unittest.skipUnless(Path("/usr/share/omarchy/shell/plugins/lock/Service.qml").is_file(), "Omarchy stock plugin is not installed")
    def test_live_installed_stock_generates_with_project_template_without_live_writes(self):
        self.stock = Path("/usr/share/omarchy/shell/plugins/lock")
        self.template = MODULE_PATH.parents[1] / "qml" / "SecureSaver.qml.in"
        before = {p.relative_to(self.stock).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in self.stock.rglob("*") if p.is_file()}
        metadata = self.generate()
        self.assertEqual(metadata["source_sha256"]["Service.qml"], before["Service.qml"])
        self.assertEqual(metadata["source_sha256"]["LockView.qml"], before["LockView.qml"])
        self.assertIn("previewSaver: true", (self.output / "Service.qml").read_text())
        self.assertIn("active: root.saverActive", (self.output / "LockView.qml").read_text())
        self.assertNotIn("@@", (self.output / "SecureSaver.qml").read_text())
        self.assertFalse((self.output / "manifest.json").exists())
        after = {p.relative_to(self.stock).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in self.stock.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_upstream_authentication_changes_are_copied_not_replaced_by_fixture(self):
        path = self.stock / "Service.qml"
        original = path.read_bytes().replace(b"  function beginLock() {", b'  function upstreamAddedAuthenticationGuard() {\n    // braces in comments are not objects: { }\n    var label = "quoted \\\" text } {"\n    return passwordPamConfigured\n  }\n\n  function beginLock() {')
        path.write_bytes(original)
        metadata = self.generate(fps=15, blank_seconds=30)
        expected = original.replace(b"readonly property int idleMs: 5000", b"readonly property int idleMs: 30000").replace(
            b"      inputEnabled: false\n      loadBackground: root.previewVisible",
            b"      inputEnabled: false\n      previewSaver: true\n      loadBackground: root.previewVisible")
        expected = expected.replace(b"  function runWake() {\n",
            b"  property int promptActivitySerial: 0\n\n  function runWake() {\n    promptActivitySerial += 1\n").replace(
            b"        passwordText: root.enteredPassword\n",
            b"        passwordText: root.enteredPassword\n        promptActivitySerial: root.promptActivitySerial\n")
        self.assertEqual((self.output / "Service.qml").read_bytes(), expected)
        self.assertEqual(metadata["source_sha256"]["Service.qml"], hashlib.sha256(original).hexdigest())

    def test_already_patched_sources_are_refused_without_double_patching(self):
        self.generate()
        generated = self.output
        self.output = self.root / "second-generation"
        for name in ("Service.qml", "LockView.qml"):
            with self.subTest(name=name):
                path = self.stock / name
                original = path.read_bytes()
                try:
                    path.write_bytes((generated / name).read_bytes())
                    with self.assertRaises(ValueError):
                        self.generate()
                    self.assertFalse(self.output.exists())
                finally:
                    path.write_bytes(original)

    def test_structural_guard_accepts_new_idle_constant_without_version_pinning(self):
        service = self.stock / "Service.qml"
        original = service.read_bytes().replace(b"readonly property int idleMs: 5000", b"readonly property int idleMs: 12000 // upstream default changed")
        service.write_bytes(original)
        metadata = self.generate(blank_seconds=90)
        result = (self.output / "Service.qml").read_bytes()
        self.assertIn(b"readonly property int idleMs: 90000 // upstream default changed", result)
        self.assertNotIn(b"idleMs: 12000", result)
        self.assertEqual(metadata["compatibility"], "idleMs")

    def test_older_interval_marker_in_comment_cannot_mask_unsupported_interval(self):
        path = self.stock / "Service.qml"
        source = path.read_text(encoding="utf-8").replace("    readonly property int idleMs: 5000\n", "").replace("    interval: idleMs\n", "/*\n    interval: 5000\n*/\n    interval: 7000\n")
        path.write_text(source, encoding="utf-8")
        with self.assertRaises(ValueError):
            self.generate()
        self.assertFalse(self.output.exists())

    def test_missing_password_focus_method_is_unsupported(self):
        path = self.stock / "LockView.qml"
        path.write_text(path.read_text(encoding="utf-8").replace("function forcePasswordFocus()", "function changedFocusMethod()"), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.generate()
        self.assertFalse(self.output.exists())

    def test_copies_support_files_renders_template_and_returns_source_hashes(self):
        nested = self.stock / "assets"
        nested.mkdir()
        (nested / "sample.bin").write_bytes(bytes(range(256)))
        (self.stock / "poster.sh").chmod(0o755)
        metadata = self.generate()
        self.assertFalse((self.output / "manifest.json").exists())
        self.assertEqual((self.output / "assets/sample.bin").read_bytes(), bytes(range(256)))
        self.assertEqual((self.output / "FingerprintModel.js").read_bytes(), (self.stock / "FingerprintModel.js").read_bytes())
        self.assertEqual((self.output / "poster.sh").stat().st_mode & 0o777, 0o755)
        saver = (self.output / "SecureSaver.qml").read_text(encoding="utf-8")
        self.assertNotIn("@@", saver)
        self.assertIn("property int fps: 60", saver)
        self.assertIn("property bool metrics: false", saver)
        for name in ("Service.qml", "LockView.qml"):
            self.assertEqual(metadata["source_sha256"][name], hashlib.sha256((self.stock / name).read_bytes()).hexdigest())
        self.assertNotIn(str(self.root), json.dumps(metadata))


if __name__ == "__main__":
    unittest.main()
