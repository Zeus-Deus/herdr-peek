"""Read-only, bounded image export on the pane-owning server. No GUI launches."""
import base64
import json
import os
import socket
import signal
import sys
import subprocess
import stat
import struct
import tempfile

import kinds
import paths

MAX_SOURCE = 32 * 1024 * 1024
MAX_PNG = 8 * 1024 * 1024
WIDTH, HEIGHT = 1600, 1200


def export_candidate(candidate, temp_root=None):
    """Snapshot the exact listed regular file, convert privately, then erase it."""
    path = candidate['path']
    if candidate.get('kind') not in ('image', 'anim'):
        raise ValueError('not an image; use the ordinary peek.pick action for other files')
    with tempfile.TemporaryDirectory(prefix='peek-export-', dir=temp_root) as root:
        os.chmod(root, 0o700)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as source:
            st = os.fstat(source.fileno())
            if (not stat.S_ISREG(st.st_mode) or st.st_size > MAX_SOURCE
                    or signature(st) != candidate.get('signature')):
                raise ValueError('file changed or exceeds the source limit; pick again')
            suffix = kinds.ext(path)
            suffix = suffix if suffix in kinds.IMAGE | kinds.ANIM else 'bin'
            snapshot = os.path.join(root, 'source.' + suffix)
            with open(snapshot, 'xb') as target:
                os.chmod(snapshot, 0o600)
                count = 0
                while True:
                    chunk = source.read(65536)
                    if not chunk:
                        break
                    count += len(chunk)
                    if count > MAX_SOURCE:
                        raise ValueError('source size limit exceeded')
                    target.write(chunk)
            if signature(os.fstat(source.fileno())) != candidate['signature']:
                raise ValueError('file changed while copying; pick again')
        if kinds.classify(snapshot) not in ('image', 'anim'):
            raise ValueError('not an image; use the ordinary peek.pick action for other files')
        if suffix != 'png':
            raise ValueError('unsupported native raster format; only confined PNG decoding is supported')
        import native_raster
        data = native_raster.image_png(snapshot, root, WIDTH, HEIGHT)
        if not data or len(data) > MAX_PNG or data[:8] != b'\x89PNG\r\n\x1a\n' or len(data) < 24:
            raise ValueError('could not produce a bounded PNG; install an image converter on the server')
        w, h = struct.unpack('>II', data[16:24])
        if not 0 < w <= WIDTH or not 0 < h <= HEIGHT:
            raise ValueError('preview dimensions exceed the limit; install an image converter on the server')
        return data


def signature(st):
    return dict(dev=st.st_dev, ino=st.st_ino, size=st.st_size,
                mtime_ns=st.st_mtime_ns, ctime_ns=st.st_ctime_ns)


def candidates_from_text(text, cwd, bases=(), selected=False):
    hits = (paths.path_from_selection(text, cwd, bases) if selected else
            paths.find_paths(text, cwd, bases,
                             width=max((paths.display_width(r) for r in text.split('\n')), default=0)))
    found = {}
    for hit in hits:
        try:
            st = os.lstat(hit.path)
            if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_SOURCE:
                continue
            found[hit.path] = dict(path=hit.path, line=hit.line, row=hit.row,
                                  col=hit.col, kind=kinds.classify(hit.path),
                                  signature=signature(st))
        except OSError:
            continue
    return sorted(found.values(), key=lambda item: (item['row'], item['col']))


def server_cli(context, args):
    from native_protocol import run_bounded
    env = dict(os.environ)
    argv = [os.environ.get('HERDR_BIN_PATH') or 'herdr']
    if context.get('server_socket'):
        env['HERDR_SOCKET_PATH'] = context['server_socket']
    elif context.get('session'):
        argv += ['--session', context['session']]
    return run_bounded(argv + args, env=env, timeout=15, limit=1024 * 1024)


