"""Persistent loopback test receiver; deliberately not a production listener."""
import http.server,json,os,pathlib,shutil,socket,threading,uuid
from repair_protocol import *

class Receiver:
    def __init__(self,root,parties,receipt_channel='loopback_test_only'):
        self.root=plain_path(root);self.root.mkdir(parents=True,exist_ok=True,mode=0o700)
        if os.name!='nt' and self.root.stat().st_mode&0o077:raise ValueError('artifact storage directory must already be private to its owner')
        self.guard=Lock(self.root/'receiver.lock');self.parties=parties;self.mutex=threading.RLock()
        self.bytes_received=0;self.calls={'begin':0,'chunk':0,'commit':0,'status':0};self.receipt_channel=receipt_channel
    def close(self):self.guard.close()
    def directory(self,mid):
        if not isinstance(mid,str) or not HEX.fullmatch(mid):raise ValueError('bad delivery id')
        return plain_path(self.root/mid)
    def manifest(self,mid):
        m=read_json(self.directory(mid)/'manifest.json')
        if validate_manifest(m)!=mid:raise ValueError('stored manifest corrupt')
        if (m['task_id'],m['provider'],m['requester']) not in self.parties:raise ValueError('delivery parties not admitted')
        return m
    def begin(self,m):
        with self.mutex:
            self.calls['begin']+=1;mid=validate_manifest(m)
            if (m['task_id'],m['provider'],m['requester']) not in self.parties:raise ValueError('delivery parties not admitted')
            d=self.directory(mid);d.mkdir(exist_ok=True)
            path=d/'manifest.json'
            if path.exists():
                if read_json(path)!=m:raise ValueError('existing delivery manifest mismatch')
            else:atomic_json(path,m)
            return self.status(mid)
    def chunk_path(self,mid,fi,ci):return plain_path(self.directory(mid)/'chunks'/str(fi)/str(ci))
    def expected_chunk(self,m,fi,ci):
        if type(fi)is not int or type(ci)is not int or not 0<=fi<len(m['files']):raise ValueError('invalid chunk index')
        f=m['files'][fi]
        if not 0<=ci<len(f['chunks']):raise ValueError('invalid chunk index')
        return f['chunks'][ci],min(CHUNK,f['size']-ci*CHUNK)
    def put(self,mid,fi,ci,data):
        with self.mutex:
            self.calls['chunk']+=1;m=self.manifest(mid);sha,size=self.expected_chunk(m,fi,ci)
            if len(data)!=size or digest(data)!=sha:raise ValueError('chunk size/hash mismatch')
            target=self.chunk_path(mid,fi,ci);target.parent.mkdir(parents=True,exist_ok=True)
            if target.exists():
                if target.stat().st_size!=size or sha_file(target)!=sha:raise ValueError('stored chunk corrupt; repair explicitly')
                return {'manifest_sha256':mid,'file':fi,'chunk':ci,'sha256':sha,'state':'already_verified'}
            tmp=target.with_name(target.name+'.tmp-'+uuid.uuid4().hex)
            try:
                with tmp.open('xb') as out:out.write(data);out.flush();os.fsync(out.fileno())
                os.replace(tmp,target)
            finally:
                if tmp.exists():tmp.unlink()
            self.bytes_received+=len(data)
            return {'manifest_sha256':mid,'file':fi,'chunk':ci,'sha256':sha,'state':'verified'}
    def status(self,mid):
        with self.mutex:
            self.calls['status']+=1;m=self.manifest(mid);present=[]
            for fi,f in enumerate(m['files']):
                indices=[]
                for ci,sha in enumerate(f['chunks']):
                    path=self.chunk_path(mid,fi,ci);_,size=self.expected_chunk(m,fi,ci)
                    if path.exists():
                        if not path.is_file() or path.stat().st_size!=size or sha_file(path)!=sha:raise ValueError('stored chunk corrupt')
                        indices.append(ci)
                present.append(indices)
            receipt_path=self.directory(mid)/'receipt.json';receipt=read_json(receipt_path) if receipt_path.exists() else None
            if receipt:
                if receipt.get('manifest_sha256')!=mid:raise ValueError('receipt identity mismatch')
                for fi,f in enumerate(m['files']):
                    path=source_path(self.directory(mid)/'files'/str(fi),f['name'])
                    if not path.is_file() or path.stat().st_size!=f['size'] or sha_file(path)!=f['sha256']:raise ValueError('received file corrupt')
            return {'manifest_sha256':mid,'present':present,'receipt':receipt,'signature_verification':'not_available_loopback_test'}
    def commit(self,mid):
        with self.mutex:
            self.calls['commit']+=1;m=self.manifest(mid);state=self.status(mid)
            if state['receipt']:return state['receipt']
            if any(len(state['present'][i])!=len(f['chunks']) for i,f in enumerate(m['files'])):raise ValueError('chunks missing')
            verified=[]
            for fi,f in enumerate(m['files']):
                target=source_path(self.directory(mid)/'files'/str(fi),f['name']);target.parent.mkdir(parents=True,exist_ok=True)
                if target.exists():
                    if target.stat().st_size!=f['size'] or sha_file(target)!=f['sha256']:raise ValueError('existing assembled file corrupt')
                else:
                    tmp=target.with_name(target.name+'.tmp-'+uuid.uuid4().hex)
                    try:
                        with tmp.open('xb') as out:
                            for ci in range(len(f['chunks'])):
                                with self.chunk_path(mid,fi,ci).open('rb') as inp:shutil.copyfileobj(inp,out,CHUNK)
                            out.flush();os.fsync(out.fileno())
                        if tmp.stat().st_size!=f['size'] or sha_file(tmp)!=f['sha256']:raise ValueError('assembled file mismatch')
                        os.replace(tmp,target)
                    finally:
                        if tmp.exists():tmp.unlink()
                verified.append({k:f[k] for k in ('name','size','sha256')})
            receipt={'schema':VERSION,'task_id':m['task_id'],'manifest_sha256':mid,'receiver_files_verified':True,'files':verified,'received_at':now(),
                     'channel':'loopback_test_only','signature_verification':'not_available_loopback_test','anet.receipt_verified':None,'requester_acceptance':'not_verified'}
            if self.receipt_channel=='portal_https_storage':
                receipt.pop('receiver_files_verified');receipt.update(channel='portal_https_storage',server_files_verified=True,recipient_files_verified=False,signature_verification='not_anet_receipt')
            atomic_json(self.directory(mid)/'receipt.json',receipt);return receipt

