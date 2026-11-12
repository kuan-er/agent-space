"""The taskd/1 protocol: task specs, control messages, executor reports and status envelopes.

Everything a requester, an executor node or an executor adapter exchanges is plain JSON, so any
client can speak it. Nothing here knows which transport, executor or platform is in use.
"""
from __future__ import annotations

import json
import re

PROTOCOL = 'taskd/1'
STATUS_PREFIX = 'TASKD '

# Task states, and the A2A state each one is reported as.
STATES = ('accepted', 'planning', 'awaiting_approval', 'running', 'verifying',
          'needs_user', 'blocked', 'completed', 'stopped')
ACTIVE = frozenset({'accepted', 'planning', 'running', 'verifying'})
WAITING = frozenset({'awaiting_approval', 'needs_user', 'blocked'})
FINAL = frozenset({'completed', 'stopped'})


def a2a_state(state, stop_reason=None):
    """The A2A task state a taskd state is reported as (design §9)."""
    if state in ACTIVE:
        return 'working'
    if state in WAITING:
        return 'input-required'
    if state == 'completed':
        return 'completed'
    if state == 'stopped':
        return 'canceled' if stop_reason == 'canceled' else 'failed'
    raise ValueError('unknown state: %r' % (state,))


CONTROL_KINDS = frozenset({'capabilities', 'status', 'approve_plan', 'answer', 'hint', 'cancel', 'revise_plan', 'pause', 'resume'})
CHECK_ID = re.compile(r'^[a-z0-9][a-z0-9_-]{0,39}$')
TASK_REF = re.compile(r'^[A-Za-z0-9_.:-]{1,200}$')

MAX_GOAL = 64 * 1024
MAX_TEXT = 8 * 1024
MAX_ITEMS = 50

DEFAULT_BUDGET = {'max_steps': 40, 'max_hours': 12, 'step_timeout_min': 30}
DEFAULT_LIMITS = {'max_steps': 200, 'max_hours': 72, 'step_timeout_min': 120}


class Rejected(ValueError):
    """A task or message that cannot be accepted; reason is a stable code, message a sentence."""

    def __init__(self, reason, message):
        super().__init__(message)
        self.reason, self.message = reason, message


def parse_message(text):
    """Classify one requester message: ('control', obj), ('submit', obj) or ('text', str)."""
    body = (text or '').strip()
    if body.startswith('{'):
        try:
            obj = json.loads(body)
        except ValueError:
            obj = None
        if isinstance(obj, dict) and isinstance(obj.get('taskd'), str):
            return ('submit' if obj['taskd'] in ('submit', 'run') else 'control'), obj
    return 'text', body


def _text(value, name, limit=MAX_TEXT, required=False):
    if value is None and not required:
        return None
    if not isinstance(value, str) or (required and not value.strip()):
        raise Rejected('invalid_spec', '%s must be a non-empty string' % name)
    if len(value.encode('utf-8')) > limit:
        raise Rejected('invalid_spec', '%s is longer than %d bytes' % (name, limit))
    return value


