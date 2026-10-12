"""Real private Herdr endpoint + synthetic foreground desktop (no windows)."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from sandbox import Sandbox

ROOT = Path(__file__).resolve().parents[1]
MAIN = str(ROOT / 'peek/main.py')


class PrivateSandbox(Sandbox):
    @property
    def socket(self):
        return str(Path(self.root, 'api.sock'))
    @property
    def env(self):
        return dict(super().env, HERDR_CLIENT_SOCKET_PATH=str(Path(self.root, 'client.sock')),
                    XDG_RUNTIME_DIR=self.root)
    def start_server(self):
        with open(Path(self.root, 'server.log'), 'ab') as log:
            self.server = subprocess.Popen([self.bin, 'server'], env=self.env, stdin=subprocess.DEVNULL,
                                           stdout=log, stderr=log, start_new_session=True)
        self.wait(lambda: self.cli('pane', 'list')[0] == 0, 15, 'server')


class ClientEndpointTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='pn-')
        self.root = Path(self.temp.name)
        self.sb = PrivateSandbox(str(self.root / 'server'), shutil.which('herdr'))
        self.sb.setup()
        self.sb.start_server()
        self.files = self.root / 'files'
        self.files.mkdir()
        subprocess.run(['magick', '-size', '32x24', 'xc:blue', str(self.files / 'first.png')], check=True)
        subprocess.run(['magick', '-size', '48x30', 'xc:red', str(self.files / 'last.png')], check=True)
        (self.files / 'notes.txt').write_text('not an image')
        self.sb.cli('workspace', 'create', '--cwd', str(self.files), '--label', 'preview-test', '--focus', check=True)
        self.sb.attach_client()
        self.sb.wait(lambda: len(self.sb.panes()) == 1, 10, 'pane')
        pane = self.sb.panes()[0]['pane_id']
        self.sb.cli('pane', 'send-text', pane,
                    "printf '\\033[2J\\033[Hfirst.png\\nlast.png\\nnotes.txt\\n'; sleep 120\n", check=True)
        self.sb.wait(lambda: 'notes.txt' in self.sb.read(pane), 10, 'paths')
        self.context = dict(version=1, endpoint_id='private-test', pane_id=pane, server_socket=self.sb.socket)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.client_home = self.root / 'client-home'
        self.client_home.mkdir()
        data = self.client_home / 'data'
        (data / 'applications').mkdir(parents=True)
        (data / 'applications/imv.desktop').write_text('[Desktop Entry]\nType=Application\nExec=imv %F\n')
        self.marker = self.root / 'viewer.json'
        self.release = self.root / 'release'
        self.dialog = self.root / 'dialog.json'
        self.tool('xdg-mime', "print('imv.desktop')\n")
        self.tool('zenity', "import json,sys\nfrom pathlib import Path\nPath(" + repr(str(self.dialog)) + ").write_text(json.dumps(sys.argv))\nprint('1') if '--list' in sys.argv else None\n")
        self.tool('imv', "import json,os,stat,sys,time\nfrom pathlib import Path\np=Path(sys.argv[-1])\nPath(" + repr(str(self.marker)) + ").write_text(json.dumps({'pid':os.getpid(),'path':str(p),'mode':stat.S_IMODE(p.stat().st_mode),'dir_mode':stat.S_IMODE(p.parent.stat().st_mode),'png':p.read_bytes()[:8].hex()}))\nwhile not Path(" + repr(str(self.release)) + ").exists(): time.sleep(.02)\n")
        self.env = dict(self.sb.env, HOME=str(self.client_home), XDG_CONFIG_HOME=str(self.client_home / 'config'),
                        XDG_DATA_HOME=str(data), XDG_DATA_DIRS=str(self.client_home / 'empty-data'),
                        XDG_STATE_HOME=str(self.client_home / 'state'), XDG_CACHE_HOME=str(self.client_home / 'cache'),
                        TMPDIR=str(self.root), PATH=str(self.bin) + os.pathsep + os.environ['PATH'],
                        HERDR_CLIENT_CONTEXT_JSON=json.dumps(self.context))
        self.children = []
    def tool(self, name, source):
        path = self.bin / name
        path.write_text('#!' + sys.executable + '\n' + source)
        path.chmod(0o700)
    def spawn(self, command):
        proc = subprocess.Popen([sys.executable, MAIN, command], env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.children.append(proc)
        return proc
    def wait_marker(self, proc):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not self.marker.exists() and proc.poll() is None:
            time.sleep(.02)
        self.assertTrue(self.marker.exists(), proc.communicate(timeout=2)[1].decode() if proc.poll() is not None else 'no viewer')
        return json.loads(self.marker.read_text())
    def tearDown(self):
        self.release.touch()
        for proc in self.children:
            if proc.poll() is None:
                proc.terminate()
            try:
                proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
        self.sb.stop()
        self.temp.cleanup()
    def test_client_pick_uses_laptop_picker_before_opening_chosen_screen_path(self):
        self.tool('zenity', "import json,sys\nfrom pathlib import Path\nPath(" + repr(str(self.dialog)) + ").write_text(json.dumps(sys.argv))\nprint('0') if '--list' in sys.argv else None\n")
        proc = self.spawn('client-pick')
        marker = self.wait_marker(proc)
        args = json.loads(self.dialog.read_text())
        self.assertIn('--list', args)
        self.assertTrue(any('first.png' in arg for arg in args))
        self.assertTrue(any('notes.txt' in arg for arg in args), 'nonimages remain selectable with explicit safe fallback')
        import struct
        self.assertEqual(struct.unpack('>II', Path(marker['path']).read_bytes()[16:24]), (1600, 1200))
        self.release.touch()
        _, err = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 0, err.decode())

    def test_authenticated_private_ssh_discovers_enabled_exporter_and_previews_locally(self):
        import getpass
        import socket
        import hashlib
        keys = self.root / 'ssh'
        keys.mkdir(mode=0o700)
        for name in ('host', 'client'):
            subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(keys / name)], check=True)
        shutil.copy(keys / 'client.pub', keys / 'authorized_keys')
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        (keys / 'known_hosts').write_text('[127.0.0.1]:' + str(port) + ' ' + (keys / 'host.pub').read_text())
        known_before = hashlib.sha256((keys / 'known_hosts').read_bytes()).hexdigest()
        force = keys / 'remote.py'
        remote_env = self.sb.env
        force.write_text('#!' + sys.executable + '\nimport os\nos.environ.update(' + repr(remote_env) + ')\nos.execv("/bin/sh", ["sh", "-c", os.environ["SSH_ORIGINAL_COMMAND"]])\n')
        force.chmod(0o700)
        cfg = keys / 'sshd_config'
        cfg.write_text('ListenAddress 127.0.0.1\nPort ' + str(port) + '\nHostKey ' + str(keys / 'host') +
                       '\nAuthorizedKeysFile ' + str(keys / 'authorized_keys') + '\nPidFile ' + str(keys / 'pid') +
                       '\nPasswordAuthentication no\nKbdInteractiveAuthentication no\nUsePAM no\nStrictModes no\nAllowUsers ' + getpass.getuser() +
                       '\nForceCommand ' + str(force) + '\n')
        with open(keys / 'sshd.log', 'wb') as log:
            sshd = subprocess.Popen(['/usr/bin/sshd', '-D', '-e', '-f', str(cfg)], stdout=log, stderr=log)
        try:
            def ready():
                if sshd.poll() is not None:
                    raise AssertionError((keys / 'sshd.log').read_text())
                try:
                    with socket.create_connection(('127.0.0.1', port), .1):
                        return True
                except OSError:
                    return False
            self.sb.wait(ready, 5, 'private sshd')
            client_cfg = keys / 'config'
            client_cfg.write_text('Host native-test\n HostName 127.0.0.1\n Port ' + str(port) + '\n User ' + getpass.getuser() +
                                  '\n IdentityFile ' + str(keys / 'client') + '\n IdentitiesOnly yes\n UserKnownHostsFile ' +
                                  str(keys / 'known_hosts') + '\n GlobalKnownHostsFile /dev/null\n')
            self.tool('ssh', 'import os,sys\nos.execv("/usr/bin/ssh", ["ssh", "-F", ' + repr(str(client_cfg)) + '] + sys.argv[1:])\n')
            self.env['HERDR_CLIENT_CONTEXT_JSON'] = json.dumps(dict(self.context, ssh_target='native-test', machine_id='saved-private'))
            proc = self.spawn('client-last')
            marker = self.wait_marker(proc)
            self.assertTrue(Path(marker['path']).exists())
            self.release.touch()
            _, err = proc.communicate(timeout=10)
            self.assertEqual(proc.returncode, 0, err.decode())
            self.assertFalse(Path(marker['path']).parent.exists())
            self.assertEqual(hashlib.sha256((keys / 'known_hosts').read_bytes()).hexdigest(), known_before)
            self.assertFalse((Path(self.sb.home) / '.local/state/herdr-peek').exists())
        finally:
            sshd.terminate()
            sshd.wait(timeout=5)

    def test_client_termination_reaps_owned_viewer_before_removing_preview(self):
        proc = self.spawn('client-last')
        marker = self.wait_marker(proc)
        proc.terminate()
        proc.wait(timeout=5)
        self.assertFalse(Path(marker['path']).parent.exists(), 'cancelled client left a preview behind')
        with self.assertRaises(ProcessLookupError):
            os.kill(marker['pid'], 0)

    def test_unsupported_selection_never_silently_opens_a_different_screen_image(self):
        params = dict(pane_id=self.context['pane_id'], anchor=dict(row=0, col=0),
                      cursor=dict(row=0, col=8), content_revision=999999)
        self.env['HERDR_CLIENT_CONTEXT_JSON'] = json.dumps(dict(self.context, selection=params))
        self.release.touch()
        proc = self.spawn('client-pick')
        _, err = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 1, 'a stale/unsupported selection must not fall back to other screen paths')
        self.assertFalse(self.marker.exists())
        self.assertIn('selection', err.decode())

    def test_client_cancellation_during_fetch_allows_server_snapshot_cleanup(self):
        converter_marker = self.root / 'conversion.json'
        converter_release = self.root / 'converter-release'
        real_magick = shutil.which('magick')
        self.tool('magick', 'import json,os,sys,time\nfrom pathlib import Path\n'
                  'if "-resize" not in sys.argv: os.execv(' + repr(real_magick) + ', [' + repr(real_magick) + '] + sys.argv[1:])\n'
                  'src=next(a for a in sys.argv if a.startswith("PNG:") and a.endswith("[0]"))\n'
                  'Path(' + repr(str(converter_marker)) + ').write_text(json.dumps({"source":src.removeprefix("PNG:").removesuffix("[0]"),"pid":os.getpid()}))\nwhile not Path(' + repr(str(converter_release)) + ').exists(): time.sleep(.02)\n')
        proc = self.spawn('client-last')
        try:
            deadline = time.monotonic() + 5
            while not converter_marker.exists() and proc.poll() is None and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue(converter_marker.exists(), 'conversion should be in flight')
            snapshot = Path(json.loads(converter_marker.read_text())['source'])
            self.assertTrue(snapshot.exists())
            proc.terminate()
            proc.wait(timeout=5)
            self.assertFalse(snapshot.parent.exists(), 'client killed helper before its private snapshot cleanup')
        finally:
            converter_release.touch()
            if converter_marker.exists():
                import signal
                child = json.loads(converter_marker.read_text())['pid']
                try:
                    argv = Path('/proc', str(child), 'cmdline').read_bytes().split(b'\0')
                    if len(argv) > 1 and os.fsdecode(argv[1]) == str(self.bin / 'magick'):
                        os.kill(child, signal.SIGTERM)
                except OSError:
                    pass

    def test_client_last_ignores_selection_and_still_uses_bottommost_screen_image(self):
        params = dict(pane_id=self.context['pane_id'], anchor=dict(row=0, col=0),
                      cursor=dict(row=0, col=8), content_revision=999999)
        self.env['HERDR_CLIENT_CONTEXT_JSON'] = json.dumps(dict(self.context, selection=params))
        proc = self.spawn('client-last')
        marker = self.wait_marker(proc)
        import struct
        self.assertEqual(struct.unpack('>II', Path(marker['path']).read_bytes()[16:24]), (1600, 1000))
        self.release.touch()
        _, err = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 0, err.decode())

    def test_picker_cancel_is_success_without_transfer_or_viewer(self):
        self.tool('zenity', 'import sys\nsys.exit(1)\n')
        proc = self.spawn('client-pick')
        _, err = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 0, err.decode())
        self.assertFalse(self.marker.exists())
        self.assertFalse(list(self.root.glob('peek-preview-*')))

    def test_nonimage_choice_is_a_visible_safe_error_without_server_or_client_viewer(self):
        self.tool('zenity', "import json,sys\nfrom pathlib import Path\nPath(" + repr(str(self.dialog)) + ").write_text(json.dumps(sys.argv))\nprint('2') if '--list' in sys.argv else None\n")
        proc = self.spawn('client-pick')
        _, err = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 1)
        self.assertIn('ordinary peek.pick', err.decode())
        self.assertIn('--error', json.loads(self.dialog.read_text()))
        self.assertFalse(self.marker.exists())
        self.assertEqual(len(self.sb.panes()), 1)
        self.assertFalse(list(self.root.glob('peek-export-*')))

    def test_unsupported_desktop_association_is_visible_and_never_uses_a_launcher(self):
        self.tool('xdg-mime', "print('unsupported.desktop')\n")
        association = Path(self.env['XDG_DATA_HOME'], 'applications/unsupported.desktop')
        association.write_text('[Desktop Entry]\nType=Application\nDBusActivatable=true\nExec=eog %F\n')
        proc = self.spawn('client-last')
        _, err = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 1)
        self.assertIn('foreground', err.decode())
        self.assertIn('--error', json.loads(self.dialog.read_text()))
        self.assertFalse(self.marker.exists())
        self.assertFalse(list(self.root.glob('peek-preview-*')))

    def test_viewer_failure_removes_preview_and_reports_error(self):
        self.tool('imv', 'import json,sys\nfrom pathlib import Path\nPath(' + repr(str(self.marker)) + ').write_text(json.dumps({"path":sys.argv[-1]}))\nsys.exit(9)\n')
        proc = self.spawn('client-last')
        _, err = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 1)
        self.assertIn('foreground image viewer failed', err.decode())
        marker = json.loads(self.marker.read_text())
        self.assertFalse(Path(marker['path']).parent.exists())
        self.assertIn('--error', json.loads(self.dialog.read_text()))

    def test_client_last_keeps_private_preview_until_foreground_viewer_exits(self):
        proc = self.spawn('client-last')
        marker = self.wait_marker(proc)
        preview = Path(marker['path'])
        self.assertEqual(marker['mode'], 0o600)
        self.assertEqual(marker['dir_mode'], 0o700)
        self.assertEqual(marker['png'], '89504e470d0a1a0a')
        self.assertTrue(preview.exists())
        self.assertIsNone(proc.poll())
        # Last ignores the lower nonimage: 48 x 30, not first image's 32 x 24.
        import struct
        self.assertEqual(struct.unpack('>II', preview.read_bytes()[16:24]), (1600, 1000))
        self.release.touch()
        _, err = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 0, err.decode())
        self.assertFalse(preview.parent.exists())
        self.assertEqual(len(self.sb.panes()), 1, 'no server-native or split viewer')


if __name__ == '__main__':
    unittest.main()
