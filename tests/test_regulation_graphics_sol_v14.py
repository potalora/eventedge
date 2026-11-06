"""Constructed multi-original note contract tests; no native accuracy claim."""
from copy import deepcopy
import json
import time

import pytest

from test_regulation_graphics_grid import assets, response, review_response
from test_regulation_graphics_sol_contract import POLICY, sol_provenance
from tradingagents.strategies.data_sources import regulation_graphics_grid as g
from tradingagents.strategies.data_sources.regulation_graphics_material import encode_material, decode_material
from tradingagents.strategies.runtime_deadline import model_budget


def content():
    return dict(status='complete', context_closure='resolved', text=['Totalᵃ', 'Otherᵇ'], tables=[],
        notes=[dict(marker='a', marker_image_position=2, body_fragments=[
            dict(image_position=2, body='Foreign prefix,\n'), dict(image_position=1, body='local suffix.')],
            anchors=[dict(at={'text':0}, marker_text='ᵃ')], scopes=[{'text':0}], consumer_sources=[]),
        dict(marker='b', marker_image_position=1, body_fragments=[
            dict(image_position=1, body='Local prefix,\n'), dict(image_position=3, body='foreign suffix.')],
            anchors=[dict(at={'text':1}, marker_text='ᵇ')], scopes=[{'text':1}], consumer_sources=[])],
        continuations=[], visuals=[], reading_order=[{'text':0}, {'text':1},
            {'note':0,'fragment':1}, {'note':1,'fragment':0}], unresolved=[])


def request():
    rows = assets(3)
    plan = g.prepare_execution_plan(rows, model_policy=POLICY)
    return rows, plan, g.prepare_recognition(rows, plan, 1)


def prepared(value=None):
    rows, plan, q = request()
    rec = g.validate_recognition(json.dumps(content() if value is None else value), q, sol_provenance(q))
    rq = g.prepare_review(rows, plan, 1, rec)
    review = dict(complete=True, context_resolved=True, checked_text=[True for _ in rec['result']['text']],
        checked_tables=[], checked_notes=[dict(object=True, body_fragments=[True for _ in n['body_fragments']])
            for n in rec['result']['notes']], checked_continuations=[], checked_visuals=[],
        checked_reading_order=[True for _ in rec['result']['reading_order']],
        checks={name:True for name in g.REVIEW_CHECKS}, errors=[])
    return rows, plan, q, rec, rq, review


def test_v14_mixed_note_fragments_preserve_identity_source_order_and_lossless_material():
    rows, plan, q, rec, rq, review = prepared()
    assert plan['version'] == 'regulation-graphics-execution-plan-v8'
    assert json.loads(q.context_json)['version'] == 'regulation-graphics-request-v14'
    assert json.loads(rq.user) == {'recognition':content()}
    assert [f['image_position'] for f in rec['result']['notes'][0]['body_fragments']] == [2,1]
    for stage in (q, rq):
        logical = g._logical_request(stage)
        assert logical['model'] == 'gpt-6.1-sol' and logical['effort'] == 'high'
    rev = g.validate_review(json.dumps(review), rq, rec, sol_provenance(rq))
    chain = g.seal_bundle(1, [{'recognition':{'receipt':rec},'review':{'receipt':rev}}], 0)
    before = deepcopy(chain)
    material = g.replay_target(rows, plan, 1, chain)[0]
    assert material['content']['notes'] == content()['notes']
    assert decode_material(encode_material(material)) == material
    assert chain == before
    assert plan['max_corrections'] == 1 and plan['radius'] == 3


def test_v14_systems_teach_fragment_locality_without_old_whole_note_instructions():
    _, _, q, _, rq, _ = prepared()
    for stage in (q, rq):
        assert 'marker_image_position' in stage.system and 'body_fragments' in stage.system
        assert '{"note":0,"fragment":0}' in stage.system
        assert 'logical source reading order' in stage.system
        assert 'body is in image1' not in stage.system
        assert 'checked_text,checked_notes,checked_reading_order are boolean arrays' not in stage.system
    assert 'Each checked_notes item has object' in rq.system


