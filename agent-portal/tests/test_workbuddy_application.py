"""Synthetic OAuth application setup and documented token wire contracts."""
import json
import os
from pathlib import Path
import stat
import types
import unittest
from unittest.mock import patch
import workbuddy_auth as w
import test_workbuddy_device_oauth as fixture


@unittest.skipUnless(os.name=='posix','production private application storage uses POSIX ownership')
class ApplicationHTTPTests(unittest.TestCase):
    req=fixture.DeviceOAuthTests.req
    member=fixture.DeviceOAuthTests.member
    connection=fixture.DeviceOAuthTests.connection

    def setUp(self):
        fixture.DeviceOAuthTests.setUp(self);self.config.stop();self.m.BASE=w.CALLBACK_URI.rsplit('/api/',1)[0]
        self.env=patch.dict(os.environ,{},clear=True);self.env.start();self.addCleanup(self.env.stop)
        with self.m.db() as c:
            c.execute("UPDATE users SET role='admin' WHERE username='owner'")

    def test_admin_csrf_and_private_secret_never_returned(self):
        path='/api/admin/workbuddy-app';fields={'client_id':'synthetic-client','client_secret':'synthetic-secret-value'}
        with self.m.db() as c:schema=c.execute("SELECT sql FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
        self.assertEqual(self.req(path,user=self.other)[0],403)
        self.assertEqual(self.req(path,'POST',fields,user=self.other)[0],403)
        self.assertEqual(self.req(path,'POST',fields,node=self.node)[0],401)
        self.assertEqual(self.req(path,'POST',fields,user=self.owner,csrf=False)[0],403)
        self.assertEqual(self.req(path,'POST',fields,user=self.owner,origin='https://evil.invalid')[0],403)
        self.assertEqual(self.req(path,'PATCH',fields,user=self.owner)[0],405)
        self.assertEqual(self.req(path,user=self.owner)[1]['missing'],['client_id','client_secret'])
        code,value,_=self.req(path,'POST',fields,user=self.owner);self.assertEqual(code,200)
        self.assertEqual(set(value),{'configured','client_id_present','secret_present','redirect_uri','missing'})
        self.assertTrue(value['configured']);self.assertEqual(value['redirect_uri'],w.CALLBACK_URI)
        self.assertNotIn(fields['client_secret'],json.dumps(value))
        self.assertNotIn(fields['client_id'],json.dumps(value))
        self.assertEqual(self.req(path,user=self.owner)[1],value)
        app=self.broker._application_path()
        self.assertEqual(stat.S_IMODE(app.stat().st_mode),0o600)
        self.assertEqual(stat.S_IMODE(app.parent.stat().st_mode),0o700)
        self.assertEqual(json.loads(app.read_text())['client_secret'],fields['client_secret'])
        fields['client_secret']='rotated-synthetic-secret'
        self.assertEqual(self.req(path,'POST',fields,user=self.owner)[0],200)
        self.assertEqual(json.loads(app.read_text())['client_secret'],fields['client_secret'])
        self.assertEqual(list(app.parent.glob('*.tmp')),[])
        with self.m.db() as c:self.assertEqual(c.execute("SELECT sql FROM sqlite_master WHERE type='table' ORDER BY name").fetchall(),schema)

    def test_invalid_fields_and_existing_weak_storage_are_rejected_without_repair(self):
        path='/api/admin/workbuddy-app';valid={'client_id':'synthetic-client','client_secret':'synthetic-secret'}
        for fields in ({},valid|{'redirect_uri':'https://evil.invalid'},valid|{'client_secret':'bad\nsecret'},valid|{'client_id':[]},valid|{'client_secret':' '}):
            self.assertEqual(self.req(path,'POST',fields,user=self.owner)[0],400)
        folder=self.broker._application_path().parent;folder.mkdir(mode=0o755);folder.chmod(0o755)
        self.assertEqual(self.req(path,'POST',valid,user=self.owner)[0],503)
        self.assertEqual(stat.S_IMODE(folder.stat().st_mode),0o755)
        self.assertFalse(self.broker._application_path().exists())

    def test_existing_symlink_or_shared_file_is_rejected_without_chmod(self):
        path='/api/admin/workbuddy-app';valid={'client_id':'synthetic-client','client_secret':'synthetic-secret'}
        app=self.broker._application_path();app.parent.mkdir(mode=0o700)
        outside=self.m.DATA/'external-secret.json';outside.write_text('unrelated-private-data');outside.chmod(0o600)
        app.symlink_to(outside)
        self.assertEqual(self.req(path,'POST',valid,user=self.owner)[0],503)
        self.assertEqual(outside.read_text(),'unrelated-private-data');self.assertTrue(app.is_symlink())
        app.unlink();app.symlink_to(self.m.DATA/'missing')
        self.assertEqual(self.req(path,'POST',valid,user=self.owner)[0],503);self.assertTrue(app.is_symlink())
        app.unlink();app.write_text(json.dumps(dict(schema=1,**valid)));app.chmod(0o644)
        self.assertEqual(self.req(path,'POST',valid,user=self.owner)[0],503)
        self.assertEqual(stat.S_IMODE(app.stat().st_mode),0o644)


class TokenExchangeTests(unittest.TestCase):
    def setUp(self):
        class Problem(Exception):pass
        self.Problem=Problem
        self.portal=types.SimpleNamespace(Problem=Problem,now=lambda:100)
        self.broker=w.WorkBuddy(self.portal)
        self.configuration=patch.object(self.broker,'config',return_value=('synthetic-client','synthetic-secret',w.CALLBACK_URI))
        self.configuration.start();self.addCleanup(self.configuration.stop)

    def exchange(self,value,fields=None):
        class Response:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self,n):return json.dumps(value).encode()
        with patch.object(w,'build_opener') as opener:
            opener.return_value.open.return_value=Response()
            return self.broker.exchange(fields or {'grant_type':'authorization_code','code':'synthetic-code'},w.CLOUD_SCOPES)

    def payload(self):return {'access_token':'synthetic-access','refresh_token':'synthetic-refresh','expires_in':3600,'scope':w.CLOUD_SCOPES}

    def test_documented_flat_and_legacy_envelope(self):
        for value in (self.payload(),{'code':0,'data':self.payload()}):
            self.assertEqual(self.exchange(value)['expires_at'],3700)

    def test_refresh_without_rotated_refresh_token_retains_exact_current_token(self):
        value=self.payload();value.pop('refresh_token')
        self.assertEqual(self.exchange(value,{'grant_type':'refresh_token','refresh_token':'retained-refresh'})['refresh_token'],'retained-refresh')
        with self.assertRaises(self.Problem):self.exchange(value)

    def test_missing_cloud_scope_malformed_types_or_tokens_reject_without_leaks(self):
        for delta in ({'scope':w.SCOPES},{'access_token':'synthetic-secret\n'},{'refresh_token':None},{'expires_in':True},{'expires_in':-1}):
            with self.assertRaises(self.Problem) as got:self.exchange(self.payload()|delta)
            self.assertNotIn('synthetic-secret',str(got.exception))
        for value in ([],{'code':1,'data':{'secret':'synthetic-secret'}},{'code':0,'data':[]}):
            with self.assertRaises(self.Problem):self.exchange(value)


if __name__=='__main__':unittest.main(verbosity=2)
