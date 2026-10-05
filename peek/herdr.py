"""Thin wrappers around the herdr CLI (the plugin API)."""

import json
import os
import subprocess


def bin_path():
    return os.environ.get("HERDR_BIN_PATH") or "herdr"


def run(args, timeout=10, raw=False):
    """Run `herdr <args>`; return (ok, parsed-json-or-text)."""
    try:
        proc = subprocess.run(
            [bin_path()] + list(args),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    out = proc.stdout.decode("utf-8", "replace")
    if proc.returncode != 0:
        return False, (out + proc.stderr.decode("utf-8", "replace")).strip()
    if raw:
        return True, out
    try:
        return True, json.loads(out)
    except ValueError:
        return True, out


def result(payload):
    if isinstance(payload, dict):
        return payload.get("result", payload)
    return {}


def context():
    raw = os.environ.get("HERDR_PLUGIN_CONTEXT_JSON") or "{}"
    try:
        ctx = json.loads(raw)
    except ValueError:
        ctx = {}
    return ctx if isinstance(ctx, dict) else {}


def pane_get(pane_id):
    if not pane_id:
        return None
    ok, payload = run(["pane", "get", pane_id])
    if not ok:
        return None
    return result(payload).get("pane")


def pane_read(pane_id, source="visible", lines=None):
    args = ["pane", "read", pane_id, "--source", source, "--format", "text"]
    if lines:
        args += ["--lines", str(lines)]
    ok, out = run(args, raw=True)
    return out if ok else ""


def find_pane_id(payload):
    """Dig the opened pane id out of a plugin.pane.open response."""
    if isinstance(payload, dict):
        for key in ("pane_id",):
            if isinstance(payload.get(key), str):
                return payload[key]
        for value in payload.values():
            found = find_pane_id(value)
            if found:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = find_pane_id(value)
            if found:
                return found
    return None


def open_plugin_pane(entrypoint, target_pane=None, placement=None, direction=None, env=None, focus=True):
    plugin_id = os.environ.get("HERDR_PLUGIN_ID") or "peek"
    args = ["plugin", "pane", "open", "--plugin", plugin_id, "--entrypoint", entrypoint]
    if placement:
        args += ["--placement", placement]
    if target_pane:
        args += ["--target-pane", target_pane]
    if direction:
        args += ["--direction", direction]
    for key, value in (env or {}).items():
        args += ["--env", "%s=%s" % (key, value)]
    args.append("--focus" if focus else "--no-focus")
    ok, payload = run(args)
    return ok, payload


def focus_plugin_pane(pane_id):
    ok, _ = run(["plugin", "pane", "focus", pane_id])
    return ok


def close_plugin_pane(pane_id):
    ok, _ = run(["plugin", "pane", "close", pane_id])
    return ok


def notify(title, body=None):
    args = ["notification", "show", title]
    if body:
        args += ["--body", body]
    run(args, timeout=5)
