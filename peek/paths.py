"""Find file paths that exist in a block of terminal text.

Works on plain text rows (as returned by `herdr pane read --source visible`).
Each hit records where it sits on screen so the picker can draw a hint there.
"""

import os
import re
import unicodedata
from urllib.parse import unquote, urlparse

# A raw token: anything up to whitespace or common delimiters. Backslash-escaped
# spaces stay inside the token ("my\ file.png").
TOKEN_RE = re.compile(r"(?:\\ |[^\s\"'`<>()\[\]{}|;,])+")
QUOTED_RE = re.compile(r"([\"'`])([^\"'`\n]{1,400}?)\1")
FILE_URL_RE = re.compile(r"file://[^\s\"'<>`]+")
LINE_SUFFIX_RE = re.compile(r"^(.*?)(?::(\d+))(?::(\d+))?:?$")
HAS_EXT_RE = re.compile(r"\.[A-Za-z0-9][A-Za-z0-9_+-]{0,9}$")

LEAD_STRIP = "([{<'\"`*•●▸>→⎿"
TRAIL_STRIP = ".,;:!?)]}>'\"`*…"


class Hit(object):
    __slots__ = ("path", "line", "row", "col", "width", "text", "is_dir")

    def __init__(self, path, line, row, col, width, text, is_dir):
        self.path = path
        self.line = line
        self.row = row
        self.col = col
        self.width = width
        self.text = text
        self.is_dir = is_dir

    def as_dict(self):
        return {k: getattr(self, k) for k in self.__slots__}

    def __repr__(self):
        return "Hit(%r, line=%r, row=%r, col=%r)" % (self.path, self.line, self.row, self.col)


def char_width(ch):
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def display_width(text):
    return sum(char_width(c) for c in text)


def _looks_like_path(token):
    if len(token) < 2 or "://" in token:
        return False
    if token.startswith(("~/", "/", "./", "../")):
        return True
    if "/" in token:
        return True
    return bool(HAS_EXT_RE.search(token))


def _resolve(token, bases):
    """Return (abs_path, line) for a token that names an existing path."""
    line = None
    candidates = [(token, None)]
    m = LINE_SUFFIX_RE.match(token)
    if m and m.group(2):
        candidates.insert(0, (m.group(1), int(m.group(2))))
    for raw, ln in candidates:
        raw = raw.replace("\\ ", " ")
        variants = [raw]
        if raw.startswith("@"):
            variants.append(raw[1:])
        if raw.startswith(("a/", "b/")):
            variants.append(raw[2:])
        for v in variants:
            if not v or v in (".", "..", "~"):
                continue
            v = os.path.expanduser(v)
            if os.path.isabs(v):
                options = [v]
            else:
                options = [os.path.join(b, v) for b in bases if b]
            for opt in options:
                try:
                    if os.path.exists(opt):
                        return os.path.normpath(opt), ln if ln is not None else line
                except (OSError, ValueError):
                    continue
    return None, None


def _clean(token):
    start = 0
    end = len(token)
    while start < end and token[start] in LEAD_STRIP:
        start += 1
    while end > start and token[end - 1] in TRAIL_STRIP:
        end -= 1
    return start, token[start:end]


def _col_of(row_text, index):
    return display_width(row_text[:index])


def _rows_to_logical(rows, width):
    """Join rows that were hard-wrapped at the pane edge.

    Returns a list of (text, segments) where segments maps logical offsets back
    to (row, start_index_in_row).
    """
    logical = []
    i = 0
    while i < len(rows):
        text = rows[i]
        segs = [(0, i)]
        while width and display_width(rows[i]) >= width and i + 1 < len(rows) and rows[i + 1][:1] not in ("", " "):
            i += 1
            segs.append((len(text), i))
            text += rows[i]
        logical.append((text, segs))
        i += 1
    return logical


def _locate(segs, offset):
    row = segs[0][1]
    base = 0
    for seg_off, seg_row in segs:
        if seg_off <= offset:
            row, base = seg_row, seg_off
    return row, offset - base


