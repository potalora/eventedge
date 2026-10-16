"""Bounded observational source incidents; never execution or repair authority."""
from __future__ import annotations

from datetime import date, datetime
import hashlib
import json
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from .trading_calendar import is_session, previous_session

POLICY = 'source_incidents_v1'
MAX_REPORT_BYTES = 2 * 1024 * 1024
_PROVIDERS = frozenset({'alpaca', 'edgar', 'fred', 'finnhub', 'congress', 'regulations',
    'courtlistener', 'noaa', 'usda', 'drought_monitor', 'usaspending', 'cftc',
    'yfinance', 'openbb', 'bounded_documents', 'equity_universe'})
_REASONS = frozenset({'timeout', 'transport_error', 'http_error', 'invalid_response',
    'provider_error', 'batch_failure', 'missing_source_evidence'})
_FIELDS = {'provider', 'status', 'observation', 'reason_code', 'http_status',
    'first_failed_session', 'last_failed_session', 'consecutive_failed_sessions'}
_SAFE_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,191}')


def _day(value):
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError('invalid incident session')
    return parsed


def _identity(report):
    if not all(isinstance(report.get(key), str) and _SAFE_ID.fullmatch(report[key])
               for key in ('generation_id', 'generation_commit')):
        raise ValueError('invalid incident report identity')
    session = _day(report['session'])
    if not is_session(session):
        raise ValueError('invalid incident report session')
    return session


def _history(report, previous):
    session = _identity(report)
    if _identity(previous) != previous_session(session):
        raise ValueError('incident history is not the prior session')
    if any(report[key] != previous[key] for key in ('generation_id', 'generation_commit')):
        raise ValueError('incident history identity differs')
    configuration = report.get('sources', {}).get('configuration')
    if configuration is not None and configuration != _history_configuration(previous):
        raise ValueError('incident configuration differs')
    summary = previous['source_incidents']
    rows = summary['open_incidents']
    if summary['policy'] != POLICY or not isinstance(rows, list) or len(rows) > len(_PROVIDERS):
        raise ValueError('invalid incident history contract')
    result = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != _FIELDS:
            raise ValueError('invalid incident fields')
        provider, count = row['provider'], row['consecutive_failed_sessions']
        undated = (row['first_failed_session'] is None and row['last_failed_session'] is None
                   and row['observation'] == 'unknown' and row['reason_code'] == 'missing_source_evidence')
        if (provider not in _PROVIDERS or provider in result
                or row['status'] not in {'degraded', 'repair_required'}
                or row['observation'] not in {'failed', 'unknown'}
                or row['reason_code'] not in _REASONS or type(count) is not int or not 0 <= count <= 3
                or (row['observation'] == 'unknown' and count != 0)
                or (row['observation'] == 'failed' and count == 0)
                or not undated and not (_day(row['first_failed_session']) <= _day(row['last_failed_session']) <= _day(previous['session']))
                or row['http_status'] is not None and
                   (type(row['http_status']) is not int or not 100 <= row['http_status'] <= 599)):
            raise ValueError('invalid incident history row')
        result[provider] = dict(row)
    return result


def _history_configuration(previous):
    retained = previous['source_incidents'].get('configuration')
    observed = previous.get('sources', {}).get('configuration')
    if (retained is not None and (not isinstance(retained, str) or not _SAFE_ID.fullmatch(retained))
            or observed is not None and retained != observed):
        raise ValueError('invalid retained incident configuration')
    return retained


def _fresh(report):
    try:
        acquired = datetime.fromisoformat(report['sources']['acquired_at'])
        return (acquired.utcoffset() is not None and acquired.astimezone(
            ZoneInfo('America/New_York')).date() == _day(report['session']))
    except (KeyError, TypeError, ValueError, AttributeError):
        return False


