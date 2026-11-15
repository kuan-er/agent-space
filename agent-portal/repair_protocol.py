"""Delivery-only v1. No task execution, ANet mutation, credential or session API."""
import hashlib,json,os,pathlib,re,stat,time,uuid

VERSION='delivery-repair/1'
ORIGINAL_TASK='ix_fe18ee600e2b1f0271718bb00e13fb49'
CHUNK=512*1024
MAX_TOTAL=2*1024*1024*1024
MAX_MANIFEST=1024*1024
HEX=re.compile(r'[0-9a-f]{64}\Z')
TASK_ID=re.compile(r'ix_[0-9a-f]{16,64}\Z')
NAME=re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z')

def canonical(value):return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode('utf-8')
def digest(data):return hashlib.sha256(data).hexdigest()
def read_json(path):return json.loads(pathlib.Path(path).read_text(encoding='utf-8'))
def sha_file(path):
    h=hashlib.sha256()
    with pathlib.Path(path).open('rb') as f:
        while data:=f.read(CHUNK):h.update(data)
    return h.hexdigest()
def now():return time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())

def plain_path(path):
    p=pathlib.Path(path).absolute()
    for item in reversed((p,*p.parents)):
        if not item.exists():continue
        s=item.lstat()
        if stat.S_ISLNK(s.st_mode) or getattr(s,'st_file_attributes',0)&0x400:
            raise ValueError('symlink/reparse path refused')
    return p

def validate_name(name):
    if not isinstance(name,str) or not NAME.fullmatch(name) or name.endswith('.') or name.upper().split('.')[0] in {'CON','PRN','AUX','NUL',*(f'COM{i}' for i in range(1,10)),*(f'LPT{i}' for i in range(1,10))}:
        raise ValueError('unsafe file name')
    return name
def source_path(root,name):
    validate_name(name)
    p=plain_path(plain_path(root)/name)
    if p.parent!=pathlib.Path(root).absolute():raise ValueError('source outside root')
    return p

def atomic_json(path,value):
    p=plain_path(path);p.parent.mkdir(parents=True,exist_ok=True)
    temporary=p.with_name(p.name+'.tmp-'+uuid.uuid4().hex)
    try:
        with temporary.open('xb') as f:f.write(canonical(value));f.flush();os.fsync(f.fileno())
        os.replace(temporary,p)
    finally:
        if temporary.exists():temporary.unlink()

