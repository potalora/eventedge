"""V13 request/schema/replay regressions with constructed content only.

These tests establish contract behavior, never original-image accuracy.
"""
from copy import deepcopy
from dataclasses import replace
import json

import pytest

from test_regulation_graphics_grid import assets, response, review_response, provenance
from test_regulation_graphics_sol_contract import POLICY, sol_provenance
from tradingagents.strategies.data_sources import regulation_graphics_grid as g


def prepared(*, external=False):
    rows = assets(2 if external else 1)
    plan = g._execution_plan(rows, POLICY, 'regulation-graphics-execution-plan-v7')
    q = g.prepare_recognition(rows, plan, 0)
    value = response(q, external=external)
    value['tables'][0]['matrix'].append(['Other item', '2'])
    value['notes'][0]['body'] = 'Both reported quantities exclude withdrawn items.'
    # Synthetic applicability crosses rows and differs from the marker column.
    value['notes'][0]['scopes'] = [{'table':0, 'region':{
        'from':{'row':1, 'column':1}, 'through':{'row':2, 'column':1}}}]
    rec = g.validate_recognition(json.dumps(value), q, sol_provenance(q))
    rq = g.prepare_review(rows, plan, 0, rec)
    return rows, plan, q, rec, rq


def test_v13_review_receives_exact_candidate_and_ordinary_schema():
    rows, plan, q, rec, rq = prepared()
    assert plan['version'] == 'regulation-graphics-execution-plan-v7'
    assert json.loads(q.context_json)['version'] == 'regulation-graphics-request-v13'
    assert json.loads(rq.context_json)['version'] == 'regulation-graphics-request-v13'
    assert json.loads(rq.user) == {'recognition':rec['result']}
    for request in (q, rq):
        logical = g._logical_request(request)
        assert logical['model'] == 'gpt-6.1-sol' and logical['effort'] == 'high'
    rev = g.validate_review(json.dumps(review_response(rq, rec)), rq, rec, sol_provenance(rq))
    assert rev['version'] == 'regulation-graphics-review-receipt-v4'
    bundle = g.seal_bundle(0, [{'recognition':{'receipt':rec}, 'review':{'receipt':rev}}], 0)
    before = deepcopy(bundle)
    material = g.replay_target(rows, plan, 0, bundle)
    assert material[0]['content']['notes'] == rec['result']['notes']
    assert bundle == before


def test_v13_adds_scope_clarification_to_both_stages_without_changing_v11_base():
    rows, plan, q, rec, rq = prepared()
    oldplan = g._execution_plan(rows, POLICY, 'regulation-graphics-execution-plan-v5')
    oldq = g.prepare_recognition(rows, oldplan, 0)
    oldrec = g.validate_recognition(json.dumps(response(oldq, external=False)), oldq, sol_provenance(oldq))
    oldrq = g.prepare_review(rows, oldplan, 0, oldrec)
    for request, historical in ((q, oldq), (rq, oldrq)):
        assert request.system.startswith(historical.system)
        guidance = request.system[len(historical.system):]
        assert 'anchors identify' in guidance and 'scopes capture' in guidance
        assert 'ALL quantities or claims' in guidance
        assert 'beyond the column' in guidance
    assert plan['max_corrections'] == 1 and plan['radius'] == oldplan['radius'] == 3


@pytest.mark.parametrize('external', [False, True])
def test_scope_is_independent_of_marker_column_for_local_and_foreign_notes(external):
    _, _, _, rec, rq = prepared(external=external)
    note = rec['result']['notes'][0]
    assert note['anchors'] == [{'at':{'table':0,'row':1,'column':0},'marker_text':'ᵃ'}]
    assert note['scopes'] == [{'table':0,'region':{
        'from':{'row':1,'column':1},'through':{'row':2,'column':1}}}]
    assert note['image_position'] == (2 if external else 1)
    assert json.loads(rq.user)['recognition']['notes'][0] == note
    assert g.validate_review(json.dumps(review_response(rq, rec)), rq, rec, sol_provenance(rq))


def test_ordinary_review_rejects_obsolete_literal_values_as_extra_schema_field():
    _, _, _, rec, rq = prepared()
    value = review_response(rq, rec)
    value['literal_values'] = ['Example', 'Label', 'Count', 'Itemᵃ', '1', 'Other item', '2',
        'a', 'Both reported quantities exclude withdrawn items.', 'ᵃ']
    with pytest.raises(g.ReviewMalformed):
        g.validate_review(json.dumps(value), rq, rec, sol_provenance(rq))


