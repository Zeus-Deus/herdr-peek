"""Native preview protocol tests; no real desktop is launched."""
import importlib
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'peek'))


def module(name):
    # An assertion, not an import error, proves the missing feature in RED.
    assert importlib.util.find_spec(name) is not None, 'missing native preview module: ' + name
    return importlib.import_module(name)


class CandidateTest(unittest.TestCase):
    def test_svg_export_is_visibly_unsupported_not_generic_conversion(self):
        from unittest.mock import patch
        server = module('native_server')
        with tempfile.TemporaryDirectory() as root:
            source = Path(root, 'plain.svg')
            source.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"/>')
            candidate = server.candidates_from_text('plain.svg', root)[0]
            with patch('convert.image_png', return_value=b'not an admitted raster') as generic:
                with self.assertRaisesRegex(ValueError, 'unsupported.*raster|unsupported.*format'):
                    server.export_candidate(candidate, temp_root=root)
                generic.assert_not_called()

    def test_server_resolves_screen_paths_and_retains_bottommost_occurrence(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, 'shot.png').write_bytes(b'\x89PNG\r\n\x1a\n')
            Path(root, 'notes.txt').write_text('notes')
            candidates = module('native_server').candidates_from_text(
                'shot.png\nnotes.txt\nshot.png', root)
            self.assertEqual([c['path'] for c in candidates],
                             [str(Path(root, 'notes.txt')), str(Path(root, 'shot.png'))])
            self.assertEqual(candidates[-1]['kind'], 'image')
            self.assertEqual(candidates[-1]['row'], 2)
            self.assertEqual(set(candidates[-1]['signature']),
                             {'dev', 'ino', 'size', 'mtime_ns', 'ctime_ns'})
    def test_export_refuses_original_png_when_no_resizing_converter_exists(self):
        import json
        import subprocess
        with tempfile.TemporaryDirectory() as root:
            source = Path(root, 'small.png')
            subprocess.run(['magick', '-size', '8x6', 'xc:green', str(source)], check=True)
            candidate = module('native_server').candidates_from_text('small.png', root)[0]
            request = dict(version=1, op='export', context={}, candidate=candidate)
            entrypoint = str(Path(__file__).resolve().parents[1] / 'peek/main.py')
            env = dict(os.environ, PATH=root, TMPDIR=root)
            proc = subprocess.run([sys.executable, entrypoint, 'native-server'], env=env,
                                  input=json.dumps(request).encode(), capture_output=True, timeout=10)
            self.assertEqual(proc.returncode, 0, proc.stderr.decode())
            reply = json.loads(proc.stdout)
            self.assertFalse(reply['ok'], 'cannot transfer original PNG as a substitute for a resized export')
            self.assertIn('converter', reply['error'])
            self.assertEqual(list(Path(root).iterdir()), [source])

    def test_server_cancellation_erases_private_snapshot_and_conversion_cache(self):
        import json
        import subprocess
        import time
        with tempfile.TemporaryDirectory() as root:
            source = Path(root, 'source.png')
            subprocess.run(['magick', '-size', '8x6', 'xc:green', str(source)], check=True)
            candidate = module('native_server').candidates_from_text('source.png', root)[0]
            marker, release = Path(root, 'converter.json'), Path(root, 'release')
            tool = Path(root, 'magick')
            import shutil
            real_magick = shutil.which('magick')
            tool.write_text('#!' + sys.executable + '\nimport json,sys,time,os,signal\nfrom pathlib import Path\n'
                            'if "-resize" not in sys.argv: os.execv(' + repr(real_magick) + ', [' + repr(real_magick) + '] + sys.argv[1:])\n'
                            'child=os.fork()\n'
                            'if child==0:\n signal.signal(signal.SIGTERM,signal.SIG_IGN)\n while True: time.sleep(.02)\n'
                            'src=next(a for a in sys.argv if a.startswith("PNG:") and a.endswith("[0]"))\n'
                            'Path(' + repr(str(marker)) + ').write_text(json.dumps({"source":src.removeprefix("PNG:").removesuffix("[0]"),"pid":os.getpid(),"delegate":child,"group":os.getpgrp()}))\n'
                            'while not Path(' + repr(str(release)) + ').exists(): time.sleep(.02)\n')
            tool.chmod(0o700)
            request = dict(version=1, op='export', context={}, candidate=candidate)
            main = str(Path(__file__).resolve().parents[1] / 'peek/main.py')
            import ctypes
            libc = ctypes.CDLL(None, use_errno=True)
            previous_subreaper = ctypes.c_int()
            self.assertEqual(libc.prctl(37, ctypes.byref(previous_subreaper), 0, 0, 0), 0)
            self.assertEqual(libc.prctl(36, 1, 0, 0, 0), 0)
            proc = subprocess.Popen([sys.executable, main, 'native-server'], env=dict(os.environ, PATH=root, TMPDIR=root),
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                proc.stdin.write(json.dumps(request).encode())
                proc.stdin.close()
                proc.stdin = None
                deadline = time.monotonic() + 5
                while not marker.exists() and proc.poll() is None and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue(marker.exists(), 'conversion must be in flight')
                snapshot = Path(json.loads(marker.read_text())['source'])
                self.assertTrue(snapshot.exists())
                proc.terminate()
                proc.wait(timeout=5)
                self.assertFalse(snapshot.parent.exists(), 'server termination left private conversion data behind')
                info = json.loads(marker.read_text())
                for pid in (info['pid'], info['delegate']):
                    with self.assertRaises(ProcessLookupError, msg='cancelled converter descendant was not terminated AND reaped'):
                        os.kill(pid, 0)
                with self.assertRaises(ProcessLookupError):
                    os.killpg(info['group'], 0)
            finally:
                release.touch()
                if proc.poll() is None:
                    proc.kill()
                proc.communicate(timeout=5)
                # RED cleanup reaps only the exact recorded synthetic children.
                if marker.exists():
                    import signal
                    info = json.loads(marker.read_text())
                    for child in (info['pid'], info['delegate']):
                        try:
                            os.kill(child, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        try:
                            os.waitpid(child, 0)
                        except ChildProcessError:
                            pass
                libc.prctl(36, previous_subreaper.value, 0, 0, 0)

    def test_server_nonimage_export_has_explicit_safe_fallback_not_a_converter_or_desktop(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root, 'notes.txt')
            source.write_text('ordinary nonimage')
            candidate = module('native_server').candidates_from_text('notes.txt', root)[0]
            for kind in ('text', 'image'):
                with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, 'ordinary peek.pick'):
                    module('native_server').export_candidate(dict(candidate, kind=kind), temp_root=root)
            self.assertEqual(source.read_text(), 'ordinary nonimage')
            self.assertEqual(list(Path(root).iterdir()), [source])

    def test_candidate_limit_retains_bottommost_paths_not_the_top_of_screen(self):
        import json
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as root:
            names = ['f%03d.png' % index for index in range(300)]
            for name in names:
                Path(root, name).write_bytes(b'\x89PNG\r\n\x1a\n')
            server = module('native_server')
            def cli(context, argv):
                return (json.dumps({'result': {'pane': {'cwd': root}}}).encode()
                        if argv[:2] == ['pane', 'get'] else ('\n'.join(names)).encode())
            with patch.object(server, 'server_cli', cli):
                candidates = server.list_candidates({'pane_id': 'private-pane'})
            self.assertLessEqual(len(candidates), 256)
            self.assertEqual(candidates[-1]['path'], str(Path(root, names[-1])))

    def test_last_filters_images_before_candidate_limit(self):
        import inspect
        import json
        from unittest.mock import patch
        server = module('native_server')
        self.assertIn('images_only', inspect.signature(server.list_candidates).parameters,
                      'last needs a server-side image-filtered candidate list')
        with tempfile.TemporaryDirectory() as root:
            Path(root, 'shot.png').write_bytes(b'\x89PNG\r\n\x1a\n')
            names = ['shot.png'] + ['n%03d.txt' % i for i in range(300)]
            for name in names[1:]:
                Path(root, name).write_text('text')
            def cli(context, argv):
                return (json.dumps({'result': {'pane': {'cwd': root}}}).encode()
                        if argv[:2] == ['pane', 'get'] else ('\n'.join(names)).encode())
            with patch.object(server, 'server_cli', cli):
                candidates = server.list_candidates({'pane_id': 'private'}, images_only=True)
            self.assertEqual([c['path'] for c in candidates], [str(Path(root, 'shot.png'))])

    def test_regular_file_bounds_exclude_symlinks_fifo_directories_and_oversized_sources(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, 'shot.png').write_bytes(b'\x89PNG\r\n\x1a\n')
            Path(root, 'link.png').symlink_to(Path(root, 'shot.png'))
            Path(root, 'folder.png').mkdir()
            os.mkfifo(str(Path(root, 'pipe.png')))
            with open(Path(root, 'huge.png'), 'wb') as target:
                target.truncate(32 * 1024 * 1024 + 1)
            result = module('native_server').candidates_from_text('shot.png link.png folder.png pipe.png huge.png', root)
            self.assertEqual([c['path'] for c in result], [str(Path(root, 'shot.png'))])

    def test_export_rejects_replaced_or_in_place_modified_listed_file(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root, 'shot.png')
            source.write_bytes(b'\x89PNG\r\n\x1a\n')
            server = module('native_server')
            candidate = server.candidates_from_text('shot.png', root)[0]
            source.write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError, 'changed'):
                server.export_candidate(candidate, temp_root=root)
            source.unlink()
            source.write_bytes(b'\x89PNG\r\n\x1a\n')
            with self.assertRaisesRegex(ValueError, 'changed'):
                server.export_candidate(candidate, temp_root=root)
            self.assertEqual(list(Path(root).iterdir()), [source])

    def test_export_is_resized_png_without_persistent_cache_or_source_changes(self):
        import hashlib
        import struct
        import subprocess
        with tempfile.TemporaryDirectory() as root:
            path = Path(root, 'large.png')
            subprocess.run(['magick', '-size', '2200x1800', 'xc:red', str(path)], check=True)
            before = hashlib.sha256(path.read_bytes()).hexdigest()
            candidate = module('native_server').candidates_from_text('large.png', root)[0]
            self.assertTrue(hasattr(module('native_server'), 'export_candidate'), 'missing image export')
            data = module('native_server').export_candidate(candidate, temp_root=root)
            self.assertEqual(data[:8], b'\x89PNG\r\n\x1a\n')
            w, h = struct.unpack('>II', data[16:24])
            self.assertLessEqual(w, 1600)
            self.assertLessEqual(h, 1200)
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)
            self.assertEqual(list(Path(root).iterdir()), [path])


