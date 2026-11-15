"""Actual loopback HTTP auth/CSRF/node middleware; synthetic OAuth responses only."""
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
from urllib.parse import parse_qs,urlsplit

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


class DeviceOAuthTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        spec=importlib.util.spec_from_file_location('device_oauth_portal',ROOT/'server.py')
        self.m=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.m)
        self.m.DATA=Path(self.temp.name);self.m.BASE='https://portal.invalid';self.m.init()
        self.broker=self.m.workbuddy();self.exchange=[]
        self.config=patch.object(self.broker,'config',return_value=('fixture-client','fixture-app-secret','https://portal.invalid/api/workbuddy/callback'))
        self.config.start();self.addCleanup(self.config.stop)
        p=patch.dict(os.environ,{'WORKBUDDY_CLIENT_ID':'fixture-client'});p.start();self.addCleanup(p.stop)
        self.server=self.m.ThreadingHTTPServer(('127.0.0.1',0),self.m.Handler)
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)
        self.owner=self.member('owner');self.other=self.member('other')
        self.device,self.node=self.connection(self.owner,'device','b'+'d'*58)
        self.sibling,self.sibling_node=self.connection(self.owner,'device','b'+'s'*58)
        self.foreign,self.foreign_node=self.connection(self.other,'device','b'+'f'*58)
        self.base='/api/connections/'+self.device+'/workbuddy'
        def exchange(fields,required_scope):
            self.exchange.append((fields,required_scope))
            return {'access_token':'fixture-access','refresh_token':'fixture-refresh',
                    'expires_at':self.m.now()+3600,'scope':required_scope}
        self.stub=patch.object(self.broker,'exchange',side_effect=exchange);self.stub.start();self.addCleanup(self.stub.stop)

    def req(self,path,method='GET',data=None,user=None,node=None,csrf=True,origin=None):
        headers={'Content-Type':'application/json'}
        if user:
            headers['Cookie']=user['cookie']
            if csrf:headers['X-CSRF-Token']=user['csrf']
        if node:headers['Authorization']='Bearer '+node
        if origin:headers['Origin']=origin
        conn=HTTPConnection('127.0.0.1',self.server.server_port,timeout=5)
        conn.request(method,path,body=json.dumps(data or {}) if method!='GET' else None,headers=headers)
        response=conn.getresponse();body=response.read();ctype=response.getheader('Content-Type','')
        result=response.status,json.loads(body) if ctype.startswith('application/json') else body.decode(),response.getheader('Set-Cookie')
        conn.close();return result

    def member(self,name):
        code,_,cookie=self.req('/api/auth/register','POST',{'username':name,'name':name,'password':'fixture-password-123'})
        self.assertEqual(code,201)
        user={'cookie':cookie.split(';')[0],'csrf':''}
        user['csrf']=self.req('/api/me',user=user)[1]['csrf']
        return user

    def connection(self,user,kind,aid):
        route='/api/devices' if kind=='device' else '/api/agents'
        code,record,_=self.req(route,'POST',{'name':aid[:12],'executor':'codex'},user=user)
        self.assertEqual(code,201);token='fixture-node-'+aid
        with self.m.db() as c:
            c.execute('UPDATE agents SET aid=?,node_hash=?,node_connected=1,requester_ready=1 WHERE id=?',
                      (aid,self.m.digest(token),record['id']))
        return record['id'],token

    def authorize(self):
        code,data,_=self.req(self.base+'/authorize','POST',user=self.owner)
        self.assertEqual(code,200);url=urlsplit(data['url']);query=parse_qs(url.query)
        self.assertEqual(url.netloc,'www.workbuddy.cn')
        self.assertEqual(set(query['scope'][0].split()),{'user.localassistant.readable','user.localassistant.invokable'})
        state=query['state'][0]
        code,_,_=self.req('/api/workbuddy/callback?state='+state+'&code=fixture-code')
        self.assertEqual(code,200)
        return state

    def token(self,node=None,data=None):return self.req('/api/node/workbuddy/token','POST',data=data,node=node or self.node)

    def test_owner_explicit_local_authorization_gives_only_own_node_short_token(self):
        with self.m.db() as c:before=dict(c.execute('SELECT * FROM agents WHERE id=?',(self.device,)).fetchone())
        self.assertEqual(self.token()[0],409)
        state=self.authorize();code,data,_=self.token();self.assertEqual(code,200)
        self.assertEqual(data['access_token'],'fixture-access');self.assertFalse(data['cloud_tasks_scope']);self.assertTrue(data['local_assistant_scope'])
        self.assertNotIn('refresh_token',data);self.assertNotIn('client_secret',data)
        self.assertEqual(self.token(self.sibling_node,{'connection_id':self.device})[0],409)
        self.assertEqual(self.token(self.foreign_node,{'connection_id':self.device})[0],409)
        self.assertEqual(self.req('/api/workbuddy/callback?state='+state+'&code=again')[0],400)
        self.assertEqual(len(self.exchange),1)
        with self.m.db() as c:after=dict(c.execute('SELECT * FROM agents WHERE id=?',(self.device,)).fetchone())
        self.assertEqual(after,before)

    def test_owner_csrf_origin_and_node_credentials_cannot_be_substituted(self):
        self.assertEqual(self.req(self.base+'/authorize','POST',user=self.other)[0],404)
        self.assertEqual(self.req(self.base+'/authorize','POST',user=self.owner,csrf=False)[0],403)
        self.assertEqual(self.req(self.base+'/authorize','POST',user=self.owner,origin='https://evil.invalid')[0],403)
        self.assertEqual(self.req(self.base+'/authorize','POST',node=self.node)[0],401)
        self.assertEqual(self.token('not-a-node-token')[0],401)
        self.assertEqual(self.req('/api/node/workbuddy/token','POST')[0],401)
        self.assertEqual(self.exchange,[])

    def test_changed_aid_owner_disabled_connection_and_bad_hash_reject(self):
        self.authorize()
        for bad in ('not-sha256',[],True):self.assertEqual(self.token(data={'rejected_token_sha256':bad})[0],400)
        with self.m.db() as c:c.execute('UPDATE agents SET aid=? WHERE id=?',('b'+'x'*58,self.device))
        self.assertEqual(self.token()[0],409)
        with self.m.db() as c:c.execute('UPDATE agents SET enabled=0 WHERE id=?',(self.device,))
        self.assertEqual(self.token()[0],401)

    def test_scope_marker_is_bound_to_one_use_state_and_invalid_grants_never_leak(self):
        code,data,_=self.req(self.base+'/authorize','POST',user=self.owner)
        query=parse_qs(urlsplit(data['url']).query);state=query['state'][0]
        self.assertEqual(self.req('/api/workbuddy/callback?state='+state.replace('legacy.','cloud.',1)+'&code=x')[0],400)
        self.assertEqual(self.exchange,[])
        self.assertEqual(self.req('/api/workbuddy/callback?state='+state+'&code=x')[0],200)
        with self.broker.db() as c:c.execute('UPDATE grants SET access_token=? WHERE agent_id=?',('bad\x00token',self.device))
        code,body,_=self.token();self.assertEqual(code,409);self.assertNotIn('bad',json.dumps(body))

    def test_device_revoke_keeps_connection_execution_and_admission_fields(self):
        self.authorize()
        with self.m.db() as c:
            c.execute('UPDATE agents SET executor_ready=1,policy_ok=1 WHERE id=?',(self.device,))
            before=dict(c.execute('SELECT * FROM agents WHERE id=?',(self.device,)).fetchone())
        self.assertEqual(self.req(self.base+'/revoke','POST',user=self.owner)[0],200)
        with self.m.db() as c:after=dict(c.execute('SELECT * FROM agents WHERE id=?',(self.device,)).fetchone())
        self.assertEqual(after,before);self.assertEqual(self.token()[0],409)

    def test_legacy_agent_keeps_assistant_scope_and_refresh_contract(self):
        agent,node=self.connection(self.owner,'agent','b'+'a'*58)
        with self.m.db() as c:c.execute("UPDATE agents SET executor='workbuddy' WHERE id=?",(agent,))
        base='/api/connections/'+agent+'/workbuddy'
        code,data,_=self.req(base+'/authorize','POST',user=self.owner);self.assertEqual(code,200)
        query=parse_qs(urlsplit(data['url']).query)
        self.assertEqual(set(query['scope'][0].split()),{'user.localassistant.readable','user.localassistant.invokable'})
        self.assertEqual(self.req('/api/workbuddy/callback?state='+query['state'][0]+'&code=x')[0],200)
        with self.broker.db() as c:c.execute('UPDATE grants SET expires_at=? WHERE agent_id=?',(self.m.now(),agent))
        code,data,_=self.token(node);self.assertEqual(code,200);self.assertFalse(data['cloud_tasks_scope'])
        self.assertEqual(self.exchange[-1][0]['grant_type'],'refresh_token')
        self.assertEqual(set(self.exchange[-1][1].split()),{'user.localassistant.readable','user.localassistant.invokable'})

    def test_cloud_is_explicit_and_unknown_purpose_cannot_widen_default(self):
        for purpose in ('all',[],{},True):
            self.assertEqual(self.req(self.base+'/authorize','POST',{'purpose':purpose},user=self.owner)[0],400)
        code,data,_=self.req(self.base+'/authorize','POST',{'purpose':'cloud_tasks'},user=self.owner)
        self.assertEqual(code,200);query=parse_qs(urlsplit(data['url']).query)
        self.assertEqual(set(query['scope'][0].split()),{'user.task.readable','user.task.invokable'})
        self.assertEqual(self.req('/api/workbuddy/callback?state='+query['state'][0]+'&code=x')[0],200)
        code,data,_=self.token();self.assertEqual(code,200)
        self.assertTrue(data['cloud_tasks_scope']);self.assertFalse(data['local_assistant_scope'])
        self.authorize();code,data,_=self.token();self.assertEqual(code,200)
        self.assertFalse(data['cloud_tasks_scope']);self.assertTrue(data['local_assistant_scope'])


if __name__=='__main__':unittest.main()
