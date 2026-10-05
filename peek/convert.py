"""Optional external converters. Every helper degrades to None when a tool is missing."""

import glob
import hashlib
import json
import os
import re
import shutil
import subprocess

from state import cache_dir

_which = {}


def which(*names):
    for name in names:
        if name not in _which:
            found = shutil.which(name)
            if not found and name.startswith("/") and os.access(name, os.X_OK):
                found = name
            _which[name] = found
        if _which[name]:
            return _which[name]
    return None


def magick():
    """ImageMagick 7 (`magick`) or 6 (`convert`)."""
    tool = which("magick")
    if tool:
        return [tool]
    tool = which("convert")
    return [tool] if tool else None


def chromium():
    return which(
        "chromium",
        "chromium-browser",
        "google-chrome",
        "google-chrome-stable",
        "brave",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
    )


def soffice():
    return which("libreoffice", "soffice", "/Applications/LibreOffice.app/Contents/MacOS/soffice")


def run(cmd, timeout=60, stdin=None):
    try:
        proc = subprocess.run(
            cmd,
            input=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False, b""
    return proc.returncode == 0, proc.stdout


def key(path, *extra):
    try:
        st = os.stat(path)
        sig = "%s|%d|%d" % (os.path.abspath(path), st.st_mtime_ns, st.st_size)
    except OSError:
        sig = os.path.abspath(path)
    sig += "|" + "|".join(str(e) for e in extra)
    return hashlib.sha1(sig.encode("utf-8", "replace")).hexdigest()[:20]


def cached(name):
    return os.path.join(cache_dir(), name)


def _read(path):
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        return None


# --- images ---------------------------------------------------------------


def image_png(path, w, h, mode="fit"):
    """A PNG scaled to fit w x h pixels ("fit") or cropped to it at 1:1 ("actual")."""
    w, h = max(int(w), 16), max(int(h), 16)
    out = cached("img-%s.png" % key(path, w, h, mode))
    if os.path.exists(out):
        return _read(out)
    src = path + "[0]"
    m = magick()
    if m:
        cmd = list(m)
        if path.lower().endswith((".svg", ".svgz")):
            cmd += ["-density", "192", "-background", "none"]
        cmd += [src, "-auto-orient"]
        if mode == "fit":
            cmd += ["-resize", "%dx%d" % (w, h)]
        else:
            cmd += ["-gravity", "center", "-crop", "%dx%d+0+0" % (w, h), "+repage"]
        cmd += ["png:" + out]
        ok, _ = run(cmd)
        if ok and os.path.exists(out):
            return _read(out)
    vips = which("vips")
    if vips and mode == "fit":
        ok, _ = run([vips, "thumbnail", path, out, str(w), "--height", str(h)])
        if ok and os.path.exists(out):
            return _read(out)
    ffmpeg = which("ffmpeg")
    if ffmpeg:
        vf = "scale=%d:%d:force_original_aspect_ratio=decrease" % (w, h)
        if mode != "fit":
            vf = "crop='min(iw,%d)':'min(ih,%d)'" % (w, h)
        ok, data = run([ffmpeg, "-v", "error", "-i", path, "-frames:v", "1", "-vf", vf, "-f", "image2pipe", "-vcodec", "png", "-"])
        if ok and data:
            with open(out, "wb") as fh:
                fh.write(data)
            return data
    sips = which("sips")
    if sips:
        ok, _ = run([sips, "-s", "format", "png", "-Z", str(min(w, h)), path, "--out", out])
        if ok and os.path.exists(out):
            return _read(out)
    if path.lower().endswith(".png"):
        return _read(path)  # no converter: let the terminal scale the original
    return None


def image_info(path):
    """(width, height, frames) best effort."""
    m = magick()
    if m:
        ident = [m[0], "identify"] if m[0].endswith("magick") else [which("identify") or "identify"]
        ok, out = run(ident + ["-format", "%w %h\\n", path], timeout=20)
        if ok and out:
            lines = out.decode("utf-8", "replace").split()
            try:
                return int(lines[0]), int(lines[1]), max(1, len(lines) // 2)
            except (IndexError, ValueError):
                pass
    probe = which("ffprobe")
    if probe:
        ok, out = run([probe, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height,nb_frames", "-of", "json", path])
        if ok:
            try:
                st = json.loads(out.decode())["streams"][0]
                frames = int(st.get("nb_frames") or 1) if str(st.get("nb_frames", "")).isdigit() else 1
                return int(st["width"]), int(st["height"]), frames
            except (ValueError, KeyError, IndexError):
                pass
    data = _read_head(path, 32)
    if data and data[:8] == b"\x89PNG\r\n\x1a\n":
        import struct

        w, h = struct.unpack(">II", data[16:24])
        return w, h, 1
    return None, None, 1


def _read_head(path, n):
    try:
        with open(path, "rb") as fh:
            return fh.read(n)
    except OSError:
        return b""


def anim_frames(path, w, h, max_frames=120):
    """List of (png_bytes, delay_seconds) for GIF / animated WebP / APNG."""
    folder = cached("anim-%s" % key(path, w, h))
    delays_file = os.path.join(folder, "delays.json")
    if not os.path.exists(delays_file):
        os.makedirs(folder, exist_ok=True)
        m = magick()
        delays = []
        if m:
            ident = [m[0], "identify"] if m[0].endswith("magick") else [which("identify") or "identify"]
            ok, out = run(ident + ["-format", "%T\\n", path], timeout=30)
            if ok:
                delays = [max(int(x), 2) / 100.0 for x in out.decode().split() if x.isdigit()]
            ok, _ = run(m + [path, "-coalesce", "-resize", "%dx%d" % (w, h), os.path.join(folder, "f-%04d.png")], timeout=120)
        elif which("ffmpeg"):
            ok, _ = run([which("ffmpeg"), "-v", "error", "-i", path, "-vf", "scale=%d:%d:force_original_aspect_ratio=decrease" % (w, h), os.path.join(folder, "f-%04d.png")], timeout=120)
        files = sorted(glob.glob(os.path.join(folder, "f-*.png")))
        if not delays or len(delays) != len(files):
            delays = [0.1] * len(files)
        with open(delays_file, "w") as fh:
            json.dump(delays, fh)
    files = sorted(glob.glob(os.path.join(folder, "f-*.png")))
    try:
        with open(delays_file) as fh:
            delays = json.load(fh)
    except (OSError, ValueError):
        delays = [0.1] * len(files)
    step = max(1, (len(files) + max_frames - 1) // max_frames)
    frames = []
    for i in range(0, len(files), step):
        data = _read(files[i])
        if data:
            frames.append((data, sum(delays[i : i + step]) if i < len(delays) else 0.1))
    return frames


# --- documents --------------------------------------------------------------


def pdf_info(path):
    """(pages, (width_pt, height_pt))"""
    tool = which("pdfinfo")
    if tool:
        ok, out = run([tool, path], timeout=20)
        if ok:
            text = out.decode("utf-8", "replace")
            pages = re.search(r"^Pages:\s+(\d+)", text, re.M)
            size = re.search(r"^Page size:\s+([\d.]+) x ([\d.]+)", text, re.M)
            return (
                int(pages.group(1)) if pages else 1,
                (float(size.group(1)), float(size.group(2))) if size else (612.0, 792.0),
            )
    data = _read(path) or b""
    count = len(re.findall(rb"/Type\s*/Page[^s]", data))
    return max(count, 1), (612.0, 792.0)


def pdf_page_png(path, page, w, h):
    out_base = cached("pdf-%s" % key(path, page, w, h))
    out = out_base + ".png"
    if os.path.exists(out):
        return _read(out)
    tool = which("pdftoppm")
    if tool:
        ok, _ = run([tool, "-f", str(page), "-l", str(page), "-png", "-singlefile", "-scale-to-x", str(int(w)), "-scale-to-y", str(int(h)), path, out_base], timeout=60)
        if ok and os.path.exists(out):
            return _read(out)
    m = magick()
    if m:
        ok, _ = run(m + ["-density", "150", "%s[%d]" % (path, page - 1), "-background", "white", "-flatten", "-resize", "%dx%d" % (w, h), "png:" + out], timeout=60)
        if ok and os.path.exists(out):
            return _read(out)
    return None


def pdf_text(path, page):
    tool = which("pdftotext")
    if not tool:
        return None
    ok, out = run([tool, "-layout", "-f", str(page), "-l", str(page), path, "-"], timeout=30)
    return out.decode("utf-8", "replace") if ok else None


def office_pdf(path):
    out_dir = cached("office-%s" % key(path))
    target = os.path.join(out_dir, os.path.splitext(os.path.basename(path))[0] + ".pdf")
    if os.path.exists(target):
        return target
    tool = soffice()
    if not tool:
        return None
    os.makedirs(out_dir, exist_ok=True)
    profile = "file://" + cached("lo-profile")
    run([tool, "-env:UserInstallation=" + profile, "--headless", "--convert-to", "pdf", "--outdir", out_dir, path], timeout=180)
    return target if os.path.exists(target) else None


def html_png(path, w, h):
    out = cached("html-%s.png" % key(path, w, h))
    if os.path.exists(out):
        return _read(out)
    tool = chromium()
    if not tool:
        return None
    run(
        [
            tool,
            "--headless",
            "--disable-gpu",
            "--hide-scrollbars",
            "--no-first-run",
            "--host-resolver-rules=MAP * ~NOTFOUND",
            "--user-data-dir=" + cached("chromium-profile"),
            "--window-size=%d,%d" % (w, h),
            "--screenshot=" + out,
            "file://" + os.path.abspath(path),
        ],
        timeout=60,
    )
    return _read(out) if os.path.exists(out) else None


# --- audio / video ----------------------------------------------------------


def probe(path):
    tool = which("ffprobe")
    if not tool:
        return None
    ok, out = run([tool, "-v", "error", "-show_format", "-show_streams", "-of", "json", path], timeout=20)
    if not ok:
        return None
    try:
        return json.loads(out.decode("utf-8", "replace"))
    except ValueError:
        return None


def video_frame(path, t, w, h):
    tool = which("ffmpeg")
    if not tool:
        return None
    out = cached("vid-%s.png" % key(path, round(t, 2), w, h))
    if os.path.exists(out):
        return _read(out)
    ok, data = run(
        [tool, "-v", "error", "-ss", "%.3f" % t, "-i", path, "-frames:v", "1", "-vf", "scale=%d:%d:force_original_aspect_ratio=decrease" % (w, h), "-f", "image2pipe", "-vcodec", "png", "-"],
        timeout=30,
    )
    if ok and data:
        with open(out, "wb") as fh:
            fh.write(data)
        return data
    return None


def video_stream(path, t, w, h, fps):
    """A running ffmpeg emitting PNG frames on stdout, starting at t."""
    tool = which("ffmpeg")
    if not tool:
        return None
    return subprocess.Popen(
        [tool, "-v", "error", "-ss", "%.3f" % t, "-i", path, "-an", "-vf", "fps=%d,scale=%d:%d:force_original_aspect_ratio=decrease" % (fps, w, h), "-f", "image2pipe", "-vcodec", "png", "-"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )


def audio_wave(path, w, h):
    tool = which("ffmpeg")
    if not tool:
        return None
    out = cached("wave-%s.png" % key(path, w, h))
    if os.path.exists(out):
        return _read(out)
    ok, _ = run([tool, "-v", "error", "-i", path, "-filter_complex", "showwavespic=s=%dx%d:colors=#f0a070" % (w, h), "-frames:v", "1", out], timeout=60)
    return _read(out) if ok and os.path.exists(out) else None


def audio_player():
    for name in ("ffplay", "mpv", "afplay", "paplay", "aplay"):
        tool = which(name)
        if tool:
            if name == "ffplay":
                return [tool, "-nodisp", "-autoexit", "-loglevel", "quiet"]
            if name == "mpv":
                return [tool, "--no-video", "--really-quiet"]
            return [tool]
    return None


def file_description(path):
    tool = which("file")
    if not tool:
        return None, None
    ok, desc = run([tool, "-b", path], timeout=10)
    ok2, mime = run([tool, "-b", "--mime-type", path], timeout=10)
    return (
        desc.decode("utf-8", "replace").strip() if ok else None,
        mime.decode("utf-8", "replace").strip() if ok2 else None,
    )


TOOLS = [
    ("python3", "required", "everything"),
    ("magick", "images", "imagemagick"),
    ("vips", "images (alt)", "libvips"),
    ("ffmpeg", "gif / video / audio", "ffmpeg"),
    ("ffprobe", "video / audio info", "ffmpeg"),
    ("pdftoppm", "PDF pages", "poppler"),
    ("pdftotext", "PDF text", "poppler"),
    ("libreoffice", "Office docs", "libreoffice"),
    ("chromium", "HTML screenshots", "chromium"),
    ("bat", "code highlighting", "bat"),
    ("glow", "Markdown", "glow"),
    ("file", "unknown files", "file"),
]


def doctor():
    rows = []
    for name, use, pkg in TOOLS:
        if name == "magick":
            found = magick()
            found = found[0] if found else None
        elif name == "libreoffice":
            found = soffice()
        elif name == "chromium":
            found = chromium()
        else:
            found = which(name)
        rows.append((name, use, pkg, found))
    return rows