@pytest.mark.parametrize('field', ['marker_image_position','fragment_position'])
@pytest.mark.parametrize('bad', [None, True, False, 0, -1, 4, 1.0, '1'])
def test_note_source_positions_are_nonboolean_in_range_integers(field, bad):
    value = content()
    if field == 'marker_image_position': value['notes'][0][field] = bad
    else: value['notes'][0]['body_fragments'][0]['image_position'] = bad
    _, _, q = request()
    with pytest.raises(g.RecognitionContentError):
        g.validate_recognition(json.dumps(value), q, sol_provenance(q))


@pytest.mark.parametrize('fault', ['missing_marker_position','marker_position_mismatch','empty_fragments',
    'too_many_fragments','missing_fragment_position','missing_fragment_body','extra_fragment_marker',
    'old_note_body','empty_body','whitespace_body','oversized_body','nonstring_body'])
def test_fragment_note_schema_and_marker_provenance_fail_closed(fault):
    value = content(); note = value['notes'][0]; fragment = note['body_fragments'][0]
    if fault == 'missing_marker_position': note.pop('marker_image_position')
    elif fault == 'marker_position_mismatch': note['marker_image_position'] = 1
    elif fault == 'empty_fragments': note['body_fragments'] = []
    elif fault == 'too_many_fragments': note['body_fragments'] *= 33
    elif fault == 'missing_fragment_position': fragment.pop('image_position')
    elif fault == 'missing_fragment_body': fragment.pop('body')
    elif fault == 'extra_fragment_marker': fragment['marker'] = 'a'
    elif fault == 'old_note_body': note['body'] = 'Stitched text'
    elif fault == 'empty_body': fragment['body'] = ''
    elif fault == 'whitespace_body': fragment['body'] = ' \n'
    elif fault == 'oversized_body': fragment['body'] = 'x' * 4097
    else: fragment['body'] = None
    _, _, q = request()
    with pytest.raises(g.RecognitionContentError):
        g.validate_recognition(json.dumps(value), q, sol_provenance(q))


@pytest.mark.parametrize('pointer', [{'note':0}, {'note':0,'fragment':0}, {'note':True,'fragment':1},
    {'note':0,'fragment':True}, {'note':0,'fragment':2}, {'note':0,'fragment':-1}, {'note':2,'fragment':0}])
def test_literal_fragment_pointers_cannot_grant_foreign_or_whole_note_coverage(pointer):
    value = content()
    value['visuals'] = [dict(kind='other', description='Printed detail', precision='qualitative',
        literals=[pointer], relations=[])]
    _, _, q = request()
    with pytest.raises(g.RecognitionContentError):
        g.validate_recognition(json.dumps(value), q, sol_provenance(q))


def test_local_fragment_pointers_work_for_literal_anchors_headers_units_and_relations():
    value = content(); local = {'note':0,'fragment':1}
    value['notes'][0]['body_fragments'][1]['body'] = 'local suffix, $ᵇ.'
    value['notes'][1]['anchors'] = [dict(at=local, marker_text='ᵇ')]
    value['tables'] = [dict(matrix=[['1']], spans=[], headers=[dict(at=local, source=None,
        axis='column', regions=[dict(**{'from':{'row':0,'column':0},'through':{'row':0,'column':0}})], parents=[])],
        units=[dict(at=local, quote='$', regions=[], consumers=[local])])]
    value['reading_order'].append({'table':0})
    value['visuals'] = [dict(kind='footnote', description='Local printed detail', precision='qualitative',
        literals=[local], relations=[dict(kind='note', to=[local])])]
    _, _, q = request()
    rec = g.validate_recognition(json.dumps(value), q, sol_provenance(q))
    assert rec['result']['notes'][1]['anchors'][0]['at'] == local


