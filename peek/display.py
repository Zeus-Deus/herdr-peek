"""Can the terminal you're looking at draw images? If not, use a normal window.

herdr answers kitty graphics queries itself, so a pane can't ask the real
terminal. Instead we find the herdr client attached on this machine and look
at the terminal app it runs in. When the client is elsewhere (a devbox you
reach with `herdr machine add`) we can't see it and assume images work.
"""

import os
import subprocess
import sys

# terminals that show kitty graphics inside herdr
GRAPHICS = {"ghostty", "kitty", "wezterm", "wezterm-gui", "konsole", "wayst"}
# terminals that don't (foot only does sixel, which herdr doesn't use)
NO_GRAPHICS = {
    "foot", "footclient", "alacritty", "xterm", "urxvt", "rxvt", "st", "tilix",
    "terminator", "xfce4-terminal", "gnome-terminal-", "gnome-terminal-server",
    "kgx", "ptyxis", "terminal", "iterm2", "kitten-ssh",
}
# stop walking up at these: the terminal is on another machine / unknown
OPAQUE = {"sshd", "sshd-session", "tmux", "tmux: server", "screen", "mosh-server", "systemd", "init", "launchd"}

VISUAL_KINDS = {"image", "anim", "video", "pdf", "office", "html"}


def _proc_info(pid):
    """(ppid, name, start_time) for a pid."""
    try:
        with open("/proc/%d/stat" % pid) as fh:
            stat = fh.read()
        name = stat[stat.index("(") + 1 : stat.rindex(")")]
        rest = stat[stat.rindex(")") + 2 :].split()
        return int(rest[1]), name, int(rest[19])
    except (OSError, ValueError, IndexError):
        return None


def _ps_table():
    """pid -> (ppid, name, start) via ps, for macOS."""
    try:
        out = subprocess.run(["ps", "-axo", "pid=,ppid=,etimes=,comm="], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5).stdout.decode()
    except (OSError, subprocess.TimeoutExpired):
        return {}
    table = {}
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) == 4 and parts[0].isdigit():
            name = os.path.basename(parts[3].strip())
            table[int(parts[0])] = (int(parts[1]), name, -int(parts[2]) if parts[2].isdigit() else 0)
    return table


def _cmdline(pid):
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as fh:
            return [a.decode("utf-8", "replace") for a in fh.read().split(b"\0") if a]
    except OSError:
        return []


def _environ(pid):
    try:
        with open("/proc/%d/environ" % pid, "rb") as fh:
            pairs = [p.split(b"=", 1) for p in fh.read().split(b"\0") if b"=" in p]
        return {k.decode("utf-8", "replace"): v.decode("utf-8", "replace") for k, v in pairs}
    except OSError:
        return None


def _client_socket(env):
    """The server socket a client process connects to (default session)."""
    if env.get("HERDR_SOCKET_PATH"):
        return env["HERDR_SOCKET_PATH"]
    base = env.get("XDG_CONFIG_HOME") or os.path.join(env.get("HOME", ""), ".config")
    return os.path.join(base, "herdr", "herdr.sock")


def _is_client(argv):
    """`herdr`, `herdr --session x`, `herdr session attach x` – not the server or CLI calls."""
    if not argv or not os.path.basename(argv[0]).startswith("herdr"):
        return False
    if len(argv) == 1 or argv[1].startswith("-"):
        return "--remote" not in argv
    return argv[1:3] == ["session", "attach"]


def client_terminal():
    """Name of the terminal app showing the newest local herdr client, or None."""
    if os.path.isdir("/proc"):
        ours = os.environ.get("HERDR_SOCKET_PATH")
        clients = []
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            pid = int(entry)
            info = _proc_info(pid)
            if not (info and info[1].startswith("herdr")):
                continue
            argv = _cmdline(pid)
            if not _is_client(argv):
                continue
            if ours:
                # only clients attached to the server this plugin runs under
                env = _environ(pid)
                if env is None or "--session" in argv or "session" in argv[1:2]:
                    continue
                if os.path.realpath(_client_socket(env)) != os.path.realpath(ours):
                    continue
            clients.append((info[2], pid))
        lookup = _proc_info
    else:
        table = _ps_table()
        clients = [(v[2], pid) for pid, v in table.items() if v[1] == "herdr"]
        lookup = table.get
    for _start, pid in sorted(clients, reverse=True):
        cur = pid
        for _ in range(8):
            info = lookup(cur)
            if not info:
                break
            parent = info[0]
            pinfo = lookup(parent)
            if not pinfo:
                break
            name = pinfo[1].lower()
            if name in GRAPHICS or name in NO_GRAPHICS or name.startswith("gnome-terminal"):
                return name
            if name in OPAQUE or parent <= 1:
                break
            cur = parent
        return None
    return None


def inline_images(config):
    """True when images should be drawn in the herdr pane."""
    mode = str(config.get("images") or "auto").lower()
    if mode == "inline":
        return True
    if mode == "external":
        return False
    term = client_terminal()
    return not (term and (term in NO_GRAPHICS or term.startswith("gnome-terminal")))


def can_open_external():
    if sys.platform == "darwin":
        return True
    return bool(os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY"))


def open_external(path):
    """Open with the desktop's default app (imv, mpv, Preview, …), detached."""
    cmd = ["open", path] if sys.platform == "darwin" else ["xdg-open", path]
    try:
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
        return True
    except OSError:
        return False
