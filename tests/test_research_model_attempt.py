"""Exact logical request binding, without provider calls or prompt duplication."""
from copy import deepcopy
import hashlib
import importlib.util
import json
import time

import pytest
from openai import OpenAI

from tradingagents.strategies.learning.llm_analyzer import LLMAnalyzer
from tradingagents.strategies.trading.portfolio_committee import PortfolioCommittee
from tradingagents.strategies.runtime_deadline import model_budget


MODULE = 'tradingagents.strategies.orchestration.research_model_attempt'


def api():
    assert importlib.util.find_spec(MODULE) is not None, 'request receipt implementation is missing'
    from tradingagents.strategies.orchestration import research_model_attempt
    return research_model_attempt


def config(enabled=True):
    ar = {'autoresearch_model': 'gpt-6-luna', 'thesis_model': 'gpt-6-astra',
          'llm_effort': 'medium', 'thesis_effort': 'high', 'llm_temperature': 0.0}
    if enabled:
        ar['research_resilience_policy'] = 'dependency_isolation_v1'
    return {'autoresearch': ar}


@pytest.fixture
def transport(monkeypatch):
    calls, outcomes = [], []

    def send(payload, timeout):
        calls.append(deepcopy(payload['request']))
        result = outcomes.pop(0) if outcomes else '{"direction":"long","conviction":0.8,"rationale":"Evidence"}'
        if isinstance(result, Exception):
            raise result
        return {'text': result, 'provenance': {
            'configured_model': payload['request']['model'],
            'returned_model': payload['request']['model'], 'returned_revision': None,
            'identity_status': 'unpinned', 'response_id': 'offline-response',
            'reasoning_effort': payload['request']['effort']}}

    monkeypatch.setattr('tradingagents.strategies.llm_utils.bounded_transport', send)
    with OpenAI(api_key='offline-private', base_url='https://offline.invalid/v1') as client:
        yield client, calls, outcomes


def test_receipt_exact_utf8_and_full_logical_digest_without_prompts():
    args = dict(model='gpt-6-astra', role='thesis', effort='high', max_tokens=4096,
                temperature=0.0, system=' rôle\n', prompt='Texte € 漢字\n')
    receipt = api().request_receipt(**args)
    expected_digest = hashlib.sha256(json.dumps(args, sort_keys=True, separators=(',', ':'),
                                               ensure_ascii=True, allow_nan=False).encode()).hexdigest()
    assert receipt == {
        'version': 'research-model-request-v1',
        **{key: args[key] for key in ('model', 'role', 'effort', 'max_tokens', 'temperature')},
        'system_sha256': hashlib.sha256(args['system'].encode()).hexdigest(),
        'system_bytes': len(args['system'].encode()),
        'prompt_sha256': hashlib.sha256(args['prompt'].encode()).hexdigest(),
        'prompt_bytes': len(args['prompt'].encode()), 'request_sha256': expected_digest}
    assert api().validate_request_receipt(receipt, **args) == receipt
    assert 'system' not in receipt and 'prompt' not in receipt


@pytest.mark.parametrize('field,value', [('model', 'other'), ('role', 'bounded'), ('effort', 'low'),
    ('max_tokens', 1024), ('temperature', 0.5), ('system', 'sys '), ('prompt', 'user\n')])
def test_each_logical_argument_changes_binding(field, value):
    args = dict(model='gpt-6-astra', role='thesis', effort='high', max_tokens=4096,
                temperature=0.0, system='sys', prompt='user')
    receipt = api().request_receipt(**args)
    with pytest.raises(ValueError, match='invalid_research_model_request'):
        api().validate_request_receipt(receipt, **{**args, field: value})
    assert api().request_receipt(**{**args, field: value})['request_sha256'] != receipt['request_sha256']


@pytest.mark.parametrize('change', [lambda r: r.update(prompt_bytes=0),
    lambda r: r.update(system_sha256='0' * 64), lambda r: r.update(request_sha256='0' * 64),
    lambda r: r.update(extra=True), lambda r: r.update(max_tokens=True),
    lambda r: r.update(temperature=0), lambda r: r.update(version='other')])
