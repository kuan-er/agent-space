"""Deterministic EPS integration checks on explicitly supplied, measured artifacts.

File evidence is not live GUI proof. A native parser receipt must identify the
exact program bytes; static XML checks alone never assert runtime compilation.
"""
from datetime import datetime, timezone
import hashlib
import json
import ntpath
import re
import time
import xml.etree.ElementTree as ET

from .base import Checker, CheckResult, only


def load(ctx, path, json_data=False):
    p = ctx.resolve(path)
    if p.stat().st_size > 16 * 1024 * 1024:
        raise ValueError('preflight artifact is too large')
    raw = p.read_bytes()
    return (json.loads(raw.decode('utf-8-sig')) if json_data else raw), {
        'path': str(p), 'size': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}


def canonical(path):
    return ntpath.normcase(ntpath.normpath(path))


class EpsPreflightChecker(Checker):
    name = 'eps_preflight'
    layer = 'file'
    doc = ('eps_preflight: config, dll, dll_sha256, project_folder, program, snapshot, parser_result, '
           'max_age_seconds=120. Checks actual file hashes, project binding, fresh snapshot, '
           'XPL external declarations and a native parser receipt for the exact program hash. '
           'Missing or stale native evidence cannot pass. Does not start EPS or simulation.')

    def validate(self, params):
        p = only(params, ('config', 'dll', 'dll_sha256', 'project_folder', 'program', 'snapshot',
                          'parser_result', 'max_age_seconds'),
                 ('config', 'dll', 'dll_sha256', 'project_folder', 'program', 'snapshot', 'parser_result'))
        for k in ('config', 'dll', 'project_folder', 'program', 'snapshot', 'parser_result'):
            if not isinstance(p[k], str) or not p[k].strip():
                raise ValueError(k + ' is required')
        if not isinstance(p['dll_sha256'], str) or not re.fullmatch('[0-9a-f]{64}', p['dll_sha256']):
            raise ValueError('dll_sha256 must pin the deployed module')
        age = p.setdefault('max_age_seconds', 120)
        if type(age) not in (int, float) or not 1 <= age <= 600:
            raise ValueError('max_age_seconds must be 1..600')
        return p

    def check(self, params, ctx, baseline=None):
        issues, receipts = [], []
        def issue(code, field, expected, actual):
            issues.append(dict(code=code, field=field, expected=expected, actual=actual))
        def read(name, structured=False):
            try:
                data, receipt = load(ctx, params[name], structured)
                receipts.append(receipt)
                return data, receipt
            except (OSError, ValueError, TypeError) as e:
                issue('configuration_mismatch', name, 'readable artifact', str(e))
                return None, None
        config, _ = read('config', True)
        if isinstance(config, dict):
            actual = config.get('AllowedProjectFolder')
            if not isinstance(actual, str) or canonical(actual) != canonical(params['project_folder']):
                issue('configuration_mismatch', 'AllowedProjectFolder', params['project_folder'], actual)
        elif config is not None:
            issue('configuration_mismatch', 'config', 'JSON object', type(config).__name__)
        _, dll = read('dll')
        if dll and dll['sha256'] != params['dll_sha256']:
            issue('configuration_mismatch', 'module_sha256', params['dll_sha256'], dll['sha256'])
        raw, program = read('program')
        if raw is not None:
            try:
                tree = ET.fromstring(raw)
                if tree.tag != 'Xpl-source':
                    raise ValueError('expected Xpl-source root')
                declarations = {n.text for n in tree.findall('.//VarDeclarations/VarExtern/Var/Name')}
                used = set()
                for body in tree.findall('.//Body'):
                    used.update(re.findall(r'\bipaint_[A-Za-z0-9_]+\b', ' '.join(body.itertext())))
                missing = sorted(used - declarations)
                if missing:
                    issue('program_error', 'external_variables', 'declared ipaint variables', missing)
            except (ET.ParseError, ValueError) as e:
                issue('program_error', 'program_xml', 'parseable XPL', str(e))
        snapshot, _ = read('snapshot', True)
        parser, _ = read('parser_result', True)
        now = time.time()
        for name, data in [('snapshot', snapshot), ('parser_result', parser)]:
            if not isinstance(data, dict):
                if data is not None:
                    issue('configuration_mismatch', name, 'JSON object', type(data).__name__)
                continue
            try:
                stamp = datetime.fromisoformat(data['utc'].replace('Z', '+00:00'))
                if stamp.tzinfo is None:
                    raise ValueError('timezone missing')
                if not -2 <= now - stamp.timestamp() <= params['max_age_seconds']:
                    raise ValueError('stale or future observation')
            except (KeyError, ValueError, TypeError, AttributeError) as e:
                issue('configuration_mismatch', name + '.utc', 'fresh observation', str(e))
        if isinstance(snapshot, dict):
            folder = snapshot.get('project_folder')
            if not isinstance(folder, str) or canonical(folder) != canonical(params['project_folder']):
                issue('configuration_mismatch', 'active_project_folder', params['project_folder'], folder)
            if snapshot.get('host_is_ready') is not True or snapshot.get('read_errors') != {}:
                issue('configuration_mismatch', 'native_interface', 'ready with no read errors',
                      dict(ready=snapshot.get('host_is_ready'), errors=snapshot.get('read_errors')))
            if type(snapshot.get('eps_pid')) is not int or snapshot['eps_pid'] <= 0:
                issue('configuration_mismatch', 'eps_pid', 'positive process identity', snapshot.get('eps_pid'))
        if isinstance(parser, dict):
            if not program or parser.get('program_sha256') != program['sha256']:
                issue('program_error', 'parser_program_sha256', program and program['sha256'], parser.get('program_sha256'))
            if not isinstance(snapshot, dict) or parser.get('eps_pid') != snapshot.get('eps_pid'):
                issue('configuration_mismatch', 'parser_eps_pid', snapshot and snapshot.get('eps_pid'), parser.get('eps_pid'))
            if parser.get('source') != 'eps_native_parser':
                issue('program_error', 'parser_source', 'eps_native_parser', parser.get('source'))
            if parser.get('success') is not True or parser.get('errors') != []:
                issue('compile_failed', 'native_parser', 'success with empty errors', parser.get('errors'))
        observed = dict(code=issues[0]['code'] if issues else None, issues=issues, artifacts=receipts,
                        basis='measured files and native receipts; not live GUI or deposition verification',
                        native_deposition_verified=False)
        return CheckResult('fail' if issues else 'pass', observed, self.layer,
                           'Preflight failed; inspect the exact mismatches' if issues else 'Integration artifacts matched')
