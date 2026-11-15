"""Finite per-user installer. Existing node contents are never replaced.

All payload bytes have an embedded manifest. Only fresh, installer-owned nodes
may be initialized. No peer allow/trust, provider, payment, auth or send action.
"""
import argparse,hashlib,json,os,pathlib,re,shutil,stat,subprocess,sys,time,uuid
P=pathlib.Path
OWNER='anet-desktop-installer/1'
def sha(p):
 h=hashlib.sha256()
 with open(p,'rb') as f:
  while b:=f.read(1048576):h.update(b)
 return h.hexdigest()
def plain(p):
 p=P(os.path.abspath(p))
 for x in [p,*p.parents]:
  if x.exists() and (x.is_symlink() or getattr(x.lstat(),'st_file_attributes',0)&0x400):raise ValueError('reparse_path_refused')
 return p
def within(p,root):
 p=plain(p);root=plain(root)
 if p==root or root not in p.parents:raise ValueError('outside_owned_root')
 return p
def read(p):return json.loads(P(p).read_text(encoding='utf-8'))
def atomic(p,value):
 p=plain(p);p.parent.mkdir(parents=True,exist_ok=True);tmp=p.with_name(p.name+'.'+uuid.uuid4().hex+'.tmp')
 with open(tmp,'x',encoding='utf-8',newline='\n') as f:json.dump(value,f,ensure_ascii=False,indent=2);f.write('\n');f.flush();os.fsync(f.fileno())
 os.replace(tmp,p)
def execute(argv,env=None,timeout=60):
 if P(argv[0]).name.lower()=='powershell.exe':env={k:v for k,v in (env or os.environ).items() if k.lower()!='psmodulepath'}
 r=subprocess.run([str(x) for x in argv],env=env,stdin=subprocess.DEVNULL,capture_output=True,timeout=timeout,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
 # Never propagate raw node/config/credential diagnostics into a public receipt.
 if r.returncode:raise RuntimeError('finite_command_failed_'+P(argv[0]).stem+'_'+str(r.returncode))
 return r.stdout.decode('utf-8-sig',errors='replace')

def destination_path_preflight(m,groups):
 if os.name!='nt':return
 import winreg
 try:
  with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,r'SYSTEM\CurrentControlSet\Control\FileSystem') as key:
   if winreg.QueryValueEx(key,'LongPathsEnabled')[0]==1:return
 except OSError:pass
 for row in m['files']:
  group,relative=row['path'].split('/',1)
  if group not in groups:continue
  target=P(groups[group])/relative
  # Temporary siblings also have to fit before atomic activation.
  longest=max(str(target),str(target.parent/('.i-'+'0'*12)),key=len)
  if len(longest.encode('utf-16-le'))//2>=260:
   raise ValueError('安装目录过长，请选择较短的目录后重新安装；尚未复制程序文件。')

def process_matches(pid,exe):
 if not isinstance(pid,int) or pid<1:return False
 import ctypes
 from ctypes import wintypes
 k=ctypes.WinDLL('kernel32',use_last_error=True);k.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD];k.OpenProcess.restype=wintypes.HANDLE;k.QueryFullProcessImageNameW.argtypes=[wintypes.HANDLE,wintypes.DWORD,wintypes.LPWSTR,ctypes.POINTER(wintypes.DWORD)];k.CloseHandle.argtypes=[wintypes.HANDLE]
 handle=k.OpenProcess(0x1000,False,pid)
 if not handle:return False
 try:
  text=ctypes.create_unicode_buffer(32768);n=wintypes.DWORD(len(text))
  if not k.QueryFullProcessImageNameW(handle,0,text,ctypes.byref(n)):return False
  observed=os.path.normcase(os.path.realpath(text.value));want=os.path.normcase(os.path.realpath(exe))
  return observed in (want,os.path.normcase(str(P(want).with_name('pythonw.exe'))))
 finally:k.CloseHandle(handle)
def validate_payload(payload):
 m=read(payload/'payload-manifest.json')
 if m['schema']!='anet-desktop-payload/1':raise ValueError('payload_schema')
 seen=set()
 for f in m['files']:
  name=f['path'];p=within(payload/name,payload)
  if name.casefold() in seen or ':' in name or '\\' in name or any(v in ('','.','..') or v.endswith((' ','.')) for v in name.split('/')) or not p.is_file() or p.stat().st_size!=f['bytes'] or sha(p)!=f['sha256']:raise ValueError('payload_hash_mismatch')
  seen.add(name.casefold())
 return m

def group_digest(m,group):
 rows=[f for f in m['files'] if f['path'].startswith(group+'/')]
 if not rows:raise ValueError('empty_payload_group_'+group)
 return hashlib.sha256(json.dumps(rows,sort_keys=True,separators=(',',':')).encode()).hexdigest()[:16]
def copy_group(payload,m,group,destination,*,preserve=False,link=False):
 destination=plain(destination);prefix=group+'/'
 files=[f for f in m['files'] if f['path'].startswith(prefix)]
 if not files:raise ValueError('empty_payload_group_'+group)
 for f in files:
  target=within(destination/f['path'][len(prefix):],destination)
  if target.exists():
   if not target.is_file() or sha(target)!=f['sha256']:raise ValueError('existing_owned_code_drift_'+group)
  else:
   target.parent.mkdir(parents=True,exist_ok=True);temp=target.parent/('.i-'+uuid.uuid4().hex[:12])
   try:
    source=payload/f['path']
    if link and source.stat().st_dev==temp.parent.stat().st_dev and source.stat().st_nlink==1:
     try:os.link(source,temp)
     except OSError:shutil.copyfile(source,temp)
    else:shutil.copyfile(source,temp)
    if temp.stat().st_size!=f['bytes'] or sha(temp)!=f['sha256']:raise ValueError('staged_copy_integrity_failed')
    os.replace(temp,target)
   finally:
    if temp.exists():temp.unlink()
 return [{'path':f['path'][len(prefix):],'sha256':f['sha256'],'bytes':f['bytes']} for f in files]
