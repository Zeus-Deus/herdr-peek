"""Real private policy tests: synthetic files only, no GUI/network/resources outside fixture."""
import importlib.util
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'peek'))


def adapter():
    assert importlib.util.find_spec('native_raster'), 'missing restricted native raster converter'
    import native_raster
    return native_raster


def png(width=1, height=1):
    def chunk(kind, body):
        return struct.pack('>I', len(body)) + kind + body + struct.pack('>I', zlib.crc32(kind + body))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)) +
            chunk(b'IDAT', zlib.compress(b'\x00\x00\xff\x00')) + chunk(b'IEND', b''))


class RestrictedRasterTest(unittest.TestCase):
    def test_unverified_legacy_convert_is_not_a_native_fallback(self):
        from unittest.mock import patch
        helper = adapter()
        with patch.object(helper.shutil, 'which', side_effect=lambda name: '/synthetic/convert' if name == 'convert' else None):
            with self.assertRaisesRegex(ValueError, 'converter unavailable'):
                helper.converter()

    def test_worker_reaps_synthetic_delegate_group_on_error_timeout_stream_overflow_and_exit(self):
        import ctypes
        import json
        import signal
        helper = adapter()
        libc = ctypes.CDLL(None, use_errno=True)
        previous = ctypes.c_int()
        self.assertEqual(libc.prctl(37, ctypes.byref(previous), 0, 0, 0), 0)
        self.assertEqual(libc.prctl(36, 1, 0, 0, 0), 0)
        try:
            for mode in ('error', 'timeout', 'stdout', 'stderr', 'success'):
                with self.subTest(mode=mode), tempfile.TemporaryDirectory() as root:
                    marker = Path(root, 'delegate.json')
                    script = Path(root, 'converter.py')
                    script.write_text('import json,os,signal,sys,time\nfrom pathlib import Path\n'
                                      'child=os.fork()\n'
                                      'if child==0:\n signal.signal(signal.SIGTERM,signal.SIG_IGN)\n while True: time.sleep(.01)\n'
                                      'Path(' + repr(str(marker)) + ').write_text(json.dumps({"leader":os.getpid(),"delegate":child,"group":os.getpgrp()}))\n'
                                      'mode=sys.argv[1]\n'
                                      'if mode=="error": sys.exit(9)\n'
                                      'if mode=="timeout": time.sleep(10)\n'
                                      'if mode in ("stdout","stderr"):\n'
                                      ' fd=1 if mode=="stdout" else 2\n'
                                      ' for i in range(9): os.write(fd,b"x"*8192)\n'
                                      'else: print("ok")\n')
                    try:
                        argv = [sys.executable, str(script), mode]
                        if mode == 'success':
                            self.assertEqual(helper.run_converter(argv, {'HOME':root}, root, timeout=.5), b'ok\n')
                        else:
                            with self.assertRaisesRegex(ValueError, 'failed|timed out|stream limit'):
                                helper.run_converter(argv, {'HOME':root}, root, timeout=.5)
                        info = json.loads(marker.read_text())
                        for pid in (info['leader'], info['delegate']):
                            with self.assertRaises(ProcessLookupError, msg='converter error left a live or zombie descendant'):
                                os.kill(pid, 0)
                        with self.assertRaises(ProcessLookupError):
                            os.killpg(info['group'], 0)
                    finally:
                        if marker.exists():
                            info = json.loads(marker.read_text())
                            for pid in (info['leader'], info['delegate']):
                                try: os.kill(pid, signal.SIGKILL)
                                except ProcessLookupError: pass
                                try: os.waitpid(pid, 0)
                                except ChildProcessError: pass
        finally:
            libc.prctl(36, previous.value, 0, 0, 0)

    def test_policy_listing_without_effective_read_denial_is_refused(self):
        from unittest.mock import patch
        helper = adapter()
        with tempfile.TemporaryDirectory() as root:
            source = Path(root, 'source.png')
            source.write_bytes(png())
            listing = str(Path(root, 'policy/policy.xml')).encode()
            # The only mocked boundary is an ineffective external policy loader.
            with patch.object(helper, 'run_converter', return_value=listing):
                try:
                    helper.image_png(str(source), root, 1600, 1200)
                except ValueError as exc:
                    self.assertIn('policy enforcement', str(exc))
                except OSError as exc:
                    self.fail('listed-but-ineffective policy reached conversion: ' + str(exc))
                else:
                    self.fail('ineffective policy was accepted')

    def test_os_limits_are_in_place_at_converter_entry(self):
        import json
        helper = adapter()
        with tempfile.TemporaryDirectory() as root:
            env = {'PATH': '/usr/bin:/bin', 'HOME': root}
            script = 'import json,resource; print(json.dumps({n: resource.getrlimit(getattr(resource,n)) for n in ("RLIMIT_AS","RLIMIT_CPU","RLIMIT_FSIZE","RLIMIT_CORE","RLIMIT_NOFILE")}))'
            limits = json.loads(helper.run_converter([sys.executable, '-c', script], env, root))
            self.assertEqual(limits['RLIMIT_AS'], [1024 * 1024 * 1024] * 2, 'address space unbounded before decoder entry')
            self.assertEqual(limits['RLIMIT_CPU'], [20, 20])
            self.assertEqual(limits['RLIMIT_FSIZE'], [8 * 1024 * 1024] * 2)
            self.assertEqual(limits['RLIMIT_CORE'], [0, 0])
            self.assertEqual(limits['RLIMIT_NOFILE'], [32, 32])
            target = Path(root, 'bounded-output.bin')
            writer = ('import os\nf=os.open(' + repr(str(target)) + ',os.O_CREAT|os.O_WRONLY,0o600)\n'
                      'for _ in range(129): os.write(f,b"x"*65536)\n')
            with self.assertRaisesRegex(ValueError, 'failed'):
                helper.run_converter([sys.executable, '-c', writer], env, root)
            self.assertLessEqual(target.stat().st_size, 8 * 1024 * 1024, 'output was allowed to grow past OS limit')

    def test_compressed_png_header_is_rejected_before_starting_any_decoder(self):
        from unittest.mock import patch
        helper = adapter()
        for width, height in ((8193, 1), (4096, 4096)):
            with self.subTest(dimensions=(width, height)), tempfile.TemporaryDirectory() as root:
                source = Path(root, 'source.png')
                source.write_bytes(png(width, height))  # one compressed pixel; no large allocation
                with patch.object(helper, 'converter', side_effect=AssertionError('decoder admitted before pixel check')):
                    with self.assertRaisesRegex(ValueError, 'pixel limit'):
                        helper.image_png(str(source), root, 1600, 1200)

    def test_actual_private_policy_admits_snapshot_but_denies_secondary_and_unsupported_resources(self):
        helper = adapter()
        with tempfile.TemporaryDirectory() as root:
            source, secondary, out = (Path(root, name) for name in ('source.png', 'secondary.png', 'out.png'))
            source.write_bytes(png())
            secondary.write_bytes(png())
            env = helper.restricted_policy(str(source), str(out), root)
            tool = helper.converter()
            listing = helper.run_converter([tool, '-list', 'policy'], env, root)
            self.assertIn(str(Path(root, 'policy/policy.xml')).encode(), listing)
            for domain in (b'Delegate', b'Filter', b'Module', b'Coder', b'Path', b'Resource'):
                self.assertIn(b'Policy: ' + domain, listing)
            helper.run_converter([tool, 'PNG:' + str(source), 'PNG:' + str(out)], env, root)
            self.assertEqual(out.read_bytes()[:8], b'\x89PNG\r\n\x1a\n')
            import stat
            self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o600, 'converter output must be private at creation')
            for args in ([ 'PNG:' + str(secondary), 'PNG:' + str(out)],
                         ['@' + str(secondary), 'PNG:' + str(out)],
                         ['-size', '1x1', 'xc:green', 'PNG:' + str(out)],
                         ['PNG:' + str(source), '-process', 'synthetic-denied-filter', 'PNG:' + str(out)]):
                diagnostic = 'no decode delegate|security policy|not authorized' if 'xc:green' in args else 'security policy|not authorized'
                with self.subTest(args=args), self.assertRaisesRegex(ValueError, diagnostic):
                    helper.run_converter([tool] + args, env, root)
            self.assertEqual(secondary.read_bytes(), png())
            # Prefixing a PNG with MVG/HTTPS does not force those decoders on
            # this ImageMagick. Use an actual inert MVG/SVG body instead.
            for coder, body in [('SVG', b'<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"/>')]:
                source.write_bytes(body)
                with self.subTest(coder=coder), self.assertRaisesRegex(ValueError, 'security policy|not authorized'):
                    helper.run_converter([tool, coder + ':' + str(source), 'PNG:' + str(out)], env, root)
            source.write_bytes(png(8193, 1))  # compressed one-pixel data with over-limit header
            with self.assertRaisesRegex(ValueError, 'exceeds (?:user )?limit|cache resources exhausted'):
                helper.run_converter([tool, 'PNG:' + str(source), 'PNG:' + str(out)], env, root)
            source.write_bytes(png())
            from unittest.mock import patch
            import native_server
            candidate = native_server.candidates_from_text('source.png', root)[0]
            with patch('convert.image_png', side_effect=AssertionError('generic conversion forbidden')):
                data = native_server.export_candidate(candidate, temp_root=root)
            self.assertEqual(data[:8], b'\x89PNG\r\n\x1a\n')


if __name__ == '__main__':
    unittest.main()