def normalize_spec(message, checkers, limits=None):
    """Validate a submitted task (a spec object or plain text) against this node's checkers and limits.

    checkers maps a check type to an object with validate(params) -> params (raising ValueError).
    Returns a normalized spec; raises Rejected with a stable reason code otherwise.
    """
    limits = dict(DEFAULT_LIMITS, **(limits or {}))
    if isinstance(message, str):
        message = {'taskd': 'submit', 'goal': message}
    if not isinstance(message, dict):
        raise Rejected('invalid_spec', 'a task is a JSON object or plain text')
    if message.get('mode') not in (None, 'quick', 'long'):
        raise Rejected('invalid_spec', 'use taskd=run for lightweight execution, taskd=submit for planned work')
    proto = message.get('protocol', PROTOCOL)
    if not isinstance(proto, str) or proto.split('.')[0] != PROTOCOL:
        raise Rejected('protocol_unsupported', 'this node speaks %s, not %r' % (PROTOCOL, proto))
    if message.get('parent_task') is not None:
        raise Rejected('parent_task_unsupported', 'sub-task delegation is not supported by this node yet')
    goal = _text(message.get('goal'), 'goal', MAX_GOAL, required=True)
    resource = message.get('resource', 'auto')
    if resource not in ('auto', 'background', 'desktop'):
        raise Rejected('invalid_spec', 'resource is auto, background or desktop')

    acceptance, seen = [], set()
    items = message.get('acceptance') or []
    if not isinstance(items, list) or len(items) > MAX_ITEMS:
        raise Rejected('invalid_spec', 'acceptance must be a list of at most %d items' % MAX_ITEMS)
    for i, item in enumerate(items):
        if isinstance(item, str):
            acceptance.append({'id': 'a%d' % (i + 1), 'type': 'natural', 'text': _text(item, 'acceptance item', required=True)})
            continue
        if not isinstance(item, dict):
            raise Rejected('invalid_spec', 'acceptance items are strings or check objects')
        cid, ctype = item.get('id'), item.get('type')
        if not isinstance(cid, str) or not CHECK_ID.fullmatch(cid) or cid in seen:
            raise Rejected('invalid_check', 'check ids are unique, lower case, at most 40 characters: %r' % (cid,))
        if ctype not in checkers:
            raise Rejected('checker_unavailable', 'this node has no %r checker' % (ctype,))
        params = {k: v for k, v in item.items() if k not in ('id', 'type')}
        try:
            params = checkers[ctype].validate(params)
        except ValueError as e:
            raise Rejected('invalid_check', '%s: %s' % (cid, e)) from None
        seen.add(cid)
        acceptance.append({'id': cid, 'type': ctype, 'params': params, 'origin': 'requester', 'scope': 'final'})

    constraints = message.get('constraints') or []
    if not isinstance(constraints, list) or len(constraints) > MAX_ITEMS:
        raise Rejected('invalid_spec', 'constraints must be a list of at most %d strings' % MAX_ITEMS)
    constraints = [_text(c, 'constraint', required=True) for c in constraints]

    budget = dict(DEFAULT_BUDGET)
    given = message.get('budget') or {}
    if not isinstance(given, dict):
        raise Rejected('invalid_spec', 'budget must be an object')
    for key in DEFAULT_BUDGET:
        if key in given:
            v = given[key]
            if not isinstance(v, (int, float)) or isinstance(v, bool) or v <= 0:
                raise Rejected('invalid_spec', 'budget.%s must be a positive number' % key)
            if v > limits[key]:
                raise Rejected('over_limit', 'budget.%s=%s is over this node\'s limit %s' % (key, v, limits[key]))
            budget[key] = v
    for key in DEFAULT_BUDGET:
        budget[key] = min(budget[key], limits[key])

    approval = message.get('plan_approval', 'auto')
    if approval not in ('auto', 'always'):
        raise Rejected('invalid_spec', 'plan_approval is auto or always')
    cont = message.get('continue_from')
    if cont is not None and (not isinstance(cont, str) or not TASK_REF.fullmatch(cont)):
        raise Rejected('invalid_spec', 'continue_from must be a task id')
    ref = message.get('ref')
    if ref is not None and (not isinstance(ref, str) or not TASK_REF.fullmatch(ref)):
        raise Rejected('invalid_spec', 'ref must be a short identifier')
    orchestration = 'direct' if message.get('taskd') == 'run' else 'planned'
    preference = message.get('orchestration', 'auto')
    if preference not in ('auto', 'native', 'planned'):
        raise Rejected('invalid_spec', 'orchestration must be auto, native or planned')
    if orchestration == 'direct' and preference != 'auto':
        raise Rejected('invalid_spec', 'taskd=run already selects single execution')
    if orchestration == 'direct' and (not acceptance or any(c['type'] in ('natural', 'llm_review') for c in acceptance)):
        raise Rejected('invalid_spec', 'lightweight execution requires explicit deterministic acceptance checks')
    return {'protocol': PROTOCOL, 'goal': goal, 'acceptance': acceptance, 'constraints': constraints,
            'orchestration': orchestration, 'execution_preference': preference,
            'budget': budget, 'plan_approval': approval, 'continue_from': cont, 'ref': ref, 'resource': resource}


