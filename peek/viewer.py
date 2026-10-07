"""The peek viewer: an ordinary program in a herdr split that prints kitty graphics."""

import os
import queue
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback

import convert
import display
import herdr
import kinds
import render
import state
from term import (
    ACCENT,
    BOLD,
    ESC,
    GREEN,
    MUTED,
    RED,
    RESET,
    YELLOW,
    Raw,
    ansi_slice,
    clear,
    get_size,
    kitty_delete_all,
    kitty_free,
    kitty_hide,
    kitty_place,
    kitty_transmit,
    move,
    osc52_copy,
    pad,
    png_size,
    read_keys,
    write,
)

HOST = socket.gethostname().split(".")[0]
_next_id = [100]


def new_image_id():
    _next_id[0] += 1
    if _next_id[0] > 4000000:
        _next_id[0] = 101
    return _next_id[0]


class Box(object):
    def __init__(self, top, left, cols, rows, cell):
        self.top, self.left, self.cols, self.rows = top, left, cols, rows
        self.cw, self.ch = cell

    @property
    def px(self):
        return int(self.cols * self.cw), int(self.rows * self.ch)

    def sub(self, top_off, rows):
        return Box(self.top + top_off, self.left, self.cols, rows, (self.cw, self.ch))


def fit_geom(png, box, fit=True):
    """(top, left, cols, rows) that shows `png` centred in `box` at its aspect ratio."""
    size = png_size(png) or (box.px[0], box.px[1])
    cells_w = size[0] / box.cw
    cells_h = size[1] / box.ch
    scale = min(box.cols / max(cells_w, 0.01), box.rows / max(cells_h, 0.01))
    if not fit:
        scale = min(scale, 1.0)
    cols = max(1, min(box.cols, int(round(cells_w * scale))))
    rows = max(1, min(box.rows, int(round(cells_h * scale))))
    left = box.left + (box.cols - cols) // 2
    top = box.top + (box.rows - rows) // 2
    return top, left, cols, rows


def place_png(png, box, image_id, fit=True):
    """Escape output that uploads `png` and places it centred in `box`."""
    top, left, cols, rows = fit_geom(png, box, fit)
    return move(top, left) + kitty_transmit(png, image_id, cols, rows), (top, left, cols, rows)


# --- views -------------------------------------------------------------------


class View(object):
    keys_help = ""
    tip = None

    def __init__(self, app, path, line=None):
        self.app = app
        self.path = path
        self.line = line
        self.details = ""
        self.image_ids = []

    def draw(self, box):
        return ""

    def key(self, k, box):
        return False

    def tick(self, box):
        return None

    def wants_tick(self):
        return False

    def close(self):
        out = "".join(kitty_free(i) for i in self.image_ids)
        self.image_ids = []
        return out