class EndpointTest(unittest.TestCase):
    def test_private_endpoint_lists_and_exports_via_actual_entrypoint(self):
        import base64
        import json
        import shutil
        import subprocess
        from test_native_integration import PrivateSandbox
        with tempfile.TemporaryDirectory(prefix='pn-') as root:
            sb = PrivateSandbox(str(Path(root, 'sb')), shutil.which('herdr'))
            sb.setup()
            sb.start_server()
            try:
                files = Path(root, 'files')
                files.mkdir()
                subprocess.run(['magick', '-size', '32x24', 'xc:blue', str(files / 'shot.png')], check=True)
                sb.cli('workspace', 'create', '--cwd', str(files), '--label', 'preview-test', '--focus', check=True)
                sb.attach_client()
                sb.wait(lambda: len(sb.panes()) == 1, 10, 'pane')
                pane = sb.panes()[0]['pane_id']
                sb.cli('pane', 'send-text', pane, "printf '\\033[2J\\033[Hshot.png\\n'; sleep 30\n", check=True)
                sb.wait(lambda: 'shot.png' in sb.read(pane), 10, 'path')
                ctx = dict(version=1, endpoint_id='private-test', pane_id=pane, server_socket=sb.socket)
                request = dict(version=1, op='list', context=ctx)
                main = str(Path(__file__).resolve().parents[1] / 'peek/main.py')
                proc = subprocess.run([sys.executable, main, 'native-server'], input=json.dumps(request).encode(),
                                      env=sb.env, capture_output=True, timeout=15)
                self.assertEqual(proc.returncode, 0, proc.stderr.decode())
                response = json.loads(proc.stdout)
                self.assertTrue(response['ok'], response)
                candidate = response['candidates'][0]
                self.assertEqual(candidate['path'], str(files / 'shot.png'))
                request = dict(version=1, op='export', context=ctx, candidate=candidate)
                proc = subprocess.run([sys.executable, main, 'native-server'], input=json.dumps(request).encode(),
                                      env=sb.env, capture_output=True, timeout=15)
                self.assertEqual(proc.returncode, 0, proc.stderr.decode())
                response = json.loads(proc.stdout)
                self.assertTrue(response['ok'], response)
                self.assertEqual(base64.b64decode(response['png_base64'])[:8], b'\x89PNG\r\n\x1a\n')
            finally:
                sb.stop()


