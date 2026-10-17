"""Completion mutates retained canonical evidence across all sixteen books."""
from copy import deepcopy
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
import hashlib
import json
import sqlite3

import pytest

from test_operational_report import native_evidence, _attempt, _report, _insert, SESSION, GENERATION, COMMIT, EPOCH
from test_stabilization_contract import PROFILE, profile
from tradingagents.strategies.orchestration.source_inputs import SourceInputStore, configuration_fingerprint, source_configuration_fingerprint, source_codec_limits


def _payload(contract, tmp_path, state, failed=()):
    from tradingagents.strategies.orchestration import filing_research_policy as filing, source_lifecycle_policy as lifecycle
    from tradingagents.strategies.orchestration.operating_contract import configure_strategies
    from tradingagents.strategies.orchestration.congress_policy import declaration as congress
    from tradingagents.strategies.modules import get_paper_trade_strategies
    from tradingagents.strategies.data_sources import congress_disclosure_audit as audit
    from tradingagents.strategies.data_sources.courtlistener_scope import declared_queries, digest, evidence_digest, CONTENT_KIND
    from tradingagents.strategies.data_sources.evidence import CoverageRecords
    from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine
    from tradingagents.strategies.orchestration.litigation_targets import build_litigation_targets
    from tradingagents.strategies.state.portfolio_ledger import PortfolioLedger
    from tradingagents.strategies.data_sources.equity_universe import normalize_assets
    from test_source_capabilities import fred_payload, yahoo_engine
    from test_congress_disclosure_audit import synthetic_raw, REVISION
    strategies = configure_strategies(get_paper_trade_strategies(), contract.config['autoresearch'])
    data = {source: {} for s in strategies for source in s.data_sources if source != 'regulations'}
    data.update(filing.declaration(contract.config['autoresearch']))
    data.update(lifecycle.declaration(contract.config['autoresearch'], strategies))
    data.update(congress(contract.config['autoresearch']))
    data['edgar'] = filing.sentinel()
    from tradingagents.strategies.orchestration.source_inputs import public_source_configuration_declaration
    data.update(public_source_configuration_declaration(contract.config))
    data['fred'] = fred_payload(tmp_path / 'fred', failed)
    data['yfinance'] = yahoo_engine(tmp_path / 'yahoo')._fetch_yfinance_data('2026-09-01', SESSION)
    now = datetime(2026, 10, 6, 23, tzinfo=timezone.utc)
    # Synthetic full-schema congressional raw artifacts, reconstructed by the
    # same native decoder that reports validate. No network or personal records.
    raw = synthetic_raw()
    manifest = json.loads(raw['snapshot.json'])
    manifest.update(generatedAt=now.isoformat(), lastSourceCheckAt=now.isoformat())
    raw['snapshot.json'] = json.dumps(manifest).encode()
    prefix = f'{audit.HF}/datasets/{audit.DATASET}/resolve/{REVISION}'
    artifacts = [audit._record('revision_metadata', audit.REVISION_URL,
        json.dumps({'sha': REVISION, 'id': audit.DATASET}).encode()),
        audit._record('manifest', prefix+'/snapshot.json', raw['snapshot.json']),
        audit._record('house_index', 'https://disclosures-clerk.house.gov/public_disc/financial-pdfs/2026FD.ZIP', raw['house-2026FD.ZIP'])]
    for table, filename in [('political_filings','filings-2026.parquet'), ('political_trades','trades-2026.parquet')]:
        artifacts.append(audit._record('parquet', prefix+f'/data/{table}/2026-00000-of-00001.parquet', raw[filename]))
    data['congress'] = audit._assemble(artifacts, revision=REVISION, start='2026-09-06', end=SESSION, acquired_at=now.isoformat())
    companies = {'0': {'cik_str': 1, 'ticker': 'AAPL', 'title': 'Apple Inc.'}}
    ledgers = [PortfolioLedger(state/name/'portfolio.db', name, Decimal('5000')) for name in contract.roster]
    owner = SimpleNamespace(_epoch_id=EPOCH, _policy_id_for_horizon=lambda h: contract.horizon_policies[h],
        cohorts=[{'config': SimpleNamespace(name=name, horizon=name.split('_')[1]), 'ledger': ledger}
            for name, ledger in zip(contract.roster, ledgers)])
    settings = dict(contract.config['autoresearch']['courtlistener_focused'], policy='focused_litigation_v1')
    try:
        targets = build_litigation_targets(owner, date.fromisoformat(SESSION), cutoff=now,
            company_map=companies, settings=settings)
    finally:
        for ledger in ledgers:
            ledger.close()
    data['_courtlistener_targets'] = {**targets, 'company_map': companies}
    scope = targets['scope']
    queries = [{**q, 'status': 'complete', 'returned': 0, 'docket_ids': [], 'coverage': {
        'complete': True, 'pages': 1, 'mode': 'exhaustive_window', 'query': q['query'],
        'date_filed_after': '2026-09-22', 'date_filed_before': SESSION, 'termination': 'null_next', 'has_next': False}}
        for q in declared_queries(scope)]
    court = {'courtlistener_scope_policy': 'focused_litigation_v1', 'dockets': [], 'coverage': {
        'policy':'focused_litigation_v1', 'mode':'declared_focused_scope', 'content_kind':CONTENT_KIND,
        'scope':scope, 'scope_sha256':digest(scope), 'date_filed_after':'2026-09-22', 'date_filed_before':SESSION,
        'complete':True, 'budget_exhausted':False, 'request_cap_exhausted':False, 'returned':0,
        'acquisition_started_at':now.isoformat(), 'acquired_at':now.isoformat(), 'requests':len(queries),
        'request_cap':10, 'response_bytes':0, 'absolute_deadline':100., 'subbudget_seconds':100., 'queries':queries}}
    court['coverage']['evidence_sha256'] = evidence_digest(court)
    data['courtlistener'] = court
    owner = MultiStrategyEngine.__new__(MultiStrategyEngine)
    owner.ar_config = contract.config['autoresearch']
    owner.registry = SimpleNamespace(get=lambda _: SimpleNamespace(get_recent_large_contracts=lambda **_: CoverageRecords([], coverage={
        'mode':'exhaustive_window','complete':True,'returned':0,'pages':1,'date_from':'2026-09-06', 'date_to':SESSION,
        'date_type':'new_awards_only','amount_basis':'cumulative_award_obligations', 'issuer_attribution':{'complete':True,'unresolved':0}})))
    data['usaspending'] = owner._fetch_usaspending_data(SESSION)
    data['equity_universe'] = {'snapshot': normalize_assets([{'class':'us_equity','symbol':'AAPL','exchange':'NASDAQ',
        'status':'active','tradable':True}], observed_at=now, response_sha256='a'*64)}
    return data, now


