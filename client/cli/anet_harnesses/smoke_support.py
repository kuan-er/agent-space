"""Finite harness verification helpers; never replay an operation."""
import re


def pending_requests(value):
    """Normalize the public service envelope, not its truthiness/key count."""
    if not isinstance(value, dict) or not isinstance(value.get('requests'), list):
        raise ValueError('invalid native input envelope')
    return value['requests']


def failure_summary(events):
    """Keep useful error categories without persisting credentials/error text."""
    rows = []
    for event in events:
        if not isinstance(event, dict):
            continue
        kind = event.get('type')
        if kind not in ('turn/end', 'agent/error', 'error', 'llm/error'):
            continue
        data = event.get('data', {})
        if not isinstance(data, dict):
            continue
        reason = data.get('reason', data)
        if not isinstance(reason, dict):
            continue
        error = reason.get('error') or data.get('error') or reason
        if not isinstance(error, dict):
            error = {'message': str(error)}
        message = str(error.get('message', '')).lower()
        category = next((name for name, needles in (
            ('authentication', ('unauthorized', 'unauthenticated', 'login', 'credential', '401')),
            ('quota', ('quota', 'credit', 'balance', 'insufficient', '402', '429')),
            ('model_unavailable', ('model not found', 'unknown model', 'unsupported model')),
            ('network', ('timeout', 'network', 'connect', 'fetch failed')),
        ) if any(needle in message for needle in needles)), 'runtime')
        category = {'TRANSPORT': 'network', 'TIMEOUT': 'network', 'AUTH': 'authentication',
                    'QUOTA': 'quota', 'UNKNOWN_MODEL': 'model_unavailable'}.get(error.get('code'), category)
        record = {'event': kind, 'category': category}
        for key in ('kind', 'code', 'name'):
            value = (reason if key == 'kind' else error).get(key)
            if isinstance(value, int) or isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,80}', value):
                record[key] = value
        rows.append(record)
    return rows[-8:]
