import json
from pathlib import Path
import threading
import time
import unittest

from helpers import Scripted, make_engine, plan, report, tmpdir, wait_for
from taskd.executors.base import ExecResult


def spec(**kw):
    s = {'taskd': 'submit', 'goal': '生成报告', 'acceptance': [
        {'id': 'report', 'type': 'files', 'pattern': 'out/report.md'}]}
    s.update(kw)
    return json.dumps(s, ensure_ascii=False)


def state(store, tid='t1'):
    t = store.load(tid)
    return t and t['state']


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tmpdir()

    def run_task(self, fn, reviewer=None, text=None, policy=None, resume=True, schema=True, **config):
        ex = Scripted(fn, resume=resume, schema=schema)
        e, out, store, work = make_engine(self.tmp, ex, reviewer, policy, **config)
        self.work = work
        e.intake_new('t1', 'peer-a', 'm1', text or spec())
        return e, ex, out, store

    def test_happy_path(self):
        def fn(kind, prompt, call):
            if kind == 'plan':
                return plan([{'checks': ['report']}])
            (self.work / 'out').mkdir(exist_ok=True)
            (self.work / 'out' / 'report.md').write_text('# done')
            return report('claims_done', evidence=['out/report.md'])
        e, ex, out, store = self.run_task(fn)
        self.assertTrue(wait_for(lambda: state(store) == 'completed'), state(store))
        self.assertEqual(ex.kinds(), ['plan', 'step'])
        self.assertEqual(out.states(), ['working', 'working', 'completed'])
        final = out.sent[-1]
        self.assertEqual(final['files'][0], 'report.json')
        self.assertTrue(any(n.endswith('-report.md') for n in final['files']))
        self.assertIn('任务已完成', final['text'])
        rep = json.loads((store.task_dir('t1') / 'report.json').read_text())
        self.assertEqual(rep['checks'][0]['outcome'], 'pass')
        self.assertEqual(rep['checks'][0]['observed_by'], 'executor_node')
        self.assertEqual(rep['evidence'][0]['path'], 'out/report.md')
        # The goal written by the requester is fenced as data in every prompt.
        self.assertIn('<<<GOAL\n生成报告\nGOAL>>>', ex.calls[0]['prompt'])

    def test_rejection(self):
        ex = Scripted(lambda *a: {})
        e, out, store, work = make_engine(self.tmp, ex)
        self.assertIsNone(e.intake_new('t1', 'p', 'm1', spec(acceptance=[{'id': 'x', 'type': 'magic'}])))
        self.assertEqual(out.states(), ['rejected'])
        self.assertEqual(out.sent[0]['env']['reason'], 'checker_unavailable')
        self.assertIsNone(store.load('t1'))

    def test_peer_policy(self):
        ex = Scripted(lambda *a: {})
        e, out, store, work = make_engine(self.tmp, ex, accept_peers=['friend'])
        self.assertIsNone(e.intake_new('t1', 'stranger', 'm1', spec()))
        self.assertEqual(out.sent[0]['env']['reason'], 'peer_not_accepted')

    def test_plan_needing_approval_waits(self):
        def fn(kind, prompt, call):
            if kind == 'plan':
                return plan([{'checks': ['report']}], reasons=['needs a paid API'])
            (self.work / 'out').mkdir(exist_ok=True)
            (self.work / 'out' / 'report.md').write_text('x')
            return report('claims_done', evidence=['out/report.md'])
        e, ex, out, store = self.run_task(fn)
        self.assertTrue(wait_for(lambda: state(store) == 'awaiting_approval'))
        self.assertEqual(out.states()[-1], 'input-required')
        self.assertIn('needs a paid API', out.sent[-1]['text'])
        time.sleep(0.2)
        self.assertEqual(ex.kinds(), ['plan'])
        e.intake_message('t1', 'm2', '{"taskd":"approve_plan"}')
        self.assertTrue(wait_for(lambda: state(store) == 'completed'))

    def test_plans_run_without_asking_by_default(self):
        def fn(kind, prompt, call):
            if kind == 'plan':
                return plan([{'checks': ['report']}])
            (self.work / 'out').mkdir(exist_ok=True)
            (self.work / 'out' / 'report.md').write_text('x')
            return report('claims_done', evidence=['out/report.md'])
        e, ex, out, store = self.run_task(fn)
        self.assertTrue(wait_for(lambda: state(store) == 'completed'))
        self.assertNotIn('input-required', out.states())

    def test_failed_check_is_fed_back(self):
        def fn(kind, prompt, call):
            if kind == 'plan':
                return plan([{'checks': ['report']}])
            if call['n'] == 1:
                return report('claims_done', summary='I wrote it (I did not)')
            self.assertIn('Final verification failed', prompt)
            self.assertIn('report: fail', prompt)
            (self.work / 'out').mkdir(exist_ok=True)
            (self.work / 'out' / 'report.md').write_text('x')
            return report('claims_done', evidence=['out/report.md'])
        e, ex, out, store = self.run_task(fn)
        self.assertTrue(wait_for(lambda: state(store) == 'completed'))
        self.assertEqual(ex.kinds(), ['plan', 'step', 'step'])

    def test_no_progress_replans_then_blocks(self):
        def fn(kind, prompt, call):
            if kind == 'plan':
                return plan([{'checks': ['report']}])
            return report('working', summary='clicked again')
        e, ex, out, store = self.run_task(fn, policy={'no_progress_steps': 2, 'max_replans': 1})
        self.assertTrue(wait_for(lambda: state(store) == 'blocked'), state(store))
        kinds = ex.kinds()
        self.assertEqual(kinds, ['plan', 'step', 'step', 'plan', 'step', 'step'])
        replan_prompt = ex.calls[3]['prompt']
        self.assertIn('## Re-plan', replan_prompt)
        self.assertTrue(wait_for(lambda: out.states()[-1] == 'input-required'))
        self.assertIn('没有新的进展', out.sent[-1]['text'])

    def test_new_files_alone_are_not_acceptance_progress(self):
        n = {'i': 0}

        def fn(kind, prompt, call):
            if kind == 'plan':
                return plan([{'checks': ['report']}])
            n['i'] += 1
            (self.work / 'run.log').write_text('log %d' % n['i'])  # noise: changes every time
            if n['i'] <= 3:
                (self.work / ('finding%d.txt' % n['i'])).write_text('fact %d' % n['i'])
                return report('working', evidence=['finding%d.txt' % n['i'], 'run.log'])
            return report('working', evidence=['run.log'])
        e, ex, out, store = self.run_task(fn, policy={'no_progress_steps': 2, 'max_replans': 0})
        self.assertTrue(wait_for(lambda: state(store) == 'blocked'))
        self.assertEqual(ex.kinds().count('step'), 2)  # new files never reset the stagnation budget

    def test_needs_user_and_answer(self):
        def fn(kind, prompt, call):
            if kind == 'plan':
                return plan([{'checks': ['report']}])
            if call['n'] == 1:
                return report('needs_user', question='用哪个配置文件?')
            self.assertIn('<<<ANSWER\n用 B\nANSWER>>>', prompt)
            (self.work / 'out').mkdir(exist_ok=True)
            (self.work / 'out' / 'report.md').write_text('x')
            return report('claims_done', evidence=['out/report.md'])
        e, ex, out, store = self.run_task(fn)
        self.assertTrue(wait_for(lambda: state(store) == 'needs_user'))
        self.assertIn('用哪个配置文件', out.sent[-1]['text'])
        e.intake_message('t1', 'm2', '用 B')  # plain text answers the pending question
        self.assertTrue(wait_for(lambda: state(store) == 'completed'))

    def test_cancel_kills_the_running_step(self):
        started = threading.Event()

        def fn(kind, prompt, call):
            if kind == 'plan':
                return plan([{'checks': ['report']}])
            started.set()
            for _ in range(200):
                if e.cancel_events.get('t1') and e.cancel_events['t1'].is_set():
                    return ExecResult(False, '', None, 'canceled', canceled=True)
                time.sleep(0.02)
            return report()
        ex = Scripted(fn)
        e, out, store, work = make_engine(self.tmp, ex)
        e.intake_new('t1', 'p', 'm1', spec())
        self.assertTrue(started.wait(5))
        e.intake_message('t1', 'm2', '{"taskd":"cancel"}')
        self.assertTrue(wait_for(lambda: state(store) == 'stopped'))
        self.assertEqual(store.load('t1')['stop_reason'], 'canceled')
        self.assertEqual(out.states()[-1], 'canceled')

    def test_step_budget(self):
        def fn(kind, prompt, call):
            if kind == 'plan':
                return plan([{'checks': ['report']}])
            (self.work / ('f%d' % call['n'])).write_text(str(call['n']))
            return report('working', evidence=['f%d' % call['n']])
        e, ex, out, store = self.run_task(fn, text=spec(budget={'max_steps': 3}))
        self.assertTrue(wait_for(lambda: state(store) == 'stopped'))
        self.assertEqual(store.load('t1')['stop_reason'], 'budget_steps')
        self.assertEqual(out.states()[-2:], ['working', 'failed'])
        self.assertEqual(out.sent[-2]['files'][0], 'report.json')  # failed carries no files, so it went first

    def test_sessions_resume_within_a_phase_and_start_fresh_between(self):
        def fn(kind, prompt, call):
            if kind == 'plan':
                return plan([{'id': 'p1', 'checks': ['a']}, {'id': 'p2', 'checks': ['b']}])
            n = call['n']
            if n == 1:
                (self.work / 'note1').write_text('1')
                return report('working', evidence=['note1'])
            if n == 2:
                self.assertTrue(prompt.startswith('Step 3. Continue'))
                (self.work / 'a.txt').write_text('a')
                return report('claims_done', evidence=['a.txt'])
            self.assertIn('## Checkpoint', prompt)
            (self.work / 'b.txt').write_text('b')
            return report('claims_done', evidence=['b.txt'])
        text = spec(acceptance=[{'id': 'a', 'type': 'files', 'pattern': 'a.txt'},
                                {'id': 'b', 'type': 'files', 'pattern': 'b.txt'}])
        e, ex, out, store = self.run_task(fn, text=text)
        self.assertTrue(wait_for(lambda: state(store) == 'completed'))
        sessions = [c['session'] for c in ex.calls]
        self.assertEqual(sessions[0], None)        # plan: its own session
        self.assertIsNone(sessions[1])             # phase 1 starts fresh
        self.assertEqual(sessions[2], 's2')        # and resumes within the phase
        self.assertIsNone(sessions[3])             # phase 2 starts fresh again

    def test_executor_without_resume_or_schema(self):
        def fn(kind, prompt, call):
            self.assertIsNone(call['session'])
            self.assertIn('Output ONLY one JSON object', prompt)
            if kind == 'plan':
                return 'Here is the plan: ' + json.dumps(plan([{'checks': ['report']}]))
            (self.work / 'out').mkdir(exist_ok=True)
            (self.work / 'out' / 'report.md').write_text('x')
            return report('claims_done', evidence=['out/report.md'])
        e, ex, out, store = self.run_task(fn, resume=False, schema=False)
        self.assertTrue(wait_for(lambda: state(store) == 'completed'))

    def test_invalid_report_is_repaired_once(self):
        def fn(kind, prompt, call):
            if kind == 'plan':
                return plan([{'checks': ['report']}])
            if call['n'] == 1:
                return 'I did a lot of work!'
            self.assertIn('could not be used', prompt)
            self.assertEqual(call['session'], 's2')
            (self.work / 'out').mkdir(exist_ok=True)
            (self.work / 'out' / 'report.md').write_text('x')
            return report('claims_done', evidence=['out/report.md'])
        e, ex, out, store = self.run_task(fn)
        self.assertTrue(wait_for(lambda: state(store) == 'completed'))

    def test_blocked_needs_observed_evidence(self):
        def fn(kind, prompt, call):
            if kind == 'plan':
                return plan([{'checks': ['report']}])
            if call['n'] == 1:
                return report('blocked', blocker='license missing')
            if call['n'] > 2:
                if call['n'] == 3:
                    self.assertIn('license fixed, retry', prompt)
                return report('working')
            self.assertIn('without an observed fact backed by an evidence file', prompt)
            (self.work / 'error.txt').write_text('License server unreachable')
            return report('blocked', blocker='license server down', evidence=['error.txt'],
                          facts=[{'statement': 'the app shows a license error', 'basis': 'observed', 'evidence': ['error.txt']}])
        e, ex, out, store = self.run_task(fn)
        self.assertTrue(wait_for(lambda: state(store) == 'blocked'))
        self.assertIn('license server down', store.load('t1')['block_reason'])
        e.intake_message('t1', 'm2', '{"taskd":"hint","text":"license fixed, retry"}')
        self.assertTrue(wait_for(lambda: state(store) in ('running', 'blocked') and len(ex.calls) > 3))

    def test_reviewer_rejects_a_claimed_blocker(self):
        def fn(kind, prompt, call):
            if kind == 'plan':
                return plan([{'checks': [], 'produces': ['out/report.md'], 'final_checks': ['report']}])
            if call['n'] == 1:
                (self.work / 'err.txt').write_text('popup')
                return report('blocked', blocker='a dialog', evidence=['err.txt'],
                              facts=[{'statement': 'a dialog', 'basis': 'observed', 'evidence': ['err.txt']}])
            self.assertIn('does not accept the blocker', prompt)
            (self.work / 'out').mkdir(exist_ok=True)
            (self.work / 'out' / 'report.md').write_text('x')
            return report('claims_done', evidence=['out/report.md'])
        reviewer = Scripted(lambda k, p, c: {'verdict': 'unsupported', 'reasons': 'the dialog has a close button', 'missing': []})
        e, ex, out, store = self.run_task(fn, reviewer=reviewer)
        self.assertTrue(wait_for(lambda: state(store) == 'completed'))
        self.assertTrue(all(c['mode'] == 'review' for c in reviewer.calls))

    def test_natural_items_become_reviews_and_are_deferred_without_reviewer(self):
        def fn(kind, prompt, call):
            if kind == 'plan':
                return plan([{'checks': ['report']}])  # the executor drafted nothing for a2
            (self.work / 'out').mkdir(exist_ok=True)
            (self.work / 'out' / 'report.md').write_text('x')
            return report('claims_done', evidence=['out/report.md'])
        text = spec(acceptance=[{'id': 'report', 'type': 'files', 'pattern': 'out/report.md'}, '报告解释了原因'])
        e, ex, out, store = self.run_task(fn, text=text)
        self.assertTrue(wait_for(lambda: state(store) == 'completed'))
        rep = json.loads((store.task_dir('t1') / 'report.json').read_text())
        self.assertEqual(rep['deferred'], ['r_a2'])
        self.assertEqual(rep['drafted_checks'], ['r_a2'])
        self.assertIn('只能由你确认', out.sent[-1]['text'])

    def test_review_of_a_check_judges_the_question_and_is_reused(self):
        """Live finding: at verification the latest step's note was about another phase, and the reviewer
        rejected a true answer. A check's review decides the question on the evidence; the same evidence
        gets one verdict."""
        def fn(kind, prompt, call):
            if kind == 'plan':
                return plan([{'id': 'p1', 'produces': ['hello.txt'], 'final_checks': ['hello', 'r_a2']}, {'id': 'p2', 'produces': ['other.txt'], 'final_checks': ['other']}])
            if 'Phase p1' in prompt:
                (self.work / 'hello.txt').write_text('hello\n')
                return report('claims_done', evidence=['hello.txt'], summary='wrote hello.txt')
            (self.work / 'other.txt').write_text('x')
            return report('claims_done', evidence=['other.txt'], summary='wrote other.txt')
        reviews = []

        def review(kind, prompt, call):
            reviews.append(prompt)
            self.assertIn('Decide the question on the evidence alone', prompt)
            return {'verdict': 'supported', 'reasons': 'hello.txt is hello', 'missing': []}
        text = spec(acceptance=[{'id': 'hello', 'type': 'files', 'pattern': 'hello.txt'}, 'hello.txt says hello',
                                {'id': 'other', 'type': 'files', 'pattern': 'other.txt'}])
        e, ex, out, store = self.run_task(fn, reviewer=Scripted(review), text=text)
        self.assertTrue(wait_for(lambda: state(store) == 'completed'), state(store))
        self.assertEqual(len(reviews), 1)
        self.assertIn('wrote hello.txt', reviews[0])  # the note of the phase that owns the check

    def test_drafted_checks_are_validated(self):
        def fn(kind, prompt, call):
            if kind == 'plan':
                if call['n'] == 0:
                    return plan([{'checks': ['ghost']}])
                self.assertIn('previous plan was not usable', prompt)
                return plan([{'checks': ['d1']}], drafted=[{'id': 'd1', 'type': 'value', 'covers': 'a1',
                                                            'params_json': json.dumps({'file': 's.json', 'path': 'n', 'expect': 2})}])
            (self.work / 's.json').write_text('{"n": 2}')
            return report('claims_done', evidence=['s.json'])
        e, ex, out, store = self.run_task(fn, text=spec(acceptance=['n is 2']))
        self.assertTrue(wait_for(lambda: state(store) == 'completed'))
        t = store.load('t1')
        self.assertEqual([c['id'] for c in t['checks']], ['d1'])
        self.assertEqual(t['checks'][0]['origin'], 'drafted')

    def test_constraints_checked_at_verification(self):
        protected = Path(self.tmp) / 'install'
        protected.mkdir()
        (protected / 'app.dll').write_bytes(b'original')

        def fn(kind, prompt, call):
            if kind == 'plan':
                return plan([{'checks': ['report']}])
            (protected / 'app.dll').write_bytes(b'patched to enable the button')
            (self.work / 'out').mkdir(exist_ok=True)
            (self.work / 'out' / 'report.md').write_text('x')
            return report('claims_done', evidence=['out/report.md'])
        text = spec(acceptance=[{'id': 'report', 'type': 'files', 'pattern': 'out/report.md'},
                                {'id': 'no_tamper', 'type': 'dir_unchanged', 'path': str(protected)}])
        e, ex, out, store = self.run_task(fn, text=text, read_paths=[str(protected)], policy={'max_verify_rounds': 2})
        self.assertTrue(wait_for(lambda: state(store) == 'blocked'))
        self.assertIn('多次验收未通过', store.load('t1')['block_reason'])
        self.assertTrue(any('Final verification failed' in c['prompt'] and 'no_tamper' in c['prompt'] for c in ex.calls))

    def test_recover_marks_interrupted_step(self):
        gate = threading.Event()

        def fn(kind, prompt, call):
            if kind == 'plan':
                return plan([{'checks': ['report']}])
            if not gate.is_set():
                return ExecResult(False, '', None, 'killed')
            self.assertIn('interrupted', prompt)
            (self.work / 'out').mkdir(exist_ok=True)
            (self.work / 'out' / 'report.md').write_text('x')
            return report('claims_done', evidence=['out/report.md'])
        ex = Scripted(fn)
        e, out, store, work = make_engine(self.tmp, ex, policy={'max_exec_errors': 100})
        self.work = work
        e.intake_new('t1', 'p', 'm1', spec())
        self.assertTrue(wait_for(lambda: len(ex.calls) >= 2))
        e.shutdown()
        time.sleep(0.3)
        t = store.load('t1')
        t['step_in_progress'] = {'n': 2, 'phase': 'p1'}
        store.save(t)
        gate.set()
        e2, out2, store2, _ = make_engine(self.tmp, ex)
        e2.recover()
        self.assertTrue(wait_for(lambda: state(store2) == 'completed'))

    def test_keepalive_sends_status_only(self):
        clock = {'now': 1000.0}
        ex = Scripted(lambda *a: {})
        e, out, store, work = make_engine(self.tmp, ex)
        e.clock = lambda: clock['now']
        e.schedule = lambda: None  # no worker: the task stays active
        e.intake_new('t1', 'p', 'm1', spec())
        n = len(out.sent)
        e.keepalive()
        self.assertEqual(len(out.sent), n)
        clock['now'] += 601
        e.keepalive()
        self.assertTrue(out.sent[-1]['env']['heartbeat'])
        self.assertEqual(out.sent[-1]['state'], 'working')
        self.assertIn('last_note', out.sent[-1]['env'])

    def test_status_capabilities_and_duplicates(self):
        ex = Scripted(lambda *a: {})
        e, out, store, work = make_engine(self.tmp, ex)
        e.schedule = lambda: None
        e.intake_new('t1', 'p', 'm1', spec())
        e.intake_message('t1', 'm2', '{"taskd":"status"}')
        self.assertIn('当前状态', out.sent[-1]['text'])
        e.intake_message('t1', 'm2', '{"taskd":"status"}')  # the same message again is ignored
        e.intake_message('t1', 'm3', '{"taskd":"capabilities"}')
        self.assertEqual(out.sent[-1]['env']['capabilities'], {'protocol': 'taskd/1'})
        self.assertEqual(len([s for s in out.sent if '当前状态' in s['text']]), 1)

    def test_continue_from_same_requester_only(self):
        def fn(kind, prompt, call):
            if kind == 'plan':
                return plan([{'checks': ['report']}])
            (self.work / 'out').mkdir(exist_ok=True)
            (self.work / 'out' / 'report.md').write_text('x')
            return report('claims_done', evidence=['out/report.md'],
                          facts=[{'statement': 'the cache must be cleared first', 'basis': 'observed', 'evidence': []}])
        e, ex, out, store = self.run_task(fn)
        self.assertTrue(wait_for(lambda: state(store) == 'completed'))
        self.assertIsNone(e.intake_new('t2', 'someone-else', 'n1', spec(continue_from='t1')))
        e.intake_new('t3', 'peer-a', 'n2', spec(continue_from='t1'))
        self.assertTrue(wait_for(lambda: state(store, 't3') == 'completed'))
        plan_call = [c for c in ex.calls if c['kind'] == 'plan'][-1]
        self.assertIn('the cache must be cleared first', plan_call['prompt'])

    def test_ended_by_requester(self):
        started = threading.Event()

        def fn(kind, prompt, call):
            if kind == 'plan':
                return plan([{'checks': ['report']}])
            started.set()
            ev = e.cancel_events.get('t1')
            if ev:
                ev.wait(5)
            return ExecResult(False, '', None, 'canceled', canceled=True)
        ex = Scripted(fn)
        e, out, store, work = make_engine(self.tmp, ex)
        e.intake_new('t1', 'p', 'm1', spec())
        self.assertTrue(started.wait(5))
        n = len(out.sent)
        e.ended_by_peer('t1', 'canceled')
        self.assertTrue(wait_for(lambda: state(store) == 'stopped'))
        time.sleep(0.2)
        self.assertEqual(len(out.sent), n)  # nothing is sent on an ended task

    def test_a_task_ended_during_a_step_stays_ended(self):
        started, release = threading.Event(), threading.Event()

        def fn(kind, prompt, call):
            if kind == 'plan':
                return plan([{'checks': ['report']}])
            started.set()
            release.wait(5)
            (self.work / 'out').mkdir(exist_ok=True)
            (self.work / 'out' / 'report.md').write_text('x')
            return report('claims_done', evidence=['out/report.md'])
        e, ex, out, store = self.run_task(fn)
        self.assertTrue(started.wait(5))
        e.ended_by_peer('t1', 'canceled')
        release.set()
        time.sleep(0.5)
        self.assertEqual(state(store), 'stopped')
        self.assertNotIn('completed', out.states())

    def test_failed_reply_is_logged_not_fatal(self):
        def fn(kind, prompt, call):
            if kind == 'plan':
                return plan([{'checks': ['report']}])
            (self.work / 'out').mkdir(exist_ok=True)
            (self.work / 'out' / 'report.md').write_text('x')
            return report('claims_done', evidence=['out/report.md'])
        ex = Scripted(fn)
        e, out, store, work = make_engine(self.tmp, ex)
        self.work = work
        out.fail = True
        e.intake_new('t1', 'p', 'm1', spec())
        self.assertTrue(wait_for(lambda: state(store) == 'completed'))
        self.assertTrue(store.read_log('t1', 'reply_failed'))


