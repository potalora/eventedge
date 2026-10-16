"""Constructed compact Sol route admission and replay; no native model calls."""
from copy import deepcopy
from dataclasses import replace
import json
import time

import pytest

from test_regulation_graphics_grid import assets, response, review_response, provenance
from test_regulation_graphics_grid_runtime import Analyzer, config, state, runtime, memo_setup
from tradingagents.strategies.data_sources import regulation_graphics_grid as g
from tradingagents.strategies.learning.llm_analyzer import LLMAnalyzer
from tradingagents.strategies.runtime_deadline import model_budget

POLICY = 'sol_high_v1'


def sol_config():
    value = config()
    value['autoresearch']['regulation_graphics_model_policy'] = POLICY
    return value


def sol_provenance(q):
    value = provenance(q)
    value.update(configured_model='gpt-6.1-sol', returned_model='gpt-6.1-sol',
                 regulation_graphics_model_policy=POLICY, model_request=g._logical_request(q))
    return value


class SolAnalyzer(Analyzer):
    def __init__(self, shared):
        super().__init__(shared)
        self.config = sol_config()

    def _call_llm(self, system, user, **kwargs):
        assert kwargs.pop('regulation_graphics_model_policy') == POLICY
        assert kwargs['max_call_seconds'] == 240 and kwargs['max_tokens'] == 4096
        assert self._model_name == 'gpt-6-luna' and self._effort == 'high'
        raw = super()._call_llm(system, user, **kwargs)
        self.last_call_provenance.update(configured_model='gpt-6.1-sol', returned_model='gpt-6.1-sol',
                                         regulation_graphics_model_policy=POLICY)
        return raw


def test_sol_request_receipt_and_replay_are_bound_to_finite_route():
    rows = assets(1)
    old = g.prepare_execution_plan(rows)
    plan = g.prepare_execution_plan(rows, model_policy=POLICY)
    assert old['plan_sha256'] == 'fcac9f233afd266de22d9842bde414da3f9f30f80f835b9eda51d4e6561da0bb'
    oldq = g.prepare_recognition(rows, old, 0)
    assert json.loads(oldq.context_json)['request_identity_sha256'] == '3228cb28f1f3053eb0956f3fc556696ee35c32030112fdb2799ef3cb7e30dab5'
    assert g.legacy._digest(g._logical_request(oldq)) == 'a90c3344c62eb258eaf8ae9c82455cf8ba8b10a149b39ea06d4084e7a336f5e8'
    assert g.model_policy(old) is None and g.model_policy(plan) == POLICY
    q = g.prepare_recognition(rows, plan, 0)
    assert plan['version'] == 'regulation-graphics-execution-plan-v4'
    assert json.loads(q.context_json)['version'] == 'regulation-graphics-request-v10'
    assert json.loads(q.context_json)['model_policy'] == POLICY
    assert (q.system, q.user, q.document_inputs) == (oldq.system, oldq.user, oldq.document_inputs)
    rec = g.validate_recognition(json.dumps(response(q, external=False)), q, sol_provenance(q))
    assert rec['model_request']['model'] == 'gpt-6.1-sol'
    rq = g.prepare_review(rows, plan, 0, rec)
    rev = g.validate_review(json.dumps(review_response(rq, rec)), rq, rec, sol_provenance(rq))
    bundle = g.seal_bundle(0, [{'recognition': {'receipt': rec}, 'review': {'receipt': rev}}], 0)
    assert g.replay_target(rows, plan, 0, bundle)[0]['id'] == rows[0]['id']
    with pytest.raises(ValueError): g.replay_target(rows, old, 0, bundle)
    with pytest.raises(ValueError): g.validate_recognition(rec['response_text'], q, provenance(q))
    with pytest.raises(ValueError): g.validate_recognition(rec['response_text'], oldq, sol_provenance(q))
    for field in ('model_policy', 'version', 'plan_sha256'):
        wrong = deepcopy(plan); wrong[field] = 'wrong'
        with pytest.raises(ValueError): g.model_policy(wrong)


