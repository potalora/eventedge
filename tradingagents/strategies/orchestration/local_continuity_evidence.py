"""Read-only local continuity evidence; no production gate or runtime constructor.

Replay/cleanup receipts attest independently supervised work. The replay anchor
is retained separately on disk and binds the exact supplied receipt. This
collector verifies artifact contracts, not the honesty of an external supervisor.
SQLite query-only reads can maintain transient WAL/SHM coordination sidecars;
authoritative JSON documents and database economic content are never written.
"""
from __future__ import annotations

from datetime import date, datetime
from dataclasses import asdict
import json
import hashlib
import math
from pathlib import Path
import re
import sqlite3

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.strategies.modules import get_paper_trade_strategies
from tradingagents.strategies.data_sources.state_economics_policy import configure_strategies
from . import operational_report as report_io
from .generation_config import apply_generation_config
from .local_continuity import REQUIRED_CHECKS, classify_session
from .release_readiness import _accounting_and_staging, _session_epoch
from .research_screening import VERSION, _decode, _digest
from .research_resilience import POLICY
from .source_inputs import CONTRACT_VERSION, SourceInputStore, configuration_fingerprint, source_codec_limits
from .trading_calendar import session_close, session_open, next_session
from .runtime_lock import canonical_runtime_lock_path, runtime_lock

_HORIZONS = ('30d', '3m', '6m', '1y')
_IDENTITY = ('session', 'generation_id', 'generation_commit', 'profile_sha256', 'epoch_id')
_HASHES = ('native_before', 'native_after', 'source_before', 'source_after', 'ledger_before', 'ledger_after')
_ERRORS = (OSError, ValueError, TypeError, KeyError, AttributeError, sqlite3.Error, RecursionError)


def _require(condition):
    if not condition:
        raise ValueError('local_continuity_evidence_invalid')


def _sha(value):
    return type(value) is str and re.fullmatch('[0-9a-f]{64}', value) is not None


def _receipts(repo, identity, replay, cleanup):
    common = set(_IDENTITY)
    cleanup_ok = (type(cleanup) is dict
        and set(cleanup) == common | {'version', 'owned_workers_reaped', 'inputs_unchanged'}
        and all(cleanup.get(k) == v for k, v in identity.items())
        and cleanup['version'] == 'local-continuity-cleanup-v1'
        and cleanup['owned_workers_reaped'] is True and cleanup['inputs_unchanged'] is True)
    replay_ok = False
    try:
        _require(type(replay) is dict and set(replay) == common | set(_HASHES)
            | {'version', 'zero_calls', 'replayed_cohorts', 'anchor_sha256'})
        _require(all(replay.get(k) == v for k, v in identity.items())
            and replay['version'] == 'local-continuity-replay-v1' and replay['zero_calls'] is True
            and replay['replayed_cohorts'] == sorted(report_io.EXPECTED_COHORTS)
            and all(_sha(replay[k]) for k in (*_HASHES, 'anchor_sha256'))
            and all(replay[k+'_before'] == replay[k+'_after'] for k in ('native', 'source', 'ledger')))
        anchor = repo / 'data/local-continuity/sessions' / identity['session'] / 'replay-anchor.json'
        _require(not anchor.is_symlink() and anchor.stat().st_size <= 16384
                 and hashlib.sha256(anchor.read_bytes()).hexdigest() == replay['anchor_sha256'])
        _require(report_io._load_json(anchor) == {k:v for k,v in replay.items() if k != 'anchor_sha256'})
        replay_ok = True
    except _ERRORS:
        pass
    return replay_ok, cleanup_ok


def _native(provenance, request=None, *, role=None):
    """Native success has returned identity/response plus plan billing evidence."""
    _require(type(provenance) is dict)
    model = provenance.get('configured_model')
    expected = 'gpt-6-luna' if role == 'bounded' else 'gpt-6.1-sol'
    _require(model == expected and provenance.get('returned_model') == model
        and provenance.get('reasoning_effort') == 'high'
        and provenance.get('billing_mode') == 'chatgpt_plan'
        and provenance.get('effective_output_limit') is None
        and type(provenance.get('response_id')) is str and bool(provenance['response_id']))
    _require(type(request) is dict and set(request) == {'version','model','role','effort',
        'max_tokens','temperature','system_sha256','system_bytes','prompt_sha256','prompt_bytes','request_sha256'})
    _require(request['version']=='research-model-request-v1' and request['model']==model
        and request['role']==role and request['effort']=='high'
        and type(request['max_tokens']) is int and request['max_tokens']>0
        and type(request['temperature']) in (int,float) and math.isfinite(request['temperature'])
        and all(_sha(request[k]) for k in ('system_sha256','prompt_sha256','request_sha256'))
        and all(type(request[k]) is int and request[k]>=0 for k in ('system_bytes','prompt_bytes')))
    return model