def verify_files(root,files):
 for f in files:
  p=within(root/f['path'],root)
  if not p.is_file() or p.stat().st_size!=f['bytes'] or sha(p)!=f['sha256']:raise ValueError('installed_code_drift')
def paths(args):
 if args.fixture_root:
  base=plain(args.fixture_root)
  if len(str(base))<12 or base==P.home() or base in P.home().parents:raise ValueError('bounded_fixture_root_required')
  return base/'Programs/ANetDesktop',base/'Data/ANetDesktop'
 local=plain(os.environ['LOCALAPPDATA']);return local/'Programs/ANetDesktop',local/'ANetDesktop'
def mark(root):
 root=plain(root);root.mkdir(parents=True,exist_ok=True);p=root/'installer-owner.json'
 if p.exists():
  if read(p).get('owner')!=OWNER:raise ValueError('foreign_installation_root')
 elif list(root.iterdir()):raise ValueError('nonempty_unowned_installation_root')
 else:atomic(p,{'owner':OWNER})
def selected_node(args,data):
 if args.node_root:return plain(args.node_root),True
 if not args.fixture_root:
  existing=plain(P.home()/'.anet')
  if existing.exists() and any(existing.iterdir()):return existing,True
 return data/'node',False

def broker_python(node,default,reused):
 """Match the existing service's executable pin without editing its policy."""
 if not reused:return default
 cfgpath=node/'taskd/config.json'
 if not cfgpath.exists():return default
 policy=((read(cfgpath).get('requester') or {}).get('owned_sources') or {}).get('release_policy') or {}
 expected=policy.get('broker_exe_sha256')
 if not expected:return default
 local=P(os.environ.get('LOCALAPPDATA',str(P.home()/'AppData/Local')))
 candidates=[default,P(sys.executable),*sorted((local/'Programs/Python').glob('Python*/python.exe'))]
 for p in candidates:
  if p.is_file() and sha(p)==expected:return plain(p)
 raise ValueError('existing_source_policy_python_unavailable')

def lock_free(path):
 if not path.exists():return True
 import msvcrt
 with open(path,'a+b') as f:
  f.seek(0)
  try:msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)
  except OSError:return False
 return True

def stop_managed_requester(node):
 task=node/'taskd'
 if lock_free(task/'daemon.lock') and lock_free(task/'supervise.lock'):return
 (task/'stop').touch();until=time.monotonic()+40
 while time.monotonic()<until:
  if lock_free(task/'daemon.lock') and lock_free(task/'supervise.lock'):return
  time.sleep(.2)
 raise RuntimeError('owned_service_stop_unconfirmed_no_restart')

def space_preflight(payload,m,args,dest,data,backend,pythonroot,harnessroot,node,reuse):
 destinations=[('frontend',dest),('python',pythonroot),('harness-runtime',harnessroot)]
 if not args.backend_root:destinations.append(('backend',backend))
 if not reuse:destinations.append(('node',node))
 needed={}
 def existing_parent(p):
  while not p.exists():p=p.parent
  return p
 for group,root in destinations:
  parent=existing_parent(root);device=parent.stat().st_dev;item=needed.setdefault(device,[parent,0])
  may_link=getattr(args,'private_payload',False) and payload.stat().st_dev==device
  for f in m['files']:
   if f['path'].startswith(group+'/') and not (root/f['path'][len(group)+1:]).exists() and not may_link:item[1]+=f['bytes']
 # A bootstrap is retained per frontend version even when immutable runtime
 # files share the private extraction's validated bytes on the same volume.
 parent=existing_parent(dest);item=needed.setdefault(parent.stat().st_dev,[parent,0])
 if not (dest/'ANetSetup.exe').exists():item[1]+=P(args.self_exe).stat().st_size
 for parent,n in needed.values():
  if shutil.disk_usage(parent).free<n+64*1024*1024:raise OSError('insufficient_installation_disk_space')

def upgrade_managed_app(args,payload,m,node,data,prior):
 if not prior or prior.get('node_reused') or not prior.get('node_files'):return
 if read(node/'desktop-owned-node.json').get('owner')!=OWNER:raise ValueError('owned_node_required_for_app_upgrade')
 old={v['path']:v for v in prior['node_files']}
 changed=[f for f in m['files'] if f['path'].startswith('node/') and old.get(f['path'][5:],{}).get('sha256')!=f['sha256']]
 if not changed:return
 # Binary replacement belongs to its signed maintenance protocol. Desktop
 # updates can reconcile their explicitly owned public Python app only.
 for f in changed:
  relative=f['path'][5:]
  if not relative.startswith('taskd/app/') and relative in old:raise ValueError('managed_binary_update_requires_signed_maintenance')
 verify_files(node,prior['node_files'])
 stage=data/('taskd-app-stage-'+uuid.uuid4().hex);copy_group(payload,m,'node/taskd/app',stage,link=getattr(args,'private_payload',False))
 stop_managed_requester(node)
 target=node/'taskd/app';backup=data/('backups/taskd-app-'+prior['release']+'-'+uuid.uuid4().hex);backup.parent.mkdir(exist_ok=True)
 os.replace(target,backup)
 try:os.replace(stage,target)
 except Exception:
  if not target.exists():os.replace(backup,target)
  raise
 return str(backup)