class Lock:
    def __init__(self,path):
        self.path=plain_path(path);self.path.parent.mkdir(parents=True,exist_ok=True)
        self.file=self.path.open('a+b')
        try:
            if os.fstat(self.file.fileno()).st_size==0:self.file.write(b'0');self.file.flush()
            self.file.seek(0)
            if os.name=='nt':
                import msvcrt;msvcrt.locking(self.file.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl;fcntl.flock(self.file,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BaseException:self.file.close();raise ValueError('delivery directory already in use')
    def close(self):
        if self.file.closed:return
        self.file.seek(0)
        if os.name=='nt':
            import msvcrt;msvcrt.locking(self.file.fileno(),msvcrt.LK_UNLCK,1)
        else:
            import fcntl;fcntl.flock(self.file,fcntl.LOCK_UN)
        self.file.close()
    def __enter__(self):return self
    def __exit__(self,*args):self.close()

def describe(path):
    path=plain_path(path);h=hashlib.sha256();chunks=[]
    with path.open('rb') as f:
        before=os.fstat(f.fileno())
        if not stat.S_ISREG(before.st_mode) or not 0<before.st_size<=MAX_TOTAL:raise ValueError('file type/size unsupported')
        while data:=f.read(CHUNK):h.update(data);chunks.append(digest(data))
        after=os.fstat(f.fileno())
    current=path.stat()
    identity=lambda s:(s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns)
    if identity(before)!=identity(after) or identity(after)!=identity(current):raise ValueError('file changed while hashing')
    return {'name':path.name,'size':before.st_size,'sha256':h.hexdigest(),'chunks':chunks}

def validate_manifest(m):
    if not isinstance(m,dict) or m.get('schema')!=VERSION or not isinstance(m.get('task_id'),str) or not TASK_ID.fullmatch(m['task_id']) or m.get('execution_state')!='completed' or m.get('chunk_bytes')!=CHUNK:
        raise ValueError('manifest schema/task/state mismatch')
    if len(canonical(m))>MAX_MANIFEST:raise ValueError('manifest too large')
    if not all(isinstance(m.get(k),str) and 1<=len(m[k])<=200 for k in ('provider','requester','taskd_version')):raise ValueError('manifest party/version missing')
    if not isinstance(m.get('files'),list) or not 1<=len(m['files'])<=64:raise ValueError('invalid manifest file count')
    total=0;names=set()
    for f in m['files']:
        if not isinstance(f,dict):raise ValueError('invalid descriptor')
        name=f.get('name');validate_name(name)
        if name.casefold() in names:raise ValueError('duplicate manifest file name')
        names.add(name.casefold());size=f.get('size');chunks=f.get('chunks')
        if type(size)is not int or not 0<size<=MAX_TOTAL or not HEX.fullmatch(str(f.get('sha256',''))):raise ValueError('invalid size/hash')
        if not isinstance(chunks,list) or len(chunks)!=(size+CHUNK-1)//CHUNK or any(not isinstance(h,str) or not HEX.fullmatch(h) for h in chunks):raise ValueError('invalid chunk hashes')
        total+=size
    if total>MAX_TOTAL:raise ValueError('delivery exceeds explicit 2 GiB candidate limit')
    return digest(canonical(m))

def freeze(task_path,log_path,status_path,artifact_root,names,provider,requester):
    paths={k:str(plain_path(v)) for k,v in [('task',task_path),('log',log_path),('status',status_path),('artifacts',artifact_root)]}
    t=read_json(paths['task']);s=read_json(paths['status'])
    if t.get('id')!=ORIGINAL_TASK or t.get('state')!='completed':raise ValueError('only original completed task may be repaired')
    manifest={'schema':VERSION,'task_id':ORIGINAL_TASK,'execution_state':'completed','provider':provider,'requester':requester,'taskd_version':s.get('version'),'chunk_bytes':CHUNK,
              'files':[describe(source_path(paths['artifacts'],n)) for n in names]}
    mid=validate_manifest(manifest)
    plan={'schema':VERSION,'manifest':manifest,'manifest_sha256':mid,'source':paths,'protected':{k:sha_file(paths[k]) for k in ('task','log')}}
    preflight(plan);return plan

def preflight(plan,expected_manifest=None,expected_version=None):
    mid=validate_manifest(plan['manifest'])
    if plan['manifest']['task_id']!=ORIGINAL_TASK:raise ValueError('this repair CLI is locked to the reviewed original task')
    if plan.get('schema')!=VERSION or mid!=plan.get('manifest_sha256') or expected_manifest is not None and mid!=expected_manifest:raise ValueError('frozen manifest digest mismatch')
    source=plan['source'];t=read_json(plain_path(source['task']));s=read_json(plain_path(source['status']))
    if t.get('id')!=ORIGINAL_TASK or t.get('state')!='completed':raise ValueError('execution is not original completed task')
    if s.get('version')!=plan['manifest']['taskd_version'] or expected_version is not None and s.get('version')!=expected_version:raise ValueError('taskd version changed')
    for key in ('task','log'):
        if sha_file(plain_path(source[key]))!=plan['protected'][key]:raise ValueError('protected execution record changed')
    for f in plan['manifest']['files']:
        if describe(source_path(source['artifacts'],f['name']))!=f:raise ValueError('frozen artifact changed')
    return {'schema':VERSION,'task_id':ORIGINAL_TASK,'manifest_sha256':mid,'state':'preflight_passed','files':len(plan['manifest']['files']),'bytes':sum(f['size'] for f in plan['manifest']['files']),'execution_modified':False,'network_sent':False}
