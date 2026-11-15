"""Finite native operations. Every source is owned, persisted and resumed exactly.

No adapter searches auth/config files, guesses API keys, changes native permissions,
or interprets a tool/process exit as completed business. Optional native SDK imports
are lazy so discovery and UI startup do not require a model or credentials.
"""
import asyncio,json,threading,uuid
from pathlib import Path
from .ledger import OwnershipError,EffectUncertain
from .transport import ProtocolError,ObservationTimeout

class UnsupportedCapability(RuntimeError):pass

def identifier(value):
    if not isinstance(value,str) or not value.strip() or len(value)>512:raise ProtocolError('invalid native identity')
    return value

class CodexAdapter:
    id='codex'
    def __init__(self,rpc,ledger):self.rpc=rpc;self.ledger=ledger;self.loaded_source_ids=set()
    def initialize(self):
        result=self.rpc.request('initialize',{'clientInfo':{'name':'anet-owned-harness','title':'ANet','version':'1'},'capabilities':{'experimentalApi':True}})
        self.rpc.send({'method':'initialized','params':{}});return result
    def create(self,local_id,workspace):
        if any(s['id']==local_id for s in self.ledger.sessions()):
            s=self.ledger.session(local_id,self.id)
            if s['workspace']!=str(Path(workspace).resolve()):raise OwnershipError('workspace differs')
            return s
        cwd=str(Path(workspace).resolve());name='ANet 来源会话'
        first=self.ledger.reserve_creation(local_id,self.id,cwd,name)
        if not first:
            intent=self.ledger.creation(local_id)
            if not intent['native_id']:raise EffectUncertain('source creation has no native ACK; do not create a replacement')
            # An uncertain name ACK is reconciled by exact native metadata, not
            # by naming/creating again. Full-history read supports cold stored
            # threads on stores whose metadata-only read requires a loaded ID.
            result=self.rpc.request('thread/read',{'threadId':intent['native_id'],'includeTurns':True})
            thread=result.get('thread',{})
            if thread.get('id')!=intent['native_id'] or thread.get('name')!=name or str(Path(thread.get('cwd','')).resolve())!=cwd:
                raise EffectUncertain('original source persistence/name is not confirmed; no repeat creation')
            native=intent['native_id']
            if thread.get('status',{}).get('type') in ('idle','active'):self.loaded_source_ids.add(local_id)
        else:
            try:
                result=self.rpc.request('thread/start',{'cwd':cwd})
                native=identifier(result['thread']['id'])
                self.ledger.update_creation(local_id,'name_requested',native)
                self.rpc.request('thread/name/set',{'threadId':native,'name':name})
                self.loaded_source_ids.add(local_id)
            except BaseException:
                self.ledger.update_creation(local_id,'creation_unconfirmed');raise
        self.ledger.update_creation(local_id,'materialized',native)
        self.ledger.bind(local_id,self.id,native,cwd,'app-server/stdio');return self.ledger.session(local_id)
    def restore(self,local_id):
        s=self.ledger.session(local_id,self.id);r=self.rpc.request('thread/resume',{'threadId':s['native_id'],'cwd':s['workspace'],'excludeTurns':True})
        if r.get('thread',{}).get('id')!=s['native_id']:raise OwnershipError('resume changed native session')
        self.loaded_source_ids.add(local_id)
        return s
    def submit(self,local_id,op,text):
        s=self.ledger.session(local_id,self.id)
        def send():
            r=self.rpc.request('turn/start',{'threadId':s['native_id'],'clientUserMessageId':str(uuid.uuid5(uuid.NAMESPACE_URL,op)),'input':[{'type':'text','text':text,'text_elements':[]}]})
            return {'native_id':identifier(r['turn']['id'])}
        return self.ledger.dispatch(op,local_id,'prompt',{'text':text},send)
    def goal(self,local_id,op,objective=None,status='active',user_authorized=False):
        if not user_authorized:raise OwnershipError('goal lifecycle needs explicit originating-user authority')
        if status not in ('active','paused'):raise ValueError('only explicit start/resume/pause controls are exposed')
        s=self.ledger.session(local_id,self.id);params={'threadId':s['native_id'],'status':status,'origin':'user'}
        if objective is not None:params['objective']=objective
        def send():
            r=self.rpc.request('thread/goal/set',params);g=r.get('goal',{})
            if g.get('threadId')!=s['native_id'] or g.get('status')!=status:raise ProtocolError('goal mutation not confirmed')
            return {'native_id':s['native_id'],'result':{'control':'goal_'+status,'goal':g}}
        return self.ledger.dispatch(op,local_id,'goal_'+status,params,send)
    def status(self,local_id):
        s=self.ledger.session(local_id,self.id)
        return {'thread':self.rpc.request('thread/read',{'threadId':s['native_id'],'includeTurns':False}),
                'goal':self.rpc.request('thread/goal/get',{'threadId':s['native_id']})}
    def steer(self,local_id,op,control_id,text):
        s=self.ledger.session(local_id,self.id);r=self.ledger.get(op)
        if not r or r['session_id']!=local_id or not r['native_id'] or r['state'] in ('completed','failed','interrupted'):raise OwnershipError('steer needs exact active owned turn')
        if r['kind']!='prompt':raise UnsupportedCapability('ordinary-turn steer cannot use a Goal identity')
        params={'threadId':s['native_id'],'expectedTurnId':r['native_id'],'clientUserMessageId':str(uuid.uuid5(uuid.NAMESPACE_URL,control_id)),'input':[{'type':'text','text':text}]}
        def send():
            value=self.rpc.request('turn/steer',params)
            if value.get('turnId')!=r['native_id']:raise OwnershipError('steer acknowledgement changed turn')
            return {'native_id':r['native_id'],'result':{'control':'steer','terminal_observation_required':True}}
        return self.ledger.dispatch(control_id,local_id,'steer',params,send)
    def reconcile(self,local_id,op):
        s=self.ledger.session(local_id,self.id);r=self.ledger.get(op)
        if not r or r['session_id']!=local_id or not r['native_id']:raise EffectUncertain('no exact acknowledged turn to query')
        if r['kind']!='prompt':raise UnsupportedCapability('ordinary-turn reconciliation cannot use a Goal identity')
        cursor=None
        for _ in range(100):
            p={'threadId':s['native_id'],'itemsView':'full','limit':100}
            if cursor:p['cursor']=cursor
            page=self.rpc.request('thread/turns/list',p)
            for turn in page.get('data',[]):
                if turn.get('id')==r['native_id']:
                    if turn.get('status') in ('completed','failed','interrupted'):
                        if turn.get('status') in ('failed','interrupted'):
                            # Persistence can briefly show an interrupted tail
                            # while the owned runtime is starting its live turn.
                            runtime=self.rpc.request('thread/read',{'threadId':s['native_id'],'includeTurns':False})
                            if runtime.get('thread',{}).get('status',{}).get('type')=='active':return r
                        if turn.get('itemsView') not in (None,'full'):
                            items=[];item_cursor=None
                            for _ in range(100):
                                params={'threadId':s['native_id'],'turnId':turn['id'],'limit':100,'sortDirection':'asc'}
                                if item_cursor:params['cursor']=item_cursor
                                item_page=self.rpc.request('thread/items/list',params)
                                items.extend(entry['item'] for entry in item_page.get('data',[]) if entry.get('turnId')==turn['id'] and isinstance(entry.get('item'),dict))
                                item_cursor=item_page.get('nextCursor')
                                if not item_cursor:break
                            turn=dict(turn,items=items,itemsView='full')
                        return self.observe_turn(local_id,op,{'method':'turn/completed','params':{'threadId':s['native_id'],'turn':turn}})
                    return r
            cursor=page.get('nextCursor')
            if not cursor:break
        return r
    def cancel(self,local_id,op):
        s=self.ledger.session(local_id,self.id);r=self.ledger.get(op)
        if not r or r['session_id']!=local_id or not r['native_id']:raise OwnershipError('cancel needs exact acknowledged turn')
        if r['kind']!='prompt':raise UnsupportedCapability('Goal cancellation requires the explicit source-exclusive Goal control interface')
        return self.rpc.request('turn/interrupt',{'threadId':s['native_id'],'turnId':r['native_id']})
    def observe_turn(self,local_id,op,event):
        s=self.ledger.session(local_id,self.id);p=event.get('params',{});r=self.ledger.get(op)
        if not r or r['kind']!='prompt' or event.get('method')!='turn/completed' or p.get('threadId')!=s['native_id'] or p.get('turn',{}).get('id')!=r['native_id']:
            raise OwnershipError('completion does not name the exact owned turn')
        turn=p['turn'];state={'completed':'completed','failed':'failed','interrupted':'interrupted'}.get(turn.get('status'))
        if not state:raise ProtocolError('turn is not terminal')
        messages=[i for i in turn.get('items',[]) if i.get('type')=='agentMessage']
        final=[i for i in messages if i.get('phase') in ('final_answer','final')]
        text='\n'.join(i.get('text','') for i in (final or [i for i in messages if i.get('phase') not in ('commentary',)]))
        final_item=(final or messages)[-1] if (final or messages) else {}
        evidence={'kind':'codex_turn','session_id':s['native_id'],'activity_id':turn['id'],
                  'final_message_id':final_item.get('id'),'final_assistant':bool(final_item and text.strip()),
                  'terminal_status':turn['status'],'client_message_id':str(uuid.uuid5(uuid.NAMESPACE_URL,op))}
        return self.ledger.observe(op,r['native_id'],state,{'text':text,'native_turn_status':turn['status'],'native_evidence':evidence})

