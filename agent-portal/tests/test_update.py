"""Signed self-update: only a manifest signed by the release key, with a matching hash and a newer serial, is applied."""
import base64, hashlib, http.server, importlib.util, json, os, tempfile, threading, unittest
from pathlib import Path
from unittest.mock import patch
SRC = Path(__file__).parents[1] / 'connector.py'
spec = importlib.util.spec_from_file_location('connector', SRC); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
SECRET = hashlib.sha256(b'test release key').digest(); PUB = m.ed25519_public(SECRET)

def release(version='9.9.9', serial=100, secret=SECRET, code=None, digest=None):
    code = code if code is not None else SRC.read_text(encoding='utf-8').replace("VERSION='%s'" % m.VERSION, "VERSION='%s'" % version, 1).encode()
    manifest = json.dumps({'schema': 1, 'serial': serial, 'components': {'connector': {'version': version, 'files': {'connector.py': digest or hashlib.sha256(code).hexdigest()}}}}, sort_keys=True, separators=(',', ':')).encode()
    return {'/downloads/agent-space-updates/manifest.json': manifest,
            '/downloads/agent-space-updates/manifest.sig': base64.b64encode(m.ed25519_sign(secret, manifest)),
            '/downloads/agent-space-updates/connector-%s/connector.py' % version: code}

class UpdateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name) / 'portal'; self.folder.mkdir()
        self.here = self.folder / 'connector.py'; self.here.write_bytes(b'# old connector\n')
        self.files = {}
        files = self.files
        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def do_GET(self):
                body = files.get(self.path)
                self.send_response(200 if body is not None else 404); self.end_headers()
                if body is not None: self.wfile.write(body)
        self.srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), H); threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close); self.addCleanup(self.srv.shutdown)
        self.url = 'http://127.0.0.1:%d' % self.srv.server_port
    def check(self, **kw): return m.check_update(self.folder, self.here, self.url, key=PUB, **kw)

    def test_valid_release_is_installed_with_backup_and_pending_marker(self):
        self.files.update(release())
        self.assertEqual(self.check(), 'updated')
        self.assertIn(b"VERSION='9.9.9'", self.here.read_bytes())
        self.assertEqual((self.folder / 'updates/backup' / (m.VERSION + '-connector.py')).read_bytes(), b'# old connector\n')
        self.assertEqual(json.loads((self.folder / 'updates/pending.json').read_text())['version'], '9.9.9')
        self.assertEqual(m.update_state(self.folder)['serial'], 100)
        # An older process that is still running must not confirm the new version; only the new one may.
        m.confirm_update(self.folder); self.assertTrue((self.folder / 'updates/pending.json').exists())
        with patch.object(m, 'VERSION', '9.9.9'):
            m.confirm_update(self.folder)
        self.assertFalse((self.folder / 'updates/pending.json').exists()); self.assertEqual(m.update_state(self.folder)['result'], 'confirmed:9.9.9')

    def test_check_only_does_not_touch_the_file(self):
        self.files.update(release()); self.assertEqual(self.check(apply=False), 'available'); self.assertEqual(self.here.read_bytes(), b'# old connector\n')

    def test_wrong_signer_is_rejected(self):
        self.files.update(release(secret=hashlib.sha256(b'attacker').digest()))
        with self.assertRaisesRegex(RuntimeError, '签名无效'): self.check()
        self.assertEqual(self.here.read_bytes(), b'# old connector\n')

    def test_tampered_file_is_rejected(self):
        files = release(); files['/downloads/agent-space-updates/connector-9.9.9/connector.py'] += b'\nimport os\n'
        self.files.update(files)
        with self.assertRaisesRegex(RuntimeError, '校验值不一致'): self.check()
        self.assertEqual(self.here.read_bytes(), b'# old connector\n')

    def test_older_manifest_is_a_rollback_and_is_rejected(self):
        self.files.update(release(serial=100)); self.check(apply=False)
        self.files.update(release(version='9.9.8', serial=50))
        with self.assertRaisesRegex(RuntimeError, '防回滚'): self.check()

    def test_same_or_older_version_is_current(self):
        self.files.update(release(version=m.VERSION)); self.assertEqual(self.check(), 'current'); self.assertEqual(self.here.read_bytes(), b'# old connector\n')

    def test_release_that_fails_its_selftest_is_not_installed(self):
        code = b"print('not the version')\n"
        self.files.update(release(code=code))
        with self.assertRaisesRegex(RuntimeError, '自检未通过'): self.check()
        self.assertEqual(self.here.read_bytes(), b'# old connector\n')

    def test_release_with_syntax_error_is_not_installed(self):
        self.files.update(release(code=b'def (:\n'))
        with self.assertRaises(SyntaxError): self.check()
        self.assertEqual(self.here.read_bytes(), b'# old connector\n')

    def test_version_that_never_starts_is_rolled_back_and_blocked(self):
        self.files.update(release()); self.check()
        restarts = []
        with patch.object(m, 'restart_self', lambda: restarts.append(1)):
            for _ in range(3): m.update_guard(self.folder, self.here)
            self.assertEqual(restarts, []); self.assertIn(b"VERSION='9.9.9'", self.here.read_bytes())
            m.update_guard(self.folder, self.here)  # fourth start without a confirmed heartbeat
        self.assertEqual(restarts, [1]); self.assertEqual(self.here.read_bytes(), b'# old connector\n')
        self.assertFalse((self.folder / 'updates/pending.json').exists())
        self.files.update(release(serial=200)); self.assertEqual(self.check(), 'blocked')

    def test_https_is_required_for_remote_channels(self):
        with self.assertRaisesRegex(RuntimeError, 'HTTPS'): m.fetch_bytes('http://example.com/x', 10)

    def test_auto_update_can_be_switched_off(self):
        self.assertTrue(m.auto_update_on(self.folder)); (self.folder / 'auto-update-off').write_text('1'); self.assertFalse(m.auto_update_on(self.folder))
if __name__ == '__main__': unittest.main()
