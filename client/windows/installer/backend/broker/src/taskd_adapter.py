"""Isolated taskd requester adapter for an explicitly launched owned source.

This module is not registered in production builtin.py and reads no configuration
or credentials. The launcher passes the two explicit non-secret startup/source
records. Existing Codex GUI sessions are never discovered or resumed.
"""
import hashlib
import json
from pathlib import Path
import sys
import uuid
from broker import ROOT
from taskd.store import atomic_json, read_json, utcnow
import winlocal

class OwnedSourceRequester:
    name = 'codex_owned'

    def __init__(self, startup_record, source_binding):
        self.record = dict(startup_record)
        self.binding = dict(source_binding)
        if self.binding.get('instance') != self.record.get('instance') or self.binding.get('codex') != self.record.get('codex') or self.binding.get('broker') != self.record.get('broker'):
            raise ValueError('explicit startup and source records do not match')

    def current_session(self): return self.binding['session']
    def find_session(self, task_id, since_ts): return None

    def inspect(self, session_id):
        if session_id != self.binding['session']: raise ValueError('adapter source ID mismatch; zero IPC requests')
        return winlocal.request(self.record, 'inspect_source', binding=self.binding)

    def cwd(self, session_id): return self.inspect(session_id)['cwd']
    def idle(self, session_id): return self.inspect(session_id)['state'] == 'idle'

    def readiness(self):
        try:
            observed = self.inspect(self.binding['session'])
            return {'ready': True, 'source': 'client_owned_stdio', 'host_instance_verified': True,
                    'session': observed['session'], 'at': utcnow()}
        except (OSError, ValueError, RuntimeError, EOFError) as error:
            return {'ready': False, 'error': str(error)[:500], 'at': utcnow()}

    def loaded_sources(self):
        try: value = self.inspect(self.binding['session'])
        except (OSError, ValueError, RuntimeError, EOFError): return []
        return [{'session': value['session'], 'cwd': value['cwd']}]

    def delivery(self, session_id, prompt, cwd, journal):
        if session_id != self.binding['session'] or Path(cwd).resolve() != Path(self.binding['cwd']).resolve():
            return {'state': 'binding_error', 'error': 'explicit owned source mismatch; zero IPC requests', 'at': utcnow()}
        if not isinstance(prompt, str) or len(prompt.encode('utf-8')) > 16384:
            return {'state': 'binding_error', 'error': 'bounded 16 KiB summary required; use separate artifact delivery', 'at': utcnow()}
        pin = {'instance': self.record['instance'], 'session': session_id,
               'prompt_sha256': hashlib.sha256(prompt.encode('utf-8')).hexdigest(),
               'binding_sha256': hashlib.sha256(json.dumps(self.binding, sort_keys=True).encode()).hexdigest()}
        rec = read_json(journal, {})
        state = rec.get('_source_broker')
        if state:
            if state.get('pin') != pin:
                return {'state': 'binding_error', 'error': 'existing callback journal belongs to another source/prompt; original receipt preserved', 'at': utcnow()}
            legacy_unknown = (rec.get('state') == 'failed' and rec.get('request') and
                              not rec.get('ack') and rec.get('error_kind') is None)
            if rec.get('state') in ('completed', 'failed', 'ambiguous') and not legacy_unknown:
                # Historical delivery receipt remains true if the app later exits.
                # This is not a claim that its original source is still live.
                return rec
        else:
            if rec:
                return {'state': 'binding_error', 'error': 'journal has no broker delivery identity; do not migrate or resend automatically', 'at': utcnow()}
            state = {'pin': pin, 'delivery_id': str(uuid.uuid4()), 'created_at': utcnow()}
            rec = {'state': 'requested', '_source_broker': state, 'at': utcnow()}
            atomic_json(journal, rec)  # this ID survives uncertain IPC ACKs
        try:
            value = winlocal.request(self.record, 'deliver', binding=self.binding,
                delivery_id=state['delivery_id'], prompt=prompt)
            rec = dict(value, _source_broker=state)
        except (OSError, ValueError, RuntimeError, EOFError) as error:
            rec.update(state='reconciling', error=str(error)[:500], do_not_resend=True,
                       source_live_verified=False, at=utcnow())
        atomic_json(journal, rec)
        return rec
