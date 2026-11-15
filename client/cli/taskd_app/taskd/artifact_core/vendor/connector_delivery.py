"""Called by the existing connector with its already-loaded portal connection.

Never loads credentials from a new location, prints them, starts task execution,
or turns a requester device into a provider. HTTPS uses normal certificate checks.
"""
import http.client,json,os,pathlib,ssl,threading,urllib.parse
from repair_protocol import *
from repair_sender import Sender,KnownFailure,UnknownOutcome,NotFound
PART = 24 * 1024  # Existing portal binary request bound; no server import needed.

class HTTPSWire:
    def __init__(self,base,existing_node_credential):
        u=urllib.parse.urlsplit(base)
        if u.scheme!='https' or not u.hostname or u.username or u.password or u.path not in ('','/') or u.query or u.fragment:raise ValueError('existing HTTPS portal origin required')
        self.connection=http.client.HTTPSConnection(u.hostname,u.port or 443,timeout=15,context=ssl.create_default_context())
        self.credential=existing_node_credential;self.lock=threading.Lock()
    def close(self):self.connection.close()
    def request(self,method,path,body=None,sha=None,binary=False):
        data=body if isinstance(body,bytes) else canonical(body) if body is not None else None
        if data is not None and len(data)>(PART if isinstance(body,bytes) else 32768):raise KnownFailure('request exceeds existing proxy limit')
        headers={'Authorization':'Bearer '+self.credential,'Content-Type':'application/octet-stream' if isinstance(body,bytes) else 'application/json'}
        if sha:headers['X-Content-SHA256']=sha
        with self.lock:
            try:
                self.connection.request(method,path,body=data,headers=headers);response=self.connection.getresponse()
                limit=PART if binary else MAX_MANIFEST;raw=response.read(limit+1)
                if response.status==404:raise NotFound('delivery not found')
                if response.status>=500:raise UnknownOutcome('portal response outcome unknown')
                if response.status!=200:raise KnownFailure('portal rejected request HTTP '+str(response.status))
                if len(raw)>limit:raise KnownFailure('portal response exceeds bound')
                if binary:return raw,response.getheader('X-Content-SHA256','')
                try:return json.loads(raw)
                except (ValueError,UnicodeError):raise UnknownOutcome('invalid portal acknowledgement') from None
            except (OSError,http.client.HTTPException):
                self.connection.close();raise UnknownOutcome('portal transport outcome unknown') from None
    def json(self,value):return self.request('POST','/api/node/artifacts',value)

