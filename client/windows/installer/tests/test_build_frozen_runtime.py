"""Small archive and hardlink fixtures; never compile or execute a runtime."""
import contextlib,hashlib,importlib.util,io,json,os,tempfile,unittest,zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('build_candidate',ROOT/'build.py')
b=importlib.util.module_from_spec(spec);spec.loader.exec_module(b)

class FrozenRuntimeTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory(prefix='anet-build-small-')
  self.root=Path(self.temp.name)
  self.archive=self.root/'frozen.zip'
  with zipfile.ZipFile(self.archive,'w') as z:z.writestr('python/python.exe',b'fake runtime, never executed')
  self.rows=[{'path':'python/python.exe','bytes':len(b'fake runtime, never executed'),'sha256':hashlib.sha256(b'fake runtime, never executed').hexdigest()}]
  self.pin_stack=contextlib.ExitStack()
  self.pin_stack.enter_context(patch.object(b,'FROZEN_RUNTIME_SHA256',b.sha(self.archive)))
  self.pin_stack.enter_context(patch.object(b,'FROZEN_RUNTIME_CONTENT_KEY',b.runtime_content_key(self.rows)))
  self.pin_stack.enter_context(patch.object(b,'FROZEN_RUNTIME_BYTES',self.archive.stat().st_size))
 def tearDown(self):self.pin_stack.close();self.temp.cleanup()
 def snapshot(self,p):
  s=p.stat();return (p.read_bytes(),s.st_size,s.st_mtime_ns,s.st_ino,s.st_nlink)
 def test_defaults_select_preview3_repository_sources(self):
  a=b.build_parser().parse_args([])
  self.assertEqual(a.release,'0.3.0-preview.3')
  self.assertEqual(a.desktop_root,ROOT.parent/'desktop')
  self.assertEqual(a.harness_root,b.CLI)
  self.assertEqual(a.taskd_app,b.CLI/'taskd_app')
  self.assertIsNone(a.runtime_template)
  self.assertIsNone(a.frozen_runtime_archive)
 def test_read_only_archive_reuse_retains_bytes_identity_and_timestamp(self):
  before=self.snapshot(self.archive)
  self.assertEqual(b.frozen_runtime_archive(self.archive,self.rows),self.archive)
  self.assertEqual(self.snapshot(self.archive),before)
 def test_sha_mismatch_rejects_without_changing_frozen_archive(self):
  before=self.snapshot(self.archive)
  with patch.object(b,'FROZEN_RUNTIME_SHA256','0'*64):
   with self.assertRaisesRegex(ValueError,'frozen_runtime_archive_sha256_mismatch'):b.frozen_runtime_archive(self.archive,self.rows)
  self.assertEqual(self.snapshot(self.archive),before)
 def test_zip_entries_must_match_pinned_manifest_not_only_archive_hash(self):
  wrong=[dict(self.rows[0],bytes=self.rows[0]['bytes']+1)]
  before=self.snapshot(self.archive)
  with patch.object(b,'FROZEN_RUNTIME_CONTENT_KEY',b.runtime_content_key(wrong)):
   with self.assertRaisesRegex(ValueError,'frozen_runtime_archive_entries_mismatch'):b.frozen_runtime_archive(self.archive,wrong)
  self.assertEqual(self.snapshot(self.archive),before)
 def test_output_cannot_alias_frozen_archive_by_path_or_hardlink(self):
  alias=self.root/'output.zip';os.link(self.archive,alias)
  before=self.snapshot(self.archive)
  for output in (self.archive,alias):
   with self.assertRaisesRegex(ValueError,'frozen_runtime_output_alias_refused'):b.distinct_archive_outputs(self.archive,[output])
  self.assertEqual(self.snapshot(self.archive),before)
 def test_cross_volume_copy_requires_space_and_preserves_source(self):
  target=self.root/'payload/python.exe';before=self.snapshot(self.archive)
  with patch.object(b.os,'link',side_effect=OSError('injected cross-volume failure')),patch.object(b.shutil,'disk_usage',return_value=SimpleNamespace(free=3*1024**3)):
   b.link_runtime_file(self.archive,target)
  self.assertEqual(target.read_bytes(),self.archive.read_bytes());self.assertNotEqual(target.stat().st_ino,self.archive.stat().st_ino);self.assertEqual(self.snapshot(self.archive),before)
 def test_cross_volume_copy_refuses_low_space_before_copy(self):
  target=self.root/'payload/python.exe';before=self.snapshot(self.archive)
  with patch.object(b.os,'link',side_effect=OSError('cross-volume')),patch.object(b.shutil,'disk_usage',return_value=SimpleNamespace(free=0)),patch.object(b.shutil,'copyfile') as copy:
   with self.assertRaisesRegex(OSError,'insufficient_runtime_copy_space'):b.link_runtime_file(self.archive,target)
  copy.assert_not_called();self.assertFalse(target.exists());self.assertEqual(self.snapshot(self.archive),before)
 def test_main_refuses_archive_inside_payload_before_any_clean_or_write(self):
  build=self.root/'unsafe-build';payload=build/'payload';payload.mkdir(parents=True)
  out=build/'out';out.mkdir();(out/'sentinel').write_bytes(b'keep out');(payload/'sentinel').write_bytes(b'keep payload')
  archive=payload/'supplied-runtime.zip';archive.write_bytes(self.archive.read_bytes());before=self.snapshot(archive)
  sentinels={p:self.snapshot(p) for p in (out/'sentinel',payload/'sentinel')}
  with patch.object(b,'ROOT',build),patch('sys.argv',['build.py','--frozen-runtime-archive',str(archive)]),patch.object(b,'clean') as clean,patch.object(b,'copy') as copy,patch.object(b,'write') as write:
   with self.assertRaisesRegex(ValueError,'frozen_runtime_archive_in_payload_refused'):b.main()
  clean.assert_not_called();copy.assert_not_called();write.assert_not_called();self.assertEqual(self.snapshot(archive),before)
  for p,snapshot in sentinels.items():self.assertEqual(self.snapshot(p),snapshot)
 def test_main_refuses_payload_metadata_hardlink_before_any_clean_or_write(self):
  build=self.root/'aliased-build';payload=build/'payload';payload.mkdir(parents=True)
  out=build/'out';out.mkdir();(out/'sentinel').write_bytes(b'keep out');(payload/'sentinel').write_bytes(b'keep payload')
  metadata=payload/'payload-manifest.json';os.link(self.archive,metadata);before=self.snapshot(self.archive)
  sentinels={p:self.snapshot(p) for p in (out/'sentinel',payload/'sentinel')}
  with patch.object(b,'ROOT',build),patch('sys.argv',['build.py','--reuse-runtime','--frozen-runtime-archive',str(self.archive)]),patch.object(b,'clean') as clean,patch.object(b,'copy') as copy,patch.object(b,'write') as write:
   with self.assertRaisesRegex(ValueError,'frozen_runtime_payload_alias_refused'):b.main()
  clean.assert_not_called();copy.assert_not_called();write.assert_not_called();self.assertEqual(self.snapshot(self.archive),before)
  self.assertEqual(self.snapshot(metadata),before)
  for p,snapshot in sentinels.items():self.assertEqual(self.snapshot(p),snapshot)
 def test_main_refuses_input_anywhere_inside_output_tree_before_mutation(self):
  build=self.root/'output-tree-build';out=build/'out';out.mkdir(parents=True)
  archive=out/'read-only-input.zip';archive.write_bytes(self.archive.read_bytes());before=self.snapshot(archive)
  with patch.object(b,'ROOT',build),patch('sys.argv',['build.py','--frozen-runtime-archive',str(archive)]),patch.object(b,'clean') as clean,patch.object(b,'write') as write:
   with self.assertRaisesRegex(ValueError,'frozen_runtime_archive_in_mutable_tree_refused'):b.main()
  clean.assert_not_called();write.assert_not_called();self.assertEqual(self.snapshot(archive),before)
 def test_main_refuses_each_mutable_output_alias_and_preserves_both_sentinels(self):
  targets=['payload/frontend/install/dependencies.json','out/code.zip','out/PayloadPin.cs','out/release.json','out/runtime.zip','out/runtime-cache.json','out/ANetSetup-0.2.0-preview.7.exe']
  for index,target in enumerate(targets):
   with self.subTest(target=target):
    build=self.root/('mutable-target-'+str(index));payload=build/'payload';out=build/'out';payload.mkdir(parents=True);out.mkdir()
    (payload/'sentinel').write_bytes(b'keep payload');(out/'sentinel').write_bytes(b'keep out')
    alias=build/target;alias.parent.mkdir(parents=True,exist_ok=True);os.link(self.archive,alias)
    watched={p:self.snapshot(p) for p in (self.archive,alias,payload/'sentinel',out/'sentinel')}
    with patch.object(b,'ROOT',build),patch('sys.argv',['build.py','--reuse-runtime','--frozen-runtime-archive',str(self.archive)]),patch.object(b,'clean') as clean,patch.object(b,'copy') as copy,patch.object(b,'write') as write:
     with self.assertRaisesRegex(ValueError,'_alias_refused'):b.main()
    clean.assert_not_called();copy.assert_not_called();write.assert_not_called()
    for p,snapshot in watched.items():self.assertEqual(self.snapshot(p),snapshot)
 def exercise_main_archive_only(self,wrong_key=False):
  # Exercise the actual main archive-selection branch in a separate miniature
  # build tree. Native executables are text fixtures and never started.
  build=self.root/'tiny-build';build.mkdir()
  desktop=self.root/'tiny-desktop';sdk=self.root/'tiny-sdk';app=self.root/'tiny-app';template=self.root/'tiny-template';downloads=self.root/'tiny-downloads';out=build/'out'
  def put(path,body):path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(body)
  def row(path):return {'path':path.relative_to(template).as_posix(),'bytes':path.stat().st_size,'sha256':b.sha(path)}
  put(desktop/'out/ANetDesktop.exe',b'fake desktop')
  put(desktop/'backend-manifest.json',b'{"files":[]}')
  for name in ('install_engine.py','updater.py'):put(build/name,b'fake public code')
  put(build/'runtime_sitecustomize.py',b'# inert fixture')
  put(template/'python/python.exe',b'never execute')
  put(template/'python/python314._pth',b'python314.zip\n.\nLib/site-packages\nimport site\n')
  put(template/'python/Lib/site-packages/sitecustomize.py',b'# inert fixture')
  for name in ('codex.exe','codex-exec-server.exe','codex-code-mode-host.exe'):put(template/'harness-runtime/codex'/name,b'fake signed runtime')
  runtime_rows=[row(p) for p in sorted(template.rglob('*')) if p.is_file()]
  put(template/'payload-manifest.json',json.dumps({'files':runtime_rows}).encode())
  for name in ('anet_harnesses','anet_provider_harnesses'):put(sdk/name/'__init__.py',b'# inert package')
  for package in ('anet_setup','anet_account','yaml'):put(self.root/'cli'/package/'__init__.py',b'# inert configuration package')
  put(app/'run_taskd.py',b'# inert taskd')
  put(downloads/'python-3.14.10-embed-amd64.zip',b'inert archive')
  archive=self.root/'frozen-runtime-main.zip'
  with zipfile.ZipFile(archive,'w') as z:
   for r in runtime_rows:z.write(template/r['path'],r['path'])
  out.mkdir();put(out/'runtime.zip',b'own existing archive must not be rewritten');put(out/'runtime-cache.json',b'{"do_not_touch":true}')
  watched={p:self.snapshot(p) for p in (archive,out/'runtime.zip',out/'runtime-cache.json')}
  argv=['build.py','--desktop-root',str(desktop),'--harness-root',str(sdk),'--taskd-app',str(app),'--runtime-template',str(template),'--downloads-root',str(downloads),'--codex-bin',str(template/'harness-runtime/codex'),'--frozen-runtime-archive',str(archive),'--archive-dir',str(out),'--runtime-archive-only']
  closure=json.dumps({'schema':'anet-harness-release-closure/2','package_sha256':'fixture'}).encode()
  signature=SimpleNamespace(stdout=b'{"status":"Valid","subject":"OpenAI OpCo fixture"}')
  with contextlib.ExitStack() as stack:
   stack.enter_context(patch.object(b,'CLI',self.root/'cli'))
   for name,value in [('ROOT',build),('PYTHON_ZIP_SHA',b.sha(downloads/'python-3.14.10-embed-amd64.zip')),('FROZEN_RUNTIME_SHA256',b.sha(archive)),('FROZEN_RUNTIME_BYTES',archive.stat().st_size),('FROZEN_RUNTIME_CONTENT_KEY','0'*64 if wrong_key else b.runtime_content_key(runtime_rows))]:stack.enter_context(patch.object(b,name,value))
   stack.enter_context(patch('sys.argv',argv))
   native=stack.enter_context(patch.object(b.subprocess,'run',return_value=signature))
   closure_call=stack.enter_context(patch.object(b.subprocess,'check_output',return_value=closure))
   output=stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
   if wrong_key:
    with self.assertRaisesRegex(ValueError,'frozen_runtime_content_key_mismatch'):b.main()
   else:
    b.main();result=json.loads(output.getvalue());self.assertEqual(result['path'],str(archive));self.assertEqual(result['state'],'runtime_archive_ready')
   # Only mocked Authenticode queries and closure computations are allowed;
   # runtime-archive-only must never invoke the compiler.
   self.assertEqual(native.call_count,3);self.assertEqual(closure_call.call_count,2)
   self.assertTrue(all(call.args[0][0]=='powershell.exe' for call in native.call_args_list))
  for p,before in watched.items():self.assertEqual(self.snapshot(p),before)
  self.assertFalse((out/'code.zip').exists())
  self.assertFalse(list(build.rglob('ANetSetup-*.exe')))
 def test_main_uses_supplied_frozen_archive_without_rewriting_cache(self):self.exercise_main_archive_only()
 def test_main_content_key_mismatch_never_recompresses_or_rewrites_cache(self):self.exercise_main_archive_only(wrong_key=True)

if __name__=='__main__':unittest.main()
