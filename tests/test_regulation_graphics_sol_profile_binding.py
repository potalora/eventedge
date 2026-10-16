"""Declared graphics route binds frozen requests; no credentials or model calls."""
from copy import deepcopy
import hashlib
import json
import sqlite3

import pytest

from test_regulation_prompt_budget_integration import config
from tradingagents.strategies.data_sources import regulation_prompt_budget as budget
from tradingagents.strategies.orchestration.regulation_inputs import validate_regulation_policy
from test_operational_report import native_evidence


@pytest.mark.parametrize('policy', [None, 'unknown', '', True, 1, [], {}])
def test_ar_only_unknown_graphics_model_policy_is_rejected(policy):
    ar = config()['autoresearch']
    ar['regulation_graphics_model_policy'] = policy
    with pytest.raises(ValueError, match='invalid_regulation_prompt_graphics_model_policy'):
        budget.configured(ar)
    with pytest.raises(ValueError, match='invalid_regulation_evidence_policy'):
        validate_regulation_policy({'regulations': {}}, ar)


@pytest.mark.parametrize('missing', ['graphics_execution_policy', 'regulation_prompt_budget_policy',
    'regulation_evidence_policy'])
def test_ar_only_orphan_sol_policy_is_rejected(missing):
    ar = config()['autoresearch']
    ar['regulation_graphics_model_policy'] = 'sol_high_v1'
    ar.pop(missing)
    with pytest.raises(ValueError):
        budget.configured(ar)


def test_sol_model_policy_keeps_existing_prompt_budget_policy():
    ar = config()['autoresearch']
    assert budget.configured(ar) == budget.POLICY
    ar['regulation_graphics_model_policy'] = 'sol_high_v1'
    assert budget.configured(ar) == budget.POLICY


def failed_graphics_attempt(monkeypatch, policy):
    from test_regulation_evidence import build
    from test_regulation_graphics_grid import assets
    from tradingagents.strategies.data_sources import regulation_graphics_grid as contract
    from tradingagents.strategies.data_sources import regulation_graphics_inputs
    from tradingagents.strategies.learning import regulation_graphics_grid_runtime as runtime
    evidence, rows = build(), assets(1)
    monkeypatch.setattr(regulation_graphics_inputs, 'graphics_assets', lambda _: rows)
    attempt = {'version': 'regulation-model-attempt-v5', 'source_provenance': None,
        'model_provenance': {}, 'model_call_started': False, 'response_received': False,
        'response_text': None, 'response_sha256': None, 'response_bytes': None,
        'response_encoding': None, 'response_over_limit': False,
        'validation_status': 'failed', 'failure_reason': 'model_deadline_exhausted',
        'graphics_plan': contract.prepare_execution_plan(rows, model_policy=policy),
        'graphics_batches': [], 'graphics_failures': [], 'prompt_budget_proof': None}
    runtime._unstarted_partition(rows, attempt['graphics_plan'], attempt, 'model_deadline_exhausted')
    return evidence, attempt


def replay_attempt(evidence, attempt, policy):
    from tradingagents.strategies.orchestration.regulation_validation import _attempt
    return _attempt({'model_attempt': attempt, 'model_provenance': {}}, evidence, None, {},
        assessment_policy='recorded_abstention_v1', graphics_execution_policy='single_target_compact_v1',
        prompt_budget_policy=budget.POLICY, regulation_graphics_model_policy=policy)


@pytest.mark.parametrize('policy', [None, 'sol_high_v1'])
def test_declared_route_replays_complete_failed_chain_without_model_calls(monkeypatch, policy):
    evidence, attempt = failed_graphics_attempt(monkeypatch, policy)
    assert replay_attempt(evidence, attempt, policy) is None


