"""Laptop-owned picker, SSH transport and foreground temporary image viewer."""
import base64
import configparser
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile

from native_protocol import MAX_REQUEST, decode_response, run_bounded


def desktop_argv(entry, path):
    error = 'unsupported default image association: foreground imv %F/%f is required (no launcher or single-instance apps)'
    argv = shlex.split(entry.get('Exec', ''))
    if (entry.get('Type') != 'Application'
            or any(entry.get(key, 'false').lower() != 'false'
                   for key in ('Terminal', 'DBusActivatable', 'Hidden'))
            or len(argv) != 2 or argv[1] not in ('%F', '%f')
            or os.path.basename(argv[0]) not in ('imv', 'imv-wayland', 'imv-x11')
            or (os.path.dirname(argv[0]) and not os.path.isabs(argv[0]))):
        raise ValueError(error)
    return [argv[0], path]


def load_context(raw):
    if not raw or len(raw.encode()) > MAX_REQUEST:
        raise ValueError('missing or oversized HERDR_CLIENT_CONTEXT_JSON; a compatible client is required')
    context = json.loads(raw)
    if (not isinstance(context, dict) or type(context.get('version')) is not int
            or context['version'] != 1 or not isinstance(context.get('endpoint_id'), str)
            or not context['endpoint_id']):
        raise ValueError('invalid client endpoint context version')
    for key in ('machine_id', 'ssh_target', 'session', 'server_socket', 'workspace_id', 'tab_id', 'pane_id'):
        value = context.get(key)
        if value is not None and (not isinstance(value, str) or not value or '\x00' in value):
            raise ValueError('invalid client context field: ' + key)
    if context.get('machine_id') and not context.get('ssh_target'):
        raise ValueError('saved remote endpoint has no SSH target; refusing a local fallback')
    return context


def resolve_viewer():
    desktop_id = run_bounded(['xdg-mime', 'query', 'default', 'image/png'],
                             timeout=5, limit=4096).decode().strip()
    if not re.fullmatch(r'[A-Za-z0-9_.-]+\.desktop', desktop_id):
        raise ValueError('default PNG desktop association is missing or invalid')
    directories = [os.environ.get('XDG_DATA_HOME') or os.path.expanduser('~/.local/share')]
    directories += (os.environ.get('XDG_DATA_DIRS') or '/usr/local/share:/usr/share').split(':')
    for directory in directories:
        if not os.path.isabs(directory):
            continue
        path = os.path.join(directory, 'applications', desktop_id)
        try:
            with open(path, encoding='utf-8') as source:
                text = source.read(65537)
        except FileNotFoundError:
            continue
        if len(text) > 65536:
            raise ValueError('desktop association is too large')
        parser = configparser.ConfigParser(interpolation=None)
        parser.optionxform = str
        parser.read_string(text)
        if not parser.has_section('Desktop Entry'):
            raise ValueError('invalid default desktop association')
        entry = dict(parser['Desktop Entry'])
        argv = desktop_argv(entry, '/unused.png')
        executable = shutil.which(argv[0])
        if not executable:
            raise ValueError('default foreground image viewer is not installed')
        return [executable]
    raise ValueError('cannot find the default PNG desktop association')


BOOTSTRAP = r'''
import io,json,os,selectors,subprocess,sys,time
try:
    data=sys.stdin.buffer.read(65537)
    if len(data)>65536: raise ValueError('request exceeds the size limit')
    req=json.loads(data)
    if not isinstance(req,dict) or req.get('version')!=1: raise ValueError('unsupported request')
    ctx=req.get('context',{})
    argv=['herdr']
    if ctx.get('session'): argv+=['--session',ctx['session']]
    argv+=['plugin','list','--json','--plugin','peek']
    p=subprocess.Popen(argv,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
    out=bytearray(); end=time.monotonic()+15
    try:
        with selectors.DefaultSelector() as sel:
            sel.register(p.stdout,selectors.EVENT_READ)
            while sel.get_map():
                if time.monotonic()>end: raise ValueError('plugin discovery timed out')
                for key,_ in sel.select(.1):
                    chunk=os.read(key.fileobj.fileno(),65536)
                    if not chunk: sel.unregister(key.fileobj)
                    out.extend(chunk)
                    if len(out)>1048576: raise ValueError('plugin discovery exceeds size limit')
        if p.wait(timeout=max(.01,end-time.monotonic())): raise ValueError('cannot discover Peek on the server')
    finally:
        if p.poll() is None: p.kill(); p.wait()
        p.stdout.close()
    found=json.loads(out)
    found=found.get('result',found).get('plugins',[])
    roots=[item['plugin_root'] for item in found if item.get('plugin_id')=='peek' and item.get('enabled') is True]
    if len(roots)!=1 or not os.path.isabs(roots[0]): raise ValueError('enabled Peek installation not found')
    root=os.path.join(roots[0],'peek')
    if not os.path.isfile(os.path.join(root,'native_server.py')): raise ValueError('update Peek on the server: native exporter is not installed')
    sys.path.insert(0,root)
    import native_server
    sys.stdin=io.TextIOWrapper(io.BytesIO(data),encoding='utf-8')
    native_server.main()
except Exception as exc:
    print(json.dumps({'version':1,'ok':False,'error':str(exc)[:400]}))
'''


