#!/usr/bin/env python3
"""Capture real screenshots of peek inside herdr, in Ghostty, on Hyprland.

usage: python3 scripts/screenshots.py <herdr-bin> <out-dir> [label]

Runs an isolated herdr (own HOME/socket) as a real client in a Ghostty window
on a temporary headless Hyprland output, drives peek through the herdr CLI and
captures just that window with `grim -T`. The output is removed afterwards.
"""

import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))

from sandbox import Sandbox  # noqa: E402

OUTPUT = "PEEKSHOT"
CLASS = "dev.peek.shot"


def hyprctl(*args):
    return subprocess.run(["hyprctl"] + list(args), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True).stdout.strip()


def monitors():
    return json.loads(hyprctl("-j", "monitors"))


def clients():
    return json.loads(hyprctl("-j", "clients"))


def find_window():
    for c in clients():
        if c.get("initialClass") == CLASS:
            return c
    return None


def output_up():
    if not any(m["name"] == OUTPUT for m in monitors()):
        hyprctl("output", "create", "headless", OUTPUT)
    for _ in range(50):
        mon = next((m for m in monitors() if m["name"] == OUTPUT), None)
        if mon:
            return mon
        time.sleep(0.1)
    raise SystemExit("headless output did not appear")


def output_down():
    if any(m["name"] == OUTPUT for m in monitors()):
        hyprctl("output", "remove", OUTPUT)


def spawn_ghostty(script, workspace_id):
    rule = 'hl.window_rule({ match = { initial_class = "^dev\\\\.peek\\\\.shot$" }, workspace = "%s silent" })' % workspace_id
    hyprctl("eval", rule)
    cmd = "ghostty --gtk-single-instance=false --class=%s --window-decoration=false -e bash %s" % (CLASS, script)
    hyprctl("eval", "hl.exec_cmd(%s)" % json.dumps(cmd))
    for _ in range(150):
        win = find_window()
        if win:
            return win
        time.sleep(0.1)
    raise SystemExit("ghostty window did not appear")


def close_ghostty():
    win = find_window()
    if win:
        hyprctl("dispatch", 'hl.dsp.window.close({ window = "address:%s" })' % win["address"])


def grab(path):
    win = find_window()
    time.sleep(0.6)  # let herdr flush the frame
    subprocess.run(["grim", "-T", win["stableId"], path], check=True, timeout=20)
    print("  captured", os.path.basename(path))


def wait_text(sb, pane, needle, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        text = sb.read(pane)
        if needle in text:
            return text
        time.sleep(0.2)
    raise AssertionError("%r never appeared in %s" % (needle, pane))


def main():
    herdr_bin = os.path.abspath(sys.argv[1])
    out = os.path.abspath(sys.argv[2])
    label = sys.argv[3] if len(sys.argv) > 3 else "shot"
    os.makedirs(out, exist_ok=True)
    work = os.path.join(out, "work-" + label)
    demo = os.path.join(out, "demo")
    if not os.path.exists(os.path.join(demo, "shots", "login-dark.png")):
        subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "make_demo.py"), demo], check=True, stdout=subprocess.DEVNULL)

    sb = Sandbox(work, herdr_bin)
    sb.setup()
    sb.start_server()
    sb.cli("workspace", "create", "--cwd", demo, "--label", "web-app", "--focus", check=True)
    sb.wait(lambda: len(sb.panes()) >= 1, 10, "pane")
    agent = sb.panes()[0]["pane_id"]
    # record whether the old socket image API (removed in herdr c411883e) still exists
    api = {"version": sb.cli("--version")[1].strip(), "pane.graphics.info": sb.raw("pane.graphics.info", {"pane_id": agent})}
    with open(os.path.join(out, "%s-graphics-api.json" % label), "w") as fh:
        json.dump(api, fh, indent=1)

    client = os.path.join(work, "client.sh")
    with open(client, "w") as fh:
        for k, v in sb.env.items():
            if k.startswith(("HOME", "XDG_", "HERDR_SOCKET", "SHELL", "PATH", "LANG", "WAYLAND", "DISPLAY", "DBUS")):
                fh.write("export %s=%s\n" % (k, json.dumps(v)))
        fh.write("unset HERDR_ENV HERDR_PANE_ID HERDR_TAB_ID HERDR_WORKSPACE_ID\n")
        fh.write("exec %s\n" % herdr_bin)

    mon = output_up()
    try:
        spawn_ghostty(client, mon["activeWorkspace"]["id"])
        time.sleep(3)
        sb.cli("pane", "run", agent, "python3 %s" % os.path.join(ROOT, "scripts", "fake_agent.py"), check=True)
        wait_text(sb, agent, "login-dark.png")
        grab(os.path.join(out, "%s-1-agent.png" % label))

        known = {p["pane_id"] for p in sb.panes()}
        sb.invoke("peek.pick", {"invocation_source": "keybinding", "focused_pane_id": agent})
        sb.wait(lambda: len(sb.panes()) > len(known), 10, "picker")
        picker = next(p["pane_id"] for p in sb.panes() if p["pane_id"] not in known)
        wait_text(sb, picker, "type a letter")
        grab(os.path.join(out, "%s-2-hints.png" % label))

        # hints top to bottom: coverage.html a, src.py s, report.md d, results.csv f,
        # report.pdf g, flow.mp4 h, loading.gif j, login-light k, login-dark l
        sb.cli("pane", "send-keys", picker, "l")
        sb.wait(lambda: picker not in {p["pane_id"] for p in sb.panes()}, 10, "picker close")
        sb.wait(lambda: len(sb.panes()) > len(known), 15, "viewer")
        viewer = next(p["pane_id"] for p in sb.panes() if p["pane_id"] not in known)
        wait_text(sb, viewer, "login-dark.png")
        grab(os.path.join(out, "%s-3-image.png" % label))

        shots = [
            ("docs/report.pdf", "page 1", "4-pdf"),
            ("shots/flow.mp4", "h264", "5-video"),
            ("docs/report.md", "LOGIN FLOW", "6-markdown"),
            ("data/results.csv", "40 rows", "7-csv"),
            ("docs/coverage.html", "screenshot", "8-html"),
            ("data/tone.wav", "Test tone", "9-audio"),
            ("shots/loading.gif", "frames", "10-gif"),
        ]
        for rel, needle, name in shots:
            sb.invoke("peek.open", {"invocation_source": "link_click", "clicked_url": "file://" + os.path.join(demo, rel), "focused_pane_id": agent})
            wait_text(sb, viewer, needle, timeout=90)
            grab(os.path.join(out, "%s-%s.png" % (label, name)))
            if name == "10-gif":
                time.sleep(0.35)
                grab(os.path.join(out, "%s-%s-b.png" % (label, name)))

        # placement = "popup": the same viewer as a modal popup
        sb.cli("pane", "send-keys", viewer, "q")
        cfg_dir = sb.cli("plugin", "config-dir", "peek")[1].strip()
        with open(os.path.join(cfg_dir, "config.toml"), "w") as fh:
            fh.write('placement = "popup"\n')
        time.sleep(0.5)
        sb.invoke("peek.open", {"invocation_source": "link_click", "clicked_url": "file://" + os.path.join(demo, "shots", "login-light.png"), "focused_pane_id": agent})
        time.sleep(2.5)
        grab(os.path.join(out, "%s-11-popup.png" % label))
        sb.raw("popup.close", {})
    finally:
        close_ghostty()
        time.sleep(0.5)
        output_down()
        sb.stop()


if __name__ == "__main__":
    main()
