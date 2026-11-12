"""Signed self-update: only a manifest signed by the release key, with a matching zip and a newer serial, is
applied; only while idle; and a version that does not prove itself is rolled back and blocked."""
import base64
import contextlib
import hashlib
import http.server
import io
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import zipfile

from helpers import ROOT

from taskd import cli, update
from taskd.platform.locks import SingleInstance
from taskd.service import Daemon, VERSION
from taskd.store import Store, atomic_json, read_json

SECRET = hashlib.sha256(b'taskd test release key').digest()
PUB = update.ed25519_public(SECRET)
NEW = '9.9.9'
CHANNEL = '/downloads/agent-space-updates/'


def program_files(version, drop=()):
    """The real program as a release zip would carry it, stamped with `version`."""
    out = {}
    for p in sorted((ROOT / 'taskd').rglob('*.py')):
        rel = p.relative_to(ROOT).as_posix()
        data = p.read_bytes()
        if rel == 'taskd/service.py':
            data = re.sub(rb"^VERSION = '[^']+'", ("VERSION = '%s'" % version).encode(), data, count=1, flags=re.M)
        if rel not in drop:
            out[rel] = data
    out['run_taskd.py'] = (ROOT / 'run_taskd.py').read_bytes()
    out['README.md'] = b'readme'
    for name in ('LICENSE', 'NOTICE'):
        out[name] = (ROOT / name).read_bytes()
    return out


def make_zip(version=NEW, extra=None, files=None):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        for rel, data in (files or program_files(version)).items():
            z.writestr('taskd-%s/%s' % (version, rel), data)
        for name, data in (extra or {}).items():
            z.writestr(name, data)
    return buf.getvalue()


def channel(version=NEW, serial=100, secret=SECRET, zip_bytes=None, digest=None, with_taskd=True):
    zip_bytes = zip_bytes if zip_bytes is not None else make_zip(version)
    comps = {'connector': {'version': '0.0.1', 'files': {'connector.py': '0' * 64}}}
    if with_taskd:
        comps['taskd'] = {'version': version, 'file': 'taskd-%s.zip' % version, 'sha256': digest or hashlib.sha256(zip_bytes).hexdigest()}
    manifest = json.dumps({'schema': 1, 'serial': serial, 'components': comps}, sort_keys=True, separators=(',', ':')).encode()
    return {CHANNEL + 'manifest.json': manifest, CHANNEL + 'manifest.sig': base64.b64encode(update.ed25519_sign(secret, manifest)),
            CHANNEL + 'taskd-%s/taskd-%s.zip' % (version, version): zip_bytes}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'taskd'
        self.app = self.root / 'app'
        for rel, data in program_files(VERSION).items():
            if rel != 'README.md':
                (self.app / rel).parent.mkdir(parents=True, exist_ok=True)
                (self.app / rel).write_bytes(data)
        self.files = {}
        files = self.files

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                body = files.get(self.path)
                self.send_response(200 if body is not None else 404)
                self.end_headers()
                if body is not None:
                    self.wfile.write(body)
        self.srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.url = 'http://127.0.0.1:%d' % self.srv.server_port
        self.holds, self.restarts, self.old_service = [], [], (self.app / 'taskd' / 'service.py').read_bytes()

    def updater(self, version=VERSION, **kw):
        kw.setdefault('hold', self.holds.append)
        kw.setdefault('request_restart', lambda: self.restarts.append(1))
        return update.Updater(self.root, self.app, self.url, version=version, key=PUB, verify_delay=0, **kw)

    def installed_version(self):
        return re.search(rb"^VERSION = '([^']+)'", (self.app / 'taskd' / 'service.py').read_bytes(), re.M).group(1).decode()


