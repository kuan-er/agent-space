"""Isolated completed-task artifact queue. No executor, ANet send, or daemon API.

The caller keeps small files on the existing signed ANet route. This module only
prepares immutable snapshots and advances independently authenticated delivery.
"""
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
from .. import private_artifact_dir as private

sys.path.insert(0, str(Path(__file__).resolve().parent / 'vendor'))
from repair_protocol import (canonical, digest, plain_path, validate_manifest,
    validate_name, describe, sha_file, CHUNK, MAX_TOTAL, Lock, now, TASK_ID)
from repair_sender import KnownFailure, UnknownOutcome, NotFound
from connector_delivery import HTTPSWire as ReviewedHTTPSWire, PortalTransport, PART
from owned_receiver import receive_from_portal, identity, require_identity

SCHEMA = 'taskd-auto-artifacts/1'
INLINE_BUDGET = 900_000  # Leaves room for base64 framing + bounded terminal text.
BEGIN_BYTES = 32768


class MutationNotApplied(KnownFailure):
    """Only the new, precisely scoped server contract can prove no mutation."""
    def __init__(self, operation, mid, reason):
        self.operation, self.mid, self.reason = operation, mid, reason
        super().__init__('portal_mutation_not_applied')


class HTTPSWire(ReviewedHTTPSWire):
    """Strict inherited TLS; typed not-applied error, never string classification."""
    def request(self, method, path, body=None, sha=None, binary=False):
        data = body if isinstance(body, bytes) else canonical(body) if body is not None else None
        if data is not None and len(data) > (PART if isinstance(body, bytes) else BEGIN_BYTES):
            reject('request_exceeds_proxy_limit')
        headers = {'Authorization': 'Bearer ' + self.credential,
            'Content-Type': 'application/octet-stream' if isinstance(body, bytes) else 'application/json'}
        if sha: headers['X-Content-SHA256'] = sha
        operation = body.get('op') if isinstance(body, dict) else None
        request_mid = (validate_manifest(body.get('manifest')) if operation == 'begin'
            else body.get('delivery_id') if isinstance(body, dict) else None)
        with self.lock:
            try:
                self.connection.request(method, path, body=data, headers=headers)
                response = self.connection.getresponse(); limit = PART if binary else 1_048_576
                raw = response.read(limit + 1)
                if response.status == 404: raise NotFound('delivery_not_found')
                if response.status >= 500: raise UnknownOutcome('portal_outcome_unknown')
                if len(raw) > limit: raise UnknownOutcome('portal_response_oversized')
                if response.status != 200:
                    try: error = json.loads(raw)
                    except (ValueError, UnicodeError): error = {}
                    if (response.status == 409 and isinstance(error, dict)
                            and method == 'POST' and path == '/api/node/artifacts'
                            and error.get('error') == 'artifact_mutation_not_applied'
                            and error.get('effect') == 'not_applied'
                            and error.get('operation') in ('begin', 'received')
                            and error.get('operation') == operation and error.get('manifest_sha256') == request_mid
                            and re.fullmatch('[0-9a-f]{64}', str(error.get('manifest_sha256', '')))
                            and error.get('reason') in ('prerequisite_pending', 'authorization_denied')):
                        raise MutationNotApplied(error['operation'], error['manifest_sha256'], error['reason'])
                    failure=KnownFailure('portal_request_rejected_without_effect_proof')
                    failure.http_status=response.status
                    failure.reason_code=error.get('reason') if error.get('reason') in (
                        'fragment_validation','short_fragment','fragment_size','immutable_conflict',
                        'chunk_integrity','stored_fragment_integrity','stored_chunk_integrity','request_or_integrity') else 'http_rejected_without_effect_proof'
                    raise failure
                if binary: return raw, response.getheader('X-Content-SHA256', '')
                try: return json.loads(raw)
                except (ValueError, UnicodeError): raise UnknownOutcome('portal_acknowledgement_invalid') from None
            except (OSError, http.client.HTTPException):
                self.connection.close(); raise UnknownOutcome('portal_transport_unknown') from None


def reject(code):
    raise KnownFailure(code)


def read(path):
    p = plain_path(path)
    if not p.is_file() or p.stat().st_size > 1_048_576:
        reject('metadata_unavailable_or_oversized')
    return json.loads(p.read_text(encoding='utf-8'))


