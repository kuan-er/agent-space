import importlib.util
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).parents[1]
def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m
connector = module('visibility_connector', ROOT/'connector.py')
server = module('visibility_server', ROOT/'server.py')
producer = module('visibility_producer', ROOT.parent/'taskd/taskd/execution_facts.py')

class ExecutionFactsTests(unittest.TestCase):
    def test_boundary_sanitizers_match_and_reject_untrusted_content(self):
        raw = {'schema': 1, 'route': 'native', 'goal_status': 'complete', 'iteration': 2,
               'updated_at': time.time(), 'thread_id': 'private', 'last_event': 'native/retry',
               'retry': {'reason': 'model_at_capacity', 'attempt': 3, 'text': 'private'},
               'stop': {'requested': True, 'confirmed': False, 'resource_retained': True, 'scope': 'private'}}
        results = [m.clean_execution_facts(raw) for m in (producer, connector, server)]
        self.assertEqual(results[0], results[1]); self.assertEqual(results[1], results[2])
        self.assertNotIn('private', str(results))
        malformed = dict(raw, route=[], goal_status={}, iteration=True, updated_at=float('nan'),
                         retry={'reason': {}, 'attempt': -1}, stop={'confirmed': 'yes'})
        for m in (producer, connector, server):
            clean = m.clean_execution_facts(malformed)
            for k in ('route', 'goal_status', 'iteration', 'updated_at', 'retry'):
                self.assertNotIn(k, clean)
            self.assertEqual(clean['stop'], {})

    def test_capabilities_keep_controls_without_claiming_harness_parity(self):
        c = server.long_task_caps(json.dumps({'protocol':'taskd/1',
            'native': {'available':False, 'steer':False, 'reason':'private error'},
            'routing': {'submit_default':'native_if_available', 'planned_opt_in':True, 'private':'path'}}))
        self.assertEqual(c['native'], {'available':False, 'steer':False})
        self.assertEqual(c['routing'], {'submit_default':'native_if_available', 'planned_opt_in':True})
        self.assertNotIn('private', str(c))
        self.assertIsNone(server.long_task_caps('{"protocol":"taskd/1"}')['native'])

    def test_sync_off_preserves_facts_not_private_text_and_stale_snapshot_is_unknown(self):
        ts = time.time()
        task = {'id': 't1', 'state': 'running', 'goal': 'private goal', 'last_purpose': 'native_goal',
                'execution_facts': {'schema': 1, 'route': 'native', 'goal_status': 'complete',
                    'updated_at': ts-200, 'thread_id': 'private-session',
                    'retry': {'reason': 'model_at_capacity', 'attempt': 2},
                    'stop': {'requested': True, 'confirmed': False, 'resource_retained': True}}}
        work = {'schema': 1, 'source': 'taskd', 'scope': 'anet_tasks', 'observed_at': ts,
                'counts': {'running': 1}, 'tasks': [task]}
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with patch.object(connector, 'taskd_state', return_value={'executor':'codex', 'work':work}):
                synced = connector.work_info(root, 'codex')
        self.assertFalse(synced['content_synced'])
        self.assertNotIn('private', json.dumps(synced))
        cleaned = server.clean_work(synced)
        self.assertEqual(cleaned['tasks'][0]['execution_facts']['stop']['confirmed'], False)
        self.assertEqual(cleaned['tasks'][0]['last_purpose'], 'native_goal')
        self.assertNotIn('last_progress_at', cleaned['tasks'][0])
        agent = {'work': json.dumps(synced), 'node_connected': True}
        self.assertEqual(server.work_view(agent, 'ready', True)['state'], 'running')
        self.assertEqual(server.work_view(agent, 'ready', False)['tasks'], [])
        synced['observed_at'] = ts-100
        agent['work'] = json.dumps(synced)
        self.assertEqual(server.work_view(agent, 'ready', True)['reason'], 'stale')
        legacy = dict(task); legacy.pop('execution_facts')
        work['tasks'] = [legacy]
        self.assertNotIn('execution_facts', server.clean_work(work)['tasks'][0])

if __name__ == '__main__': unittest.main()