@pytest.fixture
def completed_supported_session(native_evidence, tmp_path, request):
    from tradingagents.strategies.orchestration import filing_research_policy as filing, source_lifecycle_policy as lifecycle
    from tradingagents.strategies.orchestration.operating_contract import configure_strategies
    from tradingagents.strategies.metrics.health import classify_strategy_run
    from tradingagents.strategies.modules import get_paper_trade_strategies
    from tradingagents.strategies.orchestration.congress_policy import audit_scope
    from tradingagents.strategies.orchestration.scoped_sources import source_scope_evidence
    from tradingagents.strategies.orchestration.research_resilience import project_sources
    from tradingagents.strategies.orchestration.research_screening import _receipt, _digest, VERSION
    repo, state, wire = native_evidence
    contract = profile()
    payload, now = _payload(contract, tmp_path, state, getattr(request, 'param', ()))
    ar = contract.config['autoresearch']
    strategies = configure_strategies(get_paper_trade_strategies(), ar)
    slot = configuration_fingerprint({'generation':GENERATION,'session':SESSION})+'.json'
    (state/'source_inputs'/slot).unlink()
    identity = {'generation':GENERATION,'session':SESSION,'commit':COMMIT,
        'configuration':source_configuration_fingerprint(contract.config)}
    source_store = SourceInputStore(tmp_path/'cache', accepted_dir=state/'source_inputs', **source_codec_limits(contract.config))
    source_store.freeze(identity, payload, acquired_at=now)
    source = SourceInputStore.decode((state/'source_inputs'/slot).read_text(), **source_store.codec_limits)
    vol = state/'source_inputs/staging_volatility'/slot
    document = SourceInputStore.decode(vol.read_text())
    document['identity']['configuration'] = identity['configuration']
    vol.write_text(SourceInputStore.encode(document))
    manifest_path = state.parent/'manifest.json'
    manifest = json.loads(manifest_path.read_text())
    manifest['generations'][0].update(config_file=str(PROFILE),config_sha256=contract.profile_sha256)
    manifest_path.write_text(json.dumps(manifest))
    scope = lifecycle.declared_scope(payload, ar, strategies)
    filing_scope = filing.declared_scope(payload, config=ar)
    _, limits, _ = source_scope_evidence(payload, ar, SESSION, now=now)
    congress_scope = audit_scope(payload, ar, SESSION, now=now)
    all_limits = {**limits, 'congress': congress_scope}
    store = SourceInputStore(tmp_path/'cache', accepted_dir=state/'source_inputs/screening', **source_codec_limits(contract.config))
    binding = {'policy':'dependency_isolation_v1','source_identity':identity,'epoch_id':EPOCH,
        'source_digest':source['digest'],'horizon_policies':contract.horizon_policies,'strategies':list(contract.strategies)}
    checkpoint = {'version':VERSION, 'binding':binding, 'horizons':{},'model_coverage':{'complete':True}}
    with sqlite3.connect(state/'metrics_v2.sqlite3') as connection:
        connection.execute('DELETE FROM strategy_health')
        for horizon in contract.horizon_policies:
            health = []
            for strategy in strategies:
                record = classify_strategy_run(epoch_id=EPOCH,session=date.fromisoformat(SESSION),
                    policy_id=contract.horizon_policies[horizon],strategy=strategy.name,data_sources=strategy.data_sources,
                    candidates=[],provider_errors={},exception=None)
                row = asdict(record)
                if strategy.name in contract.declared_exclusions:
                    row.update(status='disabled_by_policy', signal_count=0)
                    row['evidence'].update(reason=contract.declared_exclusions[strategy.name],candidate_count=0)
                    row['evidence'].update(lifecycle.health_evidence(scope,strategy.name))
                    row['evidence'].update(filing.health_evidence(filing_scope,strategy.name))
                    if strategy.name == 'congressional_trades':
                        row['evidence'].update(disclosure_policy='display_audit_only_v1', source_scope_limits={'congress':congress_scope})
                else:
                    named = {'litigation':'courtlistener','govt_contracts':'usaspending'}.get(strategy.name)
                    if named:
                        row['evidence']['source_scope_limits'] = {named:limits[named]}
                health.append(row)
                dbrow = {**row,'session':SESSION}
                _insert(connection,'strategy_health',health_id=row['health_id'],epoch_id=EPOCH,session=SESSION,payload_json=json.dumps(dbrow))
            names = set(payload) | {'fred','yfinance'} | {source for s in strategies for source in s.data_sources}
            selected, research_scope = project_sources(payload,names,codec_limits=source_codec_limits(contract.config),capability_policy=ar['source_capability_policy'])
            from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine
            from tradingagents.strategies.orchestration.research_resilience import digest
            engine = MultiStrategyEngine.__new__(MultiStrategyEngine)
            engine.config, engine.ar_config = contract.config, ar
            selected = {key:value for key,value in selected.items() if key not in
                ('congress','_congress_disclosure_policy','_source_lifecycle_policy','_filing_research_policy')}
            regime = engine._build_regime_model(selected)
            regime.update(research_hold_reasons=[], research_source_scope=research_scope,
                filing_research_isolation=None, research_projection_sha256=digest(selected,codec_limits=store.codec_limits))
            checkpoint['horizons'][horizon] = {'signals':[], 'regime':regime, 'health':health}
    checkpoint['receipt'] = _receipt(store,checkpoint)
    store.freeze({**identity,'purpose':VERSION,'epoch_id':EPOCH,'research_resilience_policy':'dependency_isolation_v1',
        'screening_binding_sha256':_digest(store,binding)}, checkpoint, acquired_at=now)
    for name in contract.roster:
        with sqlite3.connect(state/name/'portfolio.db') as connection:
            connection.execute('UPDATE staging_runs SET policy_id=?',(contract.ledger_policy_id,))
            from tradingagents.strategies.orchestration.research_resilience import digest
            regime = checkpoint['horizons'][name.split('_')[1]]['regime']
            authority = {'version':'research-book-authority-v1','policy':'dependency_isolation_v1',
                'source_bundle_sha256':regime['research_source_scope']['source_bundle_sha256'],
                'source_scope_sha256':digest(regime['research_source_scope'],codec_limits=store.codec_limits),
                'filing_isolation_sha256':digest(None,codec_limits=store.codec_limits),
                'projection_sha256':regime['research_projection_sha256'],
                'shared_context_sha256':digest(regime,codec_limits=store.codec_limits), 'entry_hold':False,'hold_reasons':[],
                'eligible_signal_ids':[],'candidate_receipts':{},'retained_origins':{}}
            document = {'status':{'status':'no_candidates','model_coverage':{'complete':True},
                'degraded':False,'input_count':0,'eligible_count':0,'selected_count':0,'selected_signal_ids':[],
                'research_authority':authority},
                'recommendations':[],'policy_decisions':[]}
            connection.execute('INSERT INTO committee_decisions VALUES (?,?,?,?)',(SESSION,EPOCH,contract.ledger_policy_id,
                json.dumps(document,sort_keys=True,separators=(',',':'))))
    for row in wire.values():
        row['source_scope_limits'] = all_limits
    _attempt(repo,wire)
    return repo,state,contract


