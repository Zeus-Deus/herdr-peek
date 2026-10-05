"""Decide which viewer a path gets."""

import os

IMAGE = {"png", "jpg", "jpeg", "webp", "heic", "heif", "tif", "tiff", "bmp", "svg", "svgz", "avif", "ico", "jxl", "ppm", "pgm", "pbm", "qoi", "tga", "psd"}
ANIM = {"gif", "apng"}
VIDEO = {"mp4", "mov", "webm", "mkv", "avi", "m4v", "mpg", "mpeg", "wmv", "flv", "3gp", "ogv"}
AUDIO = {"mp3", "wav", "flac", "ogg", "oga", "m4a", "opus", "aac", "aif", "aiff", "wma"}
OFFICE = {"docx", "doc", "xlsx", "xls", "pptx", "ppt", "odt", "ods", "odp", "rtf", "key", "pages", "numbers"}
MARKDOWN = {"md", "markdown", "mdx", "mkd"}
HTML = {"html", "htm", "xhtml"}
CSV = {"csv", "tsv"}
JSON = {"json", "geojson", "jsonc", "webmanifest"}
JSONL = {"jsonl", "ndjson"}
SQLITE = {"db", "sqlite", "sqlite3", "db3"}
ARCHIVE_SUFFIXES = (".zip", ".jar", ".whl", ".apk", ".tar", ".tgz", ".tar.gz", ".tbz2", ".tar.bz2", ".txz", ".tar.xz", ".epub")


def ext(path):
    return os.path.splitext(path)[1].lower().lstrip(".")


def head(path, n=8192):
    try:
        with open(path, "rb") as fh:
            return fh.read(n)
    except OSError:
        return b""


def is_text(data):
    if b"\0" in data:
        return False
    try:
        data.decode("utf-8")
        return True
    except UnicodeDecodeError as exc:
        # a multi-byte char cut at the end of the sample is fine
        return exc.start >= len(data) - 4


def is_animated_webp(path):
    data = head(path, 64 * 1024)
    return data[:4] == b"RIFF" and data[8:12] == b"WEBP" and b"ANIM" in data


def classify(path):
    if os.path.isdir(path):
        return "dir"
    e = ext(path)
    lower = path.lower()
    data = head(path, 8192)
    if e == "webp" and is_animated_webp(path):
        return "anim"
    if e in ANIM:
        return "anim"
    if e in IMAGE:
        return "image"
    if e in VIDEO:
        return "video"
    if e in AUDIO:
        return "audio"
    if e == "pdf" or data[:5] == b"%PDF-":
        return "pdf"
    if e in OFFICE:
        return "office"
    if lower.endswith(ARCHIVE_SUFFIXES):
        return "archive"
    if data[:16] == b"SQLite format 3\0" or (e in SQLITE and data[:16] == b"SQLite format 3\0"):
        return "sqlite"
    if e == "ipynb":
        return "notebook"
    if e in MARKDOWN:
        return "markdown"
    if e in HTML:
        return "html"
    if e in CSV:
        return "csv"
    if e in JSONL:
        return "jsonl"
    if e in JSON:
        return "json"
    # sniff by magic bytes for files without a useful extension
    if data[:8] == b"\x89PNG\r\n\x1a\n" or data[:3] == b"\xff\xd8\xff":
        return "image"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "anim"
    if data[:4] == b"PK\x03\x04":
        return "archive"
    if is_text(data):
        return "text"
    return "binary"
