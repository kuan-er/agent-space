"""Explicit local WorkBuddy MCP connection; no OAuth, trust or auto-approval edit.

The ANet native wire runs only in an isolated private profile. Its generated
entry is checked, merged with user-owned fields and committed once to WorkBuddy.
Only stdio initialization and tools/list are used by the optional verifier.
"""
from contextlib import contextmanager
import copy,ctypes,hashlib,json,os,re,shutil,subprocess,sys,time,uuid
from pathlib import Path
from .network import plain,private_folder,new_private_fd,private_existing
from .transport import RpcProcess

SCHEMA='anet-bootstrap/1'
LIMIT=1024*1024
DOCS='https://www.codebuddy.cn/docs/workbuddy/From-Beginner-to-Expert-Guide/Function-Description/MCP-Guide'

class ConnectionError(RuntimeError):pass
class Conflict(ConnectionError):pass

def digest(raw):return hashlib.sha256(raw).hexdigest()
def file_sha(path):
 h=hashlib.sha256()
 with Path(path).open('rb') as stream:
  while block:=stream.read(1048576):h.update(block)
 return h.hexdigest()
def pairs(items):
 out={}
 for key,value in items:
  if key in out:raise Conflict('duplicate_configuration_key')
  out[key]=value
 return out
def parse(raw):
 if raw is None:return {}
 if len(raw)>LIMIT:raise Conflict('configuration_too_large')
 try:value=json.loads(raw.decode('utf-8-sig'),object_pairs_hook=pairs)
 except (ValueError,UnicodeError) as error:raise Conflict('configuration_requires_manual_merge') from error
 if not isinstance(value,dict):raise Conflict('configuration_must_be_object')
 if 'mcpServers' in value and not isinstance(value['mcpServers'],dict):raise Conflict('mcp_servers_must_be_object')
 return value
def read_bytes(path):
 path=plain(path)
 if not path.exists():return None
 if not path.is_file() or path.stat().st_nlink!=1 or path.stat().st_size>LIMIT:raise Conflict('nonordinary_configuration_file')
 return path.read_bytes()
def private_raw(path,raw):
 path=Path(path);private_folder(path.parent)
 if len(raw)>LIMIT:raise ConnectionError('bounded_private_file_required')
 fd=new_private_fd(path)
 try:
  with os.fdopen(fd,'wb') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
 except BaseException:
  path.unlink(missing_ok=True);raise
def private_json(path,value):
 private_raw(path,json.dumps(value,ensure_ascii=False,indent=2).encode('utf-8'))
def norm(path):return os.path.normcase(os.path.abspath(path))
def windows_security(path):
 from ctypes import wintypes as W
 adv=ctypes.WinDLL('advapi32',use_last_error=True);get=adv.GetFileSecurityW
 get.argtypes=[W.LPCWSTR,W.DWORD,ctypes.c_void_p,W.DWORD,ctypes.POINTER(W.DWORD)];get.restype=W.BOOL
 size=W.DWORD();get(str(path),5,None,0,ctypes.byref(size))
 if not size.value:raise ctypes.WinError(ctypes.get_last_error())
 buffer=ctypes.create_string_buffer(size.value)
 if not get(str(path),5,buffer,size.value,ctypes.byref(size)):raise ctypes.WinError(ctypes.get_last_error())
 control=W.WORD();revision=W.DWORD();get_control=adv.GetSecurityDescriptorControl;get_control.argtypes=[ctypes.c_void_p,ctypes.POINTER(W.WORD),ctypes.POINTER(W.DWORD)]
 if not get_control(buffer,ctypes.byref(control),ctypes.byref(revision)):raise ctypes.WinError(ctypes.get_last_error())
 return bytes(buffer),bool(control.value&0x1000)
def set_windows_security(path,descriptor,protected,*,owner=False):
 from ctypes import wintypes as W
 adv=ctypes.WinDLL('advapi32',use_last_error=True);set_security=adv.SetFileSecurityW;set_security.argtypes=[W.LPCWSTR,W.DWORD,ctypes.c_void_p];set_security.restype=W.BOOL
 flags=4|(1 if owner else 0)|(0x80000000 if protected else 0x20000000);buffer=ctypes.create_string_buffer(descriptor)
 if not set_security(str(path),flags,buffer):raise ctypes.WinError(ctypes.get_last_error())