class _Exclusive:
    def __init__(self,ledger):self.ledger=ledger;self.active={};self.lock=threading.RLock()
    def _enter(self,sid,op):
        with self.lock:
            if sid in self.active:raise OwnershipError('another operation owns this native session')
            self.active[sid]=op
    def _leave(self,sid):
        with self.lock:self.active.pop(sid,None)

class ClaudeCodeAdapter(_Exclusive):
    id='claude_code'
    def __init__(self,ledger,cli_path,client_factory=None,native_inputs=None,permission_mode=None):
        if permission_mode not in (None,'default','acceptEdits'):
            raise ValueError('Claude permission_mode must be default or acceptEdits')
        super().__init__(ledger);self.cli_path=str(Path(cli_path).resolve());self.client_factory=client_factory;self.live={};self.cancelled=set();self.native_inputs=native_inputs;self.permission_mode=permission_mode
    def create(self,local_id,workspace):
        self.ledger.bind(local_id,self.id,str(uuid.uuid4()),workspace,'official-agent-sdk/stdio');return self.ledger.session(local_id)
    def restore(self,local_id):
        # The official SDK restores only when connecting with resume=<exact id>.
        # Do not claim a backend handshake from this local binding check.
        return dict(self.ledger.session(local_id,self.id),restore_on_next_query=True,backend_verified=False)
    def _factory(self,session,restore,permission_handler=None):
        if self.client_factory:return self.client_factory(session,restore)
        from claude_agent_sdk import ClaudeSDKClient,ClaudeAgentOptions
        kwargs={'cwd':session['workspace'],'cli_path':self.cli_path}
        if permission_handler is not None:kwargs['can_use_tool']=permission_handler
        if self.permission_mode is not None:kwargs['permission_mode']=self.permission_mode
        if restore:kwargs['resume']=session['native_id']
        else:kwargs['extra_args']={'session-id':session['native_id']}
        return ClaudeSDKClient(options=ClaudeAgentOptions(**kwargs))
    def run(self,local_id,op,text,restore=False):
        s=self.ledger.session(local_id,self.id)
        restore=restore or any(r['id']!=op and r['native_id']==s['native_id'] for r in self.ledger.operations(local_id))
        async def activity():
            permission_handler=None
            if self.native_inputs is not None:
                from .claude_native_input import ClaudeNativeInput
                permission_handler=ClaudeNativeInput(self.native_inputs,local_id,op,s['native_id'])
            client=self._factory(s,restore,permission_handler);await client.connect()
            with self.lock:self.live[local_id]=(client,asyncio.get_running_loop())
            try:
                await client.query(text)
                final=None;assistant=None;foreign_interval=False;foreign_results=0
                # receive_response() stops on the first ResultMessage, including
                # background/channel/peer results. The pinned SDK explicitly
                # identifies this application's query by origin None/human.
                async for message in client.receive_messages():
                    # Public SDK ResultMessage fields are validated; plain text is never terminal evidence.
                    if getattr(message,'session_id',None) and message.session_id!=s['native_id']:raise OwnershipError('Claude returned a different session')
                    if type(message).__name__=='UserMessage' and not getattr(message,'parent_tool_use_id',None):
                        origin=getattr(message,'origin',None)
                        foreign_interval=not (origin is None or isinstance(origin,dict) and origin.get('kind')=='human')
                        assistant=None
                    if type(message).__name__=='AssistantMessage' and not getattr(message,'parent_tool_use_id',None) and not foreign_interval:assistant=message
                    if type(message).__name__=='ResultMessage' or getattr(message,'type',None)=='result':
                        origin=getattr(message,'origin',None)
                        if not (origin is None or isinstance(origin,dict) and origin.get('kind')=='human'):
                            foreign_results+=1;assistant=None;foreign_interval=False;continue
                        if foreign_interval:assistant=None
                        final=message;break
                if final is None:raise ProtocolError('Claude stream ended without native ResultMessage')
                terminal_reason=getattr(final,'terminal_reason',None)
                state='interrupted' if terminal_reason in ('aborted_streaming','aborted_tools') else 'failed' if getattr(final,'is_error',True) or getattr(final,'subtype',None)!='success' else 'completed'
                final_text=getattr(final,'result',None) or ''
                evidence={'kind':'claude_result','session_id':s['native_id'],'activity_id':getattr(final,'uuid',None),
                    'final_message_id':getattr(assistant,'message_id',None) or getattr(assistant,'uuid',None),
                    'final_assistant':bool(assistant is not None and final_text.strip()),'terminal_status':terminal_reason or getattr(final,'subtype',None),
                    'result_correlated_to_submitted_query':True,'query_result_origin':'human' if getattr(final,'origin',None) else 'unattributed',
                    'foreign_results_discarded':foreign_results}
                return {'native_id':s['native_id'],'state':state,'result':{'text':final_text,'native_evidence':evidence, 'native_subtype':getattr(final,'subtype',None),'scope':'exclusive official SDK query interval'}}
            finally:
                if self.native_inputs is not None:self.native_inputs.close_operation(local_id,op)
                with self.lock:self.live.pop(local_id,None)
                await client.disconnect()
        def send():
            self._enter(local_id,op)
            self.cancelled.discard(local_id)
            try:return asyncio.run(activity())
            finally:
                if self.native_inputs is not None:self.native_inputs.close_operation(local_id,op)
                self._leave(local_id)
        return self.ledger.dispatch(op,local_id,'prompt',{'text':text,'restore':restore},send)
    def goal(self,local_id,op,objective,restore=False,user_authorized=False):
        if not user_authorized:raise OwnershipError('explicit user goal required')
        return self.run(local_id,op,'/goal '+objective,restore=restore)
    def cancel(self,local_id):
        self.ledger.session(local_id,self.id)
        with self.lock:value=self.live.get(local_id)
        if not value:raise OwnershipError('no live owned Claude SDK activity')
        client,loop=value
        self.cancelled.add(local_id)
        if self.native_inputs is not None:
            with self.lock:operation_id=self.active.get(local_id)
            if operation_id:self.native_inputs.close_operation(local_id,operation_id)
        try:asyncio.run_coroutine_threadsafe(client.interrupt(),loop).result(timeout=15)
        except BaseException:
            self.cancelled.discard(local_id);raise
        return {'control_acknowledged':True,'terminal_observation_required':True}
    def pause_goal(self,*args,**kwargs):raise UnsupportedCapability('Claude documents goal clear/resume-session; no independent native pause/resume goal RPC is verified')