@pytest.mark.parametrize('fault', ['missing','duplicate','foreign','bare'])
def test_reading_order_covers_every_local_fragment_once_and_no_foreign_fragments(fault):
    value = content()
    if fault == 'missing': value['reading_order'].pop()
    elif fault == 'duplicate': value['reading_order'].append({'note':0,'fragment':1})
    elif fault == 'foreign': value['reading_order'].append({'note':0,'fragment':0})
    else: value['reading_order'][-1] = {'note':1}
    _, _, q = request()
    with pytest.raises(g.RecognitionContentError):
        g.validate_recognition(json.dumps(value), q, sol_provenance(q))


@pytest.mark.parametrize('local_first', [True,False])
def test_local_fragment_reverse_continuation_may_use_foreign_consumers_without_local_anchor(local_first):
    value = content(); note = value['notes'][0]
    if local_first: note['body_fragments'].reverse(); note['marker_image_position'] = 1
    note['anchors'] = []; note['scopes'] = []
    note['consumer_sources'] = [dict(image_position=3, quote='Foreign totalᵃ', marker_text='ᵃ', association='Explicit note consumer')]
    value['reading_order'][2]['fragment'] = 0 if local_first else 1
    _, _, q = request()
    assert g.validate_recognition(json.dumps(value), q, sol_provenance(q))['result']['notes'][0] == note


@pytest.mark.parametrize('fault', [None,'no_anchor','no_scope','consumer_source'])
def test_foreign_only_note_requires_local_anchor_scope_and_no_foreign_consumers(fault):
    value = content(); note = value['notes'][0]
    note['body_fragments'][1]['image_position'] = 3
    value['reading_order'].pop(2)
    if fault == 'no_anchor': note['anchors'] = []
    elif fault == 'no_scope': note['scopes'] = []
    elif fault == 'consumer_source':
        note['consumer_sources'] = [dict(image_position=3, quote='Totalᵃ', marker_text='ᵃ', association='Consumer')]
    _, _, q = request()
    if fault is None: assert g.validate_recognition(json.dumps(value), q, sol_provenance(q))
    else:
        with pytest.raises(g.RecognitionContentError): g.validate_recognition(json.dumps(value), q, sol_provenance(q))


@pytest.mark.parametrize('fault', ['boolean_note','missing_fragment','extra_fragment','nonboolean_fragment','missing_object','extra_field'])
def test_review_note_masks_require_object_and_exact_per_fragment_boolean_shape(fault):
    _, _, _, rec, rq, review = prepared()
    note = review['checked_notes'][0]
    if fault == 'boolean_note': review['checked_notes'][0] = True
    elif fault == 'missing_fragment': note['body_fragments'].pop()
    elif fault == 'extra_fragment': note['body_fragments'].append(True)
    elif fault == 'nonboolean_fragment': note['body_fragments'][0] = 1
    elif fault == 'missing_object': note.pop('object')
    else: note['body'] = True
    with pytest.raises(g.ReviewMalformed): g.validate_review(json.dumps(review), rq, rec, sol_provenance(rq))


@pytest.mark.parametrize('part', ['object',0,1])
def test_false_note_or_fragment_review_rejects_even_when_all_other_checks_pass(part):
    _, _, _, rec, rq, review = prepared()
    if part == 'object': review['checked_notes'][0]['object'] = False
    else: review['checked_notes'][0]['body_fragments'][part] = False
    with pytest.raises(g.ReviewRejected): g.validate_review(json.dumps(review), rq, rec, sol_provenance(rq))


@pytest.mark.parametrize('pointer,exception', [({'note':0,'fragment':1}, g.ReviewRejected),
    ({'note':0,'fragment':0}, g.ReviewMalformed), ({'note':0}, g.ReviewMalformed)])
def test_review_error_locations_respect_local_fragment_boundaries(pointer, exception):
    _, _, _, rec, rq, review = prepared()
    review['errors'] = [dict(at=pointer, issue='Printed note defect')]
    with pytest.raises(exception): g.validate_review(json.dumps(review), rq, rec, sol_provenance(rq))


