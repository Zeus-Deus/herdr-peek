#!/usr/bin/env python3
"""End-to-end test against a real herdr binary, fully isolated from your session.

usage: python3 tests/e2e.py <path-to-herdr> [workdir]

Starts a private headless herdr server, links this plugin, puts an agent-style
transcript in a pane, then drives peek.last / peek.pick / selection / Ctrl+click
and every demo file type through the herdr CLI and socket.
"""

import json
import os
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
    results.append((name, bool(cond), detail))
    mark = "\033[32mPASS\033[0m" if cond else "\033[31mFAIL\033[0m"
    print("  %s  %s%s" % (mark, name, ("  (" + detail + ")") if detail and not cond else ""))
    return cond


def pane_ids(sb):
    return {p["pane_id"]: p for p in sb.panes()}


def viewer_pane(sb, known):
    for pid in pane_ids(sb):
        if pid not in known:
            return pid
    return None


def wait_text(sb, pane, needle, timeout=20):
    deadline = time.time() + timeout
    text = ""
    while time.time() < deadline:
        text = sb.read(pane)
        if (needle(text) if callable(needle) else needle in text):
            return True, text
        time.sleep(0.25)
    return False, text


def main():
    herdr_bin = os.path.abspath(sys.argv[1])
    work = os.path.abspath(sys.argv[2]) if len(sys.argv) > 2 else tempfile.mkdtemp(prefix="peek-e2e-")
    demo = os.path.join(work, "demo")
    if not os.path.exists(os.path.join(demo, "shots", "login-dark.png")):
        subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "make_demo.py"), demo], check=True, stdout=subprocess.DEVNULL)

    sb = Sandbox(os.path.join(work, "sandbox"), herdr_bin)
    sb.setup()
    sb.start_server()
    version = sb.cli("--version")[1].strip()
    print("e2e: %s (isolated server, socket %s)" % (version, sb.socket))
    try:
        run(sb, demo)
    finally:
        logs = sb.cli("plugin", "log", "list", "--plugin", "peek", "--limit", "50")[1]
        with open(os.path.join(work, "plugin-log.json"), "w") as fh:
            fh.write(logs)
        sb.stop()
    failed = [r for r in results if not r[1]]
    print("\n%s: %d passed, %d failed" % (version, len(results) - len(failed), len(failed)))
    with open(os.path.join(work, "e2e-results.json"), "w") as fh:
        json.dump({"version": version, "results": results}, fh, indent=1)
    return 1 if failed else 0


