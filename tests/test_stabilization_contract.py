"""Frozen reduced scope stays explicit and cannot admit failed model work."""
from copy import deepcopy
from pathlib import Path
import hashlib
import json

import pytest

from test_operational_report import native_evidence

PROFILE = Path(__file__).parents[1] / 'config/profiles/stabilization-v1.json'


def profile():
    from tradingagents.strategies.orchestration.operating_contract import profile_contract
    body = PROFILE.read_bytes()
    return profile_contract(json.loads(body), profile_sha256=hashlib.sha256(body).hexdigest())


def test_frozen_profile_has_exact_supported_scope():
    contract = profile()
    assert len(contract.roster) == 16
    assert len(contract.strategies) == 12
    assert len(contract.declared_exclusions) == 6
    assert len(contract.supported_strategies) == 6
    assert len(set(contract.horizon_policies.values())) == 4
    assert contract.config['decision_shadow']['enabled'] is False
    assert contract.config['autoresearch']['courtlistener_scope_policy'] == 'focused_litigation_v1'
    assert contract.config['autoresearch']['noaa_delivery_policy'] == 'ghcnd_yearly_v1'


@pytest.mark.parametrize('change', [
    lambda a: a['disabled_strategies'].update(earnings_call='anything'),
    lambda a: a['disabled_strategies'].update(insider_activity='arbitrary_denial'),
    lambda a: a.update(bounded_document_registry=[{}]),
    lambda a: a.update(regulation_evidence_policy='complete_document_text_v1'),
    lambda a: a.update(filing_research_policy=None),
    lambda a: a.update(courtlistener_scope_policy=None),
    lambda a: a.update(noaa_delivery_policy=None),
    lambda a: a.update(noaa_delivery_policy='access'),
])
def test_profile_rejects_scope_drift(change):
    from tradingagents.strategies.orchestration.operating_contract import profile_contract
    config = json.loads(PROFILE.read_text())
    change(config['autoresearch'])
    with pytest.raises(ValueError, match='stabilization_profile'):
        profile_contract(config, profile_sha256='a' * 64)


def test_filing_exclusion_requires_exact_payload_and_config_binding():
    from tradingagents.strategies.orchestration.filing_research_policy import (
        declaration, declared_scope, sentinel, validate_health, health_evidence)
    ar = profile().config['autoresearch']
    data = {**declaration(ar), 'edgar': sentinel()}
    scope = declared_scope(data, config=ar)
    records = [{'policy_id': 'policy', 'strategy': name, 'status': 'disabled_by_policy',
        'signal_count': 0, 'evidence': {'reason': reason, 'candidate_count': 0,
        **health_evidence(scope, name)}} for name, reason in scope['disabled_strategies'].items()]
    validate_health(scope, records, {'policy'})
    for change in (lambda d: d['edgar'].update(error='HTTP 403'),
                   lambda d: d['edgar'].update(events=[]),
                   lambda d: d['_filing_research_policy'].update(sha256='0'*64)):
        bad = deepcopy(data)
        change(bad)
        with pytest.raises(ValueError, match='filing_research_policy'):
            declared_scope(bad, config=ar)
    records[0]['evidence']['filing_research_sha256'] = '0'*64
    with pytest.raises(ValueError, match='filing_research_policy'):
        validate_health(scope, records, {'policy'})


def test_completion_cannot_trust_incoming_completion_flags():
    from tradingagents.strategies.orchestration.operating_contract import evaluate_completion
    result = evaluate_completion(profile(), {'workflow_complete': True,
        'supported_profile_complete': True, 'continuity_eligible': True})
    assert result['workflow_complete'] is False
    assert result['continuity_eligible'] is False
    assert result['research_coverage_complete'] is False
    assert result['reason_codes']


