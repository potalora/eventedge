"""Versioned instruction selection; unchanged compact acceptance and raw replay."""
from copy import deepcopy
import json
import time

import pytest

from test_regulation_graphics_grid import assets, response
from test_regulation_graphics_sol_contract import POLICY, SolAnalyzer, sol_provenance
from test_regulation_graphics_grid_runtime import runtime, state, memo_setup
from tradingagents.strategies.data_sources import regulation_graphics_grid as g
from tradingagents.strategies.runtime_deadline import model_budget


def retained_v4(rows):
    plan = g.prepare_execution_plan(rows, model_policy=POLICY)
    plan['version'] = 'regulation-graphics-execution-plan-v4'
    plan['plan_sha256'] = g.legacy._digest({k:v for k,v in plan.items() if k != 'plan_sha256'})
    return plan


def retained_v5(rows):
    plan = g.prepare_execution_plan(rows, model_policy=POLICY)
    plan['version'] = 'regulation-graphics-execution-plan-v5'
    plan['plan_sha256'] = g.legacy._digest({k:v for k,v in plan.items() if k != 'plan_sha256'})
    return plan


def test_v11_mechanical_supplement_is_sol_only_and_old_v10_identity_is_frozen():
    rows = assets(1)
    luna = g.prepare_recognition(rows, g.prepare_execution_plan(rows), 0)
    historical = retained_v4(rows)
    old = g.prepare_recognition(rows, historical, 0)
    fresh = g.prepare_recognition(rows, retained_v5(rows), 0)
    # Frozen from 409b1e5a8d5e's actual V5/V11 constructor, not V12 output.
    assert retained_v5(rows)['plan_sha256'] == '326a7f34081ff39b04df62d67ac5409f21d2471526cb641cd2ae744ced559d93'
    assert json.loads(fresh.context_json)['request_identity_sha256'] == '2145e684d5e8e8f13fd6f75b591e9cd70804156fc6240b93f00dda4a52b8be39'
    assert g.legacy._digest(g._logical_request(fresh)) == '1157b62306d0e07ab4ea15251f8d9e57d4b6f7dc7c782ead1a6a88b0d479fd2e'
    assert historical['plan_sha256'] == '0406ed038b59ee91fa0968a9178b2d78c5e74bc82f3ab30af64bb8bd166efd03'
    assert json.loads(old.context_json)['request_identity_sha256'] == 'cbcb11e30f489d4848f36047b93bcbebf9213d7494a3c856497859487c1d18d7'
    assert g.legacy._digest(g._logical_request(old)) == '51804a7caf98fb213528f6a889b3c71e0f475c55290801818a9019684eea7ac2'
    assert old.system == luna.system
    assert fresh.system.startswith(old.system) and fresh.system != old.system
    assert 'geometry and endpoint' in fresh.system
    assert '2–64' in fresh.system
    assert 'body is in image1' in fresh.system
    assert 'entire object graph' in fresh.system
    assert 'grammar' in fresh.system and 'punctuation' in fresh.system
    assert g._plan_request_version(historical) == 'regulation-graphics-request-v10'
    assert g._plan_request_version(retained_v5(rows)) == 'regulation-graphics-request-v11'


def test_mechanical_supplement_does_not_relax_singleton_geometry_or_foreign_note_pointers():
    rows = assets(2); plan = retained_v5(rows)
    q = g.prepare_recognition(rows, plan, 0)
    base = dict(status='complete', context_closure='resolved', text=['Printed a'], tables=[], notes=[],
                continuations=[], visuals=[], reading_order=[{'text':0}], unresolved=[])
    value = deepcopy(base)
    value['visuals'] = [dict(kind='other', description='Visible relationship', precision='qualitative', literals=[],
                            relations=[{'kind':'geometry','to':[{'text':0}]}])]
    with pytest.raises(g.RecognitionContentError) as caught:
        g.validate_recognition(json.dumps(value), q, sol_provenance(q))
    assert caught.value.issues[0]['path'] == '$.visuals[0].relations[0].to'
    value = deepcopy(base)
    value['notes'] = [dict(marker='a', body='Foreign body', image_position=2,
        anchors=[{'at':{'text':0},'marker_text':'a'}], scopes=[{'text':0}], consumer_sources=[])]
    value['visuals'] = [dict(kind='footnote', description='Printed raised marker', precision='qualitative',
        literals=[{'text':0}], relations=[{'kind':'note','to':[{'note':0}]}])]
    with pytest.raises(g.RecognitionContentError) as caught:
        g.validate_recognition(json.dumps(value), q, sol_provenance(q))
    assert caught.value.issues[0]['path'] == '$.visuals[0].relations[0].to[0]'