def restore_managed(node,data,prior,app_backup,config_backup,ledger_backup,*,restart=False):
 stop_managed_requester(node)
 target=node/'taskd/app'
 if app_backup:
  backup=within(P(app_backup),data);old_files=[dict(f,path=f['path'][len('taskd/app/'):]) for f in prior['node_files'] if f['path'].startswith('taskd/app/')]
  verify_files(backup,old_files);failed=data/('backups/failed-app-'+uuid.uuid4().hex);os.replace(target,failed);os.replace(backup,target)
 if config_backup is not None:atomic(node/'taskd/config.json',config_backup)
 if ledger_backup is not None:atomic(node/'taskd/local-patches.json',ledger_backup)
 if restart and config_backup is not None:
  stop=node/'taskd/stop'
  if stop.exists():stop.unlink()
  env=dict(os.environ,ANET_DATA_DIR=str(node),CODEX_HOME=config_backup.get('requester',{}).get('codex_home') or str(data/'requester-codex-home'),PYTHONDONTWRITEBYTECODE='1')
  argv=[prior['runtime']['python_exe'],str(node/'taskd/app/run_taskd.py'),'--data-dir',str(node),'--anet',str(node/'bin/anet.exe'),'supervise'];log=open(node/'taskd/taskd.out.log','ab')
  p=subprocess.Popen(argv,env=env,stdin=subprocess.DEVNULL,stdout=log,stderr=log,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0));log.close();atomic(data/('fixture-process.json' if prior.get('fixture') else 'service-process.json'),{'pid':p.pid,'argv':argv,'started_at':time.time()})
def preflight_managed_policy_upgrade(args,m,node,prior):
 # A code-only invocation must not leave a service's exact release pin behind.
 # The normal GUI/updater path reconciles policy under its registry guard in
 # setup_fresh; without that requested path, refuse before copying any files.
 if args.start_services or not prior or prior.get('node_reused') or not prior.get('node_files'):return
 old={v['path']:v for v in prior['node_files']}
 if not any(f['path'].startswith('node/taskd/app/') and old.get(f['path'][5:],{}).get('sha256')!=f['sha256'] for f in m['files']):return
 cfgpath=node/'taskd/config.json'
 if not cfgpath.exists():return
 requester=read(cfgpath).get('requester') or {}
 if any(isinstance(requester.get(name),dict) and 'release_policy' in requester[name] for name in ('harness_sources','owned_sources')):
  raise ValueError('existing_release_policy_requires_start_services: 升级现有协作服务请使用 --start-services，或在安装窗口选择“安装并准备”；尚未修改安装内容和服务配置。')

