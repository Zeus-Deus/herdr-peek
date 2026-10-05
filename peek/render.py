"""Text renderers. Each returns a list of ANSI lines for a given width."""

import base64
import csv
import io
import json
import os
import re
import sqlite3
import stat
import tarfile
import textwrap
import time
import zipfile

import convert
from term import ACCENT, BLUE, BOLD, CYAN, DIM, GREEN, ITALIC, MAGENTA, MUTED, RED, RESET, UNDERLINE, YELLOW, bg, visible_len

MAX_TEXT = 4 * 1024 * 1024


def human(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit)).replace(".0 ", " ")
        n /= 1024.0
    return "%d B" % n


def read_text(path, limit=MAX_TEXT):
    with open(path, "rb") as fh:
        data = fh.read(limit)
    return data.decode("utf-8", "replace").replace("\r\n", "\n").expandtabs(4)


# --- code / plain text -----------------------------------------------------


def code(path, width, line=None, language=None):
    bat = convert.which("bat", "batcat")
    if bat:
        cmd = [bat, "--color=always", "--style=numbers", "--paging=never", "--wrap=character", "--terminal-width", str(width)]
        if line:
            cmd += ["--highlight-line", str(line)]
        if language:
            cmd += ["--language", language]
        ok, out = convert.run(cmd + [path], timeout=30)
        if ok:
            return out.decode("utf-8", "replace").rstrip("\n").split("\n"), None
    text = read_text(path)
    lines = text.rstrip("\n").split("\n")
    gutter = len(str(len(lines)))
    out = []
    for i, src in enumerate(lines, 1):
        num = str(i).rjust(gutter)
        style = (bg(60, 45, 35) if i == line else "")
        body_w = max(width - gutter - 3, 10)
        chunks = [src[j : j + body_w] for j in range(0, len(src), body_w)] or [""]
        for k, chunk in enumerate(chunks):
            prefix = (MUTED + num + RESET) if k == 0 else " " * gutter
            out.append("%s %s│%s %s%s%s" % (prefix, MUTED, RESET, style, chunk, RESET))
    return out, "install bat for syntax highlighting"


# --- markdown ------------------------------------------------------------------


def _inline(text):
    text = re.sub(r"`([^`]+)`", lambda m: bg(50, 50, 60) + YELLOW + m.group(1) + RESET, text)
    text = re.sub(r"\*\*([^*]+)\*\*|__([^_]+)__", lambda m: BOLD + (m.group(1) or m.group(2)) + RESET, text)
    text = re.sub(r"(?<![*\w])\*([^*\s][^*]*)\*(?!\*)|(?<![_\w])_([^_\s][^_]*)_(?!\w)", lambda m: ITALIC + (m.group(1) or m.group(2)) + RESET, text)
    text = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", lambda m: MAGENTA + "🖼 " + (m.group(1) or m.group(2)) + RESET, text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", lambda m: UNDERLINE + BLUE + m.group(1) + RESET + MUTED + " (" + m.group(2) + ")" + RESET, text)
    return text


def _md_table(rows, width):
    cells = [[c.strip() for c in r.strip().strip("|").split("|")] for r in rows]
    cells = [c for c in cells if not all(re.match(r"^:?-{2,}:?$", x) for x in c if x)]
    if not cells:
        return []
    ncol = max(len(r) for r in cells)
    for r in cells:
        r.extend([""] * (ncol - len(r)))
    widths = [max(len(r[i]) for r in cells) for i in range(ncol)]
    out = []
    for idx, r in enumerate(cells):
        line = " │ ".join(_inline(r[i].ljust(widths[i])) for i in range(ncol))
        out.append(("  " + BOLD + line + RESET) if idx == 0 else "  " + line)
        if idx == 0:
            out.append("  " + MUTED + "─┼─".join("─" * w for w in widths) + RESET)
    return out