def test_preflight_declares_scope_and_skips_disabled_screens(tmp_path):
    from types import SimpleNamespace
    from tradingagents.strategies.modules import get_paper_trade_strategies
    from tradingagents.strategies.orchestration import filing_research_policy as filing
    from tradingagents.strategies.orchestration import source_lifecycle_policy as lifecycle
    from tradingagents.strategies.orchestration.congress_policy import declaration as congress
    from tradingagents.strategies.orchestration.preflight import _run_screen_preflight
    contract = profile()
    strategies = get_paper_trade_strategies()
    from tradingagents.strategies.orchestration.operating_contract import configure_strategies
    strategies = configure_strategies(strategies, contract.config['autoresearch'])
    for strategy in strategies:
        def screen(data, session, params, name=strategy.name):
            assert name not in contract.declared_exclusions, 'excluded strategy was screened'
            return []
        strategy.screen = screen
    ar = contract.config['autoresearch']
    payload = {source: {} for strategy in strategies for source in strategy.data_sources}
    payload.pop('regulations', None)
    payload.update(filing.declaration(ar))
    payload.update(lifecycle.declaration(ar, strategies))
    payload.update(congress(ar))
    payload['edgar'] = filing.sentinel()
    from test_source_capabilities import fred_payload, yahoo_engine
    payload['fred'] = fred_payload(tmp_path / 'fred')
    payload['yfinance'] = yahoo_engine(tmp_path / 'yahoo')._fetch_yfinance_data('2026-09-01', '2026-10-06')
    payload['equity_universe'] = {}
    engine = SimpleNamespace(paper_trade_strategies=strategies, _fetch_all_data=lambda *args, **kwargs: payload)
    result = _run_screen_preflight(contract.config, '2026-10-06', engine=engine)
    assert result['ok'], result
    assert result['coverage_excluded'] is True
    assert result['research_coverage_complete'] is False
    assert result['declared_exclusions'] == contract.declared_exclusions
    assert len(result['horizons']) == 4
    assert all(row['insider_activity']['status'] == 'disabled_by_policy' for row in result['horizons'].values())
    payload['fred'] = fred_payload(tmp_path / 'fred-partial', ['PAYEMS'])
    partial = _run_screen_preflight(contract.config, '2026-10-06', engine=engine)
    assert partial['ok'], partial
    assert partial['source_warnings']
    payload['edgar'] = {'error': 'HTTP 403'}
    bad = _run_screen_preflight(contract.config, '2026-10-06', engine=engine)
    assert bad['ok'] is False


def test_optional_commodity_document_scope_is_explicit_and_historical_unchanged():
    from tradingagents.strategies.modules import get_paper_trade_strategies
    from tradingagents.strategies.orchestration.operating_contract import configure_strategies
    original = get_paper_trade_strategies()
    legacy = configure_strategies(original, {})
    commodity = next(s for s in original if s.name == 'commodity_macro')
    assert 'regulations' in next(s for s in legacy if s.name == 'commodity_macro').data_sources
    reduced = configure_strategies(original, profile().config['autoresearch'])
    updated = next(s for s in reduced if s.name == 'commodity_macro')
    assert updated is not commodity
    assert updated.data_sources == ['yfinance', 'cftc', 'fred', 'finnhub']
    assert 'regulations' in commodity.data_sources
    assert updated._scan_catalysts('gold', {'finnhub': {'news': [{'headline': 'gold supply shock'}]}})


def test_profile_bound_report_refuses_missing_screening_and_committee(native_evidence):
    from test_operational_report import _attempt, _report
    repo, state, wire = native_evidence
    manifest_path = state.parent / 'manifest.json'
    manifest = json.loads(manifest_path.read_text())
    manifest['generations'][0].update(config_file=str(PROFILE), config_sha256=profile().profile_sha256)
    manifest_path.write_text(json.dumps(manifest))
    _attempt(repo, wire)
    report = _report(repo)
    assert report['completion']['workflow_complete'] is False
    assert report['completion']['research_coverage_complete'] is False
    assert 'screening_invalid' in report['completion']['reason_codes']


def test_controller_cannot_accept_held_supported_profile():
    import importlib.util
    spec = importlib.util.spec_from_file_location('stabilization_recover_runtime',
        Path(__file__).parents[1] / 'scripts/recover_runtime.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    report = {'outcome': 'degraded', 'accounting_valid': True, 'staging_complete': True,
        'staging_valid': True, 'evidence_complete': True, 'completion': {
        'version': 'supported_sixteen_books_v1', 'workflow_complete': False,
        'reason_codes': ['committee_incomplete']}}
    assert module._terminal_report(report)['disposition'] == 'wait_next_session'
    assert module._terminal_report(report)['reason_code'] == 'accepted_outcome'
    report['completion']['reason_codes'] = ['source_configuration_invalid']
    assert module._terminal_report(report)['disposition'] == 'requires_user'


