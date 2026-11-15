"""Explanation metadata only; isolated HTTP fixtures never send ANet tasks."""
import json
import time
import unittest
import test_portal as helpers

m=helpers.m

def facts():
    return {'schema':1,'observed_at':time.time(),'portal_admission_configured':True,
        'binding_match':True,'live_capability_match':True,'endpoint_match':True,'token_reference_match':True,
        'reason':'configuration_matches','transport':'https','authentication':'not_probed',
        'existing_trust_count':1,'existing_allow_count':0,'public_capabilities_count':0}

class AdmissionDisplayTests(unittest.TestCase):
    def test_runtime_facts_are_bounded_and_not_credentials_or_authority(self):
        source=facts()|{'token':'must-not-appear','trusted_aids':['private-peer'],'caller_match':True,'authentication':'verified'}
        clean=m.clean_runtime({'admission':source,'node_token':'must-not-appear'})
        self.assertEqual(clean['admission']['authentication'],'not_probed')
        for forbidden in ['must-not-appear','private-peer','caller_match']:self.assertNotIn(forbidden,json.dumps(clean))
        source['existing_trust_count']=True;source['existing_allow_count']=-1
        self.assertNotIn('existing_trust_count',m.clean_admission_facts(source))
        self.assertNotIn('existing_allow_count',m.clean_admission_facts(source))
    def test_legacy_and_future_observations_do_not_gain_facts(self):
        self.assertIsNone(m.clean_admission_facts(None))
        self.assertIsNone(m.clean_admission_facts(facts()|{'observed_at':time.time()+1000}))
        self.assertIsNone(m.clean_admission_facts(facts()|{'observed_at':float('nan')}))
        self.assertNotIn('admission',m.clean_runtime({'harness':'codex'}))
    def test_owner_counts_are_not_shared(self):
        raw=json.dumps(m.clean_runtime({'admission':facts()}))
        self.assertEqual(m.runtime_view(raw,True)['admission']['existing_trust_count'],1)
        for key in ['existing_trust_count','existing_allow_count','public_capabilities_count']:
            self.assertNotIn(key,m.runtime_view(raw,False)['admission'])


class AdmissionHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):helpers.PortalTest.setUpClass.__func__(cls);m.ASSISTANT=None
    @classmethod
    def tearDownClass(cls):helpers.PortalTest.tearDownClass.__func__(cls)
    req=helpers.PortalTest.req;register=helpers.PortalTest.register
    new_agent=helpers.PortalTest.new_agent;new_device=helpers.PortalTest.new_device;pair=helpers.PortalTest.pair
    def test_policy_zero_does_not_rewrite_acl_or_enable_dispatch(self):
        setup=(m.DATA/'admin-setup-link.txt').read_text().strip().split('=')[-1]
        owner=self.register(setup,'admission-owner')
        _,invitation,_=self.req('/api/admin/invitations','POST',{'label':'Other owner'},owner)
        other=self.register(invitation['url'].split('=')[-1],'admission-other')
        eps=self.new_agent(owner,'EPS fixture');mac=self.new_device(owner,'Original caller fixture');outsider=self.new_device(other,'Unrelated caller')
        eps_aid,mac_aid,other_aid=['b'+c*58 for c in ('e','m','q')]
        eps_token=self.pair(owner,eps,eps_aid);mac_token=self.pair(owner,mac,mac_aid,'device');self.pair(other,outsider,other_aid,'device')
        runtime={'harness':'codex','admission':facts()|{'trusted_aids':['must-not-appear']}}
        self.assertEqual(self.req('/api/node/heartbeat','POST',{'connected':True,'policy_ok':False,'executor_ready':True,'runtime':runtime},node=eps_token)[0],200)
        row=next(a for a in self.req('/api/agents',user=owner)[1]['agents'] if a['id']==eps)
        self.assertFalse(row['policy_ok']);self.assertFalse(row['receive_ready']);self.assertEqual(row['status'],'policy_pending')
        self.assertTrue(row['runtime']['admission']['portal_admission_configured'])
        self.assertNotIn('must-not-appear',json.dumps(row))
        self.assertTrue(self.req('/api/node/inbound','POST',{},node=eps_token,caller=mac_aid)[1]['admit'])
        self.assertFalse(self.req('/api/node/inbound','POST',{},node=eps_token,caller=other_aid)[1]['admit'])
        self.assertFalse(self.req('/api/node/inbound','POST',{},node=mac_token,caller=eps_aid)[1]['admit'])
        with self.assertRaises(m.Problem):m.assistant().tool_propose_delegation(owner,{'agent_id':eps,'goal':'fixture, never sent'})

if __name__=='__main__':unittest.main(verbosity=2)