def markdown(path, width):
    glow = convert.which("glow")
    if glow:
        ok, out = convert.run([glow, "-s", "dark", "-w", str(width), path], timeout=30)
        if ok and out.strip():
            return out.decode("utf-8", "replace").rstrip("\n").split("\n"), None
    src = read_text(path).split("\n")
    out = []
    i = 0
    wrap = max(width - 2, 20)
    in_code = False
    fence_lang = ""
    while i < len(src):
        line = src[i]
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_code = not in_code
            fence_lang = stripped[3:].strip()
            if in_code:
                out.append(MUTED + "  ┌─ " + (fence_lang or "code") + RESET)
            else:
                out.append(MUTED + "  └─" + RESET)
            i += 1
            continue
        if in_code:
            out.append(MUTED + "  │ " + RESET + GREEN + line + RESET)
            i += 1
            continue
        if "|" in stripped and i + 1 < len(src) and re.match(r"^\s*\|?\s*:?-{2,}", src[i + 1]):
            block = []
            while i < len(src) and "|" in src[i]:
                block.append(src[i])
                i += 1
            out.extend(_md_table(block, width))
            out.append("")
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if m:
            level = len(m.group(1))
            title = _inline(m.group(2))
            if level == 1:
                out.append(BOLD + ACCENT + title.upper() + RESET)
                out.append(ACCENT + "━" * min(visible_len(title) + 2, width) + RESET)
            elif level == 2:
                out.append(BOLD + ACCENT + title + RESET)
                out.append(MUTED + "─" * min(visible_len(title) + 2, width) + RESET)
            else:
                out.append(BOLD + "#" * level + " " + title + RESET)
            i += 1
            continue
        if re.match(r"^(-{3,}|\*{3,}|_{3,})$", stripped):
            out.append(MUTED + "─" * width + RESET)
            i += 1
            continue
        m = re.match(r"^(\s*)([-*+]|\d+[.)])\s+(\[[ xX]\]\s+)?(.*)$", line)
        if m:
            indent = len(m.group(1).expandtabs(4))
            bullet = m.group(2)
            box = m.group(3)
            mark = "•" if bullet in "-*+" else bullet
            if box:
                mark = (GREEN + "[x]" + RESET) if box.strip().lower() == "[x]" else (MUTED + "[ ]" + RESET)
            body = textwrap.wrap(m.group(4), max(wrap - indent - 4, 10)) or [""]
            out.append(" " * (indent + 2) + ACCENT + mark + RESET + " " + _inline(body[0]))
            for rest in body[1:]:
                out.append(" " * (indent + 4) + _inline(rest))
            i += 1
            continue
        if stripped.startswith(">"):
            body = textwrap.wrap(stripped.lstrip("> "), max(wrap - 4, 10)) or [""]
            for b in body:
                out.append(MUTED + "  ▌ " + RESET + ITALIC + _inline(b) + RESET)
            i += 1
            continue
        if not stripped:
            if out and out[-1] != "":
                out.append("")
            i += 1
            continue
        para = [stripped]
        i += 1
        while i < len(src) and src[i].strip() and not re.match(r"^\s*(#|[-*+]\s|\d+[.)]\s|>|```|~~~|\|)", src[i]):
            para.append(src[i].strip())
            i += 1
        for b in textwrap.wrap(" ".join(para), wrap):
            out.append(_inline(b))
    return out, "install glow for richer Markdown"


# --- tables ------------------------------------------------------------------


def _table(rows, width, max_col=40):
    if not rows:
        return [MUTED + "(empty)" + RESET]
    ncol = max(len(r) for r in rows)
    rows = [list(r) + [""] * (ncol - len(r)) for r in rows]
    widths = [min(max(len(str(r[i])) for r in rows), max_col) for i in range(ncol)]
    numeric = []
    for i in range(ncol):
        vals = [str(r[i]) for r in rows[1:] if str(r[i]).strip()]
        numeric.append(bool(vals) and all(re.match(r"^-?[\d,]*\.?\d+(e-?\d+)?%?$", v) for v in vals[:200]))

    def fmt(r, header=False):
        parts = []
        for i in range(ncol):
            val = str(r[i]).replace("\n", " ")
            if len(val) > widths[i]:
                val = val[: widths[i] - 1] + "…"
            val = val.rjust(widths[i]) if numeric[i] and not header else val.ljust(widths[i])
            parts.append(val)
        return (MUTED + " │ " + RESET).join(parts)

    out = [BOLD + ACCENT + fmt(rows[0], True) + RESET, MUTED + "─┼─".join("─" * w for w in widths) + RESET]
    out += [fmt(r) for r in rows[1:]]
    return out


def csv_table(path, width, max_rows=5000):
    text = read_text(path, 8 * 1024 * 1024)
    delim = "\t" if path.lower().endswith(".tsv") else ","
    if delim == ",":
        try:
            delim = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|").delimiter
        except csv.Error:
            pass
    rows = []
    for idx, row in enumerate(csv.reader(io.StringIO(text), delimiter=delim)):
        if idx > max_rows:
            break
        rows.append(row)
    lines = _table(rows, width)
    meta = "%d rows × %d cols" % (max(len(rows) - 1, 0), max((len(r) for r in rows), default=0))
    return lines, meta


# --- JSON -------------------------------------------------------------------