def install(args,payload,m):
 program,data=paths(args)
 version=m['release']
 if not re.fullmatch(r'\d+\.\d+\.\d+(?:-preview\.\d+)?',version):raise ValueError('release_name')
 versions=program/'versions';dest=versions/version
 current=data/'current.json';prior=read(current) if current.exists() else read(data/'last-install.json') if (data/'last-install.json').exists() else None
 node,reuse=selected_node(args,data)
 if prior:
  prior_node=prior['runtime'].get('anet_root',prior['runtime']['node_root'])
  if args.node_root and str(node)!=prior_node:raise ValueError('existing_node_change_refused')
  node=P(prior_node);reuse=prior['node_reused']
 preflight_managed_policy_upgrade(args,m,node,prior)
 backend=plain(args.backend_root) if args.backend_root else data/('backends/owned-source-'+group_digest(m,'backend'))
 pythonroot=data/('runtimes/python-3.14.10-'+group_digest(m,'python'));harnessroot=data/('runtimes/harnesses-'+group_digest(m,'harness-runtime'))
 destination_path_preflight(m,{'frontend':versions/('.stage-'+'0'*32),'backend':backend,'python':pythonroot,'harness-runtime':harnessroot,**({} if reuse else {'node':node})})
 mark(program);mark(data)
 space_preflight(payload,m,args,dest,data,backend,pythonroot,harnessroot,node,reuse)
 if args.backend_root:
  bm=read(payload/'frontend/backend-manifest.json');verify_files(backend,bm['files'])
  backend_files=[]
 else:backend_files=copy_group(payload,m,'backend',backend,link=getattr(args,'private_payload',False))
 python_files=copy_group(payload,m,'python',pythonroot,link=getattr(args,'private_payload',False))
 harness_files=copy_group(payload,m,'harness-runtime',harnessroot,link=getattr(args,'private_payload',False))
 # New code is staged, checked and renamed as one immutable frontend version.
 if dest.exists():
  front=[{'path':x['path'][9:],'bytes':x['bytes'],'sha256':x['sha256']} for x in m['files'] if x['path'].startswith('frontend/')]
  for f in front:
   target=within(dest/f['path'],dest)
   if not target.exists():
    target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(payload/'frontend'/f['path'],target)
  if args.action=='repair':
   # Repair only known frontend files, never delete unknown additions or state.
   for f in front:
    target=within(dest/f['path'],dest)
    if target.exists() and (not target.is_file() or target.stat().st_nlink!=1):raise ValueError('repair_nonordinary_target')
    if not target.exists() or sha(target)!=f['sha256']:
     target.parent.mkdir(parents=True,exist_ok=True);tmp=target.with_name(target.name+'.repair-'+uuid.uuid4().hex);shutil.copyfile(payload/'frontend'/f['path'],tmp);os.replace(tmp,target)
  verify_files(dest,front)
 else:
  stage=versions/('.stage-'+uuid.uuid4().hex);front=copy_group(payload,m,'frontend',stage,link=getattr(args,'private_payload',False));verify_files(stage,front);os.replace(stage,dest)
 setup=dest/'ANetSetup.exe'
 if not setup.exists():
  temp=setup.with_name('.setup-'+uuid.uuid4().hex)
  try:shutil.copyfile(args.self_exe,temp);os.replace(temp,setup)
  finally:
   if temp.exists():temp.unlink()
 elif sha(setup)!=sha(args.self_exe):raise ValueError('same_release_installer_mismatch')
 # Everything above is immutable new code; the activation failure injection
 # must not alter a running managed node or its current app.
 if args.fail_before_activate:raise RuntimeError('fixture_fail_before_activation')
 node_files=[];app_backup=None;config_backup=None;ledger_backup=None
 if not reuse:
  if node.exists() and any(node.iterdir()) and not (node/'desktop-owned-node.json').exists():raise ValueError('fresh_node_became_nonempty')
  node.mkdir(parents=True,exist_ok=True);atomic(node/'desktop-owned-node.json',{'owner':OWNER})
  if prior:
   config_backup=read(node/'taskd/config.json') if (node/'taskd/config.json').exists() else None
   ledger_backup=read(node/'taskd/local-patches.json') if (node/'taskd/local-patches.json').exists() else None
  app_backup=upgrade_managed_app(args,payload,m,node,data,prior)
  try:node_files=copy_group(payload,m,'node',node,link=getattr(args,'private_payload',False))
  except Exception:
   if app_backup:restore_managed(node,data,prior,app_backup,config_backup,ledger_backup,restart=args.start_services)
   raise
 runtime={'schema':'anet-desktop-runtime/1','python_exe':str(pythonroot/'python.exe'),'broker_python_exe':str(broker_python(node,pythonroot/'python.exe',reuse)),'node_root':str(node/'taskd'),'anet_root':str(node),'persistent_root':str(data),'backend_root':str(backend),'bootstrap_exe':str(setup),'harness_root':str(dest/'harnesses'),'harness_config_path':str(data/'harness-config.json'),'codex_exe':str(harnessroot/'codex/codex.exe'),'source_state_root':prior['runtime'].get('source_state_root',str(data/'source-state')) if prior else str(data/'source-state')}
 configpath=data/'harness-config.json'
 if not configpath.exists():
  atomic(configpath,{'schema':'anet-harness-config/1','harnesses':{'codex':{'executable':str(harnessroot/'codex/codex.exe')},'claude_code':{'executable':str(pythonroot/'Lib/site-packages/claude_agent_sdk/_bundled/claude.exe')},'deepseek_harness':{'dsh_home':str(data/'harness-state/deepseek'),'provider':'deepseek-official','model':'deepseek-v4-flash'},'workbuddy':{'token_env':'ANET_WORKBUDDY_ACCESS_TOKEN'}}})
 atomic(dest/'install/runtime.json',runtime)
 receipt={'schema':OWNER,'release':version,'program_root':str(program),'data_root':str(data),'frontend_root':str(dest),'node_reused':reuse,'runtime':runtime,'frontend_files':front,'backend_files':backend_files,'python_files':python_files,'harness_files':harness_files,'node_files':node_files,'fixture':bool(args.fixture_root),'fixture_root':str(plain(args.fixture_root)) if args.fixture_root else None,'setup_sha256':sha(setup),'at':time.time(),'signature':{'installer_authenticode':'not_signed','python':'official ZIP SHA256','anet':'pinned binary SHA256'}}
 if prior and prior['release']!=version:atomic(data/'previous.json',prior)
 atomic(dest/'install/receipt.json',receipt)
 atomic(current,receipt)
 if not args.fixture_root:
  try:register_user(receipt)
  except Exception:
   if prior and not reuse:restore_managed(node,data,prior,app_backup,config_backup,ledger_backup,restart=args.start_services)
   if prior:
    atomic(current,prior)
    try:register_user(prior)
    except Exception:pass
   elif current.exists() and read(current).get('frontend_root')==str(dest):current.unlink()
   raise
 if args.start_services:
  try:setup_fresh(args,receipt,payload,m)
  except Exception as e:
   # Network or account setup may need attention after code activation.
   # Preserve that fact rather than claim an automatic rollback took place.
   atomic(data/'setup-state.json',{'state':'needs_attention','reason':type(e).__name__+':'+str(e)[:120],'retry_policy':'check_existing_process_before_start'})
   if prior and prior['release']!=version:
    if not reuse:restore_managed(node,data,prior,app_backup,config_backup,ledger_backup,restart=True)
    atomic(current,prior)
    if not args.fixture_root:register_user(prior)
    return {'state':'rolled_back','release':prior['release'],'failed_release':version,'reason':'new_requester_startup_failed','node_state_preserved':True}
 return {'state':'installed','release':version,'existing_node_reused':reuse,'runtime':runtime,'node_setup':read(data/'setup-state.json') if (data/'setup-state.json').exists() else {'state':'not_requested'},'installer_authenticode':'not_signed'}