class NativeViewerTest(unittest.TestCase):
    def test_viewer_never_receives_malformed_or_oversized_preview_bytes(self):
        client = module('native_client')
        for data in (b'not png', b'\x89PNG\r\n\x1a\n' + b'x' * 30, b'x' * (8 * 1024 * 1024 + 1)):
            with self.subTest(size=len(data)), self.assertRaisesRegex(ValueError, 'PNG'):
                client.view_temporary(data, ['/bin/true'])

    def test_rejects_associations_without_a_known_foreground_lifetime(self):
        client = module('native_client')
        for entry in [
            {'Type': 'Application', 'Exec': 'xdg-open %F'},
            {'Type': 'Application', 'Exec': 'eog %F', 'DBusActivatable': 'true'},
            {'Type': 'Application', 'Exec': 'sh -c "imv %F"'},
            {'Type': 'Application', 'Exec': 'imv %F', 'Terminal': 'true'},
            {'Type': 'Application', 'Exec': 'imv %F', 'Hidden': 'true'},
            {'Type': 'Application', 'Exec': 'imv %F --unsafe'},
            {'Type': 'Application', 'Exec': 'imv %U'},
            {'Type': 'Link', 'Exec': 'imv %F'},
        ]:
            with self.subTest(entry=entry), self.assertRaisesRegex(ValueError, 'foreground'):
                client.desktop_argv(entry, '/private/a.png')

    def test_imv_desktop_exec_is_direct_argv_not_a_launcher(self):
        client = module('native_client')
        self.assertEqual(client.desktop_argv({'Type': 'Application', 'Exec': 'imv %F'},
                                              '/private/a file.png'), ['imv', '/private/a file.png'])
        self.assertEqual(client.desktop_argv({'Type': 'Application', 'Exec': '/usr/bin/imv %f'},
                                              '/private/a.png'), ['/usr/bin/imv', '/private/a.png'])


