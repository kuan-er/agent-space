"""Server-side WorkBuddy OAuth broker. Never distribute the application secret."""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import threading
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler

SCOPES = 'user.localassistant.readable user.localassistant.invokable'
ORIGIN = 'https://www.workbuddy.cn'


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise RuntimeError('WorkBuddy 授权接口重定向被拒绝')


class WorkBuddy:
    def __init__(self, portal):
        self.p = portal
        self.guard = threading.Lock()
        self.locks = {}

    def lock(self, agent_id):
        with self.guard:
            return self.locks.setdefault(agent_id, threading.RLock())

    def config(self):
        client = os.environ.get('WORKBUDDY_CLIENT_ID', '').strip()
        secret_path = os.environ.get('WORKBUDDY_CLIENT_SECRET_FILE', '')
        base = self.p.BASE.rstrip('/')
        u = urlsplit(base)
        if not client or not secret_path or u.scheme != 'https' or not u.netloc or u.username or u.password or u.path or u.query or u.fragment:
            raise self.p.Problem('WorkBuddy 应用尚未配置，请管理员配置官方应用及回调地址', 503)
        try:
            path = Path(secret_path)
            if path.is_symlink() or path.stat().st_mode & 0o077:
                raise ValueError('private file required')
            secret = path.read_text().strip()
            if not secret or len(secret) > 16384 or any(c in secret for c in '\r\n'):
                raise ValueError('invalid secret')
        except (OSError, ValueError):
            raise self.p.Problem('WorkBuddy 应用密钥未就绪，请管理员检查服务器私有配置', 503) from None
        return client, secret, base + '/api/workbuddy/callback'

    def configured(self):
        try:
            self.config()
            return True
        except self.p.Problem:
            return False

    @contextlib.contextmanager
    def db(self):
        folder = self.p.DATA / 'workbuddy'
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        if folder.is_symlink():
            raise self.p.Problem('WorkBuddy 私有存储不可用', 503)
        folder.chmod(0o700)
        path = folder / 'oauth.sqlite3'
        if path.is_symlink():
            raise self.p.Problem('WorkBuddy 私有存储不可用', 503)
        c = sqlite3.connect(path, timeout=10)
        path.chmod(0o600)
        c.row_factory = sqlite3.Row
        try:
            with c:
                c.execute('CREATE TABLE IF NOT EXISTS states(hash TEXT PRIMARY KEY,agent_id TEXT NOT NULL,owner_id TEXT NOT NULL,aid TEXT NOT NULL,expires INTEGER NOT NULL)')
                c.execute('CREATE TABLE IF NOT EXISTS grants(agent_id TEXT PRIMARY KEY,owner_id TEXT NOT NULL,aid TEXT NOT NULL,client_id TEXT NOT NULL,access_token TEXT NOT NULL,refresh_token TEXT NOT NULL,expires_at INTEGER NOT NULL,scope TEXT NOT NULL,status TEXT NOT NULL)')
                yield c
        finally:
            c.close()

    def current(self, a):
        with self.p.db() as c:
            fresh = self.p.one(c, 'SELECT a.* FROM agents a JOIN users u ON u.id=a.owner_id WHERE a.id=? AND a.enabled=1 AND u.enabled=1', (a['id'],))
        if not fresh or fresh['kind'] != 'agent' or fresh['executor'] != 'workbuddy' or not fresh['aid'] or fresh['aid'] != a['aid'] or fresh['owner_id'] != a['owner_id']:
            raise self.p.Problem('WorkBuddy 连接已改变，请重新绑定并授权', 409)
        return fresh

    def status(self, a):
        self.current(a)
        with self.db() as c:
            row = c.execute('SELECT * FROM grants WHERE agent_id=?', (a['id'],)).fetchone()
        configured = self.configured()
        valid = bool(row and row['aid'] == a['aid'] and row['owner_id'] == a['owner_id'] and row['client_id'] == os.environ.get('WORKBUDDY_CLIENT_ID','').strip())
        authorized = valid and row['status'] == 'authorized'
        return {'configured': configured, 'authorized': authorized,
                'state': 'unconfigured' if not configured else row['status'] if valid else 'unauthorized',
                'expires_at': row['expires_at'] if authorized else None}

    def begin(self, a):
        client, _, redirect = self.config()
        with self.lock(a['id']):
            self.current(a)
            state = secrets.token_urlsafe(32)
            with self.db() as c:
                c.execute('DELETE FROM states WHERE agent_id=? OR expires<?', (a['id'], self.p.now()))
                c.execute('INSERT INTO states VALUES(?,?,?,?,?)', (self.p.digest(state), a['id'], a['owner_id'], a['aid'], self.p.now()+600))
        return {'url': ORIGIN + '/openapi/v2/authorize?' + urlencode({'response_type':'code', 'client_id':client, 'redirect_uri':redirect, 'scope':SCOPES, 'state':state})}

    def exchange(self, fields):
        client, secret, _ = self.config()
        req = Request(ORIGIN + '/openapi/v2/token', data=urlencode({**fields, 'client_id':client, 'client_secret':secret}).encode(),
                      headers={'Content-Type':'application/x-www-form-urlencoded', 'Accept':'application/json'})
        try:
            with build_opener(NoRedirect).open(req, timeout=12) as response:
                raw = response.read(65537)
            if len(raw) > 65536:
                raise ValueError('oversize')
            reply = json.loads(raw)
            if not isinstance(reply, dict) or reply.get('code') != 0 or not isinstance(reply.get('data'), dict):
                raise ValueError('rejected')
            value = reply['data']
            access, refresh, ttl = value.get('access_token'), value.get('refresh_token'), value.get('expires_in')
            if not self.token_valid(access) or not self.token_valid(refresh) or type(ttl) is not int or not 0 < ttl <= 366*86400:
                raise ValueError('invalid tokens')
            scope = value.get('scope')
            if not isinstance(scope, str) or not set(SCOPES.split()).issubset(scope.split()):
                raise ValueError('missing scope')
            return {'access_token':access, 'refresh_token':refresh, 'expires_at':self.p.now()+ttl, 'scope':scope}
        except (HTTPError, URLError, TimeoutError, ValueError, RuntimeError):
            # Responses can contain credentials. Never put them into exceptions or logs.
            raise self.p.Problem('WorkBuddy 授权换取或续期失败，请重新授权；不会自动重复换取', 502) from None

    @staticmethod
    def token_valid(value):
        return isinstance(value, str) and 0 < len(value) <= 32768 and not any(c.isspace() for c in value)

    def complete(self, query):
        state = query.get('state', [''])
        if len(state) != 1 or not 20 <= len(state[0]) <= 256:
            raise self.p.Problem('WorkBuddy 授权状态无效或已过期', 400)
        with self.db() as c:
            row = c.execute('SELECT * FROM states WHERE hash=?', (self.p.digest(state[0]),)).fetchone()
        if not row:
            raise self.p.Problem('WorkBuddy 授权状态无效或已过期', 400)
        a = {'id':row['agent_id'], 'owner_id':row['owner_id'], 'aid':row['aid']}
        with self.lock(a['id']):
            with self.db() as c:
                row = c.execute('DELETE FROM states WHERE hash=? AND expires>? RETURNING *', (self.p.digest(state[0]), self.p.now())).fetchone()
            if not row:
                raise self.p.Problem('WorkBuddy 授权状态无效或已过期', 400)
            self.current(a)
            code = query.get('code', [''])
            if query.get('error') or len(code) != 1 or not 1 <= len(code[0]) <= 4096:
                raise self.p.Problem('WorkBuddy 授权未完成，请返回连接详情重新授权', 400)
            client, _, redirect = self.config()
            value = self.exchange({'grant_type':'authorization_code', 'code':code[0], 'redirect_uri':redirect})
            self.current(a)
            with self.db() as c:
                c.execute('INSERT OR REPLACE INTO grants VALUES(?,?,?,?,?,?,?,?,?)',
                          (a['id'], a['owner_id'], a['aid'], client, value['access_token'], value['refresh_token'], value['expires_at'], value['scope'], 'authorized'))
        return a['id']

    def access(self, a, rejected_hash=None):
        client, _, _ = self.config()
        with self.lock(a['id']):
            self.current(a)
            with self.db() as c:
                row = c.execute('SELECT * FROM grants WHERE agent_id=?', (a['id'],)).fetchone()
            if not row or row['status'] != 'authorized' or row['aid'] != a['aid'] or row['owner_id'] != a['owner_id'] or row['client_id'] != client:
                raise self.p.Problem('请在连接详情中完成 WorkBuddy 授权', 409)
            refresh = row['expires_at'] <= self.p.now()+60 or rejected_hash == hashlib.sha256(row['access_token'].encode()).hexdigest()
            if refresh:
                # Persist before requesting: an ambiguous network failure might have rotated
                # the refresh token. A second process must not replay the old token.
                with self.db() as c:
                    c.execute("UPDATE grants SET status='reauthorize' WHERE agent_id=?", (a['id'],))
                value = self.exchange({'grant_type':'refresh_token', 'refresh_token':row['refresh_token']})
                self.current(a)
                with self.db() as c:
                    c.execute("UPDATE grants SET access_token=?,refresh_token=?,expires_at=?,scope=?,status='authorized' WHERE agent_id=?",
                              (value['access_token'], value['refresh_token'], value['expires_at'], value['scope'], a['id']))
            else:
                value = dict(row)
            return {'access_token':value['access_token'], 'expires_at':value['expires_at']}

    def revoke(self, a):
        with self.lock(a['id']), self.db() as c:
            c.execute('DELETE FROM states WHERE agent_id=?', (a['id'],))
            c.execute('DELETE FROM grants WHERE agent_id=?', (a['id'],))