def create_json(path, value):
    with private.open_new(plain_path(path)) as out:
        out.write(canonical(value)); out.flush(); os.fsync(out.fileno())


def file_summary(manifest):
    return [{k: f[k] for k in ('name', 'size', 'sha256')} for f in manifest['files']]


def immutable_part_replay_authorized(progress,mid,fi,ci,block):
    """Fail closed without the exact authenticated, pinned storage contract."""
    expected={'schema':'immutable-part-replay/1','manifest_sha256':mid,
        'file':fi,'chunk':ci,'chunk_sha256':digest(block),'chunk_size':len(block),
        'part_bytes':PART,'semantics':'same_key_same_bytes_only',
        'scope':'authenticated_storage_only_no_business_execution'}
    policy=progress.get('immutable_replay')
    return (type(policy) is dict and set(policy)==set(expected)
        and all(type(policy[k]) is type(v) and policy[k]==v for k,v in expected.items()))


def descriptor(manifest):
    mid = validate_manifest(manifest)
    return {'schema': SCHEMA, 'task_id': manifest['task_id'],
            'provider': manifest['provider'], 'requester': manifest['requester'],
            'manifest_sha256': mid, 'file_count': len(manifest['files']),
            'total_bytes': sum(f['size'] for f in manifest['files']),
            'channel': 'portal_https_storage', 'state': 'prepared_not_transmitted',
            'signature_verification': 'not_anet_receipt', 'anet.receipt_verified': None}


def validate_descriptor(value):
    if not isinstance(value, dict) or value.get('schema') != SCHEMA:
        reject('descriptor_schema')
    if not TASK_ID.fullmatch(str(value.get('task_id', ''))): reject('descriptor_task')
    if not re.fullmatch('[0-9a-f]{64}', str(value.get('manifest_sha256', ''))): reject('descriptor_digest')
    if value.get('channel') != 'portal_https_storage' or value.get('signature_verification') != 'not_anet_receipt' or value.get('anet.receipt_verified') is not None:
        reject('descriptor_scope')
    if type(value.get('file_count')) is not int or not 1 <= value['file_count'] <= 64:
        reject('descriptor_file_count')
    if type(value.get('total_bytes')) is not int or not 1 <= value['total_bytes'] <= MAX_TOTAL:
        reject('descriptor_size')
    for key in ('provider', 'requester'):
        if not isinstance(value.get(key), str) or not 1 <= len(value[key]) <= 200:
            reject('descriptor_party')
    if len(canonical(value)) > 2048: reject('descriptor_too_large')
    return value


