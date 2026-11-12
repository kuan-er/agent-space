import contextlib
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import subprocess
import sys
from unittest import mock

from taskd import cli
from taskd.binding import BindingClient, BindingError, BindingService, PROTOCOL, source_metadata, workspace_queue
from taskd.relay import Relay
from taskd.requesters.codex import CodexRequester
from taskd.store import Store, atomic_json, read_json
from test_relay import API, SID


class Transport:
    def __init__(self):
        self.sent=[]; self.role='requester'
    def send_new(self,peer,text):
        self.sent.append((peer,text)); return 't1'
    def get(self,tid,history=0):
        return {'id':tid,'peer':'eps','role':self.role}
    def close(self):
        pass


class BindingTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.work=Path(self.tmp.name); self.node=self.work/'node'; self.root=self.node/'taskd'
        self.store=Store(self.root/'state'); self.api=API(self.work)
        self.adapter=CodexRequester(rpc_factory=self.api); self.transport=Transport()
        self.relay=Relay(store=self.store,transport=self.transport,adapters={'codex':self.adapter})
        self.service=BindingService(self.root,self.relay)
        self.service.last_scan=time.monotonic()+1000
        atomic_json(self.root/'status.json',{'requester':True,'requester_ready':True,'requester_binding':PROTOCOL,'ts':time.time()})
        self.stop=threading.Event()
        def worker():
            while not self.stop.wait(.005):self.service.tick()
        self.worker=threading.Thread(target=worker,name='requester-service');self.worker.start()
        self.addCleanup(self.finish)
    def finish(self):
        self.stop.set(); self.worker.join(2)
    def source(self):
        return source_metadata(SID,str(self.work))
    def command(self,kind,*extra):
        args=cli.parser().parse_args(['--data-dir',str(self.node),kind,*extra])
        output=io.StringIO()
        with mock.patch.dict('os.environ',CODEX_THREAD_ID=SID),contextlib.redirect_stdout(output):
            code=args.fn(args)
        return code,json.loads(output.getvalue())

    def test_sandboxed_client_binds_through_service_without_opening_app_server(self):
        inspect=self.adapter.inspect
        def guarded(session):
            if threading.current_thread() is not self.worker:raise PermissionError('sandbox denies socket')
            return inspect(session)
        with mock.patch.object(self.adapter,'inspect',side_effect=guarded):
            code,out=self.command('submit','--to','eps','--goal','do work','--cwd',str(self.work))
        self.assertEqual(code,0);self.assertTrue(out['auto_return']);self.assertEqual(out['binding']['state'],'verified')
        self.assertEqual(out['binding']['source'],'requester_daemon')
        rec=self.store.load_tracked('t1');self.assertEqual(rec['session'],SID);self.assertEqual(rec['cwd'],str(self.work.resolve()))
        self.assertFalse(any(c[0]=='turn/start' for c in self.api.calls),'binding must not wake a conversation')

    def test_global_ready_cannot_hide_per_source_failure_and_nothing_is_sent(self):
        with mock.patch.object(self.adapter,'inspect',side_effect=PermissionError('not permitted')):
            code,out=self.command('submit','--to','eps','--goal','do work','--cwd',str(self.work))
        self.assertEqual(code,2);self.assertFalse(out['sent']);self.assertFalse(out['auto_return'])
        self.assertFalse(self.transport.sent);self.assertIn('not permitted',out['binding']['error'])
        self.assertFalse(self.store.tracked_ids())

    def test_lightweight_run_uses_same_verified_binding_and_preserves_action(self):
        spec = self.work / 'run.json'
        spec.write_text(json.dumps({'taskd': 'run', 'goal': 'do work', 'acceptance': [
            {'id': 'result', 'type': 'files', 'pattern': 'result.txt'}]}))
        code, out = self.command('run', '--to', 'eps', '--spec', str(spec), '--cwd', str(self.work))
        self.assertEqual(code, 0)
        self.assertEqual(out['binding']['state'], 'verified')
        self.assertEqual(json.loads(self.transport.sent[0][1])['taskd'], 'run')
        self.assertEqual(self.store.load_tracked('t1')['session'], SID)

    def test_lightweight_cli_rejects_planned_spec_before_sending(self):
        spec = self.work / 'run.json'
        spec.write_text('{"taskd":"submit","goal":"do work"}')
        code, out = self.command('run', '--to', 'eps', '--spec', str(spec), '--cwd', str(self.work))
        self.assertEqual(code, 2)
        self.assertEqual(self.transport.sent, [])

    def test_wrong_source_cwd_is_rejected_before_dispatch(self):
        code,out=self.command('submit','--to','eps','--goal','do work','--cwd',str(self.node))
        self.assertEqual(code,2);self.assertFalse(self.transport.sent);self.assertFalse(out['sent'])

    def test_missing_session_fails_before_dispatch(self):
        with mock.patch.object(CodexRequester,'current_session',return_value=None):
            code,out=self.command('submit','--to','eps','--goal','do work')
        self.assertEqual(code,2);self.assertFalse(self.transport.sent);self.assertFalse(out['sent'])

    def test_binding_failure_after_send_preserves_task_id_and_prohibits_resubmission(self):
        self.transport.role='provider'
        code,out=self.command('submit','--to','eps','--goal','do work','--cwd',str(self.work))
        self.assertEqual(code,2);self.assertEqual(len(self.transport.sent),1)
        self.assertEqual(out['task_id'],'t1');self.assertTrue(out['sent']);self.assertTrue(out['do_not_resubmit'])
        self.assertFalse(out['auto_return']);self.assertFalse(self.store.tracked_ids())
        intent=read_json(Path(out['submission_intent']));self.assertEqual(intent['state'],'binding_failed')

    def test_track_requires_verified_daemon_receipt(self):
        code,out=self.command('track','t1','--cwd',str(self.work))
        self.assertEqual(code,0);self.assertEqual(out['binding']['state'],'verified')
        self.assertEqual(out['session'],SID);self.assertFalse(self.transport.sent)

    def test_no_running_service_fails_before_send(self):
        atomic_json(self.root/'status.json',{'requester':True,'requester_ready':True,'ts':time.time()})
        code,out=self.command('submit','--to','eps','--goal','do work','--cwd',str(self.work))
        self.assertEqual(code,2);self.assertFalse(self.transport.sent);self.assertIn('unavailable',out['binding']['error'])

    def test_explicit_no_callback_does_not_claim_binding(self):
        with mock.patch.object(CodexRequester,'current_session',return_value=None):
            code,out=self.command('submit','--to','eps','--goal','do work','--no-callback')
        self.assertEqual(code,0);self.assertFalse(out['auto_return']);self.assertEqual(out['binding']['state'],'disabled')
        self.assertTrue(self.store.load_tracked('t1')['callback_disabled'])

    def test_ambiguous_dispatch_is_journaled_and_not_retried(self):
        def lose_response(*args):
            self.transport.sent.append(args)
            raise EOFError('lost send response')
        with mock.patch.object(self.transport,'send_new',side_effect=lose_response):
            code,out=self.command('submit','--to','eps','--goal','do work','--cwd',str(self.work))
        self.assertEqual(code,2);self.assertIsNone(out['sent']);self.assertTrue(out['do_not_resubmit'])
        self.assertEqual(len(self.transport.sent),1)
        self.assertEqual(read_json(Path(out['submission_intent']))['state'],'dispatch_unconfirmed')

    def test_restarting_service_never_resends_uncertain_submission(self):
        import uuid
        request={'protocol':PROTOCOL,'request_id':str(uuid.uuid4()),'operation':'submit_task',
                 'created_at':'2026-11-08T00:00:00Z','expires_at':time.time()+60,
                 'payload':{'source':self.source(),'peer':'eps','spec':{'taskd':'submit','goal':'do work'}}}
        def lost(*args):
            self.transport.sent.append(args);raise EOFError('lost acknowledgement')
        with mock.patch.object(self.transport,'send_new',side_effect=lost):
            with self.assertRaises(BindingError):self.service.handle(request)
            restarted=BindingService(self.root,self.relay)
            with self.assertRaises(BindingError):restarted.handle(request)
        self.assertEqual(len(self.transport.sent),1)

    def test_source_binding_receipt_requires_actual_task_id_after_submit(self):
        handle=self.service.handle
        def wrong(request):
            result=handle(request)
            if request['operation']=='submit_task':result['task_id']=None
            return result
        with mock.patch.object(self.service,'handle',side_effect=wrong):
            code,out=self.command('submit','--to','eps','--goal','do work','--cwd',str(self.work))
        self.assertEqual(code,2);self.assertIsNone(out['sent']);self.assertTrue(out['do_not_resubmit'])
        self.assertEqual(len(self.transport.sent),1)

    def test_expired_or_unknown_command_cannot_reach_app_server(self):
        for op,expires in [('turn/start',time.time()+10),('verify_source',time.time()-1)]:
            with self.assertRaises(BindingError):self.service.handle({'protocol':PROTOCOL,'operation':op,'expires_at':expires,'payload':{'source':self.source()}})
        self.assertFalse(self.api.calls)

    def test_already_bound_task_cannot_change_source(self):
        self.relay.track('t1',client='codex',session=SID)
        other=dict(self.source(),session='different-explicit-source')
        with mock.patch.object(self.adapter,'inspect',return_value=dict(other,state='idle')):
            with self.assertRaises(BindingError):BindingClient(self.root).call('bind_task',{'task_id':'t1','source':other})
        self.assertEqual(self.store.load_tracked('t1')['session'],SID)

    def test_workspace_inbox_cannot_bind_another_project(self):
        other=self.work/'other';other.mkdir()
        (workspace_queue(self.root,other)/'inbox').mkdir(parents=True)
        project_service=BindingService(self.root,self.relay,project_queue=workspace_queue(self.root,other))
        before=len(self.api.calls)
        with self.assertRaises(BindingError):project_service.handle({'protocol':PROTOCOL,'operation':'verify_source',
            'expires_at':time.time()+10,'payload':{'source':self.source()}})
        self.assertEqual(before,len(self.api.calls))

    @unittest.skipUnless(sys.platform=='darwin','real macOS seatbelt test')
    def test_real_sandbox_without_socket_or_node_write_uses_workspace_inbox(self):
        probe=subprocess.run(['/usr/bin/sandbox-exec','-p','(version 1)(allow default)','/usr/bin/true'],capture_output=True,text=True)
        if probe.returncode and 'sandbox_apply: Operation not permitted' in probe.stderr:
            self.skipTest('host denies sandbox-exec; seatbelt integration unavailable')
        project=self.work/'project';project.mkdir();self.api.cwd=str(project.resolve())
        self.service.last_scan=0
        module_root=Path(__file__).parents[1].resolve()
        code='''import json,socket,sys
sys.path.insert(0,sys.argv[1])
from taskd.cli import main
try:
 socket.create_connection(('127.0.0.1',9),timeout=1)
 raise AssertionError('sandbox unexpectedly allowed socket')
except PermissionError:
 pass
raise SystemExit(main(['--data-dir',sys.argv[2],'submit','--to','eps','--goal','sandboxed submit','--session',sys.argv[3],'--cwd',sys.argv[4]]))
'''
        profile='(version 1)(allow default)(deny network*)(deny file-write*)(allow file-write* (subpath %s))'%json.dumps(str(project.resolve()))
        p=subprocess.run(['/usr/bin/sandbox-exec','-p',profile,sys.executable,'-c',code,str(module_root),str(self.node),SID,str(project.resolve())],capture_output=True,text=True,timeout=18)
        self.assertEqual(p.returncode,0,p.stderr)
        self.assertEqual(json.loads(p.stdout)['binding']['state'],'verified')
        self.assertEqual(json.loads(p.stdout)['task_id'],'t1')
        self.assertEqual(len(self.transport.sent),1)
        self.assertEqual(self.store.load_tracked('t1')['session'],SID)
        receipts=list(self.service.receipts.glob('*.json'))
        self.assertEqual(len(receipts),1)
        self.assertEqual(read_json(receipts[0])['inbox_scope'],'workspace')
        self.assertFalse((workspace_queue(self.root,project)/'receipts').exists())


if __name__=='__main__':unittest.main()