def test_receipt_tampering_rejected(change):
    args = dict(model='gpt-6-astra', role='thesis', effort='high', max_tokens=4096,
                temperature=0.0, system='sys', prompt='user')
    receipt = api().request_receipt(**args)
    change(receipt)
    with pytest.raises(ValueError, match='invalid_research_model_request'):
        api().validate_request_receipt(receipt, **args)


@pytest.mark.parametrize('field,value', [('max_tokens', True), ('max_tokens', 0),
    ('max_tokens', 1.5), ('temperature', float('nan')), ('temperature', float('inf')),
    ('temperature', True), ('system', None), ('prompt', b'bytes'), ('model', '')])
def test_malformed_logical_arguments_rejected(field, value):
    args = dict(model='gpt-6-astra', role='thesis', effort='high', max_tokens=4096,
                temperature=0.0, system='sys', prompt='user')
    with pytest.raises(ValueError, match='invalid_research_model_request'):
        api().request_receipt(**{**args, field: value})


@pytest.mark.parametrize('role,model,effort', [('thesis', 'gpt-6-astra', 'high'),
                                            ('bounded', 'gpt-6-luna', 'medium')])
@pytest.mark.parametrize('result', ['valid', 'invalid', 'failure'])
def test_analyzer_boundary_records_actual_arguments_for_all_responses(transport, role, model, effort, result):
    client, calls, outcomes = transport
    analyzer = LLMAnalyzer(config())
    analyzer._client = client
    response = {'valid': '{}', 'invalid': 'not-json', 'failure': RuntimeError('offline failure')}[result]
    outcomes.append(response)
    text = analyzer._call_llm('sys €', 'prompt\n', max_tokens=127, role=role)
    assert len(calls) == 1
    assert text == ('' if result == 'failure' else response)
    api().validate_request_receipt(analyzer.last_research_request, model=model, role=role,
        effort=effort, max_tokens=127, temperature=0.0, system='sys €', prompt='prompt\n')
    assert 'offline-private' not in json.dumps(analyzer.last_research_request)


def test_analyzer_failed_next_attempt_replaces_previous_request_and_fork_resets(transport):
    client, calls, _ = transport
    analyzer = LLMAnalyzer(config())
    analyzer._client = client
    analyzer._call_llm('sys', 'first')
    first = deepcopy(analyzer.last_research_request)
    fork = analyzer.fork_for_parallel()
    assert fork.last_research_request is None
    assert analyzer.last_research_request == first
    with model_budget(time.monotonic() - 1):
        assert analyzer._call_llm('sys', 'second') == ''
    assert len(calls) == 1
    assert analyzer.last_call_failure == 'model_deadline_exhausted'
    assert analyzer.last_research_request['request_sha256'] != first['request_sha256']
    api().validate_request_receipt(analyzer.last_research_request, model='gpt-6-astra', role='thesis',
        effort='high', max_tokens=4096, temperature=0.0, system='sys', prompt='second')


def test_exact_candidate_reuse_keeps_request_receipt_with_one_physical_call(transport):
    from tradingagents.strategies.candidate_response_reuse import (candidate_response_memo,
        begin_candidate_response, end_candidate_response, commit_candidate_response)
    client, calls, _ = transport
    analyzer = LLMAnalyzer(config())
    analyzer._client = client
    receipts = []
    with candidate_response_memo():
        for _ in range(2):
            token = begin_candidate_response('filing_analysis', 'filing_change', 'discovery', False)
            try:
                result = analyzer.analyze_filing_change('retained', '', 'CFR')
                assert result is not None and result['direction'] == 'long'
                commit_candidate_response()
                receipts.append(deepcopy(analyzer.last_research_request))
            finally:
                end_candidate_response(token)
    assert len(calls) == 1 and receipts[0] == receipts[1]
    assert analyzer.last_call_provenance['request_reuse']['reused'] is True


def test_analyzer_parse_failure_keeps_current_actual_request(transport):
    client, calls, outcomes = transport
    analyzer = LLMAnalyzer(config())
    analyzer._client = client
    assert analyzer.analyze_filing_change('first source', '', 'CFR')['direction'] == 'long'
    first = deepcopy(analyzer.last_research_request)
    outcomes.append('not-json')
    assert analyzer.analyze_filing_change('second source', '', 'CFR') == {}
    assert len(calls) == 2
    assert first['request_sha256'] != analyzer.last_research_request['request_sha256']
    api().validate_request_receipt(analyzer.last_research_request, role='thesis',
        **{key: calls[-1][key] for key in ('model', 'effort', 'max_tokens', 'temperature', 'system', 'prompt')})


