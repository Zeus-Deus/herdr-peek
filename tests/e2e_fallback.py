#!/usr/bin/env python3
"""End-to-end test of the no-images fallback (foot, Alacritty, …).

usage: python3 tests/e2e_fallback.py <path-to-herdr> [workdir]

The herdr client is started by a tiny launcher named `foot` or `ghostty`, so
peek's terminal detection sees a real process tree, and `xdg-open` is a stub
that records what it was asked to open.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

from sandbox import Sandbox  # noqa: E402

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    mark = "\033[32mPASS\033[0m" if cond else "\033[31mFAIL\033[0m"
    print("  %s  %s%s" % (mark, name, ("  (" + str(detail) + ")") if detail and not cond else ""))


def wait(pred, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        v = pred()
        if v:
            return v
        time.sleep(0.15)
    return None


def scenario(work, herdr_bin, demo, terminal, images=None):
    tools = os.path.join(work, "tools-" + terminal)
    os.makedirs(tools, exist_ok=True)
    opened = os.path.join(tools, "opened.log")
    if os.path.exists(opened):
        os.unlink(opened)
    with open(os.path.join(tools, "xdg-open"), "w") as fh:
        fh.write('#!/bin/sh\necho "$1" >> "%s"\n' % opened)
    with open(os.path.join(tools, "open"), "w") as fh:
        fh.write('#!/bin/sh\necho "$1" >> "%s"\n' % opened)
    launcher = os.path.join(tools, terminal)
    with open(launcher, "w") as fh:
        fh.write('#!/bin/sh\n"$@"\n')  # no exec: herdr's parent stays named after the terminal
    for f in ("xdg-open", "open", terminal):
        os.chmod(os.path.join(tools, f), 0o755)

    sb = Sandbox(os.path.join(work, "sb-" + terminal + ("-" + images if images else "")), herdr_bin, extra_path=tools)
    sb.setup()
    if images:
        cfg_dir = sb.cli("plugin", "config-dir", "peek")[1].strip()
        os.makedirs(cfg_dir, exist_ok=True)
        with open(os.path.join(cfg_dir, "config.toml"), "w") as fh:
            fh.write('images = "%s"\n' % images)
    env = sb.env
    env.setdefault("WAYLAND_DISPLAY", "wayland-test")
    sb.start_server()
    try:
        sb.cli("workspace", "create", "--cwd", demo, "--focus", check=True)
        sb.wait(lambda: len(sb.panes()) >= 1, 10, "pane")
        agent = sb.panes()[0]["pane_id"]
        sb.attach_client(via=launcher)
        sb.cli("pane", "run", agent, "python3 %s" % os.path.join(ROOT, "scripts", "fake_agent.py"), check=True)
        wait(lambda: "login-dark.png" in sb.read(agent))
        return sb, agent, opened
    except Exception:
        sb.stop()
        raise


def read_opened(path):
    try:
        with open(path) as fh:
            return fh.read()
    except OSError:
        return ""


def main():
    herdr_bin = os.path.abspath(sys.argv[1])
    work = os.path.abspath(sys.argv[2]) if len(sys.argv) > 2 else tempfile.mkdtemp(prefix="peek-fallback-")
    if os.path.exists(work):
        shutil.rmtree(work)
    os.makedirs(work)
    demo = os.path.join(work, "demo")
    subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "make_demo.py"), demo], check=True, stdout=subprocess.DEVNULL)
    print("e2e fallback: %s" % subprocess.run([herdr_bin, "--version"], stdout=subprocess.PIPE).stdout.decode().strip())

    # foot: images pop out, text still opens in the split
    sb, agent, opened = scenario(work, herdr_bin, demo, "foot")
    try:
        sb.invoke("peek.last")
        check("foot: newest image opens in the desktop viewer", wait(lambda: "login-dark.png" in read_opened(opened)), read_opened(opened))
        time.sleep(1)
        check("foot: no empty peek split is created", len(sb.panes()) == 1, [p["pane_id"] for p in sb.panes()])
        sb.invoke("peek.open", {"invocation_source": "link_click", "clicked_url": "file://" + demo + "/docs/report.md", "focused_pane_id": agent})
        viewer = wait(lambda: next((p["pane_id"] for p in sb.panes() if p["pane_id"] != agent), None))
        check("foot: Markdown still opens in the peek split", viewer and wait(lambda: "LOGIN FLOW REPORT" in sb.read(viewer)))
        sb.invoke("peek.open", {"invocation_source": "link_click", "clicked_url": "file://" + demo + "/docs/report.pdf", "focused_pane_id": agent})
        check("foot: a PDF opened while the split is up pops out too", wait(lambda: "report.pdf" in read_opened(opened)), read_opened(opened))
        check("foot: the split explains what happened", viewer and wait(lambda: "Opened report.pdf in your default viewer" in sb.read(viewer)), sb.read(viewer)[:300] if viewer else "")
        check("foot: and names the terminal", viewer and "foot can't draw images inside herdr" in sb.read(viewer))
        sb.cli("pane", "send-keys", viewer, "o")
        check("foot: o opens it again", wait(lambda: read_opened(opened).count("report.pdf") >= 2), read_opened(opened))
    finally:
        sb.stop()

    # ghostty: images stay inline
    sb, agent, opened = scenario(work, herdr_bin, demo, "ghostty")
    try:
        sb.invoke("peek.last")
        viewer = wait(lambda: next((p["pane_id"] for p in sb.panes() if p["pane_id"] != agent), None))
        check("ghostty: image shows in the peek split", viewer and wait(lambda: "1440×900" in sb.read(viewer)))
        check("ghostty: nothing popped out", read_opened(opened) == "", read_opened(opened))
    finally:
        sb.stop()

    # images = "external" forces the pop-out even in ghostty
    sb, agent, opened = scenario(work, herdr_bin, demo, "ghostty", images="external")
    try:
        sb.invoke("peek.last")
        check('images = "external": pops out even in ghostty', wait(lambda: "login-dark.png" in read_opened(opened)), read_opened(opened))
    finally:
        sb.stop()

    # images = "inline" keeps it inline even in foot
    sb, agent, opened = scenario(work, herdr_bin, demo, "foot", images="inline")
    try:
        sb.invoke("peek.last")
        viewer = wait(lambda: next((p["pane_id"] for p in sb.panes() if p["pane_id"] != agent), None))
        check('images = "inline": stays in the split even in foot', viewer and wait(lambda: "1440×900" in sb.read(viewer)) and read_opened(opened) == "")
    finally:
        sb.stop()

    failed = [r for r in results if not r[1]]
    print("\nfallback: %d passed, %d failed" % (len(results) - len(failed), len(failed)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
