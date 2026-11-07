#!/usr/bin/env python3
"""Decode real ttfx PTY output into bounded, plain-text terminal color runs."""
import argparse
import math
import codecs
import ctypes
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import signal
import struct
import subprocess
import sys
import termios
import threading
import time
import unicodedata
from functools import lru_cache

import pyte


def bounded_fps(value):
    try:
        fps = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("FPS must be a finite number in 15..120") from error
    if not math.isfinite(fps) or not 15 <= fps <= 120:
        raise argparse.ArgumentTypeError("FPS must be a finite number in 15..120")
    return fps


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cols", type=int, default=100)
    parser.add_argument("--rows", type=int, default=24)
    parser.add_argument("--fps", type=bounded_fps, default=60.0)
    args = parser.parse_args(argv)
    args.cols = max(40, min(300, args.cols))
    args.rows = max(16, min(120, args.rows))
    return args


@lru_cache(maxsize=2048)
def color(value):
    if len(value) == 6 and all(c in "0123456789abcdefABCDEF" for c in value):
        return "#" + value
    return {"black": "#000000", "red": "#cc3333", "green": "#55cc55",
            "yellow": "#ffcc33", "blue": "#5588ff", "magenta": "#dd66dd",
            "cyan": "#55dddd", "white": "#eeeeee",
            "brightblack": "#555555", "brightred": "#ff5555",
            "brightgreen": "#55ff55", "brightyellow": "#ffff55",
            "brightblue": "#5555ff", "brightmagenta": "#ff55ff",
            "brightcyan": "#55ffff", "brightwhite": "#ffffff"}.get(value, "#ffa500")


MAX_ESCAPE = 1024
DECODE_CHUNK = 4096
# Sixteen Unicode scalars fit the native renderer's 32 UTF-16-unit ceiling.
MAX_CELL = 16
MAX_FRAME_BYTES = 4 * 1024 * 1024


class BoundedScreen(pyte.Screen):
    def save_cursor(self):
        # Keep one VT-style saved cursor, not an unbounded ESC 7 stack.
        self.savepoints.clear()
        super().save_cursor()

    def draw(self, data):
        start = 0
        for offset, char in enumerate(data):
            if not unicodedata.combining(char):
                continue
            if start < offset:
                super().draw(data[start:offset])
            # Delegate wrapping, wide-cell handling and NFC to pyte, but bound
            # the affected cell after EACH mark, not after an arbitrary feed.
            super().draw(char)
            y, x = self.cursor.y, self.cursor.x
            if x:
                x -= 1
            elif y:
                y, x = y - 1, self.columns - 1
            else:
                start = offset + 1
                continue
            cell = self.buffer[y][x]
            if len(cell.data) > MAX_CELL:
                self.buffer[y][x] = cell._replace(data=cell.data[:MAX_CELL])
            start = offset + 1
        if start < len(data):
            super().draw(data[start:])


class BoundedANSI:
    """Withhold incomplete escapes; never let pyte accumulate untrusted input.

    OSC metadata and unsupported DCS/SOS/PM/APC strings are unused, so consume
    them without retaining their payload. Overlong CSI/ESC sequences remain in
    discard mode until their terminator, even across feed boundaries.
    """
    def __init__(self):
        self.state = "text"
        self.pending = ""
        self.discard = False
        self.osc = False
        self.string_escape = False

    def feed(self, text):
        output = []
        for char in text:
            if self.state == "string":
                if (char in "\x18\x1a\x9c" or (self.osc and char == "\x07")
                        or (self.string_escape and char == "\\")):
                    self.state = "text"
                self.string_escape = char == "\x1b"
                continue
            if char in "\x18\x1a" and self.state != "text":
                self.state, self.pending = "text", ""
                continue
            if char == "\x1b":
                self.state, self.pending, self.discard = "escape", char, False
                continue
            if self.state == "text":
                if char == "\x9b":
                    self.state, self.pending, self.discard = "csi", char, False
                elif char in "\x90\x98\x9d\x9e\x9f":
                    self.state, self.osc = "string", char == "\x9d"
                    self.string_escape = False
                else:
                    output.append(char)
                continue
            if self.state == "escape" and self.pending == "\x1b":
                if char == "[":
                    self.state, self.pending = "csi", "\x1b["
                    continue
                elif char in "]PX^_":
                    self.state, self.pending, self.osc = "string", "", char == "]"
                    self.string_escape = False
                    continue
            if not self.discard:
                if len(self.pending) < MAX_ESCAPE:
                    self.pending += char
                else:
                    self.pending, self.discard = "", True
            if self.state == "csi":
                complete = "@" <= char <= "~"
                # Do not hand malformed/non-ASCII parameters to pyte's int().
                if not (" " <= char <= "~" or char in "\x00\x07\x08\t\n\v\f\r\x7f"):
                    self.pending, self.discard = "", True
            else:
                complete = "0" <= char <= "~"
            if complete:
                if not self.discard:
                    output.append(self.pending)
                self.state, self.pending = "text", ""
        return "".join(output)


