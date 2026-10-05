#!/usr/bin/env python3
"""End-to-end test of peek on a connected machine (`herdr machine add`).

usage: uv run --with pyte python3 tests/e2e_remote.py <laptop-herdr> <devbox-herdr> [workdir]

Builds two isolated herdr installs on this computer and joins them over a
private, user-level sshd on 127.0.0.1 (its own host key, authorized key and
port; nothing in ~/.ssh is read or written):

  laptop  - the client you type into. Has peek's key bindings, NOT the plugin.
  devbox  - the "remote" server. Has peek installed and the files.

Then it types prefix+shift+f / prefix+f into the laptop client while the
devbox workspace is selected, and checks peek ran on the devbox and that its
image reached the laptop's terminal as kitty graphics.
"""

import getpass
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

from e2e_input import COLS, PREFIX, ROWS, Term, check, pane_ids, results, sgr, viewer_title, wait  # noqa: E402
from sandbox import Sandbox  # noqa: E402

PORT = 22000 + os.getpid() % 2000


def sh(*cmd):
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main():
    laptop_bin, devbox_bin = os.path.abspath(sys.argv[1]), os.path.abspath(sys.argv[2])
    work = os.path.abspath(sys.argv[3]) if len(sys.argv) > 3 else tempfile.mkdtemp(prefix="peek-remote-")
    if os.path.exists(work):
        shutil.rmtree(work)
    os.makedirs(work)
    demo = os.path.join(work, "demo")
    subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "make_demo.py"), demo], check=True, stdout=subprocess.DEVNULL)

    # --- devbox: herdr + peek, reachable through a private sshd -------------
    devbox = Sandbox(os.path.join(work, "devbox"), devbox_bin)
    devbox.setup()
    rbin = os.path.join(work, "devbox-bin")
    os.makedirs(rbin)
    os.symlink(devbox_bin, os.path.join(rbin, "herdr"))
    env_script = os.path.join(work, "devbox-login.sh")
    with open(env_script, "w") as fh:
        fh.write("#!/bin/sh\n")
        for key in ("HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "HERDR_SOCKET_PATH"):
            fh.write("export %s='%s'\n" % (key, devbox.env[key]))
        fh.write("export PATH='%s':\"$PATH\"\n" % rbin)
        fh.write('if [ -n "$SSH_ORIGINAL_COMMAND" ]; then exec /bin/sh -c "$SSH_ORIGINAL_COMMAND"; else exec /bin/bash -l; fi\n')
    os.chmod(env_script, 0o755)

    keys = os.path.join(work, "ssh")
    os.makedirs(keys, mode=0o700)
    sh("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", os.path.join(keys, "host"))
    sh("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", os.path.join(keys, "client"))
    shutil.copy(os.path.join(keys, "client.pub"), os.path.join(keys, "authorized_keys"))
    sshd_cfg = os.path.join(keys, "sshd_config")
    with open(sshd_cfg, "w") as fh:
        fh.write(
            "Port %d\nListenAddress 127.0.0.1\nHostKey %s\nAuthorizedKeysFile %s\nPidFile %s\n"
            "PasswordAuthentication no\nKbdInteractiveAuthentication no\nUsePAM no\nStrictModes no\n"
            "AllowUsers %s\nForceCommand %s\n"
            % (PORT, os.path.join(keys, "host"), os.path.join(keys, "authorized_keys"), os.path.join(keys, "sshd.pid"), getpass.getuser(), env_script)
        )
    sshd = subprocess.Popen(["/usr/bin/sshd", "-D", "-e", "-f", sshd_cfg], stdout=open(os.path.join(keys, "sshd.log"), "wb"), stderr=subprocess.STDOUT)

    # --- laptop: herdr with peek's keys but no plugin; `ssh` goes to the devbox
    wrap = os.path.join(work, "laptop-bin")
    os.makedirs(wrap)
    with open(os.path.join(wrap, "ssh"), "w") as fh:
        fh.write(
            "#!/bin/sh\nexec /usr/bin/ssh -p %d -i '%s' -o IdentitiesOnly=yes -o StrictHostKeyChecking=no "
            "-o UserKnownHostsFile='%s' -o BatchMode=yes \"$@\"\n" % (PORT, os.path.join(keys, "client"), os.path.join(keys, "known_hosts"))
        )
    os.chmod(os.path.join(wrap, "ssh"), 0o755)
    laptop = Sandbox(os.path.join(work, "laptop"), laptop_bin, extra_path=wrap)
    laptop.setup(link=False)

    term = Term()
    try:
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                socket.create_connection(("127.0.0.1", PORT), 0.5).close()
                break
            except OSError:
                time.sleep(0.2)
        out = subprocess.run([os.path.join(wrap, "ssh"), "127.0.0.1", "herdr --version; echo $HOME"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=20).stdout.decode()
        check("ssh reaches the isolated devbox (%s)" % out.split("\n")[0], devbox.home in out, out)

        devbox.start_server()
        devbox.cli("workspace", "create", "--cwd", demo, "--label", "web-app", "--focus", check=True)
        devbox.wait(lambda: len(devbox.panes()) >= 1, 10, "devbox pane")
        agent = devbox.panes()[0]["pane_id"]
        devbox.cli("pane", "run", agent, "python3 %s" % os.path.join(ROOT, "scripts", "fake_agent.py"), check=True)

        laptop.start_server()
        check("laptop has no plugins installed", "peek" not in laptop.cli("plugin", "list")[1], laptop.cli("plugin", "list")[1])
        code, out, err = laptop.cli("machine", "add", "--label", "devbox", "127.0.0.1", timeout=90)
        check("herdr machine add devbox", code == 0, (out + err)[-600:])
        term.screen.reply = lambda data: laptop.type(data)
        laptop.attach_client(COLS, ROWS, on_output=term.feed)

        # pick the devbox's workspace in the sidebar
        row = wait(lambda: term.find("devbox"), 20)
        check("laptop sidebar lists the devbox", row, "\n".join(l.rstrip() for l in term.lines()[:20]))
        if row:
            x, y = row
            wait(lambda: term.find("web-app"), 10)
            target = term.find("web-app") or (x, y)
            laptop.type(sgr(0, target[0], target[1]))
            laptop.type(sgr(0, target[0], target[1], release=True))
        check("laptop client shows the devbox agent pane", wait(lambda: term.find("shots/login-dark.png"), 20), "\n".join(l.rstrip()[:120] for l in term.lines()[:25]))

        before = set(pane_ids(devbox))
        del laptop.client_out[:]
        laptop.type(PREFIX)
        time.sleep(0.25)
        laptop.type("F")
        check("prefix+shift+f on the laptop opens the newest devbox file", wait(lambda: viewer_title(term) == "login-dark.png", 30), str(viewer_title(term)))
        check("the viewer split was created on the devbox", wait(lambda: len(pane_ids(devbox)) == len(before) + 1), str(pane_ids(devbox)))
        check("devbox's image reached the laptop terminal as kitty graphics", wait(lambda: b"\x1b_G" in laptop.client_out, 15), "%d bytes" % len(laptop.client_out))
        logs = devbox.cli("plugin", "log", "list", "--plugin", "peek")[1]
        check("peek.last ran on the devbox (devbox plugin log)", '"action_id":"last"' in logs.replace(" ", ""), logs[:300])

        laptop.type("q")
        wait(lambda: len(pane_ids(devbox)) == len(before), 10)
        laptop.type(PREFIX)
        time.sleep(0.25)
        laptop.type("f")
        # the hint badge sits just before the path (the footer row can fall outside
        # the visible area when a newer client drives an older server)
        def badge():
            pos = term.find("shots/flow.mp4")
            if not pos:
                return None
            import re

            found = re.findall(r"[a-z]{1,2}", term.lines()[pos[1]][: pos[0]])
            return found[-1] if found else None

        letter = wait(badge, 20)
        check("prefix+f on the laptop shows hints over the devbox pane", letter, "\n".join(l.rstrip()[:150] for l in term.lines() if l.strip()))
        if letter:
            laptop.type(letter)
        check("typing its letter opens flow.mp4 from the devbox", wait(lambda: viewer_title(term) == "flow.mp4", 30), str(viewer_title(term)))
    finally:
        laptop.stop()
        devbox.stop()
        sshd.terminate()
    failed = [r for r in results if not r[1]]
    print("\nremote (laptop %s → devbox %s): %d passed, %d failed" % (laptop.cli("--version")[1].strip(), devbox.cli("--version")[1].strip(), len(results) - len(failed), len(failed)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
