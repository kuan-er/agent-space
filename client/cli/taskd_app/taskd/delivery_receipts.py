"""Provider-side delivery facts. An RPC ACK is not requester verification."""
from pathlib import Path
from .store import utcnow, atomic_json
from .checkers.generic import sha256_file


def record(store, task, message, files, *, result=None, reason=None):
    metadata = (result or {}).get('metadata', {}) if isinstance(result, dict) else {}
    receipt = {'schema': 1, 'task': task['id'], 'execution_state': task['state'],
               'message_state': message, 'observed_at': utcnow(), 'reason': reason,
               'anet.effect_status': metadata.get('anet.effect_status'),
               'anet.receipt_verified': metadata.get('anet.receipt_verified'),
               'requester_files_verified': False,
               'files': [{'name': Path(p).name, 'size': Path(p).stat().st_size,
                          'sha256': sha256_file(p),
                          'transport_state': 'submitted_acknowledged' if message == 'acknowledged' else 'unconfirmed'}
                         for p in files or []]}
    task['delivery'] = {k: v for k, v in receipt.items() if k != 'files'}
    atomic_json(store.task_dir(task['id']) / 'delivery-receipt.json', receipt)
    store.log(task['id'], 'delivery_receipt', receipt=receipt)
    return receipt