def prepare_completed(destination, work_dir, task, paths, inline_budget=INLINE_BUDGET, *, make_directory=None):
    """Explicit local taskd hook, called once on NEW completed task artifacts.

    No network is opened. Each source must be inside the authorized work_dir.
    A new owned directory is required; interrupted preparations are preserved.
    Local snapshot/hash time is not claimed to be asynchronous. Upload is.
    """
    if task.get('state') != 'completed' or not TASK_ID.fullmatch(str(task.get('id', ''))):
        reject('only_completed_task_supported')
    if type(inline_budget) is not int or not 0 <= inline_budget <= INLINE_BUDGET:
        reject('inline_budget')
    for key in ('provider', 'requester', 'taskd_version'):
        if not isinstance(task.get(key), str) or not 1 <= len(task[key]) <= 200:
            reject('task_party_or_version')
    work = plain_path(work_dir).resolve()
    dest = plain_path(destination).resolve()
    if dest.exists() or dest.is_relative_to(work) or work.is_relative_to(dest):
        reject('new_independent_spool_required')
    if not dest.parent.is_dir(): reject('spool_parent_missing')
    if not isinstance(paths, list) or not 1 <= len(paths) <= 64 or len(set(paths)) != len(paths):
        reject('finite_unique_artifact_list_required')
    sources = []
    for name in paths:
        rel = Path(name)
        if rel.is_absolute() or '..' in rel.parts: reject('source_outside_workspace')
        p = plain_path(work / rel)
        if not p.is_relative_to(work) or not stat.S_ISREG(p.stat().st_mode): reject('source_not_regular')
        if not 0 < p.stat().st_size <= MAX_TOTAL: reject('source_size')
        sources.append(p)
    total = sum(p.stat().st_size for p in sources)
    if total > MAX_TOTAL or shutil.disk_usage(dest.parent).free < total + 1_048_576:
        reject('spool_capacity')
    mkdir = make_directory or (lambda path: path.mkdir(mode=0o700))
    mkdir(dest)
    payload = dest / 'payload'; mkdir(payload)
    private.write_new(dest / 'queue.lock',b'0')
    create_json(dest / 'owner.json', {'schema': SCHEMA, 'root': identity(dest, True),
        'payload': identity(payload, True), 'lock': identity(dest / 'queue.lock')})
    inline, external, mapping = [], [], []
    remaining = inline_budget
    for index, source in enumerate(sources):
        name = 'n%03d-' % index + source.name[:120]
        validate_name(name)
        target = payload / name
        with source.open('rb') as inp, private.open_new(target) as out:
            before = os.fstat(inp.fileno())
            if not stat.S_ISREG(before.st_mode): reject('source_changed_type')
            h = hashlib.sha256(); chunks = []; copied = 0
            while True:
                block = inp.read(CHUNK)
                if not block: break
                copied += len(block)
                if copied > before.st_size or copied > MAX_TOTAL: reject('source_grew')
                out.write(block); h.update(block); chunks.append(digest(block))
            out.flush(); os.fsync(out.fileno())
            after = os.fstat(inp.fileno())
        current = source.stat()
        key = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns)
        if key(before) != key(after) or key(after) != key(current) or copied != before.st_size:
            reject('source_changed_during_snapshot')
        f = {'name': name, 'size': copied, 'sha256': h.hexdigest(), 'chunks': chunks}
        if sha_file(target) != f['sha256']: reject('snapshot_integrity')
        mapping.append({'source_relative': str(source.relative_to(work)), 'wire_name': name})
        if copied <= remaining:
            inline.append(f); remaining -= copied
        else: external.append(f)
    manifest = None
    if external:
        manifest = {'schema': 'delivery-repair/1', 'task_id': task['id'],
            'execution_state': 'completed', 'provider': task['provider'],
            'requester': task['requester'], 'taskd_version': task['taskd_version'],
            'chunk_bytes': CHUNK, 'files': external}
        validate_manifest(manifest)
        if len(canonical({'op': 'begin', 'manifest': manifest})) > BEGIN_BYTES:
            reject('portal_manifest_32k_limit')
        create_json(dest / 'manifest.json', manifest)
    plan = {'schema': SCHEMA, 'task_id': task['id'], 'execution_state': 'completed',
        'provider': task['provider'], 'requester': task['requester'],
        'inline': inline, 'manifest': manifest, 'mapping': mapping,
        'external_delivery': descriptor(manifest) if manifest else None}
    create_json(dest / 'plan.json', plan)
    create_json(dest / 'queue.json', {'schema': SCHEMA, 'plan_sha256': digest(canonical(plan)),
        'state': 'waiting_terminal_receipt' if manifest else 'inline_only',
        'pending': None, 'terminal_observation': None, 'events': [],
        'execution_state': 'completed', 'execution_modified': False,
        'server_files_verified': False, 'recipient_files_verified': False,
        'anet.receipt_verified': None, 'requester_acceptance': 'not_verified'})
    return Job(dest).terminal_payload()


def prepare_for_terminal(*args, **kwargs):
    """Failure never authorizes suppression or replay of the terminal summary."""
    try: return prepare_completed(*args, **kwargs)
    except (KnownFailure, OSError, ValueError, TypeError):
        task = kwargs.get('task') or (args[2] if len(args) > 2 and isinstance(args[2], dict) else {})
        execution_state = task.get('state', 'unverified')
        return {'execution_state': execution_state, 'inline_files': [],
            'external_delivery': {'schema': SCHEMA, 'state': 'preparation_failed',
                'reason': 'local_artifact_preparation_failed', 'files_verified': False},
            'send_terminal_summary': execution_state == 'completed', 'resume_execution': False}