class SignatureTests(unittest.TestCase):
    def test_rfc8032_vectors(self):
        vectors = [
            ('9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60', 'd75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a', '',
             'e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b'),
            ('4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb', '3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c', '72',
             '92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00')]
        for sk, pk, msg, sig in map(lambda v: [bytes.fromhex(x) for x in v], vectors):
            self.assertEqual(update.ed25519_public(sk), pk)
            self.assertEqual(update.ed25519_sign(sk, msg), sig)
            self.assertTrue(update.ed25519_verify(pk, msg, sig))
            self.assertFalse(update.ed25519_verify(pk, msg + b'x', sig))
            bad = bytearray(sig)
            bad[40] ^= 1
            self.assertFalse(update.ed25519_verify(pk, msg, bytes(bad)))

    def test_release_key_is_the_one_the_connector_trusts(self):
        connector = ROOT.parent / 'agent-portal' / 'connector.py'
        if not connector.exists():
            self.skipTest('agent-portal is not next to taskd')
        found = re.search(r"UPDATE_KEY=bytes\.fromhex\('([0-9a-f]{64})'\)", connector.read_text(encoding='utf-8'))
        self.assertEqual(update.UPDATE_KEY.hex(), found.group(1))


class ApplyTests(Fixture):
    def test_valid_release_is_applied_when_idle(self):
        self.files.update(channel())
        up = self.updater(was_ready=lambda: True)
        self.assertEqual(up.check(), 'updated')
        self.assertEqual(self.installed_version(), NEW)
        self.assertEqual((self.root / 'updates/backup' / VERSION / 'taskd/service.py').read_bytes(), self.old_service)
        pending = read_json(self.root / 'updates/pending.json')
        self.assertEqual((pending['version'], pending['from'], pending['tries'], pending['selftest_ok_before']), (NEW, VERSION, 0, True))
        self.assertEqual(up.state()['serial'], 100)
        self.assertEqual(self.restarts, [1])
        self.assertEqual(self.holds, [True])  # new work stays held: this process is about to end
        self.assertFalse(list((self.root / 'updates').glob('stage-*')))
        self.assertFalse((self.app / 'README.md').exists())

    def test_busy_node_postpones_and_releases_the_hold(self):
        self.files.update(channel())
        up = self.updater(is_idle=lambda: False)
        self.assertEqual(up.check(), 'busy')
        self.assertEqual(self.installed_version(), VERSION)
        self.assertEqual((self.holds, self.restarts), ([True, False], []))
        self.assertIsNone(up.pending())
        self.assertEqual(up.state()['result'], 'busy:' + NEW)

    def test_check_only_installs_nothing(self):
        self.files.update(channel())
        self.assertEqual(self.updater().check(apply=False), 'available')
        self.assertEqual(self.installed_version(), VERSION)

    def test_same_or_older_version_is_current_and_missing_component_is_current(self):
        self.files.update(channel(version=VERSION))
        self.assertEqual(self.updater().check(), 'current')
        self.files.update(channel(with_taskd=False, serial=101))
        self.assertEqual(self.updater().check(), 'current')

    def test_wrong_signer_is_rejected(self):
        self.files.update(channel(secret=hashlib.sha256(b'attacker').digest()))
        with self.assertRaisesRegex(RuntimeError, 'signature is invalid'):
            self.updater().check()
        self.assertEqual(self.installed_version(), VERSION)

    def test_tampered_zip_is_rejected(self):
        files = channel()
        files[CHANNEL + 'taskd-%s/taskd-%s.zip' % (NEW, NEW)] += b'tampered'
        self.files.update(files)
        with self.assertRaisesRegex(RuntimeError, 'does not match the signed checksum'):
            self.updater().check()
        self.assertEqual(self.installed_version(), VERSION)

    def test_older_manifest_is_a_rollback_and_is_refused(self):
        self.files.update(channel(serial=100))
        self.updater().check(apply=False)
        self.files.update(channel(version='9.9.8', serial=50))
        with self.assertRaisesRegex(RuntimeError, 'rollback refused'):
            self.updater().check()

    def test_unsafe_zips_are_refused(self):
        evil = [
            ('path traversal', make_zip(extra={'taskd-%s/../evil.py' % NEW: b'x=1'})),
            ('absolute path', make_zip(extra={'/etc/evil.py': b'x=1'})),
            ('windows separator', make_zip(extra={'taskd-%s/taskd\\evil.py' % NEW: b'x=1'})),
            ('outside the release directory', make_zip(extra={'other/taskd/evil.py': b'x=1'})),
            ('unexpected file type', make_zip(extra={'taskd-%s/taskd/evil.sh' % NEW: b'rm'})),
            ('unexpected top-level file', make_zip(extra={'taskd-%s/evil.py' % NEW: b'x=1'})),
            ('not a taskd program', make_zip(files={'run_taskd.py': b'x'})),
            ('syntax error', make_zip(files=dict(program_files(NEW), **{'taskd/cli.py': b'def (:\n'}))),
            ('manifest/zip version mismatch', make_zip(files=program_files('9.9.7'))),
        ]
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as z:
            for rel, data in program_files(NEW).items():
                z.writestr('taskd-%s/%s' % (NEW, rel), data)
            link = zipfile.ZipInfo('taskd-%s/taskd/link.py' % NEW)
            link.external_attr = (0o120777 << 16)
            z.writestr(link, '/etc/passwd')
        evil.append(('symlink', buf.getvalue()))
        for name, data in evil:
            with self.subTest(name):
                self.files.clear()
                self.files.update(channel(zip_bytes=data))
                with self.assertRaises((RuntimeError, SyntaxError)):
                    self.updater().check()
                self.assertEqual(self.installed_version(), VERSION)
                self.assertFalse((self.root / 'updates' / 'pending.json').exists())
                self.assertFalse(any(self.app.parent.glob('evil*')))

    def test_new_program_that_fails_its_own_version_check_is_not_installed(self):
        files = program_files(NEW)
        files['run_taskd.py'] = b"print('not the version')\n"
        self.files.update(channel(zip_bytes=make_zip(files=files)))
        with self.assertRaisesRegex(RuntimeError, 'version check'):
            self.updater().check()
        self.assertEqual(self.installed_version(), VERSION)

    def test_a_source_checkout_is_never_replaced(self):
        self.files.update(channel())
        up = self.updater(running_app=ROOT)
        with self.assertRaisesRegex(RuntimeError, 'not the installed copy'):
            up.check()
        self.assertEqual(self.installed_version(), VERSION)

    def test_https_is_required_for_remote_channels(self):
        with self.assertRaisesRegex(RuntimeError, 'HTTPS'):
            update.fetch_bytes('http://example.com/x', 10)


