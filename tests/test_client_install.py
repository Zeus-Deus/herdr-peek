"""Settings-only native opt-in: never invoke the live installer."""
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import tomllib
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'peek'))


def installer():
    assert importlib.util.find_spec('client_install'), 'missing client-only configuration helper'
    import client_install
    return client_install


class MergeTest(unittest.TestCase):
    def test_native_merge_refuses_deeply_nested_owned_metadata_without_proposal(self):
        helper = installer()
        main = '/local/peek/main.py'
        legacy = '[[keys.command]]\nkey="prefix+g"\ntype="plugin_action"\ncommand="peek.pick"\n'
        for owned, remove in ((legacy, False),
                              (helper.native_block('prefix+j', '/other/peek/main.py', 'pick'), False),
                              (helper.native_block('prefix+k', main, 'last'), True)):
            for metadata in ('metadata=[[{note="keep"}]]\n',
                             'metadata=[[[{details={note="keep"}}]]]\n'):
                source = owned + metadata
                with self.subTest(owned=owned, remove=remove, metadata=metadata), tempfile.TemporaryDirectory() as root:
                    tomllib.loads(source)  # These nested arrays are valid TOML.
                    path = Path(root, 'config.toml')
                    path.write_text(source)
                    before, identity = path.read_bytes(), path.stat()
                    with self.assertRaisesRegex(ValueError, 'unsupported.*nested'):
                        helper.apply_config(str(path), main, remove=remove)
                    self.assertEqual(path.read_bytes(), before)
                    after = path.stat()
                    for field in ('st_ino', 'st_mode', 'st_size', 'st_mtime_ns', 'st_ctime_ns'):
                        self.assertEqual(getattr(after, field), getattr(identity, field))
                    self.assertEqual(list(Path(root).iterdir()), [path], 'deep nested refusal wrote a proposal')

    def test_native_install_preserves_safe_nested_bindings_verbatim(self):
        helper = installer()
        main = '/local/peek/main.py'
        unrelated = '[[keys.command]]\nkey="prefix+x"\ntype="shell"\ncommand="keep"\n'
        other = '[[keys.client_command]]\nkey="prefix+y"\ncommand=["keep"]\n'
        for suffix in ('.metadata]\nnote="keep"\n',
                       '.metadata]]\nnote="keep"\n',
                       '."metadata".details]\nnote="keep"\n',
                       'inline', 'deep-inline'):
            def nested(family):
                if suffix == 'inline':
                    return 'metadata={note="keep"}\n'
                if suffix == 'deep-inline':
                    return 'metadata=[[{note="keep"}]]\n'
                return ('[[' if '.metadata]]' in suffix else '[') + 'keys.' + family + suffix
            for has_last in (False, True):
                with self.subTest(suffix=suffix, has_last=has_last):
                    source = (unrelated + nested('command') + other + nested('client_command') +
                              helper.native_block('prefix+g', main, 'pick') + nested('client_command'))
                    if has_last:
                        source += helper.native_block('prefix+k', main, 'last') + nested('client_command')
                    before = tomllib.loads(source)['keys']
                    merged = helper.merge_config(source, main)
                    after = tomllib.loads(merged)['keys']
                    self.assertTrue(merged.startswith(source), 'safe nested data changed')
                    self.assertEqual(after['command'], before['command'])
                    self.assertEqual(after['client_command'][:len(before['client_command'])], before['client_command'])
                    if has_last:
                        self.assertEqual(merged, source)
                    self.assertEqual(helper.merge_config(merged, main), merged)

    def test_native_merge_refuses_invalid_input_binding_families_without_proposal(self):
        helper = installer()
        for source in ('keys="not-a-table"\n',
                       '[keys.command.metadata]\nnote="keep"\n',
                       '[keys.client_command.metadata]\nnote="keep"\n',
                       '[keys]\ncommand="not-an-array"\n',
                       '[keys]\nclient_command="not-an-array"\n',
                       '[keys]\ncommand=[1]\n',
                       '[keys]\nclient_command=[1]\n'):
            for remove in (False, True):
                with self.subTest(source=source, remove=remove), tempfile.TemporaryDirectory() as root:
                    path = Path(root, 'config.toml')
                    path.write_text(source)
                    before, identity = path.read_bytes(), path.stat()
                    with self.assertRaisesRegex(ValueError, 'unsupported.*configuration'):
                        helper.apply_config(str(path), '/local/peek/main.py', remove=remove)
                    self.assertEqual(path.read_bytes(), before)
                    after = path.stat()
                    for field in ('st_ino', 'st_mode', 'st_size', 'st_mtime_ns', 'st_ctime_ns'):
                        self.assertEqual(getattr(after, field), getattr(identity, field))
                    self.assertEqual(list(Path(root).iterdir()), [path], 'input refusal wrote a proposal')

    def test_native_install_rejects_syntactically_valid_schema_corrupt_output(self):
        from unittest.mock import patch
        helper = installer()
        main = '/local/peek/main.py'
        legacy = '[[keys.command]]\nkey="prefix+g"\ntype="plugin_action"\ncommand="peek.pick"\n'
        source = legacy + helper.native_block('prefix+k', main, 'last')
        real_block = helper.native_block
        def detached_child(*args):
            return real_block(*args) + '[keys.command.metadata]\nnote="detached"\n'
        # The serialization fault recreates the original list-to-dict corruption,
        # independently of the input nested-layout guard and without mocking loads.
        bad_output = detached_child('prefix+g', main, 'pick') + helper.native_block('prefix+k', main, 'last')
        self.assertIsInstance(tomllib.loads(bad_output)['keys']['command'], dict)
        with tempfile.TemporaryDirectory() as root:
            path = Path(root, 'config.toml')
            path.write_text(source)
            before, identity = path.read_bytes(), path.stat()
            with patch.object(helper, 'native_block', side_effect=detached_child):
                with self.assertRaisesRegex(ValueError, 'unsupported keybinding configuration'):
                    helper.apply_config(str(path), main)
            self.assertEqual(path.read_bytes(), before)
            after = path.stat()
            for field in ('st_ino', 'st_mode', 'st_size', 'st_mtime_ns', 'st_ctime_ns'):
                self.assertEqual(getattr(after, field), getattr(identity, field))
            self.assertEqual(list(Path(root).iterdir()), [path], 'output refusal wrote a proposal')

    def test_native_merge_validates_result_binding_family_types_before_proposal(self):
        from unittest.mock import patch
        helper = installer()
        main = '/local/peek/main.py'
        legacy = '[[keys.command]]\nkey="prefix+g"\ntype="plugin_action"\ncommand="peek.pick"\n'
        source = '[ui]\ncopy_on_select=false\n' + legacy + helper.native_block('prefix+k', main, 'last')
        real_loads = tomllib.loads
        malformed = ('keys="not-a-table"\n',
                     '[keys.command.metadata]\nnote="detached"\n',
                     '[keys.client_command.metadata]\nnote="detached"\n',
                     '[keys]\ncommand="not-an-array"\n',
                     '[keys]\nclient_command="not-an-array"\n',
                     '[keys]\ncommand=[1]\n',
                     '[keys]\nclient_command=[1]\n')
        for remove in (False, True):
            expected = helper.merge_config(source, main, remove=remove)
            for bad_text in malformed:
                with self.subTest(remove=remove, malformed=bad_text):
                    seen = []
                    def validate(text):
                        # Inject a syntactically valid but schema-invalid parse only
                        # at the final result boundary; input and blocks stay real.
                        if text == expected:
                            seen.append(text)
                            return real_loads(bad_text)
                        return real_loads(text)
                    with tempfile.TemporaryDirectory() as root:
                        path = Path(root, 'config.toml')
                        path.write_text(source)
                        before, identity = path.read_bytes(), path.stat()
                        with patch.object(helper.tomllib, 'loads', side_effect=validate):
                            with self.assertRaisesRegex(ValueError, 'unsupported.*configuration'):
                                helper.apply_config(str(path), main, remove=remove)
                        self.assertEqual(seen, [expected])
                        self.assertEqual(path.read_bytes(), before)
                        after = path.stat()
                        for field in ('st_ino', 'st_mode', 'st_size', 'st_mtime_ns', 'st_ctime_ns'):
                            self.assertEqual(getattr(after, field), getattr(identity, field))
                        self.assertEqual(list(Path(root).iterdir()), [path], 'schema refusal wrote a proposal')

    def test_native_install_refuses_nested_bindings_being_rewritten_without_proposal(self):
        helper = installer()
        main = '/local/peek/main.py'
        legacy = '[[keys.command]]\nkey="prefix+g"\ntype="plugin_action"\ncommand="peek.pick"\n'
        foreign = helper.native_block('prefix+j', '/other/peek/main.py', 'pick')
        for family, owned in (('command', legacy), ('client_command', foreign)):
            unrelated = ('[[keys.command]]\nkey="prefix+x"\ntype="shell"\ncommand="keep"\n'
                         if family == 'command' else
                         '[[keys.client_command]]\nkey="prefix+x"\ncommand=["keep"]\n')
            for nested in ('[keys.' + family + '.metadata]\nnote="keep"\n',
                           '[[keys.' + family + '.metadata]]\nnote="keep"\n',
                           '["keys"."' + family + '"."metadata".details]\nnote="keep"\n',
                           'metadata = {note="keep"}\n'):
                for prefix in ('', unrelated):
                    source = prefix + owned + nested
                    with self.subTest(family=family, nested=nested, preceding=bool(prefix)):
                        self.assertIsInstance(tomllib.loads(source)['keys'][family], list)
                        with tempfile.TemporaryDirectory() as root:
                            path = Path(root, 'config.toml')
                            path.write_text(source)
                            path.chmod(0o600)
                            before, identity = path.read_bytes(), path.stat()
                            with self.assertRaisesRegex(ValueError, 'unsupported.*nested'):
                                helper.apply_config(str(path), main)
                            self.assertEqual(path.read_bytes(), before)
                            after = path.stat()
                            for field in ('st_ino', 'st_mode', 'st_size', 'st_mtime_ns', 'st_ctime_ns'):
                                self.assertEqual(getattr(after, field), getattr(identity, field))
                            self.assertEqual(list(Path(root).iterdir()), [path], 'refusal wrote a proposal')

    def test_native_uninstall_validates_result_before_creating_proposal(self):
        from unittest.mock import patch
        helper = installer()
        main = '/local/peek/main.py'
        legacy = '[[keys.command]]\nkey="prefix+g"\ntype="plugin_action"\ncommand="peek.pick"\n'
        other = helper.native_block('prefix+j', '/other/peek/main.py', 'pick')
        source = legacy + other + helper.native_block('prefix+k', main, 'last')
        expected = legacy + other
        real_loads = tomllib.loads
        def validate(text):
            parsed = real_loads(text)
            if text == expected:
                raise ValueError('result validation refused')
            return parsed
        with tempfile.TemporaryDirectory() as root:
            path = Path(root, 'config.toml')
            path.write_text(source)
            before = path.read_bytes()
            identity = path.stat()
            # Reject only the actual merged text, not input or individual blocks.
            with patch.object(helper.tomllib, 'loads', side_effect=validate):
                with self.assertRaisesRegex(ValueError, 'result validation refused'):
                    helper.apply_config(str(path), main, remove=True)
            self.assertEqual(path.read_bytes(), before)
            after = path.stat()
            for field in ('st_ino', 'st_mode', 'st_size', 'st_mtime_ns', 'st_ctime_ns'):
                self.assertEqual(getattr(after, field), getattr(identity, field))
            self.assertEqual(list(Path(root).iterdir()), [path], 'validation failed after writing proposal')

    def test_native_uninstall_refuses_owned_nested_tables_without_proposal(self):
        helper = installer()
        main = '/local/🖼/peek/main.py'
        owned = helper.native_block('prefix+k', main, 'last')
        other = helper.native_block('prefix+j', '/other/peek/main.py', 'pick')
        legacy = '[[keys.command]]\nkey="prefix+g"\ntype="plugin_action"\ncommand="peek.pick"\n'
        for nested in ('[keys.client_command.metadata]\nnote="keep"\n',
                       '[[keys.client_command.metadata]]\nnote="keep"\n',
                       '["keys"."client_command"."metadata".details]\nnote="keep"\n'):
            for source in (legacy + owned + nested + other, legacy + other + owned + nested):
                with self.subTest(nested=nested, owned_last=source.endswith(nested)):
                    parsed = tomllib.loads(source)  # Regression input is valid TOML.
                    self.assertEqual(len(parsed['keys']['client_command']), 2)
                    with tempfile.TemporaryDirectory() as root:
                        path = Path(root, 'config.toml')
                        path.write_text(source, encoding='utf-8')
                        path.chmod(0o600)
                        before = path.read_bytes()
                        identity = path.stat()
                        with self.assertRaisesRegex(ValueError, 'unsupported.*nested'):
                            helper.apply_config(str(path), main, remove=True)
                        self.assertEqual(path.read_bytes(), before)
                        after = path.stat()
                        for field in ('st_ino', 'st_mode', 'st_size', 'st_mtime_ns', 'st_ctime_ns'):
                            self.assertEqual(getattr(after, field), getattr(identity, field))
                        self.assertEqual(list(Path(root).iterdir()), [path], 'refusal wrote a proposal')
        # Nested data on an unrelated installation must not block narrow removal.
        unrelated = other + '[keys.client_command.metadata]\nnote="keep"\n'
        source = legacy + unrelated + owned
        self.assertEqual(helper.merge_config(source, main, remove=True), legacy + unrelated)

    def test_native_uninstall_preserves_legacy_server_actions_and_other_installations(self):
        helper = installer()
        legacy = '[[keys.command]]\nkey="prefix+g"\ntype="plugin_action"\ncommand="peek.pick"\n'
        other = helper.native_block('prefix+j', '/other/peek/main.py', 'pick')
        owned = helper.native_block('prefix+k', '/local/peek/main.py', 'last')
        removed = helper.merge_config(legacy + other + owned, '/local/peek/main.py', remove=True)
        self.assertEqual(removed, legacy + other)

    def test_non_bmp_entrypoint_round_trips_as_valid_toml(self):
        main = '/local/🖼/peek/main.py'
        try:
            merged = installer().merge_config('', main)
            parsed = tomllib.loads(merged)
        except tomllib.TOMLDecodeError as exc:
            self.fail('non-BMP path serialized as invalid TOML: ' + str(exc))
        self.assertEqual(parsed['keys']['client_command'][0]['command'][1], main)
        self.assertNotIn('\\ud83d', merged)

    def test_adds_missing_native_keys_without_overwriting_unrelated_shortcuts(self):
        merged = installer().merge_config('[ui]\ncopy_on_select = false\n', '/local/peek/main.py')
        parsed = tomllib.loads(merged)
        self.assertIn('keys', parsed, 'missing native key defaults')
        self.assertEqual([item['key'] for item in parsed['keys']['client_command']], ['prefix+f', 'prefix+shift+f'])
        occupied = '[[keys.command]]\nkey="prefix+f"\ntype="shell"\ncommand="unrelated"\n'
        with self.assertRaisesRegex(ValueError, 'occupied'):
            installer().merge_config(occupied, '/local/peek/main.py')

    def test_multiline_preferences_containing_fake_tables_are_unchanged(self):
        source = '''custom = """
[[keys.command]]
key = "not-a-binding"
type = "plugin_action"
command = "peek.pick"
"""
[ui]
copy_on_select = false
'''
        try:
            merged = installer().merge_config(source, '/local/peek/main.py')
        except ValueError as exc:
            self.fail('valid multiline preferences were treated as table headers: ' + str(exc))
        self.assertTrue(merged.startswith(source))
        self.assertEqual(tomllib.loads(merged)['custom'], tomllib.loads(source)['custom'])

    def test_existing_config_is_never_replaced_even_if_editor_deletes_or_changes_it(self):
        from unittest.mock import patch
        helper = installer()
        for operation in ('edit', 'delete', 'replace', 'symlink'):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as root:
                path = Path(root, 'config.toml')
                path.write_text('[ui]\ncopy_on_select=false\n')
                original_merge = helper.merge_config
                other = Path(root, 'other.toml')
                other.write_text('# unrelated\n')
                def merge(*args, **kwargs):
                    result = original_merge(*args, **kwargs)
                    if operation == 'edit':
                        path.write_text('# editor changed this\n')
                    else:
                        path.unlink()
                        if operation == 'replace':
                            path.write_text('# replacement\n')
                        elif operation == 'symlink':
                            path.symlink_to(other)
                    return result
                with patch.object(helper, 'merge_config', merge):
                    proposal = helper.apply_config(str(path), '/local/peek/main.py')
                self.assertIsNotNone(proposal)
                self.assertIn('client_command', tomllib.loads(Path(proposal).read_text())['keys'])
                if operation == 'delete':
                    self.assertFalse(path.exists(), 'installer resurrected a deleted config')
                elif operation == 'symlink':
                    self.assertTrue(path.is_symlink(), 'installer replaced editor symlink')
                    self.assertEqual(other.read_text(), '# unrelated\n')
                else:
                    self.assertEqual(path.read_text(), '# editor changed this\n' if operation == 'edit' else '# replacement\n')

    def test_original_snapshot_never_uses_replace_at_the_final_editor_boundary(self):
        from unittest.mock import patch
        helper = installer()
        with tempfile.TemporaryDirectory() as root:
            path = Path(root, 'config.toml')
            path.write_text('# initial\n')
            def editor_then_replace(source, destination):
                path.write_text('# last-moment editor save\n')
                os.rename(source, destination)
            with patch.object(helper.os, 'replace', side_effect=editor_then_replace) as replacement:
                helper.apply_config(str(path), '/local/peek/main.py')
            self.assertEqual(replacement.call_count, 0, 'existing config replacement cannot cooperate with arbitrary editors')
            self.assertEqual(path.read_text(), '# initial\n')

    def test_missing_config_create_refuses_intervening_empty_file(self):
        from unittest.mock import patch
        helper = installer()
        with tempfile.TemporaryDirectory() as root:
            path = Path(root, 'config.toml')
            original_merge = helper.merge_config
            def concurrent_create(*args, **kwargs):
                result = original_merge(*args, **kwargs)
                path.write_bytes(b'')
                return result
            with patch.object(helper, 'merge_config', concurrent_create):
                with self.assertRaises(FileExistsError):
                    helper.apply_config(str(path), '/local/peek/main.py')
            self.assertEqual(path.read_bytes(), b'')

    def test_existing_config_proposal_is_private_and_adoption_is_explicit(self):
        helper = installer()
        with tempfile.TemporaryDirectory() as root:
            path = Path(root, 'config.toml')
            before = '# keep\n[ui]\ncopy_on_select=false\n'
            path.write_text(before)
            path.chmod(0o600)
            mtime = path.stat().st_mtime_ns
            proposal = Path(helper.apply_config(str(path), '/local/peek/main.py'))
            self.assertEqual(path.read_text(), before)
            self.assertEqual(path.stat().st_mtime_ns, mtime)
            self.assertEqual((proposal.parent / 'baseline.toml').read_text(), before)
            self.assertEqual(stat.S_IMODE(proposal.parent.stat().st_mode), 0o700)
            for item in proposal.parent.iterdir():
                self.assertEqual(stat.S_IMODE(item.stat().st_mode), 0o600)
            metadata = json.loads((proposal.parent / 'adoption.json').read_text())
            self.assertTrue(metadata['manual_adoption_required'])
            self.assertEqual(metadata['target'], str(path))
            # The fixture deliberately adopts the reviewed bytes, not the installer.
            path.write_bytes(proposal.read_bytes())
            mtime = path.stat().st_mtime_ns
            self.assertIsNone(helper.apply_config(str(path), '/local/peek/main.py'))
            self.assertEqual(path.stat().st_mtime_ns, mtime)

    def test_core_capability_probe_refuses_installed_old_core_without_live_config_writes(self):
        helper = installer()
        self.assertTrue(hasattr(helper, 'check_compatible'), 'missing core capability gate')
        import shutil
        with self.assertRaisesRegex(ValueError, 'compatible'):
            helper.check_compatible(shutil.which('herdr'))

    def test_native_uninstall_removes_only_owned_pick_last_tables(self):
        import inspect
        helper = installer()
        self.assertIn('remove', inspect.signature(helper.merge_config).parameters, 'missing narrow uninstall')
        source = '[[keys.command]]\nkey="prefix+h"\ntype="plugin_action"\ncommand="peek.open"\n'
        native = helper.merge_config(source, '/local/peek/main.py')
        removed = helper.merge_config(native, '/local/peek/main.py', remove=True)
        self.assertEqual(removed, source + '\n')
        self.assertEqual(helper.merge_config(removed, '/local/peek/main.py', remove=True), removed)

    def test_unrecognized_inline_bindings_are_not_overwritten_or_duplicated(self):
        for command in ('unrelated', 'peek.pick'):
            source = '[keys]\ncommand=[{key="prefix+f", type="plugin_action", command=' + json.dumps(command) + '}]\n'
            with self.subTest(command=command), self.assertRaises(ValueError):
                installer().merge_config(source, '/local/peek/main.py')

    def test_replaces_only_peek_pick_last_preserving_other_config_and_safe_argv(self):
        source = '''# private preferences\nonboarding = false\n[ui]\ncopy_on_select = false\n
[[keys.command]]
key = "prefix+x"
type = "shell"
command = "printf untouched"

[[keys.command]]
key = "prefix+g"
type = "plugin_action"
command = "peek.pick"

[[keys.command]]
key = "prefix+shift+f"
type = "plugin_action"
command = "peek.last"

[[keys.command]]
key = "prefix+h"
type = "plugin_action"
command = "peek.open"
'''
        path = '/local/plugin "quoted"/peek/main.py'
        merged = installer().merge_config(source, path)
        parsed = tomllib.loads(merged)
        self.assertEqual(parsed['ui'], {'copy_on_select': False})
        self.assertEqual([c['command'] for c in parsed['keys']['command']], ['printf untouched', 'peek.open'])
        self.assertIn('command = "printf untouched"', merged)
        commands = parsed['keys']['client_command']
        self.assertEqual([c['key'] for c in commands], ['prefix+g', 'prefix+shift+f'])
        self.assertEqual(commands[0]['command'], ['python3', path, 'client-pick'])
        self.assertEqual(commands[1]['command'], ['python3', path, 'client-last'])
        self.assertEqual(installer().merge_config(merged, path), merged)