def normalize_control(obj):
    kind = obj.get('taskd')
    if kind not in CONTROL_KINDS:
        raise Rejected('unknown_control', 'unknown control message %r' % (kind,))
    text = obj.get('text')
    if kind in ('answer', 'hint'):
        text = _text(text, kind + '.text', required=True)
    if kind == 'revise_plan':
        allowed = {'taskd', 'protocol', 'request_id', 'base_revision', 'from_phase', 'reason', 'plan', 'check_updates'}
        if set(obj) - allowed:
            raise Rejected('invalid_revision', 'a revision cannot change the goal, budget or acceptance')
        rid = obj.get('request_id')
        revision = obj.get('base_revision')
        phase = obj.get('from_phase')
        if not isinstance(rid, str) or not TASK_REF.fullmatch(rid):
            raise Rejected('invalid_revision', 'request_id must be a stable unique identifier')
        if type(revision) is not int or revision < 0:
            raise Rejected('invalid_revision', 'base_revision must be a non-negative integer')
        if not isinstance(phase, str) or not CHECK_ID.fullmatch(phase):
            raise Rejected('invalid_revision', 'from_phase must name the first phase to replace')
        for phase_item in (obj.get('plan') or {}).get('phases', []) if isinstance(obj.get('plan'), dict) else []:
            if isinstance(phase_item, dict):
                phase_item.setdefault('preflight', [])
                phase_item.setdefault('requires_desktop', False)
                phase_item.setdefault('milestones', [])
        if not conforms(obj.get('plan'), PLAN_SCHEMA):
            raise Rejected('invalid_revision', 'plan must match PLAN_SCHEMA and contain the replacement suffix')
        updates = obj.get('check_updates', [])
        if not isinstance(updates, list) or len(updates) > MAX_ITEMS:
            raise Rejected('invalid_revision', 'check_updates is a bounded list')
        seen = set()
        for update in updates:
            if (not isinstance(update, dict) or set(update) != {'id', 'type', 'params', 'reason'}
                    or not isinstance(update['id'], str) or not CHECK_ID.fullmatch(update['id'])
                    or update['id'] in seen or not isinstance(update['type'], str)
                    or not isinstance(update['params'], dict)):
                raise Rejected('invalid_revision', 'each check update needs a unique id, type, params and reason')
            _text(update['reason'], 'check update reason', required=True)
            seen.add(update['id'])
        return dict(kind=kind, request_id=rid, base_revision=revision, from_phase=phase,
                    reason=_text(obj.get('reason'), 'reason', required=True), plan=obj['plan'], check_updates=updates)
    return {'kind': kind, 'text': text}


def status_line(payload):
    """The machine-readable last line of every message taskd sends to a requester."""
    return STATUS_PREFIX + json.dumps(dict(payload, protocol=PROTOCOL), ensure_ascii=False, separators=(',', ':'))


def parse_status(text):
    """The status envelope carried by a taskd message, or None."""
    for line in reversed((text or '').splitlines()):
        if line.startswith(STATUS_PREFIX + '{'):
            try:
                obj = json.loads(line[len(STATUS_PREFIX):])
            except ValueError:
                return None
            return obj if isinstance(obj, dict) and obj.get('protocol') == PROTOCOL else None
    return None


# --- what executors return (strict JSON Schemas: every property required, no extra properties) ---

def _obj(props, required=None):
    return {'type': 'object', 'additionalProperties': False,
            'required': list(required or props), 'properties': props}


_STR = {'type': 'string'}
_STRS = {'type': 'array', 'items': _STR}
_NULLSTR = {'type': ['string', 'null']}