def test_complete_supported_no_event_has_reduced_coverage(completed_supported_session):
    repo, state, contract = completed_supported_session
    report = _report(repo)
    assert report['completion']['workflow_complete'] is True, report
    assert report['completion']['continuity_eligible'] is True
    assert report['completion']['research_coverage_complete'] is False
    assert len(report['completion']['declared_exclusions']) == 6


@pytest.mark.parametrize('completed_supported_session', [('PAYEMS',)], indirect=True)
def test_optional_payroll_failure_completes_with_visible_degradation(completed_supported_session):
    repo, state, contract = completed_supported_session
    report = _report(repo)
    assert report['sources']['unresolved']
    assert report['outcome'] == 'degraded'
    assert report['input_coverage_valid'] is False
    assert report['completion']['workflow_complete'] is True, report
    assert report['completion']['continuity_eligible'] is False
    assert report['completion']['research_coverage_complete'] is False


@pytest.mark.parametrize('fault', ['missing_health','duplicate_health','unknown_exclusion','bad_filing_digest',
    'bad_retirement_digest','bad_public_configuration','bad_public_profile','http_instead_of_sentinel','required_source_failed','missing_book','invalid_staging',
    'nan_equity','screening_model_timeout','committee_model_timeout','shared_regime_hold','profile_hash_mismatch'])
