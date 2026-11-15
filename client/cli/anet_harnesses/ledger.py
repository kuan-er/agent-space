from pathlib import Path
from contextlib import contextmanager
import hashlib,json,sqlite3,time

class EffectUncertain(RuntimeError): pass
class OwnershipError(RuntimeError): pass
class ConflictingOperation(RuntimeError): pass

class Ledger:
    """Commit intent before dispatch; an ambiguous send is never auto-replayed.

    This guarantees at most one local dispatch for an operation id. It does not
    invent backend exactly-once execution or equate enqueue ACK with completion.
    """
    def __init__(self,path):
        self.path=Path(path).resolve();self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.connect() as c:
            c.executescript('''CREATE TABLE IF NOT EXISTS sessions(
                id TEXT PRIMARY KEY,harness TEXT NOT NULL,native_id TEXT NOT NULL,
                workspace TEXT NOT NULL,transport TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS operations(
                id TEXT PRIMARY KEY,session_id TEXT NOT NULL,kind TEXT NOT NULL,
                digest TEXT NOT NULL,state TEXT NOT NULL,native_id TEXT,
                result TEXT,updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS native_creations(
                id TEXT PRIMARY KEY,harness TEXT NOT NULL,workspace TEXT NOT NULL,
                native_id TEXT,state TEXT NOT NULL,name TEXT NOT NULL,updated REAL NOT NULL);''')
    def creation(self,local_id):
        with self.connect() as c:row=c.execute('SELECT * FROM native_creations WHERE id=?',(local_id,)).fetchone()
        return dict(row) if row else None
    def reserve_creation(self,local_id,harness,workspace,name):
        workspace=str(Path(workspace).resolve())
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute('SELECT * FROM native_creations WHERE id=?',(local_id,)).fetchone()
            if row:
                if (row['harness'],row['workspace'],row['name'])!=(harness,workspace,name):raise OwnershipError('source creation intent differs')
                return False
            c.execute('INSERT INTO native_creations VALUES(?,?,?,?,?,?,?)',(local_id,harness,workspace,None,'start_requested',name,time.time()))
        return True
    def update_creation(self,local_id,state,native_id=None):
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute('SELECT * FROM native_creations WHERE id=?',(local_id,)).fetchone()
            if not row:raise OwnershipError('source creation intent missing')
            if row['native_id'] and native_id and row['native_id']!=native_id:raise OwnershipError('native creation identity changed')
            c.execute('UPDATE native_creations SET state=?,native_id=?,updated=? WHERE id=?',
                      (state,native_id or row['native_id'],time.time(),local_id))
    @contextmanager
    def connect(self):
        c=sqlite3.connect(self.path,timeout=10);c.row_factory=sqlite3.Row
        try:
            with c:yield c
        finally:c.close()
    def bind(self,local_id,harness,native_id,workspace,transport):
        value=(local_id,harness,native_id,str(Path(workspace).resolve()),transport)
        with self.connect() as c:
            row=c.execute('SELECT * FROM sessions WHERE id=?',(local_id,)).fetchone()
            if row and tuple(row)!=value:raise OwnershipError('session binding differs')
            c.execute('INSERT OR IGNORE INTO sessions VALUES(?,?,?,?,?)',value)
    def session(self,local_id,harness=None):
        with self.connect() as c:row=c.execute('SELECT * FROM sessions WHERE id=?',(local_id,)).fetchone()
        if not row or harness and row['harness']!=harness:raise OwnershipError('not an owned session')
        return dict(row)
    def get(self,op):
        with self.connect() as c:row=c.execute('SELECT * FROM operations WHERE id=?',(op,)).fetchone()
        return dict(row) if row else None
    def sessions(self):
        with self.connect() as c:rows=c.execute('SELECT * FROM sessions ORDER BY id').fetchall()
        return [dict(row) for row in rows]
    def operations(self,session_id=None):
        with self.connect() as c:
            rows=c.execute('SELECT * FROM operations'+(' WHERE session_id=?' if session_id else '')+' ORDER BY updated DESC', (session_id,) if session_id else ()).fetchall()
        return [dict(row) for row in rows]
    def dispatch(self,op,session,kind,payload,send):
        self.session(session)
        digest=hashlib.sha256(json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute('SELECT * FROM operations WHERE id=?',(op,)).fetchone()
            if row:
                if (row['session_id'],row['kind'],row['digest'])!=(session,kind,digest):raise ConflictingOperation('operation id reused with different input')
                if row['state'] in ('dispatching','uncertain'):raise EffectUncertain('query/reconcile original operation; do not resend')
                return dict(row)
            c.execute('INSERT INTO operations VALUES(?,?,?,?,?,?,?,?)',(op,session,kind,digest,'dispatching',None,None,time.time()))
        try:
            receipt=send()
            local_observation=(kind=='workbuddy_local_prompt' and isinstance(receipt,dict) and receipt.get('local_observation_only') is True)
            if not isinstance(receipt,dict) or (not local_observation and (not isinstance(receipt.get('native_id'),str) or not receipt['native_id'])):
                raise EffectUncertain('backend returned no durable native identity')
        except BaseException:
            with self.connect() as c:c.execute('UPDATE operations SET state=?,updated=? WHERE id=?',('uncertain',time.time(),op))
            raise
        with self.connect() as c:
            state=receipt.get('state','acknowledged')
            allowed=('acknowledged','submitted_unconfirmed','completed','failed','interrupted')
            if local_observation:allowed=('reply_observed','result_unverified')
            if state not in allowed:raise ValueError('invalid receipt state')
            # A fast native notification may already have committed a terminal
            # observation while the dispatch caller was receiving its ACK.
            current=c.execute('SELECT state FROM operations WHERE id=?',(op,)).fetchone()
            if current['state'] not in ('completed','failed','interrupted'):
                c.execute('UPDATE operations SET state=?,native_id=?,result=?,updated=? WHERE id=?',(state,receipt['native_id'],json.dumps(receipt.get('result'),ensure_ascii=False) if 'result' in receipt else None,time.time(),op))
        return self.get(op)
    def promote(self,local_id,expected,native_id):
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE');r=c.execute('SELECT native_id FROM sessions WHERE id=?',(local_id,)).fetchone()
            if not r or r['native_id'] not in (expected,native_id):raise OwnershipError('native source binding differs')
            c.execute('UPDATE sessions SET native_id=? WHERE id=?',(native_id,local_id))
    def observe(self,op,native_id,state,result=None):
        if state not in ('running','waiting_input','completed','failed','interrupted','result_unverified'):raise ValueError('invalid observation')
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE');r=c.execute('SELECT * FROM operations WHERE id=?',(op,)).fetchone()
            if not r or r['native_id']!=native_id:raise OwnershipError('observation is not bound to this exact operation')
            if r['state'] in ('completed','failed','interrupted') and state!=r['state']:raise OwnershipError('terminal operation cannot regress')
            c.execute('UPDATE operations SET state=?,result=?,updated=? WHERE id=?',(state,json.dumps(result,ensure_ascii=False) if result is not None else r['result'],time.time(),op))
        return self.get(op)
