"""Server-side WorkBuddy OAuth broker. Never distribute the application secret."""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import secrets
import re
import sqlite3
import stat
import threading
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler

SCOPES = 'user.localassistant.readable user.localassistant.invokable'
CLOUD_SCOPES = 'user.task.readable user.task.invokable'
ORIGIN = 'https://www.workbuddy.cn'
CALLBACK_URI = 'https://124.222.173.36:8888/api/workbuddy/callback'


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
        saved = self._saved_application()
        if saved is not None:
            if self.p.BASE.rstrip('/') + '/api/workbuddy/callback' != CALLBACK_URI:
                raise self.p.Problem('WorkBuddy 回调地址与固定部署地址不一致',503)
            return saved['client_id'],saved['client_secret'],CALLBACK_URI
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

    def _application_path(self):return self.p.DATA/'workbuddy'/'application.json'

    def _saved_application(self):
        path=self._application_path()
        if path.is_symlink():raise self.p.Problem('WorkBuddy 应用私有配置不可用，请管理员检查配置',503)
        if not path.exists():return None
        folder=path.parent
        try:
            for item in (folder,path):
                info=item.lstat()
                kind_ok=stat.S_ISDIR(info.st_mode) if item==folder else stat.S_ISREG(info.st_mode)
                if not kind_ok or item.is_symlink() or info.st_mode & 0o077 or (hasattr(os,'geteuid') and info.st_uid!=os.geteuid()):
                    raise ValueError('private application storage required')
            if path.stat().st_size>32768:raise ValueError('oversized application record')
            value=json.loads(path.read_text('utf-8'))
            if (not isinstance(value,dict) or set(value)!={'schema','client_id','client_secret'} or value.get('schema')!=1
                or not self._application_fields_valid(value['client_id'],value['client_secret'])):
                raise ValueError('invalid application record')
            return value
        except (OSError,ValueError,TypeError):
            raise self.p.Problem('WorkBuddy 应用私有配置不可用，请管理员检查配置',503) from None

    @staticmethod
    def _application_fields_valid(client,secret):
        return (isinstance(client,str) and bool(re.fullmatch('[A-Za-z0-9._:-]{1,256}',client))
            and isinstance(secret,str) and 1<=len(secret)<=16384 and all(33<=ord(c)<=126 for c in secret))

    def application_status(self):
        missing=[];client_present=False;secret_present=False
        try:
            value=self._saved_application()
            if value is not None:
                client_present=bool(value['client_id']);secret_present=bool(value['client_secret'])
            else:
                client_present=bool(os.environ.get('WORKBUDDY_CLIENT_ID','').strip())
                file=os.environ.get('WORKBUDDY_CLIENT_SECRET_FILE','')
                secret_present=bool(file and Path(file).is_file())
            if not client_present:missing.append('client_id')
            if not secret_present:missing.append('client_secret')
            configured=self.configured()
            if not configured and not missing:missing.append('private_configuration_or_callback')
        except self.p.Problem:
            configured=False;missing=['private_configuration']
        return {'configured':configured,'client_id_present':client_present,'secret_present':secret_present,
                'redirect_uri':CALLBACK_URI,'missing':missing}

    def configure_application(self,fields):
        if (not isinstance(fields,dict) or set(fields)!={'client_id','client_secret'}
            or not self._application_fields_valid(fields['client_id'],fields['client_secret'])):
            raise self.p.Problem('请填写有效的 Client ID 和 Client Secret，密钥不要包含空白或控制字符',400)
        folder=self._application_path().parent
        try:
            folder.mkdir(mode=0o700,exist_ok=True)
            info=folder.lstat()
            if not stat.S_ISDIR(info.st_mode) or folder.is_symlink() or info.st_mode&0o077 or (hasattr(os,'geteuid') and info.st_uid!=os.geteuid()):
                raise ValueError('existing private storage ownership differs')
            path=self._application_path()
            if path.exists() or path.is_symlink():self._saved_application()
            temp=folder/('application-'+uuid.uuid4().hex+'.tmp')
            descriptor=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0),0o600)
            try:
                with os.fdopen(descriptor,'w',encoding='utf-8') as output:
                    json.dump(dict(schema=1,**fields),output,ensure_ascii=False);output.flush();os.fsync(output.fileno())
                os.replace(temp,path)
            finally:
                if temp.exists():temp.unlink()
        except (OSError,ValueError):
            raise self.p.Problem('WorkBuddy 应用私有配置保存失败，未放宽原有目录权限',503) from None
        return self.application_status()

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
        # A multi-harness Windows connection remains a caller device. Its owner
        # explicitly starts OAuth via the authenticated + CSRF-protected route;
        # access() still requires that exact owner/node/AID's completed grant.
        eligible = bool(fresh and (fresh['kind'] == 'device' or
                                   fresh['kind'] == 'agent' and fresh['executor'] == 'workbuddy'))
        if not eligible or not fresh['aid'] or fresh['aid'] != a['aid'] or fresh['owner_id'] != a['owner_id']:
            raise self.p.Problem('WorkBuddy 连接已改变，请重新绑定并授权', 409)
        return fresh

    def status(self, a):
        self.current(a)
        with self.db() as c:
            row = c.execute('SELECT * FROM grants WHERE agent_id=?', (a['id'],)).fetchone()
        configured = self.configured()
        try:current_client=self.config()[0]
        except self.p.Problem:current_client=None
        valid = bool(row and row['aid'] == a['aid'] and row['owner_id'] == a['owner_id'] and row['client_id'] == current_client)
        authorized = valid and row['status'] == 'authorized'
        return {'configured': configured, 'authorized': authorized,
                'state': 'unconfigured' if not configured else row['status'] if valid else 'unauthorized',
                'expires_at': row['expires_at'] if authorized else None,
                'local_assistant_scope': bool(authorized and set(SCOPES.split()).issubset(row['scope'].split())),
                'cloud_tasks_scope': bool(authorized and set(CLOUD_SCOPES.split()).issubset(row['scope'].split())),
                'authorization_target': 'local_assistant'}

    def begin(self, a, purpose='local_assistant'):
        client, _, redirect = self.config()
        with self.lock(a['id']):
            fresh = self.current(a)
            if purpose not in ('local_assistant','cloud_tasks') or (purpose=='cloud_tasks' and fresh['kind']!='device'):
                raise self.p.Problem('请选择明确的本地助理或云端任务授权用途',400)
            cloud = purpose=='cloud_tasks'
            requested_scope = CLOUD_SCOPES if cloud else SCOPES
            # The full random state is stored by digest. Its scope marker is
            # inseparable from that one-use state, without a database migration.
            state = ('cloud.' if cloud else 'legacy.') + secrets.token_urlsafe(32)
            with self.db() as c:
                c.execute('DELETE FROM states WHERE agent_id=? OR expires<?', (a['id'], self.p.now()))
                c.execute('INSERT INTO states VALUES(?,?,?,?,?)', (self.p.digest(state), a['id'], a['owner_id'], a['aid'], self.p.now()+600))
        return {'url': ORIGIN + '/openapi/v2/authorize?' + urlencode({'response_type':'code', 'client_id':client, 'redirect_uri':redirect, 'scope':requested_scope, 'state':state}),
                'authorization_target':'cloud_tasks' if cloud else 'local_assistant'}

    def exchange(self, fields, required_scope=SCOPES):
        client, secret, _ = self.config()
        req = Request(ORIGIN + '/openapi/v2/token', data=urlencode({**fields, 'client_id':client, 'client_secret':secret}).encode(),
                      headers={'Content-Type':'application/x-www-form-urlencoded', 'Accept':'application/json'})
        try:
            with build_opener(NoRedirect).open(req, timeout=12) as response:
                raw = response.read(65537)
            if len(raw) > 65536:
                raise ValueError('oversize')
            reply = json.loads(raw)
            if not isinstance(reply,dict):raise ValueError('invalid OAuth token response')
            if 'code' in reply:
                if reply.get('code')!=0 or not isinstance(reply.get('data'),dict):raise ValueError('rejected')
                value=reply['data']
            else:value=reply
            access, refresh, ttl = value.get('access_token'), value.get('refresh_token'), value.get('expires_in')
            if 'refresh_token' not in value and fields.get('grant_type')=='refresh_token':refresh=fields.get('refresh_token')
            if not self.token_valid(access) or not self.token_valid(refresh) or type(ttl) is not int or not 0 < ttl <= 366*86400:
                raise ValueError('invalid tokens')
            scope = value.get('scope')
            if not isinstance(scope, str) or not set(required_scope.split()).issubset(scope.split()):
                raise ValueError('missing scope')
            return {'access_token':access, 'refresh_token':refresh, 'expires_at':self.p.now()+ttl, 'scope':scope}
        except (HTTPError, URLError, TimeoutError, ValueError, RuntimeError):
            # Responses can contain credentials. Never put them into exceptions or logs.
            raise self.p.Problem('WorkBuddy 授权换取或续期失败，请重新授权；不会自动重复换取', 502) from None

    @staticmethod
    def token_valid(value):
        return isinstance(value, str) and 0 < len(value) <= 32768 and bool(re.fullmatch(r'[A-Za-z0-9._~+/\-]+=*',value))

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
            required_scope = CLOUD_SCOPES if state[0].startswith('cloud.') else SCOPES
            value = self.exchange({'grant_type':'authorization_code', 'code':code[0], 'redirect_uri':redirect}, required_scope)
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
            if not self.token_valid(row['access_token']) or not self.token_valid(row['refresh_token']) or type(row['expires_at']) is not int or not isinstance(row['scope'],str):
                raise self.p.Problem('WorkBuddy 授权记录无效，请重新授权',409)
            refresh = row['expires_at'] <= self.p.now()+60 or rejected_hash == hashlib.sha256(row['access_token'].encode()).hexdigest()
            if refresh:
                # Persist before requesting: an ambiguous network failure might have rotated
                # the refresh token. A second process must not replay the old token.
                with self.db() as c:
                    c.execute("UPDATE grants SET status='reauthorize' WHERE agent_id=?", (a['id'],))
                known_scopes=(SCOPES+' '+CLOUD_SCOPES).split()
                required_scope=' '.join(scope for scope in known_scopes if scope in row['scope'].split())
                if not required_scope:raise self.p.Problem('WorkBuddy 授权缺少已知用途权限，请重新授权',409)
                value = self.exchange({'grant_type':'refresh_token', 'refresh_token':row['refresh_token']},required_scope)
                self.current(a)
                with self.db() as c:
                    c.execute("UPDATE grants SET access_token=?,refresh_token=?,expires_at=?,scope=?,status='authorized' WHERE agent_id=?",
                              (value['access_token'], value['refresh_token'], value['expires_at'], value['scope'], a['id']))
            else:
                value = dict(row)
            return {'access_token':value['access_token'], 'expires_at':value['expires_at'],
                    'scope':value['scope'],
                    'local_assistant_scope':set(SCOPES.split()).issubset(value['scope'].split()),
                    'cloud_tasks_scope':set(CLOUD_SCOPES.split()).issubset(value['scope'].split())}

    def revoke(self, a):
        with self.lock(a['id']), self.db() as c:
            c.execute('DELETE FROM states WHERE agent_id=?', (a['id'],))
            c.execute('DELETE FROM grants WHERE agent_id=?', (a['id'],))
