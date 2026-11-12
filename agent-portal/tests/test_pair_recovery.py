"""Real HTTP/SQLite pairing fault injection; ANet profile/Hub and Windows are simulated."""
import contextlib, importlib.util, io, json, tempfile, threading, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError

def load(name):
    spec=importlib.util.spec_from_file_location('recovery_'+name,Path(__file__).parents[1]/(name+'.py'))
    mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod);return mod
s=load('server');m=load('connector')

class RecoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/'node';self.folder=self.root/'portal'
        s.DATA=Path(self.tmp.name)/'server';s.RATES.clear();s.init()
        self.profiles={};self.operations=[]
        s.hub_agent=lambda aid:self.profiles.get(aid,{})
        s.hub_action=lambda action,aid:self.operations.append((action,aid))
        self.http=s.ThreadingHTTPServer(('127.0.0.1',0),s.Handler)
        threading.Thread(target=self.http.serve_forever,daemon=True).start()
        self.addCleanup(self.http.server_close);self.addCleanup(self.http.shutdown)
        self.base=s.BASE='http://127.0.0.1:'+str(self.http.server_port)
        self.owner='1'*24;self.other='2'*24;self.record='3'*24;self.aid='b'+'a'*58
        with s.db() as c:
            for uid in (self.owner,self.other):
                c.execute('INSERT INTO users VALUES(?,?,?,?,?,?,?)',(uid,uid,uid,'unused','member',1,s.now()))
                c.execute('INSERT INTO sessions VALUES(?,?,?,?)',(s.digest(uid),uid,'csrf',s.now()+3600))
            c.execute('INSERT INTO agents(id,owner_id,name,description,kind,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',(self.record,self.owner,'Test device','','device',s.now(),s.now()))
        self.summary='original summary';self.readme='original readme';self.fail_restore=False
        self.args=SimpleNamespace(data_dir=self.root,portal=self.base,action='pair',anet='/fake/anet')
        self.token=self.issue()['url'].split('=')[-1]
    def request(self,path,data=None,owner=None):
        req=Request(self.base+path,data=json.dumps(data or {}).encode(),headers={'Content-Type':'application/json',**({'Cookie':'agent_portal_session='+owner,'X-CSRF-Token':'csrf'} if owner else {})})
        try:
            with urlopen(req,timeout=4) as r:return r.status,json.load(r)
        except HTTPError as e:return e.code,json.load(e)
    def issue(self,owner=None):
        code,body=self.request('/api/devices/'+self.record+'/pair',owner=owner or self.owner)
        self.assertEqual(code,200);return body
    def row(self):
        with s.db() as c:return s.one(c,'SELECT * FROM agents WHERE id=?',(self.record,))
    def status(self):return {'aid':self.aid,'hub_url':s.HUB,'hub':{'state':'registered'},'profile':{'summary':self.summary},'readme':self.readme}
    def anet_run(self,*cmd):
        if cmd[:2]==('profile','set'):
            value=cmd[cmd.index('--summary')+1]
            if self.fail_restore and value=='original summary':raise OSError('profile restore failed')
            self.summary=value;self.profiles[self.aid]={'summary':value}
        elif cmd!=('up',):raise AssertionError(cmd)
    def connect(self,action='pair',token=None):
        self.args.action=action
        with patch.object(m.getpass,'getpass',return_value=token or self.token),contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
            return m.pair_node(self.args,self.anet_run,self.status,lambda:None)
    def test_lost_complete_response_recovers_without_new_token(self):
        original=m.remote
        def lost(base,path,data,*args,**kw):
            result=original(base,path,data,*args,**kw)
            if path=='/api/pair/complete':raise TimeoutError('response lost after commit')
            return result
        with patch.object(m,'remote',side_effect=lost):state=self.connect()
        self.assertEqual(state['aid'],self.aid);self.assertIsNone(self.row()['pair_hash'])
        self.assertEqual(self.summary,'original summary');self.assertFalse((self.folder/'pair-pending.json').exists())
        self.assertEqual(original(self.base,'/api/node/info',{},state['node_token'])['agent_id'],self.record)
    def test_response_and_followup_network_loss_resume_later(self):
        original=m.remote;committed=False
        def outage(base,path,data,*args,**kw):
            nonlocal committed
            if committed:raise TimeoutError('offline')
            result=original(base,path,data,*args,**kw)
            if path=='/api/pair/complete':committed=True;raise TimeoutError('lost')
            return result
        with patch.object(m,'remote',side_effect=outage),self.assertRaises(TimeoutError):self.connect()
        self.assertFalse((self.folder/'connection.json').exists());self.assertTrue((self.folder/'pair-pending.json').exists())
        self.assertIsNone(self.row()['pair_hash']);state=self.connect('resume')
        self.assertEqual(state['agent_id'],self.record)
    def test_active_file_failure_keeps_private_pending_and_resume(self):
        original=m.write_private
        def fail(p,data):
            if p.name=='connection.json':raise OSError('disk write failed')
            original(p,data)
        with patch.object(m,'write_private',side_effect=fail),self.assertRaises(OSError):self.connect()
        pending=self.folder/'pair-pending.json';self.assertEqual(pending.stat().st_mode&0o777,0o600)
        self.assertIsNone(self.row()['pair_hash']);self.connect('resume')
        self.assertTrue((self.folder/'connection.json').exists());self.assertFalse(pending.exists())
    def test_acl_preflight_failure_does_not_consume_or_prepare(self):
        with patch.object(m,'protect_private',side_effect=RuntimeError('ACL failure')),self.assertRaisesRegex(RuntimeError,'ACL'):self.connect()
        self.assertIsNotNone(self.row()['pair_hash']);self.assertIsNone(self.row()['candidate_aid']);self.assertEqual(self.operations,[])
    def test_profile_restore_error_is_warning_not_lost_binding(self):
        self.fail_restore=True;state=self.connect();self.assertEqual(state['agent_id'],self.record)
        self.assertTrue((self.folder/'connection.json').exists());self.assertTrue((self.folder/'pair-pending.json').exists())
        self.fail_restore=False;self.connect('resume');self.assertEqual(self.summary,'original summary')
        self.assertFalse((self.folder/'pair-pending.json').exists())
    def test_repeated_pair_keeps_valid_credential_and_never_prompts(self):
        state=self.connect();self.issue()
        with patch.object(m.getpass,'getpass',side_effect=AssertionError('must not ask')):
            again=m.pair_node(self.args,self.anet_run,self.status,lambda:None)
        self.assertEqual(state['node_token'],again['node_token']);self.assertEqual(state['aid'],again['aid'])
        self.assertIsNotNone(self.row()['pair_hash'])
    def test_old_client_credential_loss_recovery_same_record_only(self):
        state=self.connect();(self.folder/'connection.json').unlink();d=self.issue();self.assertTrue(d['recovery'])
        token=d['url'].split('=')[-1]
        self.assertEqual(self.request('/api/pair/prepare',{'token':token,'aid':'b'+'c'*58})[0],409)
        self.assertEqual(m.remote(self.base,'/api/node/info',{},state['node_token'])['aid'],self.aid)
        recovered=self.connect(token=token)
        self.assertEqual(recovered['agent_id'],state['agent_id']);self.assertNotEqual(recovered['node_token'],state['node_token'])
        with self.assertRaises(m.RemoteError):m.remote(self.base,'/api/node/info',{},state['node_token'])
        with s.db() as c:self.assertEqual(c.execute('SELECT count(*) FROM agents').fetchone()[0],1)
        self.assertEqual(self.request('/api/devices/'+self.record+'/pair',owner=self.other)[0],404)
    def test_expired_recovery_never_revokes_bound_hub_identity(self):
        self.connect();token=self.issue()['url'].split('=')[-1]
        self.request('/api/pair/prepare',{'token':token,'aid':self.aid})
        with s.db() as c:c.execute('UPDATE agents SET pair_expires=?,provisioned=1 WHERE id=?',(s.now()-1,self.record))
        self.operations.clear();s.cleanup_once();self.assertEqual(self.operations,[])
        self.assertEqual(self.row()['aid'],self.aid);self.assertIsNotNone(self.row()['node_hash'])
    def test_token_expired_before_complete_renew_same_pending_record(self):
        original=m.remote
        def stop(base,path,data,*args,**kw):
            if path=='/api/pair/complete':raise TimeoutError('offline before request')
            return original(base,path,data,*args,**kw)
        with patch.object(m,'remote',side_effect=stop),self.assertRaises(TimeoutError):self.connect()
        fresh=self.issue()['url'].split('=')[-1]
        with self.assertRaisesRegex(RuntimeError,'过期或被替换'):self.connect('resume')
        self.connect(token=fresh);self.assertEqual(self.row()['aid'],self.aid)
    def test_server_commit_survives_broken_response_and_legacy_clients(self):
        self.request('/api/pair/prepare',{'token':self.token,'aid':self.aid})
        self.profiles[self.aid]={'summary':self.row()['challenge']}
        code,body=self.request('/api/pair/complete',{'token':self.token,'aid':self.aid})
        self.assertEqual(code,200);self.assertEqual(len(body['node_token']),43)
        token=self.issue()['url'].split('=')[-1];self.request('/api/pair/prepare',{'token':token,'aid':self.aid})
        self.profiles[self.aid]={'summary':self.row()['challenge']};secret='x'*43
        respond=s.Handler.respond
        def broken(handler,data,*args,**kwargs):
            if handler.path=='/api/pair/complete':raise BrokenPipeError()
            return respond(handler,data,*args,**kwargs)
        with patch.object(s.Handler,'respond',broken),self.assertRaises(Exception):self.request('/api/pair/complete',{'token':token,'aid':self.aid,'node_token':secret})
        self.assertEqual(m.remote(self.base,'/api/node/info',{},secret)['aid'],self.aid)
    def test_invalid_client_credential_does_not_consume_token(self):
        self.request('/api/pair/prepare',{'token':self.token,'aid':self.aid})
        self.profiles[self.aid]={'summary':self.row()['challenge']}
        self.assertEqual(self.request('/api/pair/complete',{'token':self.token,'aid':self.aid,'node_token':'weak'})[0],400)
        self.assertIsNotNone(self.row()['pair_hash']);self.assertIsNone(self.row()['aid'])
    def test_network_failure_on_existing_state_does_not_start_new_pair(self):
        state=self.connect()
        with patch.object(m,'remote',side_effect=TimeoutError('offline')),self.assertRaises(TimeoutError):self.connect()
        self.assertEqual(json.loads((self.folder/'connection.json').read_text())['node_token'],state['node_token'])
    def test_pending_for_another_record_refuses_replacement(self):
        original=m.remote
        def stop(base,path,data,*args,**kw):
            if path=='/api/pair/prepare':raise TimeoutError('offline')
            return original(base,path,data,*args,**kw)
        with patch.object(m,'remote',side_effect=stop),self.assertRaises(TimeoutError):self.connect()
        with s.db() as c:
            c.execute('UPDATE agents SET pair_expires=? WHERE id=?',(s.now()-1,self.record))
            c.execute('INSERT INTO agents(id,owner_id,name,description,kind,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',('4'*24,self.owner,'Duplicate','','device',s.now(),s.now()))
        code,body=self.request('/api/devices/'+('4'*24)+'/pair',owner=self.owner);self.assertEqual(code,200)
        with self.assertRaisesRegex(RuntimeError,'另一个连接'):self.connect(token=body['url'].split('=')[-1])

if __name__=='__main__':unittest.main(verbosity=2)