@pytest.mark.parametrize('pointer', [None,{'text':0}])
def test_foreign_fragment_defect_uses_null_or_affected_local_consumer(pointer):
    _, _, _, rec, rq, review = prepared()
    review['errors'] = [dict(at=pointer,issue='Note 0 fragment 0 in image2 omits printed foreign text.')]
    with pytest.raises(g.ReviewRejected) as caught:
        g.validate_review(json.dumps(review),rq,rec,sol_provenance(rq))
    assert caught.value.issues[0]['issue'].startswith('Note 0 fragment 0 in image2')


def test_fragment_review_correction_uses_fresh_candidate_and_replays_bounded_chain():
    from test_regulation_graphics_grid import provenance
    from test_regulation_graphics_sol_contract import SolAnalyzer
    from test_regulation_graphics_grid_runtime import state, runtime
    class FragmentAnalyzer(SolAnalyzer):
        def _call_llm(self,system,user,**kwargs):
            assert kwargs['regulation_graphics_model_policy']==POLICY
            assert kwargs['max_call_seconds']==240 and kwargs['max_tokens']==4096
            payload=json.loads(user); stage='review'if'recognition'in payload else'recognition'
            serial=len(self.state['calls']);self.state['calls'].append((stage,payload))
            q=g.legacy.GraphicsRequest(system,user,'{}',kwargs['document_inputs'])
            if stage=='recognition':
                value=content()
                if serial==0:value['notes'][0]['body_fragments'][0]['body']='Constructed wrong prefix.'
            else:
                value=review_response(q,{'result':payload['recognition']})
                if serial==1:
                    value['checked_notes'][0]['body_fragments'][0]=False
                    value['errors']=[dict(at=None,issue='Note 0 fragment 0 in image2 differs from the constructed source.')]
            self.last_call_provenance=provenance(q)
            self.last_call_provenance.update(configured_model='gpt-6.1-sol',returned_model='gpt-6.1-sol',
                regulation_graphics_model_policy=POLICY,response_id='constructed-fragment-call-'+str(serial))
            raw=json.dumps(value);self.last_call_response={'received':True,'text':raw}
            return raw
    rows,plan,_=request();shared=state()
    with model_budget(time.monotonic()+10): good,bad=runtime()._target(FragmentAnalyzer(shared),rows,plan,1)
    assert bad is None and good['accepted_round']==1
    assert [row[0]for row in shared['calls']]==['recognition','review','recognition','review']
    assert shared['calls'][2][1]['correction']['issues'][0]['path']=='$'
    assert shared['calls'][3][1]=={'recognition':content()}
    assert g.replay_target(rows,plan,1,good)[0]['content']['notes']==content()['notes']


@pytest.mark.parametrize('budget,over', [('literal',False),('literal',True),('foreign',False),('foreign',True),('structural',False),('structural',True)])
def test_every_fragment_counts_toward_unchanged_aggregate_limits(budget, over):
    value = content()
    if budget == 'literal':
        value['notes'][0]['body_fragments'] = [dict(image_position=2, body='x'*4000) for _ in range(15)]
        value['notes'][0]['body_fragments'].append(dict(image_position=1,body='x'*(3984 if over else 3983)))
        value['notes'] = value['notes'][:1]; value['reading_order'] = [{'text':0},{'text':1},{'note':0,'fragment':15}]
        # Text 12 + marker 1 + added 4 + fragment bodies 63983/63984 = 64000/64001.
        value['text'][1] += 'abcd'
    elif budget == 'foreign':
        for i,note in enumerate(value['notes']):
            note['marker_image_position'] = 2
            note['body_fragments'] = [dict(image_position=2,body='Foreign fragment') for _ in range(64 if i==0 else (63 if over else 62))]
        value['reading_order'] = [{'text':0},{'text':1}]
    else:
        # Four fragments plus 508/509 visuals cross the structural limit exactly.
        value['visuals'] = [dict(kind='other',description='Printed geometry',precision='qualitative',literals=[],relations=[])
            for _ in range(509 if over else 508)]
    _, _, q = request()
    if over:
        with pytest.raises(g.RecognitionContentError) as caught: g.validate_recognition(json.dumps(value), q, sol_provenance(q))
        assert str(caught.value) == 'invalid_regulation_graphics_content_limit'
    else: assert g.validate_recognition(json.dumps(value), q, sol_provenance(q))


