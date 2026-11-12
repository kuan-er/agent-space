#!/usr/bin/env python3
"""Independent team Agent portal. Python standard library; SQLite storage."""
import argparse, base64, calendar, importlib.util, contextlib, hashlib, hmac, json, os, re, secrets, sqlite3, subprocess, threading, time
from collections import defaultdict, deque
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get('PORTAL_DATA', ROOT / '.data'))
BASE = os.environ.get('PORTAL_URL', 'http://127.0.0.1:8890').rstrip('/')
HUB = os.environ.get('ANET_HUB_URL', 'http://127.0.0.1:8088')
HUB_LOCAL = os.environ.get('ANET_HUB_LOCAL', 'http://127.0.0.1:8088')
HELPER = os.environ.get('PORTAL_HUB_HELPER', '')
AID = re.compile(r'^b[a-z2-7]{40,100}$')
LOCK = threading.RLock()
RATES = defaultdict(deque)
# Admission checks seen in the last few minutes: (time, receiving agent id, caller AID, admitted).
# Memory only, never written to disk; the home map lights a link when a node is actually called.
PULSES = deque(maxlen=400)
HARNESSES = [
    {'id':'codex','name':'Codex','description':'使用本机 Codex CLI 执行任务。'},
    {'id':'pi','name':'Pi','description':'使用本机 Pi 执行任务，保留现有接入方式。'},
    {'id':'workbuddy','name':'WorkBuddy','description':'通过官方 Open API 连接本地助理；需要在执行设备完成 WorkBuddy 授权。'},
    {'id':'deepseek','name':'DeepSeek Harness','description':'使用官方 dsh headless 执行任务；模型配置保存在执行设备。'},
    {'id':'custom','name':'其他 / 自定义 harness','description':'接入支持标准输入任务、标准输出最终结果的本地命令。'},
]
EXECUTORS = {h['id'] for h in HARNESSES}

class Problem(Exception):
    def __init__(self, message, status=400): self.message, self.status = message, status

def now(): return int(time.time())
def uid(): return secrets.token_hex(12)
def token(): return secrets.token_urlsafe(32)
def digest(s): return hashlib.sha256(s.encode()).hexdigest()
def password_hash(p):
    if not isinstance(p, str) or len(p) < 8 or len(p) > 200: raise Problem('密码需为 8–200 个字符')
    salt = secrets.token_hex(16)
    return salt + ':' + hashlib.pbkdf2_hmac('sha256', p.encode(), salt.encode(), 310000).hex()
def password_ok(p, stored):
    salt, want = stored.split(':')
    return hmac.compare_digest(hashlib.pbkdf2_hmac('sha256', p.encode(), salt.encode(), 310000).hex(), want)
@contextlib.contextmanager
def db():
    c = sqlite3.connect(DATA / 'portal.sqlite3', timeout=10)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA foreign_keys=ON')
    try:
        with c: yield c
    finally: c.close()
def one(c, q, args=()):
    r = c.execute(q, args).fetchone()
    return dict(r) if r else None