@pytest.mark.parametrize('policy', [None, 'sol_high_v1'])
def test_cross_route_retained_plan_is_rejected_before_replay(monkeypatch, policy):
    evidence, attempt = failed_graphics_attempt(monkeypatch, policy)
    other = 'sol_high_v1' if policy is None else None
    with pytest.raises(ValueError, match='regulatory graphics model policy differs'):
        replay_attempt(evidence, attempt, other)


def completed_graphics(tmp_path, monkeypatch, policy):
    from test_regulation_replay_report import completed
    from test_regulation_evidence import DOC
    from test_regulation_abstention import abstention
    from test_regulation_graphics_grid import assets, response, review_response, provenance
    from tradingagents.strategies.data_sources import regulation_graphics_grid as contract
    from tradingagents.strategies.data_sources import regulation_graphics_inputs
    from tradingagents.strategies.data_sources.regulation_assessment import prepare_request, validate_assessment
    from tradingagents.strategies.data_sources.regulation_evidence import digest
    data, records = completed(tmp_path, monkeypatch)
    ar = config()['autoresearch']
    if policy is not None:
        ar['regulation_graphics_model_policy'] = policy
    graph = data['regulations']['regulation_evidence']
    graph['retrieval_policy'] = ar['regulation_retrieval_policy']
    for original in graph['corpus'].values():
        original['retrieval_policy'] = ar['regulation_retrieval_policy']
        original['manifest_sha256'] = digest({k: v for k, v in original.items() if k != 'manifest_sha256'})
    graph['manifest_sha256'] = digest({k: v for k, v in graph.items() if k != 'manifest_sha256'})
    evidence, images = graph['corpus'][DOC], assets(1)
    monkeypatch.setattr(regulation_graphics_inputs, 'graphics_assets', lambda _: images)
    plan = contract.prepare_execution_plan(images, model_policy=policy)
    def identity(request):
        value = provenance(request)
        if policy is not None:
            value.update(configured_model='gpt-6.1-sol', returned_model='gpt-6.1-sol',
                regulation_graphics_model_policy=policy)
        return value
    request = contract.prepare_recognition(images, plan, 0)
    recognition = contract.validate_recognition(json.dumps(response(request, external=False)), request, identity(request))
    request = contract.prepare_review(images, plan, 0, recognition)
    review = contract.validate_review(json.dumps(review_response(request, recognition)), request, recognition, identity(request))
    bundles = [contract.seal_bundle(0, [{'recognition': {'receipt': recognition}, 'review': {'receipt': review}}], 0)]
    request = prepare_request(evidence, assessment_policy='recorded_abstention_v1',
        prompt_budget_policy=budget.POLICY, graphics_plan=plan, graphics_batches=bundles)
    value = abstention(evidence, graphical=True)
    value['citations'] = [{'unit_id': key, 'quote': text[:15]} for key, text in request.context.unit_texts.items()]
    raw = json.dumps(value)
    assessment = validate_assessment(raw, request.context)
    attempt = {'version': 'regulation-model-attempt-v5',
        'source_provenance': json.loads(request.context.provenance_json), 'model_provenance': {},
        'model_call_started': True, 'response_received': True, 'response_text': raw,
        'response_sha256': hashlib.sha256(raw.encode()).hexdigest(), 'response_bytes': len(raw.encode()),
        'response_encoding': 'utf-8', 'response_over_limit': False, 'validation_status': 'validated',
        'failure_reason': None, 'graphics_plan': plan, 'graphics_batches': bundles,
        'graphics_failures': [], 'prompt_budget_proof': assessment['source_provenance']['prompt_budget_proof']}
    scope = validate_regulation_policy(data, ar)
    for record in records:
        record['evidence']['regulation_source_coverage'] = deepcopy(scope)
        row = record['evidence']['regulation_assessments'][0]
        row.update(assessment=deepcopy(assessment), model_attempt=deepcopy(attempt),
            model_provenance={}, document_assessment=assessment['document_assessment'],
            regulation_evidence_status=assessment['regulation_evidence_status'],
            source_manifest_sha256=graph['manifest_sha256'], evidence_sha256=evidence['manifest_sha256'])
    return data, records, ar