class DeepSeekAdapter(_Exclusive):
    id='deepseek_harness'
    def __init__(self,ledger,dsh_home,workspace,provider,model,harness_factory=None,native_inputs=None):
        super().__init__(ledger)
        self.home=str(Path(dsh_home).resolve());self.workspace=str(Path(workspace).resolve());self.provider=identifier(provider);self.model=identifier(model)
        self.native_input_bridge=None
        if native_inputs is not None:
            from .dsh_native_input import DeepSeekNativeInput
            self.native_input_bridge=DeepSeekNativeInput(native_inputs)
        if harness_factory:self.harness=harness_factory()
        else:
            from deepseek_harness import DeepSeekHarness
            kwargs={'dsh_home':self.home,'cwd':self.workspace,'provider':provider,'model':model}
            from .runtime_environment import system_proxy_environment, native_runtime_cwd
            proxy_env=system_proxy_environment()
            if proxy_env:kwargs['env']=proxy_env
            runtime_cwd=native_runtime_cwd()
            if runtime_cwd:kwargs['runtime_cwd']=runtime_cwd
            if self.native_input_bridge is not None:kwargs.update(approval_requests=True,on_user_question=self.native_input_bridge.on_user_question)
            self.harness=DeepSeekHarness(**kwargs)
        if self.native_input_bridge is not None:self.native_input_bridge.start(self.harness.client)
        self.sessions={}
    def create(self,local_id,workspace):
        if str(Path(workspace).resolve())!=self.workspace:raise OwnershipError('runtime belongs to a different workspace')
        sid='session-'+uuid.uuid4().hex;self.ledger.bind(local_id,self.id,sid,workspace,'official-dsh-sdk:'+self.home)
        self.sessions[local_id]=self.harness.start_session(sid);return self.ledger.session(local_id)
    def restore(self,local_id):
        s=self.ledger.session(local_id,self.id)
        if s['workspace']!=self.workspace or s['transport']!='official-dsh-sdk:'+self.home:raise OwnershipError('DeepSeek runtime home/workspace differs')
        self.sessions[local_id]=self.harness.resume_session(s['native_id']);return s
    def run(self,local_id,op,text,control=False):
        s=self.ledger.session(local_id,self.id)
        if local_id not in self.sessions:raise OwnershipError('restore the exact native session first')
        if control and text not in ('/goal','/goal pause','/goal resume'):raise ValueError('only exact native Goal controls may share a live session')
        control_binding=False
        # A resume can start model activity. Its owner-input binding must be
        # exclusive before recording a new native-control dispatch intent.
        if control and self.native_input_bridge is not None and self.ledger.get(op) is None:
            action='resume' if text=='/goal resume' else 'pause' if text=='/goal pause' else 'show'
            control_binding=self.native_input_bridge.begin_control(local_id,op,s['native_id'],action)
        def send():
            if not control:self._enter(local_id,op)
            try:
                if not control and self.native_input_bridge is not None:self.native_input_bridge.bind_operation(local_id,op,s['native_id'])
                result=self.sessions[local_id].run(text)
                if result.session_id!=s['native_id']:raise OwnershipError('DeepSeek changed native session')
                reason=result.finish_reason
                state='completed' if reason=='completed' else 'interrupted' if reason in ('aborted','cancelled') else 'failed'
                events=getattr(result,'events',[]);activity=None;final_id=None;final_assistant=False
                for event in events:
                    data=event.get('data',{})
                    if event.get('type')=='agent/inbox/spliced' and activity is None:
                        inserted=data.get('inserted',[])
                        activity=next((m.get('id') for m in inserted if isinstance(m,dict) and m.get('id')),None)
                    if event.get('type')=='assistant/message':
                        message=data.get('message',data);final_id=message.get('id');final_assistant=True
                evidence={'kind':'deepseek_inbox_interval','session_id':s['native_id'],'activity_id':activity,
                          'final_message_id':final_id,'final_assistant':final_assistant and bool(result.final_response.strip()),'terminal_status':reason}
                from .smoke_support import failure_summary
                diagnostics=failure_summary(events) if state=='failed' else []
                return {'native_id':s['native_id'],'state':state,'result':{'text':result.final_response,'native_evidence':evidence,'native_finish_reason':reason,'failure_summary':diagnostics,'scope':'official SDK activity interval including native goal rounds'}}
            finally:
                if not control:
                    if self.native_input_bridge is not None:self.native_input_bridge.close_operation(local_id,op)
                    self._leave(local_id)
        try:return self.ledger.dispatch(op,local_id,'native_control' if control else 'prompt',{'text':text},send)
        finally:
            if control_binding:self.native_input_bridge.close_operation(local_id,op)
    def goal(self,local_id,op,objective,user_authorized=False):
        if not user_authorized:raise OwnershipError('explicit user goal required')
        return self.run(local_id,op,'/goal '+objective)
    def goal_control(self,local_id,op,action,user_authorized=False):
        if not user_authorized or action not in ('pause','resume','show'):raise OwnershipError('invalid goal control authority/action')
        return self.run(local_id,op,'/goal'+('' if action=='show' else ' '+action),control=True)
    def cancel(self,*args,**kwargs):raise UnsupportedCapability('no public per-session ordinary-turn cancel request in pinned SDK protocol; never kill an unrelated runtime')
    def close(self):
        if self.native_input_bridge is not None:self.native_input_bridge.close()
        try:self.harness.close()
        finally:
            if self.native_input_bridge is not None:self.native_input_bridge.join(1)