def _json_lines(value, indent=0, key=None, trailing=""):
    pad = "  " * indent
    label = (BLUE + json.dumps(key, ensure_ascii=False) + RESET + ": ") if key is not None else ""
    if isinstance(value, dict):
        if not value:
            return [pad + label + "{}" + trailing]
        lines = [pad + label + "{"]
        items = list(value.items())
        for n, (k, v) in enumerate(items):
            lines += _json_lines(v, indent + 1, k, "," if n < len(items) - 1 else "")
        lines.append(pad + "}" + trailing)
        return lines
    if isinstance(value, list):
        if not value:
            return [pad + label + "[]" + trailing]
        if all(not isinstance(v, (dict, list)) for v in value) and len(json.dumps(value)) < 80:
            return [pad + label + "[" + ", ".join(_scalar(v) for v in value) + "]" + trailing]
        lines = [pad + label + "["]
        for n, v in enumerate(value):
            lines += _json_lines(v, indent + 1, None, "," if n < len(value) - 1 else "")
        lines.append(pad + "]" + trailing)
        return lines
    return [pad + label + _scalar(value) + trailing]


def _scalar(v):
    if isinstance(v, str):
        return GREEN + json.dumps(v, ensure_ascii=False) + RESET
    if isinstance(v, bool) or v is None:
        return MAGENTA + json.dumps(v) + RESET
    return YELLOW + json.dumps(v) + RESET


def json_tree(path, width):
    try:
        data = json.loads(read_text(path, 32 * 1024 * 1024))
    except ValueError as exc:
        lines, _ = code(path, width)
        return [RED + "invalid JSON: %s" % exc + RESET, ""] + lines, None
    meta = None
    if isinstance(data, list):
        meta = "array · %d items" % len(data)
    elif isinstance(data, dict):
        meta = "object · %d keys" % len(data)
    return _json_lines(data), meta


def jsonl(path, width, max_records=2000):
    out = []
    count = 0
    with open(path, "rb") as fh:
        for n, raw in enumerate(fh, 1):
            raw = raw.strip()
            if not raw:
                continue
            count += 1
            if count > max_records:
                out.append(MUTED + "… more records not shown" + RESET)
                break
            out.append(MUTED + "── record %d " % n + "─" * max(width - 14, 0) + RESET)
            try:
                out += _json_lines(json.loads(raw.decode("utf-8", "replace")))
            except ValueError:
                out.append(RED + raw.decode("utf-8", "replace")[: width * 4] + RESET)
    return out, "%d records" % count


# --- notebooks -----------------------------------------------------------------


def notebook(path, width):
    nb = json.loads(read_text(path, 64 * 1024 * 1024))
    lang = ((nb.get("metadata") or {}).get("language_info") or {}).get("name") or "python"
    out = []
    images = []
    for n, cell in enumerate(nb.get("cells", []), 1):
        src = cell.get("source", "")
        src = "".join(src) if isinstance(src, list) else src
        kind = cell.get("cell_type", "code")
        if kind == "markdown":
            out.append(MUTED + "── markdown " + "─" * max(width - 12, 0) + RESET)
            out += [_inline(l) for l in src.split("\n")]
            out.append("")
            continue
        count = cell.get("execution_count")
        out.append(ACCENT + "In [%s]:" % (count if count is not None else " ") + RESET)
        for l in src.split("\n"):
            out.append(MUTED + "│ " + RESET + l)
        for output in cell.get("outputs", []) or []:
            otype = output.get("output_type")
            if otype == "stream":
                text = output.get("text", "")
                text = "".join(text) if isinstance(text, list) else text
                out += [DIM + l + RESET for l in text.rstrip("\n").split("\n")]
            elif otype in ("execute_result", "display_data"):
                data = output.get("data", {})
                if "image/png" in data:
                    raw = data["image/png"]
                    raw = "".join(raw) if isinstance(raw, list) else raw
                    target = convert.cached("nb-%s-%d.png" % (convert.key(path), len(images)))
                    with open(target, "wb") as fh:
                        fh.write(base64.b64decode(raw))
                    images.append(target)
                    out.append(MAGENTA + "🖼  output image %d · press i to view" % len(images) + RESET)
                elif "text/plain" in data:
                    text = data["text/plain"]
                    text = "".join(text) if isinstance(text, list) else text
                    prefix = ("Out[%s]: " % output.get("execution_count")) if otype == "execute_result" else ""
                    out += [(ACCENT + prefix + RESET + l) if k == 0 else l for k, l in enumerate(text.split("\n"))]
            elif otype == "error":
                out.append(RED + "%s: %s" % (output.get("ename"), output.get("evalue")) + RESET)
        out.append("")
    meta = "%d cells · %s" % (len(nb.get("cells", [])), lang)
    return out, meta, images


# --- sqlite ----------------------------------------------------------------------


