"""Letter-hint picker drawn over the agent pane (like vimium / tmux-fingers)."""

import json
import os
import subprocess
import sys
import time

import state
from paths import char_width
from term import ACCENT, BOLD, MUTED, RESET, Raw, bg, fg, get_size, move, osc52_copy, pad, read_keys, write

HINT = bg(240, 160, 112) + fg(17, 17, 17) + BOLD
MATCH = fg(240, 200, 170) + BOLD
TYPED = bg(120, 80, 56) + fg(17, 17, 17) + BOLD


def cut(text, start_col, width):
    """Slice plain text by display columns."""
    out = []
    col = 0
    for ch in text:
        w = char_width(ch)
        if col >= start_col and col + w <= start_col + width:
            out.append(ch)
        col += w
        if col >= start_col + width:
            break
    return "".join(out)


def render(pick, typed, size):
    rows = pick["rows"]
    hits = pick["hits"]
    avail = size.rows - 1
    offset = max(0, len(rows) - avail)
    by_row = {}
    for h in hits:
        by_row.setdefault(h["row"], []).append(h)
    out = [move(1, 1)]
    for screen_row in range(avail):
        r = offset + screen_row
        text = rows[r] if r < len(rows) else ""
        line = []
        col = 0
        for h in sorted(by_row.get(r, []), key=lambda x: x["col"]):
            if h["col"] < col:
                continue
            hint = h["hint"]
            live = hint.startswith(typed)
            body = cut(text, h["col"], h["width"])
            badge = (TYPED + typed + RESET if typed else "") + HINT + hint[len(typed):] + RESET
            # put the badge in the gap before the path when there is room,
            # otherwise over its first characters
            spacer = ""
            start = h["col"] - len(hint)
            if start - 1 >= col and cut(text, start - 1, len(hint) + 1).strip() == "":
                start -= 1
                spacer = " "
            gap_free = start >= col and cut(text, start, h["col"] - start).strip() == ""
            if live and gap_free:
                line.append(MUTED + cut(text, col, start - col) + RESET + badge + spacer + MATCH + body + RESET)
            elif live:
                line.append(MUTED + cut(text, col, h["col"] - col) + RESET)
                line.append(badge + MATCH + body[len(hint):] + RESET)
            else:
                line.append(MUTED + cut(text, col, h["col"] - col) + body + RESET)
            col = h["col"] + h["width"]
        line.append(MUTED + cut(text, col, 10000) + RESET)
        out.append(move(screen_row + 1, 1) + pad("".join(line), size.cols))
    count = len({h["path"] for h in hits})
    status = " %speek%s %s· %d file%s · type a letter · Shift+letter copies the path · Esc cancels%s" % (
        BOLD + ACCENT, RESET, MUTED, count, "" if count == 1 else "s", RESET
    )
    out.append(move(size.rows, 1) + pad(status, size.cols))
    return "".join(out)


def launch_open(pick, chosen_path):
    """Open the viewer after this overlay has closed (it restores focus on exit)."""
    items = pick["items"]
    index = next((i for i, it in enumerate(items) if it["path"] == chosen_path), 0)
    payload = {
        "items": items,
        "index": index,
        "tab": pick.get("tab"),
        "source_pane": pick.get("source_pane"),
        "delay": 0.25,
    }
    req = os.path.join(state.state_dir(), "open-%d.json" % os.getpid())
    state.write_json(req, payload)
    main = os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.py")
    subprocess.Popen(
        [sys.executable, main, "open-request", req],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )


def main():
    with open(os.environ["PEEK_PICK"]) as fh:
        pick = json.load(fh)
    hints = {h["hint"]: h["path"] for h in pick["hits"]}
    typed = ""
    with Raw():
        while True:
            write(render(pick, typed, get_size()))
            keys = read_keys(30.0)
            if not keys:
                continue
            for k in keys:
                if k in ("esc", "eof"):
                    return 0
                if k == "backspace":
                    typed = typed[:-1]
                    continue
                if len(k) != 1 or not k.isalpha():
                    continue
                copy = k.isupper()
                attempt = typed + k.lower()
                if attempt in hints:
                    if copy:
                        write(osc52_copy(hints[attempt]))
                        time.sleep(0.05)
                    else:
                        launch_open(pick, hints[attempt])
                    return 0
                if any(h.startswith(attempt) for h in hints):
                    typed = attempt
                else:
                    typed = ""
    return 0