def register_user(receipt):
 import winreg
 key=r'Software\Microsoft\Windows\CurrentVersion\Uninstall\ANetDesktop'
 with winreg.CreateKey(winreg.HKEY_CURRENT_USER,key) as k:
  values={'DisplayName':'ANet Desktop','DisplayVersion':receipt['release'],'Publisher':'ANet Desktop local distribution','InstallLocation':receipt['frontend_root'],'UninstallString':'"'+receipt['runtime']['bootstrap_exe']+'" --uninstall','NoModify':1}
  for name,value in values.items():winreg.SetValueEx(k,name,0,winreg.REG_DWORD if isinstance(value,int) else winreg.REG_SZ,value)
 # Only manage this exact Start-menu link; refuse unrelated existing targets.
 short=P(os.environ['APPDATA'])/'Microsoft/Windows/Start Menu/Programs/ANet Desktop.lnk'
 target=P(receipt['frontend_root'])/'ANetDesktop.exe'
 quote=lambda v:str(v).replace("'","''")
 script="$s=New-Object -ComObject WScript.Shell; $l=$s.CreateShortcut('"+quote(short)+"'); if((Test-Path -LiteralPath '"+quote(short)+"') -and $l.TargetPath -and -not $l.TargetPath.StartsWith('"+quote(receipt['program_root'])+"\\',[StringComparison]::OrdinalIgnoreCase)){throw 'foreign_shortcut'}; $l.TargetPath='"+quote(target)+"'; $l.Arguments='--runtime \""+quote(P(receipt['frontend_root'])/'install/runtime.json')+"\"'; $l.WorkingDirectory='"+quote(receipt['frontend_root'])+"'; $l.Save()"
 execute(['powershell.exe','-NoLogo','-NoProfile','-NonInteractive','-Command',script],timeout=30)

def harness_release_policy(task,python):
 helper=plain(task/'app/anet_harnesses/package_release.py')
 if not helper.is_file():raise ValueError('harness_release_closure_helper_required')
 entry="import json,runpy;h=runpy.run_path("+repr(str(helper))+");print(json.dumps({'package_schema':h['SCHEMA'],'package_sha256':h['package_digest']()}))"
 policy=json.loads(execute([python,'-B','-X','utf8','-c',entry],timeout=30))
 if policy.get('package_schema')!='anet-harness-release-closure/2' or not re.fullmatch('[a-f0-9]{64}',policy.get('package_sha256','')):raise ValueError('harness_release_closure_contract')
 policy['service_exe_sha256']=sha(python)
 return policy

def release_migration_helpers(task):
 import importlib
 app=plain(task/'app');sys.path.insert(0,str(app))
 module=importlib.import_module('taskd.harness_sources')
 if plain(module.__file__)!=plain(app/'taskd/harness_sources.py'):raise ValueError('taskd_migration_import_origin_mismatch')
 return module