class ProveItselfTests(Fixture):
    def applied(self, was_ready=True):
        self.files.update(channel())
        self.updater(was_ready=lambda: was_ready).check()
        self.restarts.clear()
        self.holds.clear()

    def test_new_version_confirms_after_its_selftest_passes(self):
        self.applied()
        calls = []
        new = self.updater(NEW, selftest=lambda: calls.append(1) or (True, 'ok'))
        self.assertEqual(new.on_start(), 'verify')
        self.assertEqual(new.verify_pending(), 'confirmed')
        self.assertEqual(calls, [1])
        self.assertIsNone(new.pending())
        self.assertEqual(new.state()['result'], 'confirmed:' + NEW)
        self.assertEqual(self.restarts, [])

    def test_failed_selftest_rolls_back_blocks_and_restarts(self):
        self.applied()
        new = self.updater(NEW, selftest=lambda: (False, 'executor down'))
        self.assertEqual(new.on_start(), 'verify')
        self.assertEqual(new.verify_pending(), 'rolled-back')
        self.assertEqual(self.installed_version(), VERSION)
        self.assertEqual(self.restarts, [1])
        self.assertEqual(new.state()['blocked'], [NEW])
        self.assertEqual(new.state()['result'], 'rolled-back:' + NEW)
        self.assertIsNone(new.pending())
        self.assertTrue((self.root / 'updates/failed' / NEW / 'taskd/service.py').is_file())
        # The old program starts again and re-runs its self-test, because the failed run overwrote the record.
        runs = []
        old = self.updater(VERSION, selftest=lambda: runs.append(1) or (True, 'ok'))
        self.assertEqual(old.on_start(), 'reselftest')
        self.assertTrue(old.reselftest())
        self.assertEqual((runs, old.on_start()), ([1], None))
        self.assertEqual(old.state()['blocked'], [NEW])
        # A blocked version is not offered again, even in a newer manifest.
        self.files.update(channel(serial=200))
        self.assertEqual(old.check(), 'blocked')

    def test_selftest_failing_on_the_old_code_too_does_not_block_the_version(self):
        self.applied()
        self.updater(NEW, selftest=lambda: (False, 'login expired')).verify_pending()
        old = self.updater(VERSION, selftest=lambda: (False, 'login expired'))
        old.on_start()
        self.assertFalse(old.reselftest())
        self.assertEqual(old.state()['blocked'], [])
        self.assertEqual(old.state()['result'], 'rolled-back:%s(environment)' % NEW)

    def test_a_node_that_was_not_ready_before_is_not_judged_by_its_selftest(self):
        self.applied(was_ready=False)
        new = self.updater(NEW, selftest=lambda: (False, 'still broken'))
        new.on_start()
        self.assertEqual(new.verify_pending(), 'confirmed')
        self.assertEqual(self.installed_version(), NEW)

    def test_requester_only_node_confirms_without_a_selftest(self):
        self.applied()
        new = self.updater(NEW)
        new.on_start()
        self.assertEqual(new.verify_pending(), 'confirmed')

    def test_version_that_never_starts_is_rolled_back_on_the_fourth_start(self):
        self.applied()
        new = self.updater(NEW)
        self.assertEqual([new.on_start() for _ in range(3)], ['verify'] * 3)
        self.assertEqual(self.installed_version(), NEW)
        self.assertIsNone(new.on_start())
        self.assertEqual(self.installed_version(), VERSION)
        self.assertEqual(self.restarts, [1])
        self.assertEqual(new.state()['blocked'], [NEW])

    def test_stale_marker_is_dropped(self):
        self.applied()
        self.assertIsNone(self.updater(VERSION).on_start())  # the old program is running: someone replaced the app
        self.assertIsNone(self.updater(VERSION).pending())

    def test_no_second_update_while_the_first_is_unproven(self):
        self.applied()
        self.files.update(channel(version='9.9.10', serial=300))
        self.assertEqual(self.updater(NEW).check(), 'busy')