@pytest.mark.parametrize('fault', ['body','source','order','marker','review_mask','original_order'])
def test_raw_replay_rejects_tampered_fragments_provenance_and_masks(fault):
    rows, plan, _, rec, rq, review = prepared()
    rev = g.validate_review(json.dumps(review), rq, rec, sol_provenance(rq))
    chain = g.seal_bundle(1,[{'recognition':{'receipt':rec},'review':{'receipt':rev}}],0)
    if fault == 'original_order':
        rows = deepcopy(rows); rows[0], rows[2] = rows[2], rows[0]
    elif fault == 'review_mask': chain['rounds'][0]['review']['receipt']['result']['checked_notes'][0]['body_fragments'][0] = False
    else:
        note = chain['rounds'][0]['recognition']['receipt']['result']['notes'][0]
        if fault == 'body': note['body_fragments'][1]['body'] += '!'
        elif fault == 'source': note['body_fragments'][1]['image_position'] = 3
        elif fault == 'order': note['body_fragments'].reverse()
        else: note['marker_image_position'] = 1
    chain['receipt_sha256'] = g.legacy._digest({k:v for k,v in chain.items() if k!='receipt_sha256'})
    with pytest.raises(ValueError): g.replay_target(rows, plan, 1, chain)


def test_v14_accepts_single_image_note_and_rejects_old_note_shape():
    rows = assets(1); plan = g.prepare_execution_plan(rows,model_policy=POLICY); q = g.prepare_recognition(rows,plan,0)
    value = content(); value['notes'] = value['notes'][:1]
    value['notes'][0].update(marker_image_position=1,body_fragments=[dict(image_position=1,body='One local body.')])
    value['reading_order'] = [{'text':0},{'text':1},{'note':0,'fragment':0}]
    rec = g.validate_recognition(json.dumps(value),q,sol_provenance(q))
    assert len(rec['result']['notes'][0]['body_fragments']) == 1
    value['notes'][0] = dict(marker='a',image_position=1,body='Old body',anchors=[],scopes=[],consumer_sources=[])
    with pytest.raises(g.RecognitionContentError): g.validate_recognition(json.dumps(value),q,sol_provenance(q))


def test_historical_v13_constructor_raw_receipts_and_replay_remain_frozen():
    rows = assets(1); plan = g._execution_plan(rows,POLICY,'regulation-graphics-execution-plan-v7')
    q = g.prepare_recognition(rows,plan,0)
    rec = g.validate_recognition(json.dumps(response(q,external=False)),q,sol_provenance(q))
    rq = g.prepare_review(rows,plan,0,rec)
    rev = g.validate_review(json.dumps(review_response(rq,rec)),rq,rec,sol_provenance(rq))
    chain = g.seal_bundle(0,[{'recognition':{'receipt':rec},'review':{'receipt':rev}}],0)
    # Frozen from actual sealed 26ecaca4276f V7/V13 before implementation.
    assert (plan['plan_sha256'],g.legacy._digest(g._logical_request(q)),g.legacy._digest(g._logical_request(rq)),
        rec['receipt_sha256'],rev['receipt_sha256'],chain['receipt_sha256']) == (
        '3d1eec83bc4efc69932130fda59e399b97f5a830ce2fa76fdc7cccad8d6f5688',
        'fe7b77aa41edd830eb3daf499d7ef40ea973c25c9afaccc6a8ff6a524629adff',
        'ad5dae6d8f01dea1930dc9a816abf2ad406f1dfe7840f4a233d48a4c7f1b95f8',
        'fdab0766dd2395e4079dc00d976936e60f868a882d5718c10fcd60cb7410eeea',
        '7d559239df771e8a63d6f5f63122e498a3393ba0dbb69b566159351f7579b892',
        'aabe2fc75c9e8143ee2935ed7c5d3d6ef19d379dab4593d434c19aacc4406f13')
    before = deepcopy(chain)
    assert g.replay_target(rows,plan,0,chain)[0]['content']['notes'][0]['body'] == 'Note 1'
    assert chain == before


