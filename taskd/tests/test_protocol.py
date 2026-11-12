import json
import unittest

from helpers import builtin
from taskd import protocol as p


class SpecTests(unittest.TestCase):
    def setUp(self):
        self.checkers = builtin.make_checkers()

    def test_plain_text_is_a_goal(self):
        s = p.normalize_spec('整理数据', self.checkers)
        self.assertEqual(s['goal'], '整理数据')
        self.assertEqual(s['budget'], p.DEFAULT_BUDGET)
        self.assertEqual(s['plan_approval'], 'auto')

    def test_checks_and_natural_items(self):
        s = p.normalize_spec({'taskd': 'submit', 'goal': 'g', 'acceptance': [
            {'id': 'report', 'type': 'files', 'pattern': 'out/*.md'}, 'explain it']}, self.checkers)
        self.assertEqual(s['acceptance'][0]['params'], {'pattern': 'out/*.md', 'count': 1, 'min_size': 1})
        self.assertEqual(s['acceptance'][1], {'id': 'a2', 'type': 'natural', 'text': 'explain it'})

    def test_rejections_have_stable_reasons(self):
        cases = [
            ({'goal': 'g', 'acceptance': [{'id': 'x', 'type': 'no_such'}]}, 'checker_unavailable'),
            ({'goal': 'g', 'acceptance': [{'id': 'x', 'type': 'files'}]}, 'invalid_check'),
            ({'goal': 'g', 'acceptance': [{'id': 'X', 'type': 'files', 'pattern': 'a'}]}, 'invalid_check'),
            ({'goal': 'g', 'protocol': 'taskd/2'}, 'protocol_unsupported'),
            ({'goal': 'g', 'parent_task': 't1'}, 'parent_task_unsupported'),
            ({'goal': ''}, 'invalid_spec'),
            ({'goal': 'g', 'budget': {'max_hours': 10000}}, 'over_limit'),
            ({'goal': 'g', 'budget': {'max_steps': True}}, 'invalid_spec'),
            ({'goal': 'g', 'plan_approval': 'never'}, 'invalid_spec'),
        ]
        for spec, reason in cases:
            with self.subTest(spec=spec):
                with self.assertRaises(p.Rejected) as cm:
                    p.normalize_spec(dict(spec, taskd='submit'), self.checkers)
                self.assertEqual(cm.exception.reason, reason)

    def test_duplicate_check_ids(self):
        with self.assertRaises(p.Rejected):
            p.normalize_spec({'goal': 'g', 'acceptance': [{'id': 'a', 'type': 'files', 'pattern': 'x'},
                                                          {'id': 'a', 'type': 'files', 'pattern': 'y'}]}, self.checkers)

    def test_limits_cap_the_budget(self):
        s = p.normalize_spec({'goal': 'g', 'budget': {'max_steps': 10}}, self.checkers, {'max_steps': 20, 'max_hours': 2})
        self.assertEqual(s['budget']['max_steps'], 10)
        self.assertEqual(s['budget']['max_hours'], 2)


class MessageTests(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(p.parse_message('{"taskd":"status"}')[0], 'control')
        self.assertEqual(p.parse_message('{"taskd":"submit","goal":"g"}')[0], 'submit')
        self.assertEqual(p.parse_message('{"x":1}'), ('text', '{"x":1}'))
        self.assertEqual(p.parse_message('hello'), ('text', 'hello'))

    def test_status_roundtrip(self):
        line = p.status_line({'task': 't', 'state': 'running'})
        self.assertEqual(p.parse_status('hello\n\n' + line)['state'], 'running')
        self.assertIsNone(p.parse_status('TASKD {"protocol":"other"}'))
        self.assertIsNone(p.parse_status('no envelope'))

    def test_a2a_states(self):
        self.assertEqual(p.a2a_state('running'), 'working')
        self.assertEqual(p.a2a_state('blocked'), 'input-required')
        self.assertEqual(p.a2a_state('stopped', 'canceled'), 'canceled')
        self.assertEqual(p.a2a_state('stopped', 'budget_steps'), 'failed')

    def test_control(self):
        self.assertEqual(p.normalize_control({'taskd': 'hint', 'text': 'x'}), {'kind': 'hint', 'text': 'x'})
        with self.assertRaises(p.Rejected):
            p.normalize_control({'taskd': 'answer'})
        with self.assertRaises(p.Rejected):
            p.normalize_control({'taskd': 'rm -rf'})


class SchemaTests(unittest.TestCase):
    def test_conforms(self):
        from helpers import report, plan
        self.assertTrue(p.conforms(report(), p.REPORT_SCHEMA))
        self.assertTrue(p.conforms(plan([{}]), p.PLAN_SCHEMA))
        bad = report()
        bad['status'] = 'finished'
        self.assertFalse(p.conforms(bad, p.REPORT_SCHEMA))
        extra = dict(report(), extra=1)
        self.assertFalse(p.conforms(extra, p.REPORT_SCHEMA))

    def test_extract_json(self):
        self.assertEqual(p.extract_json('```json\n{"a": 1}\n```'), {'a': 1})
        self.assertEqual(p.extract_json('sure: {"a": {"b": 2}} done'), {'a': {'b': 2}})
        self.assertIsNone(p.extract_json('nothing'))

    def test_strict_schemas(self):
        """Every object in the executor schemas lists all its properties as required (strict mode)."""
        def walk(s):
            if s.get('type') == 'object':
                self.assertEqual(set(s['required']), set(s['properties']))
                self.assertIs(s['additionalProperties'], False)
                for v in s['properties'].values():
                    walk(v)
            if s.get('type') == 'array':
                walk(s['items'])
        for schema in (p.REPORT_SCHEMA, p.PLAN_SCHEMA, p.REVIEW_SCHEMA):
            walk(schema)
            json.dumps(schema)


if __name__ == '__main__':
    unittest.main()
