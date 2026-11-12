"""Bounded, versioned plan changes and actionable blocker metadata."""
from copy import deepcopy
import hashlib
import json

ACTIONS = {
    'stalled': 'hint', 'configuration_mismatch': 'repair_configuration',
    'program_error': 'repair_program', 'compile_failed': 'collect_compiler_diagnostics',
    'remote_offline': 'wait_for_connection', 'files_missing': 'retry_receive',
    'verification': 'repair_failed_checks', 'environment': 'restore_environment',
    'waiting_desktop': 'wait_for_desktop', 'waiting_workspace': 'wait_for_workspace',
    'waiting_slot': 'wait_for_slot', 'executor_error': 'inspect_executor',
    'resource_mismatch': 'check_desktop_contract', 'checker_error': 'repair_checker_then_hint', 'planning_error': 'hint',
    'direct_incomplete': 'inspect_evidence_then_hint', 'routine_interrupted': 'inspect_side_effects',
    'routine_failed': 'inspect_routine_output', 'routine_changed': 'review_registered_routine',
    'unknown': 'inspect_evidence',
}


def blocker(code, detail, evidence=()):
    if code not in ACTIONS:
        code = 'unknown'
    return dict(code=code, detail=detail, action=ACTIONS[code], evidence=list(evidence))


def plan_error_code(message):
    """Stable root causes survive a model renaming phases or inventing new prose IDs."""
    for fragment, code in (
        ('unknown checks', 'unknown_check_id'), ('mandatory preflight', 'missing_preflight'),
        ('desktop lease', 'resource_mismatch'), ('plan schema', 'invalid_plan_schema'),
        ('requires a later phase artifact', 'artifact_dependency'),
        ('before its producer phase', 'artifact_dependency')):
        if fragment in message:
            return code
    return message


def prepare_revision(task, request, validate, limit, checkers=None):
    """Validate against a copy. The caller persists the proposal, never edits a live step."""
    canonical = deepcopy(request)
    if not canonical.get('check_updates'):
        canonical.pop('check_updates', None)
    for phase in canonical.get('plan', {}).get('phases', []):
        if not phase.get('milestones'):
            phase.pop('milestones', None)
        if phase.get('requires_desktop') is False:
            phase.pop('requires_desktop')  # old accepted revisions did not carry this default
    fingerprint = hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()
    old = task.get('revision_requests', {}).get(request['request_id'])
    if old:
        if old['sha256'] != fingerprint:
            raise ValueError('request_id was already used with different content')
        return None
    if task.get('pending_revision'):
        raise ValueError('another revision is pending; inspect its result first')
    if task['state'] not in ('running', 'blocked', 'needs_user', 'awaiting_approval'):
        raise ValueError('revisions require an existing running or waiting plan')
    if request['base_revision'] != task.get('plan_revision', 0):
        raise ValueError('stale base_revision; read the current plan before revising')
    if task['used'].get('plan_revisions', 0) >= limit:
        raise ValueError('plan revision limit reached; cumulative budget and history are retained')
    phases = (task.get('plan') or {}).get('phases', [])
    index = next((i for i, p in enumerate(phases) if p['id'] == request['from_phase']), None)
    if not phases and task.get('block_kind') == 'planning_error' and request['from_phase'] == 'initial':
        index = 0
    if index is None or any(p['status'] != 'done' for p in phases[:index]):
        raise ValueError('from_phase must exist and all preceding phases must be done')
    prefix = deepcopy(phases[:index])
    candidate = deepcopy(task)
    replacements = []
    locked = {c for p in prefix for c in p['checks'] + p.get('preflight', []) + p.get('final_checks', [])}
    existing = {c['id']: c for c in candidate['checks']}
    for update in request.get('check_updates', []):
        old_check = existing.get(update['id'])
        if not old_check or old_check.get('origin', 'requester') not in ('drafted', 'generated') or \
                old_check.get('scope', 'final') != 'phase' or old_check.get('covers'):
            raise ValueError('check updates may only replace phase-local generated/drafted checks; requester acceptance is immutable')
        if update['id'] in locked:
            raise ValueError('check update affects a retained completed phase; revise from that phase instead')
        if old_check['type'] == 'dir_unchanged' or update['type'] == 'dir_unchanged':
            raise ValueError('cannot replace a baseline protection check or take a new historical baseline')
        if not checkers or update['type'] not in checkers:
            raise ValueError('updated check uses an unavailable checker: ' + update['type'])
        new_check = dict(old_check, type=update['type'], params=checkers[update['type']].validate(update['params']))
        if new_check == old_check:
            raise ValueError('check update makes no change: ' + update['id'])
        candidate['checks'][candidate['checks'].index(old_check)] = new_check
        existing[update['id']] = new_check
        replacements.append({'id': update['id'], 'before': deepcopy(old_check), 'after': deepcopy(new_check),
                             'reason': update['reason']})
    proposed = deepcopy(request['plan'])
    if {p['id'] for p in prefix} & {p['id'] for p in proposed['phases']}:
        raise ValueError('replacement phase ids overlap the retained prefix')
    fields = ('id', 'title', 'done_when', 'checks', 'resource', 'requires_desktop', 'produces', 'requires', 'final_checks', 'preflight', 'milestones')
    proposed['phases'] = [{k: p.get(k, 'background' if k == 'resource' else False if k == 'requires_desktop' else []) for k in fields}
                          for p in prefix] + proposed['phases']
    plan = validate(candidate, proposed)
    # Keep prior checks assigned to the replaced suffix. A replacement cannot silently drop a gate.
    covered = {c for p in plan['phases'][index:] for c in p['checks'] + p['final_checks'] + p.get('preflight', [])}
    required = {c for p in phases[index:] for c in p['checks'] + p.get('final_checks', []) + p.get('preflight', [])}
    old_preflight = {c for p in phases[index:] for c in p.get('preflight', [])}
    new_preflight = {c for p in plan['phases'][index:] for c in p.get('preflight', [])}
    if old_preflight - new_preflight:
        raise ValueError('replacement drops mandatory preflight checks')
    missing = required - covered
    if missing:
        raise ValueError('replacement drops existing checks: ' + ', '.join(sorted(missing)))
    for i, previous in enumerate(prefix):
        plan['phases'][i].update(status=previous['status'], verified=previous.get('verified', False))
    return dict(request_id=request['request_id'], sha256=fingerprint, base_revision=request['base_revision'],
                from_index=index, reason=request['reason'], plan=plan, check_updates=replacements)