class PortalSession:
    """Connector supplies its ALREADY loaded binding. Nothing creates a token.

    expected_origin and expected_aid are local node configuration, never values
    from the remote descriptor. No redirects or TLS weakening are implemented.
    """
    def __init__(self, existing_binding, expected_aid, expected_origin, role):
        if role not in ('provider', 'requester'): reject('portal_role')
        if existing_binding.get('aid') != expected_aid or existing_binding.get('portal', '').rstrip('/') != expected_origin.rstrip('/'):
            reject('existing_binding_mismatch')
        token = existing_binding.get('node_token')
        if not isinstance(token, str) or not 16 <= len(token) <= 4096 or any(not 33 <= ord(c) <= 126 for c in token):
            reject('existing_binding_credential_invalid')
        self.aid, self.role = expected_aid, role
        self._wire = HTTPSWire(expected_origin, token)
        try:
            info = self._wire.request('GET', '/api/node/info')
            kinds = ('agent',) if role == 'provider' else ('device', 'agent')
            if not isinstance(info, dict) or info.get('aid') != expected_aid or info.get('kind') not in kinds:
                reject('authenticated_node_identity_or_role')
        except BaseException:
            self._wire.close(); raise

    def json(self, value): return self._wire.json(value)
    def request(self, *args, **kwargs): return self._wire.request(*args, **kwargs)
    def close(self): self._wire.close()