@pytest.mark.parametrize('ticker,status', [('CFR', 'selected'), ('OTHER', 'failed')])
def test_committee_grounded_recommendation_or_invalid_attribution_keeps_exact_request(transport, ticker, status):
    client, calls, outcomes = transport
    committee = PortfolioCommittee(config())
    committee._client = client
    outcomes.append(json.dumps([dict(ticker=ticker, direction='long', position_size_pct=0.05,
        confidence=0.8, rationale='Retained source catalyst', contributing_strategies=['filing_analysis'])]))
    recs = committee.synthesize([dict(ticker='CFR', direction='long', score=2.5,
                                     strategy='filing_analysis', metadata={})])
    assert bool(recs) is (ticker == 'CFR')
    assert committee.last_decision_status['status'] == status
    api().validate_request_receipt(committee.last_decision_status['model_request'], role='committee',
        **{key: calls[-1][key] for key in ('model', 'effort', 'max_tokens', 'temperature', 'system', 'prompt')})


@pytest.mark.parametrize('result', ['valid', 'invalid', 'failure', 'deadline'])
def test_committee_durable_status_records_attempt_even_when_failed(transport, result):
    client, calls, outcomes = transport
    committee = PortfolioCommittee(config())
    committee._client = client
    outcomes.append({'valid': '[]', 'invalid': 'not-json', 'failure': RuntimeError('offline failure'),
                     'deadline': 'unused'}[result])
    signals = [dict(ticker='CFR', direction='long', score=2.5, strategy='filing_analysis', metadata={})]
    with model_budget(time.monotonic() + (-1 if result == 'deadline' else 10)):
        assert committee.synthesize(signals) == []
    assert len(calls) == (0 if result == 'deadline' else 1)
    status = committee.last_decision_status
    assert status['status'] == ('abstained' if result == 'valid' else 'failed')
    receipt = status['model_request']
    assert receipt == committee.last_research_request
    assert receipt['role'] == 'committee' and receipt['max_tokens'] == 4096
    if calls:
        args = {key: calls[0][key] for key in ('model', 'effort', 'max_tokens', 'temperature', 'system', 'prompt')}
        api().validate_request_receipt(receipt, role='committee', **args)
    committee.synthesize([])
    assert committee.last_research_request is None
    assert 'model_request' not in committee.last_decision_status


def test_committee_research_hold_clears_previous_attempt_without_new_call(transport):
    client, calls, _ = transport
    committee = PortfolioCommittee(config())
    committee._client = client
    committee._call_llm(system='sys', prompt='first')
    assert committee.last_research_request is not None
    assert committee.synthesize([], regime_context={'research_hold_reasons': ['shared_regime_unavailable']}) == []
    assert len(calls) == 1
    assert committee.last_research_request is None
    assert 'model_request' not in committee.last_decision_status


def test_legacy_calls_have_no_new_request_authority(transport):
    client, calls, outcomes = transport
    analyzer = LLMAnalyzer(config(False))
    committee = PortfolioCommittee(config(False))
    analyzer._client = committee._client = client
    analyzer._call_llm('sys', 'user')
    outcomes.append('[]')
    committee.synthesize([dict(ticker='CFR', direction='long', score=2.5, strategy='filing_analysis', metadata={})])
    assert len(calls) == 2
    assert analyzer.last_research_request is None and committee.last_research_request is None
    assert 'model_request' not in committee.last_decision_status


def _committee_output_contract_case(transport, profile):
    from tradingagents.strategies.orchestration.cohort_orchestrator import SIZE_PROFILES
    client, calls, outcomes = transport
    committee = PortfolioCommittee(config(profile != 'legacy'),
        size_profile=None if profile == 'legacy' else SIZE_PROFILES[profile])
    committee._client = client
    signals = [dict(ticker='CFR', direction='long', score=2.5,
        strategy='filing_analysis', metadata={})]
    signals += [dict(ticker='SHT', direction='short', score=2.5,
        strategy=strategy, metadata={}) for strategy in ('litigation', 'supply_chain')]
    return committee, calls, outcomes, signals


