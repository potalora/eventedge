"""Deterministic exact-session operational evidence, without store migrations."""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from typing import Any

from tradingagents.strategies.modules import get_paper_trade_strategies
from tradingagents.strategies.metrics.store import MetricStore
from tradingagents.strategies.orchestration.daily_pipeline import (
    aggregate_candidate_input_issues, canonical_candidate_input_issue_summaries,
)
from tradingagents.strategies.orchestration.run_outcome import (
    DAILY_RESULT_ENVELOPE_KEYS, DAILY_RESULT_PREFIX, DAILY_RESULT_WIRE_VERSION,
)
from tradingagents.strategies.orchestration.runtime_lock import canonical_runtime_lock_path, runtime_lock
from tradingagents.strategies.orchestration.source_coverage import (
    aggregate_source_health_failures, canonical_source_scope_limits, source_scope_limits_from_health,
)
from tradingagents.strategies.orchestration._io import (
    CANONICAL_COHORTS,
    load_json_strict,
    open_sqlite,
)
from tradingagents.strategies.orchestration.source_inputs import (
    CONTRACT_VERSION, MAX_BYTES, SourceInputStore, configuration_fingerprint,
)
from tradingagents.strategies.state.generation_db import GENERATION_DB_FILENAME
from tradingagents.strategies.state.portfolio_ledger import LedgerConflictError, PortfolioLedger

EXPECTED_COHORTS = tuple(sorted(CANONICAL_COHORTS))
_SAFE_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,191}')
_PROVIDERS = frozenset({'edgar','fred','finnhub','congress','regulations','courtlistener',
                        'noaa','usda','drought_monitor','usaspending','cftc','yfinance','openbb'})
_HEALTHY = frozenset({'signals', 'legitimate_no_event'})
_FAILURES = frozenset({'data_failure', 'strategy_defect', 'unavailable'})
_PREFLIGHT_LABELS = {
    'screen': 'screen (source screening)',
    'governed': 'governed (price readiness)',
    'all': 'all (source screening and price readiness)',
}
_CONFIG_DIGEST = re.compile(r'[0-9a-f]{64}')


def _metrics_db_path(state: Path) -> Path:
    unified = state / GENERATION_DB_FILENAME
    return unified if unified.is_file() else (state / 'metrics_v2.sqlite3')


def _cohort_db_path(state: Path, cohort_name: str) -> tuple[Path, bool]:
    unified = state / GENERATION_DB_FILENAME
    if unified.is_file():
        return unified, True
    return state / cohort_name / 'portfolio.db', False


def _safe_id(value: Any) -> bool:
    return isinstance(value, str) and _SAFE_ID.fullmatch(value) is not None


def _load_json(path: Path) -> Any:
    return load_json_strict(path, max_bytes=32 * 1024 * 1024, reject_symlinks=False)


@contextmanager
def _database(path: Path):
    # mode=ro via _io.open_sqlite with closing() guaranteed.
    with open_sqlite(path, readonly=True) as connection:
        connection.execute('BEGIN')
        yield connection


def _wire(stdout: Any) -> dict[str, Any]:
    if not isinstance(stdout, str):
        raise ValueError('invalid stdout')
    lines = [line for line in stdout.splitlines() if line.strip()]
    marked = [line for line in lines if line.startswith(DAILY_RESULT_PREFIX)]
    if len(marked) != 1 or marked[0] != lines[-1]:
        raise ValueError('missing or ambiguous final wire')
    envelope = json.loads(marked[0].removeprefix(DAILY_RESULT_PREFIX))
    if (not isinstance(envelope, dict) or set(envelope) != DAILY_RESULT_ENVELOPE_KEYS
            or type(envelope['wire_version']) is not int
            or envelope['wire_version'] != DAILY_RESULT_WIRE_VERSION
            or not isinstance(envelope['cohort_results'], dict)):
        raise ValueError('invalid current wire')
    results = envelope['cohort_results']
    if set(results) != set(EXPECTED_COHORTS):
        raise ValueError('incomplete cohort wire')
    for result in results.values():
        if not isinstance(result, dict) or any(type(result.get(key)) is not bool for key in (
                'error', 'execution_valid', 'staging_valid', 'degraded', 'input_coverage_valid')):
            raise ValueError('invalid cohort lifecycle')
        if result['error'] and result['staging_valid']:
            raise ValueError('failed staging marked complete')
        if not result['error'] and (not result['execution_valid'] or
                                    not result['degraded'] and not result['staging_valid']):
            raise ValueError('invalid completed cohort')
    return results


def _wire_outcome(wire: dict) -> str:
    return ('failed' if any(row['error'] for row in wire.values()) else
            'degraded' if any(row['degraded'] for row in wire.values()) else 'clean')


def _preflight_failures(artifact: dict) -> list[dict]:
    if 'screen_source_failures' in artifact['result']:
        from tradingagents.strategies.orchestration.preflight import canonical_screen_source_failures
        failures = canonical_screen_source_failures(artifact['result']['screen_source_failures'])
        if failures is None:
            raise ValueError('invalid native preflight source diagnostics')
        return failures
    failures = []
    structured = artifact['result'].get('failures', [])
    for failure in structured if isinstance(structured, list) else []:
        if not isinstance(failure, dict):
            continue
        source = failure.get('source') or str(failure.get('strategy', '')).removeprefix('source:')
        text = str(failure.get('error', ''))
        status = re.search(r'(?:http_status[=: ]+|HTTP(?:Error)?[ :]+)([45][0-9]{2})', text, re.I)
        failures.append({'source': source if source in _PROVIDERS else 'unknown',
                         'reason_code': 'http_error' if status else 'preflight_failure',
                         'http_status': int(status[1]) if status else None})
    # Some preflight failures have only bounded process logs. Extract fixed
    # provider/status facts; never copy URLs, bodies or exception strings.
    logs = str(artifact.get('stdout', '')) + '\n' + str(artifact.get('stderr', ''))
    for line in logs.splitlines():
        status = re.search(r'(?:http_status[=: ]+|HTTP(?:Error)?[ :]+|status(?: code)?[=: ]+)([45][0-9]{2})', line, re.I)
        if not status:
            continue
        provider = next((name for name in sorted(_PROVIDERS) if name in line.lower()), 'unknown')
        item = {'source': provider, 'reason_code': 'http_error', 'http_status': int(status[1])}
        if item not in failures:
            failures.append(item)
    return sorted(failures, key=lambda item: (item['source'], item['reason_code'], item['http_status'] or 0))


def _attempts(repo: Path, generation: str, session: str, commit: str, diagnose) -> tuple[list[dict], dict | None]:
    attempts = []
    for path in sorted((repo / 'data/logs/run_attempts').glob('*.json')):
        try:
            item = _load_json(path)
            if not isinstance(item, dict):
                raise ValueError('attempt is not a mapping')
            if not _safe_id(item.get('generation_id')) or not isinstance(item.get('requested_session'),str):
                raise ValueError('unbound attempt identity')
            if date.fromisoformat(item['requested_session']).isoformat()!=item['requested_session']:
                raise ValueError('unbound attempt session')
            if item['generation_id'] != generation or item['requested_session'] != session:
                continue
            required = {'schema_version','generation_id','generation_commit','requested_session','action',
                        'preflight_mode','started_at','finished_at','process_return_code','process_status',
                        'stdout','stderr','result'}
            if (set(item) != required or type(item['schema_version']) is not int or item['schema_version'] != 1 or
                    item['action'] not in {'daily','preflight'} or
                    item['process_return_code'] is not None and type(item['process_return_code']) is not int or
                    item['process_status'] not in {'completed','timeout','launch_error'}):
                raise ValueError('invalid attempt contract')
            start = datetime.fromisoformat(item['started_at'])
            end = datetime.fromisoformat(item['finished_at'])
            if start.tzinfo is None or end.tzinfo is None or end < start:
                raise ValueError('invalid attempt timing')
            if not isinstance(item['result'], dict):
                raise ValueError('invalid attempt result')
            mode = item['preflight_mode']
            if item['action'] == 'preflight':
                if (not isinstance(mode, str) or mode not in _PREFLIGHT_LABELS or
                        item['result'].get('preflight_mode', mode) != mode):
                    raise ValueError('invalid preflight mode')
            elif mode is not None:
                raise ValueError('daily attempt has preflight mode')
            entry = {'attempt_id':path.name,'evidence_path':str(path.resolve()),'action':item['action'], 'started_at':start.astimezone(timezone.utc).isoformat(),
                     'finished_at':end.astimezone(timezone.utc).isoformat(),'process_return_code':item['process_return_code'],
                     'process_status':item['process_status'], 'outcome':item['result'].get('outcome'),
                     'result':item['result'], 'wire':None}
            if item['generation_commit'] != commit:
                diagnose('attempt_commit_conflict', path.name)
            if item['action'] == 'daily':
                outcome = item['result'].get('outcome')
                if outcome not in {'clean','degraded','failed'}:
                    diagnose('attempt_outcome_invalid', path.name)
                try:
                    entry['wire'] = _wire(item['stdout'])
                    failures = aggregate_source_health_failures(entry['wire'], session, require_coverage=True)
                    issues = aggregate_candidate_input_issues(entry['wire'], session)
                    if (_wire_outcome(entry['wire']) != outcome or
                            item['result'].get('success') is not (outcome == 'clean') or
                            item['result'].get('input_coverage_valid') is not all(row['input_coverage_valid'] for row in entry['wire'].values()) or
                            item['result'].get('execution_valid') is not all(row['execution_valid'] for row in entry['wire'].values()) or
                            item['result'].get('source_health_failures') != failures or
                            item['result'].get('candidate_input_issues', []) != issues):
                        diagnose('attempt_wire_conflict', path.name)
                except (ValueError, TypeError, KeyError, json.JSONDecodeError):
                    if outcome in {'clean','degraded'}:
                        diagnose('attempt_wire_invalid', path.name)
                if outcome in {'clean','degraded'} and (item['process_status'] != 'completed' or
                        item['process_return_code'] not in ({0} if outcome == 'clean' else {0,2})):
                    diagnose('attempt_process_conflict', path.name)
            else:
                entry['preflight_mode'] = mode
                entry['preflight_ok'] = item['result'].get('success')
                entry['screen_ok'] = item['result'].get('screen_ok')
                entry['generation_commit'] = item['generation_commit']
                configuration = item['result'].get('config_sha256')
                if configuration is not None and (not isinstance(configuration, str)
                        or not _CONFIG_DIGEST.fullmatch(configuration)):
                    raise ValueError('invalid preflight configuration')
                entry['configuration'] = configuration
                if 'screen_runtime_failures' in item['result']:
                    from .preflight import canonical_screen_runtime_failures
                    runtime = canonical_screen_runtime_failures(item['result']['screen_runtime_failures'])
                    if runtime is None:
                        raise ValueError('invalid preflight runtime diagnostics')
                    entry['runtime_failures'] = runtime
                entry['screen_failure_count'] = item['result'].get('screen_failure_count')
                entry['failures'] = _preflight_failures(item)
                entry['source_failures'] = entry['failures']
            attempts.append(entry)
        except (OSError, ValueError, TypeError, KeyError, RecursionError):
            diagnose('attempt_unreadable', path.name)
    attempts.sort(key=lambda item: (item['started_at'],item['finished_at'],item['attempt_id']))
    daily = [item for item in attempts if item['action'] == 'daily']
    if not daily:
        diagnose('daily_attempt_missing')
    if len(daily)>1 and daily[-1]['started_at']==daily[-2]['started_at'] and daily[-1]['finished_at']==daily[-2]['finished_at']:
        if daily[-1]['result']!=daily[-2]['result'] or daily[-1]['wire']!=daily[-2]['wire']:
            diagnose('attempt_order_ambiguous')
    return attempts, daily[-1] if daily else None