class Job:
    def __init__(self, directory):
        self.root = plain_path(directory).resolve()
        owner = read(self.root / 'owner.json')
        if owner.get('schema') != SCHEMA: reject('spool_owner')
        for name, key, isdir in (('', 'root', True), ('payload', 'payload', True), ('queue.lock', 'lock', False)):
            require_identity(self.root / name, owner.get(key), isdir)
        if os.name == 'posix' and any((self.root / p).stat().st_mode & 0o077 for p in ('', 'payload')):
            reject('spool_not_private')
        self.plan = read(self.root / 'plan.json')
        if self.plan.get('schema') != SCHEMA or self.plan.get('execution_state') != 'completed': reject('plan_schema')
        self.pin = digest(canonical(self.plan))
        self.manifest = self.plan['manifest']
        self.mid = validate_manifest(self.manifest) if self.manifest else None
        if self.manifest:
            if read(self.root / 'manifest.json') != self.manifest or descriptor(self.manifest) != self.plan['external_delivery']:
                reject('spool_manifest_changed')
        self.state = self._read_state()

    def _read_state(self):
        value = read(self.root / 'queue.json')
        if value.get('schema') != SCHEMA or value.get('plan_sha256') != self.pin:
            reject('spool_queue_identity')
        return value

    def save(self, event):
        self._read_state()  # Never overwrite an unrelated queue file.
        self.state['events'] = (self.state.get('events', []) + [{'at': now(), 'event': event}])[-128:]
        tmp = self.root / ('queue.new-' + os.urandom(12).hex())
        create_json(tmp, self.state)
        os.replace(tmp, plain_path(self.root / 'queue.json'))

    def terminal_payload(self):
        return {'execution_state': 'completed', 'send_terminal_summary': True,
            'inline_files': [str(self.root / 'payload' / f['name']) for f in self.plan['inline']],
            'external_delivery': self.plan['external_delivery'], 'resume_execution': False}

    def observe_terminal(self, observation):
        """Trusted adapter gives normalized, observed transport fields (not chat)."""
        with Lock(self.root / 'queue.lock'):
            self.state = self._read_state()
            if (observation.get('task_id') != self.plan['task_id'] or observation.get('state') != 'completed'
                    or observation.get('peer') != self.plan['requester']
                    or observation.get('receipt_verified') != 'verified'):
                reject('terminal_transport_not_verified')
            self.state['terminal_observation'] = {k: observation.get(k) for k in
                ('task_id', 'state', 'peer', 'receipt_verified', 'effect_status')}
            if self.state['state'] == 'waiting_terminal_receipt': self.state['state'] = 'queued'
            self.save('terminal_transport_observed')

    def verify_payload(self):
        for f in self.manifest['files']:
            if describe(self.root / 'payload' / f['name']) != f: reject('frozen_payload_changed')

    def remote(self, session):
        value = session.json({'op': 'status', 'delivery_id': self.mid})
        if not isinstance(value, dict) or value.get('manifest_sha256') != self.mid or value.get('manifest') != self.manifest:
            reject('remote_manifest_mismatch')
        present = value.get('present')
        if not isinstance(present, list) or len(present) != len(self.manifest['files']): reject('remote_progress')
        for indices, f in zip(present, self.manifest['files']):
            if not isinstance(indices, list) or len(indices) != len(set(indices)) or any(type(i) is not int or not 0 <= i < len(f['chunks']) for i in indices):
                reject('remote_progress')
        return value

    def receipt(self, value):
        if (not isinstance(value, dict) or value.get('schema') != 'delivery-repair/1'
                or value.get('task_id') != self.plan['task_id'] or value.get('manifest_sha256') != self.mid
                or value.get('files') != file_summary(self.manifest) or value.get('server_files_verified') is not True
                or value.get('recipient_files_verified') is not False or value.get('channel') != 'portal_https_storage'
                or value.get('signature_verification') != 'not_anet_receipt' or value.get('anet.receipt_verified') is not None):
            reject('server_receipt_scope')
        return value

    def advance(self, session):
        """One begin/24-KiB-part/commit per tick, never an execution retry.

        Pending mutation is journaled BEFORE sending. Unknown outcomes query
        the same ID. An exact authenticated immutable-fragment contract permits
        only bounded same-key/same-byte storage replay; no business or ANet send.
        Without that positive contract the original query-only rule remains.
        """
        with Lock(self.root / 'queue.lock'):
            self.state = self._read_state()
            if not self.manifest: return self.state
            if session.role != 'provider' or session.aid != self.plan['provider']: reject('sender_role')
            if not self.state.get('terminal_observation'): return self.state
            try:
                try: remote = self.remote(session)
                except NotFound:
                    if self.state.get('pending') or self.state.get('begun'):
                        self.state['state'] = 'uncertain_missing_delivery'; self.save('same_delivery_not_found'); return self.state
                    self.verify_payload()
                    self.state.update(pending={'op': 'begin'}, state='begin_pending'); self.save('begin_intent')
                    session.json({'op': 'begin', 'manifest': self.manifest})
                    # Response is deliberately not promoted to file verification.
                    self.state.update(pending=None, begun=True, state='transferring'); self.save('begin_acknowledged')
                    return self.state
                self.state['begun'] = True
                if remote.get('receipt'):
                    receipt = self.receipt(remote['receipt'])
                    self.state.update(pending=None, state='server_stored', server_files_verified=True, server_receipt=receipt)
                    recipient = remote.get('recipient_receipt')
                    if recipient:
                        validate_recipient_receipt(recipient, self.mid, self.manifest)
                        self.state.update(state='recipient_reported', recipient_report=recipient,
                            recipient_files_verified=False, recipient_claim='reported_by_authenticated_requester')
                    self.save('server_storage_observed'); return self.state
                pending = self.state.get('pending')
                if pending:
                    resolved = pending['op'] == 'begin'
                    if pending['op'] == 'part':
                        fi, ci, pi = pending['file'], pending['chunk'], pending['part']
                        block = self.block(fi, ci)
                        progress = PortalTransport(session).check_progress(session.json({'op':'part_status',
                            'delivery_id':self.mid,'file':fi,'chunk':ci}), self.mid,fi,ci,block)
                        resolved = progress['chunk_verified'] or pi in {p['index'] for p in progress['parts']}
                        if not resolved and immutable_part_replay_authorized(progress,self.mid,fi,ci,block):
                            if type(pi) is not int or not 0<=pi<(len(block)+PART-1)//PART:
                                reject('pending_part_identity_invalid')
                            fragment=block[pi*PART:(pi+1)*PART]
                            if digest(fragment)!=pending.get('sha256'):reject('pending_frozen_fragment_differs')
                            attempts=pending.get('immutable_replay_attempts',0)
                            if type(attempts) is not int or not 0<=attempts<3:
                                self.state['state']='attention_replay_limit';self.save('immutable_part_replay_limit');return self.state
                            pending.update(immutable_replay_attempts=attempts+1,
                                original_effect='unknown_not_reclassified',
                                replay_contract_sha256=digest(canonical(progress['immutable_replay'])))
                            self.state['state']='part_pending';self.save('immutable_part_replay_intent')
                            response=session.request('POST',f'/api/node/artifacts/part/{self.mid}/{fi}/{ci}/{pi}',fragment,digest(fragment))
                            ack=PortalTransport(session).check_progress(response,self.mid,fi,ci,block)
                            if not ack['chunk_verified'] and pi not in {p['index'] for p in ack['parts']}:
                                raise UnknownOutcome('part_replay_acknowledgement_unknown')
                            self.state.update(pending=None,state='transferring')
                            self.save('immutable_part_replay_acknowledged');return self.state
                    if not resolved:
                        self.state['state'] = 'unconfirmed_mutation'; self.save('same_delivery_waiting_evidence'); return self.state
                    self.state.update(pending=None, state='transferring'); self.save('pending_reconciled')
                    return self.state  # No second mutation during reconciliation.
                for fi, f in enumerate(self.manifest['files']):
                    for ci, sha in enumerate(f['chunks']):
                        if ci in remote['present'][fi]: continue
                        block = self.block(fi, ci); transport = PortalTransport(session)
                        progress = transport.check_progress(session.json({'op':'part_status','delivery_id':self.mid,'file':fi,'chunk':ci}), self.mid,fi,ci,block)
                        if progress['chunk_verified']:
                            self.state['state']='transferring'; self.save('chunk_completion_observed'); return self.state
                        seen = {p['index'] for p in progress['parts']}
                        count = (len(block)+PART-1)//PART
                        missing = [i for i in range(count) if i not in seen]
                        # Server can crash after persisting the final fragment
                        # but before assembling the immutable chunk. All hashes
                        # were positively verified above: reaffirm one ALREADY
                        # CONFIRMED fragment to trigger the existing idempotent
                        # assembly path. Never replay an unconfirmed fragment.
                        repair_confirmed_part = not missing
                        pi = missing[0] if missing else count-1
                        fragment = block[pi*PART:(pi+1)*PART]
                        self.state.update(pending={'op':'part','file':fi,'chunk':ci,'part':pi,'sha256':digest(fragment),
                            'repair_confirmed_part':repair_confirmed_part}, state='part_pending')
                        self.save('confirmed_fragment_assembly_repair_intent' if repair_confirmed_part else 'part_intent')
                        response = session.request('POST',f'/api/node/artifacts/part/{self.mid}/{fi}/{ci}/{pi}',fragment,digest(fragment))
                        ack = transport.check_progress(response,self.mid,fi,ci,block)
                        if not ack['chunk_verified'] and pi not in {p['index'] for p in ack['parts']}: raise UnknownOutcome('part_acknowledgement_unknown')
                        self.state.update(pending=None, state='transferring'); self.save('part_acknowledged')
                        return self.state
                self.state.update(pending={'op': 'commit'}, state='commit_pending'); self.save('commit_intent')
                receipt = self.receipt(session.json({'op': 'commit', 'delivery_id': self.mid}))
                self.state.update(pending=None, state='server_stored', server_files_verified=True, server_receipt=receipt)
                self.save('server_storage_acknowledged'); return self.state
            except MutationNotApplied as error:
                pending = self.state.get('pending') or {}
                if error.operation == 'begin' and error.mid == self.mid and pending.get('op') == 'begin' and error.reason == 'prerequisite_pending':
                    self.state.update(pending=None, state='waiting_prerequisite')
                    self.save('begin_definitely_not_applied')
                else:
                    self.state['state']='blocked'; self.save('not_applied_scope_mismatch')
                return self.state
            except UnknownOutcome:
                self.state['state'] = 'unconfirmed'; self.save('transport_outcome_unknown'); return self.state
            except (KnownFailure, ValueError, OSError, TypeError, KeyError, IndexError) as error:
                self.state['last_failure']={'type':type(error).__name__,
                    'http_status':getattr(error,'http_status',None),'reason_code':getattr(error,'reason_code',None)}
                self.state['state'] = 'blocked'; self.save('delivery_validation_or_access_failed'); return self.state

    def block(self, fi, ci):
        f = self.manifest['files'][fi]
        with plain_path(self.root/'payload'/f['name']).open('rb') as source:
            source.seek(ci*CHUNK); value=source.read(min(CHUNK,f['size']-ci*CHUNK))
        if digest(value) != f['chunks'][ci]: reject('frozen_payload_changed')
        return value


