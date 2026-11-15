"""Fixed-channel per-user updater. TLS and byte integrity are independent gates.

No identity/config/peer/payment files are modified by this module. A staged
installer is never executed before a complete length and SHA-256 comparison.
"""
import argparse,hashlib,json,os,re,shutil,subprocess,sys,time,urllib.parse,urllib.request,uuid
from contextlib import contextmanager
from pathlib import Path

ORIGIN='https://124.222.173.36:8888'
ENDPOINT=ORIGIN+'/api/desktop-release'
SCHEMA='anet-desktop-update/1'
MAX_BYTES=1100000000
DOWNLOAD_RESERVE_BYTES=64*1024*1024

class DownloadSpaceRequired(OSError):pass

def download_budget(data,info):
 """Only a partial pinned to the exact manifest reduces remaining bytes."""
 target=ordinary(data/'updates'/('ANetSetup-'+info['version']+'.exe'))
 partial=ordinary(target.with_name(target.name+'.partial'));metadata=ordinary(target.with_name(target.name+'.partial.json'))
 n=0
 if target.exists():verify_stage(target,info);n=info['bytes']
 elif partial.exists() and metadata.exists():
  previous=read(metadata)
  if previous.get('release')==info and partial.stat().st_size<=info['bytes']:n=partial.stat().st_size
 required=info['bytes']-n+DOWNLOAD_RESERVE_BYTES
 free=shutil.disk_usage(data).free
 return {'required_free_bytes':required,'free_bytes':free,'downloaded_bytes':n,'total_bytes':info['bytes']}

def require_download_space(data,remaining):
 if shutil.disk_usage(data).free<remaining+DOWNLOAD_RESERVE_BYTES:raise DownloadSpaceRequired('insufficient_update_download_space; existing progress retained')

def read(p):return json.loads(Path(p).read_text(encoding='utf-8'))
def digest(p):
 h=hashlib.sha256()
 with open(p,'rb') as f:
  while b:=f.read(1048576):h.update(b)
 return h.hexdigest()
def plain(p):
 p=Path(os.path.abspath(p))
 for v in (p,*p.parents):
  if v.exists() and (v.is_symlink() or getattr(v.lstat(),'st_file_attributes',0)&0x400):raise ValueError('reparse_path_refused')
 return p
def atomic(p,data):
 p=plain(p);p.parent.mkdir(parents=True,exist_ok=True);temp=p.with_name('.'+p.name+'-'+uuid.uuid4().hex)
 with temp.open('x',encoding='utf-8') as f:json.dump(data,f,ensure_ascii=False);f.flush();os.fsync(f.fileno())
 os.replace(temp,p)
def version_key(v):
 m=re.fullmatch(r'(\d+)\.(\d+)\.(\d+)(?:-preview\.(\d+))?',v or '')
 if not m:raise ValueError('unsupported_release_version')
 a,b,c,pre=m.groups();return (int(a),int(b),int(c),1 if pre is None else 0,int(pre or 0))
def same_origin(url):
 p=urllib.parse.urlsplit(url);base=urllib.parse.urlsplit(ORIGIN)
 if (p.scheme,p.netloc)!=(base.scheme,base.netloc) or p.username or p.password or p.fragment or p.query:raise ValueError('update_origin_refused')
 return url
class FixedRedirect(urllib.request.HTTPRedirectHandler):
 def redirect_request(self,req,fp,code,msg,headers,newurl):
  same_origin(newurl);return super().redirect_request(req,fp,code,msg,headers,newurl)
def network(url,offset=0,etag=None):
 same_origin(url)
 headers={'User-Agent':'ANetDesktop-Updater/1','Accept-Encoding':'identity'}
 if offset:headers['Range']='bytes='+str(offset)+'-'
 if offset and etag:headers['If-Range']=etag
 return urllib.request.build_opener(FixedRedirect()).open(urllib.request.Request(url,headers=headers),timeout=45)
