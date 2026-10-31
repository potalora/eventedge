"""Bounded supplemental optional-enrichment observation; never repair authority.

Canonical operational reports and accepted decisions remain unchanged. Only a
trusted caller with the canonical runtime lease may publish this projection.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
import sqlite3

VERSION = 'eventedge-enrichment-monitor-v1'
MAX_BYTES = 64 * 1024
MAX_FAILURES = 256


def _validate_query_facts(original, acquisition):
    """A valid query receipt must also describe the accepted enrichment facts."""
    from ..data_sources.finra_bulk import population_digest
    from ..data_sources.fetch_errors import source_number
    from datetime import date
    values = original.get('short_interest', {})
    errors = original.get('errors', {}).get('short_interest', {})
    failure = 'FINRA receipt contradicts accepted enrichment facts'
    if type(values) is not dict or type(errors) is not dict:
        raise ValueError(failure)
    for value in values.values():
        if (type(value) is not dict or set(value) != {'short_interest', 'short_pct_of_float', 'days_to_cover', 'date'}
                or not source_number(value['short_interest'], minimum=0)
                or not source_number(value['days_to_cover'], minimum=0)
                or value['short_pct_of_float'] is not None and not source_number(value['short_pct_of_float'], minimum=0)):
            raise ValueError(failure)
        try:
            if type(value['date']) is not str or date.fromisoformat(value['date']).isoformat() != value['date']:
                raise ValueError
        except ValueError:
            raise ValueError(failure) from None
    population = set(values) | set(errors)
    if (set(values) & set(errors) or len(population) != acquisition['requested_count']
            or population_digest(population) != acquisition['population_sha256']):
        raise ValueError(failure)
    terminal = acquisition['attempts'][-1] if acquisition['attempts'] else None
    coverage = terminal.get('query_coverage') if terminal else None
    if coverage is None:
        if len(values) != acquisition['cached_count']:
            raise ValueError(failure)
        return
    accepted = set(coverage['accepted_symbols'])
    unavailable = set(coverage['missing_symbols']) | set(coverage['invalid_symbols'])
    if (not accepted <= set(values) or unavailable != set(errors)
            or len(set(values) - accepted) != acquisition['cached_count']
            or any(type(values[symbol]) is not dict
                or values[symbol].get('date') != coverage['required_settlement_date'] for symbol in accepted)):
        raise ValueError(failure)


def assess_accepted_enrichment(original, scope, *, capability_config=None, codec_limits=None):
    """Project fixed diagnostics only after native accepted-input validation."""
    from .research_resilience import project_enrichment
    from .multi_strategy_engine import _enrichment_failures
    from ..data_sources.finra_bulk import validated_acquisition
    projected, expected = project_enrichment(original, capability_config=capability_config, codec_limits=codec_limits)
    if expected != scope:
        raise ValueError('enrichment monitor scope mismatch')
    failures = _enrichment_failures(original)
    if len(failures) > MAX_FAILURES:
        raise ValueError('enrichment monitor failure bound')
    public = []
    for failure in failures:
        operation, symbol = failure['operation'], failure['symbol']
        # Error prose and URLs cannot establish a status. Preserve only an
        # explicitly recorded numeric status from this accepted observation.
        details = original.get('errors', {}).get(operation, {})
        if operation != 'factors':
            details = details.get(symbol, {})
        status = details.get('http_status') if isinstance(details, dict) else None
        status = status if type(status) is int and 100 <= status <= 599 else None
        public.append({'provider': 'finra' if operation == 'short_interest' else 'openbb',
            **failure, 'http_status': status})
    healthy = []
    for operation, payload in sorted(projected.items()):
        symbols = [None] if operation == 'factors' else sorted(payload)
        if operation != 'factors' and any(type(symbol) is not str
                or not re.fullmatch(r'[A-Za-z0-9.^=_-]{1,32}', symbol) for symbol in symbols):
            raise ValueError('invalid enrichment healthy symbol')
        healthy.extend({'provider': 'finra' if operation == 'short_interest' else 'openbb',
            'operation': operation, 'symbol': symbol} for symbol in symbols)
    if len(healthy) > MAX_FAILURES:
        raise ValueError('enrichment monitor healthy scope bound')
    result = {'healthy_scope': healthy, 'status': 'degraded' if scope['excluded'] or scope['hold_reasons'] else 'complete',
        'automatic_repair_supported': False, 'accepted_enrichment_sha256': scope['original_sha256'],
        'failures': public, 'hold_reasons': list(scope['hold_reasons']),
        'short_interest_acquisition': None}
    acquisition = original.get('short_interest_acquisition')
    if acquisition is not None:
        acquisition = validated_acquisition(acquisition)
        terminal = acquisition['attempts'][-1] if acquisition['attempts'] else None
        if result['status'] == 'complete' and ((terminal is not None and terminal['status'] == 'error')
                or terminal is None and acquisition['cached_count'] != acquisition['requested_count']):
            raise ValueError('FINRA receipt contradicts complete enrichment scope')
        if acquisition['schema_version'] == 4:
            # Query evidence describes exact symbols, not archive inventory.
            # Preserve the full bounded contract for independent native validation.
            _validate_query_facts(original, acquisition)
            result['short_interest_acquisition'] = acquisition
        else:
            attempts = []
            for attempt in acquisition['attempts']:
                item = {key: attempt.get(key) for key in ('status', 'reason_code', 'acquired_at')}
                coverage = attempt.get('archive_coverage')
                item['archive_coverage'] = ({key: coverage[key] for key in
                    ('required_settlement_date', 'current_complete', 'historical_complete')} if coverage is not None else None)
                probe = attempt.get('current_archive_probe')
                item['current_archive_probe'] = ({key: probe[key] for key in
                    ('kind', 'settlement_date', 'observed_at', 'http_status', 'outcome')} if probe is not None else None)
                attempts.append(item)
            result['short_interest_acquisition'] = {key: acquisition[key] for key in
                ('schema_version', 'requested_count', 'cached_count', 'population_sha256')}
            result['short_interest_acquisition']['attempts'] = attempts
    if len(_bytes(result)) > MAX_BYTES:
        raise ValueError('enrichment monitor size bound')
    return result


def _bytes(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False) + '\n').encode()


def _read_accepted_observation(repo, metadata, report):
    """Reuse canonical read-only proof validators, including all sixteen books."""
    from . import operational_report as reports
    from .generation_config import FROZEN_CONFIG_NAME, read_generation_config
    from .operating_contract import profile_contract
    state = repo / 'data/generations' / report['generation_id']
    config_file = state / FROZEN_CONFIG_NAME
    if (Path(metadata['state_dir']) != state or Path(metadata['config_file']) != config_file
            or config_file.is_symlink() or state.resolve() != state):
        raise ValueError('enrichment monitor configuration path mismatch')
    _, config, digest = read_generation_config(config_file, metadata['config_sha256'])
    if report.get('profile_sha256') != digest:
        raise ValueError('enrichment monitor profile mismatch')
    profile = profile_contract(config, profile_sha256=digest)
    diagnostics, retained, observation = [], {}, {}
    def diagnose(code, *_):
        diagnostics.append(code)
    sources = reports._sources(state, report['generation_id'], report['session'], report['generation_commit'],
        diagnose, retained_sources=retained, operating_profile=profile, frozen_config=config)
    from .operating_contract import source_support
    sources['capability_scope'], _ = source_support(config, retained['_completion_source_envelope']['payload'])
    if (diagnostics or sources != report['sources']):
        raise ValueError('enrichment monitor source mismatch')
    epoch, health = reports._metrics(state, report['generation_id'], report['session'], report['generation_commit'],
        diagnose, sources.get('declared_exclusions', {}))
    if epoch != report['epoch_id']:
        raise ValueError('enrichment monitor metric epoch mismatch')
    reports._completion_facts(state, profile, report, health,
        retained['_completion_source_envelope'], diagnose, enrichment_observation=observation)
    if diagnostics or not observation:
        raise ValueError('enrichment monitor accepted proof unavailable')
    return observation


def publish_enrichment_monitor(repo_root, report, paths, *, metadata, now=None):
    """Write only a separate sidecar, under the caller's canonical runtime lock.

    A missing or invalid accepted proof produces unavailable, never recovery.
    This function does not acquire sources, enqueue workers, or send messages.
    """
    from .decision_clock import latest_completed_session
    from .trading_calendar import session_close, next_session
    from datetime import date
    from .operational_report import render_operational_report
    from .source_repair import _Capture, _now, _decode, _COMMIT, _GENERATION
    from .source_repair_intervention import _publish_sidecar
    repo = Path(repo_root).absolute()
    generation, commit, session = (report[key] for key in ('generation_id', 'generation_commit', 'session'))
    if (not _GENERATION.fullmatch(generation) or not _COMMIT.fullmatch(commit)
            or metadata['gen_id'] != generation or metadata['git_commit'] != commit):
        raise ValueError('enrichment monitor generation mismatch')
    folder = repo / 'docs/reports'
    stem = f'{session}-{generation}-operational-report'
    if Path(paths['json']) != folder / (stem + '.json') or Path(paths['markdown']) != folder / (stem + '.md'):
        raise ValueError('enrichment monitor report path mismatch')
    capture = _Capture(folder)
    raw, markdown = capture.read(stem + '.json', 2 * 1024 * 1024), capture.read(stem + '.md', 2 * 1024 * 1024)
    if _decode(raw, 2 * 1024 * 1024) != report or markdown != render_operational_report(report).encode():
        raise ValueError('enrichment monitor report pair mismatch')
    result = {'version': VERSION, 'generation_id': generation, 'generation_commit': commit,
        'session': session, 'valid_from': session_close(date.fromisoformat(session)).isoformat(),
        'valid_until': session_close(next_session(date.fromisoformat(session))).isoformat(),
        'report_sha256': hashlib.sha256(raw + b'\0' + markdown).hexdigest(),
        'configuration': report.get('sources', {}).get('configuration'),
        'workflow_complete': report.get('completion', {}).get('workflow_complete'),
        'continuity_eligible': report.get('completion', {}).get('continuity_eligible'),
        'status': 'unavailable', 'automatic_repair_supported': False,
        'accepted_enrichment_sha256': None, 'failures': [], 'healthy_scope': [], 'hold_reasons': [],
        'short_interest_acquisition': None, 'diagnostics': []}
    if session != latest_completed_session(_now(now)).isoformat():
        result.update(status='stale', diagnostics=['stale_report'])
    else:
        try:
            result.update(_read_accepted_observation(repo, metadata, report))
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, AttributeError):
            result['diagnostics'] = ['accepted_enrichment_unavailable']
    capture.verify()
    data = _bytes(result)
    if len(data) > MAX_BYTES:
        raise ValueError('enrichment monitor size bound')
    destination = folder / 'source-repair' / f'{session}-{generation}-enrichment-monitor.json'
    _publish_sidecar(destination, data)
    return {'path': str(destination), 'status': result['status'], 'diagnostics': result['diagnostics']}
