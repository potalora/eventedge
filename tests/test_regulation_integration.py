"""Frozen complete-rule integration may never fall back to title-only models."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from tradingagents.strategies.learning.llm_analyzer import LLMAnalyzer
from tradingagents.strategies.modules.regulatory_pipeline import RegulatoryPipelineStrategy
from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine
from test_regulation_assessment import neutral, universe
from test_regulation_evidence import DOC
from test_regulation_source_evidence import run, source, transport


def inputs(monkeypatch):
    transport(monkeypatch)
    rules = run(source.RegulationsSource(api_key='fixture-key'))
    payload = {'proposed_rules': list(rules), 'coverage': rules.coverage,
               'regulation_evidence': rules.regulation_evidence}
    assets, companies = universe()
    data = {'regulations': payload, 'edgar': {'company_tickers': companies}}
    candidate = RegulatoryPipelineStrategy().screen(data, '2026-10-09', {'analysis_budget': 5})[0]
    return candidate, data, assets


def analyze(candidate, data, assets, response, monkeypatch):
    analyzer = LLMAnalyzer()
    calls = []
    def call(system, user, **kwargs):
        calls.append((system, user, kwargs))
        return json.dumps(response)
    monkeypatch.setattr(analyzer, '_call_llm', call)
    monkeypatch.setattr(analyzer, 'analyze_regulation', lambda *a, **k: pytest.fail('title fallback'))
    engine = MultiStrategyEngine.__new__(MultiStrategyEngine)
    engine.ar_config = {'regulation_evidence_policy': 'complete_document_text_v1'}
    engine.registry = SimpleNamespace(get=lambda _: pytest.fail('live ticker lookup'))
    engine._analyze_candidate(candidate, 'regulatory_pipeline', None, analyzer,
                              filing_data=data, universe=assets)
    return calls


def test_inputs_bind_full_source_without_invented_applicability(monkeypatch):
    from tradingagents.strategies.orchestration.regulation_inputs import regulation_analysis_inputs
    candidate, data, assets = inputs(monkeypatch)
    result = regulation_analysis_inputs(candidate, data, assets)
    assert result['evidence'] == data['regulations']['regulation_evidence']['corpus'][DOC]
    assert result['target_bindings'] == []


def test_full_rule_assessment_default_is_not_position_count_limited(monkeypatch):
    candidate, data, assets = inputs(monkeypatch)
    rows = []
    for index in range(7):
        row = deepcopy(data['regulations']['proposed_rules'][0])
        row['document_id'] = row['regulation_evidence_ref'] = f'EPA-TEST-2026-{index:04}'
        rows.append(row)
    data['regulations']['proposed_rules'] = rows
    result = RegulatoryPipelineStrategy().screen(data, '2026-10-09', {'max_positions': 3})
    assert len(result) == 7 and result.admission_manifest['excluded'] == []


@pytest.mark.parametrize('mutation', ['missing_graph', 'changed_row', 'changed_candidate', 'failed_status'])
def test_input_tampering_cannot_reach_model(monkeypatch, mutation):
    candidate, data, assets = inputs(monkeypatch)
    response = neutral(data['regulations']['regulation_evidence']['corpus'][DOC])
    if mutation == 'missing_graph': data['regulations'].pop('regulation_evidence')
    if mutation == 'changed_row': data['regulations']['proposed_rules'][0]['title'] = 'Other'
    if mutation == 'changed_candidate': candidate.metadata['document_id'] = 'Other'
    if mutation == 'failed_status': candidate.metadata['regulation_evidence_status'] = 'failed'
    assert analyze(candidate, data, assets, response, monkeypatch) == []
    assert candidate.metadata['analysis_status'] == 'failed'
    assert candidate.journal_only


def test_complete_no_thesis_is_journaled_without_ticker_invention(monkeypatch):
    candidate, data, assets = inputs(monkeypatch)
    response = neutral(data['regulations']['regulation_evidence']['corpus'][DOC])
    calls = analyze(candidate, data, assets, response, monkeypatch)
    assert len(calls) == 1
    assert len(json.loads(calls[0][1])['units'][0]['text']) > 5000
    assert candidate.metadata['analysis_status'] == 'validated'
    assert candidate.metadata['document_assessment'] == 'assessed_no_directional_thesis'
    assert candidate.metadata['regulation_evidence_status'] == 'sufficient'
    assert candidate.journal_only and candidate.ticker == '' and candidate.direction == 'neutral'
    assert not candidate.metadata.get('non_actionable_reason')


def test_directional_requires_recomputed_source_and_universe_bridge(monkeypatch):
    from tradingagents.strategies.orchestration.regulation_inputs import regulation_analysis_inputs
    candidate, data, assets = inputs(monkeypatch)
    arguments = regulation_analysis_inputs(candidate, data, assets)
    assert arguments['target_bindings'] == []
    response = neutral(arguments['evidence'])
    response.update(outcome='assessed_directional', direction='short', target_ticker='ACME',
                    target_binding_sha256='a' * 64, evidence_claim='This action applies to ACME CORPORATION.')
    analyze(candidate, data, assets, response, monkeypatch)
    assert candidate.metadata['analysis_status'] == 'failed'
    assert candidate.metadata['analysis_failure_reason'] == 'invalid_regulation_target'
    assert candidate.ticker == '' and candidate.journal_only


def test_insufficient_is_retained_and_failed(monkeypatch):
    candidate, data, assets = inputs(monkeypatch)
    response = neutral(data['regulations']['regulation_evidence']['corpus'][DOC])
    response.update(outcome='insufficient', direction=None, conviction=None,
                    unresolved_material_dependencies=[{'reference': 'Applicability', 'reason': 'Unknown'}])
    analyze(candidate, data, assets, response, monkeypatch)
    assert candidate.metadata['analysis_status'] == 'failed'
    assert candidate.metadata['regulation_assessment']['document_assessment'] == 'insufficient'
    assert candidate.journal_only and candidate.ticker == ''


def test_legacy_neutral_empty_tickers_is_not_a_valid_assessment(monkeypatch):
    candidate, data, assets = inputs(monkeypatch)
    analyze(candidate, data, assets, {'direction': 'neutral', 'conviction': .5,
                                     'affected_tickers': [], 'rationale': 'No companies'}, monkeypatch)
    assert candidate.metadata['analysis_status'] == 'failed'
    assert candidate.journal_only


def test_configured_policy_rejects_stripped_candidate_marker(monkeypatch):
    candidate, data, assets = inputs(monkeypatch)
    response = neutral(data['regulations']['regulation_evidence']['corpus'][DOC])
    candidate.metadata.pop('regulation_evidence_policy')
    assert analyze(candidate, data, assets, response, monkeypatch) == []
    assert candidate.metadata['analysis_status'] == 'failed'


def test_monitor_and_engine_transport_policy_and_graph(monkeypatch):
    candidate, data, assets = inputs(monkeypatch)
    from tradingagents.strategies.learning.event_monitor import EventMonitor
    from tradingagents.strategies.data_sources.evidence import CoverageRecords
    rules = CoverageRecords(data['regulations']['proposed_rules'], coverage=data['regulations']['coverage'])
    rules.regulation_evidence = data['regulations']['regulation_evidence']
    calls = []
    stub = SimpleNamespace(is_available=lambda: True,
        get_recent_proposed_rules=lambda **kw: calls.append(kw) or rules)
    engine = MultiStrategyEngine.__new__(MultiStrategyEngine)
    engine.registry = SimpleNamespace(get=lambda _: stub)
    engine.ar_config = {'regulation_evidence_policy': 'complete_document_text_v1'}
    result = engine._fetch_regulations_data('2026-10-09')
    assert calls[0]['evidence_policy'] == 'complete_document_text_v1'
    assert calls[0]['as_of'] == '2026-10-09'
    assert result['regulation_evidence'] == rules.regulation_evidence


def test_legacy_monitor_does_not_add_new_keyword(monkeypatch):
    from tradingagents.strategies.learning.event_monitor import EventMonitor
    calls = []
    stub = SimpleNamespace(is_available=lambda: True,
        get_recent_proposed_rules=lambda **kw: calls.append(kw) or [])
    EventMonitor(SimpleNamespace(get=lambda _: stub)).poll_proposed_rules()
    assert 'evidence_policy' not in calls[0]


def test_policy_strict_presence_declaration_and_error_reconciliation(monkeypatch):
    from tradingagents.strategies.orchestration.regulation_inputs import validate_regulation_policy
    candidate, data, assets = inputs(monkeypatch)
    config = {'regulation_evidence_policy': 'complete_document_text_v1'}
    assert validate_regulation_policy(data, config)['content_complete'] is True
    with pytest.raises(ValueError): validate_regulation_policy(data, {})
    stripped = deepcopy(data)
    stripped['regulations'].pop('regulation_evidence')
    stripped['regulations']['error'] = 'Failed'
    with pytest.raises(ValueError): validate_regulation_policy(stripped, config)
    data['regulations']['error'] = 'Failed'
    with pytest.raises(ValueError): validate_regulation_policy(data, config)
    assert validate_regulation_policy({'regulations': {'proposed_rules': []}}, {}) is None


def test_failed_source_keeps_validated_graph_and_error(monkeypatch):
    from tradingagents.strategies.orchestration.regulation_inputs import validate_regulation_policy
    from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
    transport(monkeypatch, status=403)
    with pytest.raises(SourceFetchError) as failure:
        run(source.RegulationsSource(api_key='fixture-key'))
    payload = deepcopy(failure.value.partial_data)
    config = {'regulation_evidence_policy': 'complete_document_text_v1'}
    with pytest.raises(ValueError): validate_regulation_policy({'regulations': payload}, config)
    payload['error'] = str(failure.value)
    summary = validate_regulation_policy({'regulations': payload}, config)
    assert summary['content_complete'] is False and summary['failed_rows'] == 1
    assert summary['failed_document_ids'] == [DOC]


@pytest.mark.parametrize('sufficient', [True, False])
def test_native_health_retains_blank_ticker_assessment_and_source(tmp_path, monkeypatch, sufficient):
    candidate, data, assets = inputs(monkeypatch)
    data.update(yfinance={}, openbb={}, equity_universe={'snapshot': assets.evidence})
    engine = MultiStrategyEngine(config={'autoresearch': {'state_dir': str(tmp_path),
        'regulation_evidence_policy': 'complete_document_text_v1',
        'equity_universe_policy': 'us_exchange_listed_sip_v1'}}, strategies=[RegulatoryPipelineStrategy()])
    monkeypatch.setattr(engine, '_build_regime_model', lambda _: {})
    engine._analyzer = LLMAnalyzer()
    evidence = data['regulations']['regulation_evidence']['corpus'][DOC]
    response = neutral(evidence)
    if not sufficient:
        response.update(outcome='insufficient', direction=None, conviction=None,
                        unresolved_material_dependencies=[{'reference': 'Applicability', 'reason': 'Unknown'}])
    monkeypatch.setattr(engine._analyzer, '_call_llm', lambda *a, **kw: json.dumps(response))
    signals, _, records = engine.screen_and_enrich('2026-10-09', data, epoch_id='epoch', policy_id='policy')
    assert signals == []
    health = records[0]
    assert health.status == ('signals' if sufficient else 'data_failure')
    assert health.evidence['regulation_source_coverage']['content_complete'] is True
    assessments = health.evidence['regulation_assessments']
    assert len(assessments) == health.evidence['admitted_count'] == 1
    assert assessments[0]['ticker'] == '' and assessments[0]['journal_only'] is True
    assert assessments[0]['evidence_sha256'] == evidence['manifest_sha256']
    assert assessments[0]['source_rows'] == data['regulations']['regulation_evidence']['rows']
    assert assessments[0]['document_assessment'] == response['outcome']


def test_invalid_source_keeps_failure_health_without_model(tmp_path, monkeypatch):
    candidate, data, assets = inputs(monkeypatch)
    data.update(yfinance={}, openbb={})
    data['regulations'].pop('regulation_evidence')
    engine = MultiStrategyEngine(config={'autoresearch': {'state_dir': str(tmp_path),
        'regulation_evidence_policy': 'complete_document_text_v1'}}, strategies=[RegulatoryPipelineStrategy()])
    monkeypatch.setattr(engine, '_build_regime_model', lambda _: {})
    monkeypatch.setattr(engine, '_enrich_with_llm', lambda *a, **kw: pytest.fail('invalid graph model'))
    signals, _, records = engine.screen_and_enrich('2026-10-09', data, epoch_id='epoch', policy_id='policy')
    assert signals == [] and records[0].status == 'data_failure'
    assert records[0].evidence['regulation_source_coverage'] is None
    assert records[0].evidence['regulation_assessments'] == []