@pytest.mark.parametrize('value', [None, True, '', 'unknown'])
def test_present_unknown_or_orphan_profile_marker_rejected(value):
    candidate = sol_config(); candidate['autoresearch']['regulation_graphics_model_policy'] = value
    with pytest.raises(ValueError): g.configured(candidate)
    candidate = sol_config(); candidate['autoresearch'].pop('graphics_execution_policy')
    with pytest.raises(ValueError): g.configured(candidate)
    with pytest.raises(ValueError): g.prepare_execution_plan(assets(1), model_policy='unknown')


@pytest.mark.parametrize('fault', [None, 'partial', 'reject', 'malformed', 'terminal_partial'])
def test_sol_success_correction_and_failed_partition_replay(fault):
    shared = state(fail=0) if fault == 'terminal_partial' else state(faults={(0, 'recognition' if fault == 'partial' else 'review', 0): fault})
    analyzer = SolAnalyzer(shared); rows = assets(1); attempt = {}
    with model_budget(time.monotonic()+10):
        if fault == 'terminal_partial':
            with pytest.raises(ValueError): runtime().recognize_graphics(analyzer, rows, attempt)
        else: runtime().recognize_graphics(analyzer, rows, attempt)
    assert attempt['graphics_plan']['model_policy'] == POLICY
    result = runtime().replay_attempt(rows, attempt)
    assert (result is None) is (fault == 'terminal_partial')
    assert analyzer._model_name == 'gpt-6-luna' and analyzer._effort == 'high'
    assert len(shared['calls']) <= 4
    bad = deepcopy(attempt); bad['graphics_plan'] = g.prepare_execution_plan(rows)
    with pytest.raises(ValueError): runtime().replay_attempt(rows, bad)


def analyzer_boundary(monkeypatch):
    from tradingagents.strategies import chatgpt_plan_client as clients, llm_utils
    monkeypatch.setattr(clients, '_profile_identity', lambda path: 'inert-test-profile')
    calls = []
    def call(client, **kwargs):
        calls.append(kwargs)
        kwargs['provenance'].update(returned_model=kwargs['model'])
        return '{}'
    monkeypatch.setattr(llm_utils, 'call_analysis_model', call)
    return calls


def test_analyzer_sol_call_uses_local_model_and_fork_keeps_base_luna(monkeypatch):
    calls = analyzer_boundary(monkeypatch)
    candidate = sol_config(); candidate['autoresearch']['research_resilience_policy'] = 'dependency_isolation_v1'
    parent = LLMAnalyzer(candidate); worker = parent.fork_for_parallel()
    q = g.prepare_recognition(assets(1), g.prepare_execution_plan(assets(1), model_policy=POLICY), 0)
    before = deepcopy(parent.config)
    assert worker._call_llm(q.system, q.user, role='bounded', document_inputs=q.document_inputs,
        max_call_seconds=240, regulation_graphics_model_policy=POLICY) == '{}'
    assert calls[0]['model'] == 'gpt-6.1-sol' and calls[0]['effort'] == 'high'
    assert calls[0]['regulation_graphics_model_policy'] == POLICY
    assert worker.last_call_provenance['regulation_graphics_model_policy'] == POLICY
    assert worker.last_research_request['model'] == 'gpt-6.1-sol'
    assert worker._model_name == parent._model_name == 'gpt-6-luna'
    assert worker.config == parent.config == before
    assert worker._call_llm(q.system, q.user, role='bounded', document_inputs=q.document_inputs, max_call_seconds=240) == '{}'
    assert calls[1]['model'] == 'gpt-6-luna' and 'regulation_graphics_model_policy' not in calls[1]


