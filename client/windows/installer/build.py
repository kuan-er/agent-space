"""Build a single offline per-user Windows installer from explicit public files.

Never traverse a user/node state directory for payload inclusion. Every source
file is selected by a public-code manifest or finite dependency directory.
"""
import argparse,hashlib,json,os,re,shutil,subprocess,sys,tarfile,zipfile,uuid
from pathlib import Path
ROOT=Path(__file__).resolve().parent
CLI=ROOT.parents[1]/'cli'
PYTHON_ZIP_SHA='90905e64a840557d8c1bc4f5b74db3d7ee85e3ffb217611cac1c7813acea4159'
FROZEN_RUNTIME_SHA256='7562c13bcde00214a7e5ed757107086f3317187e2c4385986657eed2af049580'
FROZEN_RUNTIME_CONTENT_KEY='f974e9b61f9f57186a2090a88ccf7e3db492392537ceabe6d9aa17891253771a'
FROZEN_RUNTIME_BYTES=478463574

def sha(p):
 h=hashlib.sha256()
 with open(p,'rb') as f:
  while b:=f.read(1048576):h.update(b)
 return h.hexdigest()
def write(p,v):p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
def copy(src,dst):
 if src.is_symlink() or getattr(src.stat(),'st_file_attributes',0)&0x400:raise ValueError('reparse_source')
 if dst.is_file() and dst.stat().st_size==src.stat().st_size and sha(dst)==sha(src):return
 dst.parent.mkdir(parents=True,exist_ok=True)
 if dst.exists() and dst.stat().st_nlink>1:
  temp=dst.with_name('.'+dst.name+'.build-temp');shutil.copyfile(src,temp);os.replace(temp,dst)
 else:shutil.copyfile(src,dst)
def clean(path):
 path=path.resolve()
 if path.parent!=ROOT or path.name!='payload':raise ValueError('bounded_build_payload_required')
 if path.exists():shutil.rmtree(path)
 path.mkdir()
def build_parser():
 ap=argparse.ArgumentParser()
 ap.add_argument('--desktop-root',type=Path,default=ROOT.parent/'desktop')
 ap.add_argument('--frontend-bin',type=Path)
 ap.add_argument('--backend-code',type=Path)
 ap.add_argument('--downloads-root',type=Path,default=ROOT/'downloads')
 ap.add_argument('--runtime-template',type=Path)
 ap.add_argument('--harness-overlay',type=Path,action='append',default=[])
 ap.add_argument('--harness-root',type=Path,default=CLI)
 ap.add_argument('--config-cli-root',type=Path,default=CLI/'anet_setup')
 ap.add_argument('--taskd-app',type=Path,default=CLI/'taskd_app')
 ap.add_argument('--taskd-overlay',type=Path)
 ap.add_argument('--codex-bin',type=Path,default=ROOT/'downloads/codex')
 ap.add_argument('--node-binary',type=Path)
 ap.add_argument('--node-release',type=Path)
 ap.add_argument('--release',default='0.3.0-preview.3')
 ap.add_argument('--prepare-only',action='store_true')
 ap.add_argument('--reuse-runtime',action='store_true')
 ap.add_argument('--archive-dir',type=Path)
 ap.add_argument('--runtime-archive-only',action='store_true')
 ap.add_argument('--frozen-runtime-archive',type=Path,help='Read-only V6 archive; exact release content key and SHA-256 are required. Never recompressed.')
 return ap
def runtime_content_key(rows):
 return hashlib.sha256(json.dumps(rows,sort_keys=True,separators=(',',':')).encode()).hexdigest()
def frozen_archive_input(path):
 path=Path(os.path.abspath(path))
 for component in (path,*path.parents):
  if component.is_symlink() or component.exists() and getattr(component.lstat(),'st_file_attributes',0)&0x400:raise ValueError('frozen_runtime_reparse_refused')
 before=path.stat()
 if not path.is_file() or before.st_size!=FROZEN_RUNTIME_BYTES:raise ValueError('frozen_runtime_archive_size_mismatch')
 if sha(path)!=FROZEN_RUNTIME_SHA256:raise ValueError('frozen_runtime_archive_sha256_mismatch')
 after=path.stat()
 if (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns)!=(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns):raise ValueError('frozen_runtime_archive_changed_during_read')
 return path