def _preflight_runtime_observations(attempts, generation, session):
    """Retain failures; resolve only with a later, equivalent native screen."""
    observations = []
    preflights = [row for row in attempts if row['action'] == 'preflight']
    for failed in preflights:
        if failed['configuration'] is None or not failed.get('runtime_failures'):
            continue  # Legacy/unbound evidence cannot establish notification identity.
        identity = (failed['generation_commit'], failed['configuration'], failed['preflight_mode'])
        later = next((row for row in preflights
            if (row['generation_commit'], row['configuration'], row['preflight_mode']) == identity
            and row['started_at'] > failed['finished_at']
            and row['process_status'] == 'completed' and row['process_return_code'] == 0
            and row['preflight_ok'] is True and row['screen_ok'] is True
            and row.get('runtime_failures') == [] and row['source_failures'] == []
            and type(row['screen_failure_count']) is int and row['screen_failure_count'] == 0), None)
        for failure in failed.get('runtime_failures', []):
            observations.append({'attempt_id': failed['attempt_id'], 'generation_id': generation,
                'generation_commit': failed['generation_commit'], 'configuration': failed['configuration'],
                'session': session, 'preflight_mode': failed['preflight_mode'], **failure,
                'status': 'resolved' if later else 'open',
                'resolved_by': later['attempt_id'] if later else None})
    return observations


def _manifest_history(metadata: dict, attempts: list[dict], latest: dict | None, repo: Path, session: str, diagnose) -> None:
    history = metadata.get('run_history')
    if not isinstance(history, list):
        diagnose('manifest_session_history_invalid')
        return
    exact = [row for row in history if isinstance(row,dict) and row.get('date')==session and row.get('action')=='daily']
    if not exact:
        diagnose('manifest_session_history_missing')
        return
    by_path = {row['evidence_path']:row for row in attempts if row['action']=='daily'}
    declared = set()
    for row in exact:
        reference = row.get('evidence_path')
        if not isinstance(reference,str):
            diagnose('manifest_session_artifact_missing')
            continue
        path = Path(reference)
        reference = str((path if path.is_absolute() else repo/path).resolve())
        attempt = by_path.get(reference)
        if attempt is None:
            diagnose('manifest_session_artifact_missing')
            continue
        declared.add(attempt['attempt_id'])
        for key in ('outcome','success','execution_valid','input_coverage_valid','source_health_failures','candidate_input_issues'):
            if row.get(key)!=attempt['result'].get(key):
                diagnose('manifest_session_result_conflict',attempt['attempt_id'])
    if latest is not None and latest['attempt_id'] not in declared:
        diagnose('manifest_session_latest_missing')


def _declared_exclusion(row: dict, disabled: dict | None = None) -> bool:
    reason = next((getattr(strategy, 'retirement_reason', None)
                   for strategy in get_paper_trade_strategies()
                   if strategy.name == row.get('strategy')), None)
    evidence = row.get('evidence')
    from .source_lifecycle_policy import POLICY as LIFECYCLE_POLICY
    if (isinstance(evidence, dict) and evidence.get('source_lifecycle_policy') == LIFECYCLE_POLICY
            and (disabled or {}).get(row.get('strategy')) == evidence.get('reason')
            and isinstance(evidence.get('source_lifecycle_sha256'), str)
            and re.fullmatch('[a-f0-9]{64}', evidence['source_lifecycle_sha256'])):
        reason = evidence['reason']
    return (row.get('status') == 'disabled_by_policy' and reason is not None
            and row.get('signal_count') == 0 and isinstance(evidence, dict)
            and evidence.get('reason') == reason)


def _metrics(state: Path, generation: str, session: str, commit: str, diagnose, disabled=None) -> tuple[str | None, list[dict]]:
    path = _metrics_db_path(state)
    if not path.exists():
        diagnose('metrics_missing')
        return None, []
    try:
        with _database(path) as connection:
            epochs = []
            for row in connection.execute('SELECT epoch_id,payload_json FROM metric_epochs'):
                value = json.loads(row['payload_json'])
                if value['generation_id'] != generation or not value['start_session'] <= session or value.get('end_session') and value['end_session'] < session:
                    continue
                if value['epoch_id'] != row['epoch_id'] or value['generation_commit'] != commit:
                    diagnose('metric_epoch_identity_conflict')
                epochs.append(value)
            if len(epochs) != 1:
                diagnose('metric_epoch_ambiguous')
                return None, []
            epoch = epochs[0]['epoch_id']
            health = []
            for row in connection.execute('SELECT health_id,epoch_id,session,payload_json FROM strategy_health WHERE session=? ORDER BY health_id', (session,)):
                value = json.loads(row['payload_json'])
                if (value.get('health_id') != row['health_id'] or value.get('epoch_id') != row['epoch_id'] or
                        value.get('session') != session or row['epoch_id'] != epoch):
                    diagnose('health_identity_conflict', row['health_id'])
                    continue
                if (not all(_safe_id(value.get(key)) for key in ('health_id','policy_id','strategy')) or
                        (value.get('status') not in _HEALTHY | _FAILURES and not _declared_exclusion(value, disabled)) or
                        not isinstance(value.get('evidence'),dict)):
                    diagnose('health_contract_invalid', row['health_id'])
                    continue
                health.append(value)
            return epoch, health
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
        diagnose('metrics_unreadable')
        return None, []


def _book(state: Path, name: str, generation: str, session: str, epoch: str | None, diagnose) -> dict:
    result = {'accounting_valid':False, 'staging_complete':False, 'staging_volatility':{}, 'fills':None}
    path, unified = _cohort_db_path(state, name)
    if not path.exists():
        diagnose('ledger_missing', name)
        return result
    try:
        with _database(path) as connection:
            if unified:
                identity_ok = True
                runs = connection.execute('SELECT * FROM session_runs WHERE cohort_id=? AND session=?', (name, session)).fetchall()
                snapshots = connection.execute('SELECT * FROM account_snapshots WHERE cohort_id=? AND session=?', (name, session)).fetchall()
                contexts = connection.execute('SELECT * FROM session_execution_contexts WHERE cohort_id=? AND session=?', (name, session)).fetchall()
            else:
                identity = connection.execute("SELECT metadata_value FROM schema_metadata WHERE metadata_key='cohort_id'").fetchone()
                identity_ok = identity is not None and identity[0] == name
                if not identity_ok:
                    diagnose('ledger_cohort_conflict', name)
                runs = connection.execute('SELECT * FROM session_runs WHERE session=?', (session,)).fetchall()
                snapshots = connection.execute('SELECT * FROM account_snapshots WHERE session=?', (session,)).fetchall()
                contexts = connection.execute('SELECT * FROM session_execution_contexts WHERE session=?', (session,)).fetchall()
            valid = (identity_ok and epoch is not None
                     and len(runs)==1 and runs[0]['cohort_id']==name and runs[0]['valid']==1 and bool(runs[0]['completed_at']) and not runs[0]['invalid_reason']
                     and len(snapshots)==1 and snapshots[0]['cohort_id']==name and snapshots[0]['epoch_id']==epoch and snapshots[0]['valid']==1 and not snapshots[0]['invalid_reason']
                     and len(contexts)==1 and contexts[0]['cohort_id']==name and contexts[0]['epoch_id']==epoch)
            if not valid:
                diagnose('ledger_session_invalid', name)
            if len(snapshots)==1:
                equity = Decimal(snapshots[0]['net_equity'])
                if not equity.is_finite():
                    diagnose('snapshot_equity_invalid', name)
                    valid = False
            if unified:
                stages = connection.execute('SELECT cohort_id,epoch_id,completed_at FROM staging_runs WHERE cohort_id=? AND session=?', (name, session)).fetchall()
                result['staging_complete'] = len(stages)==1 and stages[0]['cohort_id']==name and stages[0]['epoch_id']==epoch and bool(stages[0]['completed_at'])
                policy_rows=connection.execute("SELECT * FROM policy_audit_records WHERE cohort_id=? AND session=? AND record_type='policy_session_context' AND record_id=?", (name, session, f'{session}:staging')).fetchall()
            else:
                stages = connection.execute('SELECT cohort_id,epoch_id,completed_at FROM staging_runs WHERE session=?', (session,)).fetchall()
                result['staging_complete'] = len(stages)==1 and stages[0]['cohort_id']==name and stages[0]['epoch_id']==epoch and bool(stages[0]['completed_at'])
                policy_rows=connection.execute("SELECT * FROM policy_session_contexts WHERE session=? AND binding_kind='staging'",(session,)).fetchall()
            if not policy_rows and result['staging_complete']:
                diagnose('staging_volatility_context_missing',name)
            elif policy_rows:
                try:
                    from tradingagents.strategies.trading.portfolio_policy import _validated_volatility_evidence
                    if len(policy_rows)!=1:
                        raise ValueError('ambiguous staging context')
                    # This native decoder is pure: it reads only the supplied
                    # row, verifying canonical JSON and every binding digest.
                    context=PortfolioLedger._policy_session_context_from_row(None,policy_rows[0])
                    if context['cohort_id']!=name or context['epoch_id']!=epoch or context['session']!=date.fromisoformat(session):
                        raise ValueError('staging context identity conflict')
                    result['staging_volatility']=_validated_volatility_evidence(context['context']['annualized_volatility'])
                    clock = context['context'].get('decision_clock')
                    if isinstance(clock,dict) and 'document_review_binding' in clock:
                        result['document_review_clock'] = clock
                except (LedgerConflictError,ValueError,TypeError,KeyError,AttributeError):
                    diagnose('staging_volatility_context_invalid',name)
            if unified:
                fills = connection.execute('''SELECT f.fill_id,f.side,f.session,i.intent_id,i.cohort_id,
                    i.side AS intent_side,i.eligible_session,i.price_rule FROM fills f LEFT JOIN order_intents i ON i.intent_id=f.intent_id
                    WHERE f.cohort_id=? AND f.session=? ORDER BY f.fill_id''', (name, session)).fetchall()
            else:
                fills = connection.execute('''SELECT f.fill_id,f.side,f.session,i.intent_id,i.cohort_id,
                    i.side AS intent_side,i.eligible_session,i.price_rule FROM fills f LEFT JOIN order_intents i ON i.intent_id=f.intent_id
                    WHERE f.session=? ORDER BY f.fill_id''', (session,)).fetchall()
            def eligible(row):
                eligible_session=date.fromisoformat(row['eligible_session'])
                if eligible_session.isoformat()!=row['eligible_session']:
                    return False
                return ((row['price_rule']=='next_session_open' and eligible_session==date.fromisoformat(session)) or
                        (row['price_rule']=='resting_stop' and eligible_session<=date.fromisoformat(session)))
            if any(row['intent_id'] is None or row['cohort_id']!=name or row['side']!=row['intent_side'] or not eligible(row) or row['side'] not in {'buy','short','sell','cover'} for row in fills):
                diagnose('fill_identity_conflict', name)
            else:
                result['fills'] = {'total':len({row['fill_id'] for row in fills}),
                                  'entries':len({row['fill_id'] for row in fills if row['side'] in {'buy','short'}}),
                                  'exits':len({row['fill_id'] for row in fills if row['side'] in {'sell','cover'}})}
            result['accounting_valid'] = valid
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, InvalidOperation):
        diagnose('ledger_unreadable', name)
    return result