class TextView(View):
    keys_help = "j/k scroll · ←/→ pan · g/G top/end"

    def __init__(self, app, path, line=None, producer=None):
        View.__init__(self, app, path, line)
        self.producer = producer
        self.lines = []
        self.scroll = 0
        self.hscroll = 0
        self.width = None
        self.jumped = False

    def build(self, width):
        out = self.producer(width)
        lines, extra = out[0], out[1] if len(out) > 1 else None
        if len(out) > 2:
            self.extra = out[2]
        return lines, extra

    def ensure(self, box):
        if self.width != box.cols:
            self.width = box.cols
            lines, extra = self.build(box.cols)
            self.lines = lines
            if extra and extra.startswith("install"):
                self.tip = extra
            elif extra:
                self.details = extra
            if self.line and not self.jumped:
                self.jumped = True
                target = self._line_index(self.line)
                self.scroll = max(0, target - box.rows // 3)

    def _line_index(self, line):
        pat = re.compile(r"^\s*%d(\s|│|$)" % line)
        for i, l in enumerate(self.lines):
            if pat.match(re.sub(r"\x1b\[[0-9;]*m", "", l)):
                return i
        return max(0, line - 1)

    def draw(self, box):
        box = Box(box.top, box.left + 1, box.cols - 1, box.rows, (box.cw, box.ch))
        self.ensure(box)
        self.scroll = max(0, min(self.scroll, max(0, len(self.lines) - box.rows)))
        out = []
        for i in range(box.rows):
            idx = self.scroll + i
            text = self.lines[idx] if idx < len(self.lines) else ""
            out.append(move(box.top + i, box.left) + pad(ansi_slice(text, self.hscroll, box.cols), box.cols))
        return "".join(out)

    def key(self, k, box):
        page = max(1, box.rows - 2)
        moves = {
            "j": 1, "down": 1, "k": -1, "up": -1,
            "d": page // 2, "u": -(page // 2), "pgdn": page, "pgup": -page,
            "space": page,
        }
        if k in moves:
            self.scroll += moves[k]
        elif k in ("g", "home"):
            self.scroll = 0
        elif k in ("G", "end"):
            self.scroll = len(self.lines)
        elif k in ("right", "l"):
            self.hscroll += 8
        elif k in ("left", "h"):
            self.hscroll = max(0, self.hscroll - 8)
        else:
            return False
        return True


class ImageView(View):
    keys_help = "f fit/1:1"

    def __init__(self, app, path, line=None):
        View.__init__(self, app, path, line)
        w, h, _ = convert.image_info(path)
        if w and h:
            self.details = "%d×%d" % (w, h)
        self.fit = True
        self.cache = {}

    def png(self, box):
        k = (box.px, self.fit)
        if k not in self.cache:
            self.cache[k] = convert.image_png(self.path, box.px[0], box.px[1], "fit" if self.fit else "actual")
        return self.cache[k]

    def draw(self, box):
        png = self.png(box)
        if not png:
            self.tip = "install imagemagick (or libvips / ffmpeg) to render this image"
            return move(box.top + 1, box.left + 2) + YELLOW + "Can't decode this image here." + RESET
        image_id = new_image_id()
        self.image_ids.append(image_id)
        out, _ = place_png(png, box, image_id, fit=self.fit)
        return out

    def key(self, k, box):
        if k == "f":
            self.fit = not self.fit
            self.app.toast("fit to split" if self.fit else "actual size (1:1)")
            return True
        return False


class PngView(ImageView):
    """An already-rendered PNG (HTML screenshot, notebook output)."""

    def __init__(self, app, path, line, data):
        View.__init__(self, app, path, line)
        self.data = data
        self.fit = True
        size = png_size(data)
        self.details = ("%d×%d" % size) if size else ""

    def png(self, box):
        return self.data


class AnimView(View):
    keys_help = "space pause"

    def __init__(self, app, path, line=None):
        View.__init__(self, app, path, line)
        w, h, frames = convert.image_info(path)
        self.details = ("%d×%d · %d frames" % (w, h, frames)) if w else ""
        self.frames = None
        self.box_px = None
        self.idx = 0
        self.ids = []
        self.playing = True
        self.due = 0
        self.geom = None

    def draw(self, box):
        if self.box_px != box.px or self.frames is None:
            self.box_px = box.px
            self.frames = convert.anim_frames(self.path, box.px[0], box.px[1])
        if not self.frames:
            return ImageView(self.app, self.path).draw(box)
        self.ids = [new_image_id() for _ in self.frames]
        self.image_ids.extend(self.ids)
        out = []
        # upload every frame once, then just move placements around
        for fid, (png, _delay) in zip(self.ids, self.frames):
            out.append(kitty_transmit(png, fid, place=False))
        self.geom = fit_geom(self.frames[self.idx][0], box)
        out.append(move(self.geom[0], self.geom[1]) + kitty_place(self.ids[self.idx], self.geom[2], self.geom[3]))
        self.due = time.time() + self.frames[self.idx][1]
        return "".join(out)

    def wants_tick(self):
        return self.playing and bool(self.frames) and len(self.frames) > 1

    def tick(self, box):
        if not self.wants_tick() or time.time() < self.due or not self.geom:
            return None
        prev = self.ids[self.idx]
        self.idx = (self.idx + 1) % len(self.frames)
        self.due = time.time() + max(self.frames[self.idx][1], 0.02)
        top, left, cols, rows = self.geom
        return move(top, left) + kitty_place(self.ids[self.idx], cols, rows) + kitty_hide(prev)

    def key(self, k, box):
        if k == "space":
            self.playing = not self.playing
            self.app.toast("playing" if self.playing else "paused")
            return False
        return False

    def next_due(self):
        return self.due


class PngReader(threading.Thread):
    """Splits a stream of concatenated PNGs from ffmpeg into frames."""

    def __init__(self, proc, frames):
        threading.Thread.__init__(self)
        self.daemon = True
        self.proc = proc
        self.frames = frames

    def run(self):
        stream = self.proc.stdout
        try:
            while True:
                sig = stream.read(8)
                if len(sig) < 8:
                    break
                buf = [sig]
                while True:
                    head = stream.read(8)
                    if len(head) < 8:
                        return
                    length = int.from_bytes(head[:4], "big")
                    body = stream.read(length + 4)
                    buf.append(head)
                    buf.append(body)
                    if head[4:8] == b"IEND":
                        break
                self.frames.put(b"".join(buf))
        finally:
            self.frames.put(None)


def fmt_time(t):
    t = max(0, int(t))
    return "%d:%02d" % (t // 60, t % 60) if t < 3600 else "%d:%02d:%02d" % (t // 3600, (t // 60) % 60, t % 60)


class VideoView(View):
    keys_help = "space play · ←/→ seek 5s · [/] 30s"

    def __init__(self, app, path, line=None):
        View.__init__(self, app, path, line)
        info = convert.probe(path) or {}
        self.duration = float((info.get("format") or {}).get("duration") or 0)
        vstream = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), {})
        self.details = " · ".join(
            x
            for x in (
                "%s×%s" % (vstream.get("width"), vstream.get("height")) if vstream.get("width") else "",
                fmt_time(self.duration) if self.duration else "",
                vstream.get("codec_name", ""),
            )
            if x
        )
        if not convert.which("ffmpeg"):
            self.tip = "install ffmpeg for video frames"
        self.t = min(self.duration * 0.1, 3.0) if self.duration else 0.0
        self.proc = None
        self.frames = None
        self.fps = int(app.config.get("video_fps") or 8)
        self.due = 0
        self.ids = [new_image_id(), new_image_id()]
        self.image_ids.extend(self.ids)
        self.flip = 0
        self.box = None

    def frame_box(self, box):
        return box.sub(0, max(1, box.rows - 2))

    def bar(self, box):
        width = max(box.cols - 16, 10)
        frac = (self.t / self.duration) if self.duration else 0
        filled = int(width * min(max(frac, 0), 1))
        state_icon = "▶" if self.proc else "❚❚"
        text = " %s %s%s%s%s %s / %s" % (
            state_icon,
            ACCENT,
            "━" * filled,
            MUTED + "─" * (width - filled),
            RESET,
            fmt_time(self.t),
            fmt_time(self.duration),
        )
        return move(box.top + box.rows - 1, box.left) + pad(text, box.cols)

    def show(self, png, box):
        fb = self.frame_box(box)
        new = self.ids[self.flip]
        old = self.ids[1 - self.flip]
        self.flip = 1 - self.flip
        out, _ = place_png(png, fb, new)
        return out + kitty_free(old)

    def draw(self, box):
        self.box = box
        if not convert.which("ffmpeg"):
            return move(box.top + 1, box.left + 2) + YELLOW + "ffmpeg not found: showing file info only" + RESET
        fb = self.frame_box(box)
        png = convert.video_frame(self.path, self.t, fb.px[0], fb.px[1])
        out = self.show(png, box) if png else move(box.top + 1, box.left + 2) + RED + "could not decode a frame" + RESET
        return out + self.bar(box)

    def start(self, box):
        fb = self.frame_box(box)
        self.frames = queue.Queue(maxsize=4)
        self.proc = convert.video_stream(self.path, self.t, fb.px[0], fb.px[1], self.fps)
        if self.proc:
            PngReader(self.proc, self.frames).start()
            self.due = time.time()

    def stop(self):
        if self.proc:
            try:
                self.proc.kill()
                self.proc.wait(timeout=2)
            except Exception:
                pass
        self.proc = None
        self.frames = None

    def wants_tick(self):
        return self.proc is not None

    def next_due(self):
        return self.due

    def tick(self, box):
        if not self.proc or time.time() < self.due:
            return None
        try:
            png = self.frames.get_nowait()
        except queue.Empty:
            return None
        if png is None:
            self.stop()
            return self.bar(box)
        self.due = time.time() + 1.0 / self.fps
        self.t = min(self.t + 1.0 / self.fps, self.duration or self.t + 1.0 / self.fps)
        return self.show(png, box) + self.bar(box)

    def seek(self, delta, box):
        playing = self.proc is not None
        self.stop()
        self.t = max(0.0, min(self.t + delta, max(self.duration - 0.1, 0)))
        if playing:
            self.start(box)
        return True

    def key(self, k, box):
        if k == "space":
            if self.proc:
                self.stop()
                write(self.bar(box))
            else:
                if self.duration and self.t >= self.duration - 0.2:
                    self.t = 0
                self.start(box)
            return False
        if k in ("right", "l"):
            return self.seek(5, box)
        if k in ("left", "h"):
            return self.seek(-5, box)
        if k == "]":
            return self.seek(30, box)
        if k == "[":
            return self.seek(-30, box)
        if k in ("0", "home"):
            return self.seek(-self.t, box)
        return False

    def close(self):
        self.stop()
        return View.close(self)


