"""Terminal plumbing: raw input, sizes, ANSI helpers and kitty graphics."""

import base64
import fcntl
import os
import re
import select
import struct
import sys
import termios
import tty

from paths import char_width

ESC = "\x1b"
ANSI_RE = re.compile(r"\x1b\[[0-9;:?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")

RESET = ESC + "[0m"
BOLD = ESC + "[1m"
DIM = ESC + "[2m"
ITALIC = ESC + "[3m"
UNDERLINE = ESC + "[4m"
REVERSE = ESC + "[7m"


def fg(r, g, b):
    return "%s[38;2;%d;%d;%dm" % (ESC, r, g, b)


def bg(r, g, b):
    return "%s[48;2;%d;%d;%dm" % (ESC, r, g, b)


ACCENT = fg(240, 160, 112)
MUTED = fg(138, 138, 150)
GREEN = fg(126, 200, 140)
BLUE = fg(130, 170, 240)
YELLOW = fg(224, 168, 74)
MAGENTA = fg(200, 140, 220)
RED = fg(239, 115, 102)
CYAN = fg(110, 200, 210)


class Size(object):
    def __init__(self, cols, rows, xpix, ypix):
        self.cols = max(cols, 10)
        self.rows = max(rows, 4)
        self.xpix = xpix
        self.ypix = ypix

    @property
    def cell(self):
        if self.xpix > 0 and self.ypix > 0:
            return self.xpix / float(self.cols), self.ypix / float(self.rows)
        return None


def get_size(fd=None):
    fd = sys.stdout.fileno() if fd is None else fd
    try:
        rows, cols, xpix, ypix = struct.unpack("HHHH", fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8))
    except OSError:
        return Size(80, 24, 0, 0)
    return Size(cols, rows, xpix, ypix)


class Raw(object):
    """cbreak + no echo + alternate screen; restores everything on exit."""

    def __init__(self, alt_screen=True):
        self.alt = alt_screen
        self.fd = sys.stdin.fileno()
        self.saved = None

    def __enter__(self):
        try:
            self.saved = termios.tcgetattr(self.fd)
            tty.setcbreak(self.fd)
            attrs = termios.tcgetattr(self.fd)
            attrs[3] &= ~termios.ECHO
            termios.tcsetattr(self.fd, termios.TCSANOW, attrs)
        except termios.error:
            self.saved = None
        out = ""
        if self.alt:
            out += ESC + "[?1049h"
        out += ESC + "[?25l"
        write(out)
        return self

    def __exit__(self, *exc):
        write(kitty_delete_all() + ESC + "[?25h" + (ESC + "[?1049l" if self.alt else ""))
        if self.saved is not None:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)
        return False


def write(text):
    data = text.encode("utf-8", "replace") if isinstance(text, str) else text
    fd = sys.stdout.fileno()
    view = memoryview(data)
    while view:
        try:
            n = os.write(fd, view)
        except BlockingIOError:
            select.select([], [fd], [], 1.0)
            continue
        view = view[n:]


KEYS = {
    ESC + "[A": "up",
    ESC + "[B": "down",
    ESC + "[C": "right",
    ESC + "[D": "left",
    ESC + "OA": "up",
    ESC + "OB": "down",
    ESC + "OC": "right",
    ESC + "OD": "left",
    ESC + "[5~": "pgup",
    ESC + "[6~": "pgdn",
    ESC + "[H": "home",
    ESC + "[F": "end",
    ESC + "[1~": "home",
    ESC + "[4~": "end",
    ESC + "OH": "home",
    ESC + "OF": "end",
    ESC + "[Z": "backtab",
    "\r": "enter",
    "\n": "enter",
    "\x7f": "backspace",
    "\x08": "backspace",
    "\t": "tab",
    " ": "space",
    ESC: "esc",
}

_pending = []


