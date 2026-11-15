"""Ordered, deterministic phase checkpoints; no executor text can mark one passed."""
import hashlib
import json


def signature(milestone, checks):
    by_id = {c['id']: c for c in checks}
    definitions = [{k: by_id[cid].get(k) for k in ('id', 'type', 'params')}
                   for cid in sorted(milestone['checks'])]
    return hashlib.sha256(json.dumps(definitions, sort_keys=True).encode()).hexdigest()


def pending(task, phase):
    receipts = task.get('progress', {}).get('milestones', {})
    return [m for m in phase.get('milestones', [])
            if receipts.get(phase['id'] + ':' + m['id'], {}).get('signature') != signature(m, task['checks'])]


def view(task, phase):
    items = phase.get('milestones', [])
    remaining = pending(task, phase)
    return {'total': len(items), 'done': len(items) - len(remaining),
            'current': remaining[0]['title'] if remaining else '',
            'id': remaining[0]['id'] if remaining else ''}


def advance(task, phase, results, at):
    """Credit a verified ordered prefix once per check definition, even after replanning."""
    progress = task['progress']
    receipts = progress.setdefault('milestones', {})
    credits = progress.setdefault('milestone_credits', [])
    advanced = []
    for m in pending(task, phase):
        if not all(results.get(cid, {}).get('outcome') == 'pass' for cid in m['checks']):
            break
        sig = signature(m, task['checks'])
        receipts[phase['id'] + ':' + m['id']] = {'signature': sig, 'at': at, 'checks': m['checks']}
        if sig not in credits:
            credits.append(sig)
            advanced.append(m['id'])
    return advanced