class LoopTests(Fixture):
    def run_loop(self, up, wait=2.0, first_delay=0):
        stop = threading.Event()
        up.request_restart = lambda: (self.restarts.append(1), stop.set())
        th = threading.Thread(target=up.run, args=(stop,), kwargs={'first_delay': first_delay}, daemon=True)
        th.start()
        th.join(wait)
        stop.set()
        th.join(5)
        return not th.is_alive()

    def test_loop_checks_and_applies_on_schedule(self):
        self.files.update(channel())
        self.assertTrue(self.run_loop(self.updater()))
        self.assertEqual(self.installed_version(), NEW)
        self.assertEqual(self.restarts, [1])

    def test_loop_does_nothing_when_auto_update_is_off(self):
        self.files.update(channel())
        up = self.updater()
        up.set_auto(False)
        self.assertTrue(self.run_loop(up, wait=1.0))
        self.assertEqual((self.installed_version(), self.restarts), (VERSION, []))
        self.assertEqual(up.public_state(), {'auto_update': False, 'update_state': ''})
        up.set_auto(True)
        self.assertTrue(up.public_state()['auto_update'])

    def test_loop_reports_an_untrusted_channel_in_the_state(self):
        self.files.update(channel(secret=hashlib.sha256(b'attacker').digest()))
        up = self.updater()
        self.assertTrue(self.run_loop(up, wait=1.0))
        self.assertTrue(up.state()['result'].startswith('error:'))
        self.assertEqual(self.installed_version(), VERSION)

    def test_loop_runs_the_proof_of_a_new_version_first(self):
        self.files.update(channel())
        self.updater().check()
        self.restarts.clear()
        runs = []
        new = self.updater(NEW, selftest=lambda: runs.append(1) or (True, 'ok'))
        self.assertTrue(self.run_loop(new, wait=1.0, first_delay=3600))
        self.assertEqual((runs, new.pending()), ([1], None))

    def test_check_now_flag_wakes_the_loop(self):
        self.files.update(channel())
        up = self.updater()
        (self.root / 'updates').mkdir(parents=True, exist_ok=True)
        (self.root / 'updates' / 'check-now').write_text('1')
        self.assertTrue(self.run_loop(up, wait=3.0, first_delay=3600))
        self.assertEqual(self.installed_version(), NEW)
        self.assertFalse((self.root / 'updates' / 'check-now').exists())