def test_readiness_exposes_supported_continuity_separate_from_full_coverage(tmp_path):
    from test_release_readiness import _fixture, _assess
    repo, state, manifest = _fixture(tmp_path)
    manifest['generations'][0].update(config_file=str(PROFILE), config_sha256=profile().profile_sha256)
    (state.parent / 'manifest.json').write_text(json.dumps(manifest))
    report = _assess(repo)
    assert report['operational_continuity_complete'] is False
    assert report['research_coverage_complete'] is False
    assert all(row['operational_continuity_complete'] is False for row in report['sessions'])


def test_exact_profile_constructs_the_full_cohort_orchestrator(tmp_path, monkeypatch):
    from tradingagents.strategies.orchestration.cohort_orchestrator import CohortOrchestrator, build_default_cohorts
    config = deepcopy(profile().config)
    config['autoresearch']['state_dir'] = str(tmp_path)
    owner = CohortOrchestrator(build_default_cohorts(config), config, generation_id='gen_001', generation_commit='a'*40)
    try:
        assert len(owner.cohorts) == 16
        assert all('regulations' not in next(s for s in c['engine'].paper_trade_strategies
            if s.name == 'commodity_macro').data_sources for c in owner.cohorts)
    finally:
        for c in owner.cohorts:
            c['ledger'].close()


@pytest.mark.parametrize('blocklist,user_agent,valid', [
    ('GOOGL,GOOG', None, True), ('AAPL', None, False),
    (None, 'unexpected contact', False),
])
def test_frozen_worker_rejects_ambient_behavior_drift(monkeypatch, blocklist, user_agent, valid):
    import importlib.util
    from types import SimpleNamespace
    from tradingagents.strategies.orchestration.generation_config import CONFIG_FILE_ENV, CONFIG_SHA_ENV
    spec = importlib.util.spec_from_file_location('stabilization_run_cohorts', Path(__file__).parents[1]/'scripts/run_cohorts.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv(CONFIG_FILE_ENV, str(PROFILE))
    monkeypatch.setenv(CONFIG_SHA_ENV, profile().profile_sha256)
    monkeypatch.delenv('BLOCKED_TICKERS', raising=False)
    monkeypatch.delenv('EDGAR_USER_AGENT', raising=False)
    if blocklist is not None:
        monkeypatch.setenv('BLOCKED_TICKERS', blocklist)
    if user_agent is not None:
        monkeypatch.setenv('EDGAR_USER_AGENT', user_agent)
    args = SimpleNamespace(block_tickers=None)
    if valid:
        config = module._build_config(args)
        assert config['autoresearch']['blocked_tickers'] == ['GOOGL','GOOG']
    else:
        with pytest.raises(ValueError, match='generation_config_behavior_conflict'):
            module._build_config(args)


def test_supported_readiness_uses_canonical_central_epoch(tmp_path):
    import sqlite3
    from test_release_readiness import _fixture, SESSIONS
    from tradingagents.strategies.orchestration.release_readiness import _accounting_and_staging, COHORTS
    repo, state, manifest = _fixture(tmp_path)
    commit = manifest['generations'][0]['git_commit']
    for name in COHORTS:
        with sqlite3.connect(state/name/'portfolio.db') as connection:
            connection.execute('DELETE FROM metric_epochs')
    # Historical contracts retain the prior per-ledger epoch requirement.
    assert _accounting_and_staging(state,SESSIONS[-1],'epoch','gen_015',None) == (False,False)
    assert _accounting_and_staging(state,SESSIONS[-1],'epoch','gen_015',None,
        central_epoch_commit=commit) == (True,True)
    assert _accounting_and_staging(state,SESSIONS[-1],'epoch','gen_015',None,
        central_epoch_commit='0'*40) == (False,False)