def test_canonical_mutations_cannot_complete(completed_supported_session, fault):
    repo, state, contract = completed_supported_session
    source_path = next(p for p in (state/'source_inputs').glob('*.json'))
    screen_path = next((state/'source_inputs/screening').glob('*.json'))
    if fault in {'missing_health','duplicate_health','unknown_exclusion'}:
        with sqlite3.connect(state/'metrics_v2.sqlite3') as connection:
            key, raw = connection.execute('SELECT health_id,payload_json FROM strategy_health LIMIT 1').fetchone()
            if fault == 'missing_health':
                connection.execute('DELETE FROM strategy_health WHERE health_id=?',(key,))
            else:
                row = json.loads(raw)
                if fault == 'unknown_exclusion':
                    row['status'] = 'disabled_by_policy'
                    row['evidence'].update(reason='unreviewed_disabled_reason',candidate_count=0)
                    connection.execute('UPDATE strategy_health SET payload_json=? WHERE health_id=?',(json.dumps(row),key))
                else:
                    row['health_id'] += '-duplicate'
                    _insert(connection,'strategy_health',health_id=row['health_id'],epoch_id=EPOCH,session=SESSION,payload_json=json.dumps(row))
    elif fault in {'bad_filing_digest','bad_retirement_digest','bad_public_configuration','bad_public_profile','http_instead_of_sentinel','required_source_failed'}:
        document = SourceInputStore.decode(source_path.read_text())
        payload = document['payload']
        if fault == 'bad_filing_digest':
            payload['_filing_research_policy']['sha256'] = '0'*64
        elif fault == 'bad_retirement_digest':
            payload['_source_lifecycle_policy']['sha256'] = '0'*64
        elif fault == 'bad_public_configuration':
            payload['_source_public_configuration']['configuration_sha256'] = '0'*64
        elif fault == 'bad_public_profile':
            payload['_source_public_configuration']['profile_sha256'] = '0'*64
        elif fault == 'http_instead_of_sentinel':
            payload['edgar'] = {'error':'HTTP 403'}
        else:
            payload['finnhub']['error'] = 'provider failed'
        document['digest'] = hashlib.sha256(SourceInputStore.encode(payload).encode()).hexdigest()
        source_path.write_text(SourceInputStore.encode(document))
    elif fault in {'missing_book','invalid_staging','nan_equity','committee_model_timeout'}:
        with sqlite3.connect(state/contract.roster[0]/'portfolio.db') as connection:
            if fault == 'missing_book':
                connection.execute('DELETE FROM session_runs')
            elif fault == 'invalid_staging':
                connection.execute("UPDATE staging_runs SET completed_at='' ")
            elif fault == 'nan_equity':
                connection.execute("UPDATE account_snapshots SET net_equity='NaN'")
            else:
                raw = connection.execute('SELECT payload_json FROM committee_decisions').fetchone()[0]
                document = json.loads(raw)
                document['status'].update(status='failed',degraded=True,reason='model_deadline_exhausted',model_coverage={'complete':False,'reason':'model_deadline_exhausted'})
                connection.execute('UPDATE committee_decisions SET payload_json=?',(
                    json.dumps(document,sort_keys=True,separators=(',',':')),))
    elif fault == 'profile_hash_mismatch':
        manifest_path = state.parent/'manifest.json'
        document = json.loads(manifest_path.read_text())
        document['generations'][0]['config_sha256'] = '0'*64
        manifest_path.write_text(json.dumps(document))
    else:
        from tradingagents.strategies.orchestration.research_screening import _receipt
        store = SourceInputStore(state/'source_cache',accepted_dir=state/'source_inputs/screening',**source_codec_limits(contract.config))
        envelope = SourceInputStore.decode(screen_path.read_text(),**store.codec_limits)
        document = envelope['payload']
        if fault == 'screening_model_timeout':
            document['model_coverage'] = {'complete':False,'reason':'model_deadline_exhausted'}
        else:
            document['horizons']['30d']['regime']['research_hold_reasons'] = ['shared_regime_unavailable']
        document['receipt'] = _receipt(store,document)
        envelope['digest'] = hashlib.sha256(SourceInputStore.encode(document,**store.codec_limits).encode()).hexdigest()
        screen_path.write_text(SourceInputStore.encode(envelope,**store.codec_limits))
    report = _report(repo)
    assert report.get('completion',{}).get('workflow_complete') is not True, report
    assert report['evidence_complete'] is False or report['completion']['reason_codes']


