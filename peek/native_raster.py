"""Restricted PNG adapter for native export ONLY; no general Peek fallbacks.

Policies constrain ImageMagick features, not arbitrary native decoder bugs.
The environment and all policy files are per-export; installed policies stay intact.
"""
import os
import selectors
import shutil
import subprocess
import time
from xml.sax.saxutils import quoteattr

MAX_OUTPUT = 8 * 1024 * 1024
MAX_STREAM = 64 * 1024


def converter():
    tool = shutil.which('magick')
    if not tool:
        raise ValueError('restricted PNG converter unavailable; install ImageMagick on the server')
    return tool


def restricted_policy(snapshot, output, root):
    """Deny everything except the one PNG snapshot read and one PNG write."""
    root, snapshot, output = map(os.path.realpath, (root, snapshot, output))
    for path in (root, snapshot, output):
        if any(char in path for char in '*?[]{}\\\r\n\x00'):
            raise ValueError('unsupported temporary path for restricted raster policy')
    if os.path.dirname(snapshot) != root or os.path.dirname(output) != root or snapshot == output:
        raise ValueError('raster snapshot and output must be distinct private files')
    for name in ('policy', 'home', 'scratch'):
        os.mkdir(os.path.join(root, name), 0o700)
    rules = []
    def rule(**attrs):
        rules.append('<policy ' + ' '.join(key + '=' + quoteattr(value) for key, value in attrs.items()) + '/>')
    for name, value in (('thread', '1'), ('time', '20'), ('width', '8192'), ('height', '8192'),
                        ('area', '8MP'), ('list-length', '4'), ('memory', '256MiB'),
                        ('map', '0'), ('disk', '0'), ('file', '16')):
        rule(domain='resource', name=name, value=value)
    rule(domain='system', name='max-memory-request', value='256MiB')
    for domain in ('delegate', 'filter', 'module', 'coder', 'path'):
        rule(domain=domain, rights='none', pattern='*')
    rule(domain='module', rights='read | write', pattern='PNG')
    rule(domain='coder', rights='read | write', pattern='PNG')
    rule(domain='path', rights='read', pattern=snapshot)
    rule(domain='path', rights='write', pattern=output)
    # Explicitly retain indirect, pipes and stdio denials after the exceptions.
    for pattern in ('@*', '|*', '-', '[Ff][Dd]:*'):
        rule(domain='path', rights='none', pattern=pattern)
    policy = os.path.join(root, 'policy', 'policy.xml')
    fd = os.open(policy, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as target:
        target.write('<policymap>\n' + '\n'.join(rules) + '\n</policymap>\n')
    return dict(PATH='/usr/bin:/bin', LANG='C', LC_ALL='C', HOME=root + '/home',
                XDG_CONFIG_HOME=root + '/home', MAGICK_CONFIGURE_PATH=root + '/policy',
                MAGICK_TEMPORARY_PATH=root + '/scratch', TMPDIR=root + '/scratch',
                MAGICK_THREAD_LIMIT='1', OMP_NUM_THREADS='1', MAGICK_OCL_DEVICE='OFF')


def child_limits():
    import resource
    os.setpgrp()
    os.umask(0o077)
    for kind, value in ((resource.RLIMIT_AS, 1024 * 1024 * 1024), (resource.RLIMIT_CPU, 20),
                        (resource.RLIMIT_FSIZE, MAX_OUTPUT), (resource.RLIMIT_CORE, 0),
                        (resource.RLIMIT_NOFILE, 32)):
        resource.setrlimit(kind, (value, value))


def run_converter(argv, env, root, timeout=20):
    """A dedicated Linux subreaper owns the converter group, never the caller.

    Terminating this worker unwinds group kill/reap before the parent's bounded
    transport returns. No preexec hook or subreaper setting touches the caller.
    """
    import json
    import sys
    if sys.platform != 'linux':
        raise ValueError('restricted native raster converter requires Linux child ownership')
    from native_protocol import run_bounded
    request = json.dumps(dict(argv=argv, env=env, root=root, timeout=timeout)).encode()
    return run_bounded([sys.executable, '-I', os.path.abspath(__file__), '--worker'],
                       data=request, env=env, timeout=timeout + 3, limit=MAX_STREAM)


def supervise(argv, env, root, timeout):
    import signal
    handled = (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)
    old_mask = signal.pthread_sigmask(signal.SIG_BLOCK, handled)
    proc = None
    group_closed = False
    try:
        proc = subprocess.Popen(argv, env=env, cwd=root, stdin=subprocess.DEVNULL,
                                preexec_fn=child_limits, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        # Establish ownership/finally before exposing pending cancellation.
        signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)
        streams = {proc.stdout: bytearray(), proc.stderr: bytearray()}
        deadline = time.monotonic() + timeout
        with selectors.DefaultSelector() as selector:
            for stream in streams:
                selector.register(stream, selectors.EVENT_READ)
            while selector.get_map() or not group_closed:
                if time.monotonic() >= deadline:
                    raise ValueError('restricted raster converter timed out')
                # Observe leader death without reaping: its PID/group cannot be
                # reused before the entire owned group has been terminated.
                if not group_closed and os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT):
                    os.killpg(proc.pid, signal.SIGKILL)
                    group_closed = True
                for key, _ in selector.select(.05):
                    data = os.read(key.fileobj.fileno(), 8192)
                    if not data:
                        selector.unregister(key.fileobj)
                        continue
                    buffer = streams[key.fileobj]
                    if len(buffer) + len(data) > MAX_STREAM:
                        raise ValueError('restricted raster converter stream limit exceeded')
                    buffer.extend(data)
            proc.wait()
        if proc.returncode:
            raise ValueError('restricted raster converter failed: ' + streams[proc.stderr].decode('utf-8', 'replace')[:400])
        return bytes(streams[proc.stdout])
    finally:
        signal.pthread_sigmask(signal.SIG_BLOCK, handled)
        if proc is not None:
            if not group_closed:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            proc.wait()
            proc.stdout.close()
            proc.stderr.close()
            # Only the worker is a subreaper; all adopted converter descendants
            # are killed group members and must be reaped, not left as zombies.
            while True:
                try:
                    os.waitpid(-1, 0)
                except ChildProcessError:
                    break