def native_replace(target,temporary):
 if os.name=='nt' and target.exists():
  from ctypes import wintypes as W
  k=ctypes.WinDLL('kernel32',use_last_error=True);f=k.ReplaceFileW
  f.argtypes=[W.LPCWSTR,W.LPCWSTR,W.LPCWSTR,W.DWORD,ctypes.c_void_p,ctypes.c_void_p];f.restype=W.BOOL
  descriptor,protected=windows_security(target)
  # Copy the old owner/access rules before publishing. Keeping this staging
  # copy protected prevents the staging directory adding broader inherited
  # ACEs. No privilege is enabled to bypass an owner/security refusal.
  set_windows_security(temporary,descriptor,True,owner=True)
  # ReplaceFile preserves other file metadata; merge failures are not ignored.
  if not f(str(target),str(temporary),None,0,None,None):raise ctypes.WinError(ctypes.get_last_error())
  set_windows_security(target,descriptor,protected)
 else:os.replace(temporary,target)

def native_wire(binary,node,stage,raw):
 """Reuse the installed native wire without giving it the real user profile."""
 home=stage/'profile';private_folder(home);folder=home/'.workbuddy';private_folder(folder)
 target=folder/'mcp.json'
 if raw is not None:private_raw(target,json.dumps(parse(raw),ensure_ascii=False,indent=2).encode('utf-8'))
 env=dict(os.environ,USERPROFILE=str(home),ANET_DATA_DIR=str(node));env.pop('ANET_ID',None)
 proc=subprocess.run([str(binary),'agents','wire','workbuddy'],env=env,cwd=str(home),stdin=subprocess.DEVNULL,capture_output=True,timeout=30,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
 if proc.returncode:raise ConnectionError('native_workbuddy_wire_rejected')
 for path in home.rglob('*'):
  if path.is_file() and path!=target and not (path.parent==folder and re.fullmatch(r'mcp\.json\.anet-bak-[0-9-]+',path.name)):
   raise ConnectionError('native_wire_unexpected_file')
 result=parse(target.read_bytes());entry=(result.get('mcpServers') or {}).get('anet')
 expected={'command':str(binary),'args':['mcp'],'env':{'ANET_DATA_DIR':str(node)}}
 if entry!=expected:raise ConnectionError('native_wire_identity_mismatch')
 return entry

def native_probe(binary,node):
 env=dict(os.environ,ANET_DATA_DIR=str(node));env.pop('ANET_ID',None)
 rpc=RpcProcess([str(binary),'mcp'],str(node),env)
 try:
  result=rpc.request('initialize',{'protocolVersion':'2025-03-26','capabilities':{},'clientInfo':{'name':'ANetDesktop-WorkBuddy-Check','version':'1'}},timeout=15)
  if not isinstance(result,dict) or not str(result.get('protocolVersion','')) or (result.get('serverInfo') or {}).get('name')!='anet':raise ConnectionError('mcp_initialize_unverified')
  rpc.send({'jsonrpc':'2.0','method':'notifications/initialized'})
  tools=rpc.request('tools/list',{},timeout=15)
  names={row.get('name') for row in tools.get('tools',[]) if isinstance(row,dict)}
  required={'guide','node_status','send_message','wait_task'}
  if not required.issubset(names):raise ConnectionError('anet_mcp_tools_missing')
  return {'mcp_handshake':'verified','protocol_version':result['protocolVersion'],'server_name':'anet','server_version':result.get('serverInfo',{}).get('version'),'tool_count':len(names),'business_tools_called':0,'model_requests':0,'workbuddy_client':'unverified','native_use':'unverified'}
 finally:rpc.close()

def native_client():
 """Presence only. Neither a folder nor a registry entry proves login/use."""
 found=[]
 local=Path(os.environ.get('LOCALAPPDATA',str(Path.home()/'AppData/Local')))
 for p in (local/'Programs/WorkBuddy/WorkBuddy.exe',local/'WorkBuddy/WorkBuddy.exe',Path(os.environ.get('ProgramFiles',r'C:\Program Files'))/'WorkBuddy/WorkBuddy.exe'):
  if p.is_file():found.append(str(p))
 executable=shutil.which('WorkBuddy.exe')
 if executable and Path(executable).is_file():found.append(executable)
 if os.name=='nt':
  import winreg
  for hive in (winreg.HKEY_CURRENT_USER,winreg.HKEY_LOCAL_MACHINE):
   for sub in (r'Software\Microsoft\Windows\CurrentVersion\Uninstall',r'Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall'):
    try:
     with winreg.OpenKey(hive,sub) as root:
      for i in range(winreg.QueryInfoKey(root)[0]):
       try:
        with winreg.OpenKey(root,winreg.EnumKey(root,i)) as key:
         name=winreg.QueryValueEx(key,'DisplayName')[0]
         if not isinstance(name,str) or not re.fullmatch(r'WorkBuddy(?:\s+[0-9.]+)?',name,re.I):continue
         location=winreg.QueryValueEx(key,'InstallLocation')[0]
         for filename in ('WorkBuddy.exe','workbuddy.exe'):
          p=Path(location)/filename
          if p.is_file():found.append(str(p))
       except (OSError,TypeError,ValueError):continue
    except OSError:continue
 return {'presence':'detected' if found else 'not_observed','path':found[0] if found else None,'login':'unverified','use':'unverified'}

class Manager:
 def __init__(self,runtime,*,home=None,wire=None,probe=None,detect=None):
  if runtime.get('schema')!='anet-desktop-runtime/1':raise ConnectionError('runtime_schema_invalid')
  self.node=plain(runtime.get('anet_root') or Path(runtime['node_root']).parent)
  if not self.node.is_dir():raise ConnectionError('selected_anet_node_unavailable')
  candidate=runtime.get('anet_exe')
  if not candidate and (self.node/'bin/anet.exe').is_file():candidate=str(self.node/'bin/anet.exe')
  if not candidate:
   cfg=parse(read_bytes(self.node/'taskd/config.json'));candidate=(cfg.get('transport') or {}).get('binary')
  if not candidate:raise ConnectionError('selected_anet_binary_unavailable')
  self.binary=plain(candidate)
  if not self.binary.is_file() or self.binary.suffix.lower()!='.exe':raise ConnectionError('native_anet_executable_required')
  self.binary_sha=file_sha(self.binary);self.home=plain(home or Path.home());self.folder=self.home/'.workbuddy';self.config=self.folder/'mcp.json';self.private=self.folder/'.anet-desktop-mcp'
  self.wire=wire or native_wire;self.probe=probe or native_probe;self.detect=detect or native_client
 def projection(self,entry):
  if not isinstance(entry,dict) or entry.get('args')!=['mcp'] or entry.get('type','stdio')!='stdio':raise Conflict('same_named_mcp_server_is_foreign')
  env=entry.get('env')
  if not isinstance(env,dict) or not isinstance(env.get('ANET_DATA_DIR'),str) or norm(env['ANET_DATA_DIR'])!=norm(self.node):raise Conflict('existing_mcp_uses_different_anet_identity')
  if env.get('ANET_ID') not in (None,'','default'):raise Conflict('existing_mcp_overrides_anet_identity')
  command=entry.get('command')
  if not isinstance(command,str) or not Path(command).is_absolute():raise Conflict('existing_mcp_command_unverified')
  return {'command':command,'args':['mcp'],'node_root':str(self.node)}
 def receipt(self):
  path=self.private/'receipt.json'
  if not path.exists():return None
  private_existing(self.private,True);private_existing(path)
  return parse(read_bytes(path))
 def owned(self,entry):
  projection=self.projection(entry)
  if norm(projection['command'])==norm(self.binary):return True
  receipt=self.receipt()
  if receipt and receipt.get('schema')=='anet-workbuddy-mcp-receipt/1' and receipt.get('projection')==projection:return True
  raise Conflict('existing_mcp_command_requires_review')
 def view(self,state,*,configured=False,reason='',verification=None,**extra):
  client=self.detect();proof={'configuration':'matched' if configured else 'missing','mcp_handshake':'not_run','workbuddy_client':'unverified','native_use':'unverified'};proof.update(verification or {})
  return {'schema':SCHEMA,'harness':'workbuddy','integration':'local_mcp','state':state,'configured':configured,'reason':reason,'config_path':str(self.config),'requires_workbuddy_reload':configured,'client':client,'verification':proof,'docs':DOCS,'trust_changed':False,'auto_approve_changed':False,'business_tasks_sent':0,**extra}
 def status(self):
  try:
   raw=read_bytes(self.config);root=parse(raw);entry=(root.get('mcpServers') or {}).get('anet')
   if entry is None:return self.view('not_configured',reason='尚未把 ANet 工具连接到本机 WorkBuddy。')
   self.owned(entry);matched=norm(entry['command'])==norm(self.binary)
   if not matched:return self.view('not_configured',reason='已保留旧的 ANet 配置，可更新到当前客户端。')
   proof=None;receipt=self.receipt()
   if receipt and receipt.get('configuration_sha256')==digest(raw) and receipt.get('binary_sha256')==self.binary_sha:proof=receipt.get('verification')
   disabled=entry.get('disabled') is True or entry.get('enabled') is False
   state='already_configured' if self.detect()['presence']=='detected' else 'needs_install'
   return self.view(state,configured=True,verification=proof,disabled=disabled,reason='连接配置已准备。请在 WorkBuddy 重载 MCP，并确认 ANet 状态为绿色。' if not disabled else '连接配置已保留；此 MCP 在 WorkBuddy 中仍处于停用状态，未替你启用。')
  except Conflict as error:return self.view('conflict',reason=str(error),verification={'configuration':'conflict'})
 @contextmanager
 def locked(self):
  if not self.folder.exists():self.folder.mkdir()
  plain(self.folder);private_folder(self.private)
  path=self.private/'operation.lock'
  if not path.exists():
   try:private_raw(path,b'0')
   except FileExistsError:pass
  private_existing(path)
  with path.open('r+b') as lock:
   if os.fstat(lock.fileno()).st_nlink!=1:raise Conflict('nonordinary_connection_lock')
   if os.name=='nt':
    import msvcrt
    try:msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
    except OSError:raise Conflict('another_workbuddy_connection_is_running') from None
   else:
    import fcntl
    try:fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
    except OSError:raise Conflict('another_workbuddy_connection_is_running') from None
   yield
 def export(self):
  path=self.private/('ANet-WorkBuddy-import-'+uuid.uuid4().hex[:8]+'.json')
  private_json(path,{'mcpServers':{'anet':{'command':str(self.binary),'args':['mcp'],'env':{'ANET_DATA_DIR':str(self.node)}}}})
  return str(path)
 def verify(self):
  status=self.status()
  if not status['configured']:return status
  before=read_bytes(self.config)
  try:proof=self.probe(self.binary,self.node)
  except Exception as error:proof={'mcp_handshake':'failed','probe_error_type':type(error).__name__,'workbuddy_client':'unverified','native_use':'unverified','business_tools_called':0,'model_requests':0}
  with self.locked():
   raw=read_bytes(self.config)
   if raw!=before:raise Conflict('configuration_changed_during_verification')
   if file_sha(self.binary)!=self.binary_sha:raise Conflict('native_binary_changed_during_verification')
   root=parse(raw);entry=root['mcpServers']['anet'];self.owned(entry)
   value=self.receipt() or {};value.update({'schema':'anet-workbuddy-mcp-receipt/1','projection':self.projection(entry),'configuration_sha256':digest(raw),'binary_sha256':file_sha(self.binary),'verification':proof,'at':time.time()})
   self.save_receipt(value)
  reason='本地 ANet MCP 握手及工具列表已核验。请在 WorkBuddy 重载并确认绿色状态；尚未验证 WorkBuddy 实际调用。' if proof.get('mcp_handshake')=='verified' else '配置仍已保留，本地 MCP 握手尚未通过；请先准备或启动 ANet 客户端后重新检查。'
  return self.view(status['state'],configured=True,verification=proof,reason=reason)
 def save_receipt(self,value):
  path=self.private/'receipt.json';temporary=self.private/('.receipt-'+uuid.uuid4().hex[:12]);private_json(temporary,value);os.replace(temporary,path)
 def connect(self):
  with self.locked():
   before=read_bytes(self.config)
   try:
    root=parse(before);entry=(root.get('mcpServers') or {}).get('anet')
    if entry is not None:self.owned(entry)
   except Conflict as error:return self.view('conflict',reason=str(error),verification={'configuration':'conflict'},import_config_path=self.export())
   if entry is not None and norm(entry['command'])==norm(self.binary):return self.status()
   stage=self.private/('stage-'+uuid.uuid4().hex);private_folder(stage)
   try:
    generated=self.wire(self.binary,self.node,stage,before)
    expected={'command':str(self.binary),'args':['mcp'],'env':{'ANET_DATA_DIR':str(self.node)}}
    if generated!=expected or file_sha(self.binary)!=self.binary_sha:raise ConnectionError('native_wire_identity_mismatch')
    merged=copy.deepcopy(root);servers=merged.setdefault('mcpServers',{});replacement=copy.deepcopy(entry or {})
    replacement.update(command=generated['command'],args=generated['args']);replacement.setdefault('env',{}).update(generated['env']);servers['anet']=replacement
    # Only the selected entry changes. Unknown values, secrets, disabled flags
    # and autoApprove preferences remain exact JSON values.
    if {k:v for k,v in servers.items() if k!='anet'}!={k:v for k,v in (root.get('mcpServers') or {}).items() if k!='anet'}:raise ConnectionError('unrelated_mcp_changed')
    after=json.dumps(merged,ensure_ascii=False,indent=2).encode('utf-8')+b'\n'
    backup=None
    if before is not None:
     backup=self.private/('mcp-original-'+uuid.uuid4().hex[:12]+'.json');private_raw(backup,before)
    if read_bytes(self.config)!=before:raise Conflict('configuration_changed_during_connection')
    temporary=self.private/('.config-'+uuid.uuid4().hex[:12]);private_raw(temporary,after)
    try:native_replace(self.config,temporary)
    finally:temporary.unlink(missing_ok=True)
    if read_bytes(self.config)!=after:raise ConnectionError('configuration_commit_unconfirmed')
    self.save_receipt({'schema':'anet-workbuddy-mcp-receipt/1','projection':self.projection(replacement),'configuration_sha256':digest(after),'binary_sha256':self.binary_sha,'backup_path':str(backup) if backup else None,'native_wire_used':True,'at':time.time()})
   finally:
    if stage.exists() and stage.resolve().parent==self.private.resolve() and stage.name.startswith('stage-'):shutil.rmtree(stage)
  result=self.status();result['state']='configured' if result['client']['presence']=='detected' else 'needs_install';result['native_wire_used']=True
  if result['client']['presence']=='not_observed':result['reason']='本机连接配置已准备；尚未检测到 WorkBuddy。安装或启动后，在其 MCP 设置重载 ANet。'
  return result

def load_runtime(path):
 raw=read_bytes(plain(path));value=parse(raw)
 if value.get('schema')!='anet-desktop-runtime/1':raise ConnectionError('runtime_schema_invalid')
 return value
def main():
 import argparse
 p=argparse.ArgumentParser();p.add_argument('--runtime',required=True);a=p.parse_args()
 try:
  raw=sys.stdin.buffer.readline(65537)
  if len(raw)>65536:raise ConnectionError('bounded_action_required')
  request=json.loads(raw);action=request.get('action')
  if set(request)-{'action'} or action not in ('status','connect','verify'):raise ConnectionError('finite_workbuddy_action_required')
  manager=Manager(load_runtime(a.runtime));value=getattr(manager,action)();print(json.dumps(value,ensure_ascii=False));return 0
 except (Conflict,ConnectionError) as error:
  print(json.dumps({'schema':SCHEMA,'harness':'workbuddy','integration':'local_mcp','state':'conflict','configured':False,'reason':str(error),'business_tasks_sent':0},ensure_ascii=False));return 1
 except Exception as error:
  print(json.dumps({'schema':SCHEMA,'harness':'workbuddy','integration':'local_mcp','state':'conflict','configured':False,'reason':'本机连接未确认，请保留原配置并重试检查。','error_type':type(error).__name__,'business_tasks_sent':0},ensure_ascii=False));return 1
if __name__=='__main__':raise SystemExit(main())