def _candidate_issues(state: Path, epoch: str | None, session: str, diagnose) -> list[dict]:
    if epoch is None:
        return []
    try:
        with _database(_metrics_db_path(state)) as connection:
            rows=connection.execute('''SELECT issue_id,epoch_id,session,dependency_kind,ticker,payload_json
                FROM candidate_input_issues WHERE session=?
                ORDER BY dependency_kind,ticker,issue_id LIMIT 1001''',(session,)).fetchall()
            if len(rows)>1000:
                raise ValueError('too many candidate issues')
            references=[]
            for row in rows:
                # Pure native decoder checks canonical bytes and column identity;
                # using it does not instantiate or migrate a store.
                issue=MetricStore._candidate_input_issue(tuple(row))
                if issue.epoch_id!=epoch or not set(issue.affected_cohorts)<=set(EXPECTED_COHORTS):
                    raise ValueError('candidate issue scope conflict')
                references.append(issue.reference())
            return canonical_candidate_input_issue_summaries(references,session) if references else []
    except (OSError,sqlite3.Error,ValueError,TypeError,KeyError):
        diagnose('candidate_issues_invalid')
        return []


def _health_coverage(health: list[dict], epoch: str | None, session: str, diagnose, disabled=None, *, strategies=None) -> list[dict]:
    strategies = ({strategy.name for strategy in get_paper_trade_strategies()}
                  if strategies is None else set(strategies))
    failures = []
    scoped = {}
    for row in health:
        key = (row['policy_id'], row['strategy'])
        bound_policy = any(row['policy_id']=='foundation-'+horizon or row['policy_id'].endswith(':health:'+horizon) for horizon in ('30d','3m','6m','1y'))
        if key in scoped or row['strategy'] not in strategies or not bound_policy:
            diagnose('health_scope_conflict', row['health_id'])
        scoped[key] = row
    for horizon in ('30d','3m','6m','1y'):
        policies = {row['policy_id'] for row in health if row['policy_id']=='foundation-'+horizon or row['policy_id'].endswith(':health:'+horizon)}
        if len(policies)!=1:
            diagnose('health_policy_missing_or_ambiguous', horizon)
            policy = 'foundation-'+horizon
        else:
            policy = next(iter(policies))
        affected = [name for name in EXPECTED_COHORTS if name.startswith('horizon_'+horizon+'_')]
        for strategy in sorted(strategies):
            row = scoped.get((policy,strategy))
            if row is not None and (row['status'] in _HEALTHY or _declared_exclusion(row, disabled)):
                continue
            if row is None:
                diagnose('health_missing', policy+':'+strategy)
            errors = row.get('evidence',{}).get('provider_errors',{}) if row else {}
            identifier = hashlib.sha256(json.dumps([epoch,session,policy,strategy]).encode()).hexdigest()[:32]
            failures.append({'health_id':row['health_id'] if row else 'missing_health_'+identifier,
                             'epoch_id':epoch,'session':session,'policy_id':policy,'strategy':strategy,
                             'status':row['status'] if row else 'missing_health',
                             'sources':sorted(key for key in errors if _safe_id(key)) if isinstance(errors,dict) else [],
                             'affected_cohorts':affected})
    return sorted(failures,key=lambda row:(row['policy_id'],row['strategy'],row['health_id']))


def _accepted_envelope(path: Path, generation: str, session: str, commit: str, *, purpose: str | None = None) -> dict:
    from .source_inputs import _from_plain

    if path.stat().st_size > MAX_BYTES:
        raise ValueError('source byte bound')
    raw_doc = load_json_strict(path, max_bytes=MAX_BYTES, reject_symlinks=False)
    if (not isinstance(raw_doc, dict)
            or set(raw_doc) != {'version', 'identity', 'acquired_at', 'payload', 'digest'}
            or raw_doc['version'] != CONTRACT_VERSION):
        raise ValueError('source identity/content conflict')
    identity = raw_doc['identity']
    keys = {'generation', 'session', 'commit', 'configuration'} | ({'purpose'} if purpose else set())
    if (not isinstance(identity, dict) or set(identity) != keys
            or identity.get('purpose') != purpose
            or identity['generation'] != generation
            or identity['session'] != session
            or identity['commit'] != commit
            or not isinstance(identity['configuration'], str)
            or not identity['configuration']):
        raise ValueError('source identity/content conflict')
    payload_json = json.dumps(raw_doc['payload'], allow_nan=False, separators=(',', ':'), sort_keys=True)
    if raw_doc['digest'] != hashlib.sha256(payload_json.encode('utf-8')).hexdigest():
        raise ValueError('source identity/content conflict')
    acquired_raw = raw_doc['acquired_at']
    if isinstance(acquired_raw, dict):
        acquired = _from_plain(acquired_raw, depth=0)
    else:
        acquired = (datetime.fromisoformat(acquired_raw) if isinstance(acquired_raw, str) else acquired_raw)
    if not isinstance(acquired, datetime) or acquired.tzinfo is None or acquired > datetime.now(timezone.utc):
        raise ValueError('source identity/content conflict')
    table_dir = path.parent / (path.stem + '.tables')
    payload = _from_plain(raw_doc['payload'], depth=0, table_dir=table_dir)
    if not isinstance(payload, dict):
        raise ValueError('source identity/content conflict')
    return {
        'version': raw_doc['version'],
        'identity': identity,
        'acquired_at': acquired,
        'payload': payload,
        'digest': raw_doc['digest'],
    }


def _volatility(state: Path, generation: str, session: str, commit: str, sources: dict, issues: list[dict], books: dict, diagnose) -> dict:
    from tradingagents.strategies.orchestration.trading_calendar import previous_session
    from tradingagents.strategies.trading.portfolio_policy import build_annualized_volatility_evidence
    slot=configuration_fingerprint({'generation':generation,'session':session})
    path=state/'source_inputs/staging_volatility'/(slot+'.json')
    result={'valid':False,'evidence_path':str(path),'tickers':[]}
    if not path.exists():
        diagnose('accepted_volatility_missing')
        return result
    try:
        envelope=_accepted_envelope(path,generation,session,commit,purpose='staging-volatility-v1')
        document=envelope['payload']
        expected=document['expected_sessions']
        quarantined=tuple(sorted(issue['ticker'] for issue in issues if issue['dependency_kind']=='volatility_history'))
        if (envelope['identity']['configuration']!=sources.get('configuration') or
                set(document)!={'price_history','expected_sessions','lookback','floor','quarantined_tickers'} or
                type(document['lookback']) is not int or document['lookback']<=0 or
                not isinstance(document['price_history'],dict) or not isinstance(expected,tuple) or
                not expected or any(type(value) is not date for value in expected) or
                expected[-1]!=previous_session(date.fromisoformat(session)) or
                any(previous_session(later)!=earlier for earlier,later in zip(expected,expected[1:])) or
                document['quarantined_tickers']!=quarantined):
            raise ValueError('volatility source contract invalid')
        measured=build_annualized_volatility_evidence(document['price_history'],document['price_history'],
            lookback_sessions=document['lookback'],floor=document['floor'],expected_sessions=expected)
        if any(measured.get(ticker)!=value for book in books.values() for ticker,value in book['staging_volatility'].items()):
            raise ValueError('accepted history differs from bound staging evidence')
        result.update(valid=True,tickers=sorted(measured),lookback_sessions=document['lookback'],
            acquired_at=envelope['acquired_at'].isoformat())
    except (OSError,ValueError,TypeError,KeyError,AttributeError):
        diagnose('accepted_volatility_invalid')
    return result


def _regulation_transport_recoveries(payload, validated_scope):
    _ = (payload, validated_scope)
    return {}, False