def transport_argv(context):
    target = context.get('ssh_target')
    if target:
        # Also protect trusted SSH ProxyCommand/LocalCommand %h expansion:
        # a data argv destination must not contain shell metacharacters.
        if target.startswith('-') or not re.fullmatch(r'[A-Za-z0-9_.@:%+\[\]-]+', target):
            raise ValueError('invalid SSH target; use an ordinary host/user@host or SSH config alias')
        return ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
                '-o', 'UpdateHostKeys=no', '-o', 'ConnectTimeout=10',
                '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=2',
                '--', target, 'python3 -c ' + shlex.quote(BOOTSTRAP)]
    return [sys.executable, os.path.join(os.path.dirname(__file__), 'main.py'), 'native-server']


def fetch(context, op, candidate=None, images_only=False):
    server_context = dict(context)
    if context.get('ssh_target'):
        server_context.pop('server_socket', None)  # a laptop socket is never meaningful on the remote host
    request = dict(version=1, op=op, context=server_context, images_only=images_only)
    if candidate is not None:
        request['candidate'] = candidate
    env = dict(os.environ)
    if not context.get('ssh_target') and not context.get('server_socket'):
        env.pop('HERDR_SOCKET_PATH', None)
    return decode_response(run_bounded(transport_argv(context), json.dumps(request).encode(), env=env))


def view_temporary(data, viewer):
    from native_protocol import validate_png
    validate_png(data)
    with tempfile.TemporaryDirectory(prefix='peek-preview-') as root:
        os.chmod(root, 0o700)
        path = os.path.join(root, 'preview.png')
        with open(path, 'xb') as target:
            os.chmod(path, 0o600)
            target.write(data)
        with subprocess.Popen(viewer + [path], stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, start_new_session=True) as proc:
            try:
                code = proc.wait()
            finally:
                if proc.poll() is None:
                    os.killpg(proc.pid, signal.SIGTERM)
                    try:
                        proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        os.killpg(proc.pid, signal.SIGKILL)
                        proc.wait()
        if code:
            raise ValueError('foreground image viewer failed (exit %s)' % code)


def show_error(message):
    sys.stderr.write('peek: ' + message + '\n')
    if shutil.which('zenity'):
        try:
            run_bounded(['zenity', '--error', '--no-markup', '--title=Peek', '--text=' + message], timeout=60, limit=4096)
        except (OSError, ValueError, subprocess.SubprocessError):
            pass


def pick_candidate(candidates):
    argv = ['zenity', '--list', '--no-markup', '--title=Peek: screen paths',
            '--column=ID', '--column=File', '--column=Kind', '--hide-column=1', '--print-column=1',
            '--width=900', '--height=500']
    for index, item in enumerate(candidates):
        label = ''.join(char if char.isprintable() else ' ' for char in item['path'])
        argv += [str(index), label, item['kind']]
    proc = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode == 1:
        return None
    answer = proc.stdout.decode().strip()
    if proc.returncode or not re.fullmatch(r'[0-9]{1,3}', answer) or int(answer) >= len(candidates):
        raise ValueError('native picker failed or returned an invalid choice')
    return candidates[int(answer)]


def main(command):
    def cancel(signum, frame):
        raise KeyboardInterrupt
    previous = signal.signal(signal.SIGTERM, cancel)
    try:
        return run_client(command)
    except KeyboardInterrupt:
        return 130
    finally:
        signal.signal(signal.SIGTERM, previous)


def run_client(command):
    try:
        context = load_context(os.environ.get('HERDR_CLIENT_CONTEXT_JSON'))
        if command == 'client-last':
            context.pop('selection', None)
        candidates = fetch(context, 'list', images_only=command == 'client-last')['candidates']
        images = [item for item in candidates if item.get('kind') in ('image', 'anim')]
        if not images:
            raise ValueError('no images on screen; use ordinary peek.pick/peek.last for text and other files')
        if command == 'client-pick':
            if not candidates:
                raise ValueError('no file paths on screen')
            candidate = pick_candidate(candidates)
            if candidate is None:
                return 0
            if candidate.get('kind') not in ('image', 'anim'):
                raise ValueError('native previews are images only; use ordinary peek.pick/peek.last for this file (no server desktop launch)')
        else:
            candidate = images[-1]
        viewer = resolve_viewer()
        response = fetch(context, 'export', candidate)
        data = base64.b64decode(response['png_base64'], validate=True)
        view_temporary(data, viewer)
        return 0
    except (OSError, ValueError, KeyError, TypeError, configparser.Error, subprocess.SubprocessError) as exc:
        show_error(str(exc)[:400])
        return 1