def test_historical_completion_does_not_depend_on_current_credentials(completed_supported_session, monkeypatch):
    repo, state, contract = completed_supported_session
    assert _report(repo)['completion']['workflow_complete'] is True
    for key in ('FRED_API_KEY','FINNHUB_API_KEY','ALPACA_API_KEY','ALPACA_SECRET_KEY'):
        monkeypatch.setenv(key, 'a-later-credential-not-the-original-session')
    assert _report(repo)['completion']['workflow_complete'] is True


def test_unknown_committee_status_is_integrity_failure(completed_supported_session):
    repo, state, contract = completed_supported_session
    with sqlite3.connect(state/contract.roster[0]/'portfolio.db') as connection:
        document = json.loads(connection.execute('SELECT payload_json FROM committee_decisions').fetchone()[0])
        document['status']['status'] = 'made_up_status'
        connection.execute('UPDATE committee_decisions SET payload_json=?',(
            json.dumps(document,sort_keys=True,separators=(',',':')),))
    report = _report(repo)
    assert report['evidence_complete'] is False
    assert {'code':'accepted_committee_invalid','scope':contract.roster[0]} in report['diagnostics']


def test_unknown_committee_source_receipt_cannot_complete(completed_supported_session):
    repo, state, contract = completed_supported_session
    with sqlite3.connect(state/contract.roster[0]/'portfolio.db') as connection:
        document = json.loads(connection.execute('SELECT payload_json FROM committee_decisions').fetchone()[0])
        document['status']['research_authority'] = {'shared_context_sha256':'0'*64}
        connection.execute('UPDATE committee_decisions SET payload_json=?',(
            json.dumps(document,sort_keys=True,separators=(',',':')),))
    assert _report(repo)['completion']['workflow_complete'] is False