def _award_recipient_transport_recoveries(payload: dict, *, session: str) -> tuple[dict, bool]:
    """Replay every new recipient proof before interpreting its retained failure.

    Historical award payloads have no such proof and keep their old reporting.
    New evidence must bind to the complete award scope, even when no matching
    transport diagnostic exists.
    """
    from ..data_sources.award_attribution_policy import validate_attribution_scope
    from ..data_sources.fetch_errors import SourceFetchError
    if not isinstance(payload, dict):
        return {}, False
    data = payload.get('data')
    records = data.get('contracts', []) if isinstance(data, dict) else []
    proof_rows = [row for row in records if isinstance(row, dict)
                  and (row.get('recipient_identity_status') == 'native_recipient_verified'
                       or 'recipient_identity_evidence' in row)] if isinstance(records, list) else []
    if not proof_rows:
        return {}, False
    from ..data_sources.award_recipient_evidence import (
        canonical_json_sha256, detail_request_operation, validate_recipient_fallback,
    )
    error = payload.get('error')
    coverages = (payload.get('coverage'), data.get('coverage'))
    if (isinstance(error, str) and error.strip() and 'award_attribution_policy' not in payload
            and not any(isinstance(coverage, dict) and 'attribution_scope' in coverage
                        for coverage in coverages)):
        # A failed acquisition retains observations, including partial captures.
        # These confer no source-scope or transport-recovery authority. Mixed
        # populations must still replay every row that claims recipient success.
        from urllib.parse import quote
        operations = set()
        try:
            for row in proof_rows:
                if row.get('recipient_identity_status') == 'native_recipient_verified':
                    validate_recipient_fallback(row)
                elif (row.get('recipient_identity_status') != 'lookup_failed'
                        or row.get('parent_recipient_uei') != ''
                        or not isinstance(row.get('recipient_identity_evidence'), dict)):
                    raise ValueError('invalid failed award recipient observation')
                evidence = row['recipient_identity_evidence']
                detail = evidence.get('detail_attempt')
                if isinstance(detail, dict) and isinstance(detail.get('url'), str):
                    operations.add(detail_request_operation(detail['url']))
                key = row.get('generated_internal_id') or row.get('internal_id')
                if type(key) in (str, int):
                    operations.add(detail_request_operation(
                        'https://api.usaspending.gov/api/v2/awards/' + quote(str(key), safe='') + '/'))
        except SourceFetchError as failure:
            raise ValueError('award recipient evidence invalid') from failure
        events = payload.get('_request_diagnostics', [])
        if not isinstance(events, list):
            raise ValueError('award recipient diagnostics invalid')
        for event in events:
            if (isinstance(event, dict) and isinstance(event.get('operation'), str)
                    and event['operation'] in operations and event.get('http_status') == 404
                    and event.get('recovered') is not False):
                raise ValueError('award recipient original transport failure changed')
        return {}, False
    try:
        scope = validate_attribution_scope(payload, session=session)
    except SourceFetchError as error:
        raise ValueError('award recipient evidence invalid') from error
    proofs = {}
    for row in records:
        if row.get('recipient_identity_status') != 'native_recipient_verified':
            continue
        evidence = row['recipient_identity_evidence']
        operation = detail_request_operation(evidence['detail_attempt']['url'])
        if operation in proofs:
            raise ValueError('duplicate award recipient proof operation')
        proofs[operation] = {
            'policy': evidence['policy'],
            'award_attribution_scope_sha256': scope['scope_sha256'],
            'award_key': row['award_key'],
            'detail_body_sha256': evidence['detail_attempt']['body_sha256'],
            'transaction_proof_sha256': canonical_json_sha256(evidence['transaction_pages']),
            'recipient_profile_body_sha256': evidence['recipient_profile']['body_sha256']}
    events = payload.get('_request_diagnostics', [])
    if not isinstance(events, list):
        raise ValueError('award recipient diagnostics invalid')
    by_operation = {operation: [] for operation in proofs}
    for index, event in enumerate(events):
        if isinstance(event, dict) and isinstance(event.get('operation'), str) and event['operation'] in proofs:
            if event.get('recovered') is not False:
                raise ValueError('award recipient original transport failure changed')
            by_operation[event['operation']].append((index, event))
    recovered, mismatch = {}, False
    fields = ('provider', 'operation', 'reason_code', 'http_status', 'recovered')
    for operation, basis in proofs.items():
        matches = by_operation[operation]
        expected = ('usaspending', operation, 'http_error', 404, False)
        if (len(matches) != 1 or tuple(matches[0][1].get(key) for key in fields) != expected
                or type(matches[0][1].get('http_status')) is not int
                or type(matches[0][1].get('attempts')) is not int
                or not 1 <= matches[0][1]['attempts'] <= 5
                or matches[0][1].get('recovered') is not False):
            mismatch = True
            continue
        recovered[matches[0][0]] = basis
    return recovered, mismatch


def _sources(state: Path, generation: str, session: str, commit: str, diagnose, *, retained_sources=None, operating_profile=None, frozen_config=None) -> dict:
    result = {'recovered':[], 'unresolved':[], 'observations':{}}
    slot = configuration_fingerprint({'generation':generation,'session':session})
    path = state / 'source_inputs' / (slot+'.json')
    if not path.exists():
        diagnose('accepted_sources_missing')
        return result
    try:
        envelope = _accepted_envelope(path,generation,session,commit)
        identity = envelope['identity']
        if operating_profile is not None and retained_sources is not None:
            retained_sources['_completion_source_envelope'] = envelope
        result['configuration'] = identity['configuration']
        result['acquired_at'] = envelope['acquired_at'].isoformat()
        award_recipient_recoveries, award_recipient_mismatch = _award_recipient_transport_recoveries(
            envelope['payload'].get('usaspending', {}), session=session)
        regulation_recoveries, regulation_mismatch = {}, False
        bulk_scope = None
        active_strategies = get_paper_trade_strategies()
        if operating_profile is not None:
            from .operating_contract import configure_strategies
            active_strategies = configure_strategies(active_strategies, operating_profile.config['autoresearch'], full_config=operating_profile.config)
        _active_tier2 = frozenset({'yfinance', 'fred', 'finnhub', 'usaspending', 'noaa', 'usda', 'drought_monitor', 'cftc'})
        required = {source for strategy in active_strategies for source in strategy.data_sources if source in _active_tier2}
        for source in sorted(required-set(envelope['payload'])):
            diagnose('accepted_source_missing',source)
            result['observations'][source]='missing'
            result['unresolved'].append({'provider':source,'reason_code':'missing_source_evidence','http_status':None})
        for source,payload in sorted(envelope['payload'].items()):
            if not _safe_id(source) or source.startswith('_'):
                continue
            if not isinstance(payload,dict):
                diagnose('accepted_source_payload_invalid',source)
                result['observations'][source]='invalid'
                result['unresolved'].append({'provider':source,'reason_code':'invalid_response','http_status':None})
                continue
            coverage=payload.get('_coverage')
            coverage_status=coverage.get('status') if isinstance(coverage,dict) else None
            if '_coverage' in payload and coverage_status not in {'ok','success','success_empty','complete','partial','failed','unavailable'}:
                diagnose('source_coverage_invalid',source)
                result['observations'][source]='invalid'
                result['unresolved'].append({'provider':source,'reason_code':'invalid_response','http_status':None})
            else:
                result['observations'][source]=coverage_status or ('failed' if payload.get('error') not in (None,'') else 'success')
            if source == 'regulations' and regulation_mismatch:
                result['unresolved'].append({'provider':source,'source':source,
                    'reason_code':'government_copy_diagnostics_mismatch','http_status':None})
            if source == 'usaspending' and award_recipient_mismatch:
                result['unresolved'].append({'provider':source,'source':source,
                    'reason_code':'recipient_fallback_diagnostics_mismatch','http_status':None})
            for event_index, event in enumerate(payload.get('_request_diagnostics',[])):
                if not isinstance(event,dict) or not all(_safe_id(event.get(key)) for key in ('provider','operation')) or event.get('reason_code') is not None and not _safe_id(event['reason_code']) or type(event.get('recovered')) is not bool or type(event.get('attempts')) is not int or not 0<=event['attempts']<=5:
                    raise ValueError('source diagnostics invalid')
                status = event.get('http_status')
                if status is not None and (type(status) is not int or not 100<=status<=599):
                    raise ValueError('source status invalid')
                safe = {key:event.get(key) for key in ('provider','operation','reason_code','http_status','attempts','recovered')}
                # Request-budget identity can differ from the observed source
                # (e.g. Alpaca transports the equity-universe source).
                safe['source'] = source
                safe['reason_code'] = event.get('reason_code') or 'success'
                if source == 'regulations' and event_index in regulation_recoveries:
                    safe.update(recovered=True, transport_recovered=event['recovered'],
                                recovery_basis=regulation_recoveries[event_index])
                if source == 'usaspending' and event_index in award_recipient_recoveries:
                    safe.update(recovered=True, transport_recovered=False,
                                recovery_basis=award_recipient_recoveries[event_index])
                if safe['recovered']:
                    result['recovered'].append(safe)
                elif safe['reason_code']!='success':
                    result['unresolved'].append(safe)
            if source == 'edgar' and bulk_scope is not None:
                # Exact validated declaration+sentinel only; arbitrary SEC errors
                # and focused-litigation identity failures remain failures.
                result['observations'][source] = 'disabled_by_policy'
                continue
            if payload.get('error') not in (None,'') or coverage_status in {'partial','failed'}:
                if coverage_status in {'success','success_empty','complete'}:
                    diagnose('source_coverage_invalid',source)
                result['unresolved'].append({'provider':source,'reason_code':coverage.get('reason_code','provider_error') if isinstance(coverage,dict) and _safe_id(coverage.get('reason_code','provider_error')) else 'provider_error',
                                             'http_status':coverage.get('http_status') if isinstance(coverage,dict) and type(coverage.get('http_status')) is int else None})
        for kind in ('recovered','unresolved'):
            result[kind] = sorted({json.dumps(row,sort_keys=True):row for row in result[kind]}.values(),key=lambda row:json.dumps(row,sort_keys=True))
        # Bind observational learning only after the accepted-source projection
        # has passed its existing identity, digest and source-contract checks.
        result['observation_sha256'] = envelope['digest']
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        diagnose('accepted_sources_invalid')
    return result


def report_runtime_guard(repo, *, snapshot_guaranteed=False):
    """Retain a verified controller lease without releasing or downgrading it."""
    if snapshot_guaranteed:
        return nullcontext()
    from .runtime_lock import RuntimeLockInvalid
    raw_fd = os.environ.get('EVENTEDGE_RUNTIME_LOCK_FD')
    raw_mode = os.environ.get('EVENTEDGE_RUNTIME_LOCK_MODE')
    if raw_fd is None and raw_mode is None:
        return runtime_lock(canonical_runtime_lock_path(repo), exclusive=False)
    if (not isinstance(raw_fd, str) or not raw_fd.isdecimal()
            or raw_mode not in {'shared', 'exclusive'}):
        raise RuntimeLockInvalid('inherited report runtime lock is invalid')
    exclusive = raw_mode == 'exclusive'
    return runtime_lock(canonical_runtime_lock_path(repo), exclusive=exclusive,
        inherited_fd=int(raw_fd), inherited_exclusive=exclusive)


