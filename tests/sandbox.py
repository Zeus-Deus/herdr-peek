"""An isolated herdr install (own HOME / XDG dirs / socket) for end-to-end tests.

Nothing here touches the real ~/.config/herdr or a running herdr session.
"""

import json
import os
import shutil
import subprocess
import time

PLUGIN_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

KEYS = """
[[keys.command]]
key = "prefix+f"
type = "plugin_action"
command = "peek.pick"
description = "peek: pick a file on screen"

[[keys.command]]
key = "prefix+shift+f"
type = "plugin_action"
command = "peek.last"
description = "peek: open newest file"
"""


class Sandbox(object):
    def __init__(self, root, herdr_bin, extra_path=None):
        self.extra_path = extra_path
        self.root = os.path.abspath(root)
        self.bin = os.path.abspath(herdr_bin)
        self.home = os.path.join(self.root, "home")
        self.server = None

    @property
    def env(self):
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith("HERDR_") and k not in ("TMUX", "ZELLIJ")
        }
        env.update(
            HOME=self.home,
            XDG_CONFIG_HOME=os.path.join(self.home, ".config"),
            XDG_DATA_HOME=os.path.join(self.home, ".local", "share"),
            XDG_STATE_HOME=os.path.join(self.home, ".local", "state"),
            XDG_CACHE_HOME=os.path.join(self.home, ".cache"),
            SHELL="/bin/bash",
            TERM="xterm-256color",
            HERDR_SOCKET_PATH=self.socket,
        )
        if self.extra_path:
            env["PATH"] = self.extra_path + os.pathsep + env.get("PATH", "")
        return env

    @property
    def socket(self):
        # sun_path is ~108 bytes, so keep the socket out of deep temp dirs
        import hashlib

        tag = hashlib.sha1(self.root.encode()).hexdigest()[:8]
        base = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
        return os.path.join(base, "peek-sb-%s.sock" % tag)

    def setup(self, fresh=True, link=True):
        if fresh and os.path.exists(self.root):
            self.stop()
            shutil.rmtree(self.root)
        cfg_dir = os.path.join(self.home, ".config", "herdr")
        os.makedirs(cfg_dir, exist_ok=True)
        with open(os.path.join(cfg_dir, "config.toml"), "w") as fh:
            # copy_on_select = false keeps a mouse selection alive for prefix+f
            fh.write("onboarding = false\n\n[ui]\ncopy_on_select = false\n" + KEYS)
        with open(os.path.join(self.home, ".bashrc"), "w") as fh:
            fh.write("PS1='\\[\\e[38;2;240;160;112m\\]❯\\[\\e[0m\\] '\nexport HISTFILE=/dev/null\n")
        with open(os.path.join(self.home, ".bash_profile"), "w") as fh:
            fh.write(". ~/.bashrc\n")
        if link:
            self.cli("plugin", "link", PLUGIN_ROOT, check=True)

    def cli(self, *args, check=False, timeout=20, input=None):
        proc = subprocess.run(
            [self.bin] + [str(a) for a in args],
            env=self.env,
            input=input,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
        out = proc.stdout.decode("utf-8", "replace")
        if check and proc.returncode != 0:
            raise RuntimeError("herdr %s failed: %s%s" % (" ".join(map(str, args)), out, proc.stderr.decode()))
        return proc.returncode, out, proc.stderr.decode("utf-8", "replace")

    def json(self, *args):
        code, out, err = self.cli(*args)
        if code != 0:
            raise RuntimeError("herdr %s: %s %s" % (" ".join(args), out, err))
        return json.loads(out)

    def start_server(self):
        log = open(os.path.join(self.root, "server.log"), "ab")
        self.server = subprocess.Popen([self.bin, "server"], env=self.env, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
        self.wait(lambda: self.cli("pane", "list")[0] == 0, 15, "server socket")

    def type(self, data):
        """Send raw bytes to the attached client, as if typed in its terminal."""
        if isinstance(data, str):
            data = data.encode("utf-8")
        os.write(self.client_fd, data)

    def attach_client(self, cols=200, rows=50, cell=(10, 20), on_output=None, via=None):
        """Run an interactive `herdr` client in a private pty (no window needed)."""
        import fcntl
        import pty
        import struct
        import termios
        import threading

        pid, fd = pty.fork()
        if pid == 0:
            if via:  # e.g. a launcher named `foot`, so the client's parent looks like that terminal
                os.execve(via, [via, self.bin], self.env)
            os.execve(self.bin, [self.bin], self.env)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, cols * cell[0], rows * cell[1]))
        self.client_pid, self.client_fd = pid, fd
        self.client_out = bytearray()

        def drain():
            while True:
                try:
                    data = os.read(fd, 65536)
                except OSError:
                    return
                if not data:
                    return
                self.client_out += data
                del self.client_out[: max(0, len(self.client_out) - 4 * 1024 * 1024)]
                if on_output:
                    on_output(data)

        threading.Thread(target=drain, daemon=True).start()
        return pid

    def detach_client(self):
        pid = getattr(self, "client_pid", None)
        if pid:
            import signal

            try:
                os.kill(pid, signal.SIGTERM)
                os.waitpid(pid, 0)
            except OSError:
                pass
            self.client_pid = None

    def stop(self):
        self.detach_client()
        if os.path.exists(self.socket):
            try:
                self.cli("server", "stop", timeout=10)
            except Exception:
                pass
        if self.server and self.server.poll() is None:
            self.server.terminate()
            try:
                self.server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.server.kill()

    @staticmethod
    def wait(pred, timeout, what):
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            try:
                if pred():
                    return True
            except Exception as exc:  # retried until the deadline
                last = exc
            time.sleep(0.2)
        raise AssertionError("timed out waiting for %s (%s)" % (what, last))

    def panes(self):
        res = self.json("pane", "list")["result"]
        return res.get("panes", res if isinstance(res, list) else [])

    def read(self, pane_id, source="visible"):
        return self.cli("pane", "read", pane_id, "--source", source, "--format", "text")[1]

    def invoke(self, action, context=None):
        """plugin.action.invoke over the raw socket, so we can pass selected_text etc."""
        import socket as _socket

        req = {"id": "e2e", "method": "plugin.action.invoke", "params": {"action_id": action, "context": context or {"invocation_source": "keybinding"}}}
        s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        s.connect(self.socket)
        s.sendall((json.dumps(req) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
        s.close()
        return json.loads(buf.decode())

    def raw(self, method, params):
        import socket as _socket

        s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        s.connect(self.socket)
        s.sendall((json.dumps({"id": "e2e", "method": method, "params": params}) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
        s.close()
        return json.loads(buf.decode())