def frozen_runtime_archive(path,rows):
 # The pins belong to the independently accepted V6 release, not an editable
 # cache adjacent to the supplied archive. A mismatch never enters a writer.
 if runtime_content_key(rows)!=FROZEN_RUNTIME_CONTENT_KEY:raise ValueError('frozen_runtime_content_key_mismatch')
 path=frozen_archive_input(path);before=path.stat()
 with zipfile.ZipFile(path,'r') as z:
  entries=z.infolist();expected={row['path']:row['bytes'] for row in rows}
  if len(entries)!=len(expected) or len({e.filename.casefold() for e in entries})!=len(entries) or {e.filename:e.file_size for e in entries}!=expected:raise ValueError('frozen_runtime_archive_entries_mismatch')
 after=path.stat()
 if (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns)!=(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns):raise ValueError('frozen_runtime_archive_changed_during_read')
 return path
def distinct_archive_outputs(archive,outputs):
 archive=Path(os.path.abspath(archive))
 for output in outputs:
  output=Path(os.path.abspath(output))
  if os.path.normcase(str(output))==os.path.normcase(str(archive)) or output.exists() and os.path.samefile(archive,output):raise ValueError('frozen_runtime_output_alias_refused')
def protect_frozen_input(archive,payload,outputs,mutable_roots=()):
 archive=Path(os.path.abspath(archive));payload=Path(os.path.abspath(payload))
 if archive==payload or payload in archive.parents:raise ValueError('frozen_runtime_archive_in_payload_refused')
 for root in mutable_roots:
  root=Path(os.path.abspath(root))
  if archive==root or root in archive.parents:raise ValueError('frozen_runtime_archive_in_mutable_tree_refused')
 distinct_archive_outputs(archive,outputs)
 # Check aliases before clean(), copy(), or direct metadata writes. In reuse
 # mode a pre-existing metadata hardlink must not truncate the shared input.
 for root in dict.fromkeys([payload,*[Path(os.path.abspath(p)) for p in mutable_roots]]):
  if not root.exists():continue
  for file in (root,*root.rglob('*')):
   if file.is_symlink() or getattr(file.lstat(),'st_file_attributes',0)&0x400:raise ValueError('frozen_runtime_payload_reparse_refused')
   if file.is_file() and os.path.samefile(archive,file):raise ValueError('frozen_runtime_payload_alias_refused')
 return frozen_archive_input(archive)
def link_runtime_file(source,target):
 target.parent.mkdir(parents=True,exist_ok=True)
 # Reuse immutable bytes on the same volume; this workspace has an explicit
 # full-copy space allowance when the retained runtime is on another drive.
 try:os.link(source,target)
 except OSError:
  if shutil.disk_usage(target.parent).free < source.stat().st_size + 2*1024**3:raise OSError('insufficient_runtime_copy_space')
  shutil.copyfile(source,target)
