"""Portable bridge contract; run with python -m unittest discover -s tests."""
import contextlib
import fcntl
import importlib.util
import io
import json
import os
import select
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("screensaver_bridge", ROOT / "src/bridge.py")
assert spec is not None and spec.loader is not None
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


class FrameTests(unittest.TestCase):
    def test_incremental_unicode_ansi_and_plain_text(self):
        decoder = bridge.TerminalDecoder(80, 24)
        payload = "\x1b[2;4H\x1b[38;2;12;34;56mé界<svg>".encode()
        for byte in payload:
            decoder.feed(bytes([byte]))
        runs = bridge.frame_runs(decoder.screen)
        self.assertEqual(runs[0][:3], [1, 3, "#0c2238"])
        self.assertEqual("".join(run[3] for run in runs), "é界<svg>")
        self.assertTrue(any(run[1] == 6 and run[3].startswith("<") for run in runs))
        self.assertNotIn("html", json.dumps({"runs": runs}))

    def test_escape_sequences_are_bounded_before_pyte(self):
        for prefix, body, suffix in ((b"\x1b]2;", b"x", b"\x07"),
                                     (b"\x1b[", b"1;", b"m"),
                                     (b"\x1b[", b"1", b"m"),
                                     (b"\x1bP", b"x", b"\x1b\\"),
                                     (b"\x1bX", b"x", b"\x1b\\"),
                                     (b"\x1b^", b"x", b"\x1b\\"),
                                     (b"\x1b_", b"x", b"\x1b\\"),
                                     (b"\x1b", b" ", b"B"),
                                     ("\x90".encode(), b"x", "\x9c".encode()),
                                     ("\x98".encode(), b"x", "\x9c".encode()),
                                     ("\x9e".encode(), b"x", "\x9c".encode()),
                                     ("\x9f".encode(), b"x", "\x9c".encode()),
                                     ("\x9d2;".encode(), b"x", "\x9c".encode()),
                                     ("\x9b".encode(), b"1;", b"m")):
            with self.subTest(prefix=prefix, body=body):
                decoder = bridge.TerminalDecoder(40, 16)
                decoder.feed(prefix)
                for _ in range(64):
                    decoder.feed(body * 1024)
                    # Suspended pyte parser locals expose its otherwise hidden
                    # OSC string / CSI digit and parameter accumulation.
                    state = decoder.stream._parser.gi_frame.f_locals
                    retained = sum(len(value) for key, value in state.items()
                                   if key in ("param", "current", "params"))
                    self.assertLessEqual(retained, 1024)
                    self.assertLessEqual(len(decoder.ansi.pending), 1024)
                    self.assertEqual(bridge.frame_runs(decoder.screen), [])
                for byte in suffix:
                    decoder.feed(bytes([byte]))
                decoder.feed(b"\x1b[31mZ")
                self.assertEqual(bridge.frame_runs(decoder.screen),
                                 [[0, 0, "#cc3333", "Z"]])

    def test_repeated_chunked_cursor_saves_keep_only_latest_state(self):
        decoder = bridge.TerminalDecoder(40, 16)
        for batch in range(20):
            row, col = batch % 16 + 1, batch + 1
            decoder.feed(f"\x1b[{row};{col}H\x1b[1;44;38;2;12;34;{batch}m".encode())
            # Repeated saves span feed calls, including a split ESC 7.
            decoder.feed(b"\x1b7" * 999 + b"\x1b")
            decoder.feed(b"7")
            self.assertEqual(len(decoder.screen.savepoints), 1)

        expected_cursor = (decoder.screen.cursor.y, decoder.screen.cursor.x)
        decoder.feed(b"\x1b[1;1H\x1b[0;31m\x1b")
        decoder.feed(b"8")
        self.assertEqual((decoder.screen.cursor.y, decoder.screen.cursor.x),
                         expected_cursor)
        self.assertEqual(decoder.screen.cursor.attrs.fg, "0c2213")
        self.assertEqual(decoder.screen.cursor.attrs.bg, "blue")
        self.assertTrue(decoder.screen.cursor.attrs.bold)
        decoder.feed(b"Z")
        self.assertEqual(bridge.frame_runs(decoder.screen),
                         [[*expected_cursor, "#0c2213", "Z"]])

    def test_huge_osc_title_is_discarded(self):
        decoder = bridge.TerminalDecoder(40, 16)
        decoder.feed(b"\x1b]0;" + b"x" * 65536 + b"\x1b\\OK")
        self.assertLessEqual(len(decoder.screen.title), 1024)
        self.assertLessEqual(len(decoder.screen.icon_name), 1024)
        self.assertEqual(bridge.frame_runs(decoder.screen),
                         [[0, 0, "#ffa500", "OK"]])

    def test_combining_cells_are_bounded_across_chunks(self):
        decoder = bridge.TerminalDecoder(40, 16)
        decoder.feed(b"x")
        for _ in range(8):
            decoder.feed(("\u0301" * 1024).encode())
            self.assertLessEqual(len(decoder.screen.buffer[0][0].data), 16)
        decoder.feed(b"\x1b[32mY")
        self.assertEqual(bridge.frame_runs(decoder.screen)[-1],
                         [0, 1, "#55cc55", "Y"])

    def test_huge_combining_chunk_is_bounded_during_decoding(self):
        import unicodedata
        normalize = unicodedata.normalize

        def bounded_normalize(form, value):
            self.assertLessEqual(len(value), 33,
                                 "cell grew before post-feed pruning")
            return normalize(form, value)

        decoder = bridge.TerminalDecoder(40, 16)
        with mock.patch.object(unicodedata, "normalize", bounded_normalize):
            decoder.feed(("界" + "\u0301" * 65536).encode())
        for line in decoder.screen.buffer.values():
            for cell in line.values():
                self.assertLessEqual(len(cell.data), 16)
                self.assertLessEqual(len(cell.data.encode("utf-16-le")) // 2, 32)

    def test_oversized_run_text_is_rejected_before_serialization(self):
        decoder = bridge.TerminalDecoder(40, 16)
        decoder.screen.buffer[0][0] = decoder.screen.default_char._replace(data="x" * 65536)
        with self.assertRaisesRegex(ValueError, "cell"):
            bridge.frame_runs(decoder.screen)

    def test_maximum_unicode_grid_fits_wire_budget(self):
        decoder = bridge.TerminalDecoder(300, 120)
        # One astral base plus 15 astral combining marks: worst-case UTF-8
        # bytes per allowed cell, and exactly the native 32 UTF-16-unit limit.
        text = "\U0001f642" + "\U0001d185" * 15
        cell = decoder.screen.default_char._replace(data=text)
        for row in range(120):
            for col in range(300):
                decoder.screen.buffer[row][col] = cell
        runs = bridge.frame_runs(decoder.screen)
        self.assertEqual(len(runs), 36000)
        wire = (json.dumps({"runs": runs}, ensure_ascii=False,
                           separators=(",", ":")) + "\n").encode()
        self.assertLessEqual(len(wire), bridge.MAX_FRAME_BYTES)
        self.assertLessEqual(bridge.MAX_FRAME_BYTES, 4 * 1024 * 1024)

    def test_bright_and_indexed_ansi_colors(self):
        decoder = bridge.TerminalDecoder(80, 24)
        decoder.feed(b"\x1b[91mA\x1b[38;5;196mB")
        self.assertEqual(bridge.frame_runs(decoder.screen),
                         [[0, 0, "#ff5555", "A"], [0, 1, "#ff0000", "B"]])

    def test_incomplete_utf8_is_replaced_at_eof(self):
        decoder = bridge.TerminalDecoder(80, 24)
        decoder.feed(b"\xe2")
        decoder.feed(b"", final=True)
        self.assertEqual(bridge.frame_runs(decoder.screen), [[0, 0, "#ffa500", "\ufffd"]])

    def test_dynamic_grid_extremes_and_ansi_colors(self):
        decoder = bridge.TerminalDecoder(300, 120)
        decoder.feed(b"\x1b[120;300H\x1b[31mZ")
        self.assertEqual(bridge.frame_runs(decoder.screen), [[119, 299, "#cc3333", "Z"]])
        self.assertEqual(bridge.color("aBc123"), "#aBc123")
        self.assertEqual(bridge.color("not-a-color"), "#ffa500")


class ArgumentTests(unittest.TestCase):
    def test_fps_is_finite_bounded_and_grid_is_clamped(self):
        self.assertEqual(bridge.parse_args([]).fps, 60)
        for fps in ("15", "120", "59.94"):
            self.assertEqual(bridge.parse_args(["--fps", fps]).fps, float(fps))
        for fps in ("nan", "inf", "-inf", "0", "14.99", "120.01"):
            with self.subTest(fps=fps), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                bridge.parse_args(["--fps=" + fps])
        args = bridge.parse_args(["--cols", "999", "--rows", "999"])
        self.assertEqual((args.cols, args.rows), (300, 120))
        args = bridge.parse_args(["--cols", "0", "--rows", "0"])
        self.assertEqual((args.cols, args.rows), (40, 16))


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        brand = self.home / ".config/omarchy/branding/screensaver.txt"
        brand.parent.mkdir(parents=True)
        brand.write_text("OWN BRAND 界 <svg>")
        executable = self.home / "ttfx"
        executable.write_text("#!" + sys.executable + "\n" + '''import json, os, signal, sys, time
from pathlib import Path
home = Path(os.environ["HOME"])
with (home / "starts").open("a") as output:
    output.write(json.dumps({"pid": os.getpid(), "args": sys.argv[1:]}) + "\\n")
if os.environ.get("EXIT_IMMEDIATELY"):
    sys.exit(0)
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
if os.environ.get("FILL_SCREEN"):
    grandchild = os.fork()
    if not grandchild:
        (home / "grandchild").write_text(str(os.getpid()))
        while True:
            time.sleep(.05)
    def wait_but_do_not_exit(*_):
        os.waitpid(grandchild, 0)
    # The grandchild exits on group TERM and is reaped here; the effect itself
    # remains alive to exercise the bridge's group KILL escalation and wait().
    signal.signal(signal.SIGTERM, wait_but_do_not_exit)
    os.write(1, b"\\x1b[H" + b"X" * (300 * 120))
else:
    os.write(1, b"\\x1b[2J\\x1b[1;1H\\x1b[31mHELLO")
while True:
    time.sleep(.05)
''')
        executable.chmod(0o755)
        self.env = {**os.environ, "HOME": str(self.home),
                    "PATH": str(self.home) + os.pathsep + os.environ["PATH"]}

    def start(self, cols=179, rows=50, **env):
        proc = subprocess.Popen([sys.executable, str(ROOT / "src/bridge.py"),
                                 "--cols", str(cols), "--rows", str(rows), "--fps", "60"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env={**self.env, **env})
        self.addCleanup(self.stop, proc)
        return proc

    @staticmethod
    def stop(proc):
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=4)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        if proc.stdout:
            proc.stdout.close()
        if proc.stderr:
            proc.stderr.close()

    def starts(self):
        path = self.home / "starts"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def wait_for_child(self, proc):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            if self.starts():
                return self.starts()[0]
            self.assertIsNone(proc.poll(), "bridge exited before spawning ttfx")
            time.sleep(.02)
        self.fail("ttfx not started")

    def assert_child_gone(self, pid):
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_real_pty_branding_and_signal_cleanup(self):
        proc = self.start()
        started = self.wait_for_child(proc)
        self.assertIn(str(self.home / ".config/omarchy/branding/screensaver.txt"), started["args"])
        self.assertIn("179", started["args"])
        deadline = time.monotonic() + 3
        frame = None
        while time.monotonic() < deadline:
            if select.select([proc.stdout], [], [], 1)[0]:
                frame = json.loads(proc.stdout.readline())
                if frame["runs"]:
                    break
        self.assertEqual(frame["runs"], [[0, 0, "#cc3333", "HELLO"]])
        proc.terminate()
        self.assertEqual(proc.wait(timeout=4), 0)
        self.assert_child_gone(started["pid"])

    def test_broken_pipe_terminates_child_without_traceback(self):
        proc = self.start()
        started = self.wait_for_child(proc)
        proc.stdout.close()
        self.assertEqual(proc.wait(timeout=4), 0)
        self.assert_child_gone(started["pid"])
        self.assertNotIn(b"Traceback", proc.stderr.read())

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux pipe/process probe")
    def test_sigterm_exits_with_full_unread_stdout_and_reaps_effect_group(self):
        proc = self.start(cols=300, rows=120, FILL_SCREEN="1")
        assert proc.stdout is not None and proc.stderr is not None
        fcntl.fcntl(proc.stdout, fcntl.F_SETPIPE_SZ, 4096)
        started = self.wait_for_child(proc)
        deadline = time.monotonic() + 3
        grandchild_file = self.home / "grandchild"
        while not grandchild_file.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertTrue(grandchild_file.exists(), "effect grandchild not started")
        grandchild = int(grandchild_file.read_text())
        # Keep the read end OPEN throughout the assertion, without ever reading
        # a byte. Even one 300x120 frame exceeds this pipe's capacity.
        self.assertTrue(select.select([proc.stdout], [], [], 3)[0])
        time.sleep(.3)

        def cpu_ticks():
            fields = Path(f"/proc/{proc.pid}/stat").read_text().split()
            return int(fields[13]) + int(fields[14])

        before = cpu_ticks()
        time.sleep(.4)
        cpu_seconds = (cpu_ticks() - before) / os.sysconf("SC_CLK_TCK")
        self.assertLess(cpu_seconds, .25, "busy loop while stdout is backpressured")
        self.assertEqual(len(self.starts()), 1)
        proc.terminate()
        try:
            code = proc.wait(timeout=2.5)
        except subprocess.TimeoutExpired:
            # Failure cleanup closes (never drains) the pipe so the old bridge
            # can escape its blocked write and reap the isolated fixture.
            proc.stdout.close()
            proc.wait(timeout=4)
            self.fail("SIGTERM did not exit while stdout remained open and unread")
        self.assertEqual(code, 0)
        self.assert_child_gone(started["pid"])
        self.assert_child_gone(grandchild)
        self.assertNotIn(b"Traceback", proc.stderr.read())

    def test_short_effects_back_off_instead_of_busy_restarting(self):
        proc = self.start(EXIT_IMMEDIATELY="1")
        self.wait_for_child(proc)
        time.sleep(.7)
        proc.terminate()
        self.assertEqual(proc.wait(timeout=4), 0)
        self.assertLessEqual(len(self.starts()), 4)


if __name__ == "__main__":
    unittest.main()