@pytest.mark.parametrize('old', [True,False])
def test_old_v10_and_new_v11_failed_chains_replay_without_rewriting(old):
    rows=assets(1); plan=retained_v4(rows) if old else retained_v5(rows)
    with model_budget(time.monotonic()+10):
        good,bad=runtime()._target(SolAnalyzer(state(fail=0)),rows,plan,0)
    before=deepcopy(bad)
    assert good is None and len(bad['rounds']) == 2
    assert runtime().replay_target_chain(rows,plan,0,bad) is None and bad == before
    expected='regulation-graphics-request-v10' if old else 'regulation-graphics-request-v11'
    assert all(r['recognition']['failure']['request_identity']['version'] == expected for r in bad['rounds'])
    other=retained_v5(rows) if old else retained_v4(rows)
    with pytest.raises(ValueError): runtime().replay_target_chain(rows,other,0,bad)


def test_v11_review_and_request_tampering_select_exact_versioned_system():
    rows=assets(1); plan=retained_v5(rows)
    q=g.prepare_recognition(rows,plan,0)
    rec=g.validate_recognition(json.dumps(response(q,external=False)),q,sol_provenance(q))
    rq=g.prepare_review(rows,plan,0,rec)
    assert json.loads(rq.context_json)['request_identity_sha256'] == '991c9bfb0fbd23c31c4aa819d23a83d06f5d56781bbad0f07ce9f439c797965f'
    assert g.legacy._digest(g._logical_request(rq)) == '23a33eec780a00f71b294c4bdec00035875710a082b260cb3626184ed1de4dd1'
    assert rq.system.startswith(g._REVIEW_SYSTEM) and rq.system != g._REVIEW_SYSTEM
    assert 'body is in image1' in rq.system and '4096' in rq.system
    for system,context in [(g._RECOGNITION_SYSTEM,json.loads(q.context_json)),(q.system+'tampered',json.loads(q.context_json))]:
        wrong=g.legacy.GraphicsRequest(system,q.user,g.legacy._canonical(context),q.document_inputs)
        with pytest.raises(ValueError):g.validate_recognition(json.dumps(response(q,external=False)),wrong,sol_provenance(q))
    context=json.loads(q.context_json);context['version']='regulation-graphics-request-v10'
    context['request_identity_sha256']=g.legacy._digest({k:v for k,v in context.items() if k!='request_identity_sha256'})
    wrong=g.legacy.GraphicsRequest(q.system,q.user,g.legacy._canonical(context),q.document_inputs)
    with pytest.raises(ValueError):g.validate_recognition(json.dumps(response(q,external=False)),wrong,sol_provenance(q))


def test_memo_binds_retained_request_version_and_rejects_cross_version_partition(monkeypatch):
    scope,begin,end=memo_setup(monkeypatch);rt=runtime();rows=assets(1);shared=state();analyzer=SolAnalyzer(shared)
    with model_budget(time.monotonic()+10),scope():
        token=begin('regulation','thesis','doc-id',False)
        try:
            oldplan=retained_v5(rows);newplan=g.prepare_execution_plan(rows,model_policy=POLICY)
            oldkey=rt._memo_key(analyzer,rows,oldplan)[1];newkey=rt._memo_key(analyzer,rows,newplan)[1]
            assert oldkey != newkey
            good,bad=rt._target(analyzer,rows,oldplan,0);assert bad is None
            from tradingagents.strategies.candidate_response_reuse import _MEMO
            _MEMO.get().graphics_entries[newkey]={'graphics_plan':oldplan,'graphics_batches':[good],'graphics_failures':[]}
            count=len(shared['calls'])
            with pytest.raises(ValueError):rt.recognize_graphics(analyzer,rows,{})
            assert len(shared['calls']) == count
        finally:end(token)
