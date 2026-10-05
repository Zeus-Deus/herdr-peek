"""Plugin state: what the viewer should show, and where the viewer lives."""

import json
import os
import re
import time


def state_dir():
    path = os.environ.get("HERDR_PLUGIN_STATE_DIR") or os.path.join(
        os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "herdr-peek"
    )
    os.makedirs(path, exist_ok=True)
    return path


def cache_dir():
    path = os.path.join(state_dir(), "cache")
    os.makedirs(path, exist_ok=True)
    return path


def _key(tab_id):
    return re.sub(r"[^A-Za-z0-9_-]", "_", tab_id or "default")


def current_file(tab_id):
    return os.path.join(state_dir(), "current-%s.json" % _key(tab_id))


def viewer_file(tab_id):
    return os.path.join(state_dir(), "viewer-%s.json" % _key(tab_id))


def write_json(path, data):
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)


def read_json(path):
    try:
        with open(path) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


STARTED = time.time()


def set_current(tab_id, items, index, source_pane=None, started=None):
    """items: list of {"path": str, "line": int|None}.

    `started` is when the invocation began; a slower, older invocation never
    overwrites what a newer one already asked for.
    """
    started = STARTED if started is None else started
    existing = read_json(current_file(tab_id)) or {}
    if (existing.get("started") or 0) > started:
        return existing
    data = {
        "seq": time.time(),
        "started": started,
        "items": items,
        "index": max(0, min(index, len(items) - 1)),
        "source_pane": source_pane,
    }
    write_json(current_file(tab_id), data)
    return data


def load_config():
    """Tiny `key = value` reader for $HERDR_PLUGIN_CONFIG_DIR/config.toml."""
    cfg = {
        "placement": "split",
        "direction": "right",
        "alphabet": "asdfghjklqwertyuiopzxcvbnm",
        "video_fps": 8,
    }
    base = os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
    if not base:
        return cfg
    try:
        with open(os.path.join(base, "config.toml")) as fh:
            lines = fh.read().splitlines()
    except OSError:
        return cfg
    for line in lines:
        line = line.split("#", 1)[0].strip()
        if "=" not in line:
            continue
        key, value = [p.strip() for p in line.split("=", 1)]
        if value[:1] in ("'", '"'):
            value = value[1:-1]
        elif value.lower() in ("true", "false"):
            value = value.lower() == "true"
        else:
            try:
                value = int(value)
            except ValueError:
                pass
        cfg[key] = value
    return cfg
