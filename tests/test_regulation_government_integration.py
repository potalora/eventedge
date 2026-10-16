"""A government-copy source must match the prospectively configured policy."""
from types import SimpleNamespace
import json

import pytest

from test_regulation_integration import inputs
from tradingagents.strategies.data_sources.evidence import CoverageRecords
from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine
from tradingagents.strategies.orchestration.regulation_inputs import validate_regulation_policy


COPY_POLICY = 'verified_government_copy_v1'
EVIDENCE_POLICY = 'complete_document_text_v1'


def test_model_gets_subscript_semantics_without_displacing_table_columns(monkeypatch):
    from test_regulation_government_acquisition import setup_transport, acquire, payload, DOC
    from test_regulation_government_copy import MAC
    from tradingagents.strategies.data_sources.regulation_assessment import prepare_request
    body = MAC.replace(b'SUMMARY:', b'Pollutant       Limit\nPM<INF>10</INF>            150\nSUMMARY:', 1)
    setup_transport(monkeypatch, government_body=body)
    evidence = payload(acquire())['regulation_evidence']['corpus'][DOC]
    request = prepare_request(evidence)
    unit = json.loads(request.user)['units'][0]
    assert 'PM10            150' in unit['text']
    offset = unit['text'].index('PM10') + 2
    assert unit['text_annotations'] == [{'kind': 'subscript', 'text_codepoint_start': offset,
                                         'text_codepoint_end': offset + 2}]


@pytest.mark.parametrize('policy', [COPY_POLICY, 'unknown', False, {}])
def test_configured_copy_policy_cannot_accept_a_legacy_source_graph(monkeypatch, policy):
    _, data, _ = inputs(monkeypatch)
    with pytest.raises(ValueError, match='invalid_regulation_evidence_policy'):
        validate_regulation_policy(data, {'regulation_evidence_policy': EVIDENCE_POLICY,
                                         'regulation_retrieval_policy': policy})


def test_orphan_copy_policy_cannot_silently_disable_source_validation():
    with pytest.raises(ValueError, match='invalid_regulation_evidence_policy'):
        validate_regulation_policy({}, {'regulation_retrieval_policy': COPY_POLICY})


def test_engine_monitor_passes_copy_policy_to_the_actual_source_boundary():
    calls = []
    rules = CoverageRecords([], coverage={'complete': True})
    source = SimpleNamespace(is_available=lambda: True,
        get_recent_proposed_rules=lambda **kw: calls.append(kw) or rules)
    engine = MultiStrategyEngine.__new__(MultiStrategyEngine)
    engine.registry = SimpleNamespace(get=lambda _: source)
    engine.ar_config = {'regulation_evidence_policy': EVIDENCE_POLICY,
                       'regulation_retrieval_policy': COPY_POLICY}
    engine._fetch_regulations_data('2026-10-09')
    assert len(calls) == 1
    assert calls[0]['evidence_policy'] == EVIDENCE_POLICY
    assert calls[0].get('retrieval_policy') == COPY_POLICY
    assert calls[0]['as_of'] == '2026-10-09'


def test_analysis_worker_rechecks_configured_copy_policy_before_model(monkeypatch):
    from test_regulation_assessment import neutral
    from test_regulation_evidence import DOC
    from tradingagents.strategies.learning.llm_analyzer import LLMAnalyzer
    candidate, data, assets = inputs(monkeypatch)
    analyzer = LLMAnalyzer()
    calls = []
    response = neutral(data['regulations']['regulation_evidence']['corpus'][DOC])
    monkeypatch.setattr(analyzer, '_call_llm', lambda *a, **kw: calls.append(a) or json.dumps(response))
    engine = MultiStrategyEngine.__new__(MultiStrategyEngine)
    engine.ar_config = {'regulation_evidence_policy': EVIDENCE_POLICY,
                       'regulation_retrieval_policy': COPY_POLICY}
    engine.registry = SimpleNamespace(get=lambda _: pytest.fail('live lookup'))
    engine._analyze_candidate(candidate, 'regulatory_pipeline', None, analyzer,
                              filing_data=data, universe=assets)
    assert calls == []
    assert candidate.metadata['analysis_status'] == 'failed'
    assert candidate.journal_only