def run(sb, demo):
    sb.cli("workspace", "create", "--cwd", demo, "--label", "web-app", "--focus", check=True)
    sb.wait(lambda: len(sb.panes()) >= 1, 10, "first pane")
    # a real interactive client in a pty, so client-only UI (popups, kitty output) runs
    sb.attach_client()
    time.sleep(2)
    agent = list(pane_ids(sb))[0]
    sb.cli("pane", "run", agent, "python3 %s" % os.path.join(ROOT, "scripts", "fake_agent.py"), check=True)
    ok, _ = wait_text(sb, agent, "shots/login-dark.png")
    check("agent transcript visible", ok)
    known = set(pane_ids(sb))

    # 1. prefix+shift+f -> newest file on screen opens in a split
    del sb.client_out[:]
    res = sb.invoke("peek.last")
    check("peek.last invoked", "error" not in res, json.dumps(res)[:300])
    sb.wait(lambda: viewer_pane(sb, known) is not None, 15, "viewer pane")
    viewer = viewer_pane(sb, known)
    ok, text = wait_text(sb, viewer, "login-dark.png")
    check("last: viewer shows newest file (login-dark.png)", ok, text[:200])
    check("last: image dimensions in title", "1440×900" in text, text.split("\n")[0])
    try:
        sb.wait(lambda: b"\x1b_G" in sb.client_out, 10, "kitty graphics at the client")
        got = True
    except AssertionError:
        got = False
    check("herdr forwards the image to the terminal as kitty graphics", got, "%d bytes of client output" % len(sb.client_out))
    layout = sb.json("pane", "layout")["result"]
    check("viewer opened as a split beside the agent", len(layout.get("panes", [])) >= 2 or "split" in json.dumps(layout), json.dumps(layout)[:200])

    # 2. n / p walk every file found on screen
    sb.cli("pane", "send-keys", viewer, "n")
    ok, text = wait_text(sb, viewer, "coverage.html")
    check("n: wraps to first file on screen (coverage.html)", ok, text.split("\n")[0])
    sb.cli("pane", "send-keys", viewer, "p")
    ok, text = wait_text(sb, viewer, "login-dark.png")
    check("p: back to login-dark.png", ok, text.split("\n")[0])

    # 3. prefix+f -> letter hints, press a letter
    before = set(pane_ids(sb))
    res = sb.invoke("peek.pick", {"invocation_source": "keybinding", "focused_pane_id": agent})
    check("peek.pick invoked", "error" not in res, json.dumps(res)[:300])
    sb.wait(lambda: viewer_pane(sb, before) is not None, 15, "picker overlay")
    picker = viewer_pane(sb, before)
    ok, text = wait_text(sb, picker, "type a letter")
    check("pick: overlay shows hint bar", ok, text[-300:])
    check("pick: 9 files found", "9 files" in text, text[-200:])
    # hints run top to bottom: coverage.html=a src.py=s report.md=d results.csv=f report.pdf=g flow.mp4=h
    sb.cli("pane", "send-keys", picker, "h")
    sb.wait(lambda: picker not in pane_ids(sb), 10, "picker to close")
    ok, text = wait_text(sb, viewer, "flow.mp4")
    check("pick: letter opens that file (flow.mp4) in the same viewer", ok, text.split("\n")[0])
    check("pick: video shows duration", "0:06" in text, text[:300])
    check("viewer reused (still one viewer pane)", len(pane_ids(sb)) == len(known) + 1, str(list(pane_ids(sb))))

    # 4. selection -> opens the selected path directly
    sb.invoke("peek.pick", {"invocation_source": "keybinding", "focused_pane_id": agent, "selected_text": "docs/report.pdf"})
    ok, text = wait_text(sb, viewer, "page 1 / 2")
    check("selection: opens selected PDF (page 1 / 2)", ok, text.split("\n")[0])
    sb.cli("pane", "send-keys", viewer, "right")
    ok, text = wait_text(sb, viewer, "page 2 / 2")
    check("pdf: → turns the page", ok, text.split("\n")[0])

    # 5. every kind of file, through the Ctrl+click link handler path
    cases = [
        ("shots/login-light.png", "1440×900"),
        ("shots/loading.gif", "frames"),
        ("shots/flow.mp4", "h264"),
        ("data/tone.wav", "Test tone"),
        ("docs/report.pdf", "page 1 / 2"),
        ("docs/summary.docx", "page 1 /"),
        ("docs/report.md", "LOGIN FLOW REPORT"),
        ("docs/coverage.html", "screenshot"),
        ("src.py", "async def shoot"),
        ("data/results.csv", "40 rows"),
        ("data/config.json", "object · 8 keys"),
        ("data/events.jsonl", "5 records"),
        ("docs/analysis.ipynb", "In [1]:"),
        ("data/app.db", "users"),
        ("data/shots.zip", "login-dark.png"),
        ("data/docs.tar.gz", "docs/report.md"),
        ("data/blob.bin", "00000000"),
        ("shots", "login-dark.png"),
    ]
    for rel, needle in cases:
        full = os.path.join(demo, rel)
        if not os.path.exists(full):
            check("open %s" % rel, False, "fixture missing")
            continue
        sb.invoke("peek.open", {"invocation_source": "link_click", "clicked_url": "file://" + full, "link_handler_id": "file-link", "focused_pane_id": agent})
        name = os.path.basename(rel)
        ok, text = wait_text(
            sb,
            viewer,
            lambda t, n=needle: t.split("\n", 1)[0].lstrip().startswith(name) and n in t and "peek hit an error" not in t,
            timeout=90 if rel.endswith(".docx") else 25,
        )
        check("ctrl+click %-22s → %s" % (rel, needle), ok, (text[:400] if text else "").replace("\n", " | "))

    # 6. q closes the viewer pane
    sb.cli("pane", "send-keys", viewer, "q")
    try:
        sb.wait(lambda: viewer not in pane_ids(sb), 10, "viewer to close")
        closed = True
    except AssertionError:
        closed = False
    check("q closes the viewer", closed)

    # 7. placement = "popup" opens the same viewer as a modal popup
    cfg_dir = sb.cli("plugin", "config-dir", "peek")[1].strip()
    with open(os.path.join(cfg_dir, "config.toml"), "w") as fh:
        fh.write('placement = "popup"\n')
    res = sb.invoke("peek.open", {"invocation_source": "link_click", "clicked_url": "file://" + os.path.join(demo, "shots", "login-light.png"), "focused_pane_id": agent})
    check("popup: peek.open invoked", "error" not in res, json.dumps(res)[:300])

    def popup_viewer_pid():
        for dirpath, _dirs, files in os.walk(sb.home):  # glob skips dot-dirs
            for name in files:
                if name.startswith("viewer-") and name.endswith(".json"):
                    with open(os.path.join(dirpath, name)) as fh:
                        pid = json.load(fh).get("pid")
                    if pid:
                        os.kill(pid, 0)
                        return pid
        return None

    try:
        sb.wait(lambda: popup_viewer_pid() is not None, 10, "popup viewer process")
        popup_ok = True
    except AssertionError:
        popup_ok = False
    check("popup: viewer runs in a herdr popup", popup_ok)
    close = sb.raw("popup.close", {})
    check("popup: closes via popup.close", "error" not in close, json.dumps(close)[:200])
    os.unlink(os.path.join(cfg_dir, "config.toml"))

    logs = sb.cli("plugin", "log", "list", "--plugin", "peek", "--limit", "50")[1]
    try:
        entries = json.loads(logs)["result"].get("logs", [])
    except (ValueError, KeyError, AttributeError):
        entries = []
    bad = [e for e in entries if e.get("exit_code") not in (0, None) and e.get("status") not in ("running",)]
    check("no failing plugin commands in herdr's plugin log", not bad, json.dumps(bad)[:500])


if __name__ == "__main__":
    sys.exit(main())
