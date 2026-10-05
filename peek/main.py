#!/usr/bin/env python3
"""herdr-peek entrypoint: `python3 peek/main.py <pick|last|open|view|picker|doctor>`."""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import herdr  # noqa: E402
import paths  # noqa: E402
import state  # noqa: E402


def log(msg):
    sys.stderr.write("peek: %s\n" % msg)


class Ctx(object):
    """Who invoked us and from where."""

    def __init__(self):
        raw = herdr.context()
        self.raw = raw
        self.pane = raw.get("focused_pane_id") or os.environ.get("HERDR_PANE_ID")
        self.tab = raw.get("tab_id") or os.environ.get("HERDR_TAB_ID") or "default"
        info = herdr.pane_get(self.pane) or {}
        self.cwd = info.get("foreground_cwd") or info.get("cwd") or raw.get("focused_pane_cwd") or raw.get("workspace_cwd") or os.getcwd()
        bases = [info.get("cwd"), raw.get("focused_pane_cwd"), raw.get("workspace_cwd")]
        wt = raw.get("worktree")
        if isinstance(wt, dict):
            bases += [wt.get("path"), wt.get("root"), wt.get("repo_root")]
        bases.append(os.path.expanduser("~"))
        self.bases = [b for b in bases if isinstance(b, str) and b and b != self.cwd]
        self.selected = raw.get("selected_text")
        self.clicked = raw.get("clicked_url") or os.environ.get("HERDR_PLUGIN_CLICKED_URL")


def screen_hits(ctx):
    text = herdr.pane_read(ctx.pane, "visible") if ctx.pane else ""
    rows = text.split("\n")
    width = max((paths.display_width(r) for r in rows), default=0)
    return text, paths.find_paths(text, cwd=ctx.cwd, extra_bases=ctx.bases, width=width)


def items_from(hits):
    out = []
    seen = set()
    for h in hits:
        if h.path in seen:
            continue
        seen.add(h.path)
        out.append({"path": h.path, "line": h.line})
    return out


def ensure_viewer(tab, source_pane):
    cfg = state.load_config()
    data = state.read_json(state.viewer_file(tab)) or {}
    pane_id = data.get("pane_id")
    if pane_id and data.get("pid"):
        info = herdr.pane_get(pane_id)
        alive = info is not None and (not data.get("terminal_id") or info.get("terminal_id") == data.get("terminal_id"))
        if alive:
            try:
                os.kill(int(data["pid"]), 0)
            except (OSError, ValueError):
                alive = False
        if alive:
            herdr.focus_plugin_pane(pane_id)
            return pane_id
    placement = cfg.get("placement") or "split"
    if placement == "popup":
        # popups are a manifest-only placement; they cover the active pane
        ok, payload = herdr.open_plugin_pane("popup-viewer", env={"PEEK_TAB": tab}, focus=True)
    else:
        ok, payload = herdr.open_plugin_pane(
            "viewer",
            target_pane=source_pane,
            placement=placement,
            direction=cfg.get("direction") if placement == "split" else None,
            env={"PEEK_TAB": tab},
            focus=True,
        )
    if not ok:
        herdr.notify("peek: could not open the viewer", str(payload)[:200])
        log("open viewer failed: %s" % payload)
        return None
    return herdr.find_pane_id(payload) or "popup"


def open_items(ctx_tab, source_pane, items, index):
    if not items:
        return 1
    state.set_current(ctx_tab, items, index, source_pane)
    return 0 if ensure_viewer(ctx_tab, source_pane) else 1


def cmd_pick():
    ctx = Ctx()
    if ctx.selected and ctx.selected.strip():
        hits = paths.path_from_selection(ctx.selected, cwd=ctx.cwd, extra_bases=ctx.bases)
        if hits:
            return open_items(ctx.tab, ctx.pane, items_from(hits), 0)
    text, hits = screen_hits(ctx)
    if not hits:
        herdr.notify("peek: no file paths on screen")
        return 0
    items = items_from(hits)
    if len(items) == 1:
        return open_items(ctx.tab, ctx.pane, items, 0)
    cfg = state.load_config()
    hints = paths.assign_hints(hits, cfg.get("alphabet") or "asdfghjklqwertyuiopzxcvbnm")
    pick = {
        "rows": text.split("\n"),
        "hits": [dict(h.as_dict(), hint=hints[h.path]) for h in hits],
        "items": items,
        "tab": ctx.tab,
        "source_pane": ctx.pane,
    }
    pick_file = os.path.join(state.state_dir(), "pick-%s.json" % ctx.tab.replace(":", "_"))
    state.write_json(pick_file, pick)
    # overlays always cover the active pane (herdr rejects a target here)
    ok, payload = herdr.open_plugin_pane("picker", placement="overlay", env={"PEEK_PICK": pick_file}, focus=True)
    if not ok:
        log("picker failed: %s" % payload)
        return 1
    return 0


def cmd_last():
    ctx = Ctx()
    _, hits = screen_hits(ctx)
    best = paths.newest(hits)
    if not best:
        herdr.notify("peek: no file paths on screen")
        return 0
    items = items_from(hits)
    index = next(i for i, it in enumerate(items) if it["path"] == best.path)
    return open_items(ctx.tab, ctx.pane, items, index)


def cmd_open(argv):
    ctx = Ctx()
    target = argv[0] if argv else (ctx.clicked or ctx.selected or "")
    hits = paths.path_from_selection(target, cwd=ctx.cwd, extra_bases=ctx.bases)
    if not hits:
        herdr.notify("peek: not a file", target[:120])
        return 1
    return open_items(ctx.tab, ctx.pane, items_from(hits), 0)


def cmd_open_request(argv):
    req = state.read_json(argv[0]) or {}
    try:
        os.unlink(argv[0])
    except OSError:
        pass
    time.sleep(float(req.get("delay") or 0))
    return open_items(req.get("tab") or "default", req.get("source_pane"), req.get("items") or [], int(req.get("index") or 0))


def cmd_doctor():
    import convert

    rows = convert.doctor()
    width = max(len(r[0]) for r in rows)
    print("herdr-peek tool check (%s)" % os.uname().nodename)
    for name, use, pkg, found in rows:
        mark = "\033[32m✓\033[0m" if found else "\033[33m·\033[0m"
        print("  %s %s  %-22s %s" % (mark, name.ljust(width), use, found or "missing (install %s)" % pkg))
    missing = [r for r in rows if not r[3] and r[0] != "python3"]
    print("\nOnly python3 is required; missing tools just mean simpler views." if missing else "\nEverything is installed.")
    return 0


def main(argv):
    if not argv:
        print(__doc__)
        return 2
    cmd, rest = argv[0], argv[1:]
    if cmd == "pick":
        return cmd_pick()
    if cmd == "last":
        return cmd_last()
    if cmd == "open":
        return cmd_open(rest)
    if cmd == "open-request":
        return cmd_open_request(rest)
    if cmd == "view":
        import viewer

        return viewer.main()
    if cmd == "picker":
        import picker

        return picker.main()
    if cmd == "doctor":
        return cmd_doctor()
    log("unknown command %r" % cmd)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]) or 0)
