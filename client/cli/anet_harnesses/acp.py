"""WorkBuddy's documented SSE + JSON-RPC ACP channel, with no write retries."""
import json
import queue
import threading
from urllib.parse import urlsplit, urlencode
from urllib.request import Request, build_opener
from urllib.error import HTTPError
import uuid

from .http import NoRedirect
from .transport import ProtocolError, ObservationTimeout, RpcError


def verified_link(link):
    parsed=urlsplit(link)
    host=parsed.hostname or ''
    if (parsed.scheme!='https' or parsed.username or parsed.password or parsed.fragment or parsed.query
            or parsed.port not in (None,443)
            or not (host=='acp.workbuddy.cn' or host.endswith('.agentos-run.net'))):
        raise ProtocolError('WorkBuddy returned an unsupported ACP host')
    return link


class WorkBuddyACP:
    def __init__(self, details, session_id):
        if details.get('task_id')!=session_id:raise ProtocolError('ACP task identity differs')
        self.link=verified_link(details.get('link',''))
        self.token=details.get('token')
        if not isinstance(self.token,str) or not self.token or any(c.isspace() for c in self.token):
            raise ProtocolError('ACP credential unavailable')
        self.session_id=session_id
        self.pending={};self.lock=threading.RLock();self.closed=None
        self.text=[];self.capturing=False;self.host_requests={}
        self.opener=build_opener(NoRedirect)
        request=Request(self.link,headers={'Authorization':'Bearer '+self.token,'Accept':'text/event-stream'})
        self.stream=self.opener.open(request,timeout=30)
        self.connection_id=self.stream.headers.get('Acp-Connection-Id')
        if not self.connection_id:self.stream.close();raise ProtocolError('ACP returned no connection identity')
        self.reader=threading.Thread(target=self._read,daemon=True,name='anet-workbuddy-sse');self.reader.start()
        try:
            self.request('initialize',{'protocolVersion':1,'clientCapabilities':{'fs':{'readTextFile':False,'writeTextFile':False}}})
            self.request('session/load',{'sessionId':session_id,'cwd':'/workspace','mcpServers':[]})
        except BaseException:self.close();raise

    def _read(self):
        data=[];size=0
        try:
            while True:
                raw=self.stream.readline(2*1024*1024+1)
                if not raw:raise ProtocolError('ACP stream closed; effect requires reconciliation')
                if len(raw)>2*1024*1024:raise ProtocolError('ACP frame too large')
                line=raw.decode('utf-8').rstrip('\r\n')
                if line=='':
                    if data:self._message(json.loads('\n'.join(data)))
                    data=[];size=0
                elif line.startswith('data:'):
                    data.append(line[5:].lstrip(' '));size+=len(raw)
                    if size>2*1024*1024:raise ProtocolError('ACP event too large')
        except BaseException as exc:
            self.closed=exc
            with self.lock:
                for waiter in self.pending.values():
                    try:waiter.put_nowait(ProtocolError('ACP stream lost; do not repeat writes'))
                    except queue.Full:pass

    def _message(self,value):
        if not isinstance(value,dict):raise ProtocolError('ACP message must be an object')
        if 'method' in value:
            params=value.get('params',{})
            if params.get('sessionId')!=self.session_id:return
            if 'id' in value:
                self.host_requests[str(value['id'])]=value
                return
            update=params.get('update',{})
            if self.capturing and value['method']=='session/update' and update.get('sessionUpdate')=='agent_message_chunk':
                content=update.get('content',{})
                if content.get('type')=='text':self.text.append(content.get('text',''))
            return
        if 'id' in value:
            with self.lock:waiter=self.pending.get(str(value['id']))
            if waiter:waiter.put_nowait(value)

    def _post(self,value):
        if self.closed:raise ProtocolError('ACP stream unavailable')
        request=Request(self.link,data=json.dumps(value,ensure_ascii=False).encode(),method='POST',
                        headers={'Authorization':'Bearer '+self.token,'Content-Type':'application/json',
                                 'Acp-Connection-Id':self.connection_id})
        try:
            with self.opener.open(request,timeout=30) as response:response.read(4097)
        except HTTPError as exc:
            raise ProtocolError('ACP HTTP '+str(exc.code)+'; reconcile original operation before another write') from None

    def request(self,method,params,timeout=30):
        ident=uuid.uuid4().hex;waiter=queue.Queue(maxsize=1)
        with self.lock:self.pending[ident]=waiter
        try:
            self._post({'jsonrpc':'2.0','id':ident,'method':method,'params':params})
            try:value=waiter.get(timeout=timeout)
            except queue.Empty:raise ObservationTimeout('ACP response not observed; do not resubmit') from None
            if isinstance(value,BaseException):raise value
            if 'error' in value:raise RpcError(value['error'])
            if 'result' not in value:raise ProtocolError('ACP response has no result')
            if method=='session/prompt':self.last_prompt_id=ident
            return value['result']
        finally:
            with self.lock:self.pending.pop(ident,None)

    def prompt(self,text):
        self.text=[];self.capturing=True
        try:
            result=self.request('session/prompt',{'sessionId':self.session_id,'prompt':[{'type':'text','text':text}]},timeout=None)
            reason=result.get('stopReason')
            if reason not in ('end_turn','max_tokens','max_turn_requests','refusal','cancelled'):
                raise ProtocolError('ACP prompt has no documented terminal reason')
            return {'state':'completed' if reason=='end_turn' else 'interrupted' if reason=='cancelled' else 'failed',
                    'native_id':self.session_id,'result':{'text':''.join(self.text),
                    'native_stop_reason':reason,'native_evidence':{'kind':'workbuddy_acp_round',
                    'session_id':self.session_id,'activity_id':getattr(self,'last_prompt_id',None),
                    'final_message_id':None,'final_assistant':bool(self.text),'terminal_status':reason,
                    'response_correlated_to_request':True},
                    'scope':'exact ACP prompt response on exclusively owned channel'}}
        finally:self.capturing=False

    def close(self):
        self.closed=ProtocolError('ACP connection closed locally')
        self.stream.close()


def read_overview(details,session_id):
    """Read-only artifacts for the already-created initial task; never prompt again."""
    if details.get('task_id')!=session_id:raise ProtocolError('artifact task identity differs')
    link=verified_link(details.get('link',''));parsed=urlsplit(link)
    if not parsed.path.endswith('/acp'):return None
    base=link[:-4]
    token=details.get('token')
    if not isinstance(token,str) or not token or any(c.isspace() for c in token):return None
    url=base+'/api/session/artifacts?'+urlencode({'sessionId':session_id,'type':'overview','limit':500})
    request=Request(url,headers={'Authorization':'Bearer '+token,'Accept':'application/json'})
    with build_opener(NoRedirect).open(request,timeout=20) as response:raw=response.read(2*1024*1024+1)
    if len(raw)>2*1024*1024:raise ProtocolError('artifact response too large')
    result=json.loads(raw)
    data=result.get('data',{})
    if result.get('code')!=0 or data.get('sessionId')!=session_id:raise ProtocolError('artifact response does not match source')
    texts=[]
    for entry in data.get('artifacts',[]):
        artifact=entry.get('artifact',{})
        if entry.get('sessionId')==session_id and entry.get('event')!='deleted' and artifact.get('type')=='overview' and isinstance(artifact.get('text'),str):
            texts.append(artifact['text'])
    return '\n\n'.join(texts) or None
