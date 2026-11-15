"""Adapt the verified native CLI envelopes without inventing receipt evidence."""
import json
from assistant import Node


class PortalNode(Node):
    def thread(self, task_id):
        thread = super().thread(task_id)
        if thread.get('interaction_id') != task_id or thread.get('role') != 'outbound':
            raise RuntimeError('unexpected native thread identity')
        task = json.loads(self.run('task', 'get', task_id, '--full', timeout=30))
        metadata = task.get('metadata') or {}
        if task.get('id') != task_id or metadata.get('anet.role') != 'requester' or metadata.get('anet.peer_aid') != thread.get('peer'):
            raise RuntimeError('native task identity mismatch')
        return {**thread, 'metadata': metadata}