REPORT_SCHEMA = _obj({
    'phase': _STR,
    'status': {'type': 'string', 'enum': ['working', 'claims_done', 'blocked', 'needs_user']},
    'summary': _STR,
    'attempts': {'type': 'array', 'items': _obj({
        'what': _STR,
        'outcome': {'type': 'string', 'enum': ['worked', 'failed', 'inconclusive']},
        'observed': _STR,
        'evidence': _STRS})},
    'facts': {'type': 'array', 'items': _obj({
        'statement': _STR,
        'basis': {'type': 'string', 'enum': ['observed', 'inferred']},
        'evidence': _STRS})},
    'artifacts': _STRS,
    'next': _STR,
    'blocker': {'anyOf': [_NULLSTR, _obj({'code': _STR, 'detail': _STR, 'evidence': _STRS})]},
    'question': _NULLSTR,
})

PLAN_SCHEMA = _obj({
    'phases': {'type': 'array', 'items': _obj({
        'id': _STR, 'title': _STR, 'done_when': _STR, 'checks': _STRS,
        'resource': {'type': 'string', 'enum': ['background', 'desktop']},
        'requires_desktop': {'type': 'boolean'},
        'milestones': {'type': 'array', 'items': _obj({'id': _STR, 'title': _STR, 'checks': _STRS})},
        'produces': _STRS, 'requires': _STRS, 'final_checks': _STRS, 'preflight': _STRS})},
    'drafted_checks': {'type': 'array', 'items': _obj({
        'id': _STR, 'type': _STR, 'params_json': _STR, 'covers': _STR})},
    'approval_reasons': _STRS,
    'notes': _STR,
})

REVIEW_SCHEMA = _obj({
    'verdict': {'type': 'string', 'enum': ['supported', 'unsupported', 'unclear']},
    'reasons': _STR,
    'missing': _STRS,
})


def extract_json(text):
    """The JSON object an executor answered with: the whole text, or the last {...} block in it."""
    body = (text or '').strip()
    if body.startswith('```'):
        body = body.strip('`')
        body = body[body.find('{'):] if '{' in body else body
    try:
        obj = json.loads(body)
        if isinstance(obj, dict):
            return obj
    except ValueError:
        pass
    decoder = json.JSONDecoder()
    found, i = None, 0
    while i < len(body):
        if body[i] == '{':
            try:
                obj, end = decoder.raw_decode(body[i:])
            except ValueError:
                i += 1
                continue
            if isinstance(obj, dict):
                found = obj  # the last top-level object, not one nested in it
            i += end
            continue
        i += 1
    return found


def conforms(obj, schema):
    """A small JSON Schema check, enough for the strict schemas above."""
    if 'anyOf' in schema:
        return any(conforms(obj, s) for s in schema['anyOf'])
    t = schema.get('type')
    types = t if isinstance(t, list) else [t]
    ok = False
    for one in types:
        if one == 'object' and isinstance(obj, dict):
            ok = True
        elif one == 'array' and isinstance(obj, list):
            ok = True
        elif one == 'string' and isinstance(obj, str):
            ok = True
        elif one == 'null' and obj is None:
            ok = True
        elif one == 'integer' and isinstance(obj, int) and not isinstance(obj, bool):
            ok = True
        elif one == 'boolean' and isinstance(obj, bool):
            ok = True
    if not ok:
        return False
    if 'enum' in schema and obj not in schema['enum']:
        return False
    if isinstance(obj, dict):
        props = schema.get('properties', {})
        if any(k not in obj for k in schema.get('required', [])):
            return False
        if schema.get('additionalProperties') is False and any(k not in props for k in obj):
            return False
        return all(conforms(obj[k], props[k]) for k in obj if k in props)
    if isinstance(obj, list) and 'items' in schema:
        return all(conforms(x, schema['items']) for x in obj)
    return True


def schema_instructions(schema):
    """How to ask an executor that cannot enforce a schema for the same shape."""
    return ('Output ONLY one JSON object, with no text before or after it, that conforms to this JSON '
            'Schema:\n' + json.dumps(schema, ensure_ascii=False))