def _assert_transmitted_committee_output_contract(committee, calls, profile):
    assert len(calls) == 1
    request = calls[0]
    sentence = 'The direction field must be exactly lowercase "long" or "short".'
    assert request['system'].count(sentence) == 1
    assert request['system'].index(sentence) > request['system'].index(
        'rationale, contributing_strategies, regime_alignment.')
    assert ('"ticker": "SHT"' in request['prompt']) is (profile != '5k')
    if profile == '5k':
        assert 'LONG ONLY' in request['system']
    if profile == 'legacy':
        assert committee.last_research_request is None
        assert 'model_request' not in committee.last_decision_status
    else:
        receipt = committee.last_decision_status['model_request']
        args = {key: request[key] for key in ('model', 'effort', 'max_tokens', 'temperature', 'system', 'prompt')}
        api().validate_request_receipt(receipt, role='committee', **args)
        assert receipt['system_sha256'] == hashlib.sha256(request['system'].encode()).hexdigest()
        assert receipt['request_sha256'] == hashlib.sha256(json.dumps(
            dict(role='committee', **args), sort_keys=True, separators=(',', ':'),
            ensure_ascii=True, allow_nan=False).encode()).hexdigest()


@pytest.mark.parametrize('profile', ['legacy', '5k', '100k'])
def test_transmitted_committee_contract_keeps_lowercase_long_and_exact_request(transport, profile):
    committee, calls, outcomes, signals = _committee_output_contract_case(transport, profile)
    outcomes.append(json.dumps([dict(ticker='CFR', direction='long', position_size_pct=0.05,
        confidence=0.8, rationale='Retained source catalyst', contributing_strategies=['filing_analysis'])]))
    recommendations = committee.synthesize(signals)
    assert [(row.ticker, row.direction) for row in recommendations] == [('CFR', 'long')]
    assert committee.last_decision_status['status'] == 'selected'
    _assert_transmitted_committee_output_contract(committee, calls, profile)


@pytest.mark.parametrize('profile', ['legacy', '100k'])
def test_transmitted_committee_contract_keeps_convergent_lowercase_short(transport, profile):
    committee, calls, outcomes, signals = _committee_output_contract_case(transport, profile)
    outcomes.append(json.dumps([dict(ticker='SHT', direction='short', position_size_pct=0.05,
        confidence=0.8, rationale='Two retained catalysts', contributing_strategies=['litigation', 'supply_chain'])]))
    recommendations = committee.synthesize(signals)
    assert [(row.ticker, row.direction) for row in recommendations] == [('SHT', 'short')]
    assert committee.last_decision_status['status'] == 'selected'
    _assert_transmitted_committee_output_contract(committee, calls, profile)


@pytest.mark.parametrize('profile', ['legacy', '5k', '100k'])
@pytest.mark.parametrize('uppercase', ['LONG', 'SHORT'])
def test_uppercase_committee_row_rejects_whole_response_without_fallback(transport, monkeypatch, profile, uppercase):
    committee, calls, outcomes, signals = _committee_output_contract_case(transport, profile)
    def forbidden_fallback(*args, **kwargs):
        raise AssertionError('invalid model response must never use rule fallback')
    monkeypatch.setattr(committee, '_rule_based_synthesize', forbidden_fallback)
    valid = dict(ticker='CFR', direction='long', position_size_pct=0.05,
        confidence=0.8, rationale='Retained source catalyst', contributing_strategies=['filing_analysis'])
    invalid = dict(ticker='CFR' if uppercase == 'LONG' else 'SHT', direction=uppercase,
        position_size_pct=0.05, confidence=0.8, rationale='Retained source catalyst',
        contributing_strategies=['filing_analysis'] if uppercase == 'LONG' else ['litigation', 'supply_chain'])
    outcomes.append(json.dumps([valid, invalid]))
    assert committee.synthesize(signals) == []
    status = committee.last_decision_status
    assert status['status'] == 'failed' and status['reason'] == 'invalid_model_response'
    assert status['selected_count'] == 0 and status['degraded'] is True
    _assert_transmitted_committee_output_contract(committee, calls, profile)