def sqlite(path, width, rows_per_table=12):
    uri = "file:%s?mode=ro" % os.path.abspath(path)
    con = sqlite3.connect(uri, uri=True, timeout=1)
    try:
        cur = con.cursor()
        objects = cur.execute("select name, type from sqlite_master where type in ('table','view') and name not like 'sqlite_%' order by type, name").fetchall()
        out = []
        for name, kind in objects:
            q = '"%s"' % name.replace('"', '""')
            try:
                total = cur.execute("select count(*) from %s" % q).fetchone()[0]
            except sqlite3.Error:
                total = "?"
            cols = cur.execute("pragma table_info(%s)" % q).fetchall()
            out.append(BOLD + ACCENT + "▸ %s" % name + RESET + MUTED + "  %s · %s rows" % (kind, total) + RESET)
            out.append(MUTED + "  " + ", ".join("%s %s" % (c[1], c[2] or "") for c in cols) + RESET)
            try:
                cur.execute("select * from %s limit %d" % (q, rows_per_table))
                rows = [[d[0] for d in cur.description]] + [["" if v is None else (v if not isinstance(v, bytes) else "<%d bytes>" % len(v)) for v in r] for r in cur.fetchall()]
                out += ["  " + l for l in _table(rows, width - 2, max_col=28)]
            except sqlite3.Error as exc:
                out.append(RED + "  %s" % exc + RESET)
            out.append("")
        meta = "%d tables/views · read-only" % len(objects)
        return out or [MUTED + "(no tables)" + RESET], meta
    finally:
        con.close()


# --- archives / directories ------------------------------------------------------


def archive(path, width):
    entries = []
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zf:
            for info in zf.infolist():
                entries.append((info.filename, info.file_size, "%04d-%02d-%02d %02d:%02d" % info.date_time[:5]))
    else:
        with tarfile.open(path) as tf:
            for info in tf.getmembers():
                name = info.name + ("/" if info.isdir() else "")
                entries.append((name, info.size, time.strftime("%Y-%m-%d %H:%M", time.localtime(info.mtime))))
    total = sum(e[1] for e in entries)
    out = [BOLD + "%10s  %-16s  %s" % ("size", "modified", "name") + RESET]
    for name, size, mtime in entries:
        color = BLUE if name.endswith("/") else ""
        out.append("%s%10s%s  %s%s%s  %s%s%s" % (MUTED, human(size), RESET, MUTED, mtime, RESET, color, name, RESET))
    return out, "%d entries · %s uncompressed" % (len(entries), human(total))


def list_dir(path, show_hidden=False):
    try:
        names = os.listdir(path)
    except OSError as exc:
        return [], str(exc)
    entries = []
    for name in names:
        if not show_hidden and name.startswith("."):
            continue
        full = os.path.join(path, name)
        try:
            st = os.stat(full)
            is_dir = stat.S_ISDIR(st.st_mode)
            entries.append((not is_dir, name.lower(), name, full, is_dir, st.st_size, st.st_mtime))
        except OSError:
            entries.append((True, name.lower(), name, full, False, 0, 0))
    entries.sort()
    return [(e[2], e[3], e[4], e[5], e[6]) for e in entries], None


def dir_lines(entries, cursor, width):
    out = []
    for i, (name, _full, is_dir, size, mtime) in enumerate(entries):
        label = (BLUE + BOLD + name + "/" + RESET) if is_dir else name
        info = MUTED + ("%9s  %s" % ("" if is_dir else human(size), time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime)) if mtime else "")) + RESET
        line = "%s  %s" % (info, label)
        if i == cursor:
            line = bg(70, 50, 38) + ACCENT + "▸ " + RESET + bg(70, 50, 38) + line.replace(RESET, RESET + bg(70, 50, 38)) + RESET
        else:
            line = "  " + line
        out.append(line)
    return out


# --- fallback --------------------------------------------------------------------


def info_hex(path, width, limit=64 * 1024):
    desc, mime = convert.file_description(path)
    st = os.stat(path)
    out = [
        BOLD + "type     " + RESET + (desc or "unknown (install `file` for details)"),
        BOLD + "mime     " + RESET + (mime or "?"),
        BOLD + "size     " + RESET + "%s (%d bytes)" % (human(st.st_size), st.st_size),
        BOLD + "mode     " + RESET + stat.filemode(st.st_mode),
        BOLD + "modified " + RESET + time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)),
        "",
    ]
    per = 16 if width >= 78 else 8
    with open(path, "rb") as fh:
        data = fh.read(limit)
    for off in range(0, len(data), per):
        chunk = data[off : off + per]
        hexpart = " ".join("%02x" % b for b in chunk).ljust(per * 3 - 1)
        asc = "".join(chr(b) if 32 <= b < 127 else "·" for b in chunk)
        out.append("%s%08x%s  %s  %s%s%s" % (MUTED, off, RESET, hexpart, CYAN, asc, RESET))
    if st.st_size > limit:
        out.append(MUTED + "… %s more" % human(st.st_size - limit) + RESET)
    return out, mime
