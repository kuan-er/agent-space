"""Native Codex Goals: one persisted thread, streamed events, no outer planner.

Stdio is private to the worker. An owner-configured loopback endpoint additionally
allows the native TUI to attach. Neither requests nor peers select an endpoint.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import queue
import re
import sqlite3
import subprocess
import tempfile
import threading
import time
import uuid

from ..platform import proc
from ..requesters.appserver import AppServer, RPCError
from .base import ExecResult


class NativeRPC:
    def __init__(self, executor, on_event=lambda event: None):
        self.executor, self.on_event = executor, on_event
        self.events = queue.Queue(maxsize=8192)
        self.process = self.ws = self.stderr = None
        self.closed = False

    def __enter__(self):
        ex = self.executor
        if ex.native_endpoint:
            self.ws = AppServer(ex.native_endpoint, token_file=ex.native_token_file).__enter__()
            self.ws.sock.settimeout(None)
        else:
            argv = [ex.command, 'app-server', '-c', 'features.goals=true']
            for setting in ex.config:
                argv += ['-c', setting]
            self.stderr = tempfile.TemporaryFile()
            opts = proc.background_options(new_group=True) if os.name == 'nt' else {'start_new_session': True}
            self.process = subprocess.Popen(proc.windows_script_argv(argv), stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=self.stderr, env=ex.env(), **opts)
        threading.Thread(target=self._reader, daemon=True).start()
        try:
            if not self.ws:
                self.rpc('initialize', {'clientInfo': {'name': 'taskd-native', 'version': '1.9.0'},
                                       'capabilities': {'experimentalApi': True}})
                self.send({'method': 'initialized', 'params': {}})
        except BaseException:
            self.__exit__()
            raise
        return self

    def _reader(self):
        try:
            while not self.closed:
                if self.ws:
                    value = self.ws.receive()
                else:
                    line = self.process.stdout.readline(16 * 1024 * 1024 + 1)
                    if not line: raise EOFError('native app server disconnected')
                    if len(line) > 16 * 1024 * 1024: raise RPCError('oversized native event')
                    value = json.loads(line)
                self.events.put(value)
        except Exception as error:
            if not self.closed: self.events.put(error)

    def send(self, value):
        if self.ws: self.ws.send(value)
        else:
            self.process.stdin.write((json.dumps(value, ensure_ascii=False)+'\n').encode())
            self.process.stdin.flush()

    def receive(self, timeout=1):
        try: value = self.events.get(timeout=timeout)
        except queue.Empty: raise TimeoutError('waiting for native event') from None
        if isinstance(value, Exception): raise value
        return value

    def dispatch(self, value):
        # Remote work cannot grant its own privilege escalation. User decisions
        # are delivered through the native UI or a future explicit approval adapter.
        if value.get('method') and 'id' in value:
            self.on_event({'method': 'native/needsApproval', 'params': {'method': value['method']}})
            if value['method']=='item/commandExecution/requestApproval':
                self.send({'id':value['id'],'result':{'decision':'decline'}})
            else:
                self.send({'id':value['id'],'error':{'code':-32601,'message':'Approval requires the node owner'}})
        elif value.get('method'):
            self.on_event(value)

    def rpc(self, method, params, timeout=20):
        rid = str(uuid.uuid4())
        self.send({'id':rid, 'method':method, 'params':params})
        end=time.monotonic()+timeout
        while time.monotonic()<end:
            try: value=self.receive(min(1,max(.01,end-time.monotonic())))
            except TimeoutError: continue
            if value.get('id')==rid:
                if 'error' in value: raise RPCError(str(value['error'])[:500])
                return value.get('result')
            self.dispatch(value)
        raise TimeoutError('native RPC acknowledgement unknown: '+method)

    def __exit__(self,*args):
        self.closed=True
        if self.ws:
            try: self.ws.sock.shutdown(2)
            except OSError: pass
            self.ws.close()
        if self.process:
            try:self.process.stdin.close()
            except OSError:pass
            try:self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:proc.kill_tree(self.process)
            self.process.stdout.close()
        if self.stderr:self.stderr.close()


def probe(executor):
    if not executor.native_goals:
        return {'available':False,'reason':'disabled_by_owner'}
    try:
        with NativeRPC(executor) as rpc:
            t=rpc.rpc('thread/start',{'cwd':str(Path.cwd()),'ephemeral':True,
                'sandbox':'read-only','approvalPolicy':'never'})['thread']
            try:
                rpc.rpc('thread/goal/get',{'threadId':t['id']})
            except RPCError as error:
                # Native goals intentionally require persistence. This precise
                # rejection proves the method exists without creating a saved task.
                if 'ephemeral thread does not support goals' not in str(error):raise
        return {'available':True,'harness':'codex','probe':'app-server goal/get; no model call',
                'persistent_session':True,'steer':True,'pause':True,'cancel_cleanup':True,
                'transport':'websocket' if executor.native_endpoint else 'stdio',
                'interactive_attach':bool(executor.native_endpoint)}
    except Exception as error:
        return {'available':False,'reason':'native_probe_failed: '+str(error)[:240]}


def retry_state(executor, tid):
    home=Path(executor.codex_home or os.environ.get('CODEX_HOME') or Path.home()/'.codex')
    path=home/'logs_2.sqlite'
    if not path.exists():return None
    try:
        with sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=.2) as c:
            row=c.execute("SELECT ts,feedback_log_body FROM logs WHERE thread_id=? AND target='codex_core::responses_retry' ORDER BY id DESC LIMIT 1",(tid,)).fetchone()
        if not row:return None
        ts,body=row
        delay=re.search(r'retrying sampling request \((\d+)/(\d+) in ([0-9.]+)(ms|s)\)',body)
        turn=re.search(r'turn.id=([0-9a-f-]+)',body)
        if not delay:return None
        seconds=float(delay[3])/(1000 if delay[4]=='ms' else 1)
        return {'at':ts,'turn_id':turn[1] if turn else None,'attempt':int(delay[1]),
                'retry_at':ts+seconds,'reason':'model_at_capacity' if 'Selected model is at capacity' in body else 'upstream_retry'}
    except (OSError,sqlite3.Error):return None


def run(executor, objective, *, work_dir, session, timeout, max_turns, cancel, emit, checkpoint, messages,
        reconcile_complete=True, stop_only=False):
    start=time.monotonic();tid=session;active=None;goal=None;final='';ended=False
    turns=0;stop_confirmed=False;blocked=None;last_retry=None;approval=False
    def event(v):
        nonlocal active,goal,final,ended,turns,approval
        m=v.get('method');p=v.get('params',{});item=p.get('item',{})
        if p.get('threadId') and tid and p['threadId']!=tid:return
        if m=='turn/started':active=p['turn']['id'];ended=False;final='';turns+=1
        if m=='turn/completed':active=None;ended=p['turn']['status']=='completed'
        if m=='thread/goal/updated':goal=p.get('goal')
        if m=='item/completed' and item.get('type')=='agentMessage' and item.get('phase') in ('final_answer','final',None):final=item.get('text','')
        if m=='native/needsApproval':approval=True
        if m in ('turn/started','turn/completed','thread/goal/updated','native/needsApproval','error'):
            emit(m, p)
        elif m=='item/completed' and item.get('type') in ('commandExecution','agentMessage'):
            emit(m,{'type':item['type'],'command':item.get('command'), 'exit_code':item.get('exitCode'),
                    'text':(item.get('text') or item.get('aggregatedOutput') or '')[-4000:]})
        elif m=='item/commandExecution/outputDelta':
            emit(m,{'text':p.get('delta','')[-4000:]})
    result=None
    try:
        with NativeRPC(executor,event) as rpc:
            args={'cwd':str(work_dir),'sandbox':executor.sandbox,'approvalPolicy':'on-request'}
            if executor.model:args['model']=executor.model
            t=rpc.rpc('thread/resume' if tid else 'thread/start',dict(args,threadId=tid) if tid else args)['thread']
            tid=t['id'];checkpoint(tid)  # durable BEFORE activating work
            goal=rpc.rpc('thread/goal/get',{'threadId':tid}).get('goal')
            if not stop_only and session and reconcile_complete and goal and goal.get('status')=='complete' and t.get('status',{}).get('type')=='idle':
                history=rpc.rpc('thread/read',{'threadId':tid,'includeTurns':True})['thread'].get('turns',[])
                completed=next((v for v in reversed(history) if v['status']=='completed'),None)
                text=next((v.get('text','') for v in reversed((completed or {}).get('items',[]))
                           if v.get('type')=='agentMessage' and v.get('phase') in ('final_answer','final',None)),None)
                if text:
                    result=ExecResult(True,text,tid,duration=time.monotonic()-start)
                    result.native_turns=0;result.native_goal=goal
                    emit('native/reconciled',{'thread_id':tid,'goal_status':'complete'})
                    return result
            # Do not adopt a running thread from an unrelated owner operation.
            if t.get('status',{}).get('type')=='active':
                turns_state=rpc.rpc('thread/read',{'threadId':tid,'includeTurns':True})['thread'].get('turns',[])
                active=next((x['id'] for x in reversed(turns_state) if x['status']=='inProgress'),None)
            elif not stop_only and not (cancel and cancel.is_set()):
                # Resuming a stopped/failed verification uses the SAME thread, with
                # updated acceptance feedback in the goal. Activation starts itself.
                rpc.rpc('thread/goal/set',{'threadId':tid,'objective':objective,'status':'active'})
            next_retry=0
            while time.monotonic()-start<timeout:
                if stop_only or (cancel and cancel.is_set()):blocked='canceled';break
                if approval:blocked='native_approval_required';break
                if goal and goal.get('status')=='complete' and ended and final:
                    result=ExecResult(True,final,tid,duration=time.monotonic()-start);break
                if turns>max_turns or (turns>=max_turns and ended):blocked='native_turn_budget';break
                if goal and goal.get('status') in ('paused','blocked','budget_limited','budgetLimited'):
                    blocked='native_'+goal['status'];break
                if ended and (not goal or goal.get('status')!='complete'):
                    # Codex owns continuation. Allow its next turn; do not inject one.
                    ended=False
                if active:
                    pending=messages()
                    if pending:
                        rpc.rpc('turn/steer',{'threadId':tid,'expectedTurnId':active,
                            'input':[{'type':'text','text':pending}]})
                if time.monotonic()>=next_retry:
                    retry=retry_state(executor,tid);next_retry=time.monotonic()+10
                    if retry and retry!=last_retry and retry['turn_id']==active:
                        emit('native/retry',retry);last_retry=retry
                try:rpc.dispatch(rpc.receive(1))
                except TimeoutError:pass
            if not result:
                blocked=blocked or 'native_time_budget'
                emit('native/stopping', {})
                # A stop ACK alone is not terminal-process cleanup.
                rpc.rpc('thread/goal/set',{'threadId':tid,'status':'paused'})
                if active:rpc.rpc('turn/interrupt',{'threadId':tid,'turnId':active})
                rpc.rpc('thread/backgroundTerminals/clean',{'threadId':tid})
                for _ in range(20):
                    state=rpc.rpc('thread/read',{'threadId':tid,'includeTurns':False})['thread']['status']['type']
                    if state=='idle':stop_confirmed=True;break
                    try:rpc.dispatch(rpc.receive(.2))
                    except TimeoutError:pass
                emit('native/stopped',{'confirmed':stop_confirmed,'reason':blocked,
                                      'scope':'native turn and managed background terminals; detached services require their own checks'})
                result=ExecResult(False,final,tid,error=blocked if stop_confirmed else 'native_stop_unconfirmed',
                                  duration=time.monotonic()-start,canceled=blocked=='canceled' and stop_confirmed,
                                  timed_out=blocked=='native_time_budget')
    except Exception as error:
        # Unknown outcome: never silently fall back to a second executor.
        result=ExecResult(False,final,tid,error='native_outcome_unknown: '+str(error)[:300],duration=time.monotonic()-start)
    result.native_turns=turns
    result.native_goal=goal
    return result