class TerminalDecoder:
    """Keep UTF-8 state across arbitrary PTY read boundaries."""
    def __init__(self, columns, rows):
        self.screen = BoundedScreen(columns, rows)
        self.stream = pyte.Stream(self.screen)
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.ansi = BoundedANSI()

    def feed(self, data, final=False):
        for offset in range(0, len(data), DECODE_CHUNK):
            text = self.decoder.decode(data[offset:offset + DECODE_CHUNK])
            self.stream.feed(self.ansi.feed(text))
        if final:
            self.stream.feed(self.ansi.feed(self.decoder.decode(b"", final=True)))


def frame_runs(screen):
    runs = []
    for row in range(screen.lines):
        chars, start, ink = [], 0, None

        def flush():
            text = "".join(chars).rstrip(" ")
            if text:
                runs.append([row, start, ink, text])
            chars.clear()

        for col in range(screen.columns):
            cell = screen.buffer[row][col]
            if len(cell.data) > MAX_CELL:
                raise ValueError("oversized terminal cell")
            next_ink = color(cell.fg)
            # Wide/combining glyphs get their own absolute cell position. The
            # empty continuation cell of a wide glyph must not shift later text.
            if len(cell.data) != 1 or not cell.data.isascii():
                flush()
                if cell.data and cell.data.strip():
                    runs.append([row, col, next_ink, cell.data])
                ink = None
                continue
            if next_ink != ink:
                flush()
                start, ink = col, next_ink
            if not chars and cell.data == " ":
                start = col + 1
            else:
                chars.append(cell.data)
        flush()
    return runs


def child_parent_death_signal():
    # Omarchy is Linux; retain a portable fallback for other POSIX hosts.
    if sys.platform.startswith("linux"):
        parent = os.getppid()
        ctypes.CDLL(None).prctl(1, signal.SIGTERM, 0, 0, 0)
        if parent == 1 or os.getppid() != parent:
            os.kill(os.getpid(), signal.SIGTERM)


def terminate_child(child):
    """Reap the effect and terminate its entire isolated process group."""
    try:
        os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        child.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass
    finally:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait()


def run(args, stopping, output):
    branding = Path.home() / ".config/omarchy/branding/screensaver.txt"
    if not branding.is_file():
        print("screensaver branding missing", file=sys.stderr)
        return 1
    period = 1 / args.fps
    output_fd = output.fileno()
    os.set_blocking(output_fd, False)
    pending = memoryview(b"")
    command = ["ttfx", "-i", str(branding), "--frame-rate", format(args.fps, "g"),
               "--canvas-width", str(args.cols), "--canvas-height", str(args.rows),
               "--reuse-canvas", "--anchor-canvas", "c", "--anchor-text", "c",
               "--random-effect", "--no-eol", "--no-restore-cursor"]
    while not stopping.is_set():
        master, slave = pty.openpty()
        child = None
        decoder = TerminalDecoder(args.cols, args.rows)
        next_frame = time.monotonic()
        try:
            fcntl.ioctl(slave, termios.TIOCSWINSZ,
                        struct.pack("HHHH", args.rows, args.cols, 0, 0))
            try:
                child = subprocess.Popen(command, stdin=slave, stdout=slave,
                                         stderr=subprocess.DEVNULL,
                                         env={**os.environ, "TERM": "xterm-256color"},
                                         start_new_session=True,
                                         preexec_fn=child_parent_death_signal)
            finally:
                os.close(slave)
                slave = None
            while not stopping.is_set():
                wait = min(.1, max(0, next_frame - time.monotonic()))
                ready, writable, _ = select.select(
                    [master], [output_fd] if pending else [], [], wait)
                if stopping.is_set():
                    break
                if writable:
                    try:
                        written = os.write(output_fd, pending)
                    except BlockingIOError:
                        pass
                    else:
                        pending = pending[written:]
                if ready:
                    try:
                        data = os.read(master, 65536)
                    except OSError as error:
                        if error.errno == 5:  # Linux PTY EOF is EIO.
                            break
                        raise
                    if not data:
                        break
                    decoder.feed(data)
                if time.monotonic() >= next_frame:
                    # Keep at most ONE frame. Never replace a partially written
                    # JSON line; skip stale snapshots until it has been sent.
                    if not pending:
                        payload = (json.dumps({"runs": frame_runs(decoder.screen)},
                                              ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
                        if len(payload) <= MAX_FRAME_BYTES:
                            pending = memoryview(payload)
                    next_frame += period
                    now = time.monotonic()
                    if next_frame < now:
                        next_frame = now + period  # Never send catch-up bursts.
                if child.poll() is not None and not ready:
                    break
        finally:
            if child is not None:
                terminate_child(child)
            os.close(master)
            if slave is not None:
                os.close(slave)
        # Also throttle effects that exit immediately. Event.wait is interruptible
        # and avoids both a spin loop and delayed shutdown during restart backoff.
        stopping.wait(.25)
    return 0


def main(argv=None):
    args = parse_args(argv)
    stopping = threading.Event()
    for number in (signal.SIGTERM, signal.SIGINT):
        signal.signal(number, lambda *_: stopping.set())
    try:
        return run(args, stopping, sys.stdout)
    except BrokenPipeError:
        # Avoid a second BrokenPipe in Python's final stdout-buffer flush.
        with open(os.devnull, "w") as sink:
            os.dup2(sink.fileno(), sys.stdout.fileno())
        return 0
    except OSError as error:
        print("screensaver bridge: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