def setup_fresh(args,r,payload,m):
 data=P(r['data_root']);node=P(r['runtime'].get('anet_root',r['runtime']['node_root']));state=data/'setup-state.json'
 if r['node_reused']:
  atomic(state,{'state':'existing_node_preserved','service_mutated':False});return
 if read(node/'desktop-owned-node.json').get('owner')!=OWNER:raise ValueError('fresh_owned_node_required')
 if not m.get('node_release',{}).get('fresh_node_verified'):raise ValueError('requires_verified_node_release')
 python=P(r['runtime']['python_exe']);anet=node/'bin/anet.exe'
 task=node/'taskd';cfgpath=task/'config.json'
 harness_enabled=(task/'app/taskd/harness_sources.py').exists()
 harness_policy=harness_release_policy(task,python) if harness_enabled else None
 if harness_enabled and m.get('harness_release')!={'schema':harness_policy['package_schema'],'package_sha256':harness_policy['package_sha256']}:raise ValueError('installed_harness_release_closure_mismatch')
 requester_home=data/'requester-codex-home';requester_home.mkdir(exist_ok=True)
 env=dict(os.environ,ANET_DATA_DIR=str(node),CODEX_HOME=str(requester_home),PYTHONDONTWRITEBYTECODE='1')
 # The signed official binary creates defaults: closed inbound, no public
 # capabilities, zero spending. It never receives controller/shell/peer flags.
 if not (node/'config.json').exists():execute([anet,'init'],env=env)
 codex=P(read(data/'harness-config.json')['harnesses']['codex']['executable'])
 policy={'broker_script_sha256':sha(P(r['runtime']['backend_root'])/'broker/src/broker.py'),'broker_exe_sha256':sha(python),'codex_exe_sha256':sha(codex) if codex.exists() else '0'*64}
 existed=cfgpath.exists();before_config_sha=sha(cfgpath) if existed else None
 if not existed:
  cfg={'version':1,'taskd_version':'1.9.4','installed_at':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'installed_ts':time.time(),'transport':{'type':'anet','binary':str(anet),'data_dir':str(node)},'requester':{'clients':['codex'],'discover':True,'codex_command':str(codex),'codex_home':str(requester_home),'owned_sources':{'enabled':True,'release_policy':policy}},'executor':None,'reviewer':None,'guard':[]}
 else:cfg=read(cfgpath)
 original=json.loads(json.dumps(cfg));paired=(node/'portal/connection.json').is_file();migration=None;snapshot=None
 if harness_enabled:
  migration=release_migration_helpers(task)
  previous=(cfg.get('requester') or {}).get('harness_sources',{}).get('release_policy')
  snapshot=migration.prepare_release_upgrade(task,previous,sha(python));approved=snapshot['release_policy']
  if any(approved.get(key)!=harness_policy[key] for key in ('package_schema','package_sha256','service_exe_sha256')):raise ValueError('harness_migration_release_mismatch')
  cfg['requester']['harness_sources']={'enabled':True,'release_policy':approved}
  previous_owned=(cfg.get('requester') or {}).get('owned_sources',{}).get('release_policy') if existed else None
  policy=migration.preserve_owned_release_policy(previous_owned,policy)
 if (cfg.get('requester') or {}).get('owned_sources',{}).get('release_policy')!=policy:cfg['requester']['owned_sources']={'enabled':True,'release_policy':policy}
 if paired and (cfg.get('requester_artifacts') or {}).get('enabled') is not True:
  queue=data/'artifacts/requester-queue';queue.parent.mkdir(parents=True,exist_ok=True)
  # The worker independently checks the paired identity and token in RAM.
  # This code neither copies the connection nor declares authentication valid.
  cfg['requester_artifacts']={'enabled':True,'queue_dir':str(queue)}
 changed=not existed or cfg!=original
 def commit():
  if cfgpath.exists()!=existed or (existed and sha(cfgpath)!=before_config_sha):raise ValueError('taskd_config_changed_during_upgrade')
  if changed:atomic(cfgpath,cfg)
 if migration:
  with migration.release_upgrade_guard(task,snapshot):commit()
  atomic(data/'last-policy-upgrade.json',snapshot)
 else:commit()
 # A fresh absent registry remains absent until its policy commit is complete.
 execute([anet,'up'],env=env,timeout=120)
 (task/'auto-update-off').touch(exist_ok=True)
 # Protect the full public taskd app against a stock updater replacement.
 files={'app/'+f['path'][len('node/taskd/app/'):]:{'after':f['sha256']} for f in m['files'] if f['path'].startswith('node/taskd/app/')}
 atomic(task/'local-patches.json',{'schema':1,'component':'taskd','files':files,'preserved_existing_fixes':{}})
 sys.path.insert(0,str(task/'app'))
 from taskd.platform import autostart
 argv=[str(python),str(task/'app/run_taskd.py'),'--data-dir',str(node),'--anet',str(anet),'supervise']
 heartbeat=task/'status.json'
 if changed and heartbeat.exists():
  beat=read(heartbeat)
  if process_matches(beat.get('pid'),python):
   stop_managed_requester(node)
 if heartbeat.exists():
  beat=read(heartbeat)
  if time.time()-beat.get('ts',0)<30 and beat.get('requester') is True and process_matches(beat.get('pid'),python) and not lock_free(task/'daemon.lock') and not (task/'stop').exists():
   atomic(state,{'state':'requester_service_running_authentication_required','accept_tasks':False,'requester':True,'auto_update':False,'service_reused':True});return
 if (task/'stop').exists():stop_managed_requester(node)
 if (task/'stop').exists():(task/'stop').unlink()
 # In fixtures an isolated child is started directly, never a global login item.
 started_at=time.time()
 if args.fixture_root:
  log=open(task/'taskd.out.log','ab');p=subprocess.Popen(argv,env=env,stdin=subprocess.DEVNULL,stdout=log,stderr=log,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0));log.close()
  atomic(data/'fixture-process.json',{'pid':p.pid,'argv':argv,'started_at':time.time()})
 else:
  # Persist the same private guide-home for login starts, not the user's
  # global Codex guide/configuration directory.
  autostart.install(task,argv,task/'taskd.out.log',start=False)
  log=open(task/'taskd.out.log','ab');p=subprocess.Popen(argv,env=env,stdin=subprocess.DEVNULL,stdout=log,stderr=log,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0));log.close();atomic(data/'service-process.json',{'pid':p.pid,'argv':argv,'started_at':time.time()})
 end=time.time()+30;heartbeat=task/'status.json'
 while time.time()<end:
  try:
   beat=read(heartbeat)
   if beat.get('requester') is True and beat.get('accept_tasks') is False and beat.get('ts',0)>=started_at and process_matches(beat.get('pid'),python) and not lock_free(task/'daemon.lock'):
    atomic(state,{'state':'requester_service_running_authentication_required','accept_tasks':False,'requester':True,'auto_update':False,'node_binary':m['node_release'].get('version'),'provider_enabled':False,'portal_pairing':'user_action_required','harness_auth':'user_action_required'});return
  except (OSError,ValueError):pass
  time.sleep(.2)
 raise RuntimeError('fresh_service_readiness_unconfirmed_no_restart')
def codex_location(args,data):
 local=P(args.fixture_root)/'UserLocal' if args.fixture_root else P(os.environ['LOCALAPPDATA'])
 return local/'Programs/OpenAI/Codex/bin/codex.exe'
def _release_manifest(rows):
 return {row['path']:(row['sha256'],row['bytes']) for row in rows}