class TransportTest(unittest.TestCase):
    def test_malformed_candidate_replies_are_actionable_errors_before_picker_launch(self):
        import json
        protocol = module('native_protocol')
        for candidates in ('not a list', ['not a candidate'], [dict(path='relative.png', kind='image')]):
            with self.subTest(candidates=candidates), self.assertRaisesRegex(ValueError, 'candidate'):
                protocol.decode_response(json.dumps(dict(version=1, ok=True, candidates=candidates)).encode())

    def test_ssh_target_shell_metacharacters_are_rejected_even_with_trusted_proxy_config(self):
        client = module('native_client')
        for target in ('host;touch-pwn', 'user@host$(id)', '`id`@host', 'host|cat', 'host&other', 'user@host/foo'):
            with self.subTest(target=target), self.assertRaisesRegex(ValueError, 'SSH target'):
                client.transport_argv(dict(ssh_target=target))
        self.assertEqual(client.transport_argv(dict(ssh_target='user@[::1]'))[-2], 'user@[::1]')

    def test_ssh_uses_fixed_bootstrap_and_keeps_request_values_out_of_shell(self):
        client = module('native_client')
        self.assertTrue(hasattr(client, 'transport_argv'), 'missing secure SSH transport')
        first = client.transport_argv(dict(ssh_target='user@host', session="a'; touch /unsafe", pane_id='p'))
        second = client.transport_argv(dict(ssh_target='other-host', session='safe', pane_id='other'))
        self.assertEqual(first[-1], second[-1], 'remote shell command is fixed; all context travels on stdin')
        self.assertIn('BatchMode=yes', first)
        self.assertIn('StrictHostKeyChecking=yes', first)
        self.assertIn('UpdateHostKeys=no', first)
        self.assertIn('ConnectTimeout=10', first)
        self.assertEqual(first[-2], 'user@host')
        self.assertNotIn('/unsafe', first[-1])


