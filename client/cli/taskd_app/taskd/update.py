"""Signed self-update of the installed program (<data-dir>/taskd/app).

Trust rules, the same as the Agent Space connector's: a release is applied only when the channel manifest
verifies against UPDATE_KEY (the operator holds the private half offline), the zip matches the SHA-256 the
manifest signs, the manifest serial never goes backwards, and the zip holds only the program's own files.
It is applied only while no task is being worked on, and a version that does not prove itself (self-test
passes, or three starts) is rolled back and blocked.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
from pathlib import Path
import random
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError
import zipfile

from .platform.proc import start_detached, background_options
from .store import atomic_json, read_json

# Release key. Must equal UPDATE_KEY in agent-portal/connector.py (one key signs the whole channel).
UPDATE_KEY = bytes.fromhex('6064403f01345978fc14e4a577dc2290abf238394409cb3e077adeeaf948c907')
CHANNEL_PATH = '/downloads/agent-space-updates'
CHECK_SECONDS = 6 * 3600
BUSY_RETRY_SECONDS = 600
VERIFY_DELAY_SECONDS = 60
MAX_MANIFEST = 65536
MAX_ZIP = 3_000_000
MAX_FILE = 1_500_000
MAX_TOTAL = 8_000_000
MAX_FILES = 300
RESTART_EXIT_CODE = 75  # EX_TEMPFAIL: a unit with Restart=on-failure starts the new program

# ---- Ed25519 (RFC 8032), pure Python ------------------------------------------------------------
_P = 2 ** 255 - 19
_Q = 2 ** 252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, _P - 2, _P) % _P
_I = pow(2, (_P - 1) // 4, _P)


def _inv(x):
    return pow(x, _P - 2, _P)


def _x(y, sign):
    if y >= _P:
        return None
    xx = (y * y - 1) * _inv(_D * y * y + 1) % _P
    if xx == 0:
        return None if sign else 0
    x = pow(xx, (_P + 3) // 8, _P)
    if (x * x - xx) % _P:
        x = x * _I % _P
    if (x * x - xx) % _P:
        return None
    return _P - x if (x & 1) != sign else x


_GY = 4 * _inv(5) % _P
_GX = _x(_GY, 0)
_G = (_GX, _GY, 1, _GX * _GY % _P)


def _add(a, b):
    A = (a[1] - a[0]) * (b[1] - b[0]) % _P
    B = (a[1] + a[0]) * (b[1] + b[0]) % _P
    C = 2 * a[3] * b[3] * _D % _P
    E = 2 * a[2] * b[2] % _P
    e, f, g, h = B - A, E - C, E + C, B + A
    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


def _mul(n, pt):
    r = (0, 1, 1, 0)
    while n:
        if n & 1:
            r = _add(r, pt)
        pt = _add(pt, pt)
        n >>= 1
    return r


def _enc(pt):
    z = _inv(pt[2])
    x = pt[0] * z % _P
    y = pt[1] * z % _P
    return (y | ((x & 1) << 255)).to_bytes(32, 'little')


def _dec(b):
    if len(b) != 32:
        return None
    y = int.from_bytes(b, 'little')
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _x(y, sign)
    return None if x is None else (x, y, 1, x * y % _P)


def ed25519_verify(public, message, signature):
    if len(public) != 32 or len(signature) != 64:
        return False
    a, r = _dec(public), _dec(signature[:32])
    s = int.from_bytes(signature[32:], 'little')
    if a is None or r is None or s >= _Q:
        return False
    h = int.from_bytes(hashlib.sha512(signature[:32] + public + message).digest(), 'little') % _Q
    left, right = _mul(s, _G), _add(r, _mul(h, a))
    return ((left[0] * right[2] - right[0] * left[2]) % _P == 0
            and (left[1] * right[2] - right[1] * left[2]) % _P == 0)


def ed25519_public(secret):
    h = hashlib.sha512(secret).digest()
    a = int.from_bytes(h[:32], 'little')
    a &= (1 << 254) - 8
    a |= 1 << 254
    return _enc(_mul(a, _G))


def ed25519_sign(secret, message):
    h = hashlib.sha512(secret).digest()
    a = int.from_bytes(h[:32], 'little')
    a &= (1 << 254) - 8
    a |= 1 << 254
    pub = _enc(_mul(a, _G))
    r = int.from_bytes(hashlib.sha512(h[32:] + message).digest(), 'little') % _Q
    rb = _enc(_mul(r, _G))
    k = int.from_bytes(hashlib.sha512(rb + pub + message).digest(), 'little') % _Q
    return rb + ((r + k * a) % _Q).to_bytes(32, 'little')


# ---- release handling ---------------------------------------------------------------------------

def vtuple(v):
    return tuple(int(x) for x in v.split('.'))


def fetch_bytes(url, limit):
    if not url.startswith('https://') and not url.startswith('http://127.0.0.1:'):
        raise RuntimeError('the update channel must use HTTPS')
    with urlopen(Request(url), timeout=30) as r:
        data = r.read(limit + 1)
    if len(data) > limit:
        raise RuntimeError('update file larger than allowed')
    return data


def zip_members(zf, version):
    """The program's own files only, nothing that could land outside the stage directory."""
    top = 'taskd-%s/' % version
    out, seen, total = [], set(), 0
    infos = zf.infolist()
    if len(infos) > MAX_FILES:
        raise RuntimeError('update zip has too many entries')
    for info in infos:
        name = info.filename
        parts = name.split('/')
        if (name.startswith('/') or '\\' in name or '\x00' in name or '..' in parts
                or re.match(r'^[A-Za-z]:', name) or not name.startswith(top)):
            raise RuntimeError('update zip has an unsafe path: %r' % name[:80])
        mode = (info.external_attr >> 16) & 0o170000
        if mode not in (0, 0o100000, 0o040000):
            raise RuntimeError('update zip holds a link or special file: %r' % name[:80])
        if info.is_dir():
            continue
        rel = name[len(top):]
        if not (rel in ('run_taskd.py', 'README.md') or (rel.startswith('taskd/') and rel.endswith('.py'))):
            raise RuntimeError('update zip holds an unexpected file: %r' % rel[:80])
        if rel in seen or info.file_size > MAX_FILE:
            raise RuntimeError('update zip has a duplicate or oversized file: %r' % rel[:80])
        seen.add(rel)
        total += info.file_size
        out.append((info, rel))
    if total > MAX_TOTAL or 'run_taskd.py' not in seen or 'taskd/service.py' not in seen:
        raise RuntimeError('update zip is not a taskd program')
    return out