class PortalTransport:
    channel='portal_https_storage'
    def __init__(self,wire):self.wire=wire
    def progress(self,mid,fi,ci,data):
        p=self.wire.json({'op':'part_status','delivery_id':mid,'file':fi,'chunk':ci})
        return self.check_progress(p,mid,fi,ci,data)
    def check_progress(self,p,mid,fi,ci,data):
        if p.get('manifest_sha256')!=mid or p.get('file')!=fi or p.get('chunk')!=ci:raise KnownFailure('fragment acknowledgement identity mismatch')
        if p.get('chunk_verified'):
            if p.get('sha256')!=digest(data):raise KnownFailure('stored block hash mismatch')
            return p
        parts=p.get('parts')
        if not isinstance(parts,list) or len(parts)>(len(data)+PART-1)//PART:raise KnownFailure('invalid fragment progress')
        seen=set()
        for part in parts:
            i=part.get('index')
            if type(i)is not int or i in seen or not 0<=i<(len(data)+PART-1)//PART or part.get('sha256')!=digest(data[i*PART:(i+1)*PART]):raise KnownFailure('fragment content differs')
            seen.add(i)
        return p
    def call(self,method,path,body=None):
        parts=path.strip('/').split('/')
        if method=='GET' and len(parts)==2:return self.wire.json({'op':'status','delivery_id':parts[1]})
        if method=='POST' and parts==['deliveries']:return self.wire.json({'op':'begin','manifest':body})
        if method=='POST' and len(parts)==3 and parts[2]=='commit':return self.wire.json({'op':'commit','delivery_id':parts[1]})
        if method=='PUT' and len(parts)==6:
            mid,fi,ci=parts[1],int(parts[3]),int(parts[5]);data=body
            state=self.progress(mid,fi,ci,data)
            for pi in range((len(data)+PART-1)//PART):
                if state['chunk_verified']:break
                if pi in {p['index'] for p in state['parts']}:continue
                fragment=data[pi*PART:(pi+1)*PART]
                try:
                    acknowledgement=self.wire.request('POST',f'/api/node/artifacts/part/{mid}/{fi}/{ci}/{pi}',fragment,digest(fragment))
                except UnknownOutcome:
                    # An uncertain write is checked under the same immutable ID,
                    # never replayed as a new operation or business task.
                    state=self.progress(mid,fi,ci,data)
                    if not state['chunk_verified'] and pi not in {p['index'] for p in state['parts']}:raise
                else:
                    state=self.check_progress(acknowledgement,mid,fi,ci,data)
                    if not state['chunk_verified'] and pi not in {p['index'] for p in state['parts']}:raise UnknownOutcome('fragment not acknowledged')
            if not state['chunk_verified']:raise UnknownOutcome('block commit not observed')
            return {'manifest_sha256':mid,'file':fi,'chunk':ci,'sha256':digest(data),'state':'verified'}
        raise KnownFailure('unsupported portal transfer operation')

class PortalSender(Sender):
    def __init__(self,plan,ledger,transport):
        if len(canonical({'op':'begin','manifest':plan['manifest']}))>32768:raise KnownFailure('manifest exceeds existing portal request cap; no upload attempted')
        super().__init__(plan,ledger,transport)
    def validated_receipt(self,receipt):
        expected=[{k:f[k] for k in ('name','size','sha256')} for f in self.plan['manifest']['files']]
        if not isinstance(receipt,dict) or receipt.get('schema')!=VERSION or receipt.get('task_id')!=ORIGINAL_TASK or receipt.get('manifest_sha256')!=self.mid or receipt.get('files')!=expected or receipt.get('server_files_verified') is not True:
            raise KnownFailure('portal storage receipt mismatches manifest')
        if receipt.get('channel')!='portal_https_storage' or receipt.get('signature_verification')!='not_anet_receipt' or receipt.get('anet.receipt_verified') is not None or receipt.get('recipient_files_verified') is not False:raise KnownFailure('portal claimed unsupported receipt scope')
        return receipt
    def completion_fields(self,receipt):return {'state':'server_bytes_verified','receipt':receipt,'server_files_verified':True,'recipient_files_verified':False}

def receive_from_portal(wire,mid,destination):
    from owned_receiver import receive_from_portal as receive_owned
    return receive_owned(wire,mid,destination)


def run_repair_cli(args,root):
    """Early connector action: dry mode never loads connection credentials."""
    path=plain_path(args.repair_plan);plan=read_json(path)
    result=preflight(plan,args.repair_manifest,args.repair_version)
    if not args.apply:print(json.dumps(result,indent=2));return
    if not args.repair_manifest or not args.repair_version:raise ValueError('apply needs exact manifest hash and taskd version')
    ledger=plain_path(args.repair_ledger or path.with_name(plan['manifest_sha256']+'.ledger.json'))
    forbidden=[pathlib.Path(plan['source']['task']).parent,pathlib.Path(plan['source']['log']).parent,pathlib.Path(plan['source']['artifacts'])]
    if any(ledger.resolve().is_relative_to(p.resolve()) for p in forbidden):raise ValueError('delivery ledger cannot modify original task/artifact tree')
    # The connector normally reads this existing authorized binding. Nothing is
    # printed, copied to another node, created, or changed in this connection.
    state=read_json(plain_path(pathlib.Path(root)/'portal/connection.json'))
    if state.get('aid')!=plan['manifest']['provider']:raise ValueError('existing portal node differs from original provider')
    wire=HTTPSWire(state['portal'],state['node_token'])
    try:
        result=PortalSender(plan,ledger,PortalTransport(wire)).apply(args.repair_manifest,args.repair_version)
        print(json.dumps({k:result.get(k) for k in ('state','task_id','manifest_sha256','network','server_files_verified','recipient_files_verified','anet.receipt_verified','requester_acceptance','execution_modified')},indent=2))
    finally:wire.close()
