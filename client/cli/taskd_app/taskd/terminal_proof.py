"""Receipt-bound terminal text, never a global verdict attached to history.

One shared verifier for requester results and provider reconstructed candidates.
No identity is created. Providers use the existing CLI's public self-daemon
lookup; requesters use only the public KEL in their authenticated task view.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import uuid

MAX_TRANSCRIPT = 16 * 1024 * 1024
MAX_PUBLIC_EVIDENCE = 1024 * 1024

class ProofUnavailable(ValueError):
    pass

def fail(reason):
    raise ProofUnavailable(reason)

def strict_json(text):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value: fail('duplicate_json_key')
            value[key] = item
        return value
    return json.loads(text, object_pairs_hook=pairs,
                      parse_constant=lambda _: fail('non_finite_json'))

def raw_task(full):
    value = full.get('raw', full)
    if not isinstance(value, dict): fail('task_shape')
    return value

def task_state(task):
    return str(task.get('status', {}).get('state', '')).lower().removeprefix('task_state_')

def terminal_envelope(body):
    for line in reversed(body.splitlines()):
        if line.startswith('TASKD {'):
            value = strict_json(line[6:])
            if not isinstance(value, dict) or value.get('protocol') != 'taskd/1': fail('terminal_envelope_protocol')
            if value.get('state') != 'completed': fail('terminal_envelope_not_completed')
            return value
    fail('terminal_envelope_missing')

def go_json(value):
    """ANet transcript struct-order JSON with encoding/json HTML escaping."""
    return (json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
            .replace('&', '\\u0026').replace('<', '\\u003c').replace('>', '\\u003e')
            .replace('\u2028', '\\u2028').replace('\u2029', '\\u2029'))

def require_public_kel(data):
    # MarshalKEL is a CBOR array of signed PUBLIC events. The controller backup
    # is a map containing secrets and MUST NOT be handed to the verifier.
    if not isinstance(data, bytes) or not data or data[0] >> 5 != 4:
        fail('public_kel_export_array_required')
    return data

def receipt_claims(encoded):
    """Read PUBLIC v2 routing claims, not a signature verifier.

    Strict finite CBOR subset from ANetCore v0.16.2 evidence/evidence.go:
    ReceiptWire key 1 holds ReceiptBody bytes; body 4/5/6 are party/CIDs.
    This check prevents a wrong-party receipt causing any peer KEL lookup.
    """
    if not isinstance(encoded, str) or not 1 <= len(encoded) <= MAX_PUBLIC_EVIDENCE: fail('public_evidence_size')
    try: raw = base64.b64decode(encoded, validate=True)
    except ValueError: fail('public_evidence_encoding')
    def parse(data):
        pos = 0
        def item(depth=0):
            nonlocal pos
            if depth > 8 or pos >= len(data): fail('receipt_cbor_shape')
            header = data[pos]; pos += 1
            major, ai = header >> 5, header & 31
            if ai < 24: n = ai
            elif ai in (24,25,26,27):
                size = 1 << (ai-24)
                if pos + size > len(data): fail('receipt_cbor_shape')
                n = int.from_bytes(data[pos:pos+size], 'big'); pos += size
                if n < (24 if size == 1 else 1 << (8*(size//2))): fail('receipt_cbor_noncanonical')
            else: fail('receipt_cbor_indefinite_or_simple')
            if major == 0: return n
            if major == 1: return -1-n
            if major in (2,3):
                if n > MAX_PUBLIC_EVIDENCE or pos+n > len(data): fail('receipt_cbor_size')
                value = data[pos:pos+n]; pos += n
                return value if major == 2 else value.decode('utf-8', errors='strict')
            if major in (4,5) and n <= 64:
                if major == 4: return [item(depth+1) for _ in range(n)]
                value = {}
                for _ in range(n):
                    key = item(depth+1)
                    if type(key) is not int or key in value: fail('receipt_cbor_map_key')
                    value[key] = item(depth+1)
                return value
            fail('receipt_cbor_type')
        value = item()
        if pos != len(data): fail('receipt_cbor_trailing')
        return value
    try:
        wire = parse(raw)
        if not isinstance(wire, dict) or set(wire) != {1,2} or not isinstance(wire[1], bytes): fail('receipt_wire_shape')
        body = parse(wire[1])
        if not isinstance(body, dict) or set(body) != set(range(8)) or body[0] != 'anet.receipt/2': fail('receipt_body_version')
        if any(not isinstance(body[k], str) or not re.fullmatch('[a-z2-7]+', body[k]) for k in (4,5,6)):
            fail('receipt_claim_shape')
        return {'provider_aid': body[4], 'request_cid': body[5], 'result_cid': body[6]}
    except (UnicodeError, TypeError, KeyError): fail('receipt_cbor_shape')

def reconstruct_record(full, thread, public_row, *, role, public_kel=None):
    """A candidate only: its bytes MUST subsequently pass OfflineVerifier.

    public_row contains just schema-checked plaintext task metadata/nonce, never
    result ciphertext, DEK or private key. The caller owns the read-only getter.
    """
    if public_kel is not None: require_public_kel(public_kel)
    task = raw_task(full); meta = task.get('metadata', {}); receipt = meta.get('anet.receipt', {})
    thread = thread.get('thread', thread)
    tid = task.get('id')
    row_role = {'provider': 'inbound', 'requester': 'outbound'}.get(role)
    if (row_role is None or meta.get('anet.role') != role or public_row.get('id') != tid
            or thread.get('interaction_id') != tid or thread.get('role') != row_role
            or public_row.get('role') != row_role or public_row.get('state') != 'completed'
            or thread.get('peer') != meta.get('anet.peer_aid')
            or public_row.get('peer_aid') != meta.get('anet.peer_aid')):
        fail('provider_reconstruction_identity')
    for field in ('request_cid', 'result_cid', 'receipt_verified'):
        if public_row.get(field) != meta.get('anet.' + field): fail('provider_reconstruction_metadata')
    nonce = public_row.get('task_nonce')
    if not isinstance(nonce, str) or not re.fullmatch('[A-Za-z0-9_-]{22}', nonce): fail('task_nonce_unavailable')
    if len(base64.urlsafe_b64decode(nonce + '==')) != 16: fail('task_nonce_invalid')
    messages = []
    for message in thread.get('messages', []):
        md = message.get('metadata') or {}
        if not isinstance(md, dict): fail('thread_metadata_shape')
        if message.get('kind') != 'text' or 'anet.state' in md or any(k.startswith('x402.') for k in md):
            continue
        body = message.get('body') or ''
        if not isinstance(body, str): fail('thread_body_shape')
        atts = []
        for a in message.get('attachments') or []:
            if not isinstance(a, dict) or not all(k in a for k in ('name', 'mime', 'size', 'cid')): fail('thread_attachment_shape')
            if type(a['size']) is not int or a['size'] < 0: fail('thread_attachment_size')
            atts.append({k: a[k] for k in ('name', 'mime', 'size', 'cid')})
        if not body and not atts: continue
        side = message.get('from')
        if side not in ('me', 'them'): fail('thread_speaker_unknown')
        speaker = role if side == 'me' else ('requester' if role == 'provider' else 'provider')
        item = {'from': speaker, 'body': body}
        if atts: item['attachments'] = atts
        messages.append(item)
    result = go_json({'v': 2, 'nonce': nonce, 'messages': messages})
    return {'interaction_id': tid, 'provider': receipt.get('provider_aid'),
            'state': 'completed', 'result': result,
            'request_cid': public_row['request_cid'], 'result_cid': public_row['result_cid'],
            'receipt': receipt.get('receipt'), 'receipt_cid': receipt.get('receipt_cid'),
            'receipt_verified': public_row['receipt_verified'],
            'provider_kel': base64.b64encode(public_kel).decode('ascii') if public_kel is not None else None,
            'source': 'supported_thread_plus_plaintext_nonce_candidate'}

def reconstruct_provider_record(full, thread, public_row, public_kel=None):
    return reconstruct_record(full, thread, public_row, role='provider', public_kel=public_kel)

class OfflineVerifier:
    """The existing ANet binary verifies signatures and exact result bytes offline."""
    def __init__(self, executable, private_root, verify_directory, *, binary_sha256=None):
        self.executable = str(Path(executable).resolve())
        self.root = Path(private_root).resolve()
        self.verify_directory = verify_directory
        self.binary_sha256 = binary_sha256 or hashlib.sha256(Path(self.executable).read_bytes()).hexdigest()

    def _run(self, result_bytes, receipt, kel=None, env=None):
        from .terminal_source import bounded_cli
        path = self.root / ('verify-' + uuid.uuid4().hex + '.transcript')
        from .private_artifact_dir import open_new
        with open_new(path) as stream:
            stream.write(result_bytes); stream.flush(); os.fsync(stream.fileno())
        stat = path.stat(); identity = (stat.st_dev,stat.st_ino)
        try:
            self.verify_directory(path)
            args=['verify','--receipt',receipt]
            if kel is not None: args.extend(['--kel',kel])
            args.extend(['--result',str(path)])
            try: return bounded_cli(self.executable,args,env,limit=32768).decode('utf-8',errors='strict')
            except ProofUnavailable as error:
                if str(error)=='proof_cli_rejected':fail('offline_signature_or_content_rejected')
                raise
        finally:
            # Only the exact temporary created above can be removed. No glob,
            # old evidence deletion, permission changes or mismatching-file retry.
            self.verify_directory(self.root,root=True)
            current=path.stat()
            if (current.st_dev,current.st_ino)==identity and hashlib.sha256(path.read_bytes()).digest()==hashlib.sha256(result_bytes).digest():
                path.unlink()

    def __call__(self, result_bytes, receipt, kel):
        self.verify_directory(self.root, root=True)
        if hashlib.sha256(Path(self.executable).read_bytes()).hexdigest() != self.binary_sha256: fail('anet_binary_changed')
        for value in (receipt, kel):
            if not isinstance(value, str) or not 1 <= len(value) <= MAX_PUBLIC_EVIDENCE: fail('public_evidence_size')
            try: base64.b64decode(value, validate=True)
            except ValueError: fail('public_evidence_encoding')
        require_public_kel(base64.b64decode(kel, validate=True))
        if not 0 < len(result_bytes) <= MAX_TRANSCRIPT: fail('transcript_size')
        # --kel always supplied: this CLI branch never resolves a daemon/hub.
        text = self._run(result_bytes,receipt,kel)
        fields = {}
        for key, pattern in (
            ('provider_aid', r'signature verifies under ([a-z2-7]+)'),
            ('request_cid', r'(?m)^\s*request cid\s+([a-z2-7]+)\s*$'),
            ('result_cid', r'(?m)^\s*result cid\s+([a-z2-7]+)\s*$'),
            ('receipt_cid', r'(?m)^\s*receipt cid\s+([a-z2-7]+)\s*$')):
            found = re.findall(pattern, text)
            if len(found) != 1: fail('offline_verifier_output_contract')
            fields[key] = found[0]
        fields.update(source='installed_anet_offline_signature_and_exact_result',
                      verifier_sha256=self.binary_sha256,
                      transcript_sha256=hashlib.sha256(result_bytes).hexdigest())
        return fields

class LocalSelfVerifier(OfflineVerifier):
    """Supported local CLI only; never reads KEL/token files or enables hub fallback.

    The decoded receipt identity is cross-checked against both the caller's local
    identity and a fresh normal status response before invoking verify. The
    result must explicitly report daemon (self), not a peer/hub lookup.
    """
    def __init__(self, executable, private_root, verify_directory, *, local_aid,
                 data_dir, binary_sha256=None):
        super().__init__(executable, private_root, verify_directory, binary_sha256=binary_sha256)
        self.local_aid = local_aid
        self.env = dict(os.environ, ANET_DATA_DIR=str(data_dir))

    def __call__(self, result_bytes, receipt, kel):
        if kel is not None: fail('self_verifier_does_not_accept_kel_input')
        if receipt_claims(receipt)['provider_aid'] != self.local_aid: fail('local_self_receipt_provider_mismatch')
        self.verify_directory(self.root, root=True)
        if hashlib.sha256(Path(self.executable).read_bytes()).hexdigest() != self.binary_sha256: fail('anet_binary_changed')
        if not isinstance(receipt, str) or not 1 <= len(receipt) <= MAX_PUBLIC_EVIDENCE: fail('public_evidence_size')
        try: base64.b64decode(receipt, validate=True)
        except ValueError: fail('public_evidence_encoding')
        if not 0 < len(result_bytes) <= MAX_TRANSCRIPT: fail('transcript_size')
        from .terminal_source import bounded_cli
        try:
            status = bounded_cli(self.executable, ['status','--json'], self.env, limit=524288)
            if strict_json(status.decode('utf-8')).get('aid') != self.local_aid:
                fail('local_daemon_identity_mismatch')
        except (OSError, ValueError, UnicodeError, subprocess.TimeoutExpired): fail('local_daemon_status_unavailable')
        text = self._run(result_bytes,receipt,env=self.env)
        fetched = re.findall(r"key history for ([a-z2-7]+) fetched through this node's daemon \(self\)", text)
        if fetched != [self.local_aid]: fail('local_self_key_source_not_confirmed')
        fields = {}
        for key, pattern in (
            ('provider_aid', r'signature verifies under ([a-z2-7]+)'),
            ('request_cid', r'(?m)^\s*request cid\s+([a-z2-7]+)\s*$'),
            ('result_cid', r'(?m)^\s*result cid\s+([a-z2-7]+)\s*$'),
            ('receipt_cid', r'(?m)^\s*receipt cid\s+([a-z2-7]+)\s*$')):
            found = re.findall(pattern, text)
            if len(found) != 1: fail('offline_verifier_output_contract')
            fields[key] = found[0]
        if fields['provider_aid'] != self.local_aid: fail('local_self_provider_mismatch')
        fields.update(source='installed_anet_public_self_daemon_signature_and_exact_result',
                      verifier_sha256=self.binary_sha256,
                      transcript_sha256=hashlib.sha256(result_bytes).hexdigest())
        return fields

def verify_terminal_result(full, record, *, local_aid, role, verifier, require_descriptor=True):
    """Only return terminal body/descriptor actually inside verified result bytes."""
    task = raw_task(full); meta = task.get('metadata', {}); rc = meta.get('anet.receipt') or {}
    tid = task.get('id'); peer = meta.get('anet.peer_aid')
    if not isinstance(tid, str) or not re.fullmatch('ix_[0-9a-f]{32}', tid): fail('task_id')
    if role not in ('provider', 'requester') or meta.get('anet.role') != role: fail('task_role')
    provider = local_aid if role == 'provider' else peer
    requester = peer if role == 'provider' else local_aid
    if not provider or not requester or provider == requester: fail('task_participants')
    if task_state(task) != 'completed' or record.get('state') != 'completed': fail('task_not_completed')
    if record.get('interaction_id') != tid or record.get('provider') != provider: fail('result_task_or_provider')
    if any(v != 'verified' for v in (meta.get('anet.receipt_verified'), rc.get('verified'), record.get('receipt_verified'))): fail('receipt_not_verified')
    for field in ('request_cid', 'result_cid'):
        if not rc.get(field) or rc[field] != meta.get('anet.' + field) or rc[field] != record.get(field): fail('receipt_cid_binding')
    if rc.get('provider_aid') != provider or rc.get('receipt') != record.get('receipt'): fail('receipt_identity_binding')
    if isinstance(verifier, LocalSelfVerifier) and (role != 'provider' or verifier.local_aid != provider):
        fail('local_self_verifier_role_or_identity')
    if rc.get('receipt_cid') != record.get('receipt_cid') or not rc.get('receipt_cid'): fail('receipt_identity_binding')
    claims = receipt_claims(record['receipt'])
    if claims != {'provider_aid': provider, 'request_cid': record['request_cid'], 'result_cid': record['result_cid']}:
        fail('receipt_encoded_claim_binding')
    if role == 'requester' and rc.get('provider_kel') != record.get('provider_kel'): fail('receipt_kel_binding')
    result = record.get('result')
    if not isinstance(result, str): fail('exact_result_missing')
    result_bytes = result.encode('utf-8', errors='strict')
    if not 0 < len(result_bytes) <= MAX_TRANSCRIPT: fail('transcript_size')
    checked = verifier(result_bytes, record['receipt'], record.get('provider_kel'))
    for field, expected in (('provider_aid', provider), ('request_cid', record['request_cid']),
                            ('result_cid', record['result_cid']), ('receipt_cid', record['receipt_cid'])):
        if checked.get(field) != expected: fail('offline_verifier_identity_binding')
    transcript = strict_json(result)
    if isinstance(transcript, dict):
        if transcript.get('v') != 2 or not transcript.get('nonce'): fail('transcript_version')
        entries = transcript.get('messages')
    else: entries = transcript
    if not isinstance(entries, list): fail('transcript_messages')
    entries = [entry for entry in entries if isinstance(entry, dict) and entry.get('from') == 'provider']
    if not entries: fail('receipt_has_no_provider_result')
    body = entries[-1].get('body')
    if not isinstance(body, str): fail('terminal_body_missing')
    env = terminal_envelope(body)
    if env.get('task',tid)!=tid:fail('terminal_envelope_task_mismatch')
    descriptor = env.get('external_delivery')
    if require_descriptor and not isinstance(descriptor, dict): fail('receipt_covered_descriptor_missing')
    if descriptor is not None and (not isinstance(descriptor, dict) or descriptor.get('task_id') != tid
            or descriptor.get('provider') != provider or descriptor.get('requester') != requester):
        fail('descriptor_participants')
    # Artifact/history are used only to locate the exact already-verified bytes.
    # They can neither supply nor override the descriptor.
    matches = []
    for message in task.get('history') or []:
        texts = [p['text'] for p in message.get('parts') or [] if isinstance(p.get('text'), str)]
        if message.get('role') == 'ROLE_AGENT' and texts == [body] and message.get('messageId'):
            matches.append(message)
    if len(matches) != 1: fail('covered_message_id_unavailable_or_ambiguous')
    message = matches[0]
    # completeWithReply uses a distinct outer TypeResult MID. An inner text
    # delivery observation cannot establish ACK of that signed result envelope.
    message_delivery = message.get('metadata', {}).get('anet.delivery') if role == 'provider' else None
    return {'task_id': tid, 'provider_aid': provider, 'requester_aid': requester,
            'peer': peer, 'role': role, 'state': 'completed', 'message_role': 'provider',
            'message_id': message['messageId'], 'request_cid': record['request_cid'],
            'result_cid': record['result_cid'], 'receipt_cid': record['receipt_cid'],
            'receipt_sha256': hashlib.sha256(base64.b64decode(record['receipt'])).hexdigest(),
            'receipt_verified': 'verified', 'effect_status': meta.get('anet.effect_status'),
            'terminal_body': body, 'terminal_body_sha256': hashlib.sha256(body.encode('utf-8')).hexdigest(),
            'external_delivery': descriptor, 'envelope': env, 'verification': checked,
            'transport_delivery_observed': meta.get('anet.delivery'),
            'transport_delivery': None, 'message_delivery_observed': message_delivery,
            'terminal_ack_verified': False, 'terminal_ack_source': None,
            'signed_terminal_verified': True,
            'terminal_local_commit_verified': role == 'provider'}
