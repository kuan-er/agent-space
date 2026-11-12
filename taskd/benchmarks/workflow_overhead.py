#!/usr/bin/env python3
"""Compare actual old/new orchestration code with deterministic local fixtures.

UI Automation is mocked with an explicit 4ms call cost. This is NOT an EPS,
Windows, model-latency or end-to-end performance measurement.
"""
import argparse
import json
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch


def worker(source):
    sys.path.insert(0, str(Path(source).resolve()))
    from taskd.engine import Engine
    from taskd.store import Store
    from taskd.checkers.windows_uia import UIAPropertyChecker
    from taskd.checkers.generic import FilesChecker
    from taskd.checkers import windows_uia
    from taskd.executors.base import ExecResult

    class Outbox:
        def reply(self, *a, **k):
            pass

    class Executor:
        caps = {'resume': True, 'schema': True}
        def __init__(self, work):
            self.work = work
            self.calls = 0
            self.sessions = []
        def run(self, prompt, *, session=None, **kwargs):
            self.sessions.append(session)
            self.calls += 1
            (self.work / ('proof%d.txt' % self.calls)).write_text('operation %d' % self.calls)
            result = {'phase': 'p1', 'status': 'claims_done' if self.calls == 6 else 'working',
                      'summary': 'fixture operation', 'attempts': [], 'facts': [],
                      'artifacts': ['proof%d.txt' % self.calls], 'next': '', 'blocker': None, 'question': None}
            return ExecResult(True, json.dumps(result), 'fixture-session', duration=0)

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp); work = root / 'work'; work.mkdir()
        executor = Executor(work)
        e = Engine(config={'work_dir': str(work), 'isolate_tasks': False, 'policy': {'max_replans': 0}},
                   store=Store(root/'state'), outbox=Outbox(), executor=executor,
                   checkers={'uia_property': UIAPropertyChecker(), 'files': FilesChecker()})
        e.stopping.set()
        e.intake_new('fixture', 'peer', 'm1', json.dumps({'taskd': 'submit', 'goal': 'local benchmark'}))
        checks = [{'id': 'ui%d' % i, 'type': 'uia_property', 'params': {
            'window': 'Fixture', 'element': 'Control%d' % (i % 6), 'property': 'IsEnabled', 'expect': True}}
                  for i in range(12)]
        durations, calls, query_counts, fingerprints = [], [], [], []
        for _ in range(5):
            counts = [0, 0]
            def read(script, params):
                counts[0] += 1; counts[1] += len(params['queries'])
                time.sleep(0.004)
                return {'found_window': True, 'window': 'Fixture', 'items': [
                    dict(q, found=True, value=True) for q in params['queries']]}
            with patch.object(windows_uia.powershell, 'run_json', side_effect=read):
                start = time.monotonic(); results = e.run_checks('fixture', checks)
                durations.append(time.monotonic()-start)
            assert all(v['outcome'] == 'pass' for v in results.values())
            calls.append(counts[0]); query_counts.append(counts[1])
            fingerprints.append({k: {x: v[x] for x in ('outcome', 'observed')} for k, v in results.items()})
        # Same six file-producing operations and completion gates; new mode adds milestone grouping.
        t = e.store.load('fixture')
        phase_checks = [{'id': 'proof%d' % i, 'type': 'files', 'origin': 'drafted', 'scope': 'phase',
                         'params': FilesChecker().validate({'pattern': 'proof%d.txt' % i})} for i in range(1, 7)]
        t['checks'] = phase_checks
        phase = {'id': 'p1', 'title': 'local operations', 'done_when': 'six proofs',
                 'checks': [c['id'] for c in phase_checks], 'preflight': [], 'final_checks': [],
                 'resource': 'background', 'requires_desktop': False, 'produces': [], 'requires': [], 'status': 'pending'}
        if hasattr(e, '_timing'):
            phase['milestones'] = [{'id': c['id'], 'title': c['id'], 'checks': [c['id']]} for c in phase_checks]
        t['plan'] = {'phases': [phase]}; t['state'] = 'running'; e.store.save(t)
        start = time.monotonic()
        for _ in range(6):
            e._step('fixture')
        workflow_seconds = time.monotonic() - start
        task = e.store.load('fixture')
        assert task['state'] == 'verifying', task['state']
        assert executor.sessions == [None] + ['fixture-session'] * 5
        file_results = [r for event in e.store.read_log('fixture', 'checks')
                        for cid, r in event['results'].items() if cid.startswith('proof')]
        return {'ui': {'input_checks': 12, 'rounds': 5, 'injected_call_latency_seconds': 0.004,
                       'calls_per_round': calls, 'queries_per_round': query_counts,
                       'median_seconds': statistics.median(durations), 'results': fingerprints[0]},
                'file_workflow': {'operations': executor.calls, 'sessions': len(set(executor.sessions[1:])),
                                  'check_invocations': len(file_results), 'seconds': workflow_seconds,
                                  'state': task['state'], 'replans': task['used']['replans'],
                                  'proofs': {p.name: p.read_text() for p in sorted(work.glob('proof*.txt'))}}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worker')
    parser.add_argument('--baseline')
    parser.add_argument('--current')
    parser.add_argument('--out')
    args = parser.parse_args()
    if args.worker:
        print(json.dumps(worker(args.worker))); return
    output = {'scope': 'local deterministic fixture; UI calls mocked, no EPS/Windows or model timing',
              'baseline': {}, 'current': {}}
    for name, source in (('baseline', args.baseline), ('current', args.current)):
        result = subprocess.run([sys.executable, __file__, '--worker', source], check=True, capture_output=True, text=True)
        output[name] = json.loads(result.stdout)
    assert output['baseline']['ui']['results'] == output['current']['ui']['results']
    assert output['baseline']['file_workflow']['proofs'] == output['current']['file_workflow']['proofs']
    output['equivalent_fixture_results'] = True
    for name in ('baseline', 'current'):
        output[name]['ui'].pop('results')
    Path(args.out).write_text(json.dumps(output, indent=2) + '\n')
    print(json.dumps(output, indent=2))


if __name__ == '__main__':
    main()
