"""OAuth broker HTTP authorization, token rotation and safe task recovery."""
import contextlib
from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen

ROOT = Path(__file__).parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fixture = load('wb_fixture', ROOT / 'tests/test_pair_recovery.py')
s = fixture.s
wb = load('wb_auth', ROOT / 'workbuddy_auth.py')


class BrokerHTTPTest(unittest.TestCase):
    def setUp(self):
        self.t = fixture.RecoveryTest()
        self.t.setUp()
        self.addCleanup(self.t.doCleanups)
        with s.db() as c:
            c.execute("UPDATE agents SET kind='agent',executor='workbuddy' WHERE id=?", (self.t.record,))
        self.node = self.t.connect()['node_token']
        old_base = s.BASE
        s.BASE = 'https://portal.example.test'
        self.addCleanup(setattr, s, 'BASE', old_base)
        self.secret = Path(self.t.tmp.name) / 'app-secret'
        self.secret.write_text('server-only-secret')
        self.secret.chmod(0o600)
        env = patch.dict(os.environ, {'WORKBUDDY_CLIENT_ID':'app-test', 'WORKBUDDY_CLIENT_SECRET_FILE':str(self.secret)})
        env.start()
        self.addCleanup(env.stop)
        s.WORKBUDDY = wb.WorkBuddy(s._Portal())
        self.broker = s.WORKBUDDY
        self.addCleanup(setattr, s, 'WORKBUDDY', None)
        self.path = '/api/connections/' + self.t.record + '/workbuddy'
        self.exchanges = []

    def req(self, path, method='POST', data=None, owner=None, node=None, csrf='csrf'):
        headers = {'Content-Type':'application/json'}
        if owner:
            headers.update(Cookie='agent_portal_session='+owner, **{'X-CSRF-Token':csrf})
        if node:
            headers['Authorization'] = 'Bearer ' + node
        req = Request(self.t.base+path, data=json.dumps(data or {}).encode() if method!='GET' else None, headers=headers, method=method)
        try:
            with urlopen(req, timeout=4) as response:
                body = response.read()
                return response.status, json.loads(body) if response.headers.get_content_type()=='application/json' else body.decode()
        except HTTPError as error:
            with error:
                return error.code, json.load(error)

    def exchange(self, fields):
        self.exchanges.append(fields)
        n = len(self.exchanges)
        return {'access_token':'access-'+str(n), 'refresh_token':'refresh-'+str(n), 'expires_at':s.now()+4200, 'scope':wb.SCOPES}

    def authorize(self):
        code, value = self.req(self.path+'/authorize', owner=self.t.owner)
        self.assertEqual(code, 200)
        query = parse_qs(urlsplit(value['url']).query)
        self.assertEqual(query['redirect_uri'], [s.BASE+'/api/workbuddy/callback'])
        self.assertEqual(query['scope'], [wb.SCOPES])
        self.assertNotIn('server-only-secret', value['url'])
        callback = '/api/workbuddy/callback?' + urlencode({'state':query['state'][0], 'code':'one-use-code'})
        with patch.object(self.broker, 'exchange', side_effect=self.exchange):
            self.assertEqual(self.req(callback, 'GET')[0], 200)
        return callback

    def test_owner_csrf_and_node_boundaries(self):
        self.assertEqual(self.req(self.path, 'GET')[0], 401)
        self.assertEqual(self.req(self.path+'/authorize', owner=self.t.other)[0], 404)
        self.assertEqual(self.req(self.path+'/authorize', owner=self.t.owner, csrf='wrong')[0], 403)
        self.assertEqual(self.req('/api/node/workbuddy/token', node='wrong')[0], 401)
        self.assertEqual(self.req('/api/node/workbuddy/token', node=self.node)[0], 409)
        self.assertEqual(self.req(self.path+'/revoke', owner=self.t.other)[0], 404)

    def test_callback_one_use_and_private_status(self):
        callback = self.authorize()
        with patch.object(self.broker, 'exchange', side_effect=AssertionError('must not exchange twice')):
            self.assertEqual(self.req(callback, 'GET')[0], 400)
        status = self.req(self.path, 'GET', owner=self.t.owner)[1]
        self.assertTrue(status['authorized'])
        self.assertNotIn('access-', json.dumps(status))
        self.assertNotIn('refresh-', json.dumps(status))
        code, token = self.req('/api/node/workbuddy/token', node=self.node)
        self.assertEqual((code, token['access_token']), (200, 'access-1'))
        self.assertEqual(set(token), {'access_token','expires_at'})
        self.assertEqual((s.DATA/'workbuddy/oauth.sqlite3').stat().st_mode&0o777, 0o600)

    def test_state_expiry_replacement_and_changed_binding(self):
        first = self.req(self.path+'/authorize', owner=self.t.owner)[1]['url']
        second = self.req(self.path+'/authorize', owner=self.t.owner)[1]['url']
        def callback(url):
            state = parse_qs(urlsplit(url).query)['state'][0]
            return '/api/workbuddy/callback?'+urlencode({'state':state,'code':'x'})
        self.assertEqual(self.req(callback(first),'GET')[0], 400)
        with s.db() as c:
            c.execute('UPDATE agents SET aid=? WHERE id=?', ('b'+'c'*58,self.t.record))
        with patch.object(self.broker,'exchange',side_effect=AssertionError('changed identity')):
            self.assertEqual(self.req(callback(second),'GET')[0],409)
        with self.broker.db() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM grants').fetchone()[0],0)

    def test_expired_state_and_denied_authorization_do_not_exchange(self):
        for expired in (True,False):
            url=self.req(self.path+'/authorize',owner=self.t.owner)[1]['url']
            state=parse_qs(urlsplit(url).query)['state'][0]
            if expired:
                with self.broker.db() as c:c.execute('UPDATE states SET expires=0')
            query={'state':state, 'error':'access_denied'}
            with patch.object(self.broker,'exchange',side_effect=AssertionError('no exchange')):
                self.assertEqual(self.req('/api/workbuddy/callback?'+urlencode(query),'GET')[0],400)

    def test_concurrent_expiry_refresh_once_and_rotates_both_tokens(self):
        self.authorize()
        with self.broker.db() as c:c.execute('UPDATE grants SET expires_at=0')
        with patch.object(self.broker,'exchange',side_effect=self.exchange):
            with ThreadPoolExecutor(max_workers=6) as pool:
                results=list(pool.map(lambda _:self.req('/api/node/workbuddy/token',node=self.node),range(6)))
        self.assertTrue(all(code==200 and body['access_token']=='access-2' for code,body in results))
        self.assertEqual(len(self.exchanges),2)
        self.assertEqual(self.exchanges[1],{'grant_type':'refresh_token','refresh_token':'refresh-1'})
        with self.broker.db() as c:
            row=c.execute('SELECT * FROM grants').fetchone()
        self.assertEqual(row['refresh_token'],'refresh-2')
        self.assertGreater(row['expires_at'],s.now()+4000)

    def test_stale_401_hash_does_not_rotate_again(self):
        self.authorize()
        digest=hashlib.sha256(b'access-1').hexdigest()
        with patch.object(self.broker,'exchange',side_effect=self.exchange):
            for _ in range(2):
                code,body=self.req('/api/node/workbuddy/token',node=self.node,data={'rejected_token_sha256':digest})
                self.assertEqual((code,body['access_token']),(200,'access-2'))
        self.assertEqual(len(self.exchanges),2)

    def test_refresh_failure_requires_reauthorization_without_replaying(self):
        self.authorize()
        with self.broker.db() as c:c.execute('UPDATE grants SET expires_at=0')
        with patch.object(self.broker,'exchange',side_effect=s.Problem('续期结果未确认',502)) as exchange:
            self.assertEqual(self.req('/api/node/workbuddy/token',node=self.node)[0],502)
            self.assertEqual(self.req('/api/node/workbuddy/token',node=self.node)[0],409)
            self.assertEqual(exchange.call_count,1)
        self.assertEqual(self.req(self.path,'GET',owner=self.t.owner)[1]['state'],'reauthorize')

    def test_revoke_disconnect_and_heartbeat(self):
        self.authorize()
        self.assertEqual(self.req(self.path+'/revoke',owner=self.t.owner)[0],200)
        self.assertEqual(self.req('/api/node/workbuddy/token',node=self.node)[0],409)
        self.req('/api/node/heartbeat',node=self.node,data={'connected':True,'executor':'workbuddy','executor_ready':True,'policy_ok':True,'runtime':{'workbuddy_auth':'portal'}})
        self.assertFalse(self.t.row()['executor_ready'])
        self.authorize()
        self.assertEqual(self.req('/api/agents/'+self.t.record+'/disconnect',owner=self.t.owner)[0],200)
        with self.broker.db() as c:self.assertEqual(c.execute('SELECT count(*) FROM grants').fetchone()[0],0)

    def test_missing_or_public_secret_is_explicit_and_never_leaked(self):
        self.secret.chmod(0o644)
        status=self.req(self.path,'GET',owner=self.t.owner)[1]
        self.assertFalse(status['configured'])
        code,body=self.req(self.path+'/authorize',owner=self.t.owner)
        self.assertEqual(code,503)
        self.assertNotIn('server-only-secret',json.dumps(body))

    def test_exchange_uses_server_secret_and_actual_expiry(self):
        reply={'code':0,'data':{'access_token':'a','refresh_token':'r','expires_in':3210,'scope':wb.SCOPES}}
        class Response(io.BytesIO):
            def __enter__(self):return self
            def __exit__(self,*args):self.close()
        class Opener:
            def open(inner,req,timeout):
                self.assertEqual(req.full_url,wb.ORIGIN+'/openapi/v2/token')
                fields=parse_qs(req.data.decode())
                self.assertEqual(fields['client_secret'],['server-only-secret'])
                self.assertEqual(fields['grant_type'],['refresh_token'])
                return Response(json.dumps(reply).encode())
        with patch.object(wb,'build_opener',return_value=Opener()):
            value=self.broker.exchange({'grant_type':'refresh_token','refresh_token':'r0'})
        self.assertAlmostEqual(value['expires_at']-s.now(),3210,delta=1)
        reply['data']['scope']='user.localassistant.readable'
        with patch.object(wb,'build_opener',return_value=Opener()),self.assertRaises(s.Problem):
            self.broker.exchange({'grant_type':'refresh_token','refresh_token':'r0'})


