"""Temporary HTTP database only; no actual node, identity, or task is changed."""
import json
import time
import unittest
from unittest.mock import patch
import test_portal as helpers

m = helpers.m


class CallerReadinessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        helpers.PortalTest.setUpClass.__func__(cls)
        m.ASSISTANT = None

    @classmethod
    def tearDownClass(cls):
        helpers.PortalTest.tearDownClass.__func__(cls)

    req = helpers.PortalTest.req
    register = helpers.PortalTest.register
    new_agent = helpers.PortalTest.new_agent
    new_device = helpers.PortalTest.new_device
    pair = helpers.PortalTest.pair

    def test_role_upgrade_preserves_sender_identity_and_receiver_authorization(self):
        token = (m.DATA/'admin-setup-link.txt').read_text().strip().split('=')[-1]
        owner = self.register(token, 'caller-owner')
        _, invitation, _ = self.req('/api/admin/invitations', 'POST', {'label':'Other owner'}, owner)
        other = self.register(invitation['url'].split('=')[-1], 'caller-other')
        caller = self.new_device(owner, 'Existing Mac sender')
        receiver = self.new_agent(owner, 'Ready receiver')
        foreign = self.new_agent(other, 'Unauthorized private receiver')
        caller_aid, receiver_aid, foreign_aid = ['b'+c*58 for c in ('u','v','w')]
        caller_token = self.pair(owner, caller, caller_aid, 'device')
        receiver_token = self.pair(owner, receiver, receiver_aid)
        foreign_token = self.pair(other, foreign, foreign_aid)
        self.req('/api/node/heartbeat', 'POST', {'connected':True,'requester_ready':True}, node=caller_token)
        for node in (receiver_token, foreign_token):
            self.req('/api/node/heartbeat', 'POST', {'connected':True,'policy_ok':True,'executor_ready':True,'executor':'codex'}, node=node)

        def view():
            with m.LOCK, m.db() as c:
                row = dict(m.one(c, 'SELECT * FROM agents WHERE id=?', (caller,)))
                return row, m.agent_view(row, 'Owner', owner['id'])
        before, device = view()
        self.assertTrue(device['send_ready']); self.assertFalse(device['receive_ready'])
        self.assertTrue(device['requester_ready'])
        self.assertFalse(self.req('/api/node/inbound','POST',{},node=caller_token,caller=receiver_aid)[1]['admit'])
        # Ownership and explicit confirmation remain mandatory for the role change.
        self.assertEqual(self.req('/api/connections/'+caller+'/enable-executor','POST',{'executor':'codex','confirm':True},other)[0],404)
        self.assertEqual(self.req('/api/connections/'+caller+'/enable-executor','POST',{'executor':'codex'},owner)[0],400)
        self.assertEqual(self.req('/api/connections/'+caller+'/enable-executor','POST',{'executor':'codex','confirm':True},owner)[0],200)
        after, upgrading = view()
        for key in ('id','aid','owner_id','node_hash','node_connected','last_seen'):
            self.assertEqual(after[key], before[key], key)
        self.assertEqual(upgrading['status'], 'policy_pending')
        self.assertTrue(upgrading['send_ready']); self.assertFalse(upgrading['receive_ready'])
        self.assertTrue(upgrading['requester_ready'])
        self.assertEqual(self.req('/api/node/info','POST',{},node=caller_token)[1]['aid'],caller_aid)
        self.assertTrue(self.req('/api/node/inbound','POST',{},node=receiver_token,caller=caller_aid)[1]['admit'])
        self.assertFalse(self.req('/api/node/inbound','POST',{},node=foreign_token,caller=caller_aid)[1]['admit'])
        # Server-side assistant proposals still refuse nonready or unauthorized targets.
        assistant = m.assistant()
        for target in (caller, foreign):
            with self.assertRaises(m.Problem):
                assistant.tool_propose_delegation(owner, {'agent_id':target,'goal':'fixture only'})
        another_device = self.new_device(owner, 'Only sends')
        self.pair(owner, another_device, 'b'+'x'*58, 'device')
        with self.assertRaises(m.Problem):
            assistant.tool_propose_delegation(owner, {'agent_id':another_device,'goal':'fixture only'})
        action = assistant.tool_propose_delegation(owner, {'agent_id':receiver,'goal':'fixture only'})
        self.req('/api/node/heartbeat','POST',{'connected':False},node=receiver_token)
        with patch('threading.Thread') as thread, self.assertRaises(m.Problem):
            assistant.decide(owner, action['action_id'], True)
        thread.assert_not_called()
        # A disconnected or disabled sender is not restored by receiving readiness.
        self.assertEqual(self.req('/api/node/heartbeat','POST',{'connected':False,'policy_ok':True,'executor_ready':True,'requester_ready':True},node=caller_token)[0],200)
        _, offline = view()
        self.assertFalse(offline['send_ready']); self.assertFalse(offline['receive_ready']); self.assertFalse(offline['requester_ready'])
        with m.LOCK, m.db() as c: c.execute('UPDATE agents SET enabled=0,node_connected=1,last_seen=?,policy_ok=1,executor_ready=1 WHERE id=?',(int(time.time()),caller))
        _, disabled = view()
        self.assertFalse(disabled['send_ready']); self.assertFalse(disabled['receive_ready'])
        self.assertFalse(self.req('/api/node/inbound','POST',{},node=foreign_token,caller=caller_aid)[1]['admit'])

    def test_flags_require_fresh_bound_enabled_connected_identity(self):
        base = dict(kind='agent',executor='codex',id='fixture',name='Fixture',description='',visibility='private',owner_id='owner',aid='b'+'y'*58,enabled=1,last_seen=int(time.time()),node_connected=1,policy_ok=0,executor_ready=0,requester_ready=1,created_at=1,capabilities=None,runtime=None,work=None)
        for change in ({'last_seen':int(time.time())-91},{'last_seen':int(time.time())+120},{'aid':None},{'enabled':0},{'node_connected':0}):
            v=m.agent_view(base|change,'Owner','owner')
            self.assertFalse(v['send_ready'],change); self.assertFalse(v['receive_ready']); self.assertFalse(v['requester_ready'])
        ready=m.agent_view(base|{'policy_ok':1,'executor_ready':1},'Owner','owner')
        self.assertTrue(ready['send_ready']); self.assertTrue(ready['receive_ready'])

    def test_actual_mac_device_shape_is_normal_sender_not_failed_receiver(self):
        row=dict(kind='device',executor='codex',id='fixture-mac',name='Mac fixture',description='',visibility='private',owner_id='owner',aid='b'+'z'*58,enabled=1,last_seen=int(time.time())-15,node_connected=1,policy_ok=0,executor_ready=0,requester_ready=1,created_at=1,capabilities=None,runtime=None,work=None)
        v=m.agent_view(row,'Owner','owner')
        self.assertEqual(v['status'],'ready'); self.assertTrue(v['send_ready']); self.assertFalse(v['receive_ready'])
        self.assertTrue(v['requester_ready']); self.assertIsNone(v['runtime']); self.assertIsNone(v['capabilities'])


if __name__=='__main__': unittest.main()
