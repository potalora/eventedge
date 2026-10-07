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
from tradingagents.strategies.orchestration.source_coverage import aggregate_source_health_failures
from tradingagents.strategies.orchestration.source_inputs import (
    CONTRACT_VERSION, MAX_BYTES, SourceInputStore, configuration_fingerprint,
)
from tradingagents.strategies.state.portfolio_ledger import LedgerConflictError, PortfolioLedger

EXPECTED_COHORTS = tuple(sorted(
    f'horizon_{horizon}_size_{size}'
    for horizon in ('30d', '3m', '6m', '1y') for size in ('5k', '10k', '50k', '100k')
))
_SAFE_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,191}')
_PROVIDERS = frozenset({'edgar','fred','finnhub','congress','regulations','courtlistener',
                        'noaa','usda','drought_monitor','usaspending','cftc','yfinance','openbb'})
_HEALTHY = frozenset({'signals', 'legitimate_no_event'})
_FAILURES = frozenset({'data_failure', 'strategy_defect'})


def _safe_id(value: Any) -> bool:
    return isinstance(value, str) and _SAFE_ID.fullmatch(value) is not None


def _load_json(path: Path) -> Any:
    if path.stat().st_size > 32 * 1024 * 1024:
        raise ValueError('document too large')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate JSON field')
            result[key] = value
        return result
    return json.loads(path.read_text(), object_pairs_hook=unique,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite JSON')))


@contextmanager
def _database(path: Path):
    # mode=ro prevents file creation; no PortfolioLedger/MetricStore constructor.
    connection = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute('PRAGMA query_only=ON')
        connection.execute('BEGIN')
        yield connection
    finally:
        connection.close()


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
                entry['preflight_ok'] = item['result'].get('success')
                entry['screen_failure_count'] = item['result'].get('screen_failure_count')
                entry['failures'] = _preflight_failures(item)
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


def _metrics(state: Path, generation: str, session: str, commit: str, diagnose) -> tuple[str | None, list[dict]]:
    path = state / 'metrics_v2.sqlite3'
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
                        value.get('status') not in _HEALTHY | _FAILURES or
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
    path = state / name / 'portfolio.db'
    if not path.exists():
        diagnose('ledger_missing', name)
        return result
    try:
        with _database(path) as connection:
            identity = connection.execute("SELECT metadata_value FROM schema_metadata WHERE metadata_key='cohort_id'").fetchone()
            if identity is None or identity[0] != name:
                diagnose('ledger_cohort_conflict', name)
            runs = connection.execute('SELECT * FROM session_runs WHERE session=?', (session,)).fetchall()
            snapshots = connection.execute('SELECT * FROM account_snapshots WHERE session=?', (session,)).fetchall()
            contexts = connection.execute('SELECT * FROM session_execution_contexts WHERE session=?', (session,)).fetchall()
            valid = (identity is not None and identity[0] == name and epoch is not None
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
                except (LedgerConflictError,ValueError,TypeError,KeyError,AttributeError):
                    diagnose('staging_volatility_context_invalid',name)
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
        with _database(state/'metrics_v2.sqlite3') as connection:
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


def _health_coverage(health: list[dict], epoch: str | None, session: str, diagnose) -> list[dict]:
    strategies = {strategy.name for strategy in get_paper_trade_strategies()}
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
            if row is not None and row['status'] in _HEALTHY:
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
    if path.stat().st_size > MAX_BYTES:
        raise ValueError('source byte bound')
    envelope = SourceInputStore.decode(path.read_text())
    identity = envelope['identity']
    keys={'generation','session','commit','configuration'} | ({'purpose'} if purpose else set())
    if (set(envelope) != {'version','identity','acquired_at','payload','digest'} or
            envelope['version']!=CONTRACT_VERSION or
            set(identity)!=keys or identity.get('purpose')!=purpose or
            identity['generation']!=generation or identity['session']!=session or identity['commit']!=commit or
            not isinstance(identity['configuration'],str) or not identity['configuration'] or
            envelope['digest']!=hashlib.sha256(SourceInputStore.encode(envelope['payload']).encode()).hexdigest() or
            not isinstance(envelope['acquired_at'],datetime) or envelope['acquired_at'].tzinfo is None or
            envelope['acquired_at']>datetime.now(timezone.utc) or not isinstance(envelope['payload'],dict)):
        raise ValueError('source identity/content conflict')
    return envelope


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


def _sources(state: Path, generation: str, session: str, commit: str, diagnose) -> dict:
    result = {'recovered':[], 'unresolved':[], 'observations':{}}
    slot = configuration_fingerprint({'generation':generation,'session':session})
    path = state / 'source_inputs' / (slot+'.json')
    if not path.exists():
        diagnose('accepted_sources_missing')
        return result
    try:
        envelope = _accepted_envelope(path,generation,session,commit)
        identity = envelope['identity']
        result['configuration'] = identity['configuration']
        result['acquired_at'] = envelope['acquired_at'].isoformat()
        required={source for strategy in get_paper_trade_strategies() for source in strategy.data_sources if source!='openbb'}
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
            if '_coverage' in payload and coverage_status not in {'success','success_empty','complete','partial','failed'}:
                diagnose('source_coverage_invalid',source)
                result['observations'][source]='invalid'
                result['unresolved'].append({'provider':source,'reason_code':'invalid_response','http_status':None})
            else:
                result['observations'][source]=coverage_status or ('failed' if payload.get('error') not in (None,'') else 'success')
            for event in payload.get('_request_diagnostics',[]):
                if not isinstance(event,dict) or not all(_safe_id(event.get(key)) for key in ('provider','operation')) or event.get('reason_code') is not None and not _safe_id(event['reason_code']) or type(event.get('recovered')) is not bool or type(event.get('attempts')) is not int or not 0<=event['attempts']<=5:
                    raise ValueError('source diagnostics invalid')
                status = event.get('http_status')
                if status is not None and (type(status) is not int or not 100<=status<=599):
                    raise ValueError('source status invalid')
                safe = {key:event.get(key) for key in ('provider','operation','reason_code','http_status','attempts','recovered')}
                safe['reason_code'] = event.get('reason_code') or 'success'
                if event['recovered']:
                    result['recovered'].append(safe)
                elif safe['reason_code']!='success':
                    result['unresolved'].append(safe)
            if payload.get('error') not in (None,'') or coverage_status in {'partial','failed'}:
                if coverage_status in {'success','success_empty','complete'}:
                    diagnose('source_coverage_invalid',source)
                result['unresolved'].append({'provider':source,'reason_code':coverage.get('reason_code','provider_error') if isinstance(coverage,dict) and _safe_id(coverage.get('reason_code','provider_error')) else 'provider_error',
                                             'http_status':coverage.get('http_status') if isinstance(coverage,dict) and type(coverage.get('http_status')) is int else None})
        for kind in ('recovered','unresolved'):
            result[kind] = sorted({json.dumps(row,sort_keys=True):row for row in result[kind]}.values(),key=lambda row:json.dumps(row,sort_keys=True))
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        diagnose('accepted_sources_invalid')
    return result


def build_operational_report(repo_root: str | Path, generation: str, session: str, *, snapshot_guaranteed: bool = False) -> dict:
    """Read one stable generation/session; caller may attest an immutable snapshot.

    Normal runtime callers acquire the canonical shared lock. The explicit
    snapshot guarantee is for archived copies/offline fixtures, never an implicit
    fallback when a live lock is busy or invalid.
    """
    if not _safe_id(generation) or date.fromisoformat(session).isoformat()!=session:
        raise ValueError('invalid report identity')
    repo = Path(repo_root).resolve()
    guard = nullcontext() if snapshot_guaranteed else runtime_lock(canonical_runtime_lock_path(repo),exclusive=False)
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
            report['generation_commit'] = commit
        except (OSError, ValueError, TypeError, KeyError):
            diagnose('manifest_invalid')
            return report
        attempts,latest = _attempts(repo,generation,session,commit,diagnose)
        _manifest_history(metadata,attempts,latest,repo,session,diagnose)
        report['attempt_counts'] = {action:sum(row['action']==action for row in attempts) for action in ('daily','preflight')}
        report['attempts'] = [{key:value for key,value in row.items() if key not in {'wire','result','failures'}} for row in attempts]
        report['preflight_incidents'] = [
            {'attempt_id':row['attempt_id'],'process_return_code':row['process_return_code'],
             'screen_failure_count':row['screen_failure_count'],'failures':row['failures']}
            for row in attempts if row['action']=='preflight' and (row['preflight_ok'] is not True or row['failures'])
        ]
        epoch,health = _metrics(state,generation,session,commit,diagnose)
        report['epoch_id'] = epoch
        report['cohorts'] = {name:_book(state,name,generation,session,epoch,diagnose) for name in EXPECTED_COHORTS}
        report['accounting_valid'] = all(row['accounting_valid'] for row in report['cohorts'].values())
        report['staging_complete'] = all(row['staging_complete'] for row in report['cohorts'].values())
        report['candidate_input_issues'] = _candidate_issues(state,epoch,session,diagnose)
        for row in report['cohorts'].values():
            # The pipeline shares its quarantine set across all staging calls.
            # Completion records the phase, validity records accepted inputs.
            row['staging_valid'] = row['staging_complete'] and not report['candidate_input_issues']
        report['staging_valid'] = all(row['staging_valid'] for row in report['cohorts'].values())
        report['source_health_failures'] = _health_coverage(health,epoch,session,diagnose)
        report['input_coverage_valid'] = epoch is not None and not report['source_health_failures']
        report['sources'] = _sources(state,generation,session,commit,diagnose)
        report['volatility'] = _volatility(state,generation,session,commit,report['sources'],report['candidate_input_issues'],report['cohorts'],diagnose)
        failed_sources = {item['provider'] for item in report['sources']['unresolved']}
        required_failures = {source for strategy in get_paper_trade_strategies() for source in strategy.data_sources if source!='openbb'} & failed_sources
        if required_failures:
            report['input_coverage_valid'] = False
        for item in report['sources']['unresolved']:
            source=item['provider']
            for strategy in get_paper_trade_strategies():
                if source=='openbb' or source not in strategy.data_sources:
                    continue
                for horizon in ('30d','3m','6m','1y'):
                    failures=[failure for failure in report['source_health_failures'] if failure['strategy']==strategy.name and
                        (failure['policy_id']=='foundation-'+horizon or failure['policy_id'].endswith(':health:'+horizon))]
                    if len(failures)!=1 or (source not in failures[0]['sources'] and failures[0]['status']!='strategy_defect'):
                        diagnose('source_health_conflict',source+':'+strategy.name+':'+horizon)
        if latest and latest['wire']:
            wire = latest['wire']
            try:
                refs = aggregate_source_health_failures(wire,session,require_coverage=True)
                if refs!=report['source_health_failures']:
                    diagnose('wire_health_conflict')
                issues = aggregate_candidate_input_issues(wire,session)
                if issues!=report['candidate_input_issues']:
                    diagnose('wire_candidate_issues_conflict')
                for name,row in wire.items():
                    book=report['cohorts'][name]
                    if row['execution_valid']!=book['accounting_valid'] or row['staging_valid']!=book['staging_valid']:
                        diagnose('wire_ledger_conflict',name)
                    book['staging_valid'] = book['staging_valid'] and row['staging_valid']
                report['staging_valid'] = all(row['staging_valid'] for row in report['cohorts'].values())
            except (ValueError,TypeError,KeyError):
                diagnose('wire_coverage_invalid')
        report['evidence_complete'] = not diagnostics
        if latest and latest['outcome']=='failed':
            report['outcome']='failed'
        elif report['evidence_complete'] and report['accounting_valid'] and latest:
            report['outcome']=latest['outcome']
        report['performance_claims_withheld'] = not (report['evidence_complete'] and report['accounting_valid'] and report['outcome'] in {'clean','degraded'})
        diagnostics.sort(key=lambda row:(row['code'],row.get('scope','')))
        return report


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
    if report['performance_claims_withheld']:
        lines.extend(['','Performance claims withheld because completed, consistent evidence is unavailable.'])
    if report['candidate_input_issues']:
        lines.extend(['','## Candidate input issues',''])
        for row in report['candidate_input_issues']:
            lines.append(f"- {row['ticker']}: {row['dependency_kind']} / {row['reason_code']}; {len(row['affected_cohorts'])} affected books ({row['issue_id']}).")
    for title,key in (('Source coverage failures','source_health_failures'),('Recovered acquisition','recovered'),('Unresolved acquisition','unresolved')):
        rows=report[key] if key=='source_health_failures' else report['sources'][key]
        if rows:
            lines.extend(['',f'## {title}',''])
            for row in rows:
                if key=='source_health_failures':
                    lines.append(f"- {row['policy_id']} / {row['strategy']}: {row['status']} ({', '.join(row['sources']) or 'no source identity'}).")
                else:
                    lines.append(f"- {row['provider']}: {row['reason_code']}; HTTP {row.get('http_status') or 'unknown'}; attempts {row.get('attempts','unknown')}.")
    if report['preflight_incidents']:
        lines.extend(['','## Preflight incidents',''])
        for item in report['preflight_incidents']:
            lines.append(f"- {item['attempt_id']}: return code {item['process_return_code']}; " + '; '.join(f"{row['source']} {row['reason_code']} HTTP {row['http_status'] or 'unknown'}" for row in item['failures']))
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
    output=Path(output_dir)
    output.mkdir(parents=True,exist_ok=True)
    basename=f"{report['session']}-{report['generation_id']}-operational-report"
    paths={'json':output/(basename+'.json'),'markdown':output/(basename+'.md')}
    _publish(paths['json'],json.dumps(report,sort_keys=True,indent=2,allow_nan=False)+'\n')
    _publish(paths['markdown'],render_operational_report(report))
    return paths
