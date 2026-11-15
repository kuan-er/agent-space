import hashlib
from http.client import HTTPConnection
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import desktop_release


class DesktopReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);(self.root/'desktop-downloads').mkdir()
        self.content=b'MZ'+bytes(range(256))*32
        self.name='ANetSetup-0.2.0-preview.5.exe'
        self.file=self.root/'desktop-downloads'/self.name;self.file.write_bytes(self.content)
        self.manifest={'schema':desktop_release.SCHEMA,'version':'0.2.0-preview.5',
            'channel':'preview','artifact':{'name':self.name,'bytes':len(self.content),
            'sha256':hashlib.sha256(self.content).hexdigest(),'signing':'unsigned'}}
        self.write_manifest()
        p=patch.dict(os.environ,{},clear=False);p.start();self.addCleanup(p.stop)
        os.environ.pop('PORTAL_DESKTOP_DOWNLOADS',None)
        spec=importlib.util.spec_from_file_location('desktop_test_server',ROOT/'server.py')
        self.module=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.module)
        self.module.ROOT=self.root
        self.server=self.module.ThreadingHTTPServer(('127.0.0.1',0),self.module.Handler)
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)

    def write_manifest(self):
        (self.root/'desktop-release.json').write_text(json.dumps(self.manifest),'utf-8')

    def req(self,path,method='GET',headers=None):
        conn=HTTPConnection('127.0.0.1',self.server.server_port,timeout=5)
        conn.request(method,path,headers=headers or {})
        response=conn.getresponse();data=response.read()
        result=response.status,dict(response.getheaders()),data;conn.close();return result

    def test_public_release_is_verified_and_download_stream_matches_hash(self):
        code,headers,body=self.req('/api/desktop-release')
        self.assertEqual(code,200);data=json.loads(body);self.assertTrue(data['available'])
        self.assertEqual(data['artifact']['signing'],'unsigned')
        self.assertEqual(data['windows']['amd64']['url'],desktop_release.PUBLIC_SOURCE+data['artifact']['url'])
        self.assertEqual(data['windows']['amd64']['sha256'],data['artifact']['sha256'])
        self.assertEqual(data['signature']['status'],'unsigned')
        self.assertEqual(data['source'],desktop_release.PUBLIC_SOURCE)
        code,headers,body=self.req(data['artifact']['url'])
        self.assertEqual(code,200);self.assertEqual(body,self.content)
        self.assertEqual(headers['Accept-Ranges'],'bytes')
        self.assertEqual(hashlib.sha256(body).hexdigest(),data['artifact']['sha256'])

    def test_head_does_not_send_body_and_has_same_metadata(self):
        code,headers,body=self.req('/downloads/'+self.name,'HEAD')
        self.assertEqual(code,200);self.assertEqual(body,b'')
        self.assertEqual(int(headers['Content-Length']),len(self.content))

    def test_range_resume_suffix_open_ended_and_clamped(self):
        for header,start,end in [('bytes=12-38',12,39),('bytes=-11',len(self.content)-11,len(self.content)),
                                  ('bytes=8180-',8180,len(self.content)),('bytes=8190-99999',8190,len(self.content))]:
            with self.subTest(range=header):
                code,headers,body=self.req('/downloads/'+self.name,headers={'Range':header})
                self.assertEqual(code,206);self.assertEqual(body,self.content[start:end])
                self.assertEqual(headers['Content-Range'],f'bytes {start}-{end-1}/{len(self.content)}')

    def test_bad_and_multiple_ranges_reject_without_full_file(self):
        for value in ['bytes=999999-','bytes=1-0','bytes=-0','bytes=0-1,4-5','bytes=-','random']:
            code,headers,body=self.req('/downloads/'+self.name,headers={'Range':value})
            self.assertEqual(code,416,value);self.assertEqual(body,b'')
            self.assertEqual(headers['Content-Range'],'bytes */'+str(len(self.content)))

    def test_conditional_cache_and_stale_if_range(self):
        etag='"'+self.manifest['artifact']['sha256']+'"'
        code,_,body=self.req('/downloads/'+self.name,headers={'If-None-Match':etag})
        self.assertEqual(code,304);self.assertEqual(body,b'')
        code,_,body=self.req('/downloads/'+self.name,headers={'If-Range':'"old"','Range':'bytes=1-5'})
        self.assertEqual(code,200);self.assertEqual(body,self.content)

    def test_mutation_and_arbitrary_path_have_no_file_access(self):
        self.assertEqual(self.req('/api/desktop-release','POST')[0],405)
        for path in ['/downloads/../server.py','/downloads/%2e%2e/server.py','/downloads/unknown.exe']:
            self.assertEqual(self.req(path)[0],404)

    def test_corrupt_or_missing_payload_is_not_published(self):
        self.file.write_bytes(b'XX'+self.content[2:])
        self.assertFalse(json.loads(self.req('/api/desktop-release')[2])['available'])
        self.assertEqual(self.req('/downloads/'+self.name)[0],404)
        self.file.unlink()
        self.assertFalse(desktop_release.release(self.root)['available'])

    def test_manifest_path_traversal_is_not_published(self):
        self.manifest['artifact']['name']='../server.py';self.write_manifest()
        self.assertFalse(desktop_release.release(self.root)['available'])

    def test_manifest_update_does_not_change_an_inflight_immutable_download(self):
        original=desktop_release.release(self.root)
        other='ANetSetup-0.2.0-preview.6.exe'
        (self.root/'desktop-downloads'/other).write_bytes(b'NEW-RELEASE')
        self.manifest['artifact']={'name':other,'bytes':11,'sha256':hashlib.sha256(b'NEW-RELEASE').hexdigest()}
        self.write_manifest()
        with patch.object(desktop_release,'release',return_value=original):
            code,headers,body=self.req('/downloads/'+self.name)
        self.assertEqual(code,200);self.assertEqual(body,self.content)
        self.assertIn(self.name,headers['Content-Disposition'])

    def test_capabilities_separate_claims_from_device_readiness(self):
        supplied={'schema':desktop_release.CAPABILITY_SCHEMA,'remote_task_verified':True,
            'harnesses':[{'id':'claude','adapter':'implemented','isolated_validation':'verified',
                         'live_model_validation':['untrusted'],'result_return':'verified',
                         'node_readiness':'ready','api_key':'secret-not-public'}]}
        (self.root/'harness-capabilities.json').write_text(json.dumps(supplied),'utf-8')
        code,_,raw=self.req('/api/harness-capabilities');self.assertEqual(code,200)
        data=json.loads(raw);self.assertEqual({h['id'] for h in data['harnesses']},set(desktop_release.HARNESS_NAMES))
        self.assertFalse(data['remote_task_verified']);self.assertNotIn(b'secret-not-public',raw)
        row=next(h for h in data['harnesses'] if h['id']=='claude')
        self.assertEqual(row['live_model_validation'],'not_verified')
        self.assertEqual(row['node_readiness'],'requires_local_probe')
        self.assertEqual(row['adapter'],'implemented')

    def test_config_keeps_legacy_executor_contract_and_adds_desktop_contract(self):
        code,_,body=self.req('/api/config');self.assertEqual(code,200)
        data=json.loads(body)
        self.assertEqual({h['id'] for h in data['harnesses']},{'codex','pi','workbuddy','deepseek','custom'})
        self.assertEqual(data['harness_capabilities']['schema'],desktop_release.CAPABILITY_SCHEMA)


if __name__=='__main__':unittest.main()
