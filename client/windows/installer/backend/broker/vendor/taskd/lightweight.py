"""Bounded execution without a planner. Routines are registered by the node owner;
network requests select a name and finite parameters, never a command line.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import time

from . import prompts
from .activity import execution
from .platform import proc
from .protocol import Rejected, conforms, normalize_spec
from .resources import DESKTOP_CHECKS, desktop_enabled
from .store import atomic_json, utcnow


REPORT = {
    'type': 'object', 'additionalProperties': False,
    'properties': {
        'status': {'type': 'string', 'enum': ['claims_done', 'blocked', 'needs_user']},
        'summary': {'type': 'string'},
        'artifacts': {'type': 'array', 'items': {'type': 'string'}},
        'detail': {'type': 'string'},
    },
    'required': ['status', 'summary', 'artifacts', 'detail'],
}


def is_light(task):
    return task['spec'].get('orchestration', 'planned') in ('direct', 'routine')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def routine(config, name, peer):
    registry = config.get('routines') or {}
    item = registry.get(name) if isinstance(registry, dict) else None
    peers = item.get('peers', []) if isinstance(item, dict) else []
    if not isinstance(peers, list) or not isinstance(item, dict) or peer not in peers:
        raise Rejected('routine_unavailable', 'routine is not registered for this peer')
    argv = item.get('argv')
    if (not isinstance(argv, list) or not argv or not all(isinstance(v, str) and v for v in argv)
            or not Path(argv[0]).is_absolute()):
        raise Rejected('invalid_routine', 'owner routine requires a fixed argv with an absolute executable')
    if (item.get('resource', 'background') not in ('background', 'desktop') or
            not isinstance(item.get('acceptance'), list) or not item['acceptance']):
        raise Rejected('invalid_routine', 'owner routine requires resource and deterministic acceptance')
    if not isinstance(item.get('constraints', []), list):
        raise Rejected('invalid_routine', 'routine constraints must be a list')
    if not isinstance(item.get('repair', False), bool):
        raise Rejected('invalid_routine', 'routine repair must be a boolean')
    return item


def normalize(message, config, peer, checkers):
    request = deepcopy(message)
    item = None
    if isinstance(request, dict) and request.get('taskd') == 'run':
        if request.get('mode') is not None:
            raise Rejected('invalid_spec', 'taskd=run does not accept mode; select a registered routine by name')
        name = request.get('routine')
        if name is not None:
            if not isinstance(name, str):
                raise Rejected('invalid_spec', 'routine must be a registered name')
            item = routine(config, name, peer)
            choices, params = item.get('parameters', {}), request.get('params', {})
            if not isinstance(choices, dict) or not isinstance(params, dict) or set(params) != set(choices):
                raise Rejected('invalid_spec', 'routine parameters must match its registered parameter names')
            for key, value in params.items():
                allowed = choices[key]
                if (not isinstance(allowed, list) or not allowed or isinstance(value, (dict, list)) or
                        not any(type(value) is type(v) and value == v for v in allowed)):
                    raise Rejected('invalid_spec', 'routine parameter is not an allowed value: ' + key)
            resource = item.get('resource', 'background')
            if request.get('resource', 'auto') not in ('auto', resource):
                raise Rejected('invalid_spec', 'request resource conflicts with the registered routine')
            request['resource'] = resource
            extra = request.get('acceptance', [])
            if not isinstance(extra, list):
                raise Rejected('invalid_spec', 'acceptance must be a list')
            request['acceptance'] = list(item['acceptance']) + extra
            constraints = request.get('constraints', [])
            if not isinstance(constraints, list):
                raise Rejected('invalid_spec', 'constraints must be a list')
            # A fixed script cannot interpret new prose restrictions. Reject them instead of
            # executing a deployment which might violate the requester's narrower authority.
            registered = item.get('constraints', [])
            if any(c not in registered for c in constraints):
                raise Rejected('invalid_spec', 'new constraints require direct execution or a separately registered routine')
            request['constraints'] = list(registered)
        elif 'params' in request:
            raise Rejected('invalid_spec', 'params requires a registered routine')
    elif isinstance(request, dict) and ('routine' in request or 'params' in request):
        raise Rejected('invalid_spec', 'registered routines require taskd=run')
    spec = normalize_spec(request, checkers, config.get('limits'))
    if item:
        spec.update(orchestration='routine', routine=name, params=params, routine_digest=digest(item))
    if spec['orchestration'] != 'planned':
        needs_desktop = any(c['type'] in DESKTOP_CHECKS for c in spec['acceptance'])
        if needs_desktop and spec['resource'] == 'background':
            raise Rejected('invalid_spec', 'desktop acceptance requires resource=desktop')
        if spec['resource'] == 'auto':
            spec['resource'] = 'desktop' if needs_desktop else 'background'
        if spec['resource'] == 'desktop' and not desktop_enabled(config):
            raise Rejected('resource_unavailable', 'desktop execution is not enabled on this node')
    return spec


def prepare(engine, task):
    """One program-owned phase, also inspectable when plan approval is requested."""
    spec = task['spec']
    task['plan'] = {'phases': [{
        'id': 'run', 'title': spec['goal'][:120], 'done_when': 'All explicit acceptance checks pass',
        'checks': [], 'preflight': [], 'final_checks': [c['id'] for c in task['checks']],
        'resource': spec['resource'], 'requires_desktop': spec['resource'] == 'desktop',
        'produces': [], 'requires': [], 'milestones': [], 'status': 'pending',
    }], 'approval_reasons': [], 'notes': 'Single execution; no automatic planning or replanning.'}
    task['plan_revision'] = 1
    task['state'] = 'awaiting_approval' if spec['plan_approval'] == 'always' else 'running'
    engine.store.log(task['id'], 'direct_ready', orchestration=spec['orchestration'])
    if task['state'] == 'awaiting_approval':
        task['question'] = 'approve_plan'
        engine.send(task, '请确认这一次执行及验收。', plan=task['plan'])
    engine.store.save(task)


def _record(engine, task, report):
    report = dict(report)
    report['artifacts'] = list(dict.fromkeys(report['artifacts'] + [
        c['params']['file'] for c in task['checks'] if isinstance(c.get('params', {}).get('file'), str)]))
    evidence = engine._evidence(report, engine.task_work_dir(task))
    task['light_claimed_done'] = report['status'] == 'claims_done'
    task['last_step'] = {'summary': report['summary'][:2000], 'artifacts': report['artifacts'][:50],
                         'next': '', 'blocker': report.get('detail') or None, 'at': utcnow()}
    engine.store.log(task['id'], 'step', n=task['used']['steps'], phase='run', ok=True,
                     report=report, evidence=evidence, new=[])
    engine.store.save(task)


def _routine_step(engine, task, item):
    tid = task['id']
    with engine.store.lock(tid):
        task = engine.store.load(tid)
        if task['flags'].get('cancel'):
            engine._stop(task, 'canceled')
            return
        task['routine_state'] = 'started'
        task['step_in_progress'] = {'phase': 'run', 'routine': task['spec']['routine'], 'since': utcnow()}
        engine.store.save(task)  # durable before side effects; never rerun after an ambiguous crash
    timeout = min(task['spec']['budget']['step_timeout_min'] * 60,
                  max(0.01, task['spec']['budget']['max_hours'] * 3600 - task['used']['seconds']))
    started = time.monotonic()
    try:
        with execution(engine.store.activity, tid, timeout, purpose='routine'):
            result = proc.run(item['argv'], cwd=engine.task_work_dir(task),
                              stdin_text=json.dumps({'params': task['spec']['params'],
                                                     'inputs_dir': task.get('inputs_dir')}, ensure_ascii=False),
                              timeout=timeout, cancel=engine.cancel_events.get(tid))
    except OSError as error:
        result = proc.ProcResult(-1, '', str(error), duration=time.monotonic() - started)
    engine._timing(tid, 'routine', result.duration)
    # Output is task data. Keep it off command lines and out of model instructions.
    work = engine.task_work_dir(task)
    log = work / 'routine-output.json'
    atomic_json(log, {'exit_code': result.code, 'timed_out': result.timed_out,
                      'canceled': result.canceled, 'stdout': result.stdout, 'stderr': result.stderr})
    with engine.store.lock(tid):
        task = engine.store.load(tid)
        task['step_in_progress'] = None
        task['routine_state'] = 'passed' if result.ok else 'failed'
        task['used']['steps'] += 1
        task['used']['seconds'] += result.duration
        if result.ok:
            task['state'] = 'verifying'
        _record(engine, task, {'status': 'claims_done' if result.ok else 'blocked',
                               'summary': 'Routine exited with status %s' % result.code,
                               'artifacts': ['routine-output.json'], 'detail': ''})
        if task['flags'].get('cancel') or result.canceled:
            engine._stop(task, 'canceled')
        elif result.timed_out:
            engine._block(task, 'Routine timed out; inspect side effects before continuing', kind='routine_interrupted')
        elif result.ok:
            task['state'] = 'verifying'
            engine.store.save(task)
        elif not item.get('repair', False):
            engine._block(task, 'Routine failed; automatic repair is disabled', kind='routine_failed')
        # Otherwise the next scheduled step gets one model call, within the existing budget.


def step(engine, tid):
    with engine.store.lock(tid):
        task = engine.store.load(tid)
        spec = task['spec']
        if task.pop('retry_system_checks', False) and task.get('light_claimed_done'):
            task['state'] = 'verifying'
            engine.store.save(task)
            return
        item = None
        if spec['orchestration'] == 'routine':
            try:
                item = routine(engine.config, spec['routine'], task['peer'])
                if digest(item) != spec['routine_digest']:
                    raise Rejected('routine_changed', 'registered routine changed; submit a new reviewed request')
            except Rejected as error:
                engine._block(task, error.message, kind='routine_changed')
                return
            if not task.get('routine_state'):
                pass
            elif task['routine_state'] == 'started' and not (item.get('repair', False) and task.get('light_resume_authorized')):
                engine._block(task, 'Routine outcome is unknown after interruption; it will not be repeated', kind='routine_interrupted')
                return
            elif not item.get('repair', False):
                # A follow-up may recheck externally repaired results, never rerun the script.
                if task['routine_state'] == 'passed':
                    task['state'] = 'verifying'
                    engine.store.save(task)
                else:
                    engine._block(task, 'Routine failed; automatic repair is disabled', kind='routine_failed')
                return
        if not item or task.get('routine_state'):
            if task.get('light_model_calls', 0) > task.get('light_model_base', 0):
                engine._block(task, 'Single execution did not finish; send guidance to continue explicitly', kind='direct_incomplete')
                return
            task['light_model_calls'] = task.get('light_model_calls', 0) + 1
            task.pop('light_resume_authorized', None)
            engine.store.save(task)  # reserve the call before starting it
            prompt = ('Complete this small task in this single execution. Use existing scripts/tools when possible. '
                      'Do not produce a phase plan or a separate narrative report. Preserve backups and file integrity; '
                      'perform the real invocation/tests required by the goal. Return the short JSON result. '
                      'If blocked or missing authorization, say so; do not expand scope. '
                      'The node runs the explicit acceptance checks afterward.\n\n' + prompts.UNTRUSTED + '\n\n' +
                      prompts.task_block(spec, engine.task_work_dir(task), task['checks'], task.get('inputs_dir')))
            if item:
                prompt += ('\nA registered routine already ran. Inspect routine-output.json and current side effects '
                           'before making a bounded repair; never blindly repeat the deployment.')
            prompt += '\n\n' + prompts.fence('CONTEXT', json.dumps({
                'previous': task.get('previous'), 'last_step': task.get('last_step'),
                'messages': task['inbox'], 'interrupted': task['flags'].get('interrupted'),
            }, ensure_ascii=False))
            task['inbox'] = {'hints': [], 'answers': [], 'feedback': []}
            engine.store.save(task)
    if item and not task.get('routine_state'):
        _routine_step(engine, task, item)
        return
    report, result = engine._run_exec(tid, prompt, REPORT, None, 'run')
    with engine.store.lock(tid):
        task = engine.store.load(tid)
        if task['flags'].get('cancel') or result.canceled:
            engine._stop(task, 'canceled')
            return
        if not result.ok or not conforms(report, REPORT):
            engine._block(task, result.error or 'Invalid short execution result; no automatic report-repair call', kind='direct_incomplete')
            return
        _record(engine, task, report)
        if report['status'] == 'claims_done':
            task['state'] = 'verifying'
        elif report['status'] == 'needs_user':
            task['state'], task['question'] = 'needs_user', report['detail'] or report['summary']
            engine.send(task, task['question'], question=task['question'])
        else:
            engine._block(task, report['detail'] or report['summary'], kind='direct_incomplete')
            return
        engine.store.save(task)


def verification_failed(engine, task, results):
    """Only an owner-approved routine may use one model call after a failed check."""
    spec = task['spec']
    repair = False
    if spec['orchestration'] == 'routine' and not task.get('light_model_calls'):
        try:
            item = routine(engine.config, spec['routine'], task['peer'])
            repair = item.get('repair', False) and digest(item) == spec['routine_digest']
        except Rejected:
            pass
    if repair:
        task['inbox']['feedback'].append('Acceptance failed: ' + json.dumps(results, ensure_ascii=False)[:8000])
        task['state'] = 'running'
        engine.store.save(task)
    else:
        engine._block(task, 'Explicit acceptance failed; no automatic planning or repeat execution',
                      kind='verification', results=results)
