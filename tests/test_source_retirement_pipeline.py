"""Source retirement crosses real acceptance, health, 16 books and reporting."""
from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path

import pytest

from test_source_reliability_pipeline import pipeline, run_campaign, accepted_input_bytes, SESSION
from test_source_lifecycle_policy import POLICY, REASON, retirement


@pytest.mark.parametrize('source,dependents', [
    ('cftc', ('commodity_macro',)),
    ('edgar', ('filing_analysis', 'insider_activity', 'litigation', 'quantum_readiness', 'regulatory_pipeline')),
])
def test_explicit_retirement_has_complete_16_book_evidence_and_no_source_calls(pipeline, monkeypatch, source, dependents):
    fixture, owner, config, repo = pipeline
    config['autoresearch'].update(retirement(source, dependents))
    from tradingagents.strategies.orchestration import decision_clock
    monkeypatch.setattr(decision_clock, 'utc_now', lambda: fixture.now)
    result, wire, report = run_campaign(pipeline)
    assert result['outcome'] == 'clean' and result['input_coverage_valid'] is True
    scope = report['sources']['retirement_scope']
    assert scope['policy'] == POLICY and scope['retired_sources'][source]['strategies'] == list(dependents)
    assert all(report['disabled_strategies'][name] == REASON for name in dependents)
    records = owner._metric_store.read_strategy_health(session=SESSION)
    excluded = [row for row in records if row.strategy in dependents]
    assert len(excluded) == 4 * len(dependents)
    assert all(row.status == 'disabled_by_policy' and row.signal_count == 0
               and row.evidence['source_lifecycle_sha256'] == scope['sha256'] for row in excluded)
    if source == 'cftc':
        assert fixture.calls['cftc_sdk'] == 0
        assert not any('cftc.gov' in url for url in fixture.calls)
    else:
        assert not any('sec.gov' in url for url in fixture.calls)
    before = accepted_input_bytes(config)
    calls, models = fixture.calls.copy(), fixture.model_calls.copy()
    fixture.block_all = True
    replay, replay_wire, replay_report = run_campaign(pipeline)
    assert replay['outcome'] == 'clean' and replay_report['sources']['retirement_scope'] == scope
    assert accepted_input_bytes(config) == before
    assert fixture.calls == calls and fixture.model_calls == models and not fixture.blocked_calls
    assert len(replay_wire) == len(wire) == 16


def test_unbound_retirement_health_cannot_be_a_complete_operational_report(pipeline, monkeypatch):
    fixture, owner, config, repo = pipeline
    config['autoresearch'].update(retirement())
    from tradingagents.strategies.orchestration import decision_clock
    monkeypatch.setattr(decision_clock, 'utc_now', lambda: fixture.now)
    run_campaign(pipeline)
    state = Path(config['autoresearch']['state_dir'])
    import sqlite3
    with sqlite3.connect(state / 'metrics_v2.sqlite3') as connection:
        for identity, raw in connection.execute('SELECT health_id,payload_json FROM strategy_health').fetchall():
            row = json.loads(raw)
            if row['strategy'] == 'commodity_macro':
                row['evidence']['source_lifecycle_sha256'] = '0' * 64
                connection.execute('UPDATE strategy_health SET payload_json=? WHERE health_id=?', (json.dumps(row), identity))
    from tradingagents.strategies.orchestration.operational_report import build_operational_report
    report = build_operational_report(repo, 'gen_099', str(SESSION), snapshot_guaranteed=True)
    assert report['evidence_complete'] is False
    assert {'code': 'source_retirement_health_conflict'} in report['diagnostics']
    # A completed replay must reject changed health even though its source bytes
    # and all sixteen completed ledger phases remain present.
    fixture.block_all = True
    fixture.now += timedelta(seconds=1)
    replay = fixture.manager.run_daily(str(SESSION))['gen_099']
    assert replay['outcome'] == 'failed' and not fixture.blocked_calls


# The actual model transport remains the existing offline response fixture.
from test_research_model_outcome import worker


