"""Verification recovery must execute repairs and deliver blocked diagnostics without falsifying history."""
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from helpers import Scripted, make_engine, plan, report, tmpdir, wait_for
from test_engine import spec, state

class VerificationRecoveryTests(unittest.TestCase):
    def setUp(self):self.tmp=tmpdir()
    def blocked_engine(self):
        def work(kind,prompt,call):
            if kind=='plan':return plan([{'id':'p1','produces':['out/report.md'],'final_checks':['report']}])
            (self.work/'out').mkdir(exist_ok=True);(self.work/'out/report.md').write_text('draft')
            return report('claims_done',evidence=['out/report.md'])
        engine,out,store,self.work=make_engine(self.tmp,Scripted(work),policy={'max_verify_rounds':1})
        engine.intake_new('t1','peer','m1',spec())
        self.assertTrue(wait_for(lambda:state(store)=='completed'))
        self.assertTrue(wait_for(lambda:not any(worker.is_alive() for worker in engine.workers.values())))
        # Make a real final artifact fail after the phase completed.
        (self.work/'out/report.md').unlink()
        with store.lock('t1'):
            task=store.load('t1');task['state']='verifying';store.save(task)
        engine._verify('t1')
        self.assertEqual(state(store),'blocked')
        return engine,out,store
    def test_limit_rewinds_before_block_and_attaches_failed_observations(self):
        engine,out,store=self.blocked_engine();task=store.load('t1')
        self.assertEqual(task['phase_index'],0);self.assertEqual(task['plan']['phases'][0]['status'],'pending')
        self.assertFalse(task['plan']['phases'][0]['verified'])
        self.assertIn('report: fail',task['inbox']['feedback'][-1])
        self.assertEqual(out.sent[-1]['state'],'input-required')
        self.assertIn('report.json',out.sent[-1]['files']);self.assertIn('verification-failures.json',out.sent[-1]['files'])
        value=json.loads((store.task_dir('t1')/'report.json').read_text());self.assertTrue(value['partial']);self.assertEqual(value['checks'][0]['outcome'],'fail');self.assertIsNotNone(value['checks'][0]['observed'])
        self.assertEqual(value['block_kind'],'verification')
    def test_hint_executes_a_new_step_without_erasing_totals(self):
        engine,out,store=self.blocked_engine();before=store.load('t1')['used'].copy()
        engine.intake_message('t1','resume','{"taskd":"hint","text":"repair the missing file"}')
        self.assertTrue(wait_for(lambda:state(store)=='completed'),state(store))
        task=store.load('t1');self.assertEqual(task['used']['steps'],before['steps']+1)
        self.assertEqual(task['used']['verify_rounds'],before['verify_rounds']+1)
        self.assertEqual(task['verification_retry_base'],before['verify_rounds'])
        self.assertTrue(any('repair the missing file' in c['prompt'] for c in engine.executor.calls))
    def test_legacy_125_answer_uses_journal_and_repairs_original_task(self):
        engine,out,store=self.blocked_engine()
        with store.lock('t1'):
            task=store.load('t1');task.pop('block_kind');task.pop('verification_retry_base',None)
            task['phase_index']=len(task['plan']['phases']);task['plan']['phases'][0]['status']='done';task['inbox']['feedback']=[];store.save(task)
        before=store.load('t1')['used'].copy()
        engine.intake_message('t1','answer','{"taskd":"answer","text":"continue after the update"}')
        self.assertTrue(wait_for(lambda:state(store)=='completed'),state(store))
        task=store.load('t1');self.assertEqual(task['id'],'t1');self.assertEqual(task['used']['steps'],before['steps']+1)
        self.assertTrue(any('report: fail' in c['prompt'] for c in engine.executor.calls))
    def test_status_of_legacy_blocked_task_returns_files_without_execution(self):
        engine,out,store=self.blocked_engine();before=store.load('t1')['used'].copy()
        engine.intake_message('t1','status','{"taskd":"status"}')
        self.assertEqual(state(store),'blocked');self.assertEqual(store.load('t1')['used'],before)
        self.assertIn('report.json',out.sent[-1]['files']);self.assertIn('verification-failures.json',out.sent[-1]['files'])
    def test_retry_budget_stays_bounded_when_repairs_still_fail(self):
        engine,out,store=self.blocked_engine();before=store.load('t1')['used'].copy()
        # The phase can finish, but a final-only external check still fails.
        original=engine.run_checks
        def checks(tid,items,**kw):
            if state(store)=='verifying':
                result={'report':{'outcome':'fail','observed':{'reason':'external final condition missing'},'note':'still unavailable'}}
                store.log(tid,'checks',results=result);return result
            return original(tid,items,**kw)
        with patch.object(engine,'run_checks',side_effect=checks):
            engine.intake_message('t1','retry','{"taskd":"hint","text":"try one repair"}')
            self.assertTrue(wait_for(lambda:state(store)=='blocked' and store.load('t1')['used']['steps']>before['steps']),str(store.load('t1')['used']))
        task=store.load('t1');self.assertEqual(task['used']['verify_rounds'],before['verify_rounds']+1)
        self.assertNotEqual(task['progress']['check_state']['report'],'pass')
    def test_rewind_keeps_verified_earlier_phase(self):
        engine,out,store=self.blocked_engine();task=store.load('t1')
        task['plan']['phases']=[{'id':'p0','status':'done','verified':True,'checks':['earlier'],'final_checks':['earlier']}, {'id':'p1','status':'done','verified':True,'checks':['report'],'final_checks':['report']}, {'id':'p2','status':'done','verified':True,'checks':['later'],'final_checks':['later']}]
        engine._rewind_verification(task,{'report':{'outcome':'fail','observed':{'matches':[]}}})
        self.assertEqual(task['phase_index'],1);self.assertEqual(task['plan']['phases'][0]['status'],'done');self.assertTrue(task['plan']['phases'][0]['verified'])
        self.assertEqual([p['status'] for p in task['plan']['phases'][1:]],['pending','pending'])
    def test_one_unreadable_artifact_does_not_hide_report_and_other_files(self):
        engine,out,store=self.blocked_engine();task=store.load('t1')
        rep=engine.report(task);(self.work/'one.txt').write_text('one');(self.work/'two.txt').write_text('two')
        rep['evidence']=[{'path':'one.txt'},{'path':'two.txt'}]
        from taskd import engine as module
        original=module.sha256_file
        def sha(path):
            if Path(path).name=='one.txt':raise OSError('snapshot unavailable')
            return original(path)
        with patch.object(module,'sha256_file',side_effect=sha):files=engine._attach(task,rep)
        names=[p.name for p in files];self.assertIn('report.json',names);self.assertTrue(any(n.endswith('-two.txt') for n in names));self.assertFalse(any(n.endswith('-one.txt') for n in names))
        written=json.loads((store.task_dir('t1')/'report.json').read_text());self.assertEqual(written['attachment_errors'][0]['path'],'one.txt')

if __name__=='__main__':unittest.main(verbosity=2)