def build_operational_report(repo_root: str | Path, generation: str, session: str, *, snapshot_guaranteed: bool = False, _state_snapshot: Path | None = None) -> dict:
    """Read one stable generation/session; caller may attest an immutable snapshot.

    Normal runtime callers acquire the canonical shared lock. The explicit
    snapshot guarantee is for archived copies/offline fixtures, never an implicit
    fallback when a live lock is busy or invalid.
    """
    from .source_reliability import load_source_reliability
    if not _safe_id(generation) or date.fromisoformat(session).isoformat()!=session:
        raise ValueError('invalid report identity')
    if _state_snapshot is not None and not snapshot_guaranteed:
        raise ValueError('state snapshot requires an explicit immutable snapshot guarantee')
    repo = Path(repo_root).resolve()
    guard = report_runtime_guard(repo, snapshot_guaranteed=snapshot_guaranteed)
    with guard:
        diagnostics = []
        def diagnose(code, scope=None):
            item = {'code':code}
            if scope is not None:
                item['scope'] = scope if _safe_id(scope) else 'artifact'
            if item not in diagnostics:
                diagnostics.append(item)
        report = {'schema_version':1,'generation_id':generation,'session':session,'generation_commit':None,
                  'epoch_id':None,'outcome':'incomplete','evidence_complete':False,'accounting_valid':False,
                  'input_coverage_valid':False,'staging_complete':False,'staging_valid':False,'performance_claims_withheld':True,
                  'attempt_counts':{'daily':0,'preflight':0},'attempts':[],'preflight_incidents':[],
                  'preflight_runtime_observations':[],
                  'source_health_failures':[],'candidate_input_issues':[],'sources':{'recovered':[],'unresolved':[]},'volatility':{'valid':False},'cohorts':{},'diagnostics':diagnostics}
        try:
            manifest = _load_json(repo/'data/generations/manifest.json')
            matches = [row for row in manifest['generations'] if row['gen_id']==generation]
            if len(matches)!=1 or not _safe_id(matches[0]['git_commit']):
                raise ValueError('generation identity conflict')
            metadata = matches[0]
            commit = metadata['git_commit']
            state = Path(metadata['state_dir'])
            state = state.resolve() if state.is_absolute() else (repo/state).resolve()
            if _state_snapshot is not None:
                state = Path(_state_snapshot).resolve()
            report['generation_commit'] = commit
        except (OSError, ValueError, TypeError, KeyError):
            diagnose('manifest_invalid')
            report['source_reliability'] = load_source_reliability(report, repo)
            return report
        operating_profile = None
        frozen_profile = None
        if metadata.get('config_file') is not None:
            try:
                from .generation_config import read_generation_config
                _, frozen_profile, digest = read_generation_config(metadata['config_file'], metadata['config_sha256'])
                report['profile_sha256'] = digest
                from .operating_contract import configured as stabilization_configured, profile_contract
                if stabilization_configured(frozen_profile):
                    operating_profile = profile_contract(frozen_profile, profile_sha256=digest)
            except (OSError, ValueError, TypeError, KeyError):
                diagnose('frozen_profile_invalid')
        attempts,latest = _attempts(repo,generation,session,commit,diagnose)
        _manifest_history(metadata,attempts,latest,repo,session,diagnose)
        report['attempt_counts'] = {action:sum(row['action']==action for row in attempts) for action in ('daily','preflight')}
        report['attempts'] = [{key:value for key,value in row.items() if key not in {'wire','result','failures'}} for row in attempts]
        report['preflight_incidents'] = [
            {'attempt_id':row['attempt_id'],'process_return_code':row['process_return_code'],
             'preflight_mode':row['preflight_mode'],
             'screen_failure_count':row['screen_failure_count'],'failures':row['failures'],
             **({'runtime_failures':row['runtime_failures']} if 'runtime_failures' in row else {})}
            for row in attempts if row['action']=='preflight' and (row['preflight_ok'] is not True or row['failures'] or row.get('runtime_failures'))
        ]
        report['preflight_runtime_observations'] = _preflight_runtime_observations(attempts, generation, session)
        retained_sources = {}
        report['sources'] = _sources(state,generation,session,commit,diagnose, retained_sources=retained_sources,
            operating_profile=operating_profile, frozen_config=frozen_profile)
        completion_source = retained_sources.get('_completion_source_envelope')
        disabled = report['sources'].get('declared_exclusions', {})
        report_strategies = list(get_paper_trade_strategies())
        if operating_profile is not None:
            from .operating_contract import configure_strategies
            report_strategies = configure_strategies(report_strategies, operating_profile.config['autoresearch'], full_config=operating_profile.config)
        epoch,health = _metrics(state,generation,session,commit,diagnose,disabled)
        report['epoch_id'] = epoch
        report['cohorts'] = {name:_book(state,name,generation,session,epoch,diagnose) for name in EXPECTED_COHORTS}
        report['accounting_valid'] = all(row['accounting_valid'] for row in report['cohorts'].values())
        report['staging_complete'] = all(row['staging_complete'] for row in report['cohorts'].values())
        report['candidate_input_issues'] = _candidate_issues(state,epoch,session,diagnose)
        for row in report['cohorts'].values():
            row['staging_valid'] = row['staging_complete'] and not report['candidate_input_issues']
        report['staging_valid'] = all(row['staging_valid'] for row in report['cohorts'].values())
        report['source_health_failures'] = _health_coverage(health,epoch,session,diagnose,disabled,
            strategies={strategy.name for strategy in report_strategies})
        retained_sources.clear()
        try:
            scope_limits = source_scope_limits_from_health(health)
            expected_congress_scope = report['sources'].get('congress_audit_scope')
            if scope_limits.get('congress') != expected_congress_scope:
                diagnose('congress_audit_scope_conflict')
            if disabled.get('congressional_trades') == 'display_audit_only_v1':
                for row in health:
                    if row['strategy'] != 'congressional_trades':
                        continue
                    evidence = row['evidence']
                    if (not _declared_exclusion(row, disabled)
                            or type(row.get('signal_count')) is not int
                            or type(evidence.get('candidate_count')) is not int
                            or evidence['candidate_count'] != 0
                            or evidence.get('data_sources') != ['congress', 'yfinance']
                            or canonical_source_scope_limits(evidence.get('source_scope_limits', {}))
                               != ({'congress': expected_congress_scope} if expected_congress_scope is not None else {})):
                        diagnose('congress_audit_scope_conflict', row['policy_id'])
            if scope_limits:
                report['source_scope_limits'] = scope_limits
        except (ValueError, TypeError, KeyError):
            diagnose('source_scope_limits_invalid')
        report['disabled_strategies'] = {row['strategy']: row['evidence']['reason'] for row in health if _declared_exclusion(row,disabled)}
        report['input_coverage_valid'] = epoch is not None and not report['source_health_failures']
        report['volatility'] = _volatility(state,generation,session,commit,report['sources'],report['candidate_input_issues'],report['cohorts'],diagnose)
        optional_failures = []
        if operating_profile is not None and completion_source is not None:
            try:
                from .operating_contract import source_support, optional_source_failure
                capability_scope, required_capability_failures = source_support(operating_profile.config, completion_source['payload'])
                report['sources']['capability_scope'] = capability_scope
                optional_failures = [item for item in report['sources']['unresolved']
                    if optional_source_failure(item, completion_source['payload'], required_capability_failures)]
            except (ValueError, TypeError, KeyError, AttributeError):
                diagnose('source_capabilities_invalid')
        failed_sources = {item['provider'] for item in report['sources']['unresolved'] if item not in optional_failures}
        required_failures = {source for strategy in report_strategies
            if strategy.name not in report['disabled_strategies']
            for source in strategy.data_sources if source!='openbb'} & failed_sources
        if required_failures:
            report['input_coverage_valid'] = False
        for item in report['sources']['unresolved']:
            if item in optional_failures:
                continue
            source=item['provider']
            for strategy in report_strategies:
                if source=='openbb' or source not in strategy.data_sources or strategy.name in report['disabled_strategies']:
                    continue
                for horizon in ('30d','3m','6m','1y'):
                    failures=[failure for failure in report['source_health_failures'] if failure['strategy']==strategy.name and
                        (failure['policy_id']=='foundation-'+horizon or failure['policy_id'].endswith(':health:'+horizon))]
                    shared_hold = False
                    if operating_profile is not None and source in {'fred','yfinance'} and len(failures) == 1:
                        capabilities = report['sources'].get('capability_scope', {}).get('capabilities', {})
                        usable = lambda name: capabilities.get(name, {}).get('status') == 'usable'
                        shared_unavailable = (not usable('fred.hy_spread') or not usable('fred.yield_curve')
                            or not usable('fred.vix') and not usable('yfinance.vix'))
                        raw = [row for row in health if row['policy_id'] == failures[0]['policy_id']
                            and row['strategy'] == strategy.name]
                        shared_hold = (shared_unavailable and 'shared_regime' in failures[0]['sources']
                            and len(raw) == 1 and 'shared_regime_unavailable' in raw[0]['evidence'].get('research_hold_reasons', []))
                    if len(failures)!=1 or (source not in failures[0]['sources'] and failures[0]['status']!='strategy_defect' and not shared_hold):
                        diagnose('source_health_conflict',source+':'+strategy.name+':'+horizon)
        from .runtime_recovery import validate_recovery_receipt
        recovery = None
        try:
            recovery = validate_recovery_receipt(state, generation, session, commit,
                expected_configuration=metadata.get('config_sha256'))
            if recovery is not None:
                if (not isinstance(metadata.get('config_sha256'), str)
                        or recovery['epoch_id'] != epoch):
                    raise ValueError('recovery configuration or epoch unbound')
                recovery['original_daily_outcome'] = latest['outcome'] if latest else 'missing'
                # Recovery has its own proof. Missing original worker evidence
                # remains visible, without being fabricated as a successful run.
                historical = {'daily_attempt_missing', 'manifest_session_history_missing'}
                recovery['original_attempt_diagnostics'] = [row for row in diagnostics if row['code'] in historical]
                diagnostics[:] = [row for row in diagnostics if row['code'] not in historical]
                report['recovery'] = recovery
        except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
            recovery = None
            diagnose('recovery_receipt_invalid')
        if latest and latest['wire'] and not (recovery is not None and latest['outcome'] == 'failed'):
            wire = latest['wire']
            try:
                refs = aggregate_source_health_failures(wire,session,require_coverage=True)
                if refs!=report['source_health_failures']:
                    diagnose('wire_health_conflict')
                issues = aggregate_candidate_input_issues(wire,session)
                if issues!=report['candidate_input_issues']:
                    diagnose('wire_candidate_issues_conflict')
                for name,row in wire.items():
                    horizon = name.split('_')[1]
                    expected_scope = source_scope_limits_from_health(
                        item for item in health if item['policy_id'] == 'foundation-' + horizon
                        or item['policy_id'].endswith(':health:' + horizon))
                    if canonical_source_scope_limits(row.get('source_scope_limits', {})) != expected_scope:
                        diagnose('wire_source_scope_conflict')
                    book=report['cohorts'][name]
                    if row['execution_valid']!=book['accounting_valid'] or row['staging_valid']!=book['staging_valid']:
                        diagnose('wire_ledger_conflict',name)
                    book['staging_valid'] = book['staging_valid'] and row['staging_valid']
                report['staging_valid'] = all(row['staging_valid'] for row in report['cohorts'].values())
            except (ValueError,TypeError,KeyError):
                diagnose('wire_coverage_invalid')
        report['evidence_complete'] = not diagnostics
        if recovery is not None and report['evidence_complete'] and report['accounting_valid'] and report['staging_complete']:
            report['outcome'] = ('clean' if report['input_coverage_valid'] and report['staging_valid'] else 'degraded')
        elif latest and latest['outcome']=='failed':
            report['outcome']='failed'
        elif report['evidence_complete'] and report['accounting_valid'] and latest:
            report['outcome']=latest['outcome']
        if operating_profile is not None and optional_failures:
            # Supported capability completion does not turn a genuine optional
            # acquisition failure into full coverage. Retain the worker wire and
            # attempt/history unchanged; this report projects the wider evidence.
            report['input_coverage_valid'] = False
            if report['outcome'] == 'clean':
                report['outcome'] = 'degraded'
        report['performance_claims_withheld'] = not (report['evidence_complete'] and report['accounting_valid'] and report['outcome'] in {'clean','degraded'})
        if (report['sources'].get('regulation_analysis_coverage') or {}).get('decision_evidence_complete') is False:
            report['performance_claims_withheld'] = True
        if operating_profile is not None:
            facts = _completion_facts(state, operating_profile, report, health, completion_source, diagnose)
            # Late integrity evidence must update every derived report flag;
            # preserve explicit failed-attempt precedence.
            report['evidence_complete'] = not diagnostics
            if diagnostics:
                if report['outcome'] != 'failed':
                    report['outcome'] = 'incomplete'
                report['performance_claims_withheld'] = True
            from .operating_contract import evaluate_completion
            report['completion'] = evaluate_completion(operating_profile, facts)
        diagnostics.sort(key=lambda row:(row['code'],row.get('scope','')))
        report['source_reliability'] = load_source_reliability(report, repo)
        return report