def validate_recipient_receipt(receipt, mid, manifest):
    if (not isinstance(receipt, dict) or receipt.get('manifest_sha256') != mid
            or receipt.get('files') != file_summary(manifest)
            or receipt.get('verification') != 'reported_by_authenticated_requester'
            or receipt.get('anet.receipt_verified') is not None):
        reject('recipient_receipt_scope')


def accept_terminal_descriptor(value, observation, local_aid):
    """Requester integration must pass actual ANet metadata and message fields.

    Not callable with mere portal discovery or message text. The trusted adapter
    pins this descriptor to the provider message under the existing verified
    task receipt. This is a caller contract, not a new signature implementation.
    """
    d = validate_descriptor(value)
    if (d['requester'] != local_aid or observation.get('task_id') != d['task_id']
            or observation.get('peer') != d['provider'] or observation.get('message_role') != 'provider'
            or observation.get('state') != 'completed' or not observation.get('message_id')
            or observation.get('receipt_verified') != 'verified'
            or observation.get('external_delivery') != d):
        reject('descriptor_not_bound_to_verified_terminal')
    return d


def receive_terminal_descriptor(value, observation, session, destination, make_directory=None):
    """Receive exact descriptor after summary delivery; no callback/model call.

    Caller persists the descriptor and this independent stage, and retries only
    this stage. The reviewed receiver owns/resumes destination, checks all bytes,
    and reconciles an uncertain received ACK by querying this same delivery.
    """
    d = accept_terminal_descriptor(value, observation, session.aid)
    if session.role != 'requester': reject('recipient_role')
    remote = session.json({'op': 'status', 'delivery_id': d['manifest_sha256']})
    manifest = remote.get('manifest')
    if descriptor(manifest) != d: reject('recipient_manifest_pin')
    if not remote.get('receipt'):
        return {'state': 'waiting_storage', 'execution_state': 'completed', 'files_verified': False}
    # Validate full storage receipt using the same rules as sender.
    receipt = remote['receipt']
    expected = {'schema': 'delivery-repair/1', 'task_id': d['task_id'], 'manifest_sha256': d['manifest_sha256'],
        'files': file_summary(manifest), 'server_files_verified': True, 'recipient_files_verified': False,
        'channel': 'portal_https_storage', 'signature_verification': 'not_anet_receipt', 'anet.receipt_verified': None}
    if not isinstance(receipt, dict) or any(receipt.get(k) != v for k, v in expected.items()): reject('recipient_storage_receipt')
    try:
        received = receive_from_portal(session, d['manifest_sha256'], destination, make_directory=make_directory)
        validate_recipient_receipt(received, d['manifest_sha256'], manifest)
        return {'state': 'local_files_verified_and_reported', 'execution_state': 'completed',
            'files_verified': True, 'files': file_summary(manifest), 'directory': str(destination),
            'receipt': received, 'anet.receipt_verified': None, 'requester_acceptance': 'not_verified',
            'source_session_callback': 'not_attempted', 'execution_modified': False}
    except UnknownOutcome:
        return {'state': 'recipient_confirmation_unconfirmed', 'execution_state': 'completed',
            'files_verified': False, 'same_destination_only': True, 'execution_modified': False}