def restart_action(platform=None, env=None):
    """How this process hands over to the new program. Returns ('exit', code), ('exec',) or ('spawn',).

    A supervising parent (the Windows supervisor, launchd KeepAlive, a systemd unit) restarts the
    program when it exits; with none, a POSIX process replaces itself in place and Windows starts a
    detached copy."""
    platform = platform or sys.platform
    env = os.environ if env is None else env
    if env.get('TASKD_SUPERVISED'):
        return ('exit', 0)
    if platform == 'darwin' and env.get('XPC_SERVICE_NAME', '').startswith('org.taskd.'):
        return ('exit', 0)
    if platform.startswith('linux') and env.get('INVOCATION_ID'):
        return ('exit', RESTART_EXIT_CODE)
    return ('spawn',) if platform == 'win32' else ('exec',)


def restart(argv, platform=None, env=None, log_path=None):
    """Called once the daemon has stopped cleanly. Returns the process exit code (exec never returns)."""
    action = restart_action(platform, env)
    if action[0] == 'exit':
        return action[1]
    os.environ['TASKD_RESTARTED'] = '1'
    argv = [str(a) for a in argv]
    if action[0] == 'exec':
        os.execv(argv[0], argv)
        return 0  # not reached: the process image has been replaced
    start_detached(argv, log_path)
    return 0