class AudioView(View):
    keys_help = "space play (local only)"

    def __init__(self, app, path, line=None):
        View.__init__(self, app, path, line)
        self.info = convert.probe(path) or {}
        fmt = self.info.get("format") or {}
        self.duration = float(fmt.get("duration") or 0)
        astream = next((s for s in self.info.get("streams", []) if s.get("codec_type") == "audio"), {})
        self.details = " · ".join(x for x in (fmt_time(self.duration) if self.duration else "", astream.get("codec_name", ""), ("%s Hz" % astream["sample_rate"]) if astream.get("sample_rate") else "") if x)
        self.astream = astream
        self.player = None
        if not convert.which("ffmpeg"):
            self.tip = "install ffmpeg for a waveform and audio info"

    def draw(self, box):
        wave_rows = max(3, box.rows // 3)
        out = []
        if not self.app.inline:
            wave_rows = 0  # the waveform is a picture; skip it where pictures can't show
        png = convert.audio_wave(self.path, box.px[0], int(wave_rows * box.ch)) if wave_rows and convert.which("ffmpeg") else None
        if png:
            image_id = new_image_id()
            self.image_ids.append(image_id)
            o, _ = place_png(png, box.sub(0, wave_rows), image_id)
            out.append(o)
        fmt = self.info.get("format") or {}
        tags = dict((k.lower(), v) for k, v in (fmt.get("tags") or {}).items())
        rows = [
            ("duration", fmt_time(self.duration) if self.duration else "?"),
            ("codec", self.astream.get("codec_long_name") or self.astream.get("codec_name") or "?"),
            ("channels", str(self.astream.get("channels") or "?")),
            ("sample rate", ("%s Hz" % self.astream.get("sample_rate")) if self.astream.get("sample_rate") else "?"),
            ("bitrate", ("%d kb/s" % (int(fmt.get("bit_rate")) // 1000)) if str(fmt.get("bit_rate") or "").isdigit() else "?"),
        ]
        for tag in ("title", "artist", "album", "date", "genre"):
            if tags.get(tag):
                rows.append((tag, tags[tag]))
        top = box.top + wave_rows + 1
        for i, (k, v) in enumerate(rows):
            if top + i >= box.top + box.rows:
                break
            out.append(move(top + i, box.left + 1) + BOLD + k.ljust(12) + RESET + v)
        if os.environ.get("SSH_CONNECTION"):
            out.append(move(min(top + len(rows) + 1, box.top + box.rows - 1), box.left + 1) + MUTED + "remote machine: playback would sound on the remote, so it stays muted" + RESET)
        elif self.player and self.player.poll() is None:
            out.append(move(min(top + len(rows) + 1, box.top + box.rows - 1), box.left + 1) + GREEN + "▶ playing" + RESET)
        return "".join(out)

    def key(self, k, box):
        if k != "space":
            return False
        if self.player and self.player.poll() is None:
            self.player.terminate()
            self.player = None
            self.app.toast("stopped")
            return True
        if os.environ.get("SSH_CONNECTION"):
            self.app.toast("playback only on Local")
            return False
        cmd = convert.audio_player()
        if not cmd:
            self.app.toast("install ffmpeg (ffplay) or mpv to play audio")
            return False
        self.player = subprocess.Popen(cmd + [self.path], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.app.toast("playing")
        return True

    def close(self):
        if self.player and self.player.poll() is None:
            self.player.terminate()
        return View.close(self)


class PdfView(View):
    keys_help = "←/→ page · g/G first/last · t text"

    def __init__(self, app, path, line=None, display_path=None):
        View.__init__(self, app, path, line)
        self.pages, self.page_size = convert.pdf_info(path)
        self.page = 1
        self.text_mode = False
        self.text_view = None
        if not convert.which("pdftoppm"):
            self.tip = "install poppler for page rendering"
        self.update_details()

    def update_details(self):
        self.details = "page %d / %d" % (self.page, self.pages)

    def draw(self, box):
        self.update_details()
        if self.text_mode:
            if not self.text_view:
                page = self.page
                self.text_view = TextView(self.app, self.path, producer=lambda w: ((convert.pdf_text(self.path, page) or "install poppler (pdftotext) for text").split("\n"), None))
            return self.text_view.draw(box)
        pw, ph = self.page_size
        bw, bh = box.px
        scale = min(bw / pw, bh / ph)
        png = convert.pdf_page_png(self.path, self.page, max(int(pw * scale), 16), max(int(ph * scale), 16))
        if not png:
            self.text_mode = True
            return self.draw(box)
        image_id = new_image_id()
        self.image_ids.append(image_id)
        out, _ = place_png(png, box, image_id)
        return out

    def turn(self, page):
        page = max(1, min(self.pages, page))
        if page == self.page:
            return False
        self.page = page
        self.text_view = None
        return True

    def key(self, k, box):
        if k in ("right", "l", "pgdn", "J") or (k in ("space", "j", "down") and not self.text_mode):
            return self.turn(self.page + 1)
        if k in ("left", "h", "pgup", "K") or (k in ("k", "up") and not self.text_mode):
            return self.turn(self.page - 1)
        if k in ("g", "home") and not self.text_mode:
            return self.turn(1)
        if k in ("G", "end") and not self.text_mode:
            return self.turn(self.pages)
        if k == "t":
            self.text_mode = not self.text_mode
            self.text_view = None
            return True
        if self.text_mode and self.text_view:
            return self.text_view.key(k, box)
        return False


class BackgroundView(View):
    """Shows a message while a slow conversion runs, then becomes the real view."""

    def __init__(self, app, path, line, message, work, then):
        View.__init__(self, app, path, line)
        self.message = message
        self.result = None
        self.done = False
        self.then = then
        self.details = "converting…"

        def runner():
            try:
                self.result = work()
            except Exception as exc:  # shown in the pane
                self.result = exc
            self.done = True

        threading.Thread(target=runner, daemon=True).start()
        self.started = time.time()

    def draw(self, box):
        dots = "." * (1 + int(time.time() - self.started) % 3)
        return move(box.top + box.rows // 2, box.left + 2) + ACCENT + self.message + dots + RESET

    def wants_tick(self):
        return True

    def next_due(self):
        return time.time() + 0.25

    def tick(self, box):
        if self.done:
            self.app.replace_view(self.then(self.result))
            return None
        return self.draw(box)


class ToggleView(View):
    """Two renderings of one file (rendered ⇄ source), switched with `s`."""

    keys_help = "s rendered/source"

    def __init__(self, app, path, line, primary, secondary, labels=("rendered", "source")):
        View.__init__(self, app, path, line)
        self.views = [primary, secondary]
        self.labels = labels
        self.which = 0

    @property
    def cur(self):
        return self.views[self.which]

    def draw(self, box):
        out = self.cur.draw(box)
        self.details = " · ".join(x for x in (self.cur.details, self.labels[self.which]) if x)
        self.tip = self.cur.tip
        return out

    def key(self, k, box):
        if k == "s":
            self.app.write(self.cur.close())
            self.which = 1 - self.which
            self.app.toast(self.labels[self.which])
            return True
        return self.cur.key(k, box)

    @property
    def keys_help_dyn(self):
        return "s %s · %s" % (self.labels[1 - self.which], self.cur.keys_help)

    def wants_tick(self):
        return self.cur.wants_tick()

    def tick(self, box):
        return self.cur.tick(box)

    def next_due(self):
        return getattr(self.cur, "next_due", lambda: time.time() + 0.15)()

    def close(self):
        return "".join(v.close() for v in self.views)


class DirView(View):
    keys_help = "j/k move · enter open · backspace up · . hidden"

    def __init__(self, app, path, line=None):
        View.__init__(self, app, path, line)
        self.cursor = 0
        self.scroll = 0
        self.hidden = False
        self.load()

    def load(self):
        self.entries, err = render.list_dir(self.path, self.hidden)
        self.details = err or "%d entries" % len(self.entries)
        self.cursor = min(self.cursor, max(len(self.entries) - 1, 0))

    def draw(self, box):
        lines = render.dir_lines(self.entries, self.cursor, box.cols)
        if self.cursor < self.scroll:
            self.scroll = self.cursor
        if self.cursor >= self.scroll + box.rows:
            self.scroll = self.cursor - box.rows + 1
        out = []
        for i in range(box.rows):
            idx = self.scroll + i
            out.append(move(box.top + i, box.left) + pad(lines[idx] if idx < len(lines) else "", box.cols))
        return "".join(out)

    def key(self, k, box):
        if k in ("j", "down"):
            self.cursor = min(self.cursor + 1, len(self.entries) - 1)
        elif k in ("k", "up"):
            self.cursor = max(self.cursor - 1, 0)
        elif k in ("pgdn", "d"):
            self.cursor = min(self.cursor + box.rows - 2, len(self.entries) - 1)
        elif k in ("pgup", "u"):
            self.cursor = max(self.cursor - box.rows + 2, 0)
        elif k in ("g", "home"):
            self.cursor = 0
        elif k in ("G", "end"):
            self.cursor = len(self.entries) - 1
        elif k == ".":
            self.hidden = not self.hidden
            self.load()
        elif k in ("enter", "right", "l") and self.entries:
            self.app.push_view(make_view(self.app, self.entries[self.cursor][1]))
        elif k in ("backspace", "left", "h"):
            parent = os.path.dirname(self.path.rstrip("/")) or "/"
            if parent != self.path:
                self.app.push_view(make_view(self.app, parent))
        else:
            return False
        return True


class NotebookView(TextView):
    keys_help = "j/k scroll · i output images"

    def __init__(self, app, path, line=None):
        self.extra = []
        TextView.__init__(self, app, path, line, producer=lambda w: render.notebook(path, w))
        self.img = 0

    def key(self, k, box):
        if k == "i" and self.extra:
            target = self.extra[self.img % len(self.extra)]
            self.img += 1
            self.app.push_view(make_view(self.app, target))
            return True
        return TextView.key(self, k, box)


class ErrorView(TextView):
    def __init__(self, app, path, message):
        TextView.__init__(self, app, path, producer=lambda w: ([RED + l + RESET for l in message.rstrip().split("\n")], None))


class ExternalView(TextView):
    """For terminals that can't draw images: open the file in the desktop's viewer."""

    keys_help = "o open again"

    def __init__(self, app, path, line, kind):
        self.term = display.client_terminal()
        self.opened = display.can_open_external() and display.open_external(path)
        TextView.__init__(self, app, path, line, producer=self.card)
        self.details = kind

    def card(self, width):
        name = os.path.basename(self.path)
        term = self.term or "this terminal"
        if self.opened:
            head = [GREEN + BOLD + "Opened %s in your default viewer." % name + RESET]
        else:
            head = [YELLOW + BOLD + "Can't show %s here." % name + RESET, "", "There's no desktop on this machine to open it in."]
        return head + [
            "",
            MUTED + "%s can't draw images inside herdr. Run herdr in" % term + RESET,
            MUTED + "Ghostty, kitty or WezTerm to see them right here." + RESET,
        ], None

    def key(self, k, box):
        if k == "o" and display.can_open_external():
            display.open_external(self.path)
            self.app.toast("opened " + os.path.basename(self.path))
            return False
        return TextView.key(self, k, box)


def make_view(app, path, line=None):
    if not os.path.exists(path):
        return ErrorView(app, path, "File not found:\n  %s" % path)
    kind = kinds.classify(path)
    if kind in display.VISUAL_KINDS and not app.inline:
        return ExternalView(app, path, line, kind)
    try:
        if kind == "dir":
            return DirView(app, path, line)
        if kind == "image":
            return ImageView(app, path, line)
        if kind == "anim":
            return AnimView(app, path, line)
        if kind == "video":
            return VideoView(app, path, line)
        if kind == "audio":
            return AudioView(app, path, line)
        if kind == "pdf":
            return PdfView(app, path, line)
        if kind == "office":
            if not convert.soffice():
                v = TextView(app, path, line, producer=lambda w: render.info_hex(path, w))
                v.tip = "install libreoffice to render Office documents"
                return v

            def done(result):
                if isinstance(result, str) and os.path.exists(result):
                    v = PdfView(app, result, line)
                    v.display_path = path
                    return v
                return ErrorView(app, path, "LibreOffice could not convert this file.")

            return BackgroundView(app, path, line, "Converting with LibreOffice (first open is slow, cached after)", lambda: convert.office_pdf(path), done)
        if kind == "html":
            source = TextView(app, path, line, producer=lambda w: render.code(path, w, line))
            if not convert.chromium():
                source.tip = "install chromium for a rendered screenshot"
                return source

            def shot_done(result):
                if isinstance(result, bytes):
                    shot = PngView(app, path, line, result)
                    shot.details = "offline"
                    return ToggleView(app, path, line, shot, source, ("screenshot", "source"))
                return source

            # screenshot at a fixed desktop width; the split scales it down
            return BackgroundView(app, path, line, "Rendering page with headless Chromium", lambda: convert.html_png(path, 1280, 900), shot_done)
        if kind == "markdown":
            rendered = TextView(app, path, line, producer=lambda w: render.markdown(path, w))
            source = TextView(app, path, line, producer=lambda w: render.code(path, w, line))
            return ToggleView(app, path, line, rendered, source)
        if kind == "csv":
            return TextView(app, path, line, producer=lambda w: render.csv_table(path, w))
        if kind == "json":
            return TextView(app, path, line, producer=lambda w: render.json_tree(path, w))
        if kind == "jsonl":
            return TextView(app, path, line, producer=lambda w: render.jsonl(path, w))
        if kind == "notebook":
            return NotebookView(app, path, line)
        if kind == "sqlite":
            return TextView(app, path, line, producer=lambda w: render.sqlite(path, w))
        if kind == "archive":
            return TextView(app, path, line, producer=lambda w: render.archive(path, w))
        if kind == "text":
            return TextView(app, path, line, producer=lambda w: render.code(path, w, line))
        return TextView(app, path, line, producer=lambda w: render.info_hex(path, w))
    except Exception:
        return ErrorView(app, path, "peek hit an error opening this file:\n\n" + traceback.format_exc())


# --- app ---------------------------------------------------------------------


class App(object):
    def __init__(self, tab_id):
        self.tab = tab_id
        self.config = state.load_config()
        self.inline = display.inline_images(self.config)
        self.items = []
        self.index = 0
        self.seq = None
        self.view = None
        self.stack = []
        self.message = None
        self.message_until = 0
        self.dirty = True
        self.resized = False
        self.cell = None
        self.size = None
        self.footer_dirty = False

    # state ---------------------------------------------------------------
    def register(self):
        pane_id = os.environ.get("HERDR_PANE_ID")
        info = herdr.pane_get(pane_id) if pane_id else None
        state.write_json(
            state.viewer_file(self.tab),
            {"pane_id": pane_id, "terminal_id": (info or {}).get("terminal_id"), "pid": os.getpid()},
        )

    def unregister(self):
        data = state.read_json(state.viewer_file(self.tab)) or {}
        if data.get("pid") == os.getpid():
            try:
                os.unlink(state.viewer_file(self.tab))
            except OSError:
                pass

    def poll_current(self):
        data = state.read_json(state.current_file(self.tab))
        if not data or data.get("seq") == self.seq:
            return False
        self.seq = data.get("seq")
        self.items = data.get("items") or []
        self.index = int(data.get("index") or 0)
        self.open_index()
        return True

    # views ---------------------------------------------------------------
    def write(self, text):
        if text:
            write(text)

    def set_view(self, view):
        if self.view:
            self.write(self.view.close())
        for v in self.stack:
            self.write(v.close())
        self.stack = []
        self.view = view
        self.dirty = True

    def replace_view(self, view):
        if self.view:
            self.write(self.view.close())
        self.view = view
        self.dirty = True

    def push_view(self, view):
        self.write(kitty_delete_all())
        self.stack.append(self.view)
        self.view = view
        self.dirty = True

    def pop_view(self):
        if not self.stack:
            return False
        self.write(self.view.close())
        self.view = self.stack.pop()
        self.dirty = True
        return True

    def open_index(self):
        if not self.items:
            self.set_view(ErrorView(self, "", "Nothing to show yet.\n\nPress prefix+f in an agent pane to pick a file."))
            return
        self.index %= len(self.items)
        item = self.items[self.index]
        self.inline = display.inline_images(self.config)  # you may have switched terminals
        self.set_view(make_view(self, item.get("path"), item.get("line")))

    def toast(self, text):
        self.message = text
        self.message_until = time.time() + 2.0
        self.footer_dirty = True

    # drawing ---------------------------------------------------------------
    def measure(self):
        self.size = get_size()
        cell_file = os.path.join(state.state_dir(), "cell.json")
        if self.size.cell:
            self.cell = self.size.cell
            # popups get no pixel size from herdr; remember the real cell for them
            try:
                state.write_json(cell_file, {"cell": list(self.cell)})
            except OSError:
                pass
        elif self.cell is None:
            remembered = (state.read_json(cell_file) or {}).get("cell")
            self.cell = self.query_cell() or (tuple(remembered) if remembered else (10.0, 20.0))

    def query_cell(self):
        write(ESC + "[16t")
        deadline = time.time() + 0.3
        buf = ""
        import select as _select

        fd = sys.stdin.fileno()
        while time.time() < deadline:
            ready, _, _ = _select.select([fd], [], [], max(0, deadline - time.time()))
            if not ready:
                break
            buf += os.read(fd, 256).decode("utf-8", "replace")
            m = re.search(r"\x1b\[6;(\d+);(\d+)t", buf)
            if m:
                return float(m.group(2)), float(m.group(1))
        return None

    def content_box(self):
        rows = self.size.rows - 2
        if self.view and self.view.tip:
            rows -= 1
        return Box(2, 1, self.size.cols, max(rows, 1), self.cell)

    def title_line(self):
        if not self.view:
            return ""
        path = getattr(self.view, "display_path", None) or self.view.path or ""
        name = os.path.basename(path.rstrip("/")) or path
        try:
            size = render.human(os.path.getsize(path)) if path and os.path.isfile(path) else ""
        except OSError:
            size = ""
        parts = [x for x in (self.view.details, size, HOST) if x]
        pos = ("  [%d/%d]" % (self.index + 1, len(self.items))) if len(self.items) > 1 else ""
        text = " %s%s%s %s· %s%s%s" % (BOLD + ACCENT, name, RESET, MUTED, " · ".join(parts), pos, RESET)
        return move(1, 1) + pad(text, self.size.cols)

    def footer_line(self):
        if self.message and time.time() < self.message_until:
            text = " " + GREEN + self.message + RESET
        else:
            helper = getattr(self.view, "keys_help_dyn", None) or getattr(self.view, "keys_help", "")
            base = "n next · p prev · y copy path · q close" if len(self.items) > 1 else "y copy path · q close"
            text = " " + MUTED + " · ".join(x for x in (helper, base) if x) + RESET
        out = move(self.size.rows, 1) + pad(text, self.size.cols)
        if self.view and self.view.tip:
            out = move(self.size.rows - 1, 1) + pad(" " + YELLOW + "tip: " + self.view.tip + RESET, self.size.cols) + out
        return out

    def redraw(self):
        self.measure()
        box = self.content_box()
        out = [kitty_delete_all(), clear()]
        try:
            body = self.view.draw(box) if self.view else ""
        except Exception:
            self.view = ErrorView(self, getattr(self.view, "path", ""), "peek hit an error rendering this file:\n\n" + traceback.format_exc())
            body = self.view.draw(box)
        out.append(self.title_line())
        out.append(body)
        out.append(self.footer_line())
        write("".join(out))
        self.dirty = False

    # input -----------------------------------------------------------------
    def handle(self, k):
        box = self.content_box()
        if self.view and self.view.key(k, box):
            self.dirty = True
            return True
        if k == "q":
            return False
        if k in ("esc", "backspace"):
            if self.pop_view():
                return True
            if k == "esc":
                return False
            return True
        if k == "n" and self.items:
            self.index = (self.index + 1) % len(self.items)
            self.open_index()
        elif k == "p" and self.items:
            self.index = (self.index - 1) % len(self.items)
            self.open_index()
        elif k == "y" and self.view:
            self.copy(getattr(self.view, "display_path", None) or self.view.path)
        elif k == "r" and self.view:
            self.set_view(make_view(self, self.view.path, self.view.line))
        elif k == "eof":
            return False
        return True

    def copy(self, text):
        write(osc52_copy(text))
        if not os.environ.get("SSH_CONNECTION"):
            for cmd in (["wl-copy"], ["pbcopy"], ["xclip", "-selection", "clipboard"]):
                if convert.which(cmd[0]):
                    try:
                        subprocess.run(cmd, input=text.encode(), timeout=2, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    except Exception:
                        pass
                    break
        self.toast("copied " + text)

    def run(self):
        def on_winch(signum, frame):
            self.resized = True

        signal.signal(signal.SIGWINCH, on_winch)
        signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
        signal.signal(signal.SIGHUP, lambda *a: sys.exit(0))
        self.register()
        with Raw():
            self.measure()
            if not self.poll_current():
                self.open_index()
            try:
                self.loop()
            finally:
                if self.view:
                    write(self.view.close())
                self.unregister()

    def loop(self):
        last_msg_state = False
        while True:
            if self.dirty or self.resized:
                self.resized = False
                self.redraw()
            timeout = 0.15
            if self.view and self.view.wants_tick():
                due = getattr(self.view, "next_due", lambda: time.time())()
                timeout = max(0.0, min(timeout, due - time.time()))
            for k in read_keys(timeout):
                if not self.handle(k):
                    return
            if self.dirty:
                continue
            self.poll_current()
            if self.view and self.view.wants_tick():
                out = self.view.tick(self.content_box())
                if out:
                    write(out)
            msg_state = bool(self.message and time.time() < self.message_until)
            if msg_state != last_msg_state or self.footer_dirty:
                self.footer_dirty = False
                write(self.footer_line())
            last_msg_state = msg_state


def main():
    tab = os.environ.get("PEEK_TAB") or os.environ.get("HERDR_TAB_ID") or "default"
    direct = os.environ.get("PEEK_PATH")
    if direct:
        state.set_current(tab, [{"path": direct, "line": None}], 0)
    App(tab).run()