@pytest.mark.scheduled_acceptance
@pytest.mark.parametrize('policy', [None, 'sol_high_v1'])
def test_completed_assessment_binds_profile_through_health_and_frozen_replay(tmp_path, monkeypatch, policy):
    from test_regulation_replay_report import POLICIES, STRATEGIES
    from tradingagents.strategies.orchestration.regulation_validation import validate_regulation_health
    from tradingagents.strategies.learning.llm_analyzer import LLMAnalyzer
    data, records, ar = completed_graphics(tmp_path, monkeypatch, policy)
    before = deepcopy((data, records))
    monkeypatch.setattr(LLMAnalyzer, '_call_llm', lambda *a, **kw: pytest.fail('replay attempted model call'))
    result = validate_regulation_health(data, records, POLICIES, STRATEGIES, ar)
    assert result['assessment_complete'] and not result['decision_evidence_complete']
    assert all(row['assessed_documents'] == row['abstained_documents'] == 1 for row in result['horizons'])
    assert (data, records) == before
    changed = deepcopy(ar)
    if policy is None:
        changed['regulation_graphics_model_policy'] = 'sol_high_v1'
    else:
        changed.pop('regulation_graphics_model_policy')
    with pytest.raises(ValueError, match='invalid_regulation_health') as failure:
        validate_regulation_health(data, records, POLICIES, STRATEGIES, changed)
    assert str(failure.value.__cause__) == 'regulatory graphics model policy differs'


@pytest.mark.scheduled_acceptance
@pytest.mark.parametrize('policy', [None, 'sol_high_v1'])
@pytest.mark.parametrize('cross_route', [False, True])
def test_operational_report_binds_retained_graphics_to_frozen_profile(native_evidence, tmp_path, monkeypatch, policy, cross_route):
    from test_operational_report import _attempt, _report
    from test_prospective_replay_integrity import rewrite_payload
    repo, state, wire = native_evidence
    data, records, ar = completed_graphics(tmp_path / 'engine', monkeypatch, policy)
    rewrite_payload(next((state / 'source_inputs').glob('*.json')), lambda payload: payload.update(data))
    with sqlite3.connect(state / 'metrics_v2.sqlite3') as db:
        for identity, raw in db.execute('SELECT health_id,payload_json FROM strategy_health').fetchall():
            record = json.loads(raw)
            if record['strategy'] == 'commodity_macro':
                record['evidence'].update(regulation_source_coverage=deepcopy(
                    records[0]['evidence']['regulation_source_coverage']), regulation_assessments=[])
            elif record['strategy'] == 'regulatory_pipeline':
                record['evidence'] = deepcopy(records[0]['evidence'])
                record['status'] = records[0]['status']
            else:
                continue
            db.execute('UPDATE strategy_health SET payload_json=? WHERE health_id=?', (json.dumps(record), identity))
    if cross_route:
        if policy is None:
            ar['regulation_graphics_model_policy'] = 'sol_high_v1'
        else:
            ar.pop('regulation_graphics_model_policy')
    profile = {'autoresearch': ar, 'execution': {'mode': 'paper', 'execution_enabled': False}}
    path = state / 'generation-config.json'
    raw = json.dumps(profile, sort_keys=True).encode()
    path.write_bytes(raw)
    manifest_path = state.parent / 'manifest.json'
    manifest = json.loads(manifest_path.read_text())
    manifest['generations'][0].update(config_file=str(path), config_sha256=hashlib.sha256(raw).hexdigest())
    manifest_path.write_text(json.dumps(manifest))
    _attempt(repo, wire)
    report = _report(repo)
    assert ({'code': 'regulation_evidence_conflict'} in report['diagnostics']) is cross_route
    if not cross_route:
        assert report['sources']['regulation_analysis_coverage']['assessment_complete']