class BridgeRenewalTest(unittest.TestCase):
    def setUp(self):
        pi=load('wb_pi',ROOT/'pi_bridge.py')
        with patch.dict(sys.modules,{'pi_bridge':pi}):self.m=load('wb_bridge',ROOT/'harness_bridge.py')
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);connection=self.root/'connection.json'
        connection.write_text(json.dumps({'portal':'https://portal.example.test','node_token':'node-private'}))
        self.settings={'workbuddy_auth':'portal','connection_file':str(connection),'timeout':1}
        self.sent=[];self.message=''

    def provider(self,token,path,data=None,timeout=15):
        if path=='/localassistant':return {'online':True}
        if data:
            self.sent.append(data);self.message=data['content'];return {'message_id':'message-1'}
        marker=re.search(r'AGENT_SPACE_[a-f0-9]+_BEGIN',self.message)[0]
        return {'messages':[{'role':'assistant','content':[marker+'\nresult\n'+marker.replace('_BEGIN','_END')]}]}

    def test_poll_401_refreshes_and_reads_without_duplicate_send(self):
        expired=False
        def provider(*args,**kwargs):
            nonlocal expired
            if '?' in args[1] and not expired:
                expired=True;raise self.m.WorkBuddyUnauthorized('expired')
            return self.provider(*args,**kwargs)
        def tokens(path,rejected=None,timeout=15):return {'access_token':'new' if rejected else 'old','expires_at':int(time.time())+3600}
        with patch.object(self.m,'workbuddy_broker_token',side_effect=tokens) as broker,patch.object(self.m,'workbuddy_request',side_effect=provider):
            self.assertEqual(self.m.run_workbuddy(self.settings,'/work','task',self.root),'result')
        self.assertEqual(broker.call_count,2)
        self.assertEqual(len(self.sent),1)
        self.assertFalse((self.root/'workbuddy-pending.json').exists())

    def test_send_401_is_never_replayed_and_keeps_pending(self):
        def provider(*args,**kwargs):
            if len(args)>2 and args[2] is not None:
                self.sent.append(args[2]);raise self.m.WorkBuddyUnauthorized('expired')
            return self.provider(*args,**kwargs)
        with patch.object(self.m,'workbuddy_broker_token',return_value={'access_token':'old','expires_at':int(time.time())+3600}),patch.object(self.m,'workbuddy_request',side_effect=provider):
            with self.assertRaisesRegex(RuntimeError,'不会自动重发'):self.m.run_workbuddy(self.settings,'/work','task',self.root)
            with self.assertRaisesRegex(RuntimeError,'未确认'):self.m.run_workbuddy(self.settings,'/work','task',self.root)
        self.assertEqual(len(self.sent),1)
        self.assertTrue((self.root/'workbuddy-pending.json').exists())

    def test_broker_rejects_http_and_redirect_without_leaking_node_token(self):
        connection=Path(self.settings['connection_file'])
        connection.write_text(json.dumps({'portal':'http://evil.test','node_token':'node-private'}))
        with patch.object(self.m,'build_opener',side_effect=AssertionError('must not connect')),self.assertRaisesRegex(RuntimeError,'授权服务') as raised:
            self.m.workbuddy_broker_token(connection)
        self.assertNotIn('node-private',str(raised.exception))


if __name__=='__main__':unittest.main()