def worker_main():
    import ctypes
    import json
    import signal
    import sys
    os.setsid()
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0):  # PR_SET_CHILD_SUBREAPER
        raise ValueError('cannot establish restricted raster child ownership')
    def cancelled(signum, frame):
        raise ValueError('restricted raster converter cancelled')
    for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(signum, cancelled)
    try:
        request = sys.stdin.buffer.read(65537)
        if len(request) > 65536:
            raise ValueError('restricted raster worker request too large')
        args = json.loads(request)
        data = supervise(**args)
        sys.stdout.buffer.write(data)
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(str(exc)[:400], file=sys.stderr)
        return 1


def image_png(snapshot, root, width, height):
    import struct
    import zlib
    with open(snapshot, 'rb') as source:
        header = source.read(33)
    if (len(header) != 33 or header[:16] != b'\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIHDR' or
            zlib.crc32(header[12:29]) != struct.unpack('>I', header[29:33])[0]):
        raise ValueError('unsupported native raster format; valid PNG header required')
    w, h = struct.unpack('>II', header[16:24])
    if not (0 < w <= 8192 and 0 < h <= 8192 and w * h <= 8_000_000):
        raise ValueError('native raster source pixel limit exceeded')
    tool = converter()
    output = os.path.join(root, 'preview.png')
    env = restricted_policy(snapshot, output, root)
    listing = run_converter([tool, '-list', 'policy'], env, root)
    if (root + '/policy/policy.xml').encode() not in listing:
        raise ValueError('restricted raster policy was not loaded; export refused')
    # Prove effective path denial with an inert valid secondary PNG. Listing
    # a private policy is insufficient if a loader ignores/overrides its rules.
    probe = os.path.join(root, 'blocked-probe.png')
    fd = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as target:
        target.write(bytes.fromhex('89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de0000000c49444154789c6360f8cf0000020201007b0981780000000049454e44ae426082'))
    try:
        run_converter([tool, 'PNG:' + probe, 'PNG:' + output], env, root)
    except ValueError as exc:
        if not any(word in str(exc).lower() for word in ('security policy', 'not authorized')):
            raise ValueError('restricted raster policy enforcement could not be verified') from exc
    else:
        raise ValueError('restricted raster policy enforcement failed; secondary read allowed')
    finally:
        os.unlink(probe)
    run_converter([tool, 'PNG:' + snapshot + '[0]', '-auto-orient', '-resize',
                   '%dx%d' % (width, height), 'PNG:' + output], env, root)
    with open(output, 'rb') as source:
        data = source.read(MAX_OUTPUT + 1)
    if len(data) > MAX_OUTPUT:
        raise ValueError('restricted raster output limit exceeded')
    from native_protocol import validate_png
    return validate_png(data)


if __name__ == '__main__':
    import sys
    if sys.argv[1:] != ['--worker']:
        sys.exit('restricted raster adapter is an internal worker')
    sys.exit(worker_main())