def assess_source_incidents(report, previous_report=None, history_status='missing') -> dict:
    """Project current source evidence and exactly one prior-session summary."""
    _identity(report)
    prior = {}
    prior_configuration = None
    history_status = history_status if history_status in {'missing', 'invalid', 'available'} else 'invalid'
    if previous_report is not None:
        try:
            prior = _history(report, previous_report)
            prior_configuration = _history_configuration(previous_report)
            history_status = 'available'
        except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
            prior = {}
            prior_configuration = None
            history_status = 'invalid'
    elif history_status == 'available':
        history_status = 'invalid'
    sources = report.get('sources', {})
    configuration = sources.get('configuration')
    unavailable = not isinstance(configuration, str) or not _SAFE_ID.fullmatch(configuration)
    configuration = prior_configuration if unavailable else configuration
    if unavailable:
        history_status = 'invalid'
    observations = sources.get('observations', {})
    missing = {provider for provider, observation in observations.items()
               if provider in _PROVIDERS and observation == 'missing'}
    failures = {}
    for item in sources.get('unresolved', []):
        if not isinstance(item, dict) or item.get('source', item.get('provider')) not in _PROVIDERS:
            continue
        provider = item.get('source', item.get('provider'))
        if item.get('reason_code') == 'missing_source_evidence':
            missing.add(provider)
            continue
        reason = item.get('reason_code') if item.get('reason_code') in _REASONS else 'provider_error'
        status = item.get('http_status')
        status = status if type(status) is int and 100 <= status <= 599 else None
        terminal = reason == 'invalid_response' or status in {400, 401, 403, 404, 410}
        candidate = (terminal, reason, status)
        if provider not in failures or (terminal, reason, status or 0) > (
                failures[provider][0], failures[provider][1], failures[provider][2] or 0):
            failures[provider] = candidate
    for health in report.get('source_health_failures', []):
        for provider in health.get('sources', []):
            if provider in _PROVIDERS and provider not in missing:
                failures.setdefault(provider, (False, 'provider_error', None))
    for provider, observation in observations.items():
        if provider in _PROVIDERS and observation in {'failed', 'partial', 'invalid'}:
            failures.setdefault(provider, (observation == 'invalid',
                'invalid_response' if observation == 'invalid' else 'provider_error', None))
    if unavailable:
        missing.update(failures)
        failures.clear()
    opened, alerts = [], []

    def alert(kind, provider, row):
        digest = hashlib.sha256(json.dumps([POLICY, report['generation_id'],
            report['generation_commit'], sources.get('configuration'), report['session'],
            provider, row['first_failed_session'], kind], separators=(',', ':')).encode()).hexdigest()
        alerts.append({'kind': kind, 'provider': provider, 'alert_id': digest})

    for provider in sorted(set(prior) | set(failures) | missing):
        old = prior.get(provider)
        if provider in failures:
            terminal, reason, status = failures[provider]
            count = min(3, (old['consecutive_failed_sessions'] if old else 0) + 1)
            state = 'repair_required' if terminal or count == 3 or old and old['status'] == 'repair_required' else 'degraded'
            row = {'provider': provider, 'status': state, 'observation': 'failed',
                'reason_code': reason, 'http_status': status,
                'first_failed_session': old['first_failed_session'] if old and old['first_failed_session'] else report['session'],
                'last_failed_session': report['session'], 'consecutive_failed_sessions': count}
            if old is None:
                alert('opened', provider, row)
            elif old['status'] != state:
                alert('escalated', provider, row)
        elif (not unavailable and provider not in missing and report.get('evidence_complete') is True and _fresh(report)
                and observations.get(provider) in {'success', 'success_empty', 'complete'}):
            alert('recovered', provider, old)
            continue
        else:
            row = dict(old, observation='unknown', consecutive_failed_sessions=0) if old else {
                'provider': provider, 'status': 'degraded', 'observation': 'unknown',
                'reason_code': 'missing_source_evidence', 'http_status': None,
                'first_failed_session': None, 'last_failed_session': None, 'consecutive_failed_sessions': 0}
            if old is None:
                alert('opened', provider, row)
            elif old['observation'] != 'unknown':
                alert('observation_lost', provider, row)
        opened.append(row)
    return {'policy': POLICY, 'history_status': history_status,
            'configuration': configuration,
            'assessment_status': 'unavailable' if unavailable else 'available',
            'open_incidents': opened, 'alerts': alerts}


def load_source_incidents(report, output_dir) -> dict:
    """Read one bounded prior XNYS report; never scan or mutate older evidence."""
    try:
        session = _identity(report)
        filename = f"{previous_session(session).isoformat()}-{report['generation_id']}-operational-report.json"
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        return {'policy': POLICY, 'history_status': 'invalid', 'assessment_status': 'unavailable',
                'open_incidents': [], 'alerts': []}
    try:
        with (Path(output_dir) / filename).open('rb') as stream:
            payload = stream.read(MAX_REPORT_BYTES + 1)
        if len(payload) > MAX_REPORT_BYTES:
            raise ValueError('incident history exceeds bound')
        previous = json.loads(payload)
        return assess_source_incidents(report, previous)
    except FileNotFoundError:
        return assess_source_incidents(report, history_status='missing')
    except (OSError, UnicodeError, ValueError, TypeError, RecursionError):
        return assess_source_incidents(report, history_status='invalid')