def test_unforeseen_empty_ticker_refuses_retired_edgar_fallback_before_model_or_provider(worker):
    from tradingagents.strategies.modules.base import Candidate
    engine, calls, responses = worker
    names = ('filing_analysis', 'insider_activity', 'litigation', 'quantum_readiness', 'regulatory_pipeline')
    engine.ar_config.update(retirement('edgar', names))
    validations = []
    from types import SimpleNamespace
    engine.registry.register(SimpleNamespace(name='edgar', validate_ticker=lambda ticker: validations.append(ticker) or True))
    responses.append('{"direction":"long","conviction":0.8,"rationale":"Source retained","affected_tickers":["NVDA"]}')
    candidate = Candidate(ticker='', date=str(SESSION), metadata={
        'needs_llm_analysis': True, 'analysis_type': 'filing_change', 'current_text': 'Retained source'})
    engine._enrich_with_llm([candidate], 'unforeseen_strategy')
    assert candidate.journal_only
    assert candidate.metadata['analysis_failure_reason'] == 'invalid_retired_source_identity_dependency'
    assert validations == [] and calls == []


def test_edgar_retirement_with_nonempty_court_regulation_and_award_inputs(pipeline, monkeypatch):
    fixture, owner, config, repo = pipeline
    names = ('filing_analysis', 'insider_activity', 'litigation', 'quantum_readiness', 'regulatory_pipeline')
    config['autoresearch'].update(retirement('edgar', names))
    from tradingagents.strategies.orchestration import decision_clock
    monkeypatch.setattr(decision_clock, 'utc_now', lambda: fixture.now)
    original_http = fixture.http
    def nonempty(method, url, **kwargs):
        response = original_http(method, url, **kwargs)
        if 'courtlistener.com' in url:
            return fixture.response({'count': 1, 'next': None, 'results': [
                {'docket_id': 120, 'caseName': 'Investors v. NVIDIA Corporation',
                 'dateFiled': str(SESSION), 'court': 'nysd', 'suitNature': 'securities',
                 'cause': 'Securities fraud class action'}]})
        if 'api.regulations.gov' in url and (kwargs.get('params') or {}).get('filter[agencyId]') == 'EPA':
            return fixture.response({'data': [{'id': 'EPA-2026-0001-0001', 'attributes': {
                'title': 'Semiconductor manufacturing requirements', 'agencyId': 'EPA',
                'documentType': 'Proposed Rule', 'postedDate': str(SESSION),
                'summary': 'Proposal affecting semiconductor manufacturing', 'docketId': 'EPA-2026-0001',
                'commentEndDate': '2026-12-01'}}], 'meta': {'totalElements': 1, 'totalPages': 1}})
        if 'api.usaspending.gov' in url:
            from test_usaspending_source import API_ROW
            return fixture.response({'results': [dict(API_ROW, **{'Base Obligation Date': str(SESSION),
                'Last Modified Date': str(SESSION) + ' 17:57:06', 'Start Date': str(SESSION) + ' 00:00:00'})],
                'page_metadata': {'page': 1, 'hasNext': False}})
        return response
    fixture.http = nonempty
    from tradingagents.strategies.modules.litigation import LitigationStrategy
    from tradingagents.strategies.modules.regulatory_pipeline import RegulatoryPipelineStrategy
    def forbidden(*args, **kwargs):
        pytest.fail('retired indirect dependency strategy attempted screening')
    monkeypatch.setattr(LitigationStrategy, 'screen', forbidden)
    monkeypatch.setattr(RegulatoryPipelineStrategy, 'screen', forbidden)
    from tradingagents.strategies.data_sources import usaspending_source
    from tradingagents.strategies.orchestration import multi_strategy_engine
    monkeypatch.setattr(usaspending_source, 'datetime', multi_strategy_engine.datetime)
    result, wire, report = run_campaign(pipeline)
    assert result['outcome'] == 'clean' and report['input_coverage_valid']
    from tradingagents.strategies.orchestration.source_inputs import SourceInputStore
    raw = next(value for key, value in accepted_input_bytes(config).items() if '/' not in key)
    data = SourceInputStore.decode(raw.decode())['payload']
    assert data['courtlistener']['dockets'] and data['regulations']['proposed_rules']
    assert data['usaspending']['data']['contracts']
    assert not any('sec.gov' in url for url in fixture.calls)
    records = owner._metric_store.read_strategy_health(session=SESSION)
    government = [row for row in records if row.strategy == 'govt_contracts']
    assert len(government) == 4 and all(row.evidence['candidate_count'] > 0 for row in government)
    assert all(row.evidence.get('actionable_candidate_count', 0) == 0 for row in government)