@pytest.mark.parametrize('fault', ['unknown', 'missing_profile', 'wrong_profile', 'role', 'tokens', 'temperature', 'effort', 'model', 'cap', 'unlabelled', 'missing_originals', 'pdf'])
def test_analyzer_invalid_route_never_reaches_client_selection(monkeypatch, fault):
    from tradingagents.strategies import chatgpt_plan_client as clients
    analyzer = LLMAnalyzer(sol_config())
    q = g.prepare_recognition(assets(1), g.prepare_execution_plan(assets(1)), 0)
    kw = dict(role='bounded', document_inputs=q.document_inputs, max_call_seconds=240, regulation_graphics_model_policy=POLICY)
    if fault == 'unknown': kw['regulation_graphics_model_policy'] = 'unknown'
    elif fault == 'missing_profile': analyzer.config['autoresearch'].pop('regulation_graphics_model_policy')
    elif fault == 'wrong_profile': analyzer.config['execution']['execution_enabled'] = True
    elif fault == 'role': kw['role'] = 'thesis'
    elif fault == 'tokens': kw['max_tokens'] = 4095
    elif fault == 'temperature': analyzer._temperature = 0.1
    elif fault == 'effort': analyzer._effort = 'low'
    elif fault == 'model': analyzer._model_name = 'gpt-6.1-sol'
    elif fault == 'cap': kw['max_call_seconds'] = 120
    elif fault == 'unlabelled': kw['document_inputs'] = tuple(replace(x, presentation_policy=None) for x in q.document_inputs)
    elif fault == 'missing_originals': kw['document_inputs'] = None
    elif fault == 'pdf':
        from tradingagents.strategies.plan_document_inputs import PlanDocumentInput, ORIGINALS_LABEL_POLICY
        data = b'%PDF-1.7\nfixture'
        kw['document_inputs'] = (PlanDocumentInput('application/pdf', data, 'fixture.pdf', g.legacy.hashlib.sha256(data).hexdigest(), ORIGINALS_LABEL_POLICY),)
    selected = []
    monkeypatch.setattr(clients, 'configured_plan_client', lambda *a: selected.append(a))
    assert analyzer._call_llm(q.system, q.user, **kw) == ''
    assert analyzer.last_call_failure == 'analysis_unavailable' and selected == []


def test_invocation_memo_separates_routes_and_rejects_cross_route_cache(monkeypatch):
    scope, begin, end = memo_setup(monkeypatch); rt = runtime(); rows = assets(1); shared = state()
    with model_budget(time.monotonic()+10), scope():
        token = begin('regulation', 'thesis', 'doc-id', False)
        try:
            luna = {}; rt.recognize_graphics(Analyzer(shared), rows, luna)
            sol = {}; rt.recognize_graphics(SolAnalyzer(shared), rows, sol)
            assert len(shared['calls']) == 4
            from tradingagents.strategies.candidate_response_reuse import _MEMO
            memo = _MEMO.get()
            solkey = rt._memo_key(SolAnalyzer(shared), rows, sol['graphics_plan'])[1]
            memo.graphics_entries[solkey] = deepcopy({k:luna[k] for k in ('graphics_plan','graphics_batches','graphics_failures')})
            with pytest.raises(ValueError): rt.recognize_graphics(SolAnalyzer(shared), rows, {})
            assert len(shared['calls']) == 4
        finally: end(token)


@pytest.mark.parametrize('fault', ['extra_target', 'target_index', 'metadata', 'size', 'duplicate_id'])
def test_route_reader_rejects_resealed_malformed_plan(fault):
    plan = g.prepare_execution_plan(assets(2), model_policy=POLICY)
    if fault == 'extra_target': plan['targets'].append(deepcopy(plan['targets'][0]))
    elif fault == 'target_index': plan['targets'][0]['target_index'] = True
    elif fault == 'metadata': plan['assets'][0]['sha256'] = 'wrong'
    elif fault == 'size': plan['assets'][0]['size_bytes'] = 0
    elif fault == 'duplicate_id': plan['assets'][1]['id'] = plan['assets'][0]['id']
    plan['plan_sha256'] = g.legacy._digest({k:v for k,v in plan.items() if k != 'plan_sha256'})
    with pytest.raises(ValueError): g.model_policy(plan)


def test_sol_parent_reuse_is_rejected_before_any_native_stage(monkeypatch):
    analyzer = SolAnalyzer(state()); rows = assets(1)
    monkeypatch.setattr(analyzer, 'fork_for_parallel', lambda: analyzer)
    with model_budget(time.monotonic()+10):
        good, bad = runtime()._target(analyzer, rows, g.prepare_execution_plan(rows, model_policy=POLICY), 0)
    assert good is None and analyzer.state['calls'] == []
    assert not bad['rounds'][0]['recognition']['failure']['model_call_started']
    assert runtime().replay_target_chain(rows, g.prepare_execution_plan(rows, model_policy=POLICY), 0, bad) is None
