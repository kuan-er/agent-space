import hashlib
import importlib.util
import json
from pathlib import Path
import platform
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('anet_config_installer', Path(__file__).resolve().parents[1] / 'install.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class NativeInstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='anet-native-install-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        self.prefix = self.root / 'prefix'
        self.manifest = {'version': '0.3.0-preview.1', 'native': {'system': platform.system(), 'machine': platform.machine()}, 'files': []}
        self.add('native/anet-config', b'public native fixture', 0o755)
        self.add('main.py', b'# public source\n', 0o644)

    def add(self, name, data, mode):
        target = self.source / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        self.manifest['files'].append({'path': name, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest(), 'mode': mode})

    def install(self):
        (self.source / 'manifest.json').write_text(json.dumps(self.manifest))
        return installer.install(self.source, self.prefix)

    def test_native_launcher_is_relocatable_and_source_independent(self):
        launcher = self.install()
        text = launcher.read_text()
        expected = self.prefix / 'lib/anet-config/0.3.0-preview.1/native/anet-config'
        self.assertIn(str(expected), text)
        self.assertNotIn(str(self.source), text)
        self.assertEqual(expected.read_bytes(), b'public native fixture')

    def test_corruption_rejected_before_install_directory_created(self):
        (self.source / 'main.py').write_bytes(b'changed')
        with self.assertRaises(ValueError):
            self.install()
        self.assertFalse(self.prefix.exists())

    def test_absolute_manifest_path_cannot_escape_stage(self):
        self.manifest['files'][0]['path'] = str(self.source / 'native/anet-config')
        with self.assertRaises(ValueError):
            self.install()
        self.assertFalse(self.prefix.exists())

    def test_other_architecture_does_not_install(self):
        self.manifest['native']['machine'] = 'unsupported-architecture'
        with self.assertRaises(ValueError):
            self.install()
        self.assertFalse(self.prefix.exists())

    def test_changed_existing_version_is_not_overwritten(self):
        self.install()
        target = self.prefix / 'lib/anet-config/0.3.0-preview.1/main.py'
        target.write_bytes(b'existing owner modification')
        with self.assertRaises(ValueError):
            self.install()
        self.assertEqual(target.read_bytes(), b'existing owner modification')

    def test_nested_install_symlink_does_not_write_outside_prefix(self):
        outside = self.root / 'outside'
        outside.mkdir()
        self.prefix.mkdir()
        try:
            (self.prefix / 'lib').symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest('symlink creation not available on test host')
        with self.assertRaises(ValueError):
            self.install()
        self.assertEqual(list(outside.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