class RestartTests(unittest.TestCase):
    def test_decision_per_supervision_mode(self):
        a = update.restart_action
        # A supervisor restarts the program when it exits.
        self.assertEqual(a('win32', {'TASKD_SUPERVISED': '1'}), ('exit', 0))
        self.assertEqual(a('darwin', {'XPC_SERVICE_NAME': 'org.taskd.1933fe7f'}), ('exit', 0))
        self.assertEqual(a('linux', {'INVOCATION_ID': 'abc'}), ('exit', update.RESTART_EXIT_CODE))
        # No supervisor: a POSIX process replaces itself, a Windows one starts a detached copy.
        self.assertEqual(a('linux', {'TMUX': '/tmp/tmux-1/default,1,0'}), ('exec',))
        self.assertEqual(a('darwin', {'XPC_SERVICE_NAME': '0'}), ('exec',))
        self.assertEqual(a('darwin', {}), ('exec',))
        self.assertEqual(a('win32', {}), ('spawn',))

    def test_restart_performs_the_decision(self):
        argv = ['/py', '/app/run_taskd.py', '--data-dir', '/d', 'serve']
        self.assertEqual(update.restart(argv, 'darwin', {'XPC_SERVICE_NAME': 'org.taskd.x'}), 0)
        self.assertEqual(update.restart(argv, 'linux', {'INVOCATION_ID': '1'}), 75)
        with patch('taskd.update.os.execv') as execv, patch.dict(os.environ, {}, clear=False):
            update.restart(argv, 'linux', {})
            execv.assert_called_once_with('/py', argv)
            self.assertEqual(os.environ['TASKD_RESTARTED'], '1')
            os.environ.pop('TASKD_RESTARTED')
        with patch('taskd.update.start_detached') as spawn, patch.dict(os.environ, {}, clear=False):
            self.assertEqual(update.restart(argv, 'win32', {}, log_path='/log'), 0)
            spawn.assert_called_once_with(argv, '/log')
            os.environ.pop('TASKD_RESTARTED')

    def test_supervisor_marks_its_child(self):
        text = (ROOT / 'taskd' / 'cli.py').read_text(encoding='utf-8')
        self.assertRegex(text, r"subprocess\.call\(argv, env=dict\(os\.environ, TASKD_SUPERVISED='1'\),")


class FakeTransport:
    def close(self):
        pass


class DaemonTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def daemon(self, **kw):
        return Daemon(root=self.root, config={'work_dir': str(self.root)}, transport=FakeTransport(), poll_seconds=1, **kw)

    def test_idle_means_no_task_being_worked_on(self):
        store = Store(self.root / 'state')

        class Engine:
            def __init__(self):
                self.store, self.workers = store, {}
        d = self.daemon()
        d.engine = Engine()
        self.assertTrue(d.is_idle())
        for tid, state, idle in [('a', 'needs_user', True), ('b', 'awaiting_approval', True), ('c', 'blocked', True),
                                 ('d', 'completed', True), ('e', 'running', False)]:
            store.save({'id': tid, 'state': state, 'created_at': 1})
            self.assertEqual(d.is_idle(), idle, state)
            if not idle:
                store.save({'id': tid, 'state': 'completed', 'created_at': 1})
        for state in ('accepted', 'planning', 'verifying'):
            store.save({'id': 'f', 'state': state, 'created_at': 1})
            self.assertFalse(d.is_idle(), state)
        store.save({'id': 'f', 'state': 'completed', 'created_at': 1})
        self.assertTrue(d.is_idle())
        t = threading.Thread(target=time.sleep, args=(1,))
        t.start()
        d.engine.workers['x'] = t
        self.assertFalse(d.is_idle())  # a worker still unwinding counts as busy
        t.join()
        self.assertTrue(d.is_idle())

    def test_hold_stops_new_work(self):
        d = self.daemon()
        calls = []
        tick = d._unless_held(lambda: calls.append(1))
        tick()
        d.set_hold(True)
        tick()
        d.set_hold(False)
        tick()
        self.assertEqual(len(calls), 2)

    def test_request_restart_stops_the_daemon(self):
        d = self.daemon()
        self.assertFalse(d.restart_requested)
        d.request_restart()
        self.assertTrue(d.restart_requested and d.stop.is_set())

    def test_status_carries_version_and_update_state(self):
        class Up:
            def public_state(self):
                return {'auto_update': True, 'update_state': 'current'}
        d = self.daemon(updater=Up())
        d.write_status()
        st = read_json(self.root / 'status.json')
        self.assertEqual((st['version'], st['auto_update'], st['update_state']), (VERSION, True, 'current'))
        self.assertFalse(st['executor_ready'])

    def test_a_restart_waits_for_the_old_process_to_release_the_lock(self):
        held = SingleInstance(self.root / 'daemon.lock')
        self.assertTrue(held.acquire())
        threading.Timer(1.0, held.release).start()
        d = self.daemon(lock_wait=10)
        threading.Timer(2.5, d.stop.set).start()
        started = time.monotonic()
        d.run()
        self.assertGreater(time.monotonic() - started, 1.0)
        # Without a restart in progress a second daemon refuses at once.
        held2 = SingleInstance(self.root / 'daemon.lock')
        self.assertTrue(held2.acquire())
        with self.assertRaisesRegex(RuntimeError, 'already running'):
            self.daemon().run()
        held2.release()

    def test_new_code_invalidates_the_selftest_record(self):
        from taskd import builtin
        app = self.root / 'app'
        (app / 'taskd').mkdir(parents=True)
        (app / 'taskd' / 'x.py').write_text('a = 1\n')
        cfg = {'executor': {'type': 'command'}, 'work_dir': '/w'}
        before = builtin.fingerprint(cfg, app)
        (app / 'taskd' / 'x.py').write_text('a = 2\n')
        self.assertNotEqual(builtin.fingerprint(cfg, app), before)  # cli.build then reports executor_ready False


