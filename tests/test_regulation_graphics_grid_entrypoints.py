"""The graphics opt-in constrains native execution even for text-only rules."""
from copy import deepcopy
from unittest.mock import Mock

import pytest

from test_regulation_evidence import build
from tradingagents.strategies.learning.llm_analyzer import LLMAnalyzer
from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine

pytestmark = pytest.mark.scheduled_acceptance


def profile():
    return {'autoresearch': {
        'graphics_execution_policy': 'single_target_compact_v1',
        'regulation_prompt_budget_policy': 'o200k_base_planning_v1',
        'regulation_evidence_policy': 'complete_document_text_v1',
        'regulation_retrieval_policy': 'verified_government_copy_v3',
        'model_runtime_policy': 'full_regulation_graphics_v1',
        'llm_billing_mode': 'chatgpt_plan', 'autoresearch_model': 'gpt-6-luna',
        'llm_effort': 'high', 'thesis_model': 'gpt-6.1-sol',
        'thesis_effort': 'high', 'llm_temperature': 0.0,
    }, 'execution': {'mode': 'paper', 'execution_enabled': False}}


@pytest.mark.parametrize('factory', [LLMAnalyzer, MultiStrategyEngine])
def test_unknown_graphics_policy_is_rejected_at_construction(factory):
    config = profile()
    config['autoresearch']['graphics_execution_policy'] = 'unreviewed'
    with pytest.raises(ValueError, match='invalid_regulation_graphics_policy'):
        factory(config=config)


@pytest.mark.parametrize('section,key,value', [
    ('execution', 'execution_enabled', True),
    ('autoresearch', 'llm_billing_mode', 'api_key'),
    ('autoresearch', 'thesis_model', 'unreviewed-model'),
    ('autoresearch', 'regulation_evidence_policy', 'complete_document_text'),
])
def test_changed_policy_starts_no_model_call_for_a_text_only_rule(section, key, value):
    analyzer = LLMAnalyzer(config=deepcopy(profile()))
    analyzer.config[section][key] = value
    analyzer._call_llm = Mock(side_effect=AssertionError('model must not be invoked'))
    with pytest.raises(ValueError):
        analyzer.analyze_regulation_evidence(build())
    analyzer._call_llm.assert_not_called()
    assert analyzer.last_regulation_attempt['model_call_started'] is False
    assert analyzer.last_regulation_attempt['validation_status'] == 'failed'


@pytest.mark.parametrize("budget", [None, "unreviewed", "o200k_base_planning_v1"])
def test_source_policy_boundary_binds_compact_budget_even_for_empty_corpus(budget):
    from test_scheduled_regulation_report import empty_regulations
    from tradingagents.strategies.data_sources.regulation_evidence import digest
    from tradingagents.strategies.orchestration.regulation_inputs import validate_regulation_policy
    source = empty_regulations()
    graph = source['regulation_evidence']
    graph['retrieval_policy'] = 'verified_government_copy_v3'
    graph['manifest_sha256'] = digest({k: v for k, v in graph.items() if k != 'manifest_sha256'})
    config = profile()['autoresearch']
    if budget is None:
        config.pop('regulation_prompt_budget_policy')
    else:
        config['regulation_prompt_budget_policy'] = budget
    if budget == 'o200k_base_planning_v1':
        assert validate_regulation_policy({'regulations': source}, config)['content_complete'] is True
    else:
        with pytest.raises(ValueError, match='invalid_regulation_evidence_policy'):
            validate_regulation_policy({'regulations': source}, config)
