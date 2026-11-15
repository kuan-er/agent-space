"""Resumable sender; queries the same delivery after every ambiguous response."""
import http.client,json,pathlib,urllib.error,urllib.parse,urllib.request
from repair_protocol import *

class KnownFailure(RuntimeError):pass
class UnknownOutcome(RuntimeError):pass
class NotFound(KnownFailure):pass

class LoopbackTransport:
    channel='loopback_test_only'
    def __init__(self,url):
        parsed=urllib.parse.urlsplit(url)
        if parsed.scheme!='http' or parsed.hostname!='127.0.0.1' or not parsed.port or parsed.username or parsed.password or parsed.path not in ('','/') or parsed.query or parsed.fragment:
            raise ValueError('only explicit loopback test transport is implemented')
        self.url=url.rstrip('/');self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def call(self,method,path,body=None):
        raw=body if isinstance(body,bytes) else canonical(body) if body is not None else None
        request=urllib.request.Request(self.url+path,data=raw,method=method,headers={'Content-Type':'application/octet-stream' if isinstance(body,bytes) else 'application/json'})
        try:
            with self.opener.open(request,timeout=15) as response:
                raw=response.read(MAX_MANIFEST+1)
                if len(raw)>MAX_MANIFEST:raise KnownFailure('oversized response')
                return json.loads(raw)
        except urllib.error.HTTPError as e:
            if e.code==404:raise NotFound('delivery not found') from None
            raise KnownFailure('receiver rejected operation HTTP '+str(e.code)) from None
        except (urllib.error.URLError,http.client.HTTPException,TimeoutError,OSError):
            raise UnknownOutcome('transport outcome unknown; reconcile the same delivery') from None

class Sender:
    def __init__(self,plan,ledger,transport):
        self.plan=plan;self.ledger=plain_path(ledger);self.transport=transport
        self.mid=validate_manifest(plan['manifest']);self.state=None
    def save(self,event,**fields):
        self.state['events'].append({'at':now(),'event':event,**fields})
        self.state['events']=self.state['events'][-4096:]
        self.state['updated_at']=now();atomic_json(self.ledger,self.state)
    def query(self):
        result=self.transport.call('GET','/deliveries/'+self.mid)
        if result.get('manifest_sha256')!=self.mid:raise KnownFailure('receiver delivery identity mismatch')
        present=result.get('present');files=self.plan['manifest']['files']
        if not isinstance(present,list) or len(present)!=len(files):raise KnownFailure('invalid receiver progress')
        for indices,f in zip(present,files):
            if not isinstance(indices,list) or any(type(i)is not int or not 0<=i<len(f['chunks']) for i in indices) or len(indices)!=len(set(indices)):
                raise KnownFailure('invalid receiver chunk acknowledgement')
        return result
    def validated_receipt(self,receipt):
        expected=[{k:f[k] for k in ('name','size','sha256')} for f in self.plan['manifest']['files']]
        if not isinstance(receipt,dict) or receipt.get('schema')!=VERSION or receipt.get('task_id')!=ORIGINAL_TASK or receipt.get('manifest_sha256')!=self.mid or receipt.get('files')!=expected or receipt.get('receiver_files_verified') is not True:
            raise KnownFailure('receipt does not cover the exact frozen manifest')
        if receipt.get('channel')!='loopback_test_only' or receipt.get('signature_verification')!='not_available_loopback_test' or receipt.get('anet.receipt_verified') is not None or receipt.get('requester_acceptance')!='not_verified':
            raise KnownFailure('unsupported or misleading signature/acceptance claim')
        return receipt
    def completion_fields(self,receipt):return {'state':'receiver_bytes_verified','receipt':receipt,'receiver_files_verified':True}
    def apply(self,expected_manifest,expected_version):
        preflight(self.plan,expected_manifest,expected_version)
        with Lock(self.ledger.with_suffix('.lock')):
            if self.ledger.exists():
                self.state=read_json(self.ledger)
                if self.state.get('schema')!=VERSION or self.state.get('manifest_sha256')!=self.mid:raise KnownFailure('ledger belongs to another frozen delivery')
            else:
                self.state={'schema':VERSION,'task_id':ORIGINAL_TASK,'manifest_sha256':self.mid,'state':'prepared','events':[],
                            'execution_modified':False,'original_receipt_modified':False,'network':self.transport.channel,'anet.receipt_verified':None,'requester_acceptance':'not_verified'}
            self.save('apply_started')
            try:
                try:remote=self.query();self.save('receiver_state_queried')
                except NotFound:
                    self.save('begin_intent')
                    try:self.transport.call('POST','/deliveries',self.plan['manifest'])
                    except UnknownOutcome:self.save('begin_response_unknown')
                    # Query even after a normal ACK; begin ACK alone is not byte receipt.
                    remote=self.query();self.save('begin_reconciled')
                if remote.get('receipt'):
                    receipt=self.validated_receipt(remote['receipt']);self.save('existing_receipt_verified')
                else:
                    self.state['state']='transferring'
                    for fi,f in enumerate(self.plan['manifest']['files']):
                        present=set(remote['present'][fi]);path=source_path(self.plan['source']['artifacts'],f['name'])
                        with path.open('rb') as source:
                            for ci,sha in enumerate(f['chunks']):
                                if ci in present:continue
                                source.seek(ci*CHUNK);data=source.read(min(CHUNK,f['size']-ci*CHUNK))
                                if digest(data)!=sha:raise KnownFailure('source changed before transmission')
                                self.save('chunk_intent',file=fi,chunk=ci,sha256=sha)
                                try:
                                    ack=self.transport.call('PUT',f'/deliveries/{self.mid}/files/{fi}/chunks/{ci}',data)
                                    if ack.get('manifest_sha256')!=self.mid or ack.get('file')!=fi or ack.get('chunk')!=ci or ack.get('sha256')!=sha or ack.get('state') not in ('verified','already_verified'):
                                        raise UnknownOutcome('unrecognized chunk acknowledgement')
                                except UnknownOutcome:
                                    self.save('chunk_response_unknown',file=fi,chunk=ci)
                                    remote=self.query();self.save('chunk_status_queried',file=fi,chunk=ci)
                                    if ci not in remote['present'][fi]:raise UnknownOutcome('receiver reports missing chunk; safe explicit resume required')
                                self.save('chunk_received_hash_verified',file=fi,chunk=ci)
                    self.save('commit_intent')
                    try:receipt=self.transport.call('POST',f'/deliveries/{self.mid}/commit',{})
                    except UnknownOutcome:
                        self.save('commit_response_unknown');remote=self.query();self.save('commit_status_queried');receipt=remote.get('receipt')
                        if not receipt:raise UnknownOutcome('commit unconfirmed; explicit resume required')
                    receipt=self.validated_receipt(receipt)
                preflight(self.plan,expected_manifest,expected_version)
                self.state.update(self.completion_fields(receipt))
                self.save('receiver_bytes_verified_original_execution_unchanged')
                return self.state
            except BaseException as e:
                self.state['state']='unconfirmed' if isinstance(e,(UnknownOutcome,KeyboardInterrupt,SystemExit)) else 'failed'
                self.save('apply_stopped',error_type=type(e).__name__)
                raise