def _harness_code_release(root,schema=None):
 root=plain(root)
 if schema is None:schema='anet-harness-release-closure/2' if (root/'anet_harnesses/package_release.py').is_file() else 'anet-harness-package/1'
 names=('anet_harnesses','anet_provider_harnesses') if schema=='anet-harness-release-closure/2' else ('anet_harnesses',) if schema=='anet-harness-package/1' else ()
 if not names:raise ValueError('unsupported_harness_release_schema')
 hashes={}
 for name in names:
  package=plain(root/name)
  if not (package/'__init__.py').is_file():raise ValueError('harness_release_package_missing')
  for path in sorted(package.rglob('*.py')):
   path=plain(path)
   if not path.is_file():raise ValueError('ordinary_harness_code_required')
   relative=path.relative_to(package).as_posix();key=name+'/'+relative if len(names)==2 else relative;hashes[key]=sha(path)
 return {'package_schema':schema,'package_sha256':hashlib.sha256(json.dumps(hashes,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()}

def _rollback_snapshot(data,node):
 paths=[data/'current.json',data/'previous.json',node/'config.json',node/'local-patches.json',node/'harness-sources/sources.json',node/'harness-sources/owner.json']
 return {str(plain(path)):sha(path) if path.is_file() else None for path in paths}

def rollback_preflight(current,prior,data):
 """Read-only compatibility gate, not a reverse migration or live-source test.

 Historical exact-record compatibility does not authorize a newly started old
 SDK host. A frontend switch is permitted only with the same managed app and
 runtime release and a prior SDK matching the current primary admission tuple.
 """
 result={'allowed':False,'state':'needs_service_rollback_plan','current_version':current.get('release'),'target_version':prior.get('release'),'rollback_scope':'none','node_modified':False,'full_managed_reverse_migration':False,'checks':{}}
 def hold(reason):result['reason_code']=reason;return result
 try:
  data=plain(data)
  if current.get('schema')!=OWNER or prior.get('schema')!=OWNER:return hold('owned_current_and_prior_receipts_required')
  for receipt in (current,prior):
   if plain(receipt['data_root'])!=data or plain(receipt['runtime']['persistent_root'])!=data:return hold('rollback_data_root_mismatch')
  if plain(current['program_root'])!=plain(prior['program_root']):return hold('rollback_program_root_mismatch')
  current_node=plain(current['runtime']['node_root']);node=plain(prior['runtime']['node_root'])
  if node!=current_node or current.get('node_reused')!=prior.get('node_reused'):return hold('rollback_node_identity_or_ownership_changed')
  result['snapshot']=_rollback_snapshot(data,node)
  if read(data/'current.json')!=current or read(data/'previous.json')!=prior:return hold('rollback_receipts_changed')
  managed=not current.get('node_reused');result['managed_node']=managed
  if managed:
   if not prior.get('node_files') or _release_manifest(prior['node_files'])!=_release_manifest(current['node_files']):return hold('managed_service_release_changed')
   verify_files(node.parent,prior['node_files']);result['checks']['prior_managed_code_matches_actual']=True
  for group in ('python_files','harness_files'):
   if not prior.get(group) or _release_manifest(prior[group])!=_release_manifest(current.get(group,[])):return hold('runtime_release_changed')
  for key in ('python_exe','broker_python_exe','codex_exe'):
   before=prior['runtime'].get(key,prior['runtime']['python_exe']);now=current['runtime'].get(key,current['runtime']['python_exe'])
   if plain(before)!=plain(now) or not plain(before).is_file():return hold('runtime_executable_path_changed_or_missing')
  for key in ('source_state_root','harness_config_path'):
   if prior['runtime'].get(key)!=current['runtime'].get(key):return hold('source_state_or_harness_configuration_root_changed')
  result['checks']['same_runtime_release']=True
  cfgpath=node/'config.json';registry=node/'harness-sources/sources.json'
  if not cfgpath.exists():
   if (node/'app/taskd/harness_sources.py').exists() or registry.exists():return hold('task_service_policy_missing')
   result.update(allowed=True,state='frontend_only_rollback_ready',rollback_scope='frontend_only',reason_code='same_unconfigured_service_release');return result
  cfg=read(cfgpath);requester=cfg.get('requester') or {};generic=requester.get('harness_sources') or {};owned=requester.get('owned_sources') or {}
  if generic.get('enabled') is True:
   policy=generic.get('release_policy') or {};schema=policy.get('package_schema','anet-harness-package/1')
   expected=_harness_code_release(prior['runtime']['harness_root']);actual=_harness_code_release(node/'app',schema)
   if expected['package_schema']!=schema or expected['package_sha256']!=policy.get('package_sha256') or actual['package_sha256']!=policy.get('package_sha256') or sha(plain(prior['runtime']['python_exe']))!=policy.get('service_exe_sha256'):
    return hold('prior_sdk_not_compatible_with_current_primary')
   # compatible_releases is deliberately not used to admit a NEW old host.
   if registry.exists():
    table=read(registry)
    if not isinstance(table.get('sources'),dict):return hold('harness_registry_format_requires_review')
    for entry in table['sources'].values():
     record=entry.get('record');stamp=hashlib.sha256(json.dumps(record,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
     if not isinstance(record,dict) or stamp!=entry.get('record_sha256'):return hold('harness_registry_record_pin_changed')
     release=policy
     for candidate in policy.get('compatible_releases',[]):
      if stamp in candidate.get('registered_records',[]):release=candidate;break
     startup=record.get('startup') or {};record_schema=release.get('package_schema','anet-harness-package/1')
     if record_schema=='anet-harness-release-closure/2':
      if startup.get('package_schema')!=record_schema or startup.get('package_sha256')!=release.get('package_sha256'):return hold('harness_registry_schema_requires_review')
     elif record_schema!='anet-harness-package/1' or 'package_schema' in startup or 'package_sha256' in startup:return hold('harness_registry_schema_requires_review')
   result['checks']['prior_sdk_matches_primary_cold_start_tuple']=True
  elif registry.exists() and read(registry).get('sources'):return hold('registered_harness_policy_disabled_or_missing')
  if owned.get('enabled') is True:
   runtime=prior['runtime'];tuple_keys=('broker_script_sha256','broker_exe_sha256','codex_exe_sha256');required={'broker_script_sha256':sha(plain(P(runtime['backend_root'])/'broker/src/broker.py')),'broker_exe_sha256':sha(plain(runtime.get('broker_python_exe',runtime['python_exe']))),'codex_exe_sha256':sha(plain(runtime['codex_exe']))};policy=owned.get('release_policy') or {}
   approved=[{key:policy.get(key) for key in tuple_keys}]+policy.get('compatible_releases',[])
   if required not in approved:return hold('prior_owned_broker_tuple_not_approved')
   result['checks']['prior_owned_broker_whole_tuple_approved']=True
  result.update(allowed=True,state='frontend_only_rollback_ready',rollback_scope='frontend_only',reason_code='same_service_release_and_prior_primary_supported');return result
 except (OSError,ValueError,TypeError,KeyError,AttributeError) as error:
  result['validation_error_type']=type(error).__name__;return hold('service_rollback_compatibility_unverified')

def maintenance(args):
 program,data=paths(args);current=read(data/'current.json')
 if current.get('schema')!=OWNER:raise ValueError('owned_receipt_required')
 if args.action=='rollback':
  prior=read(data/'previous.json');verify_files(P(prior['frontend_root']),prior['frontend_files']);verify_files(P(prior['runtime']['backend_root']),read(P(prior['frontend_root'])/'backend-manifest.json')['files'])
  plan=rollback_preflight(current,prior,data)
  if not plan['allowed'] or _rollback_snapshot(data,plain(current['runtime']['node_root']))!=plan['snapshot']:
   if plan['allowed']:plan.update(allowed=False,state='needs_service_rollback_plan',reason_code='rollback_inputs_changed')
   return {'state':'needs_service_rollback_plan','release':current['release'],'requested_release':prior['release'],'rollback_performed':False,'current_pointer_written':False,'node_state_preserved':True,'reason':'任务服务或来源协议与旧版本不兼容，未切换版本；需要先制定服务回退或经验证的前向恢复方案。','preflight':plan}
  atomic(data/'current.json',prior)
  if not args.fixture_root:register_user(prior)
  return {'state':'rolled_back','release':prior['release'],'node_state_preserved':True,'rollback_scope':'frontend_only','service_release_changed':False,'full_managed_reverse_migration':False}
 if args.action=='uninstall':
  # Code only, exact owned frontend. Preserve stable runtime/backend/node and
  # every unknown file. Active service has no dependency on versioned frontend.
  root=within(P(current['frontend_root']),program)
  verify_files(root,current['frontend_files'])
  for f in current['frontend_files']:within(root/f['path'],root).unlink()
  for name in ('install/runtime.json','install/receipt.json'):
   p=within(root/name,root)
   if p.exists():p.unlink()
  # Running setup is retained; no delete-on-reboot or broad directory removal.
  atomic(data/'uninstalled.json',{'schema':OWNER,'at':time.time(),'preserved_node':True,'preserved_backend':True,'frontend':str(root)})
  atomic(data/'last-install.json',current);(data/'current.json').unlink()
  if not args.fixture_root:
   import winreg
   try:winreg.DeleteKey(winreg.HKEY_CURRENT_USER,r'Software\Microsoft\Windows\CurrentVersion\Uninstall\ANetDesktop')
   except FileNotFoundError:pass
   short=plain(P(os.environ['APPDATA'])/'Microsoft/Windows/Start Menu/Programs/ANet Desktop.lnk')
   if short.exists():
    quote=lambda v:str(v).replace("'","''")
    script="$s=New-Object -ComObject WScript.Shell; $l=$s.CreateShortcut('"+quote(short)+"'); if($l.TargetPath -eq '"+quote(root/'ANetDesktop.exe')+"'){Remove-Item -LiteralPath '"+quote(short)+"'}"
    execute(['powershell.exe','-NoLogo','-NoProfile','-NonInteractive','-Command',script],timeout=30)
  return {'state':'uninstalled','node_state_preserved':True,'stable_backend_preserved':True,'remaining_setup':str(root/'ANetSetup.exe')}
 raise ValueError('maintenance_action')
def main():
 ap=argparse.ArgumentParser();ap.add_argument('--payload',type=P,required=True);ap.add_argument('--self-exe',type=P,required=True);ap.add_argument('--action',choices=['install','repair','rollback','uninstall'],default='install');ap.add_argument('--fixture-root');ap.add_argument('--node-root');ap.add_argument('--backend-root');ap.add_argument('--private-payload',action='store_true');ap.add_argument('--start-services',action='store_true');ap.add_argument('--fail-before-activate',action='store_true');args=ap.parse_args()
 try:
  payload=plain(args.payload);m=validate_payload(payload)
  program,data=paths(args)
  # Also reject before mark/installer.lock changes. install() repeats the check
  # while holding the lifecycle lock, so this early read is not its only gate.
  if args.action in ('install','repair') and not args.start_services:
   current=data/'current.json';last=data/'last-install.json'
   prior=read(current) if current.exists() else read(last) if last.exists() else None
   if prior:preflight_managed_policy_upgrade(args,m,P(prior['runtime'].get('anet_root',prior['runtime']['node_root'])),prior)
  mark(data)
  # Native byte-range lock is released by Windows on a crash; no stale lease.
  with open(plain(data/'installer.lock'),'a+b') as lock:
   if os.fstat(lock.fileno()).st_nlink!=1:raise ValueError('nonordinary_installer_lock')
   import msvcrt
   lock.seek(0);lock.write(b'0');lock.flush();lock.seek(0);msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
   result=install(args,payload,m) if args.action in ('install','repair') else maintenance(args)
  print(json.dumps({'schema':'anet-installer-result/1',**result},ensure_ascii=False));return 0
 except Exception as e:
  print(json.dumps({'schema':'anet-installer-result/1','state':'failed','reason':str(e)[:160],'error_type':type(e).__name__,'business_tasks_sent':0}));return 1
if __name__=='__main__':raise SystemExit(main())
