def clean_execution_facts(raw, observed_now=None):
    """Content-free schema. Never forward session IDs, model text, paths or arbitrary errors."""
    import math
    import time
    if not isinstance(raw, dict) or raw.get('schema') != 1:
        return None
    now = time.time() if observed_now is None else observed_now
    out = {'schema': 1}
    choices = {
        'route': ('native', 'planned', 'direct', 'quick'),
        'routing_reason': ('explicit_planned', 'native_capability_verified', 'disabled_by_owner',
                           'adapter_has_no_verified_native_goals', 'native_probe_failed', 'unavailable'),
        'goal_status': ('active', 'paused', 'blocked', 'complete', 'budget_limited', 'budgetLimited'),
        'turn_status': ('inProgress', 'completed', 'interrupted', 'failed'),
        'last_event': ('turn/started', 'turn/completed', 'thread/goal/updated', 'native/retry',
                       'native/stopping', 'native/stopped', 'native/reconciled', 'native/needsApproval',
                       'item/completed', 'item/commandExecution/outputDelta', 'error'),
    }
    for key, allowed in choices.items():
        if isinstance(raw.get(key), str) and raw[key] in allowed:
            out[key] = raw[key]
    def stamp(value, future=30):
        return value if type(value) in (int, float) and math.isfinite(value) and 0 < value <= now + future else None
    at = stamp(raw.get('updated_at'))
    if at: out['updated_at'] = at
    if type(raw.get('iteration')) is int and 0 <= raw['iteration'] <= 100000:
        out['iteration'] = raw['iteration']
    retry = raw.get('retry')
    if isinstance(retry, dict) and retry.get('reason') in ('model_at_capacity', 'upstream_retry'):
        out['retry'] = {'reason': retry['reason']}
        if type(retry.get('attempt')) is int and 0 <= retry['attempt'] <= 100000:
            out['retry']['attempt'] = retry['attempt']
        for key, future in (('at', 30), ('retry_at', 86400)):
            at = stamp(retry.get(key), future)
            if at: out['retry'][key] = at
    stop = raw.get('stop')
    if isinstance(stop, dict):
        out['stop'] = {k: stop[k] for k in ('requested', 'confirmed', 'resource_retained') if type(stop.get(k)) is bool}
    return out
