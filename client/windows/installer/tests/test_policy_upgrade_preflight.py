"""Exercise the exact installer entry with actual preview.12 policy tuples.

The lifecycle filesystem is finite and synthetic. No real services, models,
registry writes or existing ANet installation are touched.
"""
import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('draft_installer', ROOT / 'install_engine.py')
engine = importlib.util.module_from_spec(spec)
spec.loader.exec_module(engine)
POLICY = json.loads((ROOT / 'tests/fixtures/preview12-release-policy.json').read_text(encoding='utf-8'))


class PolicyUpgradePreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='anet-policy-preflight-')
        self.root = Path(self.temp.name)
        self.payload = self.root / 'payload'
        self.fixture = self.root / 'fixture'
        self.self_exe = self.root / 'setup.exe'
        self.self_exe.write_bytes(b'finite installer fixture')
        for name, data in {
            'frontend/ANetDesktop.exe': b'desktop fixture',
            'backend/desktop_controller.py': b'backend fixture',
            'python/python.exe': b'private python fixture',
            'harness-runtime/codex/codex.exe': b'codex fixture',
            'node/taskd/app/run_taskd.py': b'old taskd fixture',
        }.items():
            p = self.payload / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
        self.args = argparse.Namespace(fixture_root=str(self.fixture), node_root=None,
            backend_root=None, self_exe=self.self_exe, action='install',
            start_services=False, fail_before_activate=False)
        self.manifest = self.make_manifest('0.2.0-preview.12')
        engine.install(self.args, self.payload, self.manifest)
        self.current = self.fixture / 'Data/ANetDesktop/current.json'
        self.receipt = engine.read(self.current)
        self.node = Path(self.receipt['runtime']['anet_root'])
        self.config = self.node / 'taskd/config.json'
        (self.node / 'identity-preserved').write_bytes(b'original fixture identity')

    def tearDown(self):
        self.temp.cleanup()

    def make_manifest(self, version):
        return {'schema': 'anet-desktop-payload/1', 'release': version, 'files': [
            {'path': p.relative_to(self.payload).as_posix(), 'bytes': p.stat().st_size,
             'sha256': engine.sha(p)} for p in sorted(self.payload.rglob('*')) if p.is_file()]}

    def set_policy(self, *names):
        engine.atomic(self.config, {'version': 1, 'requester': {n: POLICY[n] for n in names},
            'executor': None, 'guard': [], 'owner_setting': 'retain'})

    def prepare_upgrade(self, app_changed=True):
        if app_changed:
            (self.payload / 'node/taskd/app/run_taskd.py').write_bytes(b'new sealed taskd fixture')
        (self.payload / 'frontend/ANetDesktop.exe').write_bytes(b'new frontend fixture')
        return self.make_manifest('0.3.0-preview.2')

    def snapshot(self):
        return {p.relative_to(self.fixture).as_posix():
                hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else 'directory'
                for p in self.fixture.rglob('*')}

    def reject_without_mutation(self, manifest):
        before = self.snapshot()
        with patch.object(engine, 'mark', side_effect=AssertionError('must reject before first mutation')):
            with patch.object(engine, 'copy_group', side_effect=AssertionError('must not copy')):
                with patch.object(engine, 'stop_managed_requester', side_effect=AssertionError('must not stop')):
                    with self.assertRaisesRegex(ValueError, 'existing_release_policy_requires_start_services'):
                        engine.install(self.args, self.payload, manifest)
        self.assertEqual(before, self.snapshot())
        self.assertEqual(engine.read(self.current)['release'], '0.2.0-preview.12')

    def test_actual_harness_policy_raw_install_rejected_before_any_write(self):
        self.assertEqual(POLICY['source_release'], '0.2.0-preview.12')
        self.assertEqual(POLICY['harness_sources']['release_policy']['package_schema'],
                         'anet-harness-release-closure/2')
        self.set_policy('harness_sources')
        self.reject_without_mutation(self.prepare_upgrade())

    def test_actual_owned_policy_raw_repair_rejected_before_any_write(self):
        self.set_policy('owned_sources')
        self.args.action = 'repair'
        self.reject_without_mutation(self.prepare_upgrade())

    def test_main_rejects_before_owner_marker_or_lock_file_write(self):
        self.set_policy('harness_sources', 'owned_sources')
        engine.atomic(self.payload/'payload-manifest.json', self.prepare_upgrade())
        before = self.snapshot()
        output = io.StringIO()
        argv = ['install_engine.py', '--payload', str(self.payload), '--self-exe', str(self.self_exe),
                '--fixture-root', str(self.fixture), '--action', 'install']
        with patch('sys.argv', argv), contextlib.redirect_stdout(output):
            with patch.object(engine, 'mark', side_effect=AssertionError('must not write owner marker')):
                self.assertEqual(engine.main(), 1)
        result = json.loads(output.getvalue())
        self.assertIn('existing_release_policy_requires_start_services', result['reason'])
        self.assertEqual(before, self.snapshot())

    def test_both_actual_old_policies_rejected_and_preserved(self):
        self.set_policy('harness_sources', 'owned_sources')
        self.reject_without_mutation(self.prepare_upgrade())

    def test_disabled_policy_is_preserved_for_future_use_not_left_stale(self):
        self.set_policy('harness_sources')
        cfg = engine.read(self.config)
        cfg['requester']['harness_sources']['enabled'] = False
        engine.atomic(self.config, cfg)
        self.reject_without_mutation(self.prepare_upgrade())

    def test_gui_service_preparation_path_remains_enabled(self):
        self.set_policy('harness_sources', 'owned_sources')
        self.args.start_services = True
        with patch.object(engine, 'setup_fresh') as migrate_and_start:
            result = engine.install(self.args, self.payload, self.prepare_upgrade())
        self.assertEqual(result['state'], 'installed')
        migrate_and_start.assert_called_once()
        self.assertEqual((self.node / 'identity-preserved').read_bytes(), b'original fixture identity')

    def test_frontend_only_upgrade_does_not_require_service_start(self):
        self.set_policy('harness_sources')
        before = self.config.read_bytes()
        result = engine.install(self.args, self.payload, self.prepare_upgrade(app_changed=False))
        self.assertEqual(result['state'], 'installed')
        self.assertEqual(self.config.read_bytes(), before)

    def test_no_release_policy_retains_existing_code_only_behavior(self):
        self.set_policy()
        result = engine.install(self.args, self.payload, self.prepare_upgrade())
        self.assertEqual(result['state'], 'installed')
        self.assertEqual((self.node / 'taskd/app/run_taskd.py').read_bytes(), b'new sealed taskd fixture')

    def test_reused_node_is_not_modified_or_treated_as_managed_upgrade(self):
        self.set_policy('harness_sources', 'owned_sources')
        receipt = engine.read(self.current)
        receipt['node_reused'] = True
        engine.atomic(self.current, receipt)
        before = {str(p): p.read_bytes() for p in self.node.rglob('*') if p.is_file()}
        result = engine.install(self.args, self.payload, self.prepare_upgrade())
        self.assertTrue(result['existing_node_reused'])
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.node.rglob('*') if p.is_file()})


if __name__ == '__main__':
    unittest.main()