def read_selection(context):
    params = context.get('selection')
    if not isinstance(params, dict) or params.get('pane_id') != context.get('pane_id'):
        raise ValueError('selection does not belong to the focused pane')
    path = context.get('server_socket')
    if not path and not context.get('session'):
        path = os.environ.get('HERDR_SOCKET_PATH')
    if not path:
        # Selection RPC has no CLI in 0.9.1. Only its documented release socket
        # layout is supported here; custom/debug endpoints must supply a socket.
        root = os.path.join(os.environ.get('XDG_CONFIG_HOME') or os.path.expanduser('~/.config'), 'herdr')
        session = context.get('session')
        if session and session != 'default':
            import re
            if not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', session) or session in ('.', '..'):
                raise ValueError('invalid selection session')
            root = os.path.join(root, 'sessions', session)
        path = os.path.join(root, 'herdr.sock')
    data = bytearray()
    with socket.socket(socket.AF_UNIX) as conn:
        conn.settimeout(10)
        conn.connect(path)
        conn.sendall((json.dumps(dict(id='peek-selection', method='pane.selection.read', params=params)) + '\n').encode())
        while not data.endswith(b'\n'):
            chunk = conn.recv(65536)
            if not chunk:
                raise ValueError('selection response ended unexpectedly')
            data.extend(chunk)
            if len(data) > 1024 * 1024:
                raise ValueError('selection response exceeds the size limit')
    response = json.loads(data)
    result = response.get('result')
    if (response.get('id') != 'peek-selection' or not isinstance(result, dict)
            or result.get('pane_id') != params['pane_id'] or not isinstance(result.get('text'), str)):
        raise ValueError('selection unavailable or stale on this server; clear the selection to pick screen paths')
    return result['text']


def list_candidates(context, images_only=False):
    pane_id = context.get('pane_id')
    if not isinstance(pane_id, str) or not pane_id:
        raise ValueError('no focused pane in the client snapshot; focus a pane and retry')
    payload = json.loads(server_cli(context, ['pane', 'get', pane_id]))
    info = payload.get('result', payload).get('pane')
    if not isinstance(info, dict):
        raise ValueError('pane is unavailable on this endpoint')
    cwd = info.get('foreground_cwd') or info.get('cwd')
    if not isinstance(cwd, str) or not os.path.isabs(cwd):
        raise ValueError('pane working directory is unavailable')
    selected = context.get('selection') is not None
    if selected:
        text = read_selection(context)
    else:
        text = server_cli(context, ['pane', 'read', pane_id, '--source', 'visible', '--format', 'text']).decode('utf-8', 'replace')
    candidates = candidates_from_text(text, cwd, [info.get('cwd'), os.path.expanduser('~')], selected=selected)
    if images_only:
        candidates = [candidate for candidate in candidates if candidate['kind'] in ('image', 'anim')]
    return candidates[-256:]


def main():
    # This helper runs in a dedicated SSH/local child, never the plugin daemon.
    # Its deadline must expire before the laptop's transport budget does.
    def cancelled(signum, frame):
        raise ValueError('image export timed out or was cancelled')
    signals = (signal.SIGTERM, signal.SIGHUP, signal.SIGALRM)
    previous = {signum: signal.signal(signum, cancelled) for signum in signals}
    signal.alarm(60)
    try:
        return serve_request()
    finally:
        signal.alarm(0)
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def serve_request():
    from native_protocol import MAX_REQUEST, MAX_RESPONSE
    try:
        data = sys.stdin.buffer.read(MAX_REQUEST + 1)
        if len(data) > MAX_REQUEST:
            raise ValueError('request exceeds the size limit')
        request = json.loads(data)
        if not isinstance(request, dict) or request.get('version') != 1:
            raise ValueError('unsupported request version')
        context = request.get('context')
        if not isinstance(context, dict):
            raise ValueError('missing endpoint context')
        op = request.get('op')
        if op == 'list':
            response = dict(candidates=list_candidates(context, images_only=request.get('images_only') is True))
        elif op == 'export':
            candidate = request.get('candidate')
            if not isinstance(candidate, dict):
                raise ValueError('missing candidate')
            response = dict(png_base64=base64.b64encode(export_candidate(candidate)).decode('ascii'))
        else:
            raise ValueError('unsupported exporter operation')
        response.update(version=1, ok=True)
        encoded = json.dumps(response).encode()
        if len(encoded) > MAX_RESPONSE:
            raise ValueError('response exceeds the size limit')
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        encoded = json.dumps(dict(version=1, ok=False, error=str(exc)[:400])).encode()
    sys.stdout.buffer.write(encoded + b'\n')
    return 0
