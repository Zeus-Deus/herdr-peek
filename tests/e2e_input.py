#!/usr/bin/env python3
"""End-to-end test through real terminal input: keys, mouse selection, Ctrl+click.

usage: uv run --with pyte python3 tests/e2e_input.py <path-to-herdr> [workdir]

Attaches an interactive herdr client to a private pty and types into it exactly
like a terminal would: the prefix key, peek's key bindings from config.toml,
hint letters, viewer keys, an SGR mouse drag to select a path, and a
Ctrl+click on a file:// link. The client's screen is emulated with pyte so
clicks land on the right cells. Isolated from your own herdr session.
"""

import os
import re
import subprocess
import sys
import tempfile
import threading
import time

import pyte

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

from sandbox import Sandbox  # noqa: E402

COLS, ROWS = 200, 50
PREFIX = "\x02"  # ctrl+b, herdr's default prefix
results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    mark = "\033[32mPASS\033[0m" if cond else "\033[31mFAIL\033[0m"
    print("  %s  %s%s" % (mark, name, ("  (" + detail + ")") if detail and not cond else ""))
    return cond


class Screen(pyte.Screen):
    """pyte screen that answers the client's terminal queries like a real terminal."""

    reply = None

    def report_device_status(self, mode, **kwargs):
        pyte.Screen.report_device_status(self, mode)

    def write_process_input(self, data):
        if self.reply:
            self.reply(data)


class Term(object):
    """pyte screen fed from the client, with kitty APC graphics stripped."""

    def __init__(self):
        self.screen = Screen(COLS, ROWS)
        self.stream = pyte.ByteStream(self.screen)
        self.lock = threading.Lock()
        self.in_apc = False
        self.pending = b""

    def feed(self, data):
        data = self.pending + data
        self.pending = b""
        out = bytearray()
        i = 0
        while i < len(data):
            if self.in_apc:
                end = data.find(b"\x1b\\", i)
                if end < 0:
                    if data.endswith(b"\x1b"):
                        self.pending = b"\x1b"
                    return self._push(out)
                i = end + 2
                self.in_apc = False
                continue
            start = data.find(b"\x1b_", i)
            if start < 0:
                if data.endswith(b"\x1b"):
                    out += data[i:-1]
                    self.pending = b"\x1b"
                else:
                    out += data[i:]
                break
            out += data[i:start]
            self.in_apc = True
            i = start + 2
        self._push(out)

    def _push(self, out):
        if out:
            with self.lock:
                try:
                    self.stream.feed(bytes(out))
                except Exception as exc:  # never let the parser stall the pty drain
                    self.errors = getattr(self, "errors", 0) + 1
                    self.last_error = exc

    def lines(self):
        with self.lock:
            return list(self.screen.display)

    def find(self, text):
        for y, line in enumerate(self.lines()):
            x = line.find(text)
            if x >= 0:
                return x, y
        return None


def wait(pred, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            v = pred()
            if v:
                return v
        except Exception:
            pass
        time.sleep(0.15)
    return None


def sgr(button, x, y, release=False):
    """SGR mouse report, 1-based cell coordinates."""
    return "\x1b[<%d;%d;%d%s" % (button, x + 1, y + 1, "m" if release else "M")


def main():
    herdr_bin = os.path.abspath(sys.argv[1])
    work = os.path.abspath(sys.argv[2]) if len(sys.argv) > 2 else tempfile.mkdtemp(prefix="peek-input-")
    demo = os.path.join(work, "demo")
    if not os.path.exists(os.path.join(demo, "shots", "login-dark.png")):
        subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "make_demo.py"), demo], check=True, stdout=subprocess.DEVNULL)
    sb = Sandbox(os.path.join(work, "sandbox"), herdr_bin)
    sb.setup()
    version = sb.cli("--version")[1].strip()
    print("e2e input: %s" % version)
    check("config.toml keybindings load without conflicts", sb.cli("config", "check")[1].strip() == "config: ok", sb.cli("config", "check")[1])
    sb.start_server()
    term = Term()
    try:
        sb.cli("workspace", "create", "--cwd", demo, "--label", "web-app", "--focus", check=True)
        sb.wait(lambda: len(sb.panes()) >= 1, 10, "pane")
        agent = sb.panes()[0]["pane_id"]
        term.screen.reply = lambda data: sb.type(data)
        sb.attach_client(COLS, ROWS, on_output=term.feed)
        sb.cli("pane", "run", agent, "python3 %s" % os.path.join(ROOT, "scripts", "fake_agent.py"), check=True)
        check("client renders the agent transcript", wait(lambda: term.find("shots/login-dark.png")), "\n".join(term.lines()[:12]))
        run(sb, term, agent, demo)
    finally:
        sb.stop()
    failed = [r for r in results if not r[1]]
    print("\n%s (input): %d passed, %d failed" % (version, len(results) - len(failed), len(failed)))
    return 1 if failed else 0


