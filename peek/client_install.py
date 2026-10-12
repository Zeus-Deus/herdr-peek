"""Opt-in client-native key configuration; all other settings remain untouched."""
import json
import os
import re
import stat
import subprocess
import tempfile
import tomllib

HEADER = re.compile(r'^[ \t]*\[\[keys\.(client_command|command)\]\][ \t]*(?:#.*)?$')


def table_sections(text):
    """Find real table boundaries, not headers embedded in TOML strings."""
    boundaries = []
    quote = None
    offset = 0
    for line in text.splitlines(keepends=True):
        if quote is None and re.match(r'^[ \t]*\[', line):
            boundaries.append((offset, line.rstrip('\r\n')))
        index = 0
        while index < len(line):
            char = line[index]
            if quote:
                if quote[0] == '"' and char == '\\':
                    index += 2
                    continue
                if line.startswith(quote, index):
                    if len(quote) == 3:
                        while index < len(line) and line[index] == quote[0]:
                            index += 1
                    else:
                        index += 1
                    quote = None
                    continue
            elif char == '#':
                break
            elif char in ('"', "'"):
                quote = char * 3 if line.startswith(char * 3, index) else char
                index += len(quote)
                continue
            index += 1
        offset += len(line)
    for index, (start, header) in enumerate(boundaries):
        stop = boundaries[index + 1][0] if index + 1 < len(boundaries) else len(text)
        yield start, stop, header


def native_block(key, main, action):
    return ('[[keys.client_command]]\nkey = ' + json.dumps(key, ensure_ascii=False) + '\ncommand = ' +
            json.dumps(['python3', main, 'client-' + action], ensure_ascii=False) + '\n\n')


def peek_action(entry, family):
    if (family == 'command' and entry.get('type') == 'plugin_action'
            and entry.get('command') in ('peek.pick', 'peek.last')):
        return entry['command'].split('.')[1]
    argv = entry.get('command')
    if (family == 'client_command' and isinstance(argv, list) and len(argv) == 3
            and argv[0] == 'python3' and isinstance(argv[1], str)
            and argv[1].endswith('/peek/main.py') and argv[2] in ('client-pick', 'client-last')):
        return argv[2].split('-')[1]
    return None


def binding_keys(text):
    """Validate the binding families, not just TOML syntax, before any writes."""
    parsed = tomllib.loads(text)
    keys = parsed.get('keys', {})
    if not isinstance(keys, dict):
        raise ValueError('unsupported keys configuration')
    for family in ('command', 'client_command'):
        entries = keys.get(family, [])
        if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
            raise ValueError('unsupported keybinding configuration')
    return keys


def merge_config(text, main, remove=False):
    if not os.path.isabs(main):
        raise ValueError('native entrypoint must be an absolute local path')
    keys = binding_keys(text)
    def nested(value):
        return isinstance(value, dict) or (isinstance(value, list) and
                                          any(nested(item) for item in value))

    def owned_action(entry, family):
        action = peek_action(entry, family)
        if remove and (family != 'client_command' or
                       entry.get('command') != ['python3', main, 'client-' + str(action)]):
            return None
        return action

    actions = set()
    occupied = set()
    expected, replaced = 0, 0
    for family in ('command', 'client_command'):
        entries = keys.get(family, [])
        occupied.update(entry.get('key') for entry in entries)
        for entry in entries:
            action = owned_action(entry, family)
            rewrite = action and (remove or family != 'client_command' or
                                  entry.get('command') != ['python3', main, 'client-' + action])
            if rewrite and any(nested(value) for value in entry.values()):
                # Detached child headers can corrupt or reattach to another array element.
                raise ValueError('unsupported nested native Peek binding layout; ' +
                                 ('uninstall manually' if remove else 'migrate manually'))
        expected += sum(owned_action(entry, family) is not None for entry in entries)
    parts = []
    end = 0
    for start, stop, header in table_sections(text):
        match = HEADER.fullmatch(header)
        if not match:
            continue
        parts.append(text[end:start])
        block = text[start:stop]
        family = match.group(1)
        entry = tomllib.loads(block)['keys'][family][0]
        action = owned_action(entry, family)
        if action:
            replaced += 1
            actions.add(action)
            if remove:
                end = stop
                continue
            desired = ['python3', main, 'client-' + action]
            parts.append(block if family == 'client_command' and entry.get('command') == desired
                         else native_block(entry['key'], main, action))
        else:
            parts.append(block)
        end = stop
    parts.append(text[end:])
    if replaced != expected:
        raise ValueError('unsupported Peek binding layout; use explicit [[keys.command]]/[[keys.client_command]] tables')
    merged = ''.join(parts)
    if remove:
        binding_keys(merged)
        return merged
    for action, key in [('pick', 'prefix+f'), ('last', 'prefix+shift+f')]:
        if action not in actions:
            if key in occupied:
                raise ValueError('native Peek shortcut is occupied: ' + key)
            merged += '\n' + native_block(key, main, action)
    binding_keys(merged)
    return merged


