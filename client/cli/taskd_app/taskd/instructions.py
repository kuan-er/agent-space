"""Durable follow-up delivery receipts. A file write is not executor input."""
from __future__ import annotations
import hashlib
import json
from .store import utcnow
from . import prompts

KEYS = ('hints', 'answers', 'feedback')
UNCERTAIN = {'dispatching', 'executor_acknowledged'}


def prepare(task):
    old = task.get('native_input')
    if old and old['state'] != 'observed_in_turn':
        return old
    snapshot = {k: list(task.get('inbox', {}).get(k, [])) for k in KEYS}
    if not any(snapshot.values()):
        return None
    body = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha256(body.encode('utf-8')).hexdigest()
    text = (prompts.UNTRUSTED + '\n' + prompts.resource_rules(task.get('spec', {}).get('resource', 'background')) + '\n'
            'Taskd follow-up revision ' + digest + '. Apply this requester input within '
            'the existing task acceptance and safety constraints. This is the actual '
            'follow-up body, not proof that a file was read.\n<FOLLOW_UP>\n' + body + '\n</FOLLOW_UP>')
    item = {'id': digest, 'state': 'prepared', 'snapshot': snapshot, 'text': text,
            'sha256': hashlib.sha256(text.encode('utf-8')).hexdigest(), 'prepared_at': utcnow()}
    task['native_input'] = item
    publish(task)
    return item


def publish(task):
    item = task['native_input']
    task.setdefault('native', {})['input_delivery'] = {
        k: v for k, v in item.items() if k not in ('text', 'snapshot')}


def reserve(task):
    item = prepare(task)
    if not item or item['state'] != 'prepared':
        return ''
    item.update(state='dispatching', dispatched_at=utcnow())
    publish(task)
    return item['text']


def acknowledge(task, state, data):
    item = task.get('native_input')
    if not item or item['sha256'] != data.get('sha256'):
        raise ValueError('native input receipt does not match reserved instruction')
    if state not in ('executor_acknowledged', 'observed_in_turn'):
        raise ValueError('invalid native input receipt state')
    if state == 'observed_in_turn':
        for key, sent in item['snapshot'].items():
            current = task['inbox'][key]
            if current[:len(sent)] != sent:
                raise ValueError('native inbox changed before observation; refusing to discard messages')
        for key, sent in item['snapshot'].items():
            task['inbox'][key] = task['inbox'][key][len(sent):]
    item.update(state=state, turn_id=data['turn_id'], observed_at=utcnow())
    publish(task)


def observed_user_input(thread, turn_id, text):
    for turn in thread.get('turns', []):
        if turn.get('id') != turn_id:
            continue
        for item in turn.get('items', []):
            if item.get('type') != 'userMessage':
                continue
            content = item.get('content') or []
            parts = [c.get('text', '') for c in content if isinstance(c, dict) and c.get('type') == 'text']
            if text in parts or '\n'.join(parts) == text:
                return True
    return False