def read_keys(timeout):
    """Return a list of key names typed within `timeout` seconds."""
    if _pending:
        keys = list(_pending)
        del _pending[:]
        return keys
    fd = sys.stdin.fileno()
    try:
        ready, _, _ = select.select([fd], [], [], timeout)
    except (InterruptedError, ValueError):
        return []
    if not ready:
        return []
    try:
        data = os.read(fd, 1024).decode("utf-8", "replace")
    except OSError:
        return []
    if data == "":
        return ["eof"]
    keys = []
    i = 0
    while i < len(data):
        if data[i] == ESC:
            # CSI / SS3 sequences; a lone ESC is the escape key
            m = re.match(r"\x1b(\[[0-9;:?]*[ -/]*[@-~]|O[A-Za-z]|_[^\x1b]*\x1b\\)", data[i:])
            if m:
                seq = m.group(0)
                if not seq.startswith(ESC + "_"):  # drop kitty graphics replies
                    keys.append(KEYS.get(seq, seq))
                i += len(seq)
                continue
            keys.append("esc")
            i += 1
            continue
        ch = data[i]
        keys.append(KEYS.get(ch, ch))
        i += 1
    return keys


def visible_len(text):
    return sum(char_width(c) for c in ANSI_RE.sub("", text))


def ansi_slice(text, start, width):
    """Cut `width` display cells from an ANSI string, skipping `start` cells."""
    out = []
    pos = 0
    i = 0
    n = len(text)
    end = start + width
    while i < n:
        m = ANSI_RE.match(text, i)
        if m:
            out.append(m.group(0))
            i = m.end()
            continue
        ch = text[i]
        w = char_width(ch)
        if pos >= start and pos + w <= end:
            out.append(ch)
        pos += w
        i += 1
        if pos >= end:
            # keep trailing escapes cheap: just stop here
            break
    return "".join(out) + RESET


def pad(text, width):
    vis = visible_len(text)
    if vis >= width:
        return ansi_slice(text, 0, width)
    return text + " " * (width - vis)


def move(row, col):
    """1-based row/col."""
    return "%s[%d;%dH" % (ESC, row, col)


def clear():
    return ESC + "[2J" + ESC + "[H"


# --- kitty graphics -------------------------------------------------------


def _apc(control, payload=b""):
    if payload:
        return "%s_G%s;%s%s\\" % (ESC, control, payload.decode("ascii"), ESC)
    return "%s_G%s%s\\" % (ESC, control, ESC)


def kitty_transmit(png_bytes, image_id, cols=None, rows=None, place=True, placement_id=1, z=None):
    """Send a PNG (format 100) in 4096-byte chunks; optionally place it at the cursor."""
    data = base64.standard_b64encode(png_bytes)
    chunks = [data[i : i + 4096] for i in range(0, len(data), 4096)] or [b""]
    action = "T" if place else "t"
    head = "a=%s,f=100,t=d,i=%d,q=2" % (action, image_id)
    if place:
        head += ",p=%d,C=1" % placement_id
        if cols:
            head += ",c=%d" % cols
        if rows:
            head += ",r=%d" % rows
        if z is not None:
            head += ",z=%d" % z
    parts = []
    for idx, chunk in enumerate(chunks):
        more = 1 if idx < len(chunks) - 1 else 0
        ctrl = (head + ",m=%d" % more) if idx == 0 else "m=%d,q=2" % more
        parts.append(_apc(ctrl, chunk))
    return "".join(parts)


def kitty_place(image_id, cols=None, rows=None, placement_id=1):
    ctrl = "a=p,i=%d,p=%d,C=1,q=2" % (image_id, placement_id)
    if cols:
        ctrl += ",c=%d" % cols
    if rows:
        ctrl += ",r=%d" % rows
    return _apc(ctrl)


def kitty_hide(image_id):
    """Delete placements of one image but keep its data for reuse."""
    return _apc("a=d,d=i,i=%d,q=2" % image_id)


def kitty_free(image_id):
    return _apc("a=d,d=I,i=%d,q=2" % image_id)


def kitty_delete_all():
    return _apc("a=d,d=A,q=2")


def png_size(data):
    """(width, height) from a PNG header, or None."""
    if len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
        return struct.unpack(">II", data[16:24])
    return None


def osc52_copy(text):
    payload = base64.standard_b64encode(text.encode("utf-8")).decode("ascii")
    return "%s]52;c;%s\x07" % (ESC, payload)
