"""The plugins that ship with taskd, and how a node's config turns into objects. Adding an executor,
checker, probe, transport or requester client means adding it here; the core does not change."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

from .checkers.generic import GENERIC
from .checkers.eps_preflight import EpsPreflightChecker
from .checkers.windows_uia import WINDOWS
from .executors.codex import CodexExecutor
from .executors.command import CommandExecutor
from .executors.harness_provider import (WorkBuddyLocalExecutor, TaskdHarnessExecutor,
    build_workbuddy_local, build_sdk_provider)
from .probes.windows_desktop import WindowsDesktopProbe
from .protocol import DEFAULT_LIMITS, PROTOCOL
from .requesters.codex import CodexRequester
from .resources import capacity
from .transports.anet import AnetTransport

EXECUTORS = {'codex': CodexExecutor, 'command': CommandExecutor,
             'workbuddy_local': WorkBuddyLocalExecutor, 'harness_sdk': TaskdHarnessExecutor}
CHECKERS = {c.name: c for c in GENERIC + WINDOWS + (EpsPreflightChecker,)}
PROBES = {'windows_desktop': WindowsDesktopProbe}
TRANSPORTS = {'anet': AnetTransport}
REQUESTERS = {'codex': CodexRequester}


def runs_here(plugin):
    return any(p == 'any' or sys.platform.startswith(p) for p in plugin.platforms)


def make_executor(cfg):
    if not cfg:
        return None
    kind = cfg.get('type')
    if kind == 'codex':
        return CodexExecutor(command=cfg.get('command'), sandbox=cfg.get('sandbox', 'workspace-write'),
                             model=cfg.get('model'), codex_home=cfg.get('codex_home'), config=cfg.get('config', ()),
                             native_goals=cfg.get('native_goals', True), native_endpoint=cfg.get('native_endpoint'),
                             native_token_file=cfg.get('native_token_file'))
    if kind == 'command':
        return CommandExecutor(cfg['command'], cfg.get('args', ()), cfg.get('env'))
    if kind == 'workbuddy_local':
        return build_workbuddy_local(cfg)
    if kind == 'harness_sdk':
        # This is the owner's node configuration, never a requester task field.
        harness_config = cfg.get('harness_config')
        if harness_config is not None:
            if not isinstance(harness_config,dict) or not isinstance(harness_config.get('harnesses',{}),dict):
                raise ValueError('invalid owner SDK harness configuration')
            claude = harness_config.get('harnesses',{}).get('claude_code',{})
            if not isinstance(claude,dict) or ('permission_mode' in claude and claude['permission_mode'] not in ('default','acceptEdits')):
                raise ValueError('owner Claude permission_mode must be absent, default or acceptEdits')
        return build_sdk_provider(cfg)
    raise ValueError('unknown executor type %r (known: %s)' % (kind, ', '.join(EXECUTORS)))


def make_checkers(names=None):
    out = {}
    for name, cls in CHECKERS.items():
        if names is not None and name not in names:
            continue
        c = cls()
        if runs_here(c):
            out[name] = c
    return out


def make_probes(names):
    out = []
    for n in names or []:
        if n not in PROBES:
            raise ValueError('unknown probe %r' % n)
        p = PROBES[n]()
        if runs_here(p):
            out.append(p)
    return out


def make_transport(cfg, log=None):
    t = cfg.get('transport', {'type': 'anet'})
    if t.get('type', 'anet') != 'anet':
        raise ValueError('unknown transport %r' % t.get('type'))
    return AnetTransport(anet=t.get('binary'), data_dir=t.get('data_dir'), log=log)


def make_requesters(cfg):
    out = {}
    for c in (cfg.get('requester') or {}).get('clients', ['codex']):
        if c == 'codex':
            r = cfg['requester']
            out['codex'] = CodexRequester(command=r.get('codex_command'), codex_home=r.get('codex_home'),
                                         endpoint=r.get('codex_endpoint'), token_file=r.get('codex_token_file'))
    return out


def make_owned_sources(cfg, root):
    """Explicit opt-in only; no host discovery or source adoption."""
    settings=(cfg.get('requester') or {}).get('owned_sources') or {}
    if settings.get('enabled') is not True:
        return None
    if set(settings)!={'enabled','release_policy'}:
        raise ValueError('exact owned source release policy required')
    from owned_source_product.registry import OwnedSourceRegistry
    return OwnedSourceRegistry(Path(root)/'owned-sources',release_policy=settings['release_policy'])


def make_harness_sources(cfg,root):
    """Fixed installed package and explicit release pin; never discover source hosts."""
    settings=(cfg.get('requester') or {}).get('harness_sources') or {}
    if settings.get('enabled') is not True:return None
    if set(settings)!={'enabled','release_policy'}:raise ValueError('exact harness source release policy required')
    from .harness_sources import HarnessSourceRegistry
    return HarnessSourceRegistry(Path(root)/'harness-sources',release_policy=settings['release_policy'])


def capabilities(cfg, executor, checkers, reviewer):
    limits = dict(DEFAULT_LIMITS, **cfg.get('limits', {}))
    durable = getattr(executor, 'durable_step_required', False)
    quick = executor is not None and (not durable or getattr(executor, 'durable_quick_supported', False))
    return {
        'protocol': PROTOCOL, 'tier': 2,
        'native': executor.native_capability() if hasattr(executor, 'native_capability') else {
            'available': False, 'reason': 'adapter_has_no_verified_native_goals'},
        'routing': {'submit_default': 'native_if_available', 'planned_opt_in': True,
                    'run': 'provider_planned_only' if getattr(executor,'durable_step_required',False) else 'single_execution',
                    'question': 'quick' if quick else 'unsupported_native_final', 'fallback_is_reported': True},
        'workflow': {'phase_milestones': 1, 'ordered_progress': 1, 'uia_batch': 1,
                     'mixed_desktop_work': 1, 'diagnostic_progress_review': 1,
                     'timing': 1, 'timing_scope': 'wall time; executor includes model/tools/waits'},
        'recovery': {'revise_plan': 1, 'phase_preflight': 1, 'structured_blockers': 1, 'check_updates': 1,
                     'plain_text_retry': 1, 'automatic_desktop_reschedule': 1,
                     'phase_desktop_requirement': 1, 'system_error_delivery': 1},
        'executors': [executor.name] if executor else [],
        'executor_caps': executor.describe()['caps'] if executor else {},
        'checkers': {n: c.version for n, c in sorted(checkers.items())},
        'platform': {'os': sys.platform, 'gui': bool(cfg.get('probes'))},
        'review': reviewer is not None,
        'continuity': {'fixed_step_cutoff': False, 'legacy_max_steps': 'advisory',
                       'cumulative_time_cutoff': False, 'operation_watchdogs_enforced': True, 'stall_guards_enforced': True},
        'limits': {'max_hours': limits['max_hours'], 'max_steps': limits['max_steps'],
                   'step_timeout_min': limits['step_timeout_min'], 'concurrency': capacity(cfg)},
        # quick: a plain message is answered with one executor call; long: a taskd/1 spec is planned, executed and checked.
        'modes': (['quick'] if quick and cfg.get('plain_text', 'quick') == 'quick' else []) + (['long'] if durable else ['direct', 'routine', 'long']),
        'quick': {'available': quick and cfg.get('plain_text', 'quick') == 'quick',
                  'durable_single_invocation': bool(durable and quick), 'planner_calls': 0,
                  'maximum_native_dispatches_per_request': 1 if durable and quick else None,
                  'native_terminal_required': bool(durable),
                  'reason': None if quick else 'native_terminal_unavailable'},
        'lightweight': {'action': 'provider_planned_only' if getattr(executor,'durable_step_required',False) else 'run', 'planner_calls': 0, 'direct_model_calls': 1,
                        'routine_model_calls': 0, 'routine_exception_model_calls': 1,
                        'deterministic_acceptance_required': True},
        'queries': ['capabilities', 'status', 'logs'],
        'scheduling': {'scope': 'phase', 'shared_quick_long_slots': True,
                       'background_unlimited': capacity(cfg) is None,
                       'desktop_concurrency': 1, 'isolate_tasks': cfg.get('isolate_tasks', True),
                       'resources': ['background', 'desktop'], 'desktop_access': 'cooperative'},
        'accept_from': 'admitted' if cfg.get('accept_peers', 'admitted') == 'admitted' else 'listed',
    }


def fingerprint(cfg, app_dir):
    """Changing the executor or reviewer settings, or the program, invalidates a self-test."""
    h = hashlib.sha256()
    h.update(json.dumps({k: cfg.get(k) for k in ('executor', 'reviewer', 'work_dir', 'probes')},
                        sort_keys=True).encode())
    for p in sorted(Path(app_dir).rglob('*.py')):
        h.update(p.read_bytes())
    return h.hexdigest()
