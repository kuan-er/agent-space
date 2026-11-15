import collections,json,os,queue,subprocess,threading,uuid
from pathlib import Path

class RpcError(RuntimeError):
    def __init__(self,error):self.error=error;super().__init__('backend RPC error '+str(error.get('code','unknown')))
class ProtocolError(RuntimeError):pass
class ObservationTimeout(TimeoutError):pass

HOST_INPUT_EVENTS=frozenset(('serverRequest/resolved','turn/started','turn/completed',
                            'thread/closed','thread/status/changed'))

def _response_id(value):
    if not (type(value) is int and -(2**63)<=value<2**63 or isinstance(value,str) and len(value)<=512):
        raise ProtocolError('invalid typed native request ID')
    return value

class RpcProcess:
    """One exclusively owned stdio process; short observation waits never kill it."""
    def __init__(self,argv,cwd,env=None,max_frame=2*1024*1024):
        if not argv or not Path(argv[0]).is_absolute():raise ValueError('absolute executable required')
        if Path(argv[0]).suffix.lower() in ('.cmd','.bat','.ps1'):raise ValueError('use a native executable or an installed runtime entrypoint')
        self.max_frame=max_frame;self.events=queue.Queue(maxsize=8192);self.host_requests=queue.Queue(maxsize=256)
        # Requests and their retirement events retain actual wire order. Two
        # separate legacy queues cannot preserve request -> resolved ordering.
        self.host_inputs=queue.Queue(maxsize=8192)
        self.pending={};self.lock=threading.RLock();self.closed=None;self.generation=uuid.uuid4().hex
        flags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0
        self.process=subprocess.Popen([str(x) for x in argv],cwd=cwd,env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,creationflags=flags)
        self.reader=threading.Thread(target=self._read,daemon=True);self.reader.start()
        self.errors=collections.deque(maxlen=8);threading.Thread(target=self._stderr,daemon=True).start()
    def _stderr(self):
        while self.process.stderr.read(4096):pass # Never forward provider stderr, prompts or secrets.
    def _read(self):
        try:
            while True:
                line=self.process.stdout.readline(self.max_frame+1)
                if not line:raise ProtocolError('owned runtime transport closed')
                if len(line)>self.max_frame or not line.endswith(b'\n'):raise ProtocolError('invalid or oversized protocol frame')
                value=json.loads(line)
                if not isinstance(value,dict):raise ProtocolError('protocol frame is not an object')
                if 'method' in value:
                    # Publication and an owner answer's final drain/write share
                    # this lock. The reader never acquires an owner/broker lock.
                    # Blocking readline stays outside it so new RPCs can flow.
                    with self.lock:
                        if 'id' in value or value['method'] in HOST_INPUT_EVENTS:
                            self.host_inputs.put_nowait((self.generation,value))
                        (self.host_requests if 'id' in value else self.events).put_nowait(value)
                elif 'id' in value:
                    with self.lock:q=self.pending.get(str(value['id']))
                    if q:q.put_nowait(value)
                else:raise ProtocolError('unrecognized protocol frame')
        except BaseException as e:
            self.closed=e
            with self.lock:
                for q in self.pending.values():
                    try:q.put_nowait(e)
                    except queue.Full:pass
    def send(self,value):
        raw=(json.dumps(value,ensure_ascii=False,separators=(',',':'))+'\n').encode()
        if len(raw)>self.max_frame:raise ProtocolError('request too large')
        with self.lock:
            if self.closed:raise ProtocolError('owned runtime is unavailable') from self.closed
            self.process.stdin.write(raw);self.process.stdin.flush()
    def request(self,method,params=None,timeout=15):
        ident=uuid.uuid4().hex;q=queue.Queue(maxsize=1)
        with self.lock:self.pending[ident]=q
        try:
            self.send({'jsonrpc':'2.0','id':ident,'method':method,'params':params or {}})
            try:value=q.get(timeout=timeout)
            except queue.Empty:raise ObservationTimeout('RPC observation timed out; backend effect remains unknown') from None
            if isinstance(value,BaseException):raise ProtocolError('transport lost; do not replay effects') from value
            if 'error' in value:raise RpcError(value['error'])
            if 'result' not in value:raise ProtocolError('missing RPC result')
            return value['result']
        finally:
            with self.lock:self.pending.pop(ident,None)
    def notification(self,timeout=1):
        try:return self.events.get(timeout=timeout)
        except queue.Empty:
            if self.closed:raise ProtocolError('runtime closed') from self.closed
            raise ObservationTimeout('no new event; runtime is not declared finished') from None
    def answer(self,request_id,result,generation):
        with self.lock:
            if generation!=self.generation:raise ProtocolError('stale runtime answer')
            self.send({'jsonrpc':'2.0','id':_response_id(request_id),'result':result})
    def answer_error(self,request_id,error,generation):
        with self.lock:
            if generation!=self.generation:raise ProtocolError('stale runtime answer')
            if not isinstance(error,dict) or type(error.get('code')) is not int or not isinstance(error.get('message'),str):
                raise ProtocolError('invalid native RPC error response')
            self.send({'jsonrpc':'2.0','id':_response_id(request_id),'error':error})
    def close(self,graceful_method=None):
        if graceful_method:
            try:self.request(graceful_method,timeout=10)
            except (RpcError,ProtocolError,ObservationTimeout):pass
        try:self.process.stdin.close()
        except OSError:pass
        # Host shutdown does not mean an agent task completed; caller retains its ledger.
        try:self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:self.process.terminate();self.process.wait(timeout=10)
        self.reader.join(timeout=1)
        self.process.stdout.close();self.process.stderr.close()