class WorkBuddyAdapter(_Exclusive):
    """True WorkBuddy Open API. Cloud task and local desktop are not substituted."""
    id='workbuddy'
    def __init__(self,ledger,request,channel_factory=None,overview_reader=None):
        super().__init__(ledger);self.request=request;self.channel_factory=channel_factory;self.overview_reader=overview_reader
    def create(self,local_id,workspace):
        sid='pending-'+uuid.uuid4().hex;self.ledger.bind(local_id,self.id,sid,workspace,'workbuddy-cloud-tasks');return self.ledger.session(local_id)
    def submit(self,local_id,op,prompt):
        return self.prepare_submit(local_id,op,prompt)()
    def prepare_submit(self,local_id,op,prompt):
        s=self.ledger.session(local_id,self.id)
        prior=self.ledger.get(op)
        kind=prior['kind'] if prior else 'create_cloud_task' if s['native_id'].startswith('pending-') else 'cloud_prompt'
        prepared=None;channel=None
        if prior is None:
            # All authorization and read-only native setup happens BEFORE the
            # mutation ledger intent. No OAuth grant means no task was sent.
            if kind=='create_cloud_task':
                body={'prompt':prompt,'name':'ANet '+local_id}
                if hasattr(self.request,'prepare'):prepared=self.request.prepare('POST','/tasks',body)
            else:
                if self.channel_factory is None:raise UnsupportedCapability('owned WorkBuddy ACP channel is not configured')
                details=self.request('GET','/tasks/'+s['native_id'],None)
                channel=self.channel_factory(details,s['native_id'])
        def send():
            self._enter(local_id,op)
            try:
                if kind=='create_cloud_task':
                    r=prepared() if prepared else self.request('POST','/tasks',{'prompt':prompt,'name':'ANet '+local_id})
                    return {'native_id':identifier(r.get('task_id'))}
                return channel.prompt(prompt)
            finally:self._leave(local_id)
        def dispatch():
            try:r=self.ledger.dispatch(op,local_id,kind,{'prompt':prompt},send)
            finally:
                if channel:channel.close()
            if kind=='create_cloud_task':self.ledger.promote(local_id,s['native_id'],r['native_id'])
            return r
        dispatch.close=lambda:channel.close() if channel else None
        return dispatch
    def recover(self,local_id,op):
        s=self.ledger.session(local_id,self.id);r=self.ledger.get(op)
        if not r or not r['native_id']:raise EffectUncertain('creation has no task id; cannot list/search/guess a replacement or resend')
        if s['native_id'].startswith('pending-'):self.ledger.promote(local_id,s['native_id'],r['native_id'])
        return self.status(local_id,op)
    def status(self,local_id,op=None):
        s=self.ledger.session(local_id,self.id)
        if s['native_id'].startswith('pending-'):raise EffectUncertain('no acknowledged WorkBuddy task id')
        r=self.request('GET','/tasks/'+s['native_id'],None)
        if r.get('task_id')!=s['native_id']:raise OwnershipError('task response changed identity')
        status=r.get('status');projection={'task_id':s['native_id'],'native_status':status,'completed':status=='completed','remote_cloud_workspace':True}
        # Do not put ACP token, sandbox ticket or private links into the journal/UI.
        if op and status in ('completed','failed','deleted','archived'):
            state='completed' if status=='completed' else 'failed'
            text=None
            if self.overview_reader and status=='completed':
                try:text=self.overview_reader(r,s['native_id'])
                except Exception:pass
            projection.update(text=text or '',result_available=bool(text),terminal_scope='native cloud task status')
            self.ledger.observe(op,s['native_id'],state,projection)
        return projection
    def local_readiness(self):return self.request('GET','/localassistant',None)
    def submit_local(self,*args,**kwargs):raise UnsupportedCapability('local assistant messages expose no owned-session identity or trustworthy execution terminal; marker text cannot prove completion')
    def goal(self,*args,**kwargs):raise UnsupportedCapability('WorkBuddy OpenAPI has no documented native Goal lifecycle; use explicitly selected taskd planned orchestration')
    def cancel(self,*args,**kwargs):raise UnsupportedCapability('no cancel endpoint in the verified WorkBuddy REST task contract')
