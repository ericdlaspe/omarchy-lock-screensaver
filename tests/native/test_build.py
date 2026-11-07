import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class BuildTests(unittest.TestCase):
    def test_relocated_source_and_immutable_module_directory(self):
        script = ROOT / "scripts/build-native"
        self.assertTrue(script.is_file(), "portable build-native is missing")
        self.assertTrue(os.access(script, os.X_OK), "build-native must be executable")
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source with spaces"
            shutil.copytree(ROOT / "native", source / "native")
            (source / "scripts").mkdir()
            shutil.copy2(script, source / "scripts/build-native")
            output = Path(temp) / "module with spaces"
            result = subprocess.run([str(source / "scripts/build-native"), str(output)],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            library = output / "liblocksaver.so"
            self.assertTrue(library.is_file())
            self.assertEqual((output / "qmldir").read_text(), "module LockSaver\nplugin locksaver\n")
            before = (library.stat().st_ino, hashlib.sha256(library.read_bytes()).hexdigest())
            result = subprocess.run([str(script), str(output)], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("refusing", result.stderr.lower())
            self.assertEqual(before, (library.stat().st_ino, hashlib.sha256(library.read_bytes()).hexdigest()))
            for args in ([], [str(output), "extra"]):
                result = subprocess.run([str(script), *args], capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("usage", result.stderr.lower())