def select_release(manifest, node_hash):
    """Pure selector. Target hashes are rollout labels, not authentication credentials.

    Exact allowlists only; omitted node identity never opts into a canary. Validate
    every release before choosing, so malformed targeting cannot silently go global.
    """
    if set(manifest) - {'schema', 'serial', 'issued', 'releases'}:
        raise RuntimeError('unknown v2 manifest fields')
    releases = manifest.get('releases')
    if not isinstance(releases, list) or len(releases) > 100:
        raise RuntimeError('invalid release list')
    stable, canary, seen, versions = None, None, set(), {}
    for item in releases:
        if not isinstance(item, dict) or set(item) != {'component', 'audience'}:
            raise RuntimeError('invalid scoped release')
        comp, audience = item['component'], item['audience']
        if not isinstance(comp, dict) or set(comp) != {'version', 'file', 'sha256'}:
            raise RuntimeError('invalid release component')
        v, name, digest = comp['version'], comp['file'], comp['sha256']
        if (not isinstance(v, str) or not re.fullmatch(r'\d+\.\d+\.\d+', v)
                or name != 'taskd-%s.zip' % v or not isinstance(digest, str)
                or not re.fullmatch(r'[0-9a-f]{64}', digest)):
            raise RuntimeError('invalid release component')
        if v in versions and versions[v] != comp:
            raise RuntimeError('release version collision')
        versions[v] = comp
        if audience == {'kind': 'all'}:
            if stable is not None:
                raise RuntimeError('multiple stable releases')
            stable = comp
        elif isinstance(audience, dict) and set(audience) == {'kind', 'node_sha256'} and audience['kind'] == 'nodes':
            nodes = audience['node_sha256']
            if (not isinstance(nodes, list) or not nodes or len(nodes) > 1000
                    or any(not isinstance(n, str) or not re.fullmatch(r'[0-9a-f]{64}', n) for n in nodes)
                    or len(set(nodes)) != len(nodes) or seen.intersection(nodes)):
                raise RuntimeError('invalid or overlapping node targets')
            seen.update(nodes)
            if node_hash in nodes:
                canary = comp
        else:
            raise RuntimeError('invalid release audience')
    if canary is not None and (stable is None or vtuple(canary['version']) > vtuple(stable['version'])):
        return canary, 'node'
    return stable, 'stable'