@pytest.mark.parametrize('fault', ['malformed_recommendation','invented_selection_counts','false_no_candidates'])
def test_committee_content_must_match_native_input_truth(completed_supported_session, fault):
    repo, state, contract = completed_supported_session
    with sqlite3.connect(state/contract.roster[0]/'portfolio.db') as connection:
        document = json.loads(connection.execute('SELECT payload_json FROM committee_decisions').fetchone()[0])
        if fault == 'false_no_candidates':
            document['status'].update(input_count=1,eligible_count=1)
        else:
            document['status'].update(status='selected',input_count=100,eligible_count=100,selected_count=99)
            document['recommendations'] = ['invalid-native-recommendation'] if fault == 'malformed_recommendation' else [{
                'ticker':'AAPL','direction':'long','position_size_pct':.1,'confidence':.5,'rationale':'unsupported',
                'contributing_strategies':['earnings_call']}]
        connection.execute('UPDATE committee_decisions SET payload_json=?',(
            json.dumps(document,sort_keys=True,separators=(',',':')),))
    report = _report(repo)
    assert report['evidence_complete'] is False
    assert report['completion']['workflow_complete'] is False


def test_late_integrity_diagnostic_withholds_performance_and_completion(completed_supported_session):
    repo, state, contract = completed_supported_session
    with sqlite3.connect(state/contract.roster[0]/'portfolio.db') as connection:
        document = json.loads(connection.execute('SELECT payload_json FROM committee_decisions').fetchone()[0])
        document['status']['status'] = 'made_up_status'
        connection.execute('UPDATE committee_decisions SET payload_json=?',(
            json.dumps(document,sort_keys=True,separators=(',',':')),))
    report = _report(repo)
    assert report['performance_claims_withheld'] is True
    assert report['outcome'] == 'incomplete'
    assert report['completion']['workflow_complete'] is False


def test_supported_no_event_cannot_contradict_native_health_evidence(completed_supported_session):
    repo, state, contract = completed_supported_session
    from tradingagents.strategies.orchestration.research_screening import _receipt
    store = SourceInputStore(state/'source_cache',accepted_dir=state/'source_inputs/screening',**source_codec_limits(contract.config))
    path = next((state/'source_inputs/screening').glob('*.json'))
    envelope = SourceInputStore.decode(path.read_text(),**store.codec_limits)
    row = next(row for row in envelope['payload']['horizons']['30d']['health'] if row['strategy']=='earnings_call')
    row['evidence'].update(screen_completed=False,provider_errors={'unknown_source':'unknown_failure'})
    with sqlite3.connect(state/'metrics_v2.sqlite3') as connection:
        connection.execute('UPDATE strategy_health SET payload_json=? WHERE health_id=?',(
            json.dumps({**row,'session':SESSION}),row['health_id']))
    envelope['payload']['receipt'] = _receipt(store,envelope['payload'])
    envelope['digest'] = hashlib.sha256(SourceInputStore.encode(envelope['payload'],**store.codec_limits).encode()).hexdigest()
    path.write_text(SourceInputStore.encode(envelope,**store.codec_limits))
    report = _report(repo)
    assert report['evidence_complete'] is False
    assert report['completion']['workflow_complete'] is False


def test_committee_cannot_omit_native_admitted_signal(tmp_path):
    from types import SimpleNamespace
    from test_portfolio_policy_ledger import _ledger, _signal, _record_signal_policy, FRIDAY
    from tradingagents.strategies.orchestration.operational_report import _validate_committee_content
    ledger = _ledger(tmp_path)
    try:
        record = _signal()
        _record_signal_policy(ledger,record)
        document = {'status':{'status':'no_candidates','input_count':0,'eligible_count':0,'selected_count':0,
            'selected_signal_ids':[],'research_authority':{'eligible_signal_ids':[],'candidate_receipts':{}}},
            'recommendations':[],'policy_decisions':[]}
        with pytest.raises(ValueError,match='omits native admitted inputs'):
            _validate_committee_content(ledger._connection,ledger.cohort_id,
                SimpleNamespace(ledger_policy_id=record.policy_id),
                {'epoch_id':record.epoch_id,'session':FRIDAY.isoformat()},document,[])
    finally:
        ledger.close()