def _screening(state, source, epoch, config, strategies, wire, session, start, finish):
    slot = configuration_fingerprint({'generation':source['identity']['generation'], 'session':session})
    path = state / 'source_inputs/screening' / (slot+'.json')
    limits = source_codec_limits(config)
    store = SourceInputStore(state/'unused-read-only-cache', **limits)
    _require(not path.is_symlink() and path.stat().st_size <= limits.get('max_bytes', report_io.MAX_BYTES))
    envelope = SourceInputStore.decode(path.read_text(), **limits)
    policy = config['autoresearch']['paper_ledger']['policy_id']
    binding = {'policy':POLICY,'source_identity':source['identity'],'epoch_id':epoch,
        'source_digest':source['digest'], 'horizon_policies':{h:policy+':health:'+h for h in _HORIZONS},
        'strategies':sorted(strategies)}
    identity = {**source['identity'],'purpose':VERSION,'epoch_id':epoch,
        'research_resilience_policy':POLICY,'screening_binding_sha256':_digest(store,binding)}
    _require(set(envelope)=={'version','identity','acquired_at','payload','digest'}
        and envelope['version']==CONTRACT_VERSION and envelope['identity']==identity
        and envelope['digest']==hashlib.sha256(SourceInputStore.encode(envelope['payload'],**limits).encode()).hexdigest()
        and isinstance(envelope['acquired_at'],datetime) and envelope['acquired_at'].tzinfo is not None
        and start<=source['acquired_at']<=envelope['acquired_at']<=finish)
    sample, coverage, receipt = _decode(store,envelope['payload'],binding,date.fromisoformat(session))
    _require(coverage == {'complete':True})
    _require(all(row.get('research_screening_receipt')==receipt for row in wire.values()))
    with report_io._database(state/'metrics_v2.sqlite3') as db:
        durable = [json.loads(row[0]) for row in db.execute(
            'SELECT payload_json FROM strategy_health WHERE session=?',(session,))]
    for row in durable:
        row['session']=date.fromisoformat(row['session'])
    frozen = [asdict(row) for _,_,health in sample.values() for row in health]
    _require(len(durable)==len(frozen) and _digest(store,sorted(durable,key=lambda x:x['health_id']))
             ==_digest(store,sorted(frozen,key=lambda x:x['health_id'])))
    return sample