def _completion_facts(state, profile, report, health, source_envelope, diagnose, *, enrichment_observation=None):
    """Read immutable screening and native committee rows without store writes."""
    from .research_screening import _decode, _digest, VERSION, screening_codec_limits
    from .source_inputs import source_codec_limits
    facts = {'report': report, 'health': health, 'source_envelope': source_envelope or {}, 'committees': {}}
    sample = {}
    try:
        identity = source_envelope['identity']
        source_digest = hashlib.sha256(
            SourceInputStore.encode(source_envelope['payload'], **source_codec_limits(profile.config)).encode()
        ).hexdigest()
        binding = {'policy': 'dependency_isolation_v1', 'source_identity': identity,
            'epoch_id': report['epoch_id'], 'source_digest': source_digest,
            'horizon_policies': profile.horizon_policies, 'strategies': sorted(profile.strategies)}
        store = SourceInputStore(state / 'source_cache', accepted_dir=state / 'source_inputs/screening',
            **screening_codec_limits())
        checkpoint_identity = {**identity, 'purpose': VERSION, 'epoch_id': report['epoch_id'],
            'research_resilience_policy': 'dependency_isolation_v1',
            'screening_binding_sha256': _digest(store, binding)}
        checkpoint = store.load_frozen(checkpoint_identity)
        sample, _, _ = _decode(store, checkpoint, binding, date.fromisoformat(report['session']))
        facts.update(screening=checkpoint, screening_store=store)
        from .multi_strategy_engine import MultiStrategyEngine
        from .operating_contract import configure_strategies, healthy_supported_record
        from .research_resilience import validate_projection, validate_signal
        # These native validators consume configuration and accepted data only;
        # no engine constructor, registry, model or store mutation is involved.
        verifier = MultiStrategyEngine.__new__(MultiStrategyEngine)
        verifier.config, verifier.ar_config = profile.config, profile.config['autoresearch']
        verifier.paper_trade_strategies = configure_strategies(get_paper_trade_strategies(), verifier.ar_config, full_config=profile.config)
        strategy_sources = {strategy.name: strategy.data_sources for strategy in verifier.paper_trade_strategies}
        for signals, regime, records in sample.values():
            for record in records:
                if (record.strategy in profile.supported_strategies and record.status in _HEALTHY
                        and not healthy_supported_record(vars(record), strategy_sources[record.strategy])):
                    raise ValueError('supported health contradicts its native classification')
            validate_projection(verifier, source_envelope['payload'], regime)
            for signal in signals:
                if signal.get('strategy') not in profile.supported_strategies:
                    raise ValueError('unsupported signal in frozen scope')
                if not signal.get('journal_only'):
                    validate_signal(signal, regime, strategy_sources[signal['strategy']], codec_limits=store.codec_limits)
        # Reuse the canonical accepted scope decoders; configuration is the
        # frozen profile, rather than a policy asserted by its own payload.
        from .scoped_sources import source_scope_evidence
        errors, _, _ = source_scope_evidence(source_envelope['payload'], profile.config['autoresearch'],
            report['session'], now=source_envelope['acquired_at'])
        if errors:
            diagnose('supported_source_scope_invalid')
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        diagnose('accepted_screening_invalid')
    for name in profile.roster:
        try:
            db_path, unified = _cohort_db_path(state, name)
            with _database(db_path) as connection:
                stages = (
                    connection.execute('SELECT policy_id FROM staging_runs WHERE cohort_id=? AND session=?', (name, report['session'])).fetchall()
                    if unified
                    else connection.execute('SELECT policy_id FROM staging_runs WHERE session=?', (report['session'],)).fetchall()
                )
                if len(stages) != 1 or stages[0]['policy_id'] != profile.ledger_policy_id:
                    raise ValueError('staging policy differs from frozen profile')
                rows = (
                    connection.execute('SELECT * FROM committee_decisions WHERE cohort_id=? AND session=?', (name, report['session'])).fetchall()
                    if unified
                    else connection.execute('SELECT * FROM committee_decisions WHERE session=?', (report['session'],)).fetchall()
                )
                if len(rows) != 1 or rows[0]['epoch_id'] != report['epoch_id'] or rows[0]['policy_id'] != profile.ledger_policy_id:
                    raise ValueError('committee identity differs from frozen profile')
                raw = rows[0]['payload_json']
                document = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite JSON')))
                if (not isinstance(document, dict) or set(document) != {'status', 'recommendations', 'policy_decisions'}
                        or not isinstance(document['status'], dict) or not isinstance(document['recommendations'], list)
                        or not isinstance(document['policy_decisions'], list)
                        or raw != json.dumps(document, sort_keys=True, separators=(',', ':'), allow_nan=False)):
                    raise ValueError('committee document invalid')
                status = document['status']
                if (status.get('status') not in {'no_candidates','selected','abstained','failed','research_hold'}
                        or type(status.get('degraded')) is not bool
                        or any(type(status.get(key)) is not int or status[key] < 0
                            for key in ('input_count','eligible_count','selected_count'))
                        or not status['selected_count'] <= status['eligible_count'] <= status['input_count']
                        or type(status.get('model_coverage')) is not dict):
                    raise ValueError('committee status malformed')
                coverage = status['model_coverage']
                if not (coverage == {'complete': True} or set(coverage) == {'complete','reason'}
                        and coverage['complete'] is False and type(coverage['reason']) is str and coverage['reason'].strip()):
                    raise ValueError('committee model coverage malformed')
                if status['status'] in {'no_candidates','selected','abstained'} and status.get('reason', '') != '':
                    raise ValueError('successful committee has a failure reason')
                if status['status'] == 'failed' and (status['degraded'] is not True
                        or type(status.get('reason')) is not str or not status['reason'].strip()):
                    raise ValueError('committee failure lacks known operational evidence')
                if status['status'] == 'research_hold' and (status['degraded'] is not True
                        or not isinstance(status.get('research_hold_reasons'), list)
                        or not status['research_hold_reasons']
                        or any(reason not in {'shared_regime_unavailable','required_universe_unavailable'}
                            for reason in status['research_hold_reasons'])
                        or status.get('reason') not in status['research_hold_reasons']):
                    raise ValueError('committee research hold malformed')
                if (status['status'] != 'selected' and (status['selected_count'] != 0 or document['recommendations'])
                        or status['status'] == 'selected' and (not document['recommendations'] or not status['selected_count'])):
                    raise ValueError('committee selection contradicts status')
                from .research_resilience import validate_book_source_scope
                regime = sample[name.split('_')[1]][1]
                validate_book_source_scope(status, regime, codec_limits=store.codec_limits)
                accepted_pool = (
                    [s for h_signals, _, _ in sample.values() for s in h_signals]
                    if unified
                    else sample[name.split('_')[1]][0]
                )
                _validate_committee_content(connection, name, profile, report, document, accepted_pool)
                facts['committees'][name] = document
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
            diagnose('accepted_committee_invalid', name)
    return facts