class RecipientJob:
    """Durable independent requester file stage, recoverable without a model turn.

    The integration chooses a NEW inbox directory and NEW download directory
    outside .anet. Stored observation is transport evidence, never credentials.
    """
    @classmethod
    def create(cls, directory, value, observation, local_aid, destination, make_directory=None):
        d = accept_terminal_descriptor(value, observation, local_aid)
        observation = {k: observation.get(k) for k in ('task_id','state','peer','message_role',
            'message_id','receipt_verified','effect_status','external_delivery')}
        if len(canonical(observation)) > 4096: reject('terminal_observation_too_large')
        root = plain_path(directory).resolve(); dest = plain_path(destination).resolve()
        if dest.is_relative_to(root) or root.is_relative_to(dest): reject('independent_inbox_and_download_required')
        record = {'schema': SCHEMA, 'descriptor': d, 'observation': observation,
            'local_aid': local_aid, 'destination': str(dest)}
        if root.exists():
            old = cls(root)
            if old.record != record: reject('recipient_job_conflict')
            return old
        if not root.parent.is_dir() or not dest.parent.is_dir(): reject('recipient_parent_missing')
        if make_directory is None: root.mkdir(mode=0o700)
        else: make_directory(root)
        private.write_new(root / 'queue.lock',b'0')
        create_json(root / 'owner.json', {'schema': SCHEMA, 'root': identity(root, True), 'lock': identity(root / 'queue.lock')})
        create_json(root / 'record.json', record)
        create_json(root / 'state.json', {'schema': SCHEMA, 'record_sha256': digest(canonical(record)),
            'state': 'waiting_storage', 'execution_state': 'completed', 'files_verified': False,
            'confirmation_started':False, 'receipt_ever_observed':False,
            'source_session_callback': 'not_attempted', 'execution_modified': False})
        return cls(root)

    def __init__(self, directory):
        self.root = plain_path(directory).resolve(); owner = read(self.root / 'owner.json')
        if owner.get('schema') != SCHEMA: reject('recipient_job_owner')
        require_identity(self.root, owner.get('root'), True)
        require_identity(self.root / 'queue.lock', owner.get('lock'))
        if os.name == 'posix' and self.root.stat().st_mode & 0o077: reject('recipient_job_not_private')
        self.record = read(self.root / 'record.json'); self.pin = digest(canonical(self.record))
        if self.record.get('schema') != SCHEMA: reject('recipient_record_schema')
        accept_terminal_descriptor(self.record['descriptor'], self.record['observation'], self.record['local_aid'])
        self.state = self._read_state()

    def _read_state(self):
        value = read(self.root / 'state.json')
        if value.get('schema') != SCHEMA or value.get('record_sha256') != self.pin: reject('recipient_state_identity')
        return value

    def save(self):
        self._read_state()
        tmp = self.root / ('state.new-' + os.urandom(12).hex())
        create_json(tmp, self.state); os.replace(tmp, plain_path(self.root / 'state.json'))

    def advance(self, session, make_directory=None):
        with Lock(self.root / 'queue.lock'):
            self.state = self._read_state()
            if session.aid != self.record['local_aid'] or session.role != 'requester': reject('recipient_role')
            # Durable facts are independent of the latest display/error state.
            # A legacy stored receipt also forces read-only reconciliation; it
            # never authorizes a fresh POST after a transient 404 or lost status.
            if self.state.get('pending_received') or self.state.get('receipt') or self.state.get('state') == 'local_files_verified_and_reported':
                self.state['confirmation_started']=True
                if self.state.get('state') == 'local_files_verified_and_reported' and self.state.get('receipt'):
                    self.state['receipt_ever_observed']=True
                self.save()
            # Repeat validates current bytes + server receipt; never trusts stale
            # local success alone, and never starts another source-session turn.
            try:
                observed_receipt = None
                if self.state.get('confirmation_started') or self.state.get('receipt_ever_observed'):
                    remote = session.json({'op': 'status', 'delivery_id': self.record['descriptor']['manifest_sha256']})
                    observed_receipt = remote.get('recipient_receipt')
                    if remote.get('recipient_state') != 'reported_verified' or not observed_receipt:
                        self.state.update(state='recipient_confirmation_unconfirmed', files_verified=False)
                        self.save(); return self.state
                    if descriptor(remote.get('manifest')) != self.record['descriptor']:
                        reject('recipient_reconciliation_manifest')
                    validate_recipient_receipt(observed_receipt, self.record['descriptor']['manifest_sha256'], remote['manifest'])
                    self.state['receipt_ever_observed']=True
                job = self
                class JournaledSession:
                    aid, role = session.aid, session.role
                    def request(self, *args, **kwargs): return session.request(*args, **kwargs)
                    def json(self, value):
                        if value.get('op') == 'received':
                            if observed_receipt is not None:
                                if value.get('files') != observed_receipt.get('files'):
                                    reject('recipient_reconciliation_files')
                                return observed_receipt  # Reconcile, never resend uncertain ACK.
                            job.state.update(pending_received=True, confirmation_started=True, state='recipient_confirmation_pending')
                            job.save()  # Crash before/after send remains query-only next tick.
                        return session.json(value)
                result = receive_terminal_descriptor(self.record['descriptor'], self.record['observation'],
                    JournaledSession(), self.record['destination'], make_directory=make_directory)
                if result.get('state') == 'local_files_verified_and_reported':
                    self.state.update(pending_received=False, confirmation_started=True, receipt_ever_observed=True)
            except MutationNotApplied as error:
                if error.operation == 'received' and error.mid == self.record['descriptor']['manifest_sha256'] and error.reason == 'authorization_denied':
                    self.state['pending_received']=False
                    if not self.state.get('receipt_ever_observed'): self.state['confirmation_started']=False
                    result={'state':'waiting_recipient_authorization','files_verified':False}
                else: result={'state':'blocked','files_verified':False,'reason':'not_applied_scope_mismatch'}
            except (NotFound, UnknownOutcome):
                result = {'state': 'waiting_storage_or_connection', 'files_verified': False}
            except (KnownFailure, ValueError, OSError, TypeError, KeyError):
                result = {'state': 'blocked', 'files_verified': False, 'reason': 'recipient_validation_or_access_failed'}
            self.state.update(result, execution_modified=False, source_session_callback='not_attempted')
            self.save()
            return self.state