def viewer_title(term):
    for line in term.lines():
        m = re.search(r"([\w.-]+\.(?:png|mp4|gif|pdf|html|md|csv)) · ", line)
        if m:
            return m.group(1)
    return None


def pane_ids(sb):
    return [p["pane_id"] for p in sb.panes()]


def run(sb, term, agent, demo):
    base = set(pane_ids(sb))

    # prefix+shift+f: newest file on screen
    sb.type(PREFIX)
    time.sleep(0.2)
    sb.type("F")
    check("prefix+shift+f opens the newest file (login-dark.png)", wait(lambda: viewer_title(term) == "login-dark.png"), str(viewer_title(term)))
    check("the viewer is a new split", wait(lambda: len(pane_ids(sb)) == len(base) + 1))

    # keys typed now go to the focused viewer
    sb.type("n")
    check("typing n in the viewer moves to the next file", wait(lambda: viewer_title(term) == "coverage.html"), str(viewer_title(term)))
    sb.type("p")
    check("typing p goes back", wait(lambda: viewer_title(term) == "login-dark.png"), str(viewer_title(term)))
    sb.type("q")
    check("typing q closes the viewer", wait(lambda: len(pane_ids(sb)) == len(base)), str(pane_ids(sb)))

    # prefix+f: hint picker, then a letter
    sb.type(PREFIX)
    time.sleep(0.2)
    sb.type("f")
    check("prefix+f shows the hint picker", wait(lambda: term.find("type a letter")), "\n".join(term.lines()[-3:]))
    hint_row = wait(lambda: term.find("shots/flow.mp4"))
    letter = None
    if hint_row:
        x, y = hint_row
        # the badge sits in the gap just before the path
        letter = (re.findall(r"[a-z]{1,2}", term.lines()[y][:x]) or [None])[-1]
    check("the picker labels shots/flow.mp4 with a letter", letter, term.lines()[hint_row[1]] if hint_row else "")
    if letter:
        sb.type(letter)
        check("typing its letter (%s) opens flow.mp4" % letter, wait(lambda: viewer_title(term) == "flow.mp4"), str(viewer_title(term)))
    sb.type("q")
    wait(lambda: len(pane_ids(sb)) == len(base))

    # mouse: in copy mode (prefix+[), drag-select a path, then prefix+f.
    # In normal mode herdr clears a selection on any key, the prefix included,
    # and with copy_on_select = true it also clears it on mouse-up.
    pos = wait(lambda: term.find("docs/report.pdf"))
    if check("found docs/report.pdf on the client screen", pos):
        x, y = pos
        end = x + len("docs/report.pdf") - 1
        sb.type(PREFIX)
        time.sleep(0.2)
        sb.type("[")
        time.sleep(0.5)
        sb.type(sgr(0, x, y))
        for cx in range(x + 1, end + 1, 3):
            sb.type(sgr(32, cx, y))
            time.sleep(0.02)
        sb.type(sgr(32, end, y))
        sb.type(sgr(0, end, y, release=True))
        time.sleep(0.4)
        sb.type(PREFIX)
        time.sleep(0.2)
        sb.type("f")
        ok = wait(lambda: viewer_title(term) == "report.pdf")
        check("mouse selection + prefix+f opens the selected file (report.pdf)", ok, str(viewer_title(term)) + " | picker=" + str(bool(term.find("type a letter"))))
        if not ok and term.find("type a letter"):
            sb.type("\x1b")
        sb.type("q")
        wait(lambda: len(pane_ids(sb)) == len(base))

    # Ctrl+click on an OSC 8 file:// hyperlink (normal mode, so leave copy mode first)
    time.sleep(0.3)
    sb.type("q")
    time.sleep(0.5)
    pos = wait(lambda: term.find("docs/coverage.html"))
    if check("found the hyperlinked docs/coverage.html on the client screen", pos):
        x, y = pos
        x += 5  # somewhere inside the link text
        sb.type(sgr(16, x, y))
        time.sleep(0.05)
        sb.type(sgr(16, x, y, release=True))
        check("Ctrl+click on file://…/coverage.html opens it in peek", wait(lambda: viewer_title(term) == "coverage.html", timeout=40), str(viewer_title(term)))


if __name__ == "__main__":
    sys.exit(main())