def init():
    DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
    with db() as c:
        c.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY, username TEXT UNIQUE COLLATE NOCASE, name TEXT NOT NULL,
          password TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'member', enabled INTEGER NOT NULL DEFAULT 1, created_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS sessions(hash TEXT PRIMARY KEY, user_id TEXT REFERENCES users(id), csrf TEXT NOT NULL, expires INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS invitations(id TEXT PRIMARY KEY, hash TEXT UNIQUE, kind TEXT NOT NULL, label TEXT NOT NULL,
          target TEXT, created_by TEXT, created_at INTEGER NOT NULL, expires INTEGER NOT NULL, used_at INTEGER);
        CREATE TABLE IF NOT EXISTS agents(id TEXT PRIMARY KEY, owner_id TEXT REFERENCES users(id), name TEXT NOT NULL, description TEXT NOT NULL,
          visibility TEXT NOT NULL DEFAULT 'private', aid TEXT UNIQUE, enabled INTEGER NOT NULL DEFAULT 1,
          pair_hash TEXT, pair_expires INTEGER, challenge TEXT, candidate_aid TEXT, provisioned INTEGER DEFAULT 0,
          node_hash TEXT, last_seen INTEGER, policy_ok INTEGER DEFAULT 0, executor_ready INTEGER DEFAULT 0,
          created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, actor TEXT, action TEXT NOT NULL, subject TEXT, created_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS interactions(agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE, ix TEXT NOT NULL, role TEXT NOT NULL,
          peer TEXT NOT NULL, goal TEXT NOT NULL, state TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(agent_id, ix));
        CREATE TABLE IF NOT EXISTS ix_messages(agent_id TEXT NOT NULL, ix TEXT NOT NULL, msg_id TEXT NOT NULL, sender TEXT NOT NULL, kind TEXT NOT NULL,
          body TEXT NOT NULL, attachments TEXT, created_at TEXT NOT NULL, PRIMARY KEY(agent_id, ix, msg_id),
          FOREIGN KEY(agent_id, ix) REFERENCES interactions(agent_id, ix) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS assistants(user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE, aid TEXT UNIQUE, created_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS assistant_messages(id INTEGER PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          role TEXT NOT NULL, content TEXT NOT NULL, data TEXT, created_at INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS assistant_messages_user ON assistant_messages(user_id,id);
        CREATE TABLE IF NOT EXISTS assistant_actions(id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE, kind TEXT NOT NULL,
          agent_id TEXT, args TEXT NOT NULL, status TEXT NOT NULL, result TEXT, created_at INTEGER NOT NULL, decided_at INTEGER);
        CREATE TABLE IF NOT EXISTS assistant_usage(user_id TEXT NOT NULL, day TEXT NOT NULL, calls INTEGER NOT NULL, PRIMARY KEY(user_id,day));
        ''')
        c.execute('CREATE TABLE IF NOT EXISTS install_reports(id TEXT PRIMARY KEY,agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,created_at INTEGER NOT NULL,updated_at INTEGER NOT NULL,report TEXT NOT NULL)')
        # Additive migration: every existing record retains its executor role and identity.
        cols={r[1] for r in c.execute('PRAGMA table_info(agents)')}
        if 'kind' not in cols:c.execute("ALTER TABLE agents ADD COLUMN kind TEXT NOT NULL DEFAULT 'agent' CHECK(kind IN ('agent','device'))")
        if 'node_connected' not in cols:c.execute('ALTER TABLE agents ADD COLUMN node_connected INTEGER NOT NULL DEFAULT 0')
        if 'executor' not in cols:c.execute("ALTER TABLE agents ADD COLUMN executor TEXT NOT NULL DEFAULT 'codex'")
        if 'capabilities' not in cols:c.execute('ALTER TABLE agents ADD COLUMN capabilities TEXT')
        if 'share_content' not in cols:c.execute('ALTER TABLE agents ADD COLUMN share_content INTEGER NOT NULL DEFAULT 0')
        if 'sync_at' not in cols:c.execute('ALTER TABLE agents ADD COLUMN sync_at INTEGER')
        if 'runtime' not in cols:c.execute('ALTER TABLE agents ADD COLUMN runtime TEXT')
        if 'requester_ready' not in cols:c.execute('ALTER TABLE agents ADD COLUMN requester_ready INTEGER NOT NULL DEFAULT 0')
        if 'work' not in cols:c.execute('ALTER TABLE agents ADD COLUMN work TEXT')
        # SQLite cannot widen a CHECK in place. Rebuild only the old two-harness
        # schema, in this transaction, retaining every column and UNIQUE/FK rule.
        schema=c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='agents'").fetchone()[0]
        widened=re.sub(r"CHECK\s*\(\s*executor\s+IN\s*\(\s*'codex'\s*,\s*'pi'\s*\)\s*\)", '',schema,flags=re.I)
        if widened!=schema:
            widened=re.sub(r',\s*\)$', ')', widened)
            if not c.in_transaction:c.execute('BEGIN IMMEDIATE')
            indexes=[r[0] for r in c.execute("SELECT sql FROM sqlite_master WHERE tbl_name='agents' AND type='index' AND sql IS NOT NULL")]
            c.execute(re.sub(r'CREATE TABLE\s+\"?agents\"?', 'CREATE TABLE agents_harness_migration', widened, count=1,flags=re.I))
            names=','.join('"'+r[1]+'"' for r in c.execute('PRAGMA table_info(agents)'))
            c.execute(f'INSERT INTO agents_harness_migration ({names}) SELECT {names} FROM agents')
            c.execute('DROP TABLE agents');c.execute('ALTER TABLE agents_harness_migration RENAME TO agents')
            for sql in indexes:c.execute(sql)
        if not c.execute("SELECT 1 FROM users WHERE role='admin' LIMIT 1").fetchone() and not c.execute("SELECT 1 FROM invitations WHERE kind='setup' AND used_at IS NULL AND expires>?", (now(),)).fetchone():
            t=token()
            c.execute('INSERT INTO invitations VALUES(?,?,?,?,?,?,?,?,NULL)', (uid(),digest(t),'setup','初始化管理员',None,None,now(),now()+86400))
            p=DATA/'admin-setup-link.txt';p.write_text(BASE+'/#setup='+t+'\n');p.chmod(0o600)

def event(c, actor, action, subject): c.execute('INSERT INTO events(actor,action,subject,created_at) VALUES(?,?,?,?)',(actor,action,subject,now()))
def hub_action(action, aid):
    if not AID.fullmatch(aid): raise Problem('节点 AID 格式不正确')
    if not HELPER: raise Problem('Hub 接入服务未配置，请联系管理员',503)
    r=subprocess.run(['sudo','-n',HELPER,action,aid],capture_output=True,text=True,timeout=15)
    if r.returncode: raise Problem('Hub 操作失败，请联系管理员查看服务日志',503)
    return r.stdout

def hub_agent(aid):
    try:
        with urlopen(HUB_LOCAL+'/agents/'+aid,timeout=4) as r: return json.load(r).get('agent',{})
    except Exception: return {}

INSTALL_STAGES={'setup','pair','resume','configure','configure-pi','configure-harness','test-pi','test-harness','install','diagnose','report','anet-up','anet-status','anet-peers','anet-hub-register','anet-profile','anet-other'}
INSTALL_RESULTS={'started','ok','failed','pending','unverified'}
INSTALL_ERRORS={'none','network','timeout','certificate','acl','pair_expired','identity_mismatch','approval_required','missing_dependency','command_failed','permission','configuration','unknown','executor_pending','heartbeat_pending','native_launcher','dsh_profile','model_auth'}

def clean_install_report(raw):
    if not isinstance(raw,dict) or raw.get('schema')!=1 or not re.fullmatch('[a-f0-9]{24}',str(raw.get('run_id',''))):raise Problem('诊断格式无效')
    report={'schema':1,'run_id':raw['run_id']}
    report['os']=raw.get('os') if raw.get('os') in ['Windows','Darwin','Linux'] else 'Other'
    report['arch']=raw.get('arch') if raw.get('arch') in ['amd64','AMD64','x86_64','arm64','aarch64'] else 'other'
    for key in ('connector_version','python_version'):
        value=raw.get(key,'')
        report[key]=value if isinstance(value,str) and re.fullmatch(r'\d{1,3}\.\d{1,3}\.\d{1,3}',value) else ''
    if not isinstance(raw.get('events'),list) or not 1<=len(raw['events'])<=100:raise Problem('诊断事件数量无效')
    events=[]
    for item in raw['events']:
        if not isinstance(item,dict) or item.get('stage') not in INSTALL_STAGES or item.get('result') not in INSTALL_RESULTS:raise Problem('诊断阶段无效')
        event={'stage':item['stage'],'result':item['result'],'error':item.get('error') if item.get('error') in INSTALL_ERRORS else 'unknown'}
        for key,maximum in [('at',now()+300),('elapsed_ms',86400000),('exit_code',255)]:
            value=item.get(key)
            if type(value)==int and (-255 if key=='exit_code' else 0)<=value<=maximum:event[key]=value
        checks=item.get('checks')
        if isinstance(checks,dict):event['checks']={k:v for k,v in checks.items() if k in ['binding','hub','policy','executor','heartbeat','mcp'] and v in INSTALL_RESULTS}
        events.append(event)
    report['events']=events
    return report

def store_install_report(c,a,raw):
    report=clean_install_report(raw)
    # Derive the storage ID from authenticated record + random local run ID.
    rid=hashlib.sha256((a['id']+':'+report['run_id']).encode()).hexdigest()[:24]
    c.execute('INSERT INTO install_reports(id,agent_id,created_at,updated_at,report) VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET updated_at=excluded.updated_at,report=excluded.report',(rid,a['id'],now(),now(),json.dumps(report,ensure_ascii=False)))
    c.execute('DELETE FROM install_reports WHERE agent_id=? AND id NOT IN (SELECT id FROM install_reports WHERE agent_id=? ORDER BY updated_at DESC LIMIT 10)',(a['id'],a['id']))
    c.commit()
    return {'ok':True,'report_id':rid}

def agent_view(a, owner, viewer=None):
    keys=['kind','executor','id','name','description','visibility','aid','enabled','last_seen','policy_ok','executor_ready','created_at']
    v={k:a.get(k) for k in keys};v['owner_name']=owner;v['is_owner']=a.get('owner_id')==viewer
    if not a['enabled']: v['status']='disabled'
    elif not a['aid']: v['status']='pending'
    elif a.get('kind')=='device':
        v['status']='bound' if not a['last_seen'] else ('ready' if now()-a['last_seen']<=90 and a.get('node_connected') else 'offline')
    elif not a['last_seen'] or now()-a['last_seen']>90:v['status']='offline'
    elif not a['policy_ok']:v['status']='policy_pending'
    elif not a['executor_ready']:v['status']='executor_pending'
    else:v['status']='ready'
    # A node running taskd declares what long tasks it takes (taskd/README.md); stale while offline.
    caps=long_task_caps(a.get('capabilities')) if a.get('kind')=='agent' and v['status']=='ready' else None
    v['long_task']=bool(caps);v['capabilities']=caps
    v['runtime']=runtime_view(a.get('runtime'),v['is_owner']) if a.get('kind')=='agent' and v['status'] in ('ready','policy_pending','executor_pending') else None
    v['share_content']=bool(a.get('share_content'));v['sync_at']=a.get('sync_at') if v['is_owner'] else None
    v['requester_ready']=bool(a.get('requester_ready') and a.get('node_connected') and v['status']=='ready')
    if a.get('kind')=='agent':v['work']=work_view(a,v['status'],v['is_owner'])
    return v

def graph_view(c,u):
    """Everyone's connection graph for the home map, redacted for the viewer.

    The viewer's own records and other members' public, connected agents come through agent_view.
    Everything else (other members' private agents and all of their computers) is an anonymous
    {kind,status} node: no name, id, aid, description or runtime. Members carry a hashed key, never a user id.
    """
    users=[dict(r) for r in c.execute('SELECT id,name FROM users WHERE enabled=1 ORDER BY created_at LIMIT 60')]
    nodes={x['id']:[] for x in users};keys={x['id']:hashlib.sha256(('graph:'+x['id']).encode()).hexdigest()[:12] for x in users}
    refs={}  # aid -> (member key, node index, agent id, viewer owns it), for graph_links only
    for r in c.execute('SELECT a.*,u.name owner_name FROM agents a JOIN users u ON u.id=a.owner_id WHERE u.enabled=1 ORDER BY a.created_at').fetchall():
        a=dict(r)
        if a['owner_id'] not in nodes or len(nodes[a['owner_id']])>=30:continue
        if a['owner_id']==u['id']:
            v=agent_view(a,a['owner_name'],u['id']);v['redacted']=False
        elif not a['enabled'] or not a['aid']:continue
        elif a['kind']=='agent' and a['visibility']=='public':
            v=agent_view(a,a['owner_name'],u['id']);v['redacted']=False
        else:v={'kind':a['kind'],'status':agent_view(a,a['owner_name'],u['id'])['status'],'redacted':True}
        if a['aid']:refs[a['aid']]=(keys[a['owner_id']],len(nodes[a['owner_id']]),a['id'],a['owner_id']==u['id'])
        nodes[a['owner_id']].append(v)
    members=[{'key':keys[x['id']],'name':x['name'],'is_me':x['id']==u['id'],'nodes':nodes[x['id']]} for x in users]
    members.sort(key=lambda m:not m['is_me'])
    return {'members':members,'links':graph_links(c,u,members,refs),'now':now()}

LIVE_STATES={'submitted','working','input-required','auth-required'}
def graph_links(c,u,members,refs):
    """Who called whom lately, as map positions: [member key, node index], [member key] or [] for "the network".

    A link appears only where the viewer could already see that agent's interaction metadata (their own
    agents, or connected public ones). The other end is named only if the viewer can see that node;
    otherwise it is drawn from the hub, so the map never ties a private AID to a member.
    Refused calls are shown to the receiving agent's owner only.
    """
    keys={m['key']:m for m in members};me=next(m['key'] for m in members if m['is_me'])
    mine={r['aid'] for r in c.execute('SELECT aid FROM assistants WHERE user_id=? AND aid IS NOT NULL',(u['id'],))}
    def end(aid):
        x=refs.get(aid)
        if x and not keys[x[0]]['nodes'][x[1]]['redacted']:return [x[0],x[1]]
        return [me] if aid in mine else []
    by_id={v[2]:v for v in refs.values()}
    out={}
    def add(src,dst,at,live,refused=False,msgs=1):
        if src==dst:return
        k=json.dumps([src,dst,refused]);e=out.setdefault(k,{'from':src,'to':dst,'at':0,'live':False,'refused':refused,'count':0,'msgs':0})
        e['at']=max(e['at'],at);e['live']=e['live'] or live;e['count']+=1;e['msgs']+=msgs
    cutoff=now()-86400
    for r in c.execute('SELECT i.agent_id,i.role,i.peer,i.state,i.updated_at,(SELECT COUNT(*) FROM ix_messages x WHERE x.agent_id=i.agent_id AND x.ix=i.ix) msgs FROM interactions i WHERE i.updated_at>=? ORDER BY i.updated_at DESC LIMIT 400',(time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime(cutoff)),)):
        x=by_id.get(r['agent_id'])
        if not x or keys[x[0]]['nodes'][x[1]]['redacted']:continue
        try:at=min(now(),int(calendar.timegm(time.strptime(r['updated_at'][:19],'%Y-%m-%dT%H:%M:%S'))))  # node clocks drift
        except ValueError:continue
        here=[x[0],x[1]];there=end(r['peer'])
        add(*((there,here) if r['role']=='inbound' else (here,there)),at,r['state'] in LIVE_STATES and at>now()-1800,msgs=r['msgs'])
    for t,agent_id,caller,ok in list(PULSES):
        x=by_id.get(agent_id)
        if t<now()-300 or not x or keys[x[0]]['nodes'][x[1]]['redacted'] or (not ok and not x[3]):continue
        add(end(caller),[x[0],x[1]],t,False,not ok)
    return sorted(out.values(),key=lambda e:-e['at'])[:60]

def clean_runtime(raw):
    # Self-reported by the node: whitelist keys and bound every value; never trusted for access decisions.
    if not isinstance(raw,dict):return None
    out={}
    for k in ['harness','backend','agent','model','mode','untrusted','work_dir','sandbox','anet_version','connector_version','os','machine','update_state','taskd_version','taskd_update','workbuddy_auth']:
        if isinstance(raw.get(k),str) and raw[k]:out[k]=raw[k][:300 if k=='work_dir' else 80]
    if isinstance(raw.get('egress_allow'),list):out['egress_allow']=[str(h)[:100] for h in raw['egress_allow'][:20]]
    if isinstance(raw.get('read_paths'),list):out['read_paths']=[str(h)[:300] for h in raw['read_paths'][:5]]
    for k in ('taskd','sync','auto_update','gui'):
        if isinstance(raw.get(k),bool):out[k]=raw[k]
    return out or None

def runtime_view(raw,is_owner):
    try:r=json.loads(raw) if raw else None
    except (ValueError,TypeError):return None
    if not isinstance(r,dict):return None
    if not is_owner:
        # Other members see the folder name and mode, not the owner's machine paths or network allow-list.
        if r.get('work_dir'):r['work_dir']=re.split(r'[\\/]+',r['work_dir'].rstrip('\\/'))[-1]
        r.pop('egress_allow',None);r.pop('read_paths',None)
    return r

WORK_COUNTS=('running','queued','waiting','blocked')
WORK_STATES={'queued','running','planning','verifying','accepted','awaiting_approval','needs_user','blocked',
             'waiting_slot','waiting_desktop','waiting_workspace'}
WORK_EVENTS={'accepted','planning','running','verifying','awaiting_approval','needs_user','blocked','completed','stopped',
    'executor_started','executor_output','executor_finished','checks_started','checks','checks_advanced','phase_done',
    'plan','step','review','replan','environment_wait','interrupted','milestone_done'}
WORK_PURPOSES={'planning','execution','report_repair','review','native_goal'}
WORK_TIMINGS=WORK_PURPOSES|{'checks','resource_wait'}

def work_time(value):
    return value if type(value) in (int,float) and 0<value<=now()+30 else None

def work_count(value):return min(value,100000) if type(value) is int and value>=0 else 0

def clean_execution_facts(raw, observed_now=None):
    """Content-free schema. Never forward session IDs, model text, paths or arbitrary errors."""
    import math
    import time
    if not isinstance(raw, dict) or raw.get('schema') != 1:
        return None
    now = time.time() if observed_now is None else observed_now
    out = {'schema': 1}
    choices = {
        'route': ('native', 'planned', 'direct', 'quick'),
        'routing_reason': ('explicit_planned', 'native_capability_verified', 'disabled_by_owner',
                           'adapter_has_no_verified_native_goals', 'native_probe_failed', 'unavailable'),
        'goal_status': ('active', 'paused', 'blocked', 'complete', 'budget_limited', 'budgetLimited'),
        'turn_status': ('inProgress', 'completed', 'interrupted', 'failed'),
        'last_event': ('turn/started', 'turn/completed', 'thread/goal/updated', 'native/retry',
                       'native/stopping', 'native/stopped', 'native/reconciled', 'native/needsApproval',
                       'item/completed', 'item/commandExecution/outputDelta', 'error'),
    }
    for key, allowed in choices.items():
        if isinstance(raw.get(key), str) and raw[key] in allowed:
            out[key] = raw[key]
    def stamp(value, future=30):
        return value if type(value) in (int, float) and math.isfinite(value) and 0 < value <= now + future else None
    at = stamp(raw.get('updated_at'))
    if at: out['updated_at'] = at
    if type(raw.get('iteration')) is int and 0 <= raw['iteration'] <= 100000:
        out['iteration'] = raw['iteration']
    retry = raw.get('retry')
    if isinstance(retry, dict) and retry.get('reason') in ('model_at_capacity', 'upstream_retry'):
        out['retry'] = {'reason': retry['reason']}
        if type(retry.get('attempt')) is int and 0 <= retry['attempt'] <= 100000:
            out['retry']['attempt'] = retry['attempt']
        for key, future in (('at', 30), ('retry_at', 86400)):
            at = stamp(retry.get(key), future)
            if at: out['retry'][key] = at
    stop = raw.get('stop')
    if isinstance(stop, dict):
        out['stop'] = {k: stop[k] for k in ('requested', 'confirmed', 'resource_retained') if type(stop.get(k)) is bool}
    return out


def clean_work(raw):
    if not isinstance(raw,dict) or raw.get('schema')!=1 or raw.get('source')!='taskd' or raw.get('scope')!='anet_tasks':return None
    observed=work_time(raw.get('observed_at'))
    if not observed or not isinstance(raw.get('counts'),dict):return None
    counts={k:work_count(raw['counts'].get(k)) for k in WORK_COUNTS}
    out={'schema':1,'source':'taskd','scope':'anet_tasks','observed_at':observed,'counts':counts,
         'total':sum(counts.values()),'state':next((k for k in ('running','waiting','blocked','queued') if counts[k]),'idle'),
         'content_synced':raw.get('content_synced') is True,'truncated':raw.get('truncated') is True,'tasks':[]}
    for t in (raw.get('tasks') if isinstance(raw.get('tasks'),list) else [])[:50]:
        if not isinstance(t,dict) or not isinstance(t.get('state'),str) or t['state'] not in WORK_STATES or not isinstance(t.get('id'),str):continue
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}',t['id']):continue
        v={'id':t['id'],'mode':'quick' if t.get('mode')=='quick' else 'long','state':t['state'],
           'attention':t.get('attention') is True,'no_progress_steps':work_count(t.get('no_progress_steps'))}
        facts=clean_execution_facts(t.get('execution_facts'))
        if facts:v['execution_facts']=facts
        v['plan_revision']=work_count(t.get('plan_revision'))
        v['revision_pending']=t.get('revision_pending') is True
        if isinstance(t.get('last_purpose'),str) and t['last_purpose'] in WORK_PURPOSES:v['last_purpose']=t['last_purpose']
        if isinstance(t.get('timing'),dict):
            v['timing']={k:{'calls':work_count(x.get('calls')),'seconds':round(x['seconds'],3)}
                         for k,x in t['timing'].items() if k in WORK_TIMINGS and isinstance(x,dict)
                         and type(x.get('seconds')) in (int,float) and 0<=x['seconds']<=31536000}
        scheduling=t.get('scheduling')
        if isinstance(scheduling,dict) and scheduling.get('state') in ('executing','waiting_slot','waiting_desktop','waiting_workspace'):
            v['scheduling']={'state':scheduling['state'],'resource':'desktop' if scheduling.get('resource')=='desktop' else 'background',
                'blocked_by':[x for x in (scheduling.get('blocked_by') if isinstance(scheduling.get('blocked_by'),list) else [])[:50]
                              if isinstance(x,str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,110}',x)],
                'position':work_count(scheduling.get('position'))}
        for k in ('started_at','last_activity_at','last_progress_at'):
            at=work_time(t.get(k))
            if at:v[k]=at
        for k in ('last_event','progress_event'):
            if isinstance(t.get(k),str) and t[k] in WORK_EVENTS:v[k]=t[k]
        if out['content_synced']:
            if isinstance(t.get('blocker'),dict):
                v['blocker']={k:redact(t['blocker'][k])[:500] for k in ('code','action','detail') if isinstance(t['blocker'].get(k),str)}
            for k in ('goal','reason'):
                if isinstance(t.get(k),str):v[k]=redact(t[k])[:500]
            if isinstance(t.get('last_step'),dict):
                v['last_step']={k:redact(t['last_step'][k])[:1000] for k in ('summary','next','blocker','at') if isinstance(t['last_step'].get(k),str)}
        phase=t.get('phase')
        if isinstance(phase,dict):
            v['phase']={k:work_count(phase.get(k)) for k in ('index','total')}
            if out['content_synced'] and isinstance(phase.get('title'),str):v['phase']['title']=redact(phase['title'])[:160]
            m=phase.get('milestone')
            if isinstance(m,dict):
                v['phase']['milestone']={k:work_count(m.get(k)) for k in ('done','total')}
                if out['content_synced'] and isinstance(m.get('current'),str):v['phase']['milestone']['current']=redact(m['current'])[:160]
        v['events']=[dict({'event':e['event'],'at':e['at']},**({'purpose':e['purpose']} if isinstance(e.get('purpose'),str) and e['purpose'] in WORK_PURPOSES else {})) for e in (t.get('events') if isinstance(t.get('events'),list) else [])[-20:]
                     if isinstance(e,dict) and isinstance(e.get('event'),str) and e['event'] in WORK_EVENTS and work_time(e.get('at'))]
        out['tasks'].append(v)
    return out

def work_view(a,status,is_owner):
    try:work=clean_work(json.loads(a.get('work') or 'null'))
    except (ValueError,TypeError):work=None
    reason=('offline' if status in ('offline','disabled','pending') or not a.get('node_connected') else
            'unsupported' if not work else 'stale' if now()-work['observed_at']>90 else None)
    if reason:return {'state':'unknown','reason':reason,'observed_at':work['observed_at'] if work else None,'tasks':[]}
    work['tasks_visible']=bool(is_owner or a.get('share_content'))
    if not work['tasks_visible']:work['tasks']=[]
    return work

SECRET=re.compile(r'(-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)|\bsk-[A-Za-z0-9_\-]{16,}|\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=\-]{16,}|\b(?:ghp|gho|github_pat|xox[bap]|AKIA)[A-Za-z0-9_\-]{12,}|(?i:(?:api[_-]?key|secret|token|password|passwd)["\']?\s*[:=]\s*)["\']?[^\s"\',;]{6,})',re.S)
def redact(t):return SECRET.sub('[已隐藏]',t)
RETENTION_DAYS=30
IX_STATES={'submitted','working','input-required','completed','failed','canceled','rejected','auth-required'}

def iso(v,default=''):
    return v[:40] if isinstance(v,str) and re.fullmatch(r'\d{4}-\d\d-\d\dT[0-9:.]+Z',v[:40]) else default

def store_interactions(c,agent,items):
    """Node-pushed conversation records; every field is bounded and secrets are masked again here."""
    saved=0
    for it in items[:40] if isinstance(items,list) else []:
        if not isinstance(it,dict) or not re.fullmatch(r'ix_[a-f0-9]{16,64}',str(it.get('id',''))):continue
        peer=str(it.get('peer',''))
        role=it.get('role')
        created=iso(it.get('created_at'));updated=iso(it.get('updated_at'),created)
        if role not in ('inbound','outbound') or not AID.fullmatch(peer) or not created:continue
        state=str(it.get('state','working'))[:20];state=state if state in IX_STATES else 'working'
        c.execute('INSERT INTO interactions VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(agent_id,ix) DO UPDATE SET state=excluded.state,updated_at=excluded.updated_at,goal=excluded.goal',
            (agent['id'],it['id'],role,peer,redact(str(it.get('goal',''))[:8000]),state,created,updated))
        for m in (it.get('messages') or [])[:300]:
            if not isinstance(m,dict) or not re.fullmatch(r'[A-Za-z0-9\-]{8,64}',str(m.get('msg_id',''))) or m.get('from') not in ('me','them'):continue
            atts=[{'name':str(x.get('name',''))[:120],'size':x.get('size') if type(x.get('size')) is int else None,'mime':str(x.get('mime',''))[:80]} for x in (m.get('attachments') or [])[:20] if isinstance(x,dict)]
            c.execute('INSERT OR IGNORE INTO ix_messages VALUES(?,?,?,?,?,?,?,?)',(agent['id'],it['id'],m['msg_id'],m['from'],str(m.get('kind','text'))[:20],redact(str(m.get('body',''))[:8000]),json.dumps(atts,ensure_ascii=False) if atts else None,iso(m.get('created_at'),created)))
        saved+=1
    c.execute('UPDATE agents SET sync_at=? WHERE id=?',(now(),agent['id']))
    return saved

def interaction_access(c,u,agent_id):
    """(agent, content_visible). Owners see everything; members see only what the owner shared."""
    a=one(c,'SELECT a.*,u.enabled owner_enabled FROM agents a JOIN users u ON u.id=a.owner_id WHERE a.id=? AND a.kind=\'agent\'',(agent_id,))
    if not a:raise Problem('智能体不存在',404)
    if a['owner_id']==u['id']:return a,True
    if a['visibility']!='public' or not a['enabled'] or not a['owner_enabled'] or not a['aid']:raise Problem('智能体不存在或没有查看权限',404)
    return a,bool(a['share_content'])

def peer_names(c,u,peers):
    out={}
    for r in c.execute('SELECT a.aid,a.name,x.name owner_name FROM agents a JOIN users x ON x.id=a.owner_id WHERE a.aid IN (%s) AND (a.owner_id=? OR (a.visibility=\'public\' AND a.enabled=1))'%','.join('?'*len(peers)),(*peers,u['id'])) if peers else []:
        out[r['aid']]={'name':r['name'],'owner_name':r['owner_name']}
    return out

def long_task_caps(raw):
    try:c=json.loads(raw) if raw else None
    except (ValueError,TypeError):return None
    if not isinstance(c,dict) or c.get('protocol')!='taskd/1':return None
    limits=c.get('limits') if isinstance(c.get('limits'),dict) else {}
    platform=c.get('platform') if isinstance(c.get('platform'),dict) else {}
    checkers=c.get('checkers') if isinstance(c.get('checkers'),dict) else {}
    def limit(key,maximum):
        n=limits.get(key)
        return n if type(n) in (int,float) and 0<n<=maximum else None
    native=c.get('native') if isinstance(c.get('native'),dict) else {}
    native_view={k:native[k] for k in ('available','persistent_session','steer','pause','cancel_cleanup','interactive_attach') if type(native.get(k)) is bool}
    routing=c.get('routing') if isinstance(c.get('routing'),dict) else {}
    routing_view={k:routing[k] for k in ('planned_opt_in','fallback_is_reported') if type(routing.get(k)) is bool}
    if routing.get('submit_default')=='native_if_available':routing_view['submit_default']='native_if_available'
    return {'protocol':'taskd/1','native':native_view or None,'routing':routing_view or None,'checkers':sorted(str(k)[:60] for k in checkers)[:40],
            'concurrency':limit('concurrency',1000),
            'scheduling':{'scope':'phase','desktop_concurrency':1,'background_unlimited':c['scheduling'].get('background_unlimited') is True} if isinstance(c.get('scheduling'),dict) and c['scheduling'].get('scope')=='phase' else None,
            'gui':platform.get('gui') is True,'os':str(platform.get('os',''))[:20],
            'modes':[m for m in ('quick','long') if m in (c.get('modes') if isinstance(c.get('modes'),list) else ['long'])] or ['long'],
            'review':c.get('review') is True,'max_hours':limit('max_hours',8760),'max_steps':limit('max_steps',100000)}

def onboarding(a, t):
    recovery=f"这是原连接的恢复任务，必须复用原 AID {a['aid']} 和原数据目录；下面的新建身份步骤不适用。恢复链接只接受这个原身份，不创建新电脑记录。\n" if a.get('aid') else ''
    return f'''# 连接这台电脑：统一安装
{recovery}这条连接既可发任务，也可在用户选择后接收任务；只安装一次。当前用途：{'仅发任务，不配置执行器' if a.get('kind')=='device' else '发任务 + 接收任务，执行器 '+a.get('executor','codex')}。
1. 找到实际 ANet 数据目录，复用原身份、portal/connection.json 和 pair-pending.json。已有 ANet 不降级；缺少时读取 {BASE}/install/instructions.txt，按官方签名镜像安装，只从指定镜像取包，不访问 GitHub/npm 下载 ANet。
2. 将 {BASE}/connector.py、{BASE}/harness_bridge.py、{BASE}/pi_bridge.py 下载到同一私有目录，先阅读代码。使用本机实际 Python 命令（Windows 常为 python 或 py -3）。最新版支持 setup、diagnose、report。
3. 运行 `python3 connector.py setup --portal {BASE} --data-dir <原数据目录> --anet <实际anet可执行路径>`。接入凭证通过隐藏输入或进程 stdin 传递，不写入命令行、日志或 URL 查询参数：
`{t}`
setup 会复用已有绑定、按当前用途配置、安装登录心跳并检查结果。中断后重新运行同一个 setup，不新建电脑、不重置身份。安装日志自动回传阶段、版本、耗时、错误类别和检查结果；不上传路径、对话、项目文件或凭证，保留 30 天，可加 --no-report 关闭。
4. 仅当用户选择接收任务时，确认具体工作目录和本地执行权限。已有执行器沿用原配置；新配置可以把下面的 configure-pi / configure-harness 参数（如 --work-dir、--mode direct、--command 或 --credential-file）传给 setup。原生 direct 审批须由用户在交互终端完成，不代答。DeepSeek 默认保留自己的权限策略，不自动选择 danger-full-access；固定参数在 --profile headless 之后。Windows 自动获取校验过的原生 exe 桥接，不在用户电脑临时编译；已修好的原生启动器保留，不覆盖。
5. 按当前助手的 MCP 支持配置 anet mcp，明确使用同一个 ANet 数据目录；执行器也可以发任务，不单独再创建“电脑”。实际调用一个只读 ANet 工具确认 MCP；setup 尚不能自动确认所有助手的 MCP 配置。长任务 taskd 另需独立验收。
6. 回到原连接详情看“安装进度与诊断”。失败时先看阶段与错误类别；可运行 `python3 connector.py diagnose --data-dir <原目录>` 和 `python3 connector.py report --data-dir <原目录>` 补传诊断。普通网络失败保留进度重试；旧版凭证丢失/过期才从原连接获取恢复说明。报告真实完成与未完成项目，不用配置成功冒充远程任务成功。

以下是 setup 需要补齐执行器或权限时的修复参考，按缺失步骤处理，不重复从头安装：
'''+onboarding_steps(a,t)

def onboarding_steps(a, t):
    # The token is in a fragment; fetch it using a POST, never a URL query.
    if a.get('kind')=='device':return device_onboarding(a,t)
    if a.get('executor')=='pi':return pi_onboarding(a,t)
    if a.get('executor') in ['workbuddy','deepseek','custom']:return harness_onboarding(a,t)
    return f'''# 接入团队 Agent：{a['name']}

这是用户主动交给本地 Codex 的接入任务。平台：{BASE}；Hub：{HUB}。
只在这台用户电脑上配置。先解释将允许哪些工作，再操作；不读取或打印任何身份私钥、恢复口令和其他项目凭据。

1. 检测操作系统、`anet version`、`anet status --json`、`anet doctor` 和 Codex。已有 ANet 则复用身份，先备份配置；若节点属于另一 Hub 或已有其他用途，先询问用户。没有 ANet 时先读取 {BASE}/install/instructions.txt，使用我们服务器上的官方签名镜像安装，要求 >=0.3.9。必须按说明指定唯一下载源（Windows -Base，macOS/Linux --base），不访问 GitHub/npm 下载 ANet；验证官方签名和校验值。已有 operator 版本不降级、不重复安装。不要修改支付限额。
2. 本地运行 `anet up`。在用户同意的工作目录配置 Codex 执行器；不要将整个主目录作为工作目录，也不要擅自选择不受限执行。
3. 从 `{BASE}/connector.py` 下载平台辅助程序到用户私有配置目录，先阅读代码，再运行：
   `python3 connector.py pair --portal {BASE} --data-dir <实际ANet数据目录>`
   程序通过终端隐藏输入读取本次接入凭证。请由用户输入下列凭证，或通过进程标准输入安全传递，勿将其放进 shell 参数或日志：
   `{t}`
   程序通过 ANet 签名的临时 profile 验证节点归属，并在完成后恢复 profile。它不会读取身份私钥。
4. Windows 使用中心 HTTPS 权限接口（不支持 Unix socket / 本机 TCP 权限接口）；macOS/Linux 默认使用本地 Unix socket。按程序提示运行 `python3 connector.py configure --data-dir <实际ANet数据目录>`，配置只供节点使用的本地权限接口。若有既有 allow/trust 或对外能力，程序会停止，先与用户确认如何迁移，不能绕过检查。
5. 执行 `anet agents wire codex`，保留其他 MCP 配置。根据 `anet guide` 配置 autoreply 的 Codex 后端、明确工作目录、`untrusted off`。对 `admitted` 的执行模式由用户选择，优先支持的 sandbox；需要 direct 时由用户在自己的终端确认，不能绕过 ANet 的终端批准。
6. 运行辅助程序 `serve` 并按 `install` 子命令安装开机启动；macOS 支持 LaunchAgent，Linux 支持 systemd --user，Windows 使用当前用户登录启动项（不是系统服务，登录后运行）。重启 ANet 使配置生效。不能宣称当前聊天窗口已变成常驻会话。
已完成 pair 的节点如果中途失败：重新下载最新版 connector.py，使用同一 --data-dir 直接 configure，再 install；保留 portal/connection.json，不再次 pair、不重新索取接入凭证。
7. 检查网页是否显示权限和执行器已配置；用用户允许的最小任务验证结果与回执。报告每项真实状态，未测试的项目明确说明。恢复保护由用户在终端执行 `anet identity protect`。

每个用户的节点身份关联其平台账号。私有仅允许同一所有者的已连接节点；团队公开允许正常成员的已连接节点。权限接口失联默认拒绝新委派。只有 ANet 文本委派受此入口控制，configure 会拒绝遗留公开 capability 与 allow/trust 旁路。
'''

def pi_onboarding(a,t):
    return f'''# 接入团队 Pi Agent：{a['name']}

这是用户主动交给本地助手的 Pi Coding Agent 接入任务。平台：{BASE}；Hub：{HUB}。
执行任务的是 Pi，不是当前聊天会话。只在用户选定的设备上操作；不读取或打印身份私钥、恢复口令、Pi 登录凭证或其他项目密钥。

1. 检查操作系统、Python 3、`pi --version`、`anet version`、`anet status --json` 和 `anet doctor`。ANet 要求 >=0.3.9；缺少时读取 {BASE}/install/instructions.txt，按唯一镜像源和官方签名校验步骤安装，不通过 GitHub/npm 下载 ANet。已有 ANet 复用身份，不降级 operator 版本。Pi 必须已安装并在该设备上完成模型登录或配置。没有 Pi 时先按 https://github.com/earendil-works/pi 的官方说明安装，不索取或复制其他电脑的模型密钥。
2. 保留现有身份。已有同平台调用设备时，从原连接开启接收任务，复用原身份和数据目录。只有用户明确需要并行的多个执行器时才创建独立身份，不覆盖其他用途的绑定。运行命令始终显式使用该数据目录。不要修改支付限额、添加 allow/trust 或开放入站策略。
3. 从 `{BASE}/connector.py` 和 `{BASE}/pi_bridge.py` 下载两个文件到同一私有目录，先阅读代码。运行：
   `python3 connector.py pair --portal {BASE} --data-dir <此Agent的ANet数据目录>`
   通过隐藏终端输入或进程标准输入传递本次凭证，不放进 shell 参数、查询参数或日志：
   `{t}`
   pair 使用签名 profile 验证控制权，完成后恢复 profile。保留 portal/connection.json；中断后复用，不重复 pair。
4. 运行 `python3 connector.py configure --data-dir <同一目录>` 配置平台权限接口。原有 allow/trust、公开 capability 或其他执行器冲突时停止并说明，不自动覆盖。
5. 先让用户明确工作目录和执行模式。首版 Pi 适配只支持 direct：以当前本地用户权限读写文件和执行命令，工作目录不是文件隔离；未验证 Pi 沙箱。不要默认选择整个主目录，也不能冒充 sandbox。用户同意 direct 后，在用户自己的交互终端运行：
   `python3 connector.py configure-pi --data-dir <同一目录> --work-dir <用户选择的目录> --mode direct --pi-command <实际pi路径>`
   可加 `--model <provider/model>`。程序通过 ANet 的 Codex CLI 协议桥接到 Pi，保留 ANet 的入站检查、执行限额、超时和回执；真实执行器仍是 Pi。ANet 原生终端确认必须由用户完成，不代答或绕过。它不会启用不受信任节点执行。
6. 运行 `python3 connector.py test-pi --data-dir <同一目录>`，进行最小本地模型调用并核对结果；失败时报告真实错误，不能只凭配置宣称成功。随后运行 `python3 connector.py install --data-dir <同一目录>` 保持权限接口和心跳。Pi 接入不需要把 Pi 注册成 Codex MCP 客户端。
7. 回到“我的智能体”检查 Pi 类型、权限和执行器状态。只有用户指定目标与任务后才发起委派，核对实际文件、返回结果与回执。状态就绪不代表已经完成过远程任务。恢复保护由用户在终端运行 `anet identity protect`。
'''

def harness_onboarding(a,t):
    executor=a['executor'];name=next(h['name'] for h in HARNESSES if h['id']==executor)
    options={
        'deepseek':'--command <实际dsh可执行文件路径>。先运行 dsh --profile headless --help，确认支持 --json、stdin 和 --approval-policy。模型凭证按官方说明在本机配置，不上传平台。',
        'workbuddy':'推荐先在网页“我的连接 → 详情 → WorkBuddy 授权”完成官方授权，不需要额外本机参数，平台负责续期，应用密钥与 refresh_token 只留在服务器。需要 user.localassistant.readable 和 user.localassistant.invokable 权限；管理员未配置官方应用时明确报告等待配置。仅兼容旧接入时可用 --credential-file <本机授权JSON路径>，此模式不自动续期。使用当前 PC 本地助理，其工作区由 WorkBuddy 管理，不提取其他应用的登录凭证。',
        'custom':'--command <本机可执行文件路径>，可重复 --arg <固定参数>。程序须从 stdin 读取完整任务，仅向 stdout 输出最终文本，以退出码 0 表示完成；诊断写 stderr。不得使用 shell 拼接或把任务放进参数。',
    }[executor]
    return f'''# 接入 {name}：{a['name']}

这是用户主动交给本地助手的 harness 接入任务。平台：{BASE}；Hub：{HUB}。
保留已有调用设备、Agent 和身份；同一电脑开启接收任务时复用原连接和原数据目录，只有用户明确需要并行多个执行器才创建独立身份。不要更改支付限额或扩大入站权限。

1. 检查 Python >=3.10、ANet >=0.3.9 和本机 {name}。缺少 ANet 时读取 {BASE}/install/instructions.txt，使用唯一镜像源并校验官方签名，不通过 GitHub/npm 下载 ANet。已有安装和身份复用，不降级 operator 版本。官方资料：DeepSeek https://github.com/deepseek-ai/deepseek-harness/tree/master/packages/bundle/headless；WorkBuddy https://open.workbuddy.cn/docs/openapi 。安装和模型登录均由设备主人完成。
2. 将 {BASE}/connector.py、{BASE}/harness_bridge.py、{BASE}/pi_bridge.py 下载到同一私有目录，先阅读代码。运行 `python3 connector.py pair --portal {BASE} --data-dir <独立ANet目录>`；凭证通过隐藏终端输入或 stdin 传递，不能写进命令行、URL 查询参数或日志：
   `{t}`
   pair 验证签名身份后恢复原 profile；保留 portal/connection.json，中断后不要重复 pair。
3. 运行 `python3 connector.py configure --data-dir <同一目录>`，仅配置平台准入。已有 allow/trust、公开 capability 或其他执行器时先处理冲突，不覆盖。
4. 由设备主人选择具体工作目录，并在自己的交互终端完成 ANet 原生 direct 授权：
   `python3 connector.py configure-harness --data-dir <同一目录> --harness {executor} --work-dir <用户选择的目录> --mode direct <下述本机参数>`
   本机参数：{options}
   direct 是本机用户权限，所选目录不是文件隔离。DeepSeek 保留自身权限策略并拒绝需要人工批准的工具；WorkBuddy 保留本地助理的原生审批。不要绕过任何确认，不宣称当前聊天已变为常驻执行器。
5. 运行 `python3 connector.py test-harness --data-dir <同一目录>`，通过 ANet 实际桥接做最小模型自检。成功后 `python3 connector.py install --data-dir <同一目录>` 保持准入和心跳。模型密钥只留在执行设备；WorkBuddy 网页授权的长期凭证由服务器私有保存，本机只按需读取短期 access_token。
6. 检查网页上的 harness 类型、权限和自检状态。真实远程任务必须另行核对结果与回执；就绪不代表任务完成。WorkBuddy 等待超时或网络中断后，原任务可能仍在助理中运行，先在 WorkBuddy 核实再决定重试。
'''

def device_onboarding(a,t):
    return f"""# 连接发起任务的电脑：{a['name']}

这是用户主动交给本地助手的调用设备接入任务。平台：{BASE}；Hub：{HUB}。
用途仅为从本机向有权限的 Agent 委派任务。绑定调用身份、检查 MCP、同步连接状态即可。不要安装或启用本机任务执行器、不要选择执行工作目录、不要开启 autoreply、不要扩大入站权限或添加 allow/trust。若本机已有其他用途的执行配置，保留并说明，不擅自更改。

1. 检查 `anet version`、`anet status --json` 和既有 MCP 配置。已有安装和身份则复用；若连接到其他 Hub，先确认迁移或使用独立身份。没有 ANet 时先读取 {BASE}/install/instructions.txt，按我们服务器上的官方签名镜像安装说明操作，要求 >=0.3.9。必须按说明指定唯一下载源（Windows -Base，macOS/Linux --base），不访问 GitHub/npm 下载 ANet；验证官方签名和校验值。已有 operator 版本不降级、不重复安装。不要重置身份、恢复口令或支付限额。
2. 下载 `{BASE}/connector.py` 到本机私有配置目录，先阅读代码；使用本机实际 Python 3 命令（macOS/Linux 常见 python3，Windows 常见 python 或 py -3）。运行：
   `python3 connector.py pair --portal {BASE} --data-dir <实际ANet数据目录>`
   程序从隐藏终端输入读取接入凭证。由用户输入或通过进程标准输入安全传递，不放在 shell 参数或日志中。本次凭证：
   `{t}`
   程序会验证节点控制权并绑定到当前账号，不读取身份私钥。完成后保留 portal/connection.json；中断后复用它，不重复 pair。
3. 检查本地助手能否使用 ANet 通信工具。使用 Codex 时，尚未接入可用 `anet agents wire codex` 辅助配置；保留其他 MCP 配置和审批设置，必要时重新打开会话。使用其他助手时，按该软件支持的 MCP 或命令行方式配置既有 ANet 工具；如果不支持，明确说明限制，不宣称已接入。不要把执行器的支持范围当作任意聊天客户端的工具兼容性。
4. 此设备是调用端，跳过执行器和权限后端的 configure。运行：
   `python3 connector.py install --data-dir <实际ANet数据目录> --anet <实际anet可执行路径>`
   install 为当前用户安装连接状态同步（macOS LaunchAgent、Linux systemd --user、Windows 登录启动项）。它只保持节点和心跳，不启用接收任务。若启动项无法安装，在当前会话用同样参数运行 serve 并说明限制。
5. 查看“我的设备”：身份绑定成功后显示“已绑定”；状态同步正常后显示“在线，可委派”。设备不需要配置 policy/admitted/auto_reply，也不应等待执行器就绪。私有 Agent 要求调用设备与它归属同一账号；团队公开 Agent 允许正常成员的已绑定设备或节点调用。
6. 恢复保护由用户在终端执行 `anet identity protect`。报告实际 AID、Hub、MCP 检查结果和连接状态；不以配置完成冒充远程任务成功。只有用户指定目标与任务后才发送委派，并检查实际结果与回执。
"""

class _Portal:
    # assistant.py reads portal helpers through this view, so it sees current module state.
    def __getattr__(self,name):return globals()[name]
ASSISTANT=None
def assistant():
    global ASSISTANT
    if ASSISTANT is None:
        spec=importlib.util.spec_from_file_location('portal_assistant',ROOT/'assistant.py');module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        ASSISTANT=module.Assistant(_Portal())
    return ASSISTANT

WORKBUDDY=None
def workbuddy():
    global WORKBUDDY
    with LOCK:
        if WORKBUDDY is None:
            spec=importlib.util.spec_from_file_location('portal_workbuddy',ROOT/'workbuddy_auth.py');module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
            WORKBUDDY=module.WorkBuddy(_Portal())
    return WORKBUDDY

class Handler(BaseHTTPRequestHandler):
    server_version='AgentPortal/1'
    def log_message(self, fmt, *args): pass  # no credentials/tokens in access logs
    def respond(self, obj, code=200, ctype='application/json; charset=utf-8', cookie=None):
        b=(json.dumps(obj,ensure_ascii=False) if ctype.startswith('application/json') else obj).encode() if not isinstance(obj,bytes) else obj
        self.send_response(code);self.send_header('Content-Type',ctype);self.send_header('Content-Length',str(len(b)))
        self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff');self.send_header('Referrer-Policy','no-referrer')
        policy="default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        if urlsplit(self.path).path=='/architecture.html' and code==200:
            # The offline document may run only its exact static script and style blocks.
            for tag in ('script','style'):
                hashes=["'sha256-"+base64.b64encode(hashlib.sha256(block).digest()).decode()+"'" for block in re.findall(rb'<'+tag.encode()+rb'>(.*?)</'+tag.encode()+rb'>',b,re.S)]
                policy=policy.replace(tag+"-src 'self'",tag+"-src 'self' "+' '.join(hashes))
        self.send_header('X-Frame-Options','DENY');self.send_header('Content-Security-Policy',policy)
        if cookie:self.send_header('Set-Cookie',cookie)
        self.end_headers();
        if self.command != 'HEAD': self.wfile.write(b)
    def body(self):
        n=int(self.headers.get('Content-Length','0'))
        if n>(1048576 if self.path=='/api/node/sync' else 32768): raise Problem('请求内容过大',413)
        try: d=json.loads(self.rfile.read(n) or b'{}')
        except Exception:raise Problem('请求格式错误')
        if not isinstance(d,dict):raise Problem('请求格式错误')
        return d
    def rate(self, category, limit):
        key=(self.headers.get('X-Real-IP',self.client_address[0]),category)
        with LOCK:
            q=RATES[key]
            while q and q[0]<now()-600:q.popleft()
            if len(q)>=limit:raise Problem('操作过于频繁，请稍后重试',429)
            q.append(now())
    def user(self,c, mutation=False):
        cookie=SimpleCookie()
        try:cookie.load(self.headers.get('Cookie',''))
        except Exception:raise Problem('请重新登录',401)
        s=cookie.get('agent_portal_session')
        u=one(c,'SELECT u.*,s.csrf FROM sessions s JOIN users u ON s.user_id=u.id WHERE s.hash=? AND s.expires>? AND u.enabled=1',(digest(s.value) if s else '',now()))
        if not u:raise Problem('请先登录',401)
        if mutation and not hmac.compare_digest(self.headers.get('X-CSRF-Token',''),u['csrf']):raise Problem('页面已失效，请刷新后重试',403)
        return u
    def node(self,c):
        auth=self.headers.get('Authorization','')
        a=one(c,'SELECT a.* FROM agents a JOIN users u ON a.owner_id=u.id WHERE a.node_hash=? AND a.enabled=1 AND u.enabled=1',(digest(auth[7:]) if auth.startswith('Bearer ') else '',))
        if not a:raise Problem('节点凭证无效或已撤销',401)
        return a
    def own(self,c,u,aid):
        a=one(c,'SELECT * FROM agents WHERE id=? AND owner_id=?',(aid,u['id']))
        if not a:raise Problem('Agent 不存在或没有管理权限',404)
        return a
    def cookie(self,t,age=604800):return f"agent_portal_session={t}; Path=/; HttpOnly; SameSite=Strict; Max-Age={age}"+('; Secure' if BASE.startswith('https:') else '')
    def do_HEAD(self):self.dispatch('GET')
    def do_GET(self):self.dispatch('GET')
    def do_POST(self):self.dispatch('POST')
    def do_PATCH(self):self.dispatch('PATCH')
    def do_DELETE(self):self.dispatch('DELETE')
    def workbuddy_route(self,path,method,d):
        broker=workbuddy()
        if path=='/api/workbuddy/callback' and method=='GET' and self.command!='HEAD':
            broker.complete(parse_qs(urlsplit(self.path).query,keep_blank_values=True))
            return self.respond('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>WorkBuddy 授权完成</title><h1>WorkBuddy 授权已完成</h1><p>可以关闭本页，回到原来的连接详情刷新状态，再完成本机自检。</p><p><a href="/#agents">返回我的连接</a></p></html>',ctype='text/html; charset=utf-8')
        if path=='/api/node/workbuddy/token' and method=='POST':
            with LOCK,db() as c:a=self.node(c)
            self.rate('workbuddy-token:'+a['id'],1200)
            rejected=d.get('rejected_token_sha256')
            if rejected is not None and (not isinstance(rejected,str) or not re.fullmatch('[a-f0-9]{64}',rejected)):raise Problem('凭证摘要格式错误')
            return self.respond(broker.access(a,rejected))
        m=re.fullmatch(r'/api/connections/([a-f0-9]{24})/workbuddy(?:/(authorize|revoke))?',path)
        if m:
            with LOCK,db() as c:
                u=self.user(c,method!='GET');a=self.own(c,u,m[1])
            if method=='GET' and not m[2]:return self.respond(broker.status(a))
            if method=='POST' and m[2]=='authorize':
                self.rate('workbuddy-authorize:'+a['id'],20)
                return self.respond(broker.begin(a))
            if method=='POST' and m[2]=='revoke':
                broker.current(a);broker.revoke(a)
                with LOCK,db() as c:c.execute('UPDATE agents SET executor_ready=0 WHERE id=?',(a['id'],));event(c,u['id'],'workbuddy.revoked',a['id'])
                return self.respond({'ok':True})
        raise Problem('接口不存在',404)
    def dispatch(self,method):
        try:
            path=urlsplit(self.path).path
            if method=='GET' and path in ['/','/architecture.html','/app.js','/style.css','/connector.py','/pi_bridge.py','/harness_bridge.py','/favicon.svg','/windows-repair.md']:
                f=ROOT/(path[1:] if path in ['/connector.py','/pi_bridge.py','/harness_bridge.py','/windows-repair.md'] else 'static/'+{'/':'index.html'}.get(path,path[1:]))
                ct={'.html':'text/html; charset=utf-8','.js':'text/javascript; charset=utf-8','.css':'text/css; charset=utf-8','.svg':'image/svg+xml','.py':'text/plain; charset=utf-8','.md':'text/plain; charset=utf-8'}[f.suffix]
                return self.respond(f.read_bytes(),ctype=ct)
            if method=='GET' and path in ['/harness-launcher-windows-amd64.exe','/harness-launcher-windows-arm64.exe']:
                return self.respond((ROOT/'native-launcher'/path[1:]).read_bytes(),ctype='application/octet-stream')
            if path=='/healthz':return self.respond({'ok':True,'service':'agent-portal'})
            if path=='/api/config' and method=='GET':return self.respond({'registration_open':True,'harnesses':HARNESSES})
            if method not in ['GET']:
                origin=self.headers.get('Origin')
                if origin and origin!=BASE:raise Problem('不接受跨站请求',403)
                d=self.body()
            else:d={}
            if path.startswith('/api/workbuddy/') or path=='/api/node/workbuddy/token' or re.fullmatch(r'/api/connections/[a-f0-9]{24}/workbuddy(?:/(authorize|revoke))?',path):
                return self.workbuddy_route(path,method,d)
            with LOCK,db() as c:
                if path=='/api/auth/login' and method=='POST':
                    self.rate('auth',30)
                    u=one(c,'SELECT * FROM users WHERE username=? AND enabled=1',(str(d.get('username',''))[:80],))
                    if not u or not password_ok(str(d.get('password',''))[:201],u['password']):raise Problem('用户名或密码错误',401)
                    t=token();csrf=token();c.execute('DELETE FROM sessions WHERE expires<?',(now(),))
                    c.execute('INSERT INTO sessions VALUES(?,?,?,?)',(digest(t),u['id'],csrf,now()+604800))
                    event(c,u['id'],'login',u['id']);return self.respond({'ok':True},cookie=self.cookie(t))
                if path=='/api/invitations/check' and method=='POST':
                    self.rate('invite',60)
                    inv=one(c,'SELECT kind,label FROM invitations WHERE hash=? AND used_at IS NULL AND expires>?',(digest(str(d.get('token',''))),now()))
                    if not inv:raise Problem('链接已过期或已使用',404)
                    return self.respond(inv)
                if path=='/api/auth/register' and method=='POST':
                    self.rate('auth',30)
                    invitation=str(d.get('token',''));inv=None
                    if invitation:
                        inv=one(c,'SELECT * FROM invitations WHERE hash=? AND used_at IS NULL AND expires>?',(digest(invitation),now()))
                        if not inv:raise Problem('链接已过期或已使用',404)
                    else:self.rate('register',10)
                    if 'password_confirm' in d and d['password_confirm']!=d.get('password'):raise Problem('两次输入的密码不一致')
                    pw=password_hash(d.get('password',''))
                    if inv and inv['kind']=='reset':
                        c.execute('UPDATE users SET password=? WHERE id=?',(pw,inv['target']));c.execute('DELETE FROM sessions WHERE user_id=?',(inv['target'],))
                    else:
                        username=str(d.get('username','')).strip();name=str(d.get('name','')).strip()
                        if not re.fullmatch(r'[a-zA-Z0-9_\-.]{3,40}',username) or not 1<=len(name)<=40:raise Problem('用户名需为 3–40 位字母、数字或 _-.，显示名称不能为空')
                        if inv and inv['kind']=='setup' and c.execute("SELECT 1 FROM users WHERE role='admin'").fetchone():raise Problem('管理员已初始化',409)
                        user_id=uid()
                        c.execute('INSERT INTO users VALUES(?,?,?,?,?,1,?)',(user_id,username,name,pw,'admin' if inv and inv['kind']=='setup' else 'member',now()))
                        event(c,user_id,'register.invited' if inv else 'register.public',user_id)
                        if not inv:
                            session=token();c.execute('INSERT INTO sessions VALUES(?,?,?,?)',(digest(session),user_id,token(),now()+604800))
                            return self.respond({'ok':True,'role':'member','signed_in':True},201,cookie=self.cookie(session))
                    c.execute('UPDATE invitations SET used_at=? WHERE id=?',(now(),inv['id']))
                    return self.respond({'ok':True})
                if path.startswith('/api/pair/'):
                    self.rate('pair',120)
                    t=str(d.get('token',''));a=one(c,'SELECT a.* FROM agents a JOIN users u ON u.id=a.owner_id WHERE a.pair_hash=? AND a.pair_expires>? AND a.enabled=1 AND u.enabled=1',(digest(t),now()))
                    if not a:raise Problem('接入链接已过期、被替换或已使用',404)
                    if path=='/api/pair/diagnostics':
                        self.rate('install:'+a['id'],300);return self.respond(store_install_report(c,a,d.get('report')))
                    if path=='/api/pair/info':return self.respond({'agent_id':a['id'],'expected_aid':a['aid'] or a['candidate_aid'],'pair_protocol':2,'recovery':bool(a['aid']),'name':a['name'],'kind':a['kind'],'executor':a['executor'],'hub':HUB,'instructions':onboarding(a,t),'expires':a['pair_expires']})
                    aid=str(d.get('aid',''))
                    if not AID.fullmatch(aid):raise Problem('节点 AID 格式不正确')
                    if a['aid'] and a['aid']!=aid:raise Problem('恢复连接必须使用原节点身份和数据目录，不能替换 AID',409)
                    other=one(c,'SELECT id FROM agents WHERE aid=? OR (candidate_aid=? AND pair_expires>?)',(aid,aid,now()))
                    if other and other['id']!=a['id']:raise Problem('该节点已绑定或正在接入其他 Agent',409)
                    if path=='/api/pair/prepare':
                        if a['candidate_aid'] and a['candidate_aid']!=aid:raise Problem('链接已用于另一节点，请重新生成',409)
                        prior=bool(hub_agent(aid));hub_action('admit',aid)
                        challenge=a['challenge'] or 'portal-proof-'+token()
                        c.execute('UPDATE agents SET candidate_aid=?,challenge=?,provisioned=? WHERE id=?',(aid,challenge,0 if a['aid'] else (a['provisioned'] or (0 if prior else 1)),a['id']))
                        c.commit()
                        return self.respond({'challenge':challenge,'hub':HUB})
                    if path=='/api/pair/complete':
                        if a['candidate_aid']!=aid or not a['challenge']:raise Problem('请先启动接入验证',409)
                        av=hub_agent(aid)
                        if av.get('summary')!=a['challenge']:raise Problem('尚未收到节点签名的验证信息，请稍后重试',409)
                        # New clients durably save this secret BEFORE consuming the pair token.
                        # A lost response can then be recovered through authenticated node/info.
                        nt=d.get('node_token') if 'node_token' in d else token()
                        if not isinstance(nt,str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}',nt):raise Problem('节点凭证格式不正确')
                        if c.execute('SELECT 1 FROM agents WHERE node_hash=? AND id!=?',(digest(nt),a['id'])).fetchone():raise Problem('节点凭证冲突，请重新生成',409)
                        c.execute('UPDATE agents SET aid=?,node_hash=?,pair_hash=NULL,pair_expires=NULL,challenge=NULL,candidate_aid=NULL,provisioned=0,updated_at=? WHERE id=?',(aid,digest(nt),now(),a['id']))
                        event(c,a['owner_id'],'agent.recovered' if a['aid'] else 'agent.paired',a['id'])
                        c.commit()  # Commit before writing the HTTP response, including on disconnect.
                        return self.respond({'agent_id':a['id'],'node_token':nt,'portal':BASE,'name':a['name'],'kind':a['kind'],'executor':a['executor']})
                    raise Problem('接口不存在',404)
                if path.startswith('/api/node/'):
                    a=self.node(c)
                    if path=='/api/node/diagnostics':
                        self.rate('install:'+a['id'],300);return self.respond(store_install_report(c,a,d.get('report')))
                    if path=='/api/node/info':return self.respond({k:a[k] for k in ['id','aid','kind','name','executor','last_seen','node_connected','policy_ok','executor_ready']}|{'agent_id':a['id']})
                    if path=='/api/node/directory' and method=='POST':
                        rows=c.execute("SELECT a.*,u.name owner_name FROM agents a JOIN users u ON u.id=a.owner_id WHERE a.kind='agent' AND a.aid IS NOT NULL AND a.enabled=1 AND u.enabled=1 AND (a.owner_id=? OR a.visibility='public') ORDER BY a.name,a.id",(a['owner_id'],)).fetchall()
                        result=[]
                        for row in rows:
                            row=dict(row);v=agent_view(row,row['owner_name'],a['owner_id'])
                            result.append({k:v.get(k) for k in ('aid','name','description','status','executor','long_task','capabilities','work','last_seen')})
                        return self.respond({'source':'agent-space','observed_at':now(),'agents':result})
                    if path=='/api/node/heartbeat' and method=='POST':
                        # Older connectors omit executor; that remains valid for existing Codex records.
                        matching=d.get('executor','codex')==a['executor']
                        if a['executor']=='workbuddy' and isinstance(d.get('runtime'),dict) and d['runtime'].get('workbuddy_auth')=='portal':
                            auth=workbuddy().status(a)
                            matching=matching and auth['configured'] and auth['authorized']
                        caps=d.get('capabilities') if a['kind']=='agent' and isinstance(d.get('capabilities'),dict) else None
                        caps=json.dumps(caps,ensure_ascii=False) if caps and long_task_caps(json.dumps(caps)) else None
                        if caps and len(caps)>8192:caps=None
                        work=clean_work(d.get('work')) if a['kind']=='agent' and matching else None
                        c.execute('UPDATE agents SET last_seen=?,policy_ok=?,executor_ready=?,node_connected=?,capabilities=?,requester_ready=?,runtime=?,work=? WHERE id=?',(now(),bool(d.get('policy_ok')) if a['kind']=='agent' else False,bool(d.get('executor_ready') and matching) if a['kind']=='agent' else False,bool(d.get('connected')),caps,d.get('requester_ready') is True,json.dumps(clean_runtime(d.get('runtime')),ensure_ascii=False) if a['kind']=='agent' and clean_runtime(d.get('runtime')) else None,json.dumps(work,ensure_ascii=False) if work else None,a['id']))
                        return self.respond({'ok':True})
                    if path=='/api/node/sync' and method=='POST':
                        if a['kind']!='agent':raise Problem('调用设备不同步交互内容',403)
                        self.rate('sync:'+a['id'],600)
                        return self.respond({'ok':True,'saved':store_interactions(c,a,d.get('interactions'))})
                    if path in ['/api/node/admit','/api/node/inbound'] and method=='POST':
                        # Native ANet internal services put the authenticated peer in a header.
                        # A bearer token identifies the receiving node, never the caller.
                        caller=self.headers.get('X-ANet-Caller','') if path.endswith('/inbound') else str(d.get('caller',''))
                        peer=one(c,'SELECT a.owner_id FROM agents a JOIN users u ON a.owner_id=u.id WHERE a.aid=? AND a.enabled=1 AND u.enabled=1 UNION ALL SELECT s.user_id FROM assistants s JOIN users u ON u.id=s.user_id WHERE s.aid=? AND u.enabled=1',(caller,caller))
                        ok=bool(a['kind']=='agent' and peer and (a['visibility']=='public' or a['owner_id']==peer['owner_id']))
                        if a['kind']=='agent':PULSES.append((now(),a['id'],caller[:120],ok))
                        return self.respond({'admit':ok})
                    raise Problem('接口不存在',404)
                u=self.user(c,method!='GET')
                if path=='/api/me':return self.respond({k:u[k] for k in ['id','username','name','role','csrf']})
                if path=='/api/auth/logout' and method=='POST':
                    c.execute('DELETE FROM sessions WHERE user_id=? AND csrf=?',(u['id'],u['csrf']));return self.respond({'ok':True},cookie=self.cookie('',0))
                if path=='/api/auth/password' and method=='POST':
                    if not password_ok(str(d.get('current','')),u['password']):raise Problem('当前密码错误',403)
                    c.execute('UPDATE users SET password=? WHERE id=?',(password_hash(d.get('password','')),u['id']));c.execute('DELETE FROM sessions WHERE user_id=?',(u['id'],))
                    return self.respond({'ok':True},cookie=self.cookie('',0))
                if path=='/api/graph' and method=='GET':return self.respond(graph_view(c,u))
                if path=='/api/assistant' and method=='GET':
                    q=parse_qs(urlsplit(self.path).query);after=q.get('after',['0'])[0]
                    return self.respond(assistant().view(u['id'],int(after) if after.isdigit() else 0))
                if path=='/api/assistant/messages' and method=='POST':
                    self.rate('assistant',60);return self.respond({'id':assistant().say(u,d.get('text'))},202)
                if path=='/api/assistant/reset' and method=='POST':
                    assistant().reset(u['id']);return self.respond({'ok':True})
                m=re.fullmatch(r'/api/assistant/actions/([a-f0-9]{24})',path)
                if m and method=='POST':
                    if d.get('decision') not in ('approve','reject'):raise Problem('请选择确认或取消')
                    assistant().decide(u,m[1],d['decision']=='approve');return self.respond({'ok':True})
                if path in ['/api/agents','/api/devices','/api/directory'] and method=='GET':
                    kind='device' if path=='/api/devices' else 'agent'
                    condition="a.kind=? AND a.owner_id=?" if path!='/api/directory' else "a.kind='agent' AND a.visibility='public' AND a.enabled=1 AND u.enabled=1 AND a.aid IS NOT NULL"
                    rows=c.execute('SELECT a.*,u.name owner_name FROM agents a JOIN users u ON a.owner_id=u.id WHERE '+condition+' ORDER BY a.created_at DESC',(kind,u['id']) if path!='/api/directory' else ()).fetchall()
                    return self.respond({'devices' if kind=='device' else 'agents':[agent_view(dict(a),a['owner_name'],u['id']) for a in rows]})
                if path in ['/api/agents','/api/devices'] and method=='POST':
                    kind='device' if path=='/api/devices' else 'agent'
                    if c.execute('SELECT COUNT(*) FROM agents WHERE owner_id=? AND kind=?',(u['id'],kind)).fetchone()[0]>=10:raise Problem('每人每类最多创建 10 个记录')
                    name=str(d.get('name','')).strip();desc=str(d.get('description','')).strip();vis=d.get('visibility','private');executor=d.get('executor','codex')
                    if executor not in EXECUTORS or (kind=='device' and executor!='codex'):raise Problem('请选择有效的 harness；调用设备不配置执行器')
                    if kind=='device' and vis!='private':raise Problem('调用设备仅归本人管理，不能发布到团队目录')
                    if not 1<=len(name)<=60 or len(desc)>400 or vis not in ['private','public']:raise Problem('请检查名称、简介和可见范围')
                    i=uid();c.execute('INSERT INTO agents(id,owner_id,name,description,visibility,kind,executor,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)',(i,u['id'],name,desc,vis,kind,executor,now(),now()));event(c,u['id'],kind+'.created',i)
                    return self.respond({'id':i},201)
                m=re.fullmatch(r'/api/agents/([a-f0-9]{24})/interactions(?:/(ix_[a-f0-9]{16,64}))?',path)
                if m and method=='GET':
                    a,full=interaction_access(c,u,m[1])
                    if not m[2]:
                        rows=[dict(r) for r in c.execute('SELECT i.ix id,i.role,i.peer,i.state,i.created_at,i.updated_at,i.goal,(SELECT COUNT(*) FROM ix_messages x WHERE x.agent_id=i.agent_id AND x.ix=i.ix) messages FROM interactions i WHERE i.agent_id=? ORDER BY i.updated_at DESC LIMIT 100',(a['id'],))]
                        names=peer_names(c,u,list({r['peer'] for r in rows}))
                        for r in rows:
                            r['peer_agent']=names.get(r['peer']);r['goal']=r['goal'][:200] if full else ''
                        return self.respond({'content_visible':full,'share_content':bool(a['share_content']),'sync_at':a['sync_at'],'sync_on':bool(runtime_view(a.get('runtime'),True) and runtime_view(a.get('runtime'),True).get('sync')),'interactions':rows})
                    if not full:raise Problem('所有者没有公开交互内容',403)
                    i=one(c,'SELECT ix id,role,peer,goal,state,created_at,updated_at FROM interactions WHERE agent_id=? AND ix=?',(a['id'],m[2]))
                    if not i:raise Problem('交互不存在',404)
                    msgs=[dict(r) for r in c.execute('SELECT msg_id,sender,kind,body,attachments,created_at FROM ix_messages WHERE agent_id=? AND ix=? AND (body!=\'\' OR attachments IS NOT NULL) ORDER BY created_at,rowid LIMIT 500',(a['id'],m[2]))]
                    for x in msgs:x['attachments']=json.loads(x['attachments']) if x['attachments'] else []
                    i['peer_agent']=peer_names(c,u,[i['peer']]).get(i['peer'])
                    return self.respond({'interaction':i,'messages':msgs})
                m=re.fullmatch(r'/api/connections/([a-f0-9]{24})/(diagnostics|enable-executor)',path)
                if m:
                    a=self.own(c,u,m[1])
                    if m[2]=='diagnostics' and method=='POST':
                        self.rate('install:'+a['id'],300);return self.respond(store_install_report(c,a,d.get('report')))
                    if m[2]=='diagnostics' and method=='GET':
                        return self.respond({'reports':[dict(row)|{'report':json.loads(row['report'])} for row in c.execute('SELECT id,created_at,updated_at,report FROM install_reports WHERE agent_id=? ORDER BY updated_at DESC LIMIT 10',(a['id'],))]})
                    if m[2]=='enable-executor' and method=='POST':
                        executor=d.get('executor')
                        if a['kind']!='device':raise Problem('这台电脑已开启接收任务',409)
                        if executor not in EXECUTORS or d.get('confirm') is not True:raise Problem('请选择执行器，并明确确认开启接收任务')
                        if c.execute("SELECT COUNT(*) FROM agents WHERE owner_id=? AND kind='agent'",(u['id'],)).fetchone()[0]>=10:raise Problem('可接收任务的连接最多 10 个')
                        c.execute("UPDATE agents SET kind='agent',executor=?,policy_ok=0,executor_ready=0,capabilities=NULL,runtime=NULL,pair_hash=NULL,pair_expires=CASE WHEN candidate_aid IS NOT NULL THEN 0 ELSE NULL END,challenge=NULL,updated_at=? WHERE id=?",(executor,now(),a['id']))
                        event(c,u['id'],'connection.executor_enabled',a['id']);c.commit()
                        return self.respond({'ok':True,'id':a['id'],'kind':'agent','executor':executor,'needs_local_setup':True})
                    raise Problem('接口不存在',404)
                m=re.fullmatch(r'/api/(agents|devices)/([a-f0-9]{24})(/pair|/disconnect)?',path)
                if m:
                    a=self.own(c,u,m[2]);kind='device' if m[1]=='devices' else 'agent'
                    if a['kind']!=kind:raise Problem('该记录不属于此入口',404)
                    action=m[3]
                    if action=='/pair' and method=='POST':
                        # Renew on this record. Keep its AID, active credential and pending
                        # candidate reservation; issuing a recovery link must not disconnect it.
                        t=token();c.execute('UPDATE agents SET pair_hash=?,pair_expires=?,challenge=NULL WHERE id=?',(digest(t),now()+3600,a['id']))
                        c.commit()
                        return self.respond({'url':BASE+'/#connect='+t,'recovery':bool(a['aid']),'kind':kind,'executor':a['executor'],'instructions':onboarding(a,t),'expires':now()+3600})
                    if action=='/disconnect' and method=='POST':
                        candidate=a['aid'] or (a['candidate_aid'] if a['provisioned'] else None)
                        if candidate:hub_action('revoke',candidate)
                        if a['executor']=='workbuddy':workbuddy().revoke(a)
                        c.execute('DELETE FROM interactions WHERE agent_id=?',(a['id'],))
                        c.execute('UPDATE agents SET aid=NULL,node_hash=NULL,pair_hash=NULL,candidate_aid=NULL,challenge=NULL,provisioned=0,last_seen=NULL,node_connected=0,policy_ok=0,executor_ready=0,runtime=NULL,work=NULL WHERE id=?',(a['id'],));event(c,u['id'],kind+'.disconnected',a['id'])
                        return self.respond({'ok':True})
                    if method=='PATCH' and not action:
                        if 'kind' in d and d['kind']!=kind:raise Problem('不能通过修改设置改变记录类型')
                        executor=d.get('executor',a['executor'])
                        if executor not in EXECUTORS or (kind=='device' and executor!='codex'):raise Problem('请选择有效的 harness 类型')
                        if executor!=a['executor'] and (a['aid'] or a['candidate_aid']):raise Problem('请先撤销接入，再更换执行器类型',409)
                        name=str(d.get('name',a['name'])).strip();desc=str(d.get('description',a['description'])).strip();vis=d.get('visibility',a['visibility'])
                        if kind=='device' and vis!='private':raise Problem('调用设备不能发布到团队目录')
                        if not 1<=len(name)<=60 or len(desc)>400 or vis not in ['public','private']:raise Problem('请检查输入')
                        # Rotating the pending token prevents old Codex instructions binding a Pi record.
                        if executor!=a['executor']:c.execute('UPDATE agents SET pair_hash=NULL,pair_expires=NULL WHERE id=?',(a['id'],))
                        share=d.get('share_content',bool(a['share_content']))
                        if kind=='device' or not isinstance(share,bool):raise Problem('请检查输入')
                        c.execute('UPDATE agents SET name=?,description=?,visibility=?,executor=?,share_content=?,updated_at=? WHERE id=?',(name,desc,vis,executor,share,now(),a['id']));event(c,u['id'],kind+'.updated',a['id']);return self.respond({'ok':True})
                    if method=='DELETE' and not action:
                        if a['aid'] or a['candidate_aid']:raise Problem('请先断开节点，再删除记录',409)
                        c.execute('DELETE FROM agents WHERE id=?',(a['id'],));return self.respond({'ok':True})
                if path.startswith('/api/admin/'):
                    if u['role']!='admin':raise Problem('需要管理员权限',403)
                    if path=='/api/admin/overview' and method=='GET':
                        users=[dict(x) for x in c.execute("SELECT u.id,u.username,u.name,u.role,u.enabled,u.created_at,SUM(CASE WHEN a.kind='agent' THEN 1 ELSE 0 END) agents,SUM(CASE WHEN a.kind='device' THEN 1 ELSE 0 END) devices FROM users u LEFT JOIN agents a ON a.owner_id=u.id GROUP BY u.id ORDER BY u.created_at")]
                        invs=[dict(x) for x in c.execute('SELECT id,kind,label,created_at,expires,used_at FROM invitations WHERE kind != ? ORDER BY created_at DESC LIMIT 30',('setup',))]
                        return self.respond({'users':users,'invitations':invs})
                    if path=='/api/admin/invitations' and method=='POST':
                        kind=d.get('kind','join');target=d.get('target');label=str(d.get('label','团队成员'))[:60]
                        if kind not in ['join','reset']:raise Problem('链接类型错误')
                        if kind=='reset' and not one(c,'SELECT id FROM users WHERE id=?',(target,)):raise Problem('账号不存在',404)
                        t=token();i=uid();c.execute('INSERT INTO invitations VALUES(?,?,?,?,?,?,?,?,NULL)',(i,digest(t),kind,label,target,u['id'],now(),now()+86400));event(c,u['id'],'invitation.created',i)
                        return self.respond({'url':BASE+'/#invite='+t,'expires':now()+86400})
                    m=re.fullmatch(r'/api/admin/users/([a-f0-9]{24})',path)
                    if m and method=='PATCH':
                        if m[1]==u['id']:raise Problem('不能停用自己')
                        c.execute('UPDATE users SET enabled=? WHERE id=?',(bool(d.get('enabled')),m[1]));c.execute('DELETE FROM sessions WHERE user_id=?',(m[1],));event(c,u['id'],'user.updated',m[1]);return self.respond({'ok':True})
                    m=re.fullmatch(r'/api/admin/invitations/([a-f0-9]{24})',path)
                    if m and method=='DELETE':c.execute('UPDATE invitations SET expires=0 WHERE id=?',(m[1],));return self.respond({'ok':True})
                raise Problem('接口不存在',404)
        except Problem as e:self.respond({'error':e.message},e.status)
        except sqlite3.IntegrityError:self.respond({'error':'用户名已被使用，请换一个' if path=='/api/auth/register' else '用户名或节点已被使用'},409)
        except (BrokenPipeError,ConnectionResetError):pass
        except Exception as e:
            print('request failed:',type(e).__name__,flush=True);self.respond({'error':'服务暂时不可用，请稍后重试'},500)

def cleanup_once():
    with LOCK,db() as c:
        for a in c.execute('SELECT * FROM agents WHERE candidate_aid IS NOT NULL AND pair_expires<?',(now(),)).fetchall():
            if a['provisioned'] and not a['aid']:hub_action('revoke',a['candidate_aid'])
            c.execute('UPDATE agents SET candidate_aid=NULL,challenge=NULL,provisioned=0 WHERE id=?',(a['id'],))
        c.execute('DELETE FROM install_reports WHERE updated_at<?',(now()-30*86400,))
        c.execute('DELETE FROM sessions WHERE expires<?',(now(),))
        c.execute('DELETE FROM interactions WHERE updated_at<?',(time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime(now()-RETENTION_DAYS*86400)),))

def cleanup():
    while True:
        time.sleep(60)
        try:cleanup_once()
        except Exception as e:print('cleanup:',type(e).__name__,flush=True)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--host',default='127.0.0.1');p.add_argument('--port',type=int,default=8890);a=p.parse_args()
    init();threading.Thread(target=cleanup,daemon=True).start();print(f'Agent portal listening on {a.host}:{a.port}',flush=True)
    ThreadingHTTPServer((a.host,a.port),Handler).serve_forever()
