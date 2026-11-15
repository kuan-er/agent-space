"""Owned harness service: a persistent, finite NDJSON interface for ANet Desktop.

No request starts a model except explicit submit/steer/resume. Source creation,
probe and restore never send an ordinary prompt. Runtime errors are redacted.
The durable journal forbids re-dispatch after a lost acknowledgement or restart.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import queue
import re
import sys
import threading
import time
import datetime

from .adapters import (CodexAdapter, ClaudeCodeAdapter, DeepSeekAdapter,
                       WorkBuddyAdapter, UnsupportedCapability, identifier)
from .catalog import NAMES, probe_all, probe_one
from .http import WorkBuddyHttp,WorkBuddyUnauthorized
from .acp import WorkBuddyACP, read_overview
from .workbuddy_auth import PortalWorkBuddyTokenBroker,EnvironmentToken,AuthorizationRequired,public_status
from .ledger import Ledger, EffectUncertain, OwnershipError, ConflictingOperation
from .native_input import NativeInputRequests
from .codex_native_input import CodexNativeInput
from .transport import RpcProcess, ObservationTimeout, ProtocolError
from .local_adapter import WorkBuddyLocalAdapter,LOCAL_TRANSPORT,LocalNotOnline
from anet_provider_harnesses.workbuddy_local import LocalAuthorizationRequired,LocalProtocolError

TERMINAL = ('completed', 'failed', 'interrupted','reply_observed')

def workbuddy_surface(options):
    surface=options.get('surface','local')
    if surface not in ('local','cloud'):raise ValueError('WorkBuddy surface must be local or cloud')
    return surface

def source_surface(source):
    if source['transport']==LOCAL_TRANSPORT:return 'local'
    if source['transport']=='workbuddy-cloud-tasks':return 'cloud'
    raise OwnershipError('unknown persisted WorkBuddy source transport')


def load_config(path):
    if path is None:
        return {'schema': 'anet-harness-config/1', 'harnesses': {}}
    raw = Path(path).read_bytes()
    config = json.loads(raw.decode('utf-16' if raw.startswith((b'\xff\xfe', b'\xfe\xff')) else 'utf-8-sig'))
    if not isinstance(config, dict) or not isinstance(config.get('harnesses', {}), dict):
        raise ValueError('invalid harness configuration')
    if config.get('schema', 'anet-harness-config/1') != 'anet-harness-config/1':
        raise ValueError('unsupported harness configuration schema')
    for h, options in config.get('harnesses', {}).items():
        if h not in NAMES or not isinstance(options, dict):
            raise ValueError('unknown harness configuration')
        if any(k.lower() in ('token','api_key','client_secret','access_token','password') for k in options):
            raise ValueError('credentials belong in the native login or explicit token environment, never configuration')
        if h=='workbuddy':workbuddy_surface(options)
        if h=='claude_code' and 'permission_mode' in options and options['permission_mode'] not in ('default','acceptEdits'):
            raise ValueError('Claude permission_mode must be absent, default or acceptEdits')
    return config


def probe(config=None,node_root=None):
    """No model, login, native-credential file reads or remote API requests.

    A local pairing check may read the selected node's private connection
    record (including its node token) and inspect local node status. It never
    fetches an OAuth token or returns the connection credential.
    """
    config = config or {'harnesses': {}}
    rows = []
    paired_node_connection_check_attempted = False
    for harness in NAMES:
        options = config.get('harnesses', {}).get(harness, {})
        row = probe_one(harness, options.get('executable'),surface=workbuddy_surface(options) if harness=='workbuddy' else None)
        if harness == 'claude_code':
            row['sdk_available'] = importlib.util.find_spec('claude_agent_sdk') is not None
            if row['available'] and not row['sdk_available']:
                row.update(state='needs_sdk', detail='Claude Code 已安装，需要安装随 ANet 提供的 Agent SDK。')
        if harness == 'deepseek_harness':
            sdk = importlib.util.find_spec('deepseek_harness') is not None
            runtime = importlib.util.find_spec('deepseek_harness_runtime') is not None
            configured = all(isinstance(options.get(k), str) and options[k].strip() for k in ('dsh_home','provider','model'))
            row.update(sdk_available=sdk, runtime_available=runtime, configured=configured)
            if sdk and runtime:
                row.update(available=True, state='configured_unverified' if configured else 'needs_provider',
                           detail='SDK 和运行时已安装；请在连接设置中选择提供商、模型和 Harness 工作目录。')
        if harness == 'workbuddy':
            surface=workbuddy_surface(options)
            token_env = options.get('token_env', 'ANET_WORKBUDDY_ACCESS_TOKEN')
            configured = bool(surface=='cloud' and isinstance(token_env, str) and os.environ.get(token_env))
            if configured:
                auth=public_status('environment_override_unverified');auth.update(auth_source='environment_override')
            elif node_root:
                paired_node_connection_check_attempted = True
                try:auth=PortalWorkBuddyTokenBroker(node_root,surface=surface).paired_status()
                except Exception:auth=public_status('connection_unverified',surface=surface)
            else:auth=public_status('needs_pairing',surface=surface)
            row.update(configured=configured or auth['paired_locally'], available=configured,
                       state='environment_override_unverified' if configured else auth['state'],
                       auth_state='provided_not_checked' if configured else 'not_verified',
                       auth_source=auth['auth_source'],authorization=auth,
                       surface='workbuddy_local_assistant' if surface=='local' else 'workbuddy_cloud_tasks',
                       detail=('WorkBuddy 本机助理。' if surface=='local' else 'WorkBuddy 云端任务。')+auth['reason'])
        rows.append(row)
    return {'schema':'anet-harnesses/1','observed_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'harnesses':rows,'native_credential_files_read':False,
            'paired_node_connection_check_attempted':paired_node_connection_check_attempted,
            'tasks_started':0}


def source_view(row):
    value=dict(row, local_id=row['id'], source_id=row['id'], backend_id=row['native_id'])
    if row['harness']=='workbuddy':
        local=source_surface(row)=='local'
        value.update(surface='workbuddy_local_assistant' if local else 'workbuddy_cloud_tasks',
                     workspace_scope='local_delegation_context',cloud_workspace=None,cloud_workspace_verified=False)
        if local:value.update(context_id=row['id'],native_id=None,backend_id=None,native_session_id=None,
                              native_terminal_verified=False,workspace_binding_verified=False,pc_identity_verified=False)
    return value


def operation_view(row):
    if row is None:
        return None
    result = row.get('result')
    if isinstance(result, str):
        try: result = json.loads(result)
        except ValueError: result = None
    value=dict(row, operation_id=row['id'], local_id=row['session_id'],
                source_id=row['session_id'], backend_id=row.get('native_id'), result=result,
                result_text=result.get('text', '') if isinstance(result, dict) else '',
                terminal=row['state'] in TERMINAL,
                business_result_verified=False)
    if row['kind']=='workbuddy_local_prompt':
        value.update(local_observation_finished=row['state']=='reply_observed',native_terminal_verified=False,
                     business_effect_verified=False,workspace_binding_verified=False,pc_identity_verified=False,
                     surface='workbuddy_local_assistant',native_session_id=None)
    return value


class HarnessService:
    def __init__(self, state_root, workspace, config=None, adapters=None, config_path=None, node_root=None):
        self.root = Path(state_root).resolve()
        self.workspace = Path(workspace).resolve()
        if not self.workspace.is_dir():
            raise ValueError('workspace must exist')
        if os.name=='nt':
            from .ipc_windows import ensure_private_runtime_root
            ensure_private_runtime_root(self.root)
        else:self.root.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.config = config or {'harnesses': {}}
        self.config_path=Path(config_path).resolve() if config_path else self.root/'config.json'
        self.ledger = Ledger(self.root / 'harness.sqlite')
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix='anet-harness')
        self.jobs = {}
        self.job_sources = {}
        self.errors = {}
        self.adapters = dict(adapters or {})
        self.rpcs = {}
        self.prepared_sources=set()
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.codex_events = []
        self.codex_items = {}
        self.native_inputs = NativeInputRequests()
        self.codex_native_inputs = CodexNativeInput(self.ledger,lambda:self.rpcs.get('codex'))
        self.codex_goal_events={}
        self.codex_last_turn={}
        self.monitor = None
        self.bridge=None
        self.node_root=node_root
        self.delegations=None
        self.workbuddy_brokers={}
        self.preflight_rejections={}
        self.preflight_inputs={}
        from .package_release import package_digest,SCHEMA as PACKAGE_SCHEMA
        self.package_sha256=package_digest();self.package_schema=PACKAGE_SCHEMA
        with self.ledger.connect() as c:
            c.executescript('''CREATE TABLE IF NOT EXISTS source_intents(
                id TEXT PRIMARY KEY,harness TEXT NOT NULL,workspace TEXT NOT NULL,state TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS requests(
                id TEXT PRIMARY KEY,source_id TEXT NOT NULL,digest TEXT NOT NULL,error TEXT);''')
            c.executescript('''CREATE TABLE IF NOT EXISTS workbuddy_source_surfaces(id TEXT PRIMARY KEY,surface TEXT NOT NULL);
                INSERT OR IGNORE INTO workbuddy_source_surfaces SELECT id,'cloud' FROM source_intents WHERE harness='workbuddy';''')

    def _adapter(self, harness, surface=None):
        with self.lock:
            options = self.config.get('harnesses', {}).get(harness, {})
            if harness=='workbuddy':surface=surface or workbuddy_surface(options)
            key='workbuddy:'+surface if harness=='workbuddy' else harness
            if key in self.adapters:return self.adapters[key]
            # Explicit test/legacy injected cloud adapters retain their surface.
            if harness=='workbuddy' and surface=='cloud' and harness in self.adapters:return self.adapters[harness]
            if harness!='workbuddy' and harness in self.adapters:return self.adapters[harness]
            if harness == 'codex':
                path = options.get('executable') or probe_one(harness).get('path')
                if not path: raise UnsupportedCapability('Codex 尚未安装；请使用连接设置中的安装入口。')
                rpc = RpcProcess([str(Path(path).resolve()), 'app-server'], str(self.workspace))
                adapter = CodexAdapter(rpc, self.ledger)
                try: adapter.initialize()
                except BaseException:
                    rpc.close(); raise
                self.rpcs[harness] = rpc
                self.adapters[harness] = adapter
                self.monitor = threading.Thread(target=self._monitor_codex, daemon=True, name='anet-codex-events')
                self.monitor.start()
                return adapter
            elif harness == 'claude_code':
                path = options.get('executable') or probe_one(harness).get('path')
                if not path: raise UnsupportedCapability('Claude Code 尚未安装。')
                if importlib.util.find_spec('claude_agent_sdk') is None:
                    raise UnsupportedCapability('Claude Agent SDK 尚未安装。')
                adapter = ClaudeCodeAdapter(self.ledger, path,native_inputs=self.native_inputs,
                                            permission_mode=options.get('permission_mode'))
            elif harness == 'deepseek_harness':
                if not all(options.get(k) for k in ('dsh_home','provider','model')):
                    raise UnsupportedCapability('请先设置 DeepSeek Harness 提供商、模型和专用工作目录。')
                adapter = DeepSeekAdapter(self.ledger, options['dsh_home'], self.workspace,
                                          options['provider'], options['model'],native_inputs=self.native_inputs)
            elif harness == 'workbuddy':
                if surface=='local':
                    adapter=WorkBuddyLocalAdapter(self.ledger,self.node_root,self._workbuddy_token_broker('local'))
                    self.adapters[key]=adapter;return adapter
                env_name = options.get('token_env','ANET_WORKBUDDY_ACCESS_TOKEN')
                if isinstance(env_name,str) and env_name and os.environ.get(env_name):provider=EnvironmentToken(env_name)
                else:provider=self._workbuddy_token_broker('cloud')
                provider() # Authorization is checked before any source/task mutation intent.
                adapter = WorkBuddyAdapter(self.ledger, WorkBuddyHttp(provider),
                                          channel_factory=WorkBuddyACP, overview_reader=read_overview)
            else:
                raise ValueError('unknown harness')
            self.adapters[key] = adapter
            return adapter

    def _workbuddy_token_broker(self,surface='local'):
        if self.node_root is None or not Path(self.node_root).is_dir():raise AuthorizationRequired('needs_pairing',surface=surface)
        if surface not in self.workbuddy_brokers:
            from .network import NetworkError
            try:self.workbuddy_brokers[surface]=PortalWorkBuddyTokenBroker(self.node_root,surface=surface)
            except (NetworkError,OSError,ValueError):raise AuthorizationRequired('node_identity_unverified',surface=surface) from None
        return self.workbuddy_brokers[surface]

    def _source_adapter(self,source):
        return self._adapter(source['harness'],source_surface(source) if source['harness']=='workbuddy' else None)

    def workbuddy_auth_status(self,refresh=True,surface=None):
        if type(refresh) is not bool:raise ValueError('authorization refresh must be boolean')
        options=self.config.get('harnesses',{}).get('workbuddy',{})
        surface=surface or workbuddy_surface(options)
        if surface not in ('local','cloud'):raise ValueError('unknown WorkBuddy surface')
        env_name=options.get('token_env','ANET_WORKBUDDY_ACCESS_TOKEN')
        if surface=='cloud' and isinstance(env_name,str) and env_name and os.environ.get(env_name):
            result=public_status('environment_override_unverified')
            result.update(state='environment_override_unverified',auth_source='environment_override',
                          reason='已提供高级环境变量覆盖；WorkBuddy 云端权限和原生任务尚未验收。')
            return result
        try:return self._workbuddy_token_broker(surface).status(refresh=refresh)
        except AuthorizationRequired as error:return public_status(error.reason_code,surface=surface)
        except Exception:return public_status('broker_unavailable',surface=surface)

    def _monitor_codex(self):
        rpc = self.rpcs['codex']
        while not self.stop.is_set():
            try:
                event = rpc.notification(timeout=.15)
                p = event.get('params', {})
                key = (p.get('threadId'), p.get('turnId') or p.get('turn', {}).get('id'))
                if event.get('method') == 'item/completed' and p.get('item', {}).get('type') == 'agentMessage':
                    self.codex_items.setdefault(key, {})[p['item']['id']] = p['item']
                if event.get('method') == 'turn/completed':
                    self.codex_events.append(event)
                    self.codex_last_turn[p.get('threadId')]=p.get('turn',{})
                if event.get('method') == 'thread/goal/updated':
                    self.codex_goal_events[p.get('threadId')]=p.get('goal',{})
            except ObservationTimeout:
                pass
            except ProtocolError:
                return  # Transport loss cannot turn an operation into success or authorize replay.
            self._drain_codex_inputs(rpc)
            self.codex_native_inputs.sweep()
            for event in list(self.codex_events):
                p = event['params']; turn = p.get('turn', {}); thread_id = p.get('threadId')
                for row in self.ledger.operations():
                    if row['kind'] != 'prompt' or row['native_id'] != turn.get('id'): continue
                    source = self.ledger.session(row['session_id'])
                    if source['harness'] != 'codex' or source['native_id'] != thread_id: continue
                    if not turn.get('items'):
                        turn['items'] = list(self.codex_items.get((thread_id,turn['id']), {}).values())
                    try:self.adapters['codex'].observe_turn(source['id'],row['id'],event)
                    except OwnershipError:continue
                    self.codex_events.remove(event)
                    self.codex_items.pop((thread_id,turn['id']),None)
                    break
            for thread_id,goal in list(self.codex_goal_events.items()):
                self._observe_goal(thread_id,goal)

    def _drain_codex_inputs(self,rpc):
        # This ordered queue is populated at transport read time, including
        # resolved/terminal events which can overtake UI polling.
        self.codex_native_inputs.drain(rpc)

    def _observe_goal(self,thread_id,goal):
        if goal.get('threadId')!=thread_id:return
        for row in self.ledger.operations():
            if row['kind']!='goal_active' or row['native_id']!=thread_id or row['state'] in TERMINAL:continue
            result=json.loads(row['result']) if row['result'] else {}
            accepted_goal=result.get('goal',{})
            if accepted_goal.get('createdAt')!=goal.get('createdAt') or accepted_goal.get('objective')!=goal.get('objective'):continue
            native_status=goal.get('status')
            state={'active':'running','paused':'waiting_input','blocked':'waiting_input',
                   'usageLimited':'waiting_input','budgetLimited':'waiting_input','complete':'completed'}.get(native_status)
            if not state:continue
            text=''
            turn=self.codex_last_turn.get(thread_id,{})
            if state=='completed' and turn.get('status')=='completed':
                items=turn.get('items') or self.codex_items.get((thread_id,turn.get('id')),{}).values()
                text='\n'.join(i.get('text','') for i in items if i.get('type')=='agentMessage' and i.get('phase') not in ('commentary',))
            try:self.ledger.observe(row['id'],thread_id,state,dict(result,goal=goal,native_goal_status=native_status,text=text))
            except OwnershipError:
                # A verified stop may have published terminal since this row
                # was read. Preserve it and keep observing other sources.
                continue

    def create(self, source_id, harness,surface=None):
        identifier(source_id)
        if harness not in NAMES:raise ValueError('unknown harness')
        with self.lock:
            existing = next((s for s in self.ledger.sessions() if s['id']==source_id), None)
            if existing:
                if existing['harness'] != harness or existing['workspace'] != str(self.workspace):
                    raise OwnershipError('source id belongs to another harness or workspace')
                if harness=='workbuddy' and surface is not None and source_surface(existing)!=surface:raise OwnershipError('source surface is immutable')
                return source_view(existing)
            if harness=='workbuddy':
                with self.ledger.connect() as c:
                    prior=c.execute('SELECT surface FROM workbuddy_source_surfaces WHERE id=?',(source_id,)).fetchone()
                    selected=prior['surface'] if prior else surface or workbuddy_surface(self.config.get('harnesses',{}).get(harness,{}))
                    if surface is not None and surface!=selected:raise OwnershipError('source creation surface intent differs')
                    if selected not in ('local','cloud'):raise ValueError('unknown WorkBuddy source surface')
                    c.execute('INSERT OR IGNORE INTO workbuddy_source_surfaces VALUES(?,?)',(source_id,selected))
                if selected=='local':
                    self.ledger.bind(source_id,harness,'local-context:'+source_id,str(self.workspace),LOCAL_TRANSPORT)
                    self.prepared_sources.add(source_id)
                    return source_view(self.ledger.session(source_id))
                surface=selected
            # Resolve prerequisites before reserving the source. A missing install
            # has no remote effect and can safely be fixed in the setup screen.
            adapter = self._adapter(harness,surface)
            with self.ledger.connect() as c:
                c.execute('BEGIN IMMEDIATE')
                if c.execute('SELECT 1 FROM source_intents WHERE id=?',(source_id,)).fetchone():
                    if harness!='codex' or not self.ledger.creation(source_id):
                        raise EffectUncertain('source creation previously started; do not create a replacement')
                else:c.execute('INSERT INTO source_intents VALUES(?,?,?,?)',(source_id,harness,str(self.workspace),'creating'))
            try:row = adapter.create(source_id,str(self.workspace))
            except BaseException:
                with self.ledger.connect() as c:c.execute('UPDATE source_intents SET state=? WHERE id=?',('uncertain',source_id))
                raise
            with self.ledger.connect() as c:c.execute('UPDATE source_intents SET state=? WHERE id=?',('bound',source_id))
            if harness!='codex' or source_id in getattr(adapter,'loaded_source_ids',{source_id}):
                self.prepared_sources.add(source_id)
            return source_view(row)

    def restore(self, source_id):
        source=self.ledger.session(source_id)
        if source['workspace'] != str(self.workspace):raise OwnershipError('source workspace differs')
        if source['harness']=='workbuddy' and source_surface(source)=='local':
            return dict(source_view(source),backend_verified=False,detail='已恢复本地委派上下文；未创建或恢复 WorkBuddy 原生会话。')
        adapter=self._source_adapter(source)
        if hasattr(adapter,'restore'):
            if source['harness']=='deepseek_harness' and not self.ledger.operations(source_id):
                # start_session() was a lazy reservation with no native input;
                # preserve its exact ID rather than restoring a nonexistent log.
                adapter.sessions[source_id]=adapter.harness.start_session(source['native_id'])
                restored=source
            else:restored=adapter.restore(source_id)
            self.prepared_sources.add(source_id)
            return source_view(restored)
        if source['harness']=='workbuddy':
            pending=source['native_id'].startswith('pending-')
            if pending:return dict(source_view(source),backend_verified=False,detail='尚未派发云端任务。')
            return dict(source_view(source),native_status=adapter.status(source_id))
        raise UnsupportedCapability('native session restore is unavailable')

    def _reserve(self, source_id, operation_id, payload):
        identifier(operation_id)
        digest=hashlib.sha256(json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        with self.ledger.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute('SELECT * FROM requests WHERE id=?',(operation_id,)).fetchone()
            if row:
                if row['source_id']!=source_id or row['digest']!=digest:
                    raise ConflictingOperation('operation id input conflict')
                return False
            c.execute('INSERT INTO requests VALUES(?,?,?,NULL)',(operation_id,source_id,digest))
        return True

    def _launch(self, source_id, operation_id, payload, call):
        with self.lock:
            if not self._reserve(source_id,operation_id,payload):
                return {'operation_id':operation_id,'state':'already_submitted_local',
                        'operation':operation_view(self.ledger.get(operation_id)), 'retry_authorized':False}
            def run():
                try:return call()
                except BaseException as exc:
                    self.errors[operation_id]=type(exc).__name__
                    with self.ledger.connect() as c:
                        c.execute('UPDATE requests SET error=? WHERE id=?',(type(exc).__name__,operation_id))
                    raise
            self.jobs[operation_id]=self.pool.submit(run)
            self.job_sources[operation_id]=source_id
        return {'operation_id':operation_id,'state':'submitted_local','backend_acknowledged':False}

    def submit(self, source_id, operation_id, text, goal=False, user_authorized=False):
        source=self.ledger.session(source_id)
        if not isinstance(text,str) or not text.strip() or len(text.encode())>1024*1024:
            raise ValueError('text must be nonempty and at most 1 MiB')
        if type(goal) is not bool:raise ValueError('goal must be boolean')
        if goal and user_authorized is not True:raise OwnershipError('native goal needs explicit user authorization')
        if goal and source['harness']=='workbuddy' and source_surface(source)=='local':
            raise UnsupportedCapability('WorkBuddy 本地 API 没有独立原生 Goal 接口。')
        payload={'action':'submit','text':text,'goal':goal,'user_authorized':user_authorized}
        intent_digest=hashlib.sha256(json.dumps([source_id,payload],sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        if operation_id in self.preflight_inputs and self.preflight_inputs[operation_id]!=intent_digest:
            raise ConflictingOperation('authorization preflight operation input differs')
        # Validate duplicate requests before considering session busy.
        with self.ledger.connect() as c:prior=c.execute('SELECT 1 FROM requests WHERE id=?',(operation_id,)).fetchone()
        if prior:return self._launch(source_id,operation_id,payload,lambda:None)
        def rejected(code,reason):
            value={'operation_id':operation_id,'source_id':source_id,'state':code,
                   'reason':reason,'request_sent':False,'mutation_intent_recorded':False,
                   'request_scope':'workbuddy_task_or_prompt','workbuddy_mutation_sent':False,
                   'retry_authorized':True,'retry_scope':'same_operation_after_user_authorization'}
            self.preflight_inputs[operation_id]=intent_digest
            self.preflight_rejections[operation_id]=value;return value
        try:adapter=self._source_adapter(source)
        except AuthorizationRequired as error:return rejected(error.reason_code,str(error))
        prepared_workbuddy=None
        if source['harness']=='workbuddy' and not goal:
            try:prepared_workbuddy=adapter.prepare_submit(source_id,operation_id,text)
            except AuthorizationRequired as error:return rejected(error.reason_code,str(error))
            except WorkBuddyUnauthorized:return rejected('needs_authorization','请先更新 WorkBuddy 网页授权；此次尚未发送任务。')
            except LocalNotOnline:
                value=rejected('needs_online','请先在本机登录并打开 WorkBuddy 本地助理；此次尚未发送任务。')
                value['retry_scope']='same_operation_after_local_readiness';return value
            except LocalAuthorizationRequired:return rejected('needs_authorization','WorkBuddy 本地助理授权尚未就绪；此次尚未发送任务。')
            except LocalProtocolError:
                value=rejected('local_readiness_unverified','本机助理只读预检未通过；此次尚未发送任务。')
                value['retry_scope']='same_operation_after_local_readiness';return value
            except ProtocolError:return rejected('authorization_unverified','WorkBuddy 云端来源或权限尚未通过预检；此次尚未发送任务。')
        def run():
            if prepared_workbuddy:return prepared_workbuddy()
            if source['harness'] in ('codex','deepseek_harness') and source_id not in self.prepared_sources:
                self.restore(source_id)
            if goal:return adapter.goal(source_id,operation_id,text,user_authorized=True)
            if source['harness']=='deepseek_harness' and source_id not in adapter.sessions:
                adapter.restore(source_id)
            if hasattr(adapter,'submit'):return adapter.submit(source_id,operation_id,text)
            return adapter.run(source_id,operation_id,text)
        try:
            with self.lock:
                for oid,job in self.jobs.items():
                    if not job.done() and self.job_sources.get(oid)==source_id:
                        raise OwnershipError('source already has an active operation; use steer or wait')
                for row in self.ledger.operations(source_id):
                    if row['kind'] in ('prompt','cloud_prompt','create_cloud_task','workbuddy_local_prompt') and row['state'] not in TERMINAL:
                        raise EffectUncertain('existing source operation must be reconciled before another submission')
                value=self._launch(source_id,operation_id,payload,run)
                self.preflight_rejections.pop(operation_id,None)
                if prepared_workbuddy and value.get('state')=='already_submitted_local':prepared_workbuddy.close()
                return value
        except BaseException:
            if prepared_workbuddy:prepared_workbuddy.close()
            raise

    def status(self, operation_id, refresh=True):
        if type(refresh) is not bool:raise ValueError('status refresh must be boolean')
        row=self.ledger.get(operation_id)
        if row is None and operation_id in self.preflight_rejections:return dict(self.preflight_rejections[operation_id],operation=None)
        active=bool(self.jobs.get(operation_id) and not self.jobs[operation_id].done())
        if row and refresh and (row['native_id'] or row['kind']=='workbuddy_local_prompt') and row['state'] not in TERMINAL and not active:
            source=self.ledger.session(row['session_id']);h=source['harness']
            try:
                if h=='workbuddy':self._source_adapter(source).status(source['id'],operation_id)
                elif h=='codex':
                    if row['kind']=='goal_active':
                        value=self._adapter(h).rpc.request('thread/goal/get',{'threadId':source['native_id']})
                        self._observe_goal(source['native_id'],value.get('goal') or {})
                    elif row['kind']=='prompt':self._adapter(h).reconcile(source['id'],operation_id)
            except (ProtocolError,ObservationTimeout,EffectUncertain,UnsupportedCapability,AuthorizationRequired,LocalProtocolError):pass
            row=self.ledger.get(operation_id)
        with self.ledger.connect() as c:req=c.execute('SELECT * FROM requests WHERE id=?',(operation_id,)).fetchone()
        job=self.jobs.get(operation_id)
        return {'operation':operation_view(row),'local_worker_active':bool(job and not job.done()),
                'local_error':req['error'] if req else None,
                'state':'not_found' if not row and not req else row['state'] if row else 'queued_unverified',
                'retry_authorized':False}

    def list(self):
        operations=[operation_view(r) for r in self.ledger.operations()]
        known={r['operation_id'] for r in operations}
        with self.ledger.connect() as c:requests=c.execute('SELECT id,source_id,error FROM requests').fetchall()
        for request in requests:
            if request['id'] not in known:
                operations.append({'operation_id':request['id'],'local_id':request['source_id'],'source_id':request['source_id'],
                                   'state':'local_error' if request['error'] else 'queued_unverified',
                                   'local_error':request['error'],'result_text':'','result':None,'terminal':False,
                                   'business_result_verified':False,'retry_authorized':False})
        return {'schema':'anet-harness-state/1','sources':[source_view(s) for s in self.ledger.sessions()],
                'operations':operations}

    def control(self, action, source_id, operation_id, user_authorized=False, text=None, control_id=None):
        source=self.ledger.session(source_id);adapter=self._source_adapter(source);h=source['harness']
        if action=='cancel':
            row=self.ledger.get(operation_id)
            if not row or row['session_id']!=source_id:raise OwnershipError('cancel requires the exact owned operation')
            if row['state'] in TERMINAL:return {'already_terminal':True,'operation':operation_view(row)}
            if h=='codex':adapter.cancel(source_id,operation_id)
            elif h=='workbuddy' and source_surface(source)=='local':return adapter.cancel(source_id,operation_id)
            elif h=='claude_code':adapter.cancel(source_id)
            else:adapter.cancel(source_id)
            return {'control_acknowledged':True,'terminal_observation_required':True}
        if action=='steer':
            if not isinstance(text,str) or not text.strip():raise ValueError('steer text is required')
            if h!='codex':raise UnsupportedCapability('此运行时没有已验证的原任务追加接口；请等待结束后继续同一来源会话。')
            return operation_view(adapter.steer(source_id,operation_id,identifier(control_id),text))
        if action not in ('pause','resume') or user_authorized is not True:
            raise OwnershipError('pause/resume require explicit user action')
        if h=='codex':
            return operation_view(adapter.goal(source_id,operation_id,status='paused' if action=='pause' else 'active',user_authorized=True))
        if h=='deepseek_harness':
            if source_id not in adapter.sessions:adapter.restore(source_id)
            payload={'action':action}
            return self._launch(source_id,operation_id,payload,lambda:adapter.goal_control(source_id,operation_id,action,user_authorized=True))
        raise UnsupportedCapability('此运行时没有独立的原生目标暂停/继续接口；不会模拟为已暂停。')

    def requests(self):
        result=self.native_inputs.list_requests()
        rpc=self.rpcs.get('codex')
        if rpc:self._drain_codex_inputs(rpc)
        result.extend(self.codex_native_inputs.list_requests())
        return {'requests':result}

    def answer(self,request_id,source_id,result,user_authorized=False):
        if user_authorized is not True:raise OwnershipError('native response needs explicit originating-user action')
        if isinstance(request_id,str) and request_id.startswith('native-'):
            self.ledger.session(source_id)
            return self.native_inputs.answer(request_id,source_id,result,user_authorized=True,validation_result=True)
        self.ledger.session(source_id,'codex')
        return self.codex_native_inputs.answer(request_id,source_id,result,user_authorized=True,validation_result=True)

    def configure(self,harness,settings):
        if harness not in NAMES or not isinstance(settings,dict):raise ValueError('invalid harness settings')
        allowed={'codex':{'executable'},'claude_code':{'executable','permission_mode'},
                 'deepseek_harness':{'dsh_home','provider','model'},'workbuddy':{'token_env','surface'}}[harness]
        if set(settings)-allowed:raise ValueError('unsupported setting; credentials must use native login or OAuth')
        options={}
        for key,value in settings.items():
            if not isinstance(value,str) or not value.strip() or len(value)>2048:raise ValueError('invalid setting value')
            if key in ('executable','dsh_home'):
                path=Path(value)
                if not path.is_absolute():raise ValueError('absolute runtime path required')
                if key=='executable' and (not path.is_file() or path.suffix.lower() in ('.cmd','.bat','.ps1')):
                    raise ValueError('installed native executable required')
                value=str(path.resolve())
            if key=='token_env' and (not value.replace('_','').isalnum() or value[0].isdigit()):
                raise ValueError('invalid token environment name')
            if key=='surface' and value not in ('local','cloud'):raise ValueError('WorkBuddy surface must be local or cloud')
            if key=='permission_mode' and value not in ('native','default','acceptEdits'):
                raise ValueError('Claude permission_mode must be native, default or acceptEdits')
            options[key]=value
        with self.lock:
            if harness!='workbuddy' and harness in self.adapters:raise OwnershipError('runtime already connected; restart ANet to change its configuration')
            config=json.loads(json.dumps(self.config))
            config.setdefault('harnesses',{}).setdefault(harness,{}).update(options)
            if options.get('permission_mode')=='native':config['harnesses'][harness].pop('permission_mode',None)
            config['schema']='anet-harness-config/1'
            self.config_path.parent.mkdir(parents=True,exist_ok=True)
            temporary=self.config_path.with_name(self.config_path.name+'.tmp')
            temporary.write_text(json.dumps(config,ensure_ascii=False,indent=2),encoding='utf-8')
            os.replace(temporary,self.config_path)
            self.config=config
        return {'configured':True,'harness':harness,'credentials_stored':False,'probe':probe(config,self.node_root)}

    def _goal_handle(self,request):
        return False,None

    def handle(self, request):
        if not isinstance(request,dict):raise ValueError('request must be an object')
        handled,result=self._goal_handle(request)
        if handled:return result
        action=request.get('action')
        if action=='workbuddy_auth_status':return self.workbuddy_auth_status(request.get('refresh',True),request.get('surface'))
        if action in ('delegation_register','delegation_prepare','delegation_submit','delegation_status','delegation_list'):
            if self.node_root is None:raise UnsupportedCapability('此来源服务尚未连接 taskd，请通过 ANet 连接设置启动。')
            if self.delegations is None:
                from .delegation import Delegations
                self.delegations=Delegations(self,self.node_root)
            if action=='delegation_register':return self.delegations.register(identifier(request.get('source_id')),identifier(request.get('operation_id')))
            if action=='delegation_prepare':return self.delegations.prepare(identifier(request.get('source_id')),identifier(request.get('operation_id')),request.get('peer'),request.get('spec'))
            if action=='delegation_submit':return self.delegations.submit(identifier(request.get('operation_id')))
            if action=='delegation_status':return self.delegations.status(identifier(request.get('operation_id')))
            return {'delegations':[self.delegations.status(path.stem) for path in self.delegations.root.glob('*.json')]}
        if action in ('bridge_register','bridge_inspect','bridge_deliver','bridge_verify_turn'):
            if self.bridge is None:
                from .taskd_bridge import BridgeEndpoint
                self.bridge=BridgeEndpoint(self);self.bridge.start()
            if action=='bridge_register':return self.bridge.register(identifier(request.get('source_id')))
            if action=='bridge_inspect':return self.bridge.inspect(request.get('binding'))
            if action=='bridge_deliver':return self.bridge.deliver(request.get('binding'),request.get('delivery_id'),request.get('prompt'))
            return self.bridge.verify_turn(request.get('binding'),request.get('operation_id'),request.get('previous_activity'))
        if action=='configure':return self.configure(request.get('harness'),request.get('settings'))
        if action=='probe':return probe(self.config,self.node_root)
        if action=='list':return self.list()
        if action=='requests':return self.requests()
        if action=='answer':return self.answer(request.get('request_id'),identifier(request.get('source_id')),request.get('result'),request.get('user_authorized',False))
        if action=='create':return self.create(request.get('source_id'),request.get('harness'),request.get('surface'))
        if action=='restore':return self.restore(identifier(request.get('source_id')))
        if action=='submit':return self.submit(identifier(request.get('source_id')),identifier(request.get('operation_id')),request.get('text'),request.get('goal',False),request.get('user_authorized',False))
        if action=='status':return self.status(identifier(request.get('operation_id')),request.get('refresh',True))
        if action in ('pause','resume','cancel','steer'):
            return self.control(action,identifier(request.get('source_id')),identifier(request.get('operation_id')),
                                request.get('user_authorized',False),request.get('text'),request.get('control_id'))
        raise ValueError('unknown action')

    def close_owner_inputs(self):
        # The owner UI has detached. Pending interactive decisions default to
        # denial before waiting for the already-accepted native activity.
        self.native_inputs.close()
        self.codex_native_inputs.close()
        rpc=self.rpcs.get('codex')
        if rpc:self._drain_codex_inputs(rpc)

    def close(self):
        self.close_owner_inputs()
        # EOF detaches the window; already accepted model work is allowed to finish.
        # Each worker's durable operation remains reconcilable after a crash.
        self.pool.shutdown(wait=True,cancel_futures=False)
        # A Codex submit returns an enqueue ACK before its terminal event. Keep
        # the owned app-server alive after the window detaches until that exact
        # acknowledged turn finishes. Never terminate it merely because stdin
        # closed or an observation wait expired.
        while True:
            active=False
            for row in self.ledger.operations():
                if row['kind']=='prompt' and row['state'] not in TERMINAL:
                    source=self.ledger.session(row['session_id'])
                    rpc=self.rpcs.get(source['harness'])
                    if rpc and not rpc.closed and rpc.process.poll() is None:active=True
                elif row['kind']=='goal_active' and row['state'] in ('acknowledged','running'):
                    rpc=self.rpcs.get('codex')
                    if rpc and not rpc.closed and rpc.process.poll() is None:active=True
            if not active:break
            self.stop.wait(.2)
        self.stop.set()
        if self.monitor:self.monitor.join(timeout=1)
        if self.bridge:self.bridge.close()
        for rpc in self.rpcs.values():rpc.close()
        for h,adapter in self.adapters.items():
            if h=='deepseek_harness':adapter.close()


def create_service(*args,**kwargs):
    """Canonical class stays patchable by installed metadata-only hosts.

    Goal routes remain inside HarnessService.handle, so an owner/metadata guard
    patched onto that canonical method always runs before extension dispatch.
    """
    from .goal_control.service import CandidateHarnessService
    return CandidateHarnessService(*args,goal_owner_policy=True,**kwargs)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--state-root',required=True);p.add_argument('--workspace',required=True);p.add_argument('--config');p.add_argument('--node-root')
    args=p.parse_args()
    config_path=Path(args.config) if args.config else Path(args.state_root)/'config.json'
    service=create_service(args.state_root,args.workspace,load_config(config_path) if config_path.is_file() else None,config_path=config_path,node_root=args.node_root)
    from .goal_control.controller import GoalHold
    output_lock=threading.Lock()
    def respond(request):
        ident=request.get('id') if isinstance(request,dict) else None
        try:reply={'id':ident,'ok':True,'result':service.handle(request)}
        except BaseException as exc:
            # Native errors can contain bearer tokens, environment or prompts.
            # Only locally authored validation/capability messages are exposed.
            safe=(type(exc) in (ValueError,OwnershipError,EffectUncertain,ConflictingOperation,UnsupportedCapability,AuthorizationRequired))
            reply={'id':ident,'ok':False,'error':type(exc).__name__,
                   'reason':str(exc) if safe else '运行时请求未获验证，请检查原生登录或稍后核对原操作。',
                   'retry_authorized':False}
            if isinstance(exc,GoalHold):
                reply['reason_code']=str(exc) if re.fullmatch('[a-z0-9_]{1,160}',str(exc)) else 'goal_control_unverified'
                reply['reason']=reply['reason_code']
        with output_lock:
            try:print(json.dumps(reply,ensure_ascii=False,separators=(',',':')),flush=True)
            except (BrokenPipeError,OSError):pass
    # Control and status must remain responsive while a runtime handshake waits.
    requests_pool=ThreadPoolExecutor(max_workers=4,thread_name_prefix='anet-harness-request')
    try:
        for raw in sys.stdin.buffer:
            if len(raw)>2*1024*1024:
                respond({'id':None,'action':'invalid'});continue
            try:request=json.loads(raw)
            except (ValueError,UnicodeError):
                respond({'id':None,'action':'invalid'});continue
            requests_pool.submit(respond,request)
    finally:
        # EOF revokes the owner-input surface BEFORE waiting for any already
        # accepted control/handshake requests to return.
        service.close_owner_inputs()
        requests_pool.shutdown(wait=True)
        service.close()


if __name__=='__main__':
    # runpy/-m executes this file as __main__. The factory and facade must share
    # the canonical base class that metadata hosts instrument, never a second one.
    from anet_harnesses import service as canonical
    canonical.main()