def test_negative_scope_review_remains_rejected_with_local_feedback():
    _, _, _, rec, rq = prepared()
    value = review_response(rq, rec)
    value.update(complete=False, checked_notes=[False], errors=[{
        'at':{'table':0,'row':2,'column':1}, 'issue':'Note scope omits another qualified quantity.'}])
    with pytest.raises(g.ReviewRejected) as caught:
        g.validate_review(json.dumps(value), rq, rec, sol_provenance(rq))
    assert caught.value.issues == [{'path':'{"column":1,"row":2,"table":0}',
        'issue':'Note scope omits another qualified quantity.'}]


def test_successor_rejects_v6_plan_instead_of_reinterpreting_v12_as_v13():
    rows = assets(1)
    plan = g.prepare_execution_plan(rows, model_policy=POLICY)
    plan['version'] = 'regulation-graphics-execution-plan-v6'
    plan['plan_sha256'] = g.legacy._digest({k:v for k,v in plan.items() if k != 'plan_sha256'})
    with pytest.raises(ValueError): g.model_policy(plan)
    with pytest.raises(ValueError): g.prepare_recognition(rows, plan, 0)
    with pytest.raises(ValueError):
        g._execution_plan(rows, POLICY, 'regulation-graphics-execution-plan-v6')


@pytest.mark.parametrize('stage', ['recognition', 'review'])
def test_successor_rejects_v12_request_even_with_resealed_identity(stage):
    _, _, q, rec, rq = prepared()
    request = q if stage == 'recognition' else rq
    context = json.loads(request.context_json)
    context['version'] = 'regulation-graphics-request-v12'
    context['request_identity_sha256'] = g.legacy._digest({k:v for k,v in context.items() if k != 'request_identity_sha256'})
    wrong = replace(request, context_json=g.legacy._canonical(context))
    with pytest.raises(ValueError):
        if stage == 'recognition': g.validate_recognition(rec['response_text'], wrong, sol_provenance(wrong))
        else: g.validate_review(json.dumps(review_response(rq, rec)), wrong, rec, sol_provenance(wrong))
    with pytest.raises(ValueError): g._request_system(stage, 'regulation-graphics-request-v12')


# Frozen by executing the actual 409b1e5a8d5e V11 module's constructors.
# Inputs and raw JSON below are constructed; these hashes make no native claim.
FROZEN = {
    3: ('50d9961dc8fcb674d4f10b72b5659ccd7cb742c944ce73741febd073f3683a0e',
        '83d270cce0b92c97134eae74c7a4e006be8ae114d81a3484b0896874620e6a78',
        'f1d4853ece68a6f1a84547f3bcb74d79d94337826efca65548bba2397690c26d',
        '4e1fc1be168f74c060fc6a85435bac484dfe9bdc7964d3e29aed250ca93b6e1b'),
    4: ('e5c1246e2478a5099f688d7f4a991230ebec95926cc76ade0f8b45f0310a05d8',
        '9d90540d9fa88160beec71b641dee03cf0b3a91e56b38a2c7081ec899b28fdda',
        'd3d9311e0df543c6f308bd1ce7a203220b92829dbd511bc8d409ff6faa2553ba',
        '4dafdce6a968cb7dcf0af7c9b80cf60ef242430540280ff4aaef476fe1b5ef46'),
    5: ('23a33eec780a00f71b294c4bdec00035875710a082b260cb3626184ed1de4dd1',
        '8951eb22b9676975cd955c95076c4a7826b4e1b364b35f02e4dc9bf71ce7266a',
        'f15005dc51ea0a9aaa2185002e5e11fdbf20b98f05428da3660925f10a60262e',
        '946cffb2afecb4efbfe1e7c9ad31eec5f549893b81c786765ed3ea091861f518'),
}


@pytest.mark.parametrize('version', [3, 4, 5])
def test_historical_v9_v10_v11_raw_receipts_and_replay_remain_exact(version):
    rows = assets(1)
    plan = g._execution_plan(rows, None if version == 3 else POLICY,
        f'regulation-graphics-execution-plan-v{version}')
    p = provenance if version == 3 else sol_provenance
    q = g.prepare_recognition(rows, plan, 0)
    rec = g.validate_recognition(json.dumps(response(q, external=False)), q, p(q))
    rq = g.prepare_review(rows, plan, 0, rec)
    rev = g.validate_review(json.dumps(review_response(rq, rec)), rq, rec, p(rq))
    bundle = g.seal_bundle(0, [{'recognition':{'receipt':rec}, 'review':{'receipt':rev}}], 0)
    assert (g.legacy._digest(g._logical_request(rq)), rec['receipt_sha256'],
        rev['receipt_sha256'], bundle['receipt_sha256']) == FROZEN[version]
    before = deepcopy(bundle)
    assert g.replay_target(rows, plan, 0, bundle)[0]['content']['tables'][0]['matrix'][1][1] == '1'
    assert bundle == before