class Updater:
    def __init__(self, root, app_dir, base, *, version, key=None, is_idle=None, hold=None, request_restart=None,
                 selftest=None, was_ready=None, log=None, verify_delay=None, running_app=None, node_aid=None):
        self.root, self.app = Path(root), Path(app_dir)
        self.base = base.rstrip('/') if base else None
        self.version, self.key = version, key or UPDATE_KEY
        self.is_idle = is_idle or (lambda: True)
        self.hold = hold or (lambda on: None)
        self.request_restart = request_restart or (lambda: None)
        self.selftest, self.was_ready = selftest, was_ready or (lambda: True)
        self.log = log
        self.verify_delay = VERIFY_DELAY_SECONDS if verify_delay is None else verify_delay
        self.running_app = running_app
        self.node_aid = node_aid
        self.dir = self.root / 'updates'
        self._startup_done = False

    # -- state --
    def _say(self, msg, *args):
        if self.log:
            self.log.info(msg, *args)

    def state(self):
        return read_json(self.dir / 'state.json', {}) or {}

    def save_state(self, **changes):
        self.dir.mkdir(parents=True, exist_ok=True)
        st = dict(self.state(), **changes)
        atomic_json(self.dir / 'state.json', st)
        return st

    def local_patch_guard(self):
        """A local ledger requires explicit reconciliation before any tree replacement.

        Presence is fail-closed, including invalid/unreadable ledgers. Hash checks
        explain drift; they never authorize an automatic upstream replacement.
        This guard reads only the ledger and its ordinary app Python sources.
        """
        path = self.root / 'local-patches.json'
        try:
            info = path.lstat()
        except FileNotFoundError:
            return {'active': False, 'state': 'absent'}
        except OSError:
            return {'active': True, 'state': 'unreadable'}
        result = {'active': True, 'state': 'invalid'}
        try:
            if (not stat.S_ISREG(info.st_mode)
                    or getattr(info, 'st_file_attributes', 0) & 0x400
                    or info.st_size > 2_000_000):
                return result
            with path.open('rb') as source:
                opened = os.fstat(source.fileno())
                if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                    return result
                raw = source.read(2_000_001)
                current = os.fstat(source.fileno())
            final = path.lstat()
            if (len(raw) > 2_000_000
                    or (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns)
                       != (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
                    or (final.st_dev, final.st_ino, final.st_size, final.st_mtime_ns)
                       != (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)):
                return result
            result['ledger_sha256'] = hashlib.sha256(raw).hexdigest()
            ledger = json.loads(raw)
            files = ledger.get('files') if isinstance(ledger, dict) else None
            if (ledger.get('schema') != 1 or ledger.get('component') != 'taskd'
                    or not isinstance(files, dict) or not files or len(files) > MAX_FILES):
                return result
            expected = {}
            for name, entry in files.items():
                if (not isinstance(name, str) or not re.fullmatch(r'app/(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\.py', name)
                        or any(part in ('.', '..') for part in name.split('/'))
                        or not isinstance(entry, dict)
                        or not re.fullmatch(r'[0-9a-f]{64}', str(entry.get('after', '')))):
                    return result
                expected[name[4:]] = entry['after']
            preserved = ledger.get('preserved_existing_fixes', {})
            if not isinstance(preserved, dict) or len(preserved) > MAX_FILES:
                return result
            for name, digest in preserved.items():
                if (not isinstance(name, str) or not re.fullmatch(r'(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\.py', name)
                        or any(part in ('.', '..') for part in name.split('/'))
                        or not re.fullmatch(r'[0-9a-f]{64}', str(digest))
                        or (name in expected and expected[name] != digest)):
                    return result
                expected[name] = digest
            app_info = self.app.lstat()
            if (not stat.S_ISDIR(app_info.st_mode)
                    or getattr(app_info, 'st_file_attributes', 0) & 0x400):
                return result
            drift = 0
            for name, digest in expected.items():
                target = self.app
                for part in name.split('/'):
                    target = target / part
                    meta = target.lstat()
                    if stat.S_ISLNK(meta.st_mode) or getattr(meta, 'st_file_attributes', 0) & 0x400:
                        return result
                if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                    drift += 1
            result.update(state='drift' if drift else 'protected', files=len(expected), drifted_files=drift)
            return result
        except (OSError, ValueError, TypeError, AttributeError):
            return result

    def _refuse_local_patch_replace(self):
        guard = self.local_patch_guard()
        if guard['active']:
            self.save_state(result='blocked:local-patches:' + guard['state'])
            return True
        return False

    def auto_on(self):
        return not (self.root / 'auto-update-off').exists()

    def set_auto(self, on):
        flag = self.root / 'auto-update-off'
        if on and self.local_patch_guard()['active']:
            raise RuntimeError('local taskd patches require reconciliation before enabling automatic updates')
        if on:
            flag.unlink(missing_ok=True)
        else:
            flag.write_text('1', encoding='utf-8')

    def public_state(self):
        st = self.state()
        guard = self.local_patch_guard()
        return {'local_patch_guard': guard,
                'auto_update_effective': self.auto_on() and not guard['active'],
                'auto_update': self.auto_on(), 'update_state': str(st.get('result', ''))[:160],
                'update_current_version': self.version, 'update_available_version': st.get('available_version', ''),
                'update_last_check': st.get('last_check'), 'update_manifest_schema': st.get('manifest_schema'),
                'update_rollout': st.get('rollout', ''), 'update_pending': bool(self.pending())}

    def pending(self):
        return read_json(self.dir / 'pending.json')

    def needs_validation(self):
        return (self.dir / 'pending.json').exists() or (self.dir / 'rollback.json').exists()

    # -- checking and applying --
    def check(self, apply=True):
        """'current', 'available', 'blocked', 'busy' or 'updated'. Raises on anything untrusted."""
        if apply and self._refuse_local_patch_replace():
            return 'blocked'
        if not self.base:
            raise RuntimeError('no update channel: this node has no portal connection')
        base = self.base + CHANNEL_PATH
        st = self.state()
        # Separate endpoint is essential: legacy clients ignore unknown targeting fields.
        stem = 'taskd-manifest-v2'
        try:
            raw = fetch_bytes(base + '/' + stem + '.json', MAX_MANIFEST)
        except HTTPError as exc:
            if exc.code != 404 or st.get('manifest_schema') == 2:
                exc.close()
                raise
            exc.close()
            stem = 'manifest'
            raw = fetch_bytes(base + '/manifest.json', MAX_MANIFEST)
        try:
            sig = base64.b64decode(fetch_bytes(base + '/' + stem + '.sig', 1024).strip(), validate=True)
        except ValueError:
            raise RuntimeError('update manifest signature is malformed') from None
        if not ed25519_verify(self.key, raw, sig):
            raise RuntimeError('update manifest signature is invalid; ignored')
        m = json.loads(raw)
        schema = 2 if stem == 'taskd-manifest-v2' else 1
        if m.get('schema') != schema or type(m.get('serial')) is not int:
            raise RuntimeError('update manifest is malformed')
        if m['serial'] < st.get('serial', 0):
            raise RuntimeError('update manifest is older than one already accepted (rollback refused)')
        aid = self.node_aid() if callable(self.node_aid) else self.node_aid
        node_hash = hashlib.sha256(aid.encode()).hexdigest() if isinstance(aid, str) and aid else None
        comp, rollout = select_release(m, node_hash) if schema == 2 else ((m.get('components') or {}).get('taskd'), 'legacy')
        now = int(time.time())
        self.save_state(serial=m['serial'], last_check=now, manifest_schema=schema, rollout=rollout,
                        available_version=comp.get('version', '') if comp else '')
        if comp is None:
            self.save_state(result='no-release')
            return 'current'
        ver, name, digest = str(comp.get('version', '')), str(comp.get('file', '')), str(comp.get('sha256', ''))
        if (not re.fullmatch(r'\d+\.\d+\.\d+', ver) or not re.fullmatch(r'[0-9a-f]{64}', digest)
                or name != 'taskd-%s.zip' % ver):
            raise RuntimeError('update manifest has no valid taskd release')
        if ver in st.get('blocked', []):
            self.save_state(result='blocked:' + ver)
            return 'blocked'
        if vtuple(ver) <= vtuple(self.version):
            self.save_state(result=('local-ahead:' + ver) if vtuple(ver) < vtuple(self.version) else 'current')
            return 'current'
        if not apply:
            self.save_state(result='available:' + ver)
            return 'available'
        if self.running_app is not None and Path(self.running_app).resolve() != self.app.resolve():
            raise RuntimeError('this is not the installed copy (a source checkout?); not replacing it')
        if self.pending():
            self.save_state(result='pending-verification:' + ver)
            return 'busy'  # the previous update has not proved itself yet
        data = fetch_bytes('%s/taskd-%s/%s' % (base, ver, name), MAX_ZIP)
        if hashlib.sha256(data).hexdigest() != digest:
            raise RuntimeError('downloaded taskd zip does not match the signed checksum')
        stage = self._stage(data, ver)
        try:
            return self._swap(stage, ver)
        finally:
            shutil.rmtree(stage.parent, ignore_errors=True)

    def _stage(self, data, ver):
        """Unpack, compile every file and run the new program's own version command. Nothing is replaced."""
        self.dir.mkdir(parents=True, exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix='stage-', dir=self.dir))
        app = work / 'app'
        app.mkdir()
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                for info, rel in zip_members(zf, ver):
                    if rel == 'README.md':
                        continue
                    target = (app / rel).resolve()
                    if app.resolve() not in target.parents:
                        raise RuntimeError('update zip path escapes the stage directory')
                    target.parent.mkdir(parents=True, exist_ok=True)
                    content = zf.read(info)
                    if rel.endswith('.py'):
                        compile(content, rel, 'exec')
                    target.write_bytes(content)
            service = (app / 'taskd' / 'service.py').read_text(encoding='utf-8')
            found = re.search(r"^VERSION = '([^']+)'", service, re.M)
            if not found or found.group(1) != ver:
                raise RuntimeError('the zip holds a different version than the manifest signs')
            out = subprocess.run([sys.executable, str(app / 'run_taskd.py'), 'version'], capture_output=True, text=True,
                                 timeout=60, cwd=str(work), env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'),
                                 **background_options())
            if out.returncode or out.stdout.strip() != ver:
                raise RuntimeError('the new program failed its own version check; not installed')
        except BaseException:
            shutil.rmtree(work, ignore_errors=True)
            raise
        return app

    def _swap(self, staged_app, ver):
        if not (self.app / 'taskd' / 'service.py').is_file():
            raise RuntimeError('the running program is not the installed copy; refusing to replace it')
        self.hold(True)  # no new work starts while the program is swapped and the process restarts
        try:
            # Recheck under closed admission immediately before any replacement.
            if self._refuse_local_patch_replace():
                self.hold(False)
                return 'blocked'
            if not self.is_idle():
                self.hold(False)
                self.save_state(result='busy:' + ver)
                return 'busy'
            backup = self.dir / 'backup' / self.version
            shutil.rmtree(backup, ignore_errors=True)
            backup.parent.mkdir(parents=True, exist_ok=True)
            ready = bool(self.was_ready())
            os.replace(self.app, backup)
            try:
                os.replace(staged_app, self.app)
            except BaseException:
                os.replace(backup, self.app)
                raise
            atomic_json(self.dir / 'pending.json', {'version': ver, 'from': self.version, 'tries': 0,
                                                    'backup': str(backup), 'selftest_ok_before': ready,
                                                    'at': int(time.time())})
            self.save_state(result='updated:' + ver, updated_at=int(time.time()))
        except BaseException:
            self.hold(False)
            raise
        self._say('taskd %s installed; restarting to run it', ver)
        self.request_restart()  # the hold stays on: this process is about to end
        return 'updated'

    # -- after a restart --
    def on_start(self):
        """'verify' (a new version must prove itself), 'reselftest' (after a rollback), or None."""
        p = self.pending()
        if (self.dir / 'pending.json').exists():
            if not isinstance(p, dict) or not p.get('version') or not p.get('backup'):
                raise RuntimeError('pending update record is malformed; reconciliation required')
            if p.get('version') != self.version:
                raise RuntimeError('pending update version does not match running program; manual reconciliation required')
            p['tries'] = int(p.get('tries', 0)) + 1
            atomic_json(self.dir / 'pending.json', p)
            if p['tries'] > 3:
                self.rollback('did not start')
                return None
            return 'verify'
        if (self.dir / 'rollback.json').exists():
            return 'reselftest'
        return None

    def verify_pending(self):
        """Confirm the new version, or roll it back. Returns 'confirmed' or 'rolled-back'."""
        p = self.pending()
        if not p or p.get('version') != self.version:
            return None
        if self.selftest:
            ok, detail = self.selftest()
            if not ok:
                self.rollback('selftest failed: ' + str(detail)[:80])
                return 'rolled-back'
        (self.dir / 'pending.json').unlink(missing_ok=True)
        self.save_state(result='confirmed:' + self.version)
        return 'confirmed'

    def rollback(self, reason):
        if self.local_patch_guard()['active']:
            self.hold(True)
            self.save_state(result='maintenance:local-patches-rollback-reconciliation')
            raise RuntimeError('local taskd patches prevent automatic rollback; admission remains closed')
        p = self.pending() or {}
        ver, backup = p.get('version', self.version), Path(p.get('backup', ''))
        self._say('rolling back taskd %s: %s', ver, reason)
        if not (backup / 'taskd' / 'service.py').is_file():
            self.hold(True)
            self.save_state(result='error:no-backup')
            raise RuntimeError('update rollback backup is missing; admission remains closed')
        failed = self.dir / 'failed' / str(ver)
        shutil.rmtree(failed, ignore_errors=True)
        failed.parent.mkdir(parents=True, exist_ok=True)
        os.replace(self.app, failed)
        os.replace(backup, self.app)
        st = self.state()
        self.save_state(blocked=sorted(set(st.get('blocked', []) + [str(ver)])), result='rolled-back:' + str(ver))
        atomic_json(self.dir / 'rollback.json', {'version': str(ver), 'reason': reason, 'at': int(time.time())})
        (self.dir / 'pending.json').unlink(missing_ok=True)
        self.hold(True)
        self.request_restart()
        return True

    def reselftest(self):
        """The old program runs again after a rollback; its self-test record was overwritten by the new code's."""
        marker = read_json(self.dir / 'rollback.json', {}) or {}
        if not self.selftest:
            (self.dir / 'rollback.json').unlink(missing_ok=True)
            return True
        ok, _ = self.selftest()
        if ok:
            (self.dir / 'rollback.json').unlink(missing_ok=True)
        if not ok and marker.get('version'):
            # Fails on the old code as well, so the new version was not the cause: do not block it.
            st = self.state()
            self.save_state(blocked=[v for v in st.get('blocked', []) if v != marker['version']],
                            result='rolled-back:%s(environment)' % marker['version'])
        return ok

    # -- the scheduled loop --
    def _wait(self, stop, seconds):
        """Sleep, waking early for a stop or a `taskd update` asked of the running daemon."""
        end = time.monotonic() + seconds
        flag = self.dir / 'check-now'
        while not stop.is_set() and time.monotonic() < end:
            if flag.exists():
                flag.unlink(missing_ok=True)
                return True
            stop.wait(min(30, max(0.0, end - time.monotonic())))
        return False

    def startup(self, stop):
        """Validate before daemon recovery or admission. Errors leave a durable gate closed.

        Must run with the daemon process lock held. On successful rollback the restart
        callback sets stop; the old program verifies itself on its next start instead.
        """
        if self._startup_done:
            return not stop.is_set()
        needs_validation = self.needs_validation()
        if needs_validation:
            self.hold(True)
        try:
            if needs_validation and self.local_patch_guard()['active']:
                raise RuntimeError('local taskd patches conflict with pending update; reconciliation required')
            if needs_validation and not self.is_idle():
                raise RuntimeError('update verification waits for safe native stop and idle task state')
            action = self.on_start()
            if action == 'verify':
                stop.wait(self.verify_delay)
                if stop.is_set():
                    return False
                if self.verify_pending() != 'confirmed':
                    return False
            elif action == 'reselftest':
                if not self.reselftest():
                    raise RuntimeError('rolled-back program selftest failed; admission remains closed')
            if stop.is_set():
                return False
            if self.pending() or (self.dir / 'rollback.json').exists():
                raise RuntimeError('update validation markers remain unresolved')
            self._startup_done = True
            if needs_validation:
                self.hold(False)
            return True
        except Exception as e:
            self.hold(True)
            self.save_state(result='maintenance:' + str(e)[:120])
            if self.log:
                self.log.warning('update startup verification: %s', e)
            return False

    def run(self, stop, first_delay=None):
        if not self.startup(stop):
            return
        delay = random.randint(120, 600) if first_delay is None else first_delay
        failures = 0
        while not stop.is_set():
            forced = self._wait(stop, delay)
            if stop.is_set():
                break
            delay = CHECK_SECONDS + random.randint(0, 1800)
            if not (self.auto_on() or forced):
                continue
            try:
                if self.check(apply=True) == 'busy':
                    delay = BUSY_RETRY_SECONDS
                failures = 0
            except Exception as e:
                failures += 1
                delay = min(600, 30 * (2 ** min(failures, 5))) + random.randint(0, 30)
                self.save_state(last_check=int(time.time()), result='error:' + str(e)[:120],
                                retry_after_seconds=delay)
                if self.log:
                    self.log.warning('update check: %s', e)