def manifest(data,current):
 if data.get('schema')!='anet-desktop-release/1' or data.get('channel')!='preview':raise ValueError('update_channel_refused')
 if data.get('available') is False:return None
 v=data.get('version');key=version_key(v);ckey=version_key(current)
 if key<=ckey:return None
 if data.get('minimum_version') and ckey<version_key(data['minimum_version']):raise ValueError('minimum_version_not_met')
 row=(data.get('windows') or {}).get('amd64') or data
 url=urllib.parse.urljoin(ORIGIN+'/',row.get('url') or row.get('download_url') or data.get('download_url') or '')
 same_origin(url)
 if urllib.parse.urlsplit(url).path!='/downloads/ANetSetup-'+v+'.exe':raise ValueError('update_artifact_path_refused')
 n=row.get('bytes');sha=row.get('sha256')
 if type(n) is not int or not 1024<=n<=MAX_BYTES or not re.fullmatch('[0-9a-f]{64}',sha or ''):raise ValueError('update_integrity_required')
 signature=data.get('signature',{}).get('status',row.get('authenticode','unsigned'))
 if signature not in ('unsigned','verified'):raise ValueError('update_signature_state')
 return {'version':v,'url':url,'bytes':n,'sha256':sha,'channel':'preview','signature':signature}
def runtime(path):
 p=plain(path);r=read(p)
 if r.get('schema')!='anet-desktop-runtime/1':raise ValueError('runtime_schema')
 receipt=read(p.parent/'receipt.json')
 if receipt.get('schema')!='anet-desktop-installer/1' or receipt.get('runtime')!=r:raise ValueError('runtime_receipt_mismatch')
 data=plain(r['persistent_root']);current=read(data/'current.json')
 return r,data,current