if __name__ == '__main__':
    unittest.main()

class PhaseAcceptanceTests(unittest.TestCase):
    def test_early_phase_does_not_wait_for_later_final_artifact(self):
        root = tmpdir()
        seen = []
        def fn(kind, prompt, call):
            if kind == 'plan':
                # Reproduce the old bad plan: final check erroneously gates p1.
                return plan([{'id': 'p1', 'checks': ['result'], 'produces': ['input.txt']},
                             {'id': 'p2', 'requires': ['input.txt'], 'produces': ['result.txt']}])
            if 'Phase p1:' in prompt:
                self.assertFalse((work / 'result.txt').exists())
                (work / 'input.txt').write_text('input')
                seen.append('p1')
                return report('claims_done', evidence=['input.txt'])
            self.assertTrue((work / 'input.txt').exists())
            (work / 'result.txt').write_text('answer')
            seen.append('p2')
            return report('claims_done', evidence=['result.txt'])
        ex = Scripted(fn)
        e, out, store, work = make_engine(root, ex)
        e.intake_new('t1', 'p', 'm', spec(acceptance=[{'id': 'result', 'type': 'files', 'pattern': 'result.txt'}]))
        self.assertTrue(wait_for(lambda: state(store) == 'completed'))
        self.assertEqual(seen, ['p1', 'p2'])
        phases = store.load('t1')['plan']['phases']
        self.assertNotIn('result', phases[0]['checks'])
        self.assertEqual(phases[1]['final_checks'], ['result'])

    def test_invalid_forward_dependency_is_repaired_without_asking_user(self):
        root = tmpdir();plans = []
        def fn(kind, prompt, call):
            if kind == 'plan':
                plans.append(prompt)
                if len(plans) == 1:
                    return plan([{'id': 'p1', 'requires': ['later.txt']}, {'id': 'p2', 'produces': ['later.txt']}])
                self.assertIn('before its producer', prompt)
                return plan([{'id': 'p1', 'produces': ['later.txt']}])
            (work / 'later.txt').write_text('done')
            return report('claims_done', evidence=['later.txt'])
        e, out, store, work = make_engine(root, Scripted(fn))
        e.intake_new('t1', 'p', 'm', spec(acceptance=[{'id': 'result', 'type': 'files', 'pattern': 'later.txt'}]))
        self.assertTrue(wait_for(lambda: state(store) == 'completed'))
        self.assertEqual(len(plans), 2)
        self.assertNotIn('input-required', out.states())

    def test_bad_phase_claim_with_different_screenshots_does_not_reset_stall(self):
        root = tmpdir();n = [0]
        def fn(kind, prompt, call):
            if kind == 'plan':
                return plan([{'produces': ['missing.txt']}])
            n[0] += 1
            name = 'screen%d.png' % n[0]
            (work / name).write_bytes(str(n[0]).encode())
            return report('claims_done', evidence=[name])
        e, out, store, work = make_engine(root, Scripted(fn), policy={'no_progress_steps': 2, 'max_replans': 0})
        e.intake_new('t1', 'p', 'm', spec())
        self.assertTrue(wait_for(lambda: state(store) == 'blocked'))
        self.assertEqual(n[0], 2)
        self.assertEqual(store.load('t1')['phase_index'], 0)

    def test_phase_artifact_is_not_rechecked_as_final_after_consumption(self):
        root = tmpdir()
        def fn(kind, prompt, call):
            if kind == 'plan':
                return plan([{'id': 'p1', 'produces': ['temp.txt']},
                             {'id': 'p2', 'requires': ['temp.txt'], 'produces': ['result.txt']}])
            if 'Phase p1:' in prompt:
                (work / 'temp.txt').write_text('input')
                return report('claims_done', evidence=['temp.txt'])
            (work / 'temp.txt').unlink()
            (work / 'result.txt').write_text('output')
            return report('claims_done', evidence=['result.txt'])
        e, out, store, work = make_engine(root, Scripted(fn))
        e.intake_new('t1', 'p', 'm', spec(acceptance=[{'id': 'result', 'type': 'files', 'pattern': 'result.txt'}]))
        self.assertTrue(wait_for(lambda: state(store) == 'completed'))
        self.assertEqual(store.load('t1')['used']['verify_rounds'], 1)