def _accepted_enrichment_scope(state, profile, report, source_envelope, committees, *, observation=None):
    """Reconstruct optional scope from accepted inputs and all native book bindings.

    The source envelope supplies its original credential-sensitive identity.
    Neither current credentials nor a committee's claimed success can establish
    the accepted source, eligibility, enrichment, or publication clock.
    """
    from contextlib import ExitStack
    from types import SimpleNamespace
    from . import decision_clock as clock
    from .research_resilience import project_enrichment
    from .source_inputs import source_codec_limits
    from ..execution.session_activity import POLICY as ELIGIBILITY_POLICY, _validate_frozen

    session = date.fromisoformat(report['session'])
    epoch = report['epoch_id']
    config, ar = profile.config, profile.config['autoresearch']
    identity = source_envelope['identity']
    if (any(identity.get(key) != report[field] for key, field in (
            ('generation', 'generation_id'), ('session', 'session'), ('commit', 'generation_commit')))
            or tuple(sorted(committees)) != tuple(sorted(profile.roster))
            or len(profile.roster) != 16):
        raise ValueError('accepted enrichment identity or committee roster mismatch')
    store = SourceInputStore(state / 'source_cache', accepted_dir=state / 'source_inputs/decision_inputs',
        **source_codec_limits(config))
    source = source_envelope['payload']
    source_digest = hashlib.sha256(
        SourceInputStore.encode(source, **source_codec_limits(config)).encode()
    ).hexdigest()
    clock._require_research_source(store, source, source_digest)
    purpose = ('research-historical-decision-inputs-v1' if clock.policy(config) == clock.HISTORICAL
        else 'prospective-decision-inputs-v1')
    accepted = store.load_frozen({**identity, 'purpose': purpose})

    with ExitStack() as stack:
        cohorts, protected = [], set()
        for name in profile.roster:
            db_path, _unified = _cohort_db_path(state, name)
            connection = stack.enter_context(_database(db_path))
            ledger = PortfolioLedger.__new__(PortfolioLedger)
            ledger._connection, ledger.cohort_id = connection, name
            economic_context = ledger.session_execution_context(session)
            if economic_context is None or economic_context['epoch_id'] != epoch:
                raise ValueError('accepted enrichment lacks bound accounting context')
            market = json.loads(economic_context['economic_inputs_json'])['market']
            if tuple(market['required_tickers']) != economic_context['required_tickers']:
                raise ValueError('accepted enrichment protected accounting scope mismatch')
            protected.update(economic_context['required_tickers'])
            protected.update(row['symbol'] for row in market['benchmarks'])
            if (not ledger.staging_completed(session, epoch, profile.ledger_policy_id)
                    or ledger.committee_decision(session, epoch, profile.ledger_policy_id) != committees[name]):
                raise ValueError('accepted enrichment committee differs from native book')
            cohorts.append({'ledger': ledger, 'config': SimpleNamespace(name=name, horizon=name.split('_')[1]),
                'engine': SimpleNamespace(ar_config=ar)})

        eligibility = None
        if ar.get('new_entry_eligibility_policy') == ELIGIBILITY_POLICY:
            metrics = MetricStore.open_existing(_metrics_db_path(state))
            binding = metrics.read_candidate_signal_identity_binding(epoch, session)
            if binding is None or binding.epoch_id != epoch or binding.session != session:
                raise ValueError('accepted enrichment lacks candidate identity binding')
            candidates = {row['ticker'] for row in binding.identities} - protected
            eligibility_store = SourceInputStore(state / 'source_cache',
                accepted_dir=state / 'source_inputs/new_entry_eligibility', **store.codec_limits)
            eligibility = eligibility_store.load_frozen({**identity, 'policy': ELIGIBILITY_POLICY})
            if eligibility is not None:
                _validate_frozen(eligibility, session, candidates, sorted(protected),
                    source.get('equity_universe', {}).get('snapshot'))
        eligibility_digest = clock._decision_digest(store, eligibility)
        owner = SimpleNamespace(_base_config=config, _epoch_id=epoch, cohorts=cohorts)
        enrichment, context = clock._validate_research_inputs(owner, session, store, accepted,
            source, source_digest, eligibility_digest, completed=cohorts)
        for cohort in cohorts:
            staging = cohort['ledger'].read_policy_session_context(session)
            if (staging is None or staging['cohort_id'] != cohort['config'].name
                    or staging['epoch_id'] != epoch or staging['session'] != session
                    or staging['context'].get('decision_clock') != context):
                raise ValueError('accepted enrichment differs from staging decision clock')
        _, scope = project_enrichment(enrichment, codec_limits=store.codec_limits, capability_config=ar)
        if observation is not None:
            from .enrichment_monitor import assess_accepted_enrichment
            observation.update(assess_accepted_enrichment(enrichment, scope, capability_config=ar, codec_limits=store.codec_limits))
        return scope


def _validate_committee_content(connection, name, profile, report, document, accepted_signals):
    """Use native replay types/attribution and the book's accepted input IDs."""
    from dataclasses import asdict
    from ..execution.ids import stable_id
    from ..modules.base import OptionSpec
    from ..trading.portfolio_committee import TradeRecommendation, PortfolioCommittee
    from ..trading.portfolio_policy import PortfolioPolicyDecision
    status = document['status']
    authority = status['research_authority']
    identities = authority.get('eligible_signal_ids')
    receipts = authority.get('candidate_receipts')
    if (not isinstance(identities, list) or any(type(value) is not str for value in identities)
            or identities != sorted(set(identities)) or status['input_count'] != len(identities)
            or not isinstance(receipts, dict) or set(receipts) != set(identities)):
        raise ValueError('committee inputs contradict accepted authority')
    ledger = PortfolioLedger.__new__(PortfolioLedger)
    ledger._connection, ledger.cohort_id = connection, name
    session = date.fromisoformat(report['session'])
    manifest_row = ledger._read_policy_envelope(
        'policy_staging_audit_manifest',
        f"{session.isoformat()}:{report['epoch_id']}:{profile.ledger_policy_id}",
    )
    if manifest_row is not None and 'input_signal_ids' in json.loads(manifest_row['payload_json']):
        # Revalidate the exact completed native proof, never the committee's claims.
        manifest = ledger._decode_staging_manifest_row(manifest_row)
        expected_inputs = list(manifest['ingress_signal_ids'])
    else:
        # Historical manifests/callers have no exact membership proof. Retain the
        # old fail-closed global-input check rather than silently repairing them.
        expected_inputs = []
        for record in ledger.read_signals(start_session=session, end_session=session,
                epoch_id=report['epoch_id'], policy_id=profile.ledger_policy_id):
            provenance = ledger.read_signal_policy_provenance(record.signal_id)
            if provenance is None:
                raise ValueError('committee signal lacks native admission provenance')
            if provenance['order_eligible']:
                expected_inputs.append(record.signal_id)
    if identities != sorted(expected_inputs):
        raise ValueError('committee omits native admitted inputs')
    signals = []
    for identity in identities:
        context = connection.execute('SELECT payload_json,payload_hash FROM signal_candidate_contexts WHERE signal_id=?', (identity,)).fetchone()
        observation = ledger.signal_observation(identity)
        if (context is None or observation is None
                or context['payload_hash'] != stable_id('signal_candidate_context', context['payload_json'])):
            raise ValueError('committee signal context invalid')
        record, candidate, _ = observation
        signal = candidate.get('signal')
        if (record.epoch_id != report['epoch_id'] or record.reference_session.isoformat() != report['session']
                or record.policy_id != profile.ledger_policy_id or not isinstance(signal, dict)
                or any(signal.get(key) != getattr(record, key) for key in ('ticker','strategy','direction'))
                or receipts[identity] != signal.get('metadata', {}).get('research_admission')):
            raise ValueError('committee input differs from native signal')
        # Ledger JSON normalizes the typed screening codec (tuples/timestamps).
        # Compare that exact native storage representation, or use the existing
        # retained-observation decoder for a previously accepted late sample.
        encoded = json.dumps(signal, sort_keys=True, default=str)
        if not any(encoded == json.dumps(value, sort_keys=True, default=str) for value in accepted_signals):
            from .research_resilience import validate_retained_signal
            validate_retained_signal(signal, ledger, date.fromisoformat(report['session']),
                report['epoch_id'], profile.ledger_policy_id)
        signals.append({**signal, 'event_key':record.event_key, '_signal_id':identity})
    eligible = [signal for signal in signals if not signal.get('journal_only')
        and not (signal.get('metadata') or {}).get('non_actionable_reason')]
    if ((status['status'] in {'selected','abstained','no_candidates'} and status['eligible_count'] != len(eligible))
            or status['status'] == 'no_candidates' and status['eligible_count'] != 0):
        raise ValueError('committee eligible count differs from admitted inputs')
    recommendations = []
    for raw in document['recommendations']:
        values = dict(raw)
        if values.get('option_spec'):
            values['option_spec'] = OptionSpec(**values['option_spec'])
        recommendation = TradeRecommendation(**values)
        if (type(recommendation.position_size_pct) not in (int,float) or not 0 < recommendation.position_size_pct <= 1
                or type(recommendation.confidence) not in (int,float) or not 0 <= recommendation.confidence <= 1
                or type(recommendation.rationale) is not str):
            raise ValueError('committee recommendation values invalid')
        recommendations.append(recommendation)
    if (len(recommendations) != status['selected_count']
            or not PortfolioCommittee._recommendations_are_grounded(recommendations, eligible)):
        raise ValueError('committee recommendations lack native contributors')
    attributed = PortfolioCommittee._derive_attribution(recommendations, eligible)
    canonical = lambda value: json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)
    if canonical([asdict(row) for row in attributed]) != canonical(document['recommendations']):
        raise ValueError('committee recommendation attribution changed')
    selected = sorted({signal['_signal_id'] for signal in eligible for rec in recommendations
        if signal['ticker'] == rec.ticker and signal['direction'] == rec.direction
        and signal['strategy'] in rec.contributing_strategies})
    if status.get('selected_signal_ids') != selected:
        raise ValueError('committee selected IDs differ from native contributors')
    for raw in document['policy_decisions']:
        decision = PortfolioPolicyDecision(**raw)
        if (decision.decision not in {'accepted','trimmed','rejected'}
                or type(decision.requested_weight) not in (int,float)
                or type(decision.approved_weight) not in (int,float)
                or not 0 <= decision.approved_weight <= decision.requested_weight <= 1):
            raise ValueError('committee policy decision invalid')

