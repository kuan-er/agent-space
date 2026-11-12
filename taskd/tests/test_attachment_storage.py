"""Regress history collisions, disk amplification, cache corruption and cleanup."""
import base64
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from helpers import ROOT
from taskd.attachments import receive
from taskd.platform.locks import SingleInstance


def digest(data):
    return hashlib.sha256(data).hexdigest()


def cid(data):
    return 'b' + base64.b32encode(b'\x01\x55\x12\x20' + hashlib.sha256(data).digest()).decode().lower().rstrip('=')


class HistoryTransport:
    def __init__(self):
        self.calls = 0
        self.files = {'report.json': b'old report', 'report-34.json': b'current report',
                      'historical-large.bin': b'x' * (1024 * 1024)}
        self.mapping = True
        self.interrupt = False

    def pull(self, tid, dest):
        self.calls += 1
        results = []
        for name, data in self.files.items():
            p = Path(dest) / name
            p.write_bytes(data)
            results.append(dict(name=name, path=str(p), size=len(data), cid=cid(data)))
        if self.interrupt:
            raise RuntimeError('connection lost after writing files')
        return {'files': results} if self.mapping else None


class AttachmentStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.t = HistoryTransport()

    def message(self, data=b'current report', name='report.json', mid='m1'):
        return {'id': mid, 'files': [name], 'attachments': [dict(name=name, size=len(data), sha256=digest(data))]}

    def receive(self, message=None):
        return receive(self.t, 't1', message or self.message(), {}, self.root)

    def test_numbered_historical_report_uses_measured_cid(self):
        r = self.receive()
        self.assertEqual(r['state'], 'verified', r)
        self.assertEqual(Path(r['files'][0]['path']).read_bytes(), b'current report')
        self.assertFalse(list(self.root.rglob('.staging-*')))
        self.assertFalse(list(self.root.rglob('historical-large.bin')))

    def test_repeat_and_restart_reuse_receipt_without_pull_or_new_paths(self):
        r = self.receive()
        before = sorted(str(p) for p in self.root.rglob('*'))
        for _ in range(10):
            result = self.receive()
            self.assertTrue(result['reused'])
            self.assertEqual(result['files'], r['files'])
        self.assertEqual(self.t.calls, 1)
        self.assertEqual(before, sorted(str(p) for p in self.root.rglob('*')))
        self.assertEqual(json.loads((Path(r['directory'])/'receipt.json').read_text())['state'], 'verified')

    def test_new_message_alias_reuses_object_and_only_adds_small_view(self):
        self.receive()
        result = self.receive(self.message(name='result.txt', mid='m2'))
        self.assertEqual(result['state'], 'verified')
        self.assertEqual(self.t.calls, 1)
        self.assertEqual(len(list((self.root/'t1'/'objects').iterdir())), 1)

    def test_same_name_new_version_is_not_stale_cache(self):
        old = self.receive(self.message(b'old report'))
        new = self.receive()
        self.assertEqual(old['state'], new['state'])
        self.assertNotEqual(old['directory'], new['directory'])
        self.assertEqual(Path(old['files'][0]['path']).read_bytes(), b'old report')
        self.assertEqual(Path(new['files'][0]['path']).read_bytes(), b'current report')
        self.assertEqual(self.t.calls, 2)

    def test_forged_cid_never_overrides_measured_bytes(self):
        original = self.t.pull
        def forged(tid, dest):
            result = original(tid, dest)
            for f in result['files']:
                f['cid'] = cid(b'missing bytes')
            return result
        self.t.pull = forged
        r = self.receive(self.message(b'missing bytes'))
        self.assertEqual(r['state'], 'failed')
        self.assertEqual(r['files'], [])

    def test_cached_bytes_rechecked_and_repaired(self):
        r = self.receive()
        path = Path(r['files'][0]['path'])
        path.chmod(0o600); path.write_bytes(b'tampered')
        r = self.receive()
        self.assertEqual(r['state'], 'verified', r)
        self.assertEqual(path.read_bytes(), b'current report')
        self.assertEqual(self.t.calls, 2)

    def test_failure_receipt_retained_without_payload_or_retry_storm(self):
        for _ in range(10):
            r = self.receive(self.message(b'missing bytes'))
            self.assertEqual(r['state'], 'failed')
            self.assertFalse(r['retryable'])
        self.assertEqual(self.t.calls, 1)
        self.assertFalse(list(self.root.rglob('.staging-*')))
        self.assertFalse(list(self.root.rglob('historical-large.bin')))
        self.assertTrue((Path(r['directory'])/'receipt.json').exists())
        with mock.patch('taskd.attachments.time.time', return_value=r['retry_after'] + 1):
            self.receive(self.message(b'missing bytes'))
        self.assertEqual(self.t.calls, 2)

    def test_partial_pull_cleanup_and_transient_retry(self):
        self.t.interrupt = True
        r = self.receive()
        self.assertEqual(r['state'], 'failed'); self.assertTrue(r['retryable'])
        self.assertFalse(list(self.root.rglob('.staging-*')))
        self.t.interrupt = False
        self.assertEqual(self.receive()['state'], 'verified')
        self.assertEqual(self.t.calls, 2)

    def test_external_or_symlink_cid_path_cannot_be_imported(self):
        outside = self.root/'outside'; outside.write_bytes(b'current report')
        def pull(tid, dest):
            (Path(dest)/'report.json').symlink_to(outside)
            return {'files': [dict(path=str(outside), cid=cid(b'current report'))]}
        self.t.pull = pull
        self.assertEqual(self.receive()['state'], 'failed')

    def test_old_failed_receive_directories_are_untouched(self):
        old = self.root/'t1'/'receive-old'; old.mkdir(parents=True)
        (old/'original.bin').write_bytes(b'preserve old evidence')
        self.receive()
        self.assertEqual((old/'original.bin').read_bytes(), b'preserve old evidence')

    def test_concurrent_receive_defers_without_exporting_or_overwriting(self):
        parent=self.root/'t1';parent.mkdir()
        lock=SingleInstance(parent/'.receive.lock');self.assertTrue(lock.acquire())
        try:
            result=self.receive()
            self.assertEqual(result['state'],'failed');self.assertTrue(result['retryable'])
            self.assertEqual(self.t.calls,0)
        finally:
            lock.release()
        self.assertEqual(self.receive()['state'],'verified')

    def test_filesystems_without_hardlinks_still_verify_views(self):
        with mock.patch('taskd.attachments.os.link',side_effect=OSError('unsupported')):
            result=self.receive()
        self.assertEqual(result['state'],'verified',result)
        self.assertEqual(Path(result['files'][0]['path']).read_bytes(),b'current report')

    def test_invalid_cached_json_is_reconstructed_from_verified_content(self):
        result=self.receive()
        receipt=Path(result['directory'])/'receipt.json'
        for content in ('[]','{invalid json'):
            receipt.write_text(content)
            result=self.receive()
            self.assertEqual(result['state'],'verified',result)
        self.assertEqual(self.t.calls,1)


if __name__ == '__main__':
    unittest.main()