class SelectionTest(unittest.TestCase):
    def test_explicit_named_session_cannot_read_ambient_default_socket_selection(self):
        import json
        import socket
        import threading
        from unittest.mock import patch
        with tempfile.TemporaryDirectory(prefix='ps-') as root:
            config = Path(root, 'c')
            named = config / 'herdr/sessions/_blue/herdr.sock'
            named.parent.mkdir(parents=True)
            ambient = Path(root, 'default.sock')
            params = dict(pane_id='same-id-on-two-servers', anchor=dict(row=0, col=0),
                          cursor=dict(row=0, col=8), content_revision=42)
            listeners, workers = [], []
            def peer(listener, text):
                try:
                    conn, _ = listener.accept()
                except TimeoutError:
                    return
                with conn:
                    data = b''
                    while not data.endswith(b'\n'):
                        data += conn.recv(4096)
                    conn.sendall(json.dumps(dict(id='peek-selection', result=dict(pane_id=params['pane_id'], text=text))).encode() + b'\n')
            try:
                for path, text in ((named, 'named.png'), (ambient, 'wrong.png')):
                    listener = socket.socket(socket.AF_UNIX)
                    listener.bind(str(path))
                    listener.listen()
                    listener.settimeout(.5)
                    listeners.append(listener)
                    worker = threading.Thread(target=peer, args=(listener, text), daemon=True)
                    worker.start()
                    workers.append(worker)
                with patch.dict(os.environ, XDG_CONFIG_HOME=str(config), HERDR_SOCKET_PATH=str(ambient)):
                    text = module('native_server').read_selection(dict(pane_id=params['pane_id'], session='_blue', selection=params))
                self.assertEqual(text, 'named.png', 'explicit session must ignore the ambient default socket')
            finally:
                for worker in workers:
                    worker.join(2)
                for listener in listeners:
                    listener.close()

    def test_selection_rpc_preserves_exact_coordinates_and_content_revision(self):
        import json
        import socket
        import threading
        server = module('native_server')
        self.assertTrue(hasattr(server, 'read_selection'), 'missing exact selection reader')
        with tempfile.TemporaryDirectory(prefix='ps-') as root:
            sock = str(Path(root, 'api.sock'))
            params = dict(pane_id='pane-opaque', anchor=dict(row=5, col=8), cursor=dict(row=5, col=17), content_revision=42)
            received = []
            with socket.socket(socket.AF_UNIX) as listener:
                listener.bind(sock)
                listener.listen()
                def peer():
                    conn, _ = listener.accept()
                    with conn:
                        data = b''
                        while not data.endswith(b'\n'):
                            data += conn.recv(4096)
                        received.append(json.loads(data))
                        conn.sendall(json.dumps(dict(id='peek-selection', result=dict(pane_id='pane-opaque', text='shot.png'))).encode() + b'\n')
                worker = threading.Thread(target=peer, daemon=True)
                worker.start()
                text = server.read_selection(dict(pane_id='pane-opaque', server_socket=sock, selection=params))
                worker.join(2)
            self.assertEqual(text, 'shot.png')
            self.assertEqual(received, [dict(id='peek-selection', method='pane.selection.read', params=params)])


if __name__ == '__main__':
    unittest.main()