class CommandTests(Fixture):
    def setUp(self):
        super().setUp()
        patcher = patch.object(update, 'UPDATE_KEY', PUB)  # the commands build their own updater with the release key
        patcher.start()
        self.addCleanup(patcher.stop)

    def node(self):
        data = Path(self.tmp.name) / 'data'
        (data / 'taskd').mkdir(parents=True, exist_ok=True)
        atomic_json(data / 'taskd' / 'config.json', {'version': 1, 'update_portal': self.url, 'transport': {'type': 'anet'}})
        return data

    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(['--data-dir', str(self.node()), *argv])
        return code, out.getvalue().strip()

    def test_version(self):
        self.assertEqual(self.run_cli('version'), (0, VERSION))

    def test_update_check(self):
        self.files.update(channel())
        self.assertEqual(self.run_cli('update', '--check'), (0, 'available'))
        self.files.update(channel(version=VERSION, serial=101))
        self.assertEqual(self.run_cli('update', '--check'), (0, 'current'))

    def test_update_asks_a_running_daemon_instead_of_replacing_files(self):
        self.files.update(channel())
        data = self.node()
        lock = SingleInstance(data / 'taskd' / 'daemon.lock')
        self.assertTrue(lock.acquire())
        try:
            code, out = self.run_cli('update')
        finally:
            lock.release()
        self.assertEqual(code, 0)
        self.assertIn('asked it to check now', out)
        self.assertTrue((data / 'taskd' / 'updates' / 'check-now').exists())

    def test_update_from_a_source_checkout_is_refused(self):
        self.files.update(channel())
        with self.assertRaisesRegex(RuntimeError, 'not the installed copy'):
            self.run_cli('update')

    def test_auto_update_switch(self):
        self.assertEqual(self.run_cli('auto-update'), (0, 'auto-update: on'))
        self.assertEqual(self.run_cli('auto-update', 'off'), (0, 'auto-update: off'))
        self.assertTrue((self.node() / 'taskd' / 'auto-update-off').exists())
        self.assertEqual(self.run_cli('auto-update', 'on'), (0, 'auto-update: on'))

    def test_portal_comes_from_the_connectors_connection_file(self):
        data = self.node()
        cfg = {'transport': {}}
        node = cli.Node(data)
        self.assertIsNone(cli.portal_url(node, cfg))
        (data / 'portal').mkdir()
        atomic_json(data / 'portal' / 'connection.json', {'portal': 'https://example.test:8888'})
        self.assertEqual(cli.portal_url(node, cfg), 'https://example.test:8888')
        self.assertEqual(cli.portal_url(node, {'update_portal': 'http://127.0.0.1:1'}), 'http://127.0.0.1:1')
        with self.assertRaisesRegex(RuntimeError, 'no update channel'):
            update.Updater(self.root, self.app, None, version=VERSION).check()


if __name__ == '__main__':
    unittest.main()