def _collect(repo, generation, session, expected_commit, expected_profile, replay, cleanup):
    checks = {key:False for key in (*REQUIRED_CHECKS, 'no_model_work_required')}
    row = {'session':session,'generation_id':generation,'generation_commit':expected_commit,
        'profile_sha256':expected_profile,'epoch_id':'','attempts':0,'checks':checks,
        'native_models':[],'source_observations':{},'approved_exclusions':[],
        'unexpected_failures':[],'research_holds':[], 'fill_lifecycle_observed':False,
        'carried_positions_observed':False,'evidence_diagnostics':[]}
    def diagnostic(code):
        row['evidence_diagnostics'].append(code)
    try:
        manifest = report_io._load_json(repo/'data/generations/manifest.json')
        matches=[item for item in manifest['generations'] if item['gen_id']==generation]
        _require(len(matches)==1)
        metadata=matches[0]
        state=Path(metadata['state_dir'])
        state=state if state.is_absolute() else repo/state
        _require(metadata['git_commit']==expected_commit and metadata['config_sha256']==expected_profile
            and Path(metadata['config_file']).resolve()==(state/'generation-config.json').resolve())
        config=apply_generation_config(DEFAULT_CONFIG,metadata['config_file'],expected_profile)
        ar=config['autoresearch']
        _require(ar.get('research_resilience_policy')==POLICY and ar.get('llm_billing_mode')=='chatgpt_plan'
            and ar.get('autoresearch_model')=='gpt-6-luna' and ar.get('llm_effort')=='high'
            and ar.get('thesis_model')=='gpt-6.1-sol' and ar.get('thesis_effort')=='high'
            and ar.get('paper_trade',{}).get('portfolio_committee_model')=='gpt-6.1-sol'
            and ar.get('paper_trade',{}).get('portfolio_committee_effort')=='high'
            and ar.get('paper_trade',{}).get('portfolio_committee_enabled') is True)
        report=report_io.build_operational_report(repo,generation,session,snapshot_guaranteed=True)
        epoch=_session_epoch(state,session,generation,expected_commit)
        _require(epoch is not None and report['epoch_id']==epoch)
        row['epoch_id']=epoch
        checks['report']=(report['evidence_complete'] is True and report['generation_commit']==expected_commit
            and set(report['cohorts'])==set(report_io.EXPECTED_COHORTS))
        accounting,staging=_accounting_and_staging(state,session,epoch,generation,ar['paper_ledger']['policy_id'])
        checks['accounting']=accounting and report['accounting_valid'] is True
        checks['staging']=staging and report['staging_valid'] is True
        attempts=report['attempts']
        row['attempts']=len(attempts)  # A same-session preflight also consumes the live-attempt bound.
        _require(len(attempts)==1 and attempts[0]['action']=='daily')
        history=[item for item in metadata['run_history'] if item.get('action')=='daily' and item.get('date')==session]
        _require(len(history)==1)
        attempt=report_io._load_json(Path(attempts[0]['evidence_path']))
        _require(attempt['generation_id']==generation and attempt['generation_commit']==expected_commit
            and attempt['requested_session']==session and attempt['process_status']=='completed'
            and attempt['process_return_code'] in (0,2) and attempt['result']['outcome'] in ('clean','degraded'))
        wire=report_io._wire(attempt['stdout'])
        start,finish=(datetime.fromisoformat(attempt[key]) for key in ('started_at','finished_at'))
        day=date.fromisoformat(session)
        checks['timeliness']=(start.tzinfo is not None and finish.tzinfo is not None
            and session_close(day)<=start<=finish<session_open(next_session(day)))
        slot=configuration_fingerprint({'generation':generation,'session':session})
        source=report_io._accepted_envelope(state/'source_inputs'/(slot+'.json'),generation,session,expected_commit)
        strategies={strategy.name:strategy for strategy in configure_strategies(get_paper_trade_strategies(),ar)}
        from .filing_research_policy import configured as filing_disabled
        excluded=filing_disabled(ar)
        edgar=source['payload'].get('edgar',{})
        excluded=excluded and edgar.get('error')=='filing_research_disabled' and edgar.get('coverage')=={
            'complete':False,'reason':'filing_research_disabled','filing_research_policy':'disabled_v1'}
        if excluded:
            row['approved_exclusions'].append({'provider':'edgar','reason':'filing_research_disabled','policy':'disabled_v1'})
        observations=report['sources'].get('observations',{})
        row['source_observations']=observations
        required={provider for strategy in strategies.values() for provider in strategy.data_sources if provider!='openbb'}
        failures=[provider for provider in required if observations.get(provider) not in ('success','success_empty','complete')
                  and not (excluded and provider=='edgar')]
        failures += [item['provider'] for item in report['sources']['unresolved'] if not(excluded and item['provider']=='edgar')]
        row['unexpected_failures']=sorted(set(failures))
        checks['sources']=not failures
        sample=_screening(state,source,epoch,config,strategies,wire,session,start,finish)
        healthy=True; models=set(); signals=[]; retained_metadata=[]; no_events=True
        for horizon,(screened,regime,health) in sample.items():
            holds=regime.get('research_hold_reasons',[])
            _require(type(holds) is list)
            row['research_holds'].extend(holds)
            for record in health:
                _require(record.evidence.get('data_sources')==sorted(strategies[record.strategy].data_sources))
                retained_metadata.extend(record.evidence.get('state_economic_events', []))
                row['research_holds'].extend(record.evidence.get('research_hold_reasons', []))
                if record.status=='legitimate_no_event' and (record.signal_count!=0
                        or record.evidence.get('candidate_count')!=0
                        or record.evidence.get('screen_completed') is not True):
                    healthy=False
                    row['unexpected_failures'].append('incomplete_screen:'+record.strategy)
                if record.status=='signals':
                    no_events=False
                if record.status in ('signals','legitimate_no_event') or report_io._declared_exclusion(
                        {'strategy':record.strategy,'status':record.status,'signal_count':record.signal_count,'evidence':record.evidence},ar.get('disabled_strategies',{})):
                    continue
                errors=record.evidence.get('provider_errors',{})
                approved=(excluded and record.status=='data_failure' and record.signal_count==0
                    and set(errors)=={'edgar'} and errors['edgar']=='filing_research_disabled'
                    and 'edgar' in strategies[record.strategy].data_sources)
                if not approved:
                    healthy=False;row['unexpected_failures'].append('strategy:'+record.strategy)
            _require(all(signal.get('strategy') in strategies for signal in screened))
            signals.extend(screened)
        checks['screening']=healthy and not row['research_holds']
        for metadata in [signal.get('metadata',{}) for signal in signals] + retained_metadata:
            _require(metadata.get('analysis_status')=='validated' and not metadata.get('analysis_failure_reason')
                and not metadata.get('non_actionable_reason'))
            outcome=metadata['research_model_outcome']
            _require(type(outcome) is dict and set(outcome)=={'version','model_request','response_received',
                'response_sha256','response_bytes','response_encoding','analysis_status','analysis_failure_reason','model_provenance'}
                and outcome['version']=='research-model-outcome-v1' and outcome['analysis_status']=='validated'
                and outcome['analysis_failure_reason'] is None and outcome['response_received'] is True
                and _sha(outcome['response_sha256']) and type(outcome['response_bytes']) is int
                and outcome['response_bytes']>0 and outcome['response_encoding']=='utf-8')
            role=outcome['model_request']['role']
            _require(role in ('bounded','thesis'))
            models.add(_native(outcome['model_provenance'],outcome['model_request'],role=role))
        checks['model_outcomes']=True
        committee=True;committee_calls=0
        for name,result in wire.items():
            status=result.get('committee_decision_status',{})
            _require(type(status) is dict)
            valid=(status.get('mode')=='model' and status.get('status') in ('selected','abstained','no_candidates')
                and status.get('degraded') is False and not status.get('research_hold_reasons')
                and not status.get('enrichment_failures') and not status.get('reason'))
            committee=committee and valid
            if not valid:
                row['research_holds'].append('committee:'+name)
            with report_io._database(state/name/'portfolio.db') as db:
                decisions=db.execute('SELECT epoch_id,policy_id,payload_json FROM committee_decisions WHERE session=?',(session,)).fetchall()
                _require(len(decisions)==1 and decisions[0]['epoch_id']==epoch
                    and decisions[0]['policy_id']==ar['paper_ledger']['policy_id']
                    and json.loads(decisions[0]['payload_json'])['status']==status)
                if db.execute('SELECT 1 FROM lots WHERE opened_session<? AND CAST(open_qty AS REAL)>0 LIMIT 1',(session,)).fetchone():
                    row['carried_positions_observed']=True
            if status.get('status') in ('selected','abstained'):
                models.add(_native(status.get('model_provenance'),status.get('model_request'),role='committee'))
                committee_calls+=1
            elif status.get('status')=='no_candidates':
                _require(status.get('eligible_count')==0 and status.get('selected_count')==0)
            fills=report['cohorts'][name].get('fills') or {}
            if fills.get('total',0)>0:
                row['fill_lifecycle_observed']=True
        checks['committee']=committee
        checks['no_model_work_required']=not signals and not retained_metadata and committee_calls==0 and healthy and no_events
        row['native_models']=sorted(models)
    except _ERRORS:
        diagnostic('accepted_artifact_validation_failed')
        checks['model_outcomes']=False
    identity={key:row[key] for key in _IDENTITY}
    checks['replay'],checks['cleanup']=_receipts(repo,identity,replay,cleanup)
    result=classify_session(row,expected_commit=expected_commit,expected_profile=expected_profile)
    if row['evidence_diagnostics']:
        result['qualifies']=False
        result['reasons']=sorted(set(result['reasons'])|{'accepted_artifact_validation_failed'})
        result['status']='failed'
    return result


def collect_session(repo_root, generation_id, session, *, expected_commit, expected_profile,
                    replay_receipt=None, cleanup_receipt=None):
    """Collect one ordinary daily attempt under the existing shared runtime lock.

    Receipt schemas are exact (see _receipts). The separate replay-anchor.json
    contains the replay receipt without its anchor_sha256 field, whose value is
    the exact anchor file SHA256. Source/ledger/native digests are independently
    produced by the supervisor, and must agree before and after all-book replay.
    """
    _require(report_io._safe_id(generation_id) and date.fromisoformat(session).isoformat()==session
        and report_io._safe_id(expected_commit) and _sha(expected_profile))
    repo=Path(repo_root).resolve()
    lock=canonical_runtime_lock_path(repo)
    _require(lock.is_file())  # Collection must not create even a lock artifact.
    with runtime_lock(lock,exclusive=False):
        return _collect(repo,generation_id,session,expected_commit,expected_profile,replay_receipt,cleanup_receipt)
