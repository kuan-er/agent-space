import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from anet_setup import binary, backend, native
from anet_setup import __main__ as cli


class ResolverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / 'node'
        self.home = self.base / 'home'
        self.home.mkdir()
        self.name = 'anet.exe' if os.name == 'nt' else 'anet'
        self.old = self.root / 'bin' / self.name
        self.old.parent.mkdir(parents=True)
        self.old.write_bytes(b'old MCP implementation')
        self.data = b'current MCP implementation fixture; never executed'
        self.sha = hashlib.sha256(self.data).hexdigest()
        self.current = self.root / 'maintenance' / 'versions' / self.sha / self.name
        self.current.parent.mkdir(parents=True)
        self.current.write_bytes(self.data)
        self.state_path = self.root / 'maintenance/state.json'
        self.state = {'schema': 1, 'phase': 'running', 'current': {
            'path': str(self.current), 'version': '0.3.10-operator.2', 'sha256': self.sha}}
        self.write_state()
        self.version = patch.object(binary.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, b'anet 0.3.10-operator.2 (commit fixture)\n', b'')).start()
        self.addCleanup(patch.stopall)
        patch.object(binary.shutil, 'which', return_value=None).start()

    def write_state(self):
        self.state_path.write_text(json.dumps(self.state), encoding='utf-8')

    def select(self):
        return binary.resolve(self.root, home=self.home)

    def fails(self, code):
        with self.assertRaises(binary.BinaryResolutionError) as error:
            self.select()
        self.assertEqual(error.exception.code, code)

    def test_managed_current_wins_over_old_mcp_transport(self):
        taskd = self.root / 'taskd'
        taskd.mkdir()
        (taskd / 'config.json').write_text(json.dumps({'transport': {'binary': str(self.old), 'data_dir': str(self.root)}}))
        result = self.select()
        self.assertEqual(result['binary'], str(self.current))
        self.assertEqual(result['selection'], 'maintenance_verified')
        # A daemon reporting a new version cannot make an old MCP executable new.
        self.version.assert_called_once()
        self.assertEqual(self.version.call_args.args[0], [str(self.current), '--version'])

    def test_owner_data_dir_parent_segment_is_canonicalized_after_link_check(self):
        child = self.root / 'child'
        child.mkdir()
        result = binary.resolve(child / '..', home=self.home)
        self.assertEqual(result['binary'], str(self.current))
        self.assertEqual(result['selection'], 'maintenance_verified')

    def test_hash_mismatch_never_probes_or_falls_back(self):
        self.current.write_bytes(b'tampered bytes')
        self.fails('binary_hash_mismatch')
        self.version.assert_not_called()

    def test_managed_outside_boundary_never_executes(self):
        outside = self.base / self.name
        outside.write_bytes(self.data)
        self.state['current']['path'] = str(outside)
        self.write_state()
        self.fails('managed_path_mismatch')
        self.version.assert_not_called()

    def test_missing_current_does_not_choose_old(self):
        self.current.unlink()
        self.fails('managed_binary_unavailable')
        self.version.assert_not_called()

    def test_version_mismatch_does_not_choose_old(self):
        self.version.return_value.stdout = b'anet 0.3.9 (commit old)\n'
        self.fails('binary_version_mismatch')

    def test_prefix_version_is_not_a_match(self):
        self.version.return_value.stdout = b'anet 0.3.10-operator.20 (commit other)\n'
        self.fails('binary_version_mismatch')

    def test_version_timeout_is_closed(self):
        self.version.side_effect = subprocess.TimeoutExpired('anet --version', 8)
        self.fails('managed_binary_unavailable')

    def test_missing_hash_in_versions_is_not_legacy(self):
        del self.state['current']['sha256']
        self.write_state()
        self.fails('invalid_sha256')
        self.version.assert_not_called()

    def test_changed_state_during_probe_does_not_return_stale_selection(self):
        def run(*args, **kwargs):
            self.state['phase'] = 'updating'
            self.write_state()
            return subprocess.CompletedProcess([], 0, b'anet 0.3.10-operator.2\n', b'')
        self.version.side_effect = run
        self.fails('maintenance_state_changed')

    def test_changed_binary_during_probe_is_closed(self):
        def run(*args, **kwargs):
            self.current.write_bytes(b'replacement bytes')
            return subprocess.CompletedProcess([], 0, b'anet 0.3.10-operator.2\n', b'')
        self.version.side_effect = run
        self.fails('binary_changed')

    def test_managed_symlink_is_rejected(self):
        real = self.base / 'real-binary'
        self.current.replace(real)
        try:
            self.current.symlink_to(real)
        except OSError as error:
            self.skipTest('host does not allow creating a fixture symlink: ' + str(error.winerror if hasattr(error, 'winerror') else error.errno))
        self.fails('linked_path')
        self.version.assert_not_called()

    def test_broken_state_symlink_is_not_absence(self):
        self.state_path.unlink()
        try:
            self.state_path.symlink_to(self.base / 'absent-state')
        except OSError:
            self.skipTest('host does not allow creating a fixture symlink')
        self.fails('linked_path')

    def test_explicit_override_works_even_with_damaged_current(self):
        self.current.write_bytes(b'bad')
        result = binary.resolve(self.root, self.old, home=self.home)
        self.assertEqual(result['binary'], str(self.old))
        self.assertEqual(result['selection'], 'explicit')
        self.version.assert_not_called()

    def test_fresh_without_maintenance_keeps_existing_path(self):
        self.state_path.unlink()
        self.assertEqual(self.select()['binary'], str(self.old))
        self.version.assert_not_called()

    def test_bootstrap_current_preserves_fallback_and_labels_unverified(self):
        bootstrap = self.home / '.anet-maintenance-bin' / self.name
        bootstrap.parent.mkdir()
        bootstrap.write_bytes(b'bootstrap')
        self.state['current'] = {'path': str(bootstrap), 'version': '0.3.10-operator.1'}
        self.write_state()
        result = self.select()
        self.assertEqual(result['binary'], str(self.old))
        self.assertEqual(result['selection'], 'legacy_fallback_unverified')
        self.assertTrue(result['detail'])
        self.version.assert_not_called()

    def test_unknown_legacy_pointer_never_executes(self):
        self.state['current'] = {'path': str(self.base / 'untrusted'), 'version': '0.3.10'}
        self.write_state()
        self.fails('unrecognized_legacy_current')
        self.version.assert_not_called()

    def test_malformed_state_does_not_fallback(self):
        self.state_path.write_bytes(b'not json')
        self.fails('invalid_maintenance_state')

    def test_backend_all_products_receive_verified_path_once_per_context(self):
        context, resolution = binary.resolved_context({'anet_root': str(self.root), 'config_home': str(self.home)})
        seen = []
        def handler(request, ctx):
            seen.append((request, ctx))
            return {'state': 'ready'}
        account = types.ModuleType('anet_account')
        account.execute = handler
        package = types.ModuleType('anet_harnesses')
        client = types.ModuleType('anet_harnesses.client_config')
        client.execute = handler
        with patch.dict(sys.modules, {'anet_account': account, 'anet_harnesses': package, 'anet_harnesses.client_config': client}):
            for action in ('account.status', 'config.status', 'config.connect'):
                result = backend.execute({'action': action}, context)
                self.assertEqual(result['binary_resolution']['selection'], 'maintenance_verified')
        self.assertTrue(all(ctx['anet_binary'] == str(self.current) for _, ctx in seen))
        self.version.assert_called_once()

    def test_json_cannot_forge_in_memory_verification_memo(self):
        forged = {'anet_root': str(self.root), '_resolution': {'binary': str(self.old)}, '_selection_key': []}
        selected, resolution = binary.resolved_context(forged)
        self.assertEqual(selected['anet_binary'], str(self.current))
        self.assertEqual(resolution['selection'], 'maintenance_verified')
        self.version.assert_called_once()

    def test_native_install_reuses_verified_current_no_download(self):
        with patch.object(native, 'download', side_effect=AssertionError('no network')):
            result = native.install(self.root)
        self.assertEqual(result['binary'], str(self.current))
        self.assertEqual(result['state'], 'reused')

    def test_cli_context_retains_resolution_without_rechecking(self):
        args = cli.parser().parse_args(['--data-dir', str(self.root), 'status'])
        selected = cli.context(args)
        selected2, result = binary.resolved_context(selected)
        self.assertIs(selected, selected2)
        self.assertEqual(result['selection'], 'maintenance_verified')
        self.version.assert_called_once()

    def test_changing_context_selection_invalidates_process_memo(self):
        selected, _ = binary.resolved_context({'anet_root': str(self.root)})
        selected['anet_binary'] = str(self.old)
        updated, result = binary.resolved_context(selected)
        self.assertIsNot(updated, selected)
        self.assertEqual(result['selection'], 'explicit')
        self.assertEqual(result['binary'], str(self.old))
        self.version.assert_called_once()

    @unittest.skipUnless(os.name == 'nt', 'Windows stat/fstat creation-time distinction')
    def test_windows_identity_ignores_stat_fstat_ctime_difference(self):
        fields = dict(st_dev=1, st_ino=2, st_size=3, st_mtime_ns=4, st_birthtime_ns=5)
        self.assertEqual(binary.identity(types.SimpleNamespace(**fields, st_ctime_ns=5)),
                         binary.identity(types.SimpleNamespace(**fields, st_ctime_ns=99)))


if __name__ == '__main__':
    unittest.main()