@contextmanager
def update_lock(data,wait_seconds=0):
 folder=plain(data/'updates');folder.mkdir(exist_ok=True)
 with open(plain(folder/'operation.lock'),'a+b') as f:
  if os.fstat(f.fileno()).st_nlink!=1:raise ValueError('nonordinary_update_lock')
  f.seek(0);f.write(b'0');f.flush();until=time.monotonic()+wait_seconds
  while True:
   try:
    f.seek(0)
    if os.name=='nt':
     import msvcrt;msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)
    else:
     import fcntl;fcntl.flock(f.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
    break
   except OSError:
    if time.monotonic()>=until:raise RuntimeError('update_operation_already_running')
    time.sleep(.05)
  yield
def staged(data,current):
 marker=data/'updates/staged.json'
 if not marker.exists():return None
 record=read(marker);info=record['release']
 if version_key(info['version'])<=version_key(current['release']):return None
 checked=manifest({'schema':'anet-desktop-release/1','available':True,'channel':info['channel'],'version':info['version'],'windows':{'amd64':{'url':info['url'],'bytes':info['bytes'],'sha256':info['sha256'],'authenticode':info['signature']}},'signature':{'status':info['signature']}},current['release'])
 target=plain(record['path'])
 if target.parent!=plain(data/'updates') or target.name!='ANetSetup-'+info['version']+'.exe':raise ValueError('staged_path_refused')
 verify_stage(target,checked)
 return {'schema':SCHEMA,'state':'staged','available':True,'current_version':current['release'],'version':info['version'],'release':checked,'reason':'更新已下载并核验，可离线安装。'}
def check(path):
 r,data,current=runtime(path)
 ready=staged(data,current)
 if ready:return ready
 active=download_status(data,current)
 if active:return active
 with network(ENDPOINT) as response:
  raw=response.read(65537)
  if len(raw)>65536:raise ValueError('release_manifest_too_large')
  published=json.loads(raw);release=manifest(published,current['release'])
 if published.get('available') is False:return {'schema':SCHEMA,'state':'unavailable','available':False,'current_version':current['release'],'version':current['release'],'reason':'更新通道暂未发布可安装的版本。'}
 result={'schema':SCHEMA,'state':'available' if release else 'current','available':bool(release),'current_version':current['release'],'version':release['version'] if release else current['release'],'reason':'有新版本可安装。' if release else '已经是当前版本。'}
 if release:result['release']=release
 return result
def verify_stage(p,info):
 p=plain(p)
 if not p.is_file() or p.stat().st_nlink!=1 or p.stat().st_size!=info['bytes'] or digest(p)!=info['sha256']:raise ValueError('staged_installer_integrity_failed')
 if info['signature']=='verified':
  # SHA authenticates delivery from the channel; validate actual Authenticode
  # separately if the publisher declares the release signed.
  ps="(Get-AuthenticodeSignature -LiteralPath '"+str(p).replace("'","''")+"').Status.ToString()"
  out=subprocess.run(['powershell.exe','-NoLogo','-NoProfile','-NonInteractive','-Command',ps],env={k:v for k,v in os.environ.items() if k.lower()!='psmodulepath'},capture_output=True,timeout=30,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
  if out.returncode or out.stdout.decode().strip()!='Valid':raise ValueError('authenticode_validation_failed')
def checked_release(info,current):
 return manifest({'schema':'anet-desktop-release/1','available':True,'channel':info['channel'],'version':info['version'],'windows':{'amd64':{'url':info['url'],'bytes':info['bytes'],'sha256':info['sha256'],'authenticode':info['signature']}},'signature':{'status':info['signature']}},current['release'])

def ordinary(path):
 path=plain(path)
 if path.exists() and (not path.is_file() or path.stat().st_nlink!=1):raise ValueError('nonordinary_update_file')
 return path

def process_token(pid):
 """A process-object proof prevents treating a recycled PID as the worker."""
 import ctypes
 from ctypes import wintypes as W
 k=ctypes.WinDLL('kernel32',use_last_error=True);k.OpenProcess.argtypes=[W.DWORD,W.BOOL,W.DWORD];k.OpenProcess.restype=W.HANDLE;k.CloseHandle.argtypes=[W.HANDLE]
 k.GetProcessTimes.argtypes=[W.HANDLE,ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p];k.GetProcessTimes.restype=W.BOOL
 k.QueryFullProcessImageNameW.argtypes=[W.HANDLE,W.DWORD,W.LPWSTR,ctypes.POINTER(W.DWORD)];k.QueryFullProcessImageNameW.restype=W.BOOL
 k.WaitForSingleObject.argtypes=[W.HANDLE,W.DWORD];k.WaitForSingleObject.restype=W.DWORD
 handle=k.OpenProcess(0x101000,False,pid)
 if not handle:
  code=ctypes.get_last_error()
  if code in (87,1168):return None
  raise ctypes.WinError(code)
 try:
  state=k.WaitForSingleObject(handle,0)
  if state==0:return None
  if state==0xffffffff:raise ctypes.WinError(ctypes.get_last_error())
  if state!=0x102:raise RuntimeError('download_worker_liveness_unconfirmed')
  created=ctypes.c_uint64();a=ctypes.c_uint64();b=ctypes.c_uint64();c=ctypes.c_uint64()
  if not k.GetProcessTimes(handle,ctypes.byref(created),ctypes.byref(a),ctypes.byref(b),ctypes.byref(c)):
   code=ctypes.get_last_error()
   if k.WaitForSingleObject(handle,0)==0:return None
   raise ctypes.WinError(code)
  text=ctypes.create_unicode_buffer(32768);size=W.DWORD(len(text))
  if not k.QueryFullProcessImageNameW(handle,0,text,ctypes.byref(size)):
   code=ctypes.get_last_error()
   if k.WaitForSingleObject(handle,0)==0:return None
   raise ctypes.WinError(code)
  return {'pid':pid,'creation_filetime':created.value,'exe':os.path.normcase(os.path.realpath(text.value))}
 finally:k.CloseHandle(handle)

def download_status(data,current):
 marker=plain(data/'updates/download.json')
 if not marker.exists():return None
 rec=read(ordinary(marker));info=checked_release(rec['release'],current)
 if info is None:return None
 state=rec.get('state')
 if state not in ('downloading','paused','needs_attention'):return None
 if state=='downloading':
  try:live=process_token(rec['worker']['pid'])
  except OSError:return {'schema':SCHEMA,'state':'needs_attention','available':True,'current_version':current['release'],'version':info['version'],'release':info,'worker_unconfirmed':True,'reason':'已有下载工作者的退出状态尚未确认，没有重复启动。'}
  else:state='downloading' if live==rec.get('worker') else 'paused'
 partial=ordinary(data/'updates'/('ANetSetup-'+info['version']+'.exe.partial'));size=partial.stat().st_size if partial.exists() else 0
 return {'schema':SCHEMA,'state':state,'available':True,'current_version':current['release'],'version':info['version'],'release':info,'downloaded_bytes':min(size,info['bytes']),'total_bytes':info['bytes'],'reason':'更新正在后台下载，可正常关闭客户端。' if state=='downloading' else '下载已暂停，已保留进度，下次启动可以继续。'}

def launch_download(argv,log):
 flags=getattr(subprocess,'DETACHED_PROCESS',0)|getattr(subprocess,'CREATE_NEW_PROCESS_GROUP',0)
 return subprocess.Popen(argv,stdin=subprocess.DEVNULL,stdout=log,stderr=log,close_fds=True,creationflags=flags)

def request_download(path):
 result=check(path)
 if not result['available'] or result['state'] in ('staged','downloading') or result.get('worker_unconfirmed'):return result
 r,data,current=runtime(path)
 with update_lock(data,wait_seconds=5):
  ready=staged(data,current)
  if ready:return ready
  active=download_status(data,current)
  if active and (active['state']=='downloading' or active.get('worker_unconfirmed')):return active
  info=checked_release(result['release'],current)
  if not info:raise ValueError('update_no_longer_newer')
  budget=download_budget(data,info)
  if budget['free_bytes']<budget['required_free_bytes']:
   return {'schema':SCHEMA,'state':'needs_space','available':True,'current_version':current['release'],'version':info['version'],'release':info,**budget,'reason':'磁盘空间不足，更新尚未开始下载。已下载的部分会保留，释放空间后可继续。'}
  operation=uuid.uuid4().hex;folder=plain(data/'updates')
  argv=[sys.executable,'-B','-X','utf8',str(Path(__file__).resolve()),'fetch-update','--runtime',str(path),'--operation',operation]
  with ordinary(folder/'download.log').open('ab') as log:worker=launch_download(argv,log)
  token=process_token(worker.pid)
  if not token:raise RuntimeError('download_worker_start_not_confirmed')
  atomic(folder/'download.json',{'schema':SCHEMA,'state':'downloading','release':info,'operation':operation,'worker':token,'at':time.time()})
  return {'schema':SCHEMA,'state':'downloading','available':True,'current_version':current['release'],'version':info['version'],'release':info,'downloaded_bytes':0,'total_bytes':info['bytes'],'reason':'更新正在后台下载，可正常关闭客户端。'}

def fetch_update(path,operation):
 r,data,current=runtime(path)
 with update_lock(data,wait_seconds=10):
  marker=ordinary(data/'updates/download.json');rec=read(marker)
  if rec.get('operation')!=operation or rec.get('state')!='downloading' or process_token(os.getpid())!=rec.get('worker'):raise ValueError('download_worker_ownership_mismatch')
  info=checked_release(rec['release'],current)
  if not info:raise ValueError('update_no_longer_newer')
  try:
   result=download_release(path,info);atomic(marker,dict(rec,state='staged',completed_at=time.time()));return result
  except Exception as error:
   atomic(marker,dict(rec,state='needs_attention' if isinstance(error,ValueError) else 'paused',error_type=type(error).__name__,at=time.time()));raise

def download(path):
 r,data,current=runtime(path)
 with update_lock(data):
  result=check(path)
  if not result['available'] or result['state']=='staged':return result
  return download_release(path,result['release'])

def download_release(path,info):
 r,data,current=runtime(path);info=checked_release(info,current)
 if not info:raise ValueError('update_no_longer_newer')
 folder=plain(data/'updates');folder.mkdir(exist_ok=True);target=ordinary(folder/('ANetSetup-'+info['version']+'.exe'));state=folder/'staged.json'
 if target.exists():verify_stage(target,info)
 else:
  temp=ordinary(target.with_name(target.name+'.partial'));meta=ordinary(target.with_name(target.name+'.partial.json'));previous=read(meta) if meta.exists() else None
  budget=download_budget(data,info);require_download_space(data,info['bytes']-budget['downloaded_bytes'])
  if not previous or previous.get('release')!=info:
   temp.unlink(missing_ok=True);meta.unlink(missing_ok=True);previous={'schema':SCHEMA,'release':info};atomic(meta,previous)
  n=temp.stat().st_size if temp.exists() else 0
  if n>info['bytes']:temp.unlink();n=0
  try:
   if n<info['bytes']:
    source=network(info['url'],n,previous.get('etag')) if n else network(info['url'])
    with source:
     headers=getattr(source,'headers',{});status=getattr(source,'status',200)
     if headers.get('Content-Encoding','identity').lower()!='identity':raise ValueError('download_encoding_refused')
     if status==206:
      match=re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)',headers.get('Content-Range',''))
      if not match or tuple(map(int,match.groups()))!=(n,info['bytes']-1,info['bytes']):raise ValueError('download_range_mismatch')
      if previous.get('etag') and headers.get('ETag')!=previous['etag']:raise ValueError('download_etag_changed')
     elif status==200:n=0
     else:raise ValueError('download_http_status_refused')
     length=headers.get('Content-Length')
     if length is not None and (not str(length).isdigit() or int(length)!=info['bytes']-n):raise ValueError('download_response_length_mismatch')
     etag=headers.get('ETag');etag=etag if etag and not etag.startswith('W/') else None;atomic(meta,dict(previous,etag=etag))
     require_download_space(data,info['bytes']-n);next_space_check=n+4*1024*1024
     with temp.open('ab' if n else 'wb') as dest:
      while b:=source.read(65536):
       n+=len(b)
       if n>info['bytes']:raise ValueError('download_length_exceeded')
       if n>=next_space_check:
        require_download_space(data,info['bytes']-n+len(b));next_space_check=n+4*1024*1024
       dest.write(b);dest.flush()
      os.fsync(dest.fileno())
   if n<info['bytes']:raise OSError('download_interrupted')
   if digest(temp)!=info['sha256']:raise ValueError('download_integrity_failed')
   verify_stage(temp,info);os.replace(temp,target);meta.unlink(missing_ok=True)
  except ValueError:
   temp.unlink(missing_ok=True);meta.unlink(missing_ok=True);raise
 atomic(state,{'schema':SCHEMA,'release':info,'path':str(target),'current_version':current['release'],'at':time.time()})
 return {'schema':SCHEMA,'state':'staged','available':True,'current_version':current['release'],'version':info['version'],'release':info,'reason':'更新已下载并完成核验，将在退出客户端后安装。'}

def schedule(path,wait_pid=None,restart=False):
 r,data,current=runtime(path)
 with update_lock(data):return _schedule(path,wait_pid,restart)
def _schedule(path,wait_pid=None,restart=False):
 r,data,current=runtime(path);state=read(data/'updates/staged.json');info=state['release'];installer=plain(state['path'])
 if installer.parent!=plain(data/'updates') or installer.name!='ANetSetup-'+info['version']+'.exe':raise ValueError('staged_path_refused')
 if version_key(info['version'])<=version_key(current['release']):raise ValueError('update_downgrade_refused')
 verify_stage(installer,info)
 marker=data/'updates/apply.json'
 # A live scheduled worker retains the operation. Never execute a second copy.
 if marker.exists():
  old=read(marker)
  if old.get('state') in ('scheduled','needs_attention') and old.get('version')==info['version']:
   sys.path.insert(0,str(Path(__file__).resolve().parent));from install_engine import process_matches
   if old.get('state')=='scheduled' and process_matches(old.get('pid'),sys.executable):return {'schema':SCHEMA,**old,'available':True,'current_version':current['release']}
   # A lost worker acknowledgement does not establish that its installer did
   # not start. Hold the same operation for inspection; never execute again.
   return {'schema':SCHEMA,'state':'needs_attention','available':True,'version':info['version'],'current_version':current['release'],'reason':'上次更新进程已结束，安装结果待核对；没有重复执行安装。'}
 argv=[sys.executable,'-B','-X','utf8',str(Path(__file__).resolve()),'finish-update','--runtime',str(path)]
 if wait_pid:argv+=['--wait-pid',str(wait_pid)]
 if restart:argv+=['--restart']
 log=open(data/'updates/apply.log','ab')
 p=subprocess.Popen(argv,stdin=subprocess.DEVNULL,stdout=log,stderr=log,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0));log.close()
 rec={'state':'scheduled','version':info['version'],'pid':p.pid,'at':time.time(),'reason':'退出客户端后安装已核验的更新。'};atomic(marker,rec)
 return {'schema':SCHEMA,**rec,'available':True,'current_version':current['release']}