class SettingsOnlyEntrypointTest(unittest.TestCase):
    def test_opt_in_shell_installer_writes_only_private_keys_using_installed_root(self):
        import subprocess
        with tempfile.TemporaryDirectory() as root:
            home = Path(root, 'home')
            config_dir = home / '.config/herdr'
            config_dir.mkdir(parents=True)
            config = config_dir / 'config.toml'
            before = '[ui]\ncopy_on_select=false\n[[keys.command]]\nkey="prefix+x"\ntype="shell"\ncommand="untouched"\n'
            config.write_text(before)
            config.chmod(0o600)
            tool = Path(root, 'herdr')
            repo = str(Path(__file__).resolve().parents[1])
            log = Path(root, 'calls.jsonl')
            tool.write_text('#!' + sys.executable + '\nimport json,os,sys,tomllib\nfrom pathlib import Path\n'
                            'with open(' + repr(str(log)) + ',"a") as f: f.write(json.dumps({"argv":sys.argv[1:],"home":os.environ["HOME"]})+"\\n")\n'
                            'args=sys.argv[1:]\n'
                            'if args[:2]==["plugin","list"]: print(json.dumps({"result":{"plugins":[{"plugin_id":"peek","enabled":True,"plugin_root":' + repr(repo) + '}]}}))\n'
                            'elif args==["config","check"]:\n'
                            ' p=Path(os.environ.get("HERDR_CONFIG_PATH",' + repr(str(config)) + ')); d=tomllib.loads(p.read_text()); cs=d.get("keys",{}).get("client_command",[])\n'
                            ' if any(not isinstance(c["command"],list) for c in cs): print("invalid type string, expected a sequence"); sys.exit(1)\n'
                            'else: assert args==["server","reload-config"], args\n')
            tool.chmod(0o700)
            env = {k:v for k,v in os.environ.items() if not k.startswith(('HERMES_', 'HERDR_'))}
            env.update(HOME=str(home), XDG_CONFIG_HOME=str(home / '.config'), XDG_STATE_HOME=str(home / 'state'),
                       XDG_CACHE_HOME=str(home / 'cache'), XDG_DATA_HOME=str(home / 'data'), XDG_RUNTIME_DIR=str(home),
                       HERDR_SOCKET_PATH=str(Path(root, 'api')), HERDR_CLIENT_SOCKET_PATH=str(Path(root, 'client')),
                       HERDR_BIN_PATH=str(tool), TMPDIR=root)
            script = str(Path(repo, 'install.sh'))
            argv = ['sh', script, '--client-native', '--keys-only']
            proc = subprocess.run(argv, env=env, capture_output=True, timeout=15)
            self.assertEqual(proc.returncode, 0, proc.stderr.decode())
            self.assertIn('Manual adoption required', proc.stdout.decode())
            self.assertEqual(config.read_text(), before, 'proposal must not modify live original')
            proposal = next(config_dir.glob('.peek-proposal-*/config.toml'))
            config.write_bytes(proposal.read_bytes())  # explicit reviewed fixture adoption
            parsed = tomllib.loads(config.read_text())
            self.assertEqual(parsed['keys']['command'][0]['command'], 'untouched')
            self.assertEqual(parsed['keys']['client_command'][0]['command'], ['python3', str(Path(repo,'peek/main.py')), 'client-pick'])
            first = config.read_bytes()
            mtime = config.stat().st_mtime_ns
            normal = subprocess.run(['sh', script, '--keys-only'], env=env, capture_output=True, timeout=15)
            self.assertEqual(normal.returncode, 0, normal.stderr.decode())
            self.assertEqual(config.read_bytes(), first, 'ordinary updates must preserve opted-in native keys')
            self.assertEqual(config.stat().st_mtime_ns, mtime)
            proc = subprocess.run(argv, env=env, capture_output=True, timeout=15)
            self.assertEqual(proc.returncode, 0, proc.stderr.decode())
            self.assertEqual(config.read_bytes(), first)
            self.assertEqual(config.stat().st_mtime_ns, mtime)
            proc = subprocess.run(argv + ['--uninstall'], env=env, capture_output=True, timeout=15)
            self.assertEqual(proc.returncode, 0, proc.stderr.decode())
            self.assertIn('Manual adoption required', proc.stdout.decode())
            self.assertEqual(config.read_bytes(), first, 'uninstall must also propose, never replace')
            proposals = sorted(config_dir.glob('.peek-proposal-*/config.toml'), key=lambda p: p.stat().st_mtime_ns)
            config.write_bytes(proposals[-1].read_bytes())  # explicit fixture adoption
            self.assertNotIn('client_command', tomllib.loads(config.read_text())['keys'])
            self.assertIn('command="untouched"', config.read_text())
            calls = [json.loads(line)['argv'] for line in log.read_text().splitlines()]
            self.assertFalse(any(call[:2] in [['plugin','install'], ['plugin','uninstall'], ['server','stop']] for call in calls))


if __name__ == '__main__':
    unittest.main()