def find_paths(text, cwd=None, extra_bases=(), width=None):
    """Return Hits for every existing path in `text`, top to bottom."""
    rows = text.split("\n")
    if rows and rows[-1] == "":
        rows.pop()
    bases = [cwd] if cwd else [os.getcwd()]
    bases += [b for b in extra_bases if b and b not in bases]
    hits = []
    taken = {}  # row -> list of (start, end) char spans already claimed

    def claim(row, start, end):
        for s, e in taken.get(row, []):
            if start < e and s < end:
                return False
        taken.setdefault(row, []).append((start, end))
        return True

    # wrapped lines first (they hold paths cut at the pane edge), then every
    # physical row so a false join never hides a path
    units = [u for u in _rows_to_logical(rows, width) if len(u[1]) > 1]
    units += [(row, [(0, i)]) for i, row in enumerate(rows)]
    for text_line, segs in units:
        spans = []
        for m in FILE_URL_RE.finditer(text_line):
            url = m.group(0).rstrip(TRAIL_STRIP)
            path = unquote(urlparse(url).path)
            if path and os.path.exists(path):
                spans.append((m.start(), m.start() + len(url), os.path.normpath(path), None))
        for m in QUOTED_RE.finditer(text_line):
            inner = m.group(2).strip()
            if " " not in inner:
                continue  # plain tokens are handled below
            path, line = _resolve(inner, bases)
            if path:
                spans.append((m.start(2), m.start(2) + len(m.group(2)), path, line))
        for m in TOKEN_RE.finditer(text_line):
            off, cleaned = _clean(m.group(0))
            suffix = LINE_SUFFIX_RE.match(cleaned)
            bare = suffix.group(1) if suffix and suffix.group(2) else cleaned
            if not _looks_like_path(bare):
                continue
            path, line = _resolve(cleaned, bases)
            if path:
                start = m.start() + off
                spans.append((start, start + len(cleaned), path, line))
        for start, end, path, line in spans:
            row, idx = _locate(segs, start)
            row_text = rows[row]
            if not claim(row, idx, idx + (end - start)):
                continue
            hits.append(
                Hit(
                    path=path,
                    line=line,
                    row=row,
                    col=_col_of(row_text, idx),
                    width=display_width(text_line[start:end]),
                    text=text_line[start:end],
                    is_dir=os.path.isdir(path),
                )
            )
    hits.sort(key=lambda h: (h.row, h.col))
    return hits


def newest(hits):
    """The bottom-most file on screen (what an agent most recently printed)."""
    files = [h for h in hits if not h.is_dir]
    pool = files or hits
    return pool[-1] if pool else None


def unique_paths(hits):
    seen = set()
    out = []
    for h in hits:
        if h.path not in seen:
            seen.add(h.path)
            out.append(h)
    return out


def path_from_selection(text, cwd=None, extra_bases=()):
    """Resolve a selected chunk of text to hits; fall back to the whole selection."""
    text = (text or "").strip()
    if not text:
        return []
    bases = [cwd] if cwd else [os.getcwd()]
    bases += list(extra_bases)
    if text.startswith("file://"):
        path = unquote(urlparse(text).path)
        if os.path.exists(path):
            return [Hit(os.path.normpath(path), None, 0, 0, len(text), text, os.path.isdir(path))]
    whole, line = _resolve(text.strip("'\"`"), bases)
    if whole:
        return [Hit(whole, line, 0, 0, len(text), text, os.path.isdir(whole))]
    return find_paths(text, cwd=cwd, extra_bases=extra_bases)


def assign_hints(hits, alphabet="asdfghjklqwertyuiopzxcvbnm"):
    """Give each distinct path a hint; repeated paths share one."""
    paths = []
    for h in hits:
        if h.path not in paths:
            paths.append(h.path)
    n = len(paths)
    if n <= len(alphabet):
        labels = list(alphabet[:n])
    else:
        # two-letter labels, never a prefix clash with single letters
        labels = [a + b for a in alphabet for b in alphabet][:n]
    return dict(zip(paths, labels))