def render_operational_report(report: dict) -> str:
    lines = [f"# EventEdge {report['generation_id']} — {report['session']}", '',
             f"Outcome: **{report['outcome']}**. Accounting valid: {str(report['accounting_valid']).lower()}; input coverage valid: {str(report['input_coverage_valid']).lower()}; staging complete: {str(report['staging_complete']).lower()}; staging valid: {str(report['staging_valid']).lower()}.", '',
             f"Exact-session attempts: {report['attempt_counts']['daily']} daily, {report['attempt_counts']['preflight']} preflight.", '',
             'Counts are per scenario book; books share signals and source observations.', '',
             '| Book | Accounting valid | Staging complete | Entry fills | Exit fills |',
             '| --- | --- | --- | ---: | ---: |']
    for name,row in sorted(report['cohorts'].items()):
        fills=row['fills']
        lines.append(f"| {name} | {str(row['accounting_valid']).lower()} | {str(row['staging_complete']).lower()} | {fills['entries'] if fills else 'unknown'} | {fills['exits'] if fills else 'unknown'} |")
    if report['attempts']:
        lines.extend(['','## Daily attempt evidence',''])
        for attempt in report['attempts']:
            if attempt['action']=='daily':
                lines.append(f"- {attempt['attempt_id']}: {attempt['outcome']}; {attempt['process_status']}; return code {attempt['process_return_code']}.")
    preflights = [row for row in report['attempts'] if row['action'] == 'preflight']
    if preflights:
        lines.extend(['', '## Preflight attempt evidence', '',
            'Preflight modes are independent; a successful governed price check does not establish source recovery.', ''])
        for attempt in preflights:
            label = _PREFLIGHT_LABELS.get(attempt.get('preflight_mode'), 'unknown mode')
            status = 'passed' if attempt.get('preflight_ok') is True else 'failed'
            lines.append(f"- {attempt['attempt_id']}: {label}; {status}; {attempt['process_status']}; return code {attempt['process_return_code']}.")
    if report.get('recovery'):
        recovery = report['recovery']
        lines.extend(['', 'Accepted local recovery completed at ' + recovery['completed_at']
            + '; original daily attempt outcome: ' + str(recovery['original_daily_outcome'])
            + '. Its original attempt and preflight evidence remain preserved.',
            'Recovery proof: `' + recovery['proof_sha256'] + '` (' + str(recovery['books_count']) + ' books).'])
    if report.get('completion'):
        completion = report['completion']
        lines.extend(['', 'Supported workflow complete: ' + str(completion['workflow_complete']).lower()
            + '; continuity eligible: ' + str(completion['continuity_eligible']).lower()
            + '; full research coverage: false (declared exclusions).'])
        if completion['reason_codes']:
            lines.append('Completion blockers: ' + ', '.join(completion['reason_codes']) + '.')
        for warning in completion.get('audit_warnings', []):
            lines.append('Congress audit warning: publisher snapshot generated at '
                + warning['publisher_generated_at'] + '; '
                + str(warning['house_missing_filings']) + ' House filings missing. '
                'The verified snapshot is within 48 hours at acquisition. Paper continuity permits '
                'this display-only gap; source coverage remains incomplete.')
    if report.get('disabled_strategies'):
        lines.extend(['', 'Policy exclusions: ' + ', '.join(f'{name} ({reason})' for name, reason in sorted(report['disabled_strategies'].items())) + '.'])
    if report['sources'].get('filing_comparison_scope'):
        scope = report['sources']['filing_comparison_scope']
        lines.extend(['', f"Rows permitted for current-only filing assessment: {scope['current_only_rows']} "
            f"({scope['current_only_absent_rows']} without an earlier same-form filing; "
            f"{scope['current_only_ambiguous_rows']} without a unique earlier comparator). "
            'The policy requires the full current filing and prohibits comparison claims; these counts do not establish model completion.'])
    if report['sources'].get('filing_attribution_scope'):
        scope = report['sources']['filing_attribution_scope']
        lines.extend(['', f"Filing security attribution: {scope['verified_target_rows']} verified target rows, "
            f"{scope['outside_rows']} outside the declared universe, {scope['unresolved_rows']} unresolved, "
            f"from {scope['total_rows']} retained discovery rows. "
            'Unresolved rows remain non-actionable. These counts establish neither full attribution nor model completion.'])
    if report['sources'].get('filing_acquisition_scope'):
        scope = report['sources']['filing_acquisition_scope']
        lines.extend(['', f"Original filing acquisition: {scope['completed_originals']} completed originals; "
            f"spool closed: {str(scope['spool_closed']).lower()}."])
    if report['sources'].get('filing_material_scope'):
        scope = report['sources']['filing_material_scope']
        lines.extend(['', f"Strict filing evidence complete: {str(scope['strict_complete']).lower()} "
            f"({scope['strict_failed_rows']} failed rows); scoped analysis complete: {str(scope['scoped_complete']).lower()} "
            f"({scope['scoped_failed_rows']} failed rows). "
            f"{scope['quarantined_rows']} quarantined rows across {len(scope['quarantined_accessions'])} approved filings "
            'remain non-actionable material gaps. Scoped completeness excludes these analyses and does not establish model completion.'])
        if scope.get('prospective_scope_broadened'):
            lines.extend(['', f"Prospective supported-document scope broadens the exact-ten exception: "
                f"{scope['admitted_rows']} admitted rows, {scope['deferred_rows']} deferred rows, "
                f"{scope['disposition_failed_rows']} source-disposition failures. "
                'Deferrals remain non-actionable. Parser attestations require independent frozen-original audit. '
                'This scoped verdict does not establish original full-analysis coverage.'])
        if scope['quarantines']:
            lines.append('')
        for quarantine in scope['quarantines']:
            gaps = ', '.join(quarantine['gap_codes'])
            lines.append(f"- {quarantine['accession']}: {gaps}.")
    if report['sources'].get('regulation_source_coverage'):
        scope = report['sources']['regulation_source_coverage']
        lines.extend(['', f"Regulatory acquisition complete: {str(scope['content_complete']).lower()}; "
            f"{scope['complete_rows']} of {scope['required_rows']} document occurrences acquired completely; "
            f"{scope['failed_rows']} failed occurrences."])
        if scope.get('retrieval_policy'):
            lines.append(f"Regulatory retrieval policy: {scope['retrieval_policy']}.")
        analysis = report['sources'].get('regulation_analysis_coverage')
        if analysis is not None:
            lines.append(f"Regulatory analysis complete across configured horizons: {str(analysis['analysis_complete']).lower()}. "
                'Completed no-directional-thesis assessments carry no order authority; missing content remains a failure.')
            if any('abstained_documents' in row for row in analysis['horizons']):
                lines.append('Recorded issuer/exposure or external-support abstentions: ' + str(sum(
                    row.get('abstained_documents', 0) for row in analysis['horizons'])) +
                    ' document-horizon assessments; decision evidence and listed supporting gaps remain unresolved, and order authority is none.')
    if report['performance_claims_withheld']:
        lines.extend(['','Performance claims withheld because completed, consistent evidence is unavailable.'])
    if report['candidate_input_issues']:
        lines.extend(['','## Candidate input issues',''])
        for row in report['candidate_input_issues']:
            lines.append(f"- {row['ticker']}: {row['dependency_kind']} / {row['reason_code']}; {len(row['affected_cohorts'])} affected books ({row['issue_id']}).")
    if report.get('source_scope_limits'):
        lines.extend(['', '## Source scope limitations', '',
                      'Input coverage validity applies to these declared scopes; it is not a marketwide coverage claim.'])
        for provider, scope in canonical_source_scope_limits(report['source_scope_limits']).items():
            if provider == 'usaspending':
                counts = scope['counts']
                lines.append(f"- USAspending: verified listed targets only; {counts['verified_listed_target']} verified listed, "
                    f"{counts['verified_no_listed_target']} proven no-listed, {counts['unresolved']} unresolved awards. "
                    'Unresolved awards remain nonactionable attribution gaps, not proven exclusions.')
            elif provider == 'congress':
                lines.append(f"- Congressional disclosures: display/audit only; trading signals and model context disabled. "
                    f"{scope['filing_counts']['house']} House and {scope['filing_counts']['senate']} Senate filings, "
                    f"{scope['printed_row_count']} printed transaction rows; audit status {scope['status']}. "
                    'Senate coverage, extraction accuracy and historical first availability remain unverified.')
            else:
                lines.append(f"- CourtListener: docket metadata only for declared issuer and case queries, not marketwide; "
                    f"{scope['issuer_count']} issuers, {scope['case_count']} cases, {scope['docket_count']} dockets; "
                    f"{scope['omitted_issuer_count']} omitted issuers. No full-document or marketwide analysis is implied.")
    for title,key in (('Source coverage failures','source_health_failures'),('Recovered acquisition','recovered'),('Unresolved acquisition','unresolved')):
        rows=report[key] if key=='source_health_failures' else report['sources'][key]
        if rows:
            lines.extend(['',f'## {title}',''])
            for row in rows:
                if key=='source_health_failures':
                    lines.append(f"- {row['policy_id']} / {row['strategy']}: {row['status']} ({', '.join(row['sources']) or 'no source identity'}).")
                else:
                    lines.append(f"- {row['provider']}: {row['reason_code']}; HTTP {row.get('http_status') or 'unknown'}; attempts {row.get('attempts','unknown')}.")
    if 'source_incidents' in report:
        policy = report['source_incidents']
        lines.extend(['', '## Source incident policy', '',
            f"Policy: {policy['policy']}; assessment: {policy['assessment_status']}; previous-session history: {policy['history_status']}.",
            'Incident status is operational evidence and does not change trading permissions.'])
        for incident in policy['open_incidents']:
            lines.append(f"- {incident['provider']}: **{incident['status']}**.")
        if policy['assessment_status'] == 'unavailable':
            lines.append('Source incident assessment unavailable; inspect the evidence diagnostics below.')
        elif not policy['open_incidents']:
            lines.append('No observed open source incidents; this does not establish unobserved source health.')
        for alert in policy['alerts']:
            lines.append(f"- Event {alert['kind']}: {alert['provider']} ({alert['alert_id']}).")
        lines.append('Repair procedure and monitor contract: docs/source-incident-policy.md.')
    if 'source_reliability' in report:
        learning = report['source_reliability']
        lines.extend(['', '## Source reliability learning', '',
            f"Assessment: {learning['assessment_status']}; reviewed catalog: {learning['catalog_status']}; review required: {learning['review_required']}.",
            'Known patterns are previous learning, not proof of the current cause. This metadata does not change source health, retries or trading permissions.'])
        for row in learning['patterns']:
            lines.append(f"- {row['source']} / {row['operation']}: {row['reason_code']}; current attribution {row['current_attribution']}; known pattern {row['known_pattern']} ({row['pattern_id']}).")
        if learning['selection_truncated']:
            lines.append('Learning pattern bound exceeded; inspect retained canonical source evidence.')
    if report['preflight_incidents']:
        lines.extend(['','## Preflight incidents',''])
        for item in report['preflight_incidents']:
            label = _PREFLIGHT_LABELS.get(item.get('preflight_mode'), 'unknown mode')
            lines.append(f"- {item['attempt_id']}: {label}; return code {item['process_return_code']}; " + '; '.join(f"{row['source']} {row['reason_code']} HTTP {row['http_status'] or 'unknown'}" for row in item['failures']))
            for failure in item.get('runtime_failures', []):
                lines.append(f"  Runtime phase {failure['phase']}: {failure['reason_code']}. Inspect the preserved attempt and frozen configuration before rerunning this mode.")
    if report.get('preflight_runtime_observations'):
        lines.extend(['', '## Preflight runtime observations', '',
            'Runtime observations are separate from provider repair incidents. Recovery requires a later successful equivalent screen; original failures remain preserved.', ''])
        for row in report['preflight_runtime_observations']:
            lines.append(f"- {row['attempt_id']}: {row['status']}; generation={row['generation_id']}; session={row['session']}; mode={row['preflight_mode']}; phase={row['phase']}; reason={row['reason_code']}; commit={row['generation_commit']}; configuration={row['configuration']}; resolved_by={row['resolved_by'] or 'none'}.")
    if report['diagnostics']:
        lines.extend(['','## Evidence diagnostics',''])
        lines.extend(f"- {row['code']}" + (f" ({row['scope']})" if 'scope' in row else '') for row in report['diagnostics'])
    return '\n'.join(lines)+'\n'


def _publish(path: Path, content: str) -> None:
    fd, name = tempfile.mkstemp(prefix='.operational-report-',dir=path.parent)
    try:
        with os.fdopen(fd,'w') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name,path)
    finally:
        Path(name).unlink(missing_ok=True)


def write_operational_report(report: dict, output_dir: str | Path) -> dict[str, Path]:
    from .source_incidents import load_source_incidents
    output=Path(output_dir)
    output.mkdir(parents=True,exist_ok=True)
    report['source_incidents'] = load_source_incidents(report, output)
    basename=f"{report['session']}-{report['generation_id']}-operational-report"
    paths={'json':output/(basename+'.json'),'markdown':output/(basename+'.md')}
    _publish(paths['json'],json.dumps(report,sort_keys=True,indent=2,allow_nan=False)+'\n')
    _publish(paths['markdown'],render_operational_report(report))
    return paths