@pytest.mark.parametrize('version', [3,4,5,7])
def test_fragment_pointers_cannot_leak_into_historical_schemas_or_review_errors(version):
    rows=assets(1); plan=g._execution_plan(rows,None if version==3 else POLICY,f'regulation-graphics-execution-plan-v{version}')
    from test_regulation_graphics_grid import provenance
    p=provenance if version==3 else sol_provenance
    q=g.prepare_recognition(rows,plan,0); value=response(q,external=False)
    rec=g.validate_recognition(json.dumps(value),q,p(q)); rq=g.prepare_review(rows,plan,0,rec)
    value['reading_order'][-1]={'note':0,'fragment':0}
    with pytest.raises(g.RecognitionContentError): g.validate_recognition(json.dumps(value),q,p(q))
    value=response(q,external=False)
    value['visuals']=[dict(kind='other',description='Local detail',precision='qualitative',
        literals=[{'note':0,'fragment':0}],relations=[])]
    with pytest.raises(g.RecognitionContentError): g.validate_recognition(json.dumps(value),q,p(q))
    review=review_response(rq,rec); review['errors']=[dict(at={'note':0,'fragment':0},issue='New pointer in old schema')]
    with pytest.raises(g.ReviewMalformed): g.validate_review(json.dumps(review),rq,rec,p(rq))


@pytest.mark.parametrize('version,want', [
    (3,'014f5793443e57955bace69c3cb090f6c7d21c7a8ddab1b18a4a519c06c8c036'),
    (4,'439db1991471732170608486169499c4db493d2e0287b0a394eeb91b1a1595d0'),
    (5,'ddcbcf331129a4f578dfea6fc70baab97557afc5aae9014576e29a3a871d89d0'),
    (7,'9423900581e6608c1a0eb25c0fd96c32bfe218697d0b7014d1db62886a5125f5')])
def test_historical_malformed_reading_order_failure_feedback_and_chain_replay_are_frozen(version,want):
    """Hashes independently frozen from sealed 26ecaca4276f's exact validators."""
    from test_regulation_graphics_grid import provenance
    from test_regulation_graphics_grid_runtime import runtime
    rt=runtime(); rows=assets(1)
    plan=g._execution_plan(rows,None if version==3 else POLICY,f'regulation-graphics-execution-plan-v{version}')
    rounds=[]; correction=None
    for ordinal in range(2):
        q=g.prepare_recognition(rows,plan,0,correction=correction)
        value=response(q,external=False); value['reading_order'][-1]='note:0'; raw=json.dumps(value)
        p=provenance(q) if version==3 else sol_provenance(q)
        with pytest.raises(g.RecognitionContentError) as caught: g.validate_recognition(raw,q,p)
        failure=rt._empty(0,q,'recognition',ordinal)
        failure.update(model_call_started=True,response_received=True,response_text=raw,
            response_sha256=g.legacy.hashlib.sha256(raw.encode()).hexdigest(),response_bytes=len(raw.encode()),
            response_encoding='utf-8',model_provenance=p,failure_reason=str(caught.value),
            failure_class='recognition_content',feedback=caught.value.issues)
        failure['receipt_sha256']=rt._helpers()._seal(failure)
        correction={'recognition':{'failure':failure},'review':None}; rounds.append(correction)
    chain=g.seal_bundle(0,rounds,None); before=deepcopy(chain)
    assert chain['receipt_sha256']==want
    assert rt.replay_target_chain(rows,plan,0,chain) is None
    assert chain==before