def main():
 a=build_parser().parse_args()
 if not re.fullmatch(r'\d+\.\d+\.\d+(?:-preview\.\d+)?',a.release):raise ValueError('release_name')
 payload=ROOT/'payload';out=ROOT/'out';zip_dir=a.archive_dir or out
 if a.frozen_runtime_archive:
  zip_dir=Path(os.path.abspath(zip_dir));own_out=Path(os.path.abspath(out))
  if zip_dir!=own_out and own_out not in zip_dir.parents:raise ValueError('frozen_runtime_requires_owned_archive_output')
  a.frozen_runtime_archive=protect_frozen_input(a.frozen_runtime_archive,payload,[zip_dir/'code.zip',zip_dir/'runtime.zip',zip_dir/'runtime-cache.json',out/'PayloadPin.cs',out/('ANetSetup-'+a.release+'.exe'),out/'release.json'],[out,zip_dir])
 if not a.reuse_runtime:clean(payload)
 else:
  for group in ('frontend','backend','node'):
   folder=payload/group
   if folder.exists():
    if folder.resolve().parent!=payload.resolve():raise ValueError('bounded_code_group_required')
    shutil.rmtree(folder)
 out.mkdir(exist_ok=True)
 desktop=a.desktop_root;backend_code=a.backend_code or ROOT/'backend';front=a.frontend_bin or desktop/'out/ANetDesktop.exe'
 copy(front,payload/'frontend/ANetDesktop.exe');copy(desktop/'backend-manifest.json',payload/'frontend/backend-manifest.json')
 for name in ('install_engine.py','updater.py'):copy(ROOT/name,payload/'frontend/install'/name)
 bm=json.loads((desktop/'backend-manifest.json').read_text(encoding='utf-8'))
 for row in bm['files']:
  source=backend_code/row['path']
  if source.stat().st_size!=row['bytes'] or sha(source)!=row['sha256']:raise ValueError('backend_manifest_drift')
  copy(source,payload/'backend'/row['path'])
 archive=a.downloads_root/'python-3.14.10-embed-amd64.zip'
 if sha(archive)!=PYTHON_ZIP_SHA:raise ValueError('python_archive_integrity')
 if not a.reuse_runtime:
  if a.runtime_template:
   template=a.runtime_template.resolve();tm=json.loads((template/'payload-manifest.json').read_text(encoding='utf8'))
   for row in tm['files']:
    if row['path'].startswith(('python/','harness-runtime/')):
     source=template/row['path'];target=payload/row['path']
     if sha(source)!=row['sha256']:raise ValueError('runtime_template_integrity')
     link_runtime_file(source,target)
  else:
   with zipfile.ZipFile(archive) as z:z.extractall(payload/'python')
 pth=payload/'python/python314._pth';expected_pth='python314.zip\n.\nLib/site-packages\nimport site\n'
 if pth.read_text(encoding='utf8')!=expected_pth:
  temp=pth.with_name('.python-pth-build');temp.write_text(expected_pth,encoding='utf8');os.replace(temp,pth)
 wheels=a.downloads_root/'wheels'
 if not a.reuse_runtime and not a.runtime_template:subprocess.run([sys.executable,'-m','pip','install','--disable-pip-version-check','--no-compile','--no-index','--find-links',str(wheels),'--target',str(payload/'python/Lib/site-packages'),'claude-agent-sdk==0.2.181','deepseek-harness-sdk==0.2.2rc2','deepseek-harness-runtime-bin==0.2.2rc2'],check=True)
 copy(ROOT/'runtime_sitecustomize.py',payload/'python/Lib/site-packages/sitecustomize.py')
 # Dynamic adapter code is versioned with the frontend, not the large SDK
 # tree. Callers insert the exact reviewed harness_root explicitly.
 alias=payload/'python/Lib/site-packages/anet_harnesses'
 if alias.exists():
  if alias.resolve().parent!=(payload/'python/Lib/site-packages').resolve() or alias.name!='anet_harnesses':raise ValueError('bounded_adapter_alias_required')
  shutil.rmtree(alias)
 for name in ('install-catalog.json','capabilities.json','README.md'):
  if (a.harness_root/name).is_file():copy(a.harness_root/name,payload/'frontend/harnesses'/name)
 # No Codex homes, auth.json, logs, workspaces or user configuration are read.
 # The local installer's native runtime files carry valid OpenAI signatures.
 codex_receipt=[]
 for name in ('codex.exe','codex-exec-server.exe','codex-code-mode-host.exe'):
  file=a.codex_bin/name
  ps="Get-AuthenticodeSignature -LiteralPath '"+str(file).replace("'","''")+"' | ForEach-Object { [pscustomobject]@{status=$_.Status.ToString();subject=$_.SignerCertificate.Subject} } | ConvertTo-Json -Compress"
  signature_env={k:v for k,v in os.environ.items() if k.lower()!='psmodulepath'}
  proc=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',ps],env=signature_env,capture_output=True,check=True)
  verification=json.loads(proc.stdout.decode('utf-8-sig'))
  if verification['status']!='Valid' or 'OpenAI OpCo' not in verification['subject']:raise ValueError('codex_runtime_signature')
  copy(file,payload/'harness-runtime/codex'/name);codex_receipt.append({'name':name,'sha256':sha(file),**verification})
 # Pure Python public taskd source only. Exclude bytecode, runtime and private data.
 for file in sorted(a.taskd_app.rglob('*.py')):
  relative=file.relative_to(a.taskd_app)
  if '__pycache__' not in file.parts and (relative.parts[0] in ('taskd','anet_harnesses','anet_provider_harnesses','owned_source_product') or relative.as_posix()=='run_taskd.py'):
   copy(file,payload/'node/taskd/app'/relative)
 if a.taskd_overlay:
  for file in sorted(a.taskd_overlay.rglob('*.py')):
   if '__pycache__' not in file.parts:copy(file,payload/'node/taskd/app'/file.relative_to(a.taskd_overlay))
 # The final reviewed SDK root is authoritative for both fixed packages.
 # Remove inherited older package files before copying; an omitted old module
 # must not silently remain in the release closure after an overlay upgrade.
 for name in ('anet_harnesses','anet_provider_harnesses'):
  package=a.harness_root/name
  if not (package/'__init__.py').is_file():raise ValueError('fixed_harness_release_package_required')
  for app in (payload/'frontend/harnesses',payload/'node/taskd/app'):
   destination=app/name
   if destination.exists():
    if destination.resolve().parent!=app.resolve() or destination.name!=name:raise ValueError('bounded_harness_package_required')
    shutil.rmtree(destination)
  for file in sorted(package.rglob('*.py')):
   if '__pycache__' in file.parts:continue
   relative=file.relative_to(package)
   copy(file,payload/'frontend/harnesses'/name/relative);copy(file,payload/'node/taskd/app'/name/relative)
 for overlay in a.harness_overlay:
  packages=[overlay] if overlay.name in ('anet_harnesses','anet_provider_harnesses') else [overlay/name for name in ('anet_harnesses','anet_provider_harnesses') if (overlay/name).is_dir()]
  if not packages:raise ValueError('fixed_harness_overlay_package_required')
  for package in packages:
   for source in sorted(package.rglob('*.py')):
    if '__pycache__' in source.parts:continue
    relative=source.relative_to(package)
    copy(source,payload/'frontend/harnesses'/package.name/relative);copy(source,payload/'node/taskd/app'/package.name/relative)
 def release_closure(app):
  helper=app/'anet_harnesses/package_release.py'
  entry="import json,runpy;h=runpy.run_path("+repr(str(helper))+");print(json.dumps({'schema':h['SCHEMA'],'package_sha256':h['package_digest']()}))"
  return json.loads(subprocess.check_output([sys.executable,'-B','-c',entry]))
 closure=release_closure(payload/'frontend/harnesses')
 if closure.get('schema')!='anet-harness-release-closure/2' or closure!=release_closure(payload/'node/taskd/app'):raise ValueError('harness_release_closure_mismatch')
 # Configuration-only client modules are public Python source. No user state
 # or Claude design credentials are traversed when assembling the product.
 for name,source in [('anet_setup',a.config_cli_root),('anet_account',CLI/'anet_account'),('yaml',CLI/'yaml')]:
  if not (source/'__init__.py').is_file():raise ValueError('config_client_package_missing:'+name)
  for file in sorted(source.rglob('*.py')):
   if '__pycache__' not in file.parts:copy(file,payload/'frontend/harnesses'/name/file.relative_to(source))
 node_release={'fresh_node_verified':False,'reason':'matching_operator_release_pending'}
 if a.node_binary:
  if not a.node_release:raise ValueError('node_release_evidence_required')
  meta=json.loads(a.node_release.read_text(encoding='utf-8'))
  expected=meta.get('sha256') or meta.get('assets',{}).get('anet-windows-amd64',{}).get('sha256')
  if sha(a.node_binary)!=expected:raise ValueError('node_release_binary_mismatch')
  if meta.get('fresh_node_verified') is not True and not a.prepare_only:raise ValueError('fresh_node_compatibility_evidence_required')
  copy(a.node_binary,payload/'node/bin/anet.exe');copy(a.node_release,payload/'node/bin/desktop-release.json');node_release=meta
  for name in ('anet.exe.release.json','anet.exe.release.json.sig'):
   if (a.node_binary.parent/name).is_file():copy(a.node_binary.parent/name,payload/'node/bin'/name)
 write(payload/'frontend/install/dependencies.json',{'schema':'anet-desktop-dependencies/1','harness_release':closure,'python':{'version':'3.14.10','official_archive_sha256':PYTHON_ZIP_SHA},'codex':codex_receipt,'wheels':[{'name':p.name,'sha256':sha(p),'bytes':p.stat().st_size} for p in sorted(wheels.glob('*.whl'))],'node':node_release})
 rows=[{'path':str(p.relative_to(payload)).replace('\\','/'),'sha256':sha(p),'bytes':p.stat().st_size} for p in sorted(payload.rglob('*')) if p.is_file() and p!=payload/'payload-manifest.json']
 manifest={'schema':'anet-desktop-payload/1','release':a.release,'node_release':node_release,'harness_release':closure,'files':rows};write(payload/'payload-manifest.json',manifest)
 total=sum(x['bytes'] for x in rows)
 if total>1800000000:raise ValueError('expanded_payload_limit')
 if a.prepare_only:
  print(json.dumps({'state':'runtime_prepared','python_exe':str(payload/'python/python.exe'),'expanded_bytes':total}));return
 zip_dir=a.archive_dir or out;zip_dir.mkdir(parents=True,exist_ok=True)
 def is_runtime(row):
  path=row['path']
  return (path.startswith('python/') and not path.startswith('python/Lib/site-packages/anet_harnesses/')) or path.startswith('harness-runtime/')
 runtime_rows=[f for f in rows if is_runtime(f)];code_rows=[f for f in rows if not is_runtime(f)]
 runtime_zip=zip_dir/'runtime.zip';code_zip=zip_dir/'code.zip';cache=zip_dir/'runtime-cache.json'
 if a.frozen_runtime_archive:
  runtime_zip=frozen_runtime_archive(a.frozen_runtime_archive,runtime_rows)
  distinct_archive_outputs(runtime_zip,[code_zip,out/'PayloadPin.cs',out/('ANetSetup-'+a.release+'.exe'),out/'release.json'])
 else:
  cache_key=runtime_content_key(runtime_rows)
  previous=json.loads(cache.read_text()) if cache.exists() else {}
  if not(runtime_zip.exists() and previous.get('content_key')==cache_key and previous.get('sha256')==sha(runtime_zip)):
   with zipfile.ZipFile(runtime_zip,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
    for row in runtime_rows:z.write(payload/row['path'],row['path'])
   write(cache,{'content_key':cache_key,'sha256':sha(runtime_zip)})
 if a.runtime_archive_only:
  print(json.dumps({'state':'runtime_archive_ready','path':str(runtime_zip),'bytes':runtime_zip.stat().st_size,'sha256':sha(runtime_zip)}));return
 with zipfile.ZipFile(code_zip,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
  for row in code_rows:z.write(payload/row['path'],row['path'])
  z.write(payload/'payload-manifest.json','payload-manifest.json')
 with zipfile.ZipFile(runtime_zip) as rzip,zipfile.ZipFile(code_zip) as czip:
  names=rzip.namelist()+czip.namelist()
  if len(names)!=len(set(n.casefold() for n in names)) or len(names)!=len(rows)+1:raise ValueError('archive_paths_not_unique')
 pin=out/'PayloadPin.cs';pin.write_text('namespace ANetSetup { static class PayloadPin { public const string RuntimeSha256="'+sha(runtime_zip)+'"; public const string CodeSha256="'+sha(code_zip)+'"; } }',encoding='utf-8')
 exe=out/('ANetSetup-'+a.release+'.exe');temporary_exe=out/('.ANetSetup-build-'+uuid.uuid4().hex+'.exe');compiler=Path(os.environ['WINDIR'])/'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
 argv=[str(compiler),'/nologo','/target:winexe','/platform:x64','/optimize+','/utf8output','/r:System.Core.dll','/r:System.Web.Extensions.dll','/r:System.Drawing.dll','/r:System.Windows.Forms.dll','/r:System.IO.Compression.dll','/r:System.IO.Compression.FileSystem.dll','/resource:'+str(runtime_zip)+',runtime.zip','/resource:'+str(code_zip)+',code.zip','/out:'+str(temporary_exe),str(ROOT/'src/Setup.cs'),str(pin)]
 subprocess.run(argv,check=True)
 os.replace(temporary_exe,exe)
 receipt={'schema':'anet-desktop-build/1','version':a.release,'path':str(exe),'bytes':exe.stat().st_size,'sha256':sha(exe),'payload_bytes_expanded':total,'runtime_sha256':sha(runtime_zip),'code_sha256':sha(code_zip),'signature':{'status':'unsigned'},'node_release':node_release,'public_payload_files':len(rows)};write(out/'release.json',receipt);print(json.dumps(receipt,ensure_ascii=False))
if __name__=='__main__':main()