def wait_exit(pid):
 if not pid:return
 import ctypes
 from ctypes import wintypes
 k=ctypes.WinDLL('kernel32',use_last_error=True)
 k.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD];k.OpenProcess.restype=wintypes.HANDLE
 k.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD];k.CloseHandle.argtypes=[wintypes.HANDLE]
 handle=k.OpenProcess(0x100000,False,pid)
 if not handle:
  code=ctypes.get_last_error()
  if code in (87,1168):return
  raise ctypes.WinError(code)
 try:
  state=k.WaitForSingleObject(handle,600000)
  if state==0xFFFFFFFF:raise ctypes.WinError(ctypes.get_last_error())
  if state!=0:raise RuntimeError('client_exit_not_confirmed')
 finally:k.CloseHandle(handle)
def finish(path,wait_pid=None,restart=False):
 r,data,current=runtime(path)
 with update_lock(data,wait_seconds=5):return _finish(path,wait_pid,restart)
def _finish(path,wait_pid=None,restart=False):
 r,data,before=runtime(path);state=read(data/'updates/staged.json');info=state['release'];marker=data/'updates/apply.json'
 try:
  wait_exit(wait_pid);verify_stage(Path(state['path']),info)
  current=read(data/'current.json')
  if version_key(info['version'])<=version_key(current['release']):raise ValueError('update_downgrade_refused')
  argv=[state['path'],'--install','--start-services']
  if before.get('fixture'):
   fixture=plain(before['fixture_root'])
   if fixture/'Data/ANetDesktop'!=data:raise ValueError('fixture_update_root_mismatch')
   argv+=['--fixture-root',str(fixture)]
  proc=subprocess.run(argv,stdin=subprocess.DEVNULL,capture_output=True,timeout=600,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
  after=read(data/'current.json')
  if proc.returncode or after.get('release')!=info['version']:raise RuntimeError('installer_activation_not_confirmed')
  atomic(marker,{'state':'applied','version':info['version'],'at':time.time(),'prior_version':before['release']})
  if restart:subprocess.Popen([str(Path(after['frontend_root'])/'ANetDesktop.exe'),'--runtime',str(Path(after['frontend_root'])/'install/runtime.json')],close_fds=True)
  return {'schema':SCHEMA,'state':'applied','version':info['version']}
 except Exception as e:
  # Installer's activation commit restores the previous pointer on failure.
  # Do not resend or forcibly kill an operation with an unknown outcome.
  atomic(marker,{'state':'needs_attention','version':info['version'],'reason':type(e).__name__+':'+str(e)[:120],'at':time.time()})
  after=read(data/'current.json')
  if restart and after.get('release')==before.get('release') and after.get('frontend_root')==before.get('frontend_root') and after.get('frontend_root'):
   subprocess.Popen([str(Path(after['frontend_root'])/'ANetDesktop.exe'),'--runtime',str(Path(after['frontend_root'])/'install/runtime.json')],close_fds=True)
  raise
def main():
 p=argparse.ArgumentParser();p.add_argument('action',choices=['check-update','download-update','fetch-update','apply-update','finish-update']);p.add_argument('--runtime',required=True);p.add_argument('--operation');p.add_argument('--wait-pid',type=int);p.add_argument('--restart',action='store_true');a=p.parse_args()
 try:
  value=check(a.runtime) if a.action=='check-update' else request_download(a.runtime) if a.action=='download-update' else fetch_update(a.runtime,a.operation) if a.action=='fetch-update' else schedule(a.runtime,a.wait_pid,a.restart) if a.action=='apply-update' else finish(a.runtime,a.wait_pid,a.restart)
  print(json.dumps(value,ensure_ascii=False));return 0
 except Exception as e:
  print(json.dumps({'schema':SCHEMA,'state':'needs_attention','available':False,'reason':type(e).__name__+':'+str(e)[:150]},ensure_ascii=False));return 1
if __name__=='__main__':raise SystemExit(main())
