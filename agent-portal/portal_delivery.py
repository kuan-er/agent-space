"""Portal artifact transport, scoped by the EXISTING authenticated node and task.

Import after current node authentication. Never creates credentials or changes task
admission. Stored bytes are private server plaintext; this is HTTPS, not ANet E2EE.
"""
import os,pathlib,shutil,threading
from repair_protocol import *
from repair_receiver import Receiver

PART=24*1024
DDL='''CREATE TABLE IF NOT EXISTS artifact_deliveries(
 id TEXT PRIMARY KEY,task_id TEXT NOT NULL,provider_agent TEXT NOT NULL,
 requester_agent TEXT NOT NULL,manifest TEXT NOT NULL,created_at TEXT NOT NULL,
 storage_state TEXT NOT NULL,recipient_state TEXT NOT NULL,recipient_receipt TEXT)'''

class Denied(ValueError):pass

class MutationNotApplied(ValueError):
    """Emitted only at reviewed pre-mutation branches, never a generic denial."""
    def __init__(self,operation,mid,reason):
        self.operation,self.mid,self.reason=operation,mid,reason
        super().__init__('artifact_mutation_not_applied')

class PortalDeliveries:
    def __init__(self,root):
        self.root=plain_path(root);self.receiver=Receiver(self.root,set(),receipt_channel='portal_https_storage');self.lock=threading.RLock()
    def close(self):self.receiver.close()
    @staticmethod
    def initialize(c):c.execute(DDL)
    @staticmethod
    def node(c,node):
        # `node` is already obtained from Handler.node(c), never from request JSON.
        row=c.execute('SELECT a.id,a.aid,a.owner_id,a.kind,a.enabled,u.enabled owner_enabled FROM agents a JOIN users u ON u.id=a.owner_id WHERE a.id=?',(node['id'],)).fetchone()
        if not row or row['aid']!=node['aid'] or not row['enabled'] or not row['owner_enabled']:raise Denied('node authorization unavailable')
        return dict(row)
    def begin(self,c,node,manifest):
        with self.lock:
            mid=validate_manifest(manifest);provider=self.node(c,node)
            if provider['kind']!='agent' or manifest['provider']!=provider['aid']:raise Denied('provider mismatch')
            original=c.execute('SELECT role,peer,state FROM interactions WHERE agent_id=? AND ix=?',(provider['id'],manifest['task_id'])).fetchone()
            evidence=c.execute('SELECT evidence FROM interaction_evidence WHERE agent_id=? AND ix=?',(provider['id'],manifest['task_id'])).fetchone()
            if original and (original['role']!='inbound' or original['peer']!=manifest['requester']):raise Denied('original task/peer differs')
            recipient=c.execute('SELECT a.id,a.aid,a.owner_id,a.enabled,u.enabled owner_enabled FROM agents a JOIN users u ON u.id=a.owner_id WHERE a.aid=?',(manifest['requester'],)).fetchone()
            if not recipient or not recipient['enabled'] or not recipient['owner_enabled'] or recipient['owner_id']!=provider['owner_id']:
                raise Denied('recipient unavailable or explicit cross-owner authorization required')
            # Only the authenticated provider and an enabled same-owner recipient
            # can observe this pending result. No association, receiver directory,
            # fragment or receipt has been created at this branch.
            if not original or original['state']!='completed' or not evidence or json.loads(evidence['evidence']).get('anet.receipt_verified')!='verified':
                raise MutationNotApplied('begin',mid,'prerequisite_pending')
            prior=c.execute('SELECT * FROM artifact_deliveries WHERE id=?',(mid,)).fetchone()
            if prior:
                if prior['provider_agent']!=provider['id'] or prior['requester_agent']!=recipient['id'] or json.loads(prior['manifest'])!=manifest:raise Denied('delivery association mismatch')
            else:
                reserved=0
                if c.execute('SELECT COUNT(*) FROM artifact_deliveries WHERE provider_agent=?',(provider['id'],)).fetchone()[0]>=32:raise Denied('provider manifest count limit reached')
                for row in c.execute('SELECT manifest FROM artifact_deliveries'):
                    reserved+=sum(f['size'] for f in json.loads(row['manifest'])['files'])
                size=sum(f['size'] for f in manifest['files'])
                if reserved+size>MAX_TOTAL:raise Denied('provider artifact quota exceeded')
                if shutil.disk_usage(self.root).free<size*2+100*1024*1024:raise Denied('insufficient storage headroom')
                c.execute('INSERT INTO artifact_deliveries VALUES(?,?,?,?,?,?,?,?,?)',(mid,manifest['task_id'],provider['id'],recipient['id'],canonical(manifest).decode(),now(),'receiving','not_verified',None))
                c.commit() # Association is durable before any upload acknowledgement.
            self.receiver.parties.add((manifest['task_id'],manifest['provider'],manifest['requester']))
            self.receiver.begin(manifest);return self.status(c,node,mid)
    def association(self,c,node,mid,side=None):
        if not isinstance(mid,str) or not HEX.fullmatch(mid):raise Denied('invalid delivery id')
        authenticated=self.node(c,node);row=c.execute('SELECT * FROM artifact_deliveries WHERE id=?',(mid,)).fetchone()
        if not row:raise FileNotFoundError('delivery not found')
        if authenticated['id'] not in (row['provider_agent'],row['requester_agent']):raise Denied('delivery not available to node')
        if side and authenticated['id']!=row[side+'_agent']:raise Denied('delivery operation wrong party')
        # Recheck both current owners/enabled bindings; revoke access immediately.
        m=json.loads(row['manifest']);provider=c.execute('SELECT id,aid,owner_id,enabled FROM agents WHERE id=?',(row['provider_agent'],)).fetchone();recipient=c.execute('SELECT id,aid,owner_id,enabled FROM agents WHERE id=?',(row['requester_agent'],)).fetchone()
        if not provider or not recipient or not provider['enabled'] or not recipient['enabled'] or provider['aid']!=m['provider'] or recipient['aid']!=m['requester'] or provider['owner_id']!=recipient['owner_id']:
            raise Denied('delivery participant binding changed')
        if validate_manifest(m)!=mid:raise ValueError('stored delivery manifest corrupt')
        self.receiver.parties.add((m['task_id'],m['provider'],m['requester']));return dict(row),m
    def status(self,c,node,mid):
        with self.lock:
            row,m=self.association(c,node,mid);state=self.receiver.status(mid)
            if state['receipt']:
                state['receipt']=self.storage_receipt(m,mid)
            return {**state,'recipient_state':row['recipient_state'],'recipient_receipt':json.loads(row['recipient_receipt']) if row['recipient_receipt'] else None,'signature_verification':'not_anet_receipt','manifest':m}
    def storage_receipt(self,m,mid):
        return {'schema':VERSION,'task_id':m['task_id'],'manifest_sha256':mid,
                'files':[{k:f[k] for k in ('name','size','sha256')} for f in m['files']],
                'server_files_verified':True,'recipient_files_verified':False,'channel':'portal_https_storage',
                'signature_verification':'not_anet_receipt','anet.receipt_verified':None,'requester_acceptance':'not_verified'}
    def fragment_directory(self,mid,fi,ci):return plain_path(self.receiver.directory(mid)/'parts'/str(fi)/str(ci))
    def part_status(self,c,node,mid,fi,ci):
        with self.lock:
            _,m=self.association(c,node,mid,'provider');sha,size=self.receiver.expected_chunk(m,fi,ci)
            # This is a positive authenticated storage contract, not a claim
            # that an earlier request did not run. upload_part serializes this
            # immutable key and accepts an existing fragment only byte-for-byte.
            replay={'schema':'immutable-part-replay/1','manifest_sha256':mid,
                'file':fi,'chunk':ci,'chunk_sha256':sha,'chunk_size':size,
                'part_bytes':PART,'semantics':'same_key_same_bytes_only',
                'scope':'authenticated_storage_only_no_business_execution'}
            ready=self.receiver.chunk_path(mid,fi,ci)
            if ready.exists():
                if ready.stat().st_size!=size or sha_file(ready)!=sha:raise ValueError('stored chunk corrupt')
                return {'manifest_sha256':mid,'file':fi,'chunk':ci,'chunk_verified':True,'sha256':sha,'parts':[],'immutable_replay':replay}
            present=[];directory=self.fragment_directory(mid,fi,ci)
            for pi in range((size+PART-1)//PART):
                path=directory/str(pi)
                if path.exists():
                    info=path.lstat()
                    if not stat.S_ISREG(info.st_mode) or getattr(info,'st_file_attributes',0)&0x400 or info.st_size!=min(PART,size-pi*PART):raise ValueError('stored part size/type mismatch')
                    present.append({'index':pi,'sha256':sha_file(path)})
            return {'manifest_sha256':mid,'file':fi,'chunk':ci,'chunk_verified':False,'parts':present,'immutable_replay':replay}
    def upload_part(self,c,node,mid,fi,ci,pi,data,expected_sha):
        with self.lock:
            _,m=self.association(c,node,mid,'provider');sha,size=self.receiver.expected_chunk(m,fi,ci)
            if type(pi)is not int or not 0<=pi<(size+PART-1)//PART or len(data)!=min(PART,size-pi*PART) or digest(data)!=expected_sha:raise ValueError('invalid fragment')
            ready=self.receiver.chunk_path(mid,fi,ci)
            if ready.exists():return self.part_status(c,node,mid,fi,ci)
            directory=self.fragment_directory(mid,fi,ci);directory.mkdir(parents=True,exist_ok=True);target=plain_path(directory/str(pi))
            if target.exists():
                if target.read_bytes()!=data:raise ValueError('conflicting immutable fragment')
            else:
                temporary=target.with_name(target.name+'.tmp-'+uuid.uuid4().hex)
                try:
                    with temporary.open('xb') as out:out.write(data);out.flush();os.fsync(out.fileno())
                    os.replace(temporary,target)
                finally:
                    if temporary.exists():temporary.unlink()
            progress=self.part_status(c,node,mid,fi,ci)
            if len(progress['parts'])==(size+PART-1)//PART:
                assembled=b''.join((directory/str(i)).read_bytes() for i in range((size+PART-1)//PART))
                self.receiver.put(mid,fi,ci,assembled)
                # Remove only exact fragments owned by this completed chunk. This
                # never touches source/user files; interruption leaves harmless parts.
                for i in range((size+PART-1)//PART):plain_path(directory/str(i)).unlink()
                return self.part_status(c,node,mid,fi,ci)
            return progress
    def commit(self,c,node,mid):
        with self.lock:
            _,m=self.association(c,node,mid,'provider');self.receiver.commit(mid)
            c.execute("UPDATE artifact_deliveries SET storage_state='server_stored' WHERE id=?",(mid,));c.commit()
            return self.storage_receipt(m,mid)
    def list_for_recipient(self,c,node):
        authenticated=self.node(c,node)
        return [{'id':r['id'],'task_id':r['task_id'],'storage_state':r['storage_state'],'recipient_state':r['recipient_state']} for r in c.execute('SELECT id,task_id,storage_state,recipient_state FROM artifact_deliveries WHERE requester_agent=? ORDER BY created_at LIMIT 100',(authenticated['id'],))]
    def download_part(self,c,node,mid,fi,offset):
        with self.lock:
            row,m=self.association(c,node,mid,'requester')
            if row['storage_state']!='server_stored' or type(fi)is not int or not 0<=fi<len(m['files']):raise Denied('artifact not committed')
            f=m['files'][fi]
            if type(offset)is not int or offset<0 or offset>=f['size'] or offset%PART:raise ValueError('invalid read offset')
            path=source_path(self.receiver.directory(mid)/'files'/str(fi),f['name'])
            # Final full-file verification is mandatory at requester; each response
            # additionally carries a digest of the exact bounded bytes served.
            with path.open('rb') as inp:inp.seek(offset);data=inp.read(min(PART,f['size']-offset))
            if len(data)!=min(PART,f['size']-offset):raise ValueError('stored file truncated')
            return data,digest(data)
    def received(self,c,node,mid,files):
        with self.lock:
            try:row,m=self.association(c,node,mid,'requester')
            except Denied:
                if isinstance(mid,str) and HEX.fullmatch(mid):
                    raise MutationNotApplied('received',mid,'authorization_denied') from None
                raise
            if row['storage_state']!='server_stored' or files!=[{k:f[k] for k in ('name','size','sha256')} for f in m['files']]:raise ValueError('recipient report differs from frozen manifest')
            if row['recipient_receipt']:
                existing=json.loads(row['recipient_receipt'])
                if existing.get('manifest_sha256')!=mid or existing.get('files')!=files or existing.get('reporting_agent')!=node['id']:raise ValueError('conflicting recipient receipt')
                return existing
            receipt={'manifest_sha256':mid,'reporting_agent':node['id'],'files':files,'at':now(),'verification':'reported_by_authenticated_requester','anet.receipt_verified':None}
            c.execute("UPDATE artifact_deliveries SET recipient_state='reported_verified',recipient_receipt=? WHERE id=?",(canonical(receipt).decode(),mid));c.commit();return receipt
