"""Conservative presentation of observed execution and delivery facts.

Views never write old records or turn a transport ACK into recipient proof.
Every superseded observation is retained in a bounded field; logs remain intact.
"""
from copy import deepcopy
from datetime import datetime


def _time(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.timestamp() if parsed.tzinfo else None
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None


def native_view(value):
    """Archive an earlier error only after the same turn has observed success."""
    if not isinstance(value, dict):
        return deepcopy(value)
    view = deepcopy(value)
    error = view.get('error')
    if not isinstance(error, dict):
        return view
    observed, completed = _time(error.get('observed_at')), _time(view.get('updated_at'))
    # native/reconciled currently carries only a thread/goal observation. It
    # does not identify the completed turn and cannot resolve a same-turn error.
    if (view.get('last_event') != 'turn/completed'
            or view.get('turn_status') != 'completed'
            or not view.get('turn_id') or error.get('turn_id') != view['turn_id']
            or view.get('completed_turn_id', view['turn_id']) != view['turn_id']
            or observed is None or completed is None or completed < observed):
        return view
    archived = {'error': error, 'resolved_by': view['last_event'],
                'resolved_at': view['updated_at'], 'turn_id': view['turn_id']}
    history = view.get('error_history')
    history = list(history) if isinstance(history, list) else []
    if archived not in history:
        history.append(archived)
    view['error_history'] = history[-8:]
    view.pop('error', None)
    view['error_history_note'] = 'Recent resolved errors; complete event history remains in the task log.'
    return view


def scheduling_view(value, execution_state):
    """Completed business execution does not prove the resource lease released."""
    view = deepcopy(value)
    if execution_state != 'completed' or not isinstance(view, dict):
        return view
    if view.get('state') == 'executing' or str(view.get('state', '')).startswith('waiting_'):
        return {'state': 'execution_finished', 'execution_state': execution_state,
                'resource': view.get('resource'), 'resource_release': 'not_verified',
                'basis': 'completed_task_state', 'resource_observation': view}
    return view


def delivery_view(value, execution_state, *, sending=False, at=None):
    """Keep the prior ACK scoped to the execution snapshot it acknowledged.

    An outgoing message cannot contain the acknowledgement of its own send.
    That fact belongs to the subsequent local receipt/transport observation.
    """
    view = deepcopy(value)
    stale = isinstance(view, dict) and view.get('execution_state') != execution_state
    if not sending and not stale:
        return view
    return {'schema': 2, 'execution_state': execution_state,
            'message_state': 'awaiting_acknowledgement' if sending else 'not_verified_for_current_execution',
            'observed_at': at, 'observation_phase': 'before_send' if sending else 'legacy_snapshot_projection',
            'anet.effect_status': None, 'anet.receipt_verified': None,
            'requester_files_verified': False, 'previous_receipt': view,
            'note': 'This message cannot attest its own send acknowledgement or requester file verification.'}


def envelope_view(value):
    if not isinstance(value, dict):
        return deepcopy(value)
    view = deepcopy(value)
    if 'native' in view:
        view['native'] = native_view(view['native'])
    if 'scheduling' in view:
        view['scheduling'] = scheduling_view(view['scheduling'], view.get('state'))
    if 'delivery' in view:
        view['delivery'] = delivery_view(view['delivery'], view.get('state'))
    if isinstance(view.get('report'), dict):
        report = view['report']
        if 'native' in report:
            report['native'] = native_view(report['native'])
    return view