def check_compatible(herdr):
    """Probe argv table deserialization in a wholly private config, not a version guess."""
    with tempfile.TemporaryDirectory(prefix='peek-core-') as root:
        env = {key: value for key, value in os.environ.items() if not key.startswith('HERDR_')}
        env.update(HOME=root, XDG_CONFIG_HOME=root + '/c', XDG_DATA_HOME=root + '/d',
                   XDG_STATE_HOME=root + '/s', XDG_CACHE_HOME=root + '/cache', XDG_RUNTIME_DIR=root,
                   HERDR_CONFIG_PATH=root + '/probe.toml', HERDR_SOCKET_PATH=root + '/api',
                   HERDR_CLIENT_SOCKET_PATH=root + '/client')
        path = env['HERDR_CONFIG_PATH']
        with open(path, 'w') as target:
            target.write('[[keys.client_command]]\nkey="prefix+f"\ncommand="invalid-not-an-argv-array"\n')
        probe = subprocess.run([herdr, 'config', 'check'], env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=10)
        diagnostic = probe.stdout.decode('utf-8', 'replace').lower()
        if not probe.returncode or not re.search(r'invalid type|expected.*(?:sequence|array)', diagnostic):
            raise ValueError('--client-native requires an installed compatible Herdr client with [[keys.client_command]] argv support')
        with open(path, 'w') as target:
            target.write(native_block('prefix+f', '/probe/peek/main.py', 'pick'))
        probe = subprocess.run([herdr, 'config', 'check'], env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=10)
        if probe.returncode:
            raise ValueError('compatible client argv table rejected: ' + probe.stdout.decode('utf-8', 'replace')[:400])


def apply_config(path, main, remove=False):
    """Existing files are proposal-only: arbitrary editors do not share a lock.

    The proposal is based on the captured baseline, NOT a promise that the live
    path still has that identity. Manual adoption must review any newer edits.
    An absent config may be created exclusively; it is never replaced.
    """
    import hashlib
    path = os.path.abspath(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        before, identity = b'', None
    else:
        with os.fdopen(fd, 'rb') as source:
            st = os.fstat(source.fileno())
            if not stat.S_ISREG(st.st_mode) or st.st_size > 4 * 1024 * 1024:
                raise ValueError('refusing a nonregular or oversized config')
            before = source.read(4 * 1024 * 1024 + 1)
            after_read = os.fstat(source.fileno())
            fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
            if (len(before) > 4 * 1024 * 1024 or
                    any(getattr(st, field) != getattr(after_read, field) for field in fields)):
                raise ValueError('config changed while reading; no update performed')
            identity = {field: getattr(st, field) for field in fields}
    after = merge_config(before.decode('utf-8'), main, remove=remove).encode('utf-8')
    if before == after:
        return None
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    if identity is None:
        # O_EXCL atomically refuses an intervening create, including symlinks.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as target:
            target.write(after)
            target.flush()
            os.fsync(target.fileno())
        return None
    root = tempfile.mkdtemp(prefix='.peek-proposal-', dir=parent)
    proposal = os.path.join(root, 'config.toml')
    metadata = dict(target=path, baseline_sha256=hashlib.sha256(before).hexdigest(),
                    baseline_identity=identity, manual_adoption_required=True)
    for name, content in ((proposal, after), (os.path.join(root, 'baseline.toml'), before),
                          (os.path.join(root, 'adoption.json'), json.dumps(metadata, indent=2).encode())):
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as target:
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
    return proposal


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--herdr', required=True)
    parser.add_argument('--config', required=True)
    parser.add_argument('--uninstall', action='store_true')
    args = parser.parse_args(argv)
    entrypoint = os.path.abspath(os.path.join(os.path.dirname(__file__), 'main.py'))
    try:
        if not args.uninstall:
            check_compatible(args.herdr)
        proposal = apply_config(args.config, entrypoint, remove=args.uninstall)
        if proposal:
            print('Manual adoption required; original config untouched; merged proposal: ' + proposal)
        else:
            print('Peek client bindings unchanged or new config created exclusively')
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        import sys
        print('peek: ' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    import sys
    sys.exit(main())