class TestServer(http.server.ThreadingHTTPServer):
    daemon_threads=True
    def __init__(self,receiver,fault=None):
        self.receiver=receiver;self.fault=fault;self.requests=[];self.max_request_bytes=0
        super().__init__(('127.0.0.1',0),Handler)
    @property
    def url(self):return 'http://127.0.0.1:'+str(self.server_address[1])

class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version='HTTP/1.1'
    def setup(self):
        super().setup();self.connection.settimeout(15)
    def log_message(self,*args):pass
    def handle_request(self):
        parts=self.path.strip('/').split('/');operation='';result=None
        try:
            length=int(self.headers.get('Content-Length','0'))
            if length<0 or length>MAX_MANIFEST:raise ValueError('request too large')
            self.server.max_request_bytes=max(length,self.server.max_request_bytes)
            body=self.rfile.read(length)
            if len(body)!=length:raise ValueError('short request')
            self.server.requests.append([self.command,self.path,length])
            if self.command=='POST' and parts==['deliveries']:
                operation='begin';result=self.server.receiver.begin(json.loads(body))
            elif self.command=='GET' and len(parts)==2 and parts[0]=='deliveries':
                operation='status';result=self.server.receiver.status(parts[1])
            elif self.command=='PUT' and len(parts)==6 and parts[0]=='deliveries' and parts[2]=='files' and parts[4]=='chunks':
                operation='chunk';result=self.server.receiver.put(parts[1],int(parts[3]),int(parts[5]),body)
            elif self.command=='POST' and len(parts)==3 and parts[0]=='deliveries' and parts[2]=='commit':
                operation='commit';result=self.server.receiver.commit(parts[1])
            else:raise ValueError('unsupported endpoint')
            if self.server.fault and self.server.fault(operation,parts,result):
                self.close_connection=True;self.connection.shutdown(socket.SHUT_RDWR);self.connection.close();return
            self.answer(200,result)
        except FileNotFoundError:self.answer(404,{'error':'delivery_not_found'})
        except (ValueError,TypeError,KeyError,json.JSONDecodeError):self.answer(409,{'error':'request_or_integrity_check_failed'})
        except (BrokenPipeError,ConnectionResetError):self.close_connection=True
    def answer(self,status,value):
        data=canonical(value);self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
    do_GET=handle_request;do_POST=handle_request;do_PUT=handle_request

def serve_fixture(receiver,fault=None):
    server=TestServer(receiver,fault);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();return server,thread
