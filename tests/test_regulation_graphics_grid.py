"""Compact content contracts use constructed output, never native accuracy claims."""
from copy import deepcopy
import json
import pytest
from tradingagents.strategies.data_sources import regulation_graphics_grid as g
from test_regulation_graphics import assets as demo_assets,provenance as _old_provenance

def provenance(request):
    value=_old_provenance(request)
    value['response_id']='constructed-'+g.legacy._digest({'request':request.context_json,'stage':request.system})
    return value


def assets(count=4):
    first=demo_assets()[0]
    return [dict(first,id=f'EP06OC26.{88+i:03d}',filename=f'EP06OC26.{88+i:03d}.png')for i in range(count)]


def response(request,*,external=True):
    return {'status':'complete','context_closure':'resolved','text':['Example'],
        'tables':[{'matrix':[['Label','Count'],['Itemᵃ','1']], 'spans':[],
          'headers':[{'at':{'table':0,'row':0,'column':1},'source':None,'axis':'column',
             'regions':[{'from':{'row':1,'column':1},'through':{'row':'last','column':1}}],'parents':[]}],
          'units':[]}],
        'notes':[{'marker':'a','body':'Note 1','image_position':2 if external else 1,
             'anchors':[{'at':{'table':0,'row':1,'column':0},'marker_text':'ᵃ'}],
             'scopes':[{'table':0,'region':{'from':{'row':1,'column':0},'through':{'row':1,'column':'last'}}}],
             'consumer_sources':[]}],
        'continuations':[],'visuals':[],
        'reading_order':[{'text':0},{'table':0}]+([]if external else[{'note':0}]),'unresolved':[]}


def review_response(request,recognition):
    r=recognition['result']
    result={'complete':True,'context_resolved':True,'checked_text':[True for _ in r['text']],
        'checked_tables':[{'cells':[[True for _ in row]for row in t['matrix']],
           'spans':[True for _ in t['spans']],'headers':[True for _ in t['headers']],
           'units':[True for _ in t['units']]}for t in r['tables']],
        'checked_notes':[True for _ in r['notes']],
        'checked_continuations':[{'before':[True for _ in x['before']],'after':[True for _ in x['after']]}for x in r['continuations']],
        'checked_visuals':[{'object':True,'relations':[True for _ in x['relations']]}for x in r['visuals']],
        'checked_reading_order':[True for _ in r['reading_order']],
        'checks':{name:True for name in g.REVIEW_CHECKS},'errors':[]}
    return result


def bundle(rows,plan,index,response_factory=None):
    q=g.prepare_recognition(rows,plan,index)
    rec=g.validate_recognition(json.dumps(response_factory(q)if response_factory else response(q,external=len(rows)>1)),q,provenance(q))
    q=g.prepare_review(rows,plan,index,rec)
    rev=g.validate_review(json.dumps(review_response(q,rec)),q,rec,provenance(q))
    return g.seal_bundle(index,[{'recognition':{'receipt':rec},'review':{'receipt':rev}}],0)


def test_full_compact_review_replay_and_context_credit_zero():
    rows=assets();plan=g.prepare_execution_plan(rows);bs=[bundle(rows,plan,i)for i in range(len(rows))]
    mats=g.replay_document(rows,plan,bs)
    assert [m['id']for m in mats]==[a['id']for a in rows]
    assert mats[0]['content']['tables'][0]['matrix'][1][1]=='1'
    assert mats[0]['sources'][0]['id']==rows[0]['id'] and 'reading_order'in mats[0]['content']
    assert 'request_identity_sha256'not in json.loads(bs[0]['rounds'][0]['recognition']['receipt']['response_text'])
    for wrong in (bs[:-1],bs[::-1],bs+[bs[0]]):
        with pytest.raises(ValueError):g.replay_document(rows,plan,wrong)


@pytest.mark.parametrize('fault',['partial','closure','ragged','bad_span','covered_printing','cycle','range','source_index','no_anchor','unused_order','duplicate_order','marker','orphan_note'])
def test_strict_content_scopes_and_identity(fault):
    rows=assets();q=g.prepare_recognition(rows,g.prepare_execution_plan(rows),0);v=response(q);t=v['tables'][0]
    if fault=='partial':v['status']='partial'
    elif fault=='closure':v['context_closure']='unresolved'
    elif fault=='ragged':t['matrix'][1].pop()
    elif fault=='bad_span':t['spans']=[{'anchor':{'row':0,'column':0},'through':{'row':9,'column':0}}]
    elif fault=='covered_printing':t['spans']=[{'anchor':{'row':0,'column':0},'through':{'row':0,'column':1}}]
    elif fault=='cycle':t['headers'][0]['parents']=[0]
    elif fault=='range':t['headers'][0]['regions'][0]['through']['row']=99
    elif fault=='source_index':v['notes'][0]['image_position']=99
    elif fault=='no_anchor':v['notes'][0]['anchors']=[]
    elif fault=='unused_order':v['reading_order']=[]
    elif fault=='duplicate_order':v['reading_order'].append({'table':0})
    elif fault=='marker':v['notes'][0]['anchors'][0]['marker_text']='z'
    elif fault=='orphan_note':v['notes'][0]['scopes']=[]
    with pytest.raises(g.RecognitionContentError):g.validate_recognition(json.dumps(v),q,provenance(q))


def test_review_negative_is_distinct_from_malformed_and_integrity_terminal():
    rows=assets();q=g.prepare_recognition(rows,g.prepare_execution_plan(rows),0)
    rec=g.validate_recognition(json.dumps(response(q)),q,provenance(q));rq=g.prepare_review(rows,g.prepare_execution_plan(rows),0,rec)
    v=review_response(rq,rec);v['complete']=False;v['errors']=[{'at':None,'issue':'Actual omitted printing'}]
    with pytest.raises(g.ReviewRejected):g.validate_review(json.dumps(v),rq,rec,provenance(rq))
    v['checked_tables'][0]['cells'].pop()
    with pytest.raises(g.ReviewMalformed):g.validate_review(json.dumps(v),rq,rec,provenance(rq))
    bad=provenance(q);bad['returned_model']='wrong'
    with pytest.raises(ValueError)as err:g.validate_recognition(json.dumps(response(q)),q,bad)
    assert not isinstance(err.value,g.RecognitionContentError)


def test_convergent_header_dag_is_bounded_and_cycle_rejected():
    rows=assets();q=g.prepare_recognition(rows,g.prepare_execution_plan(rows),0);v=response(q)
    header=v['tables'][0]['headers'][0]
    v['tables'][0]['headers']=[dict(deepcopy(header),parents=[]if i<2 else[i-1,i-2])for i in range(48)]
    rec=g.validate_recognition(json.dumps(v),q,provenance(q));assert len(rec['result']['tables'][0]['headers'])==48
    v['tables'][0]['headers'][0]['parents']=[47]
    with pytest.raises(g.RecognitionContentError):g.validate_recognition(json.dumps(v),q,provenance(q))


def test_replay_rejects_tamper_final_round_and_duplicate_keys():
    rows=assets();p=g.prepare_execution_plan(rows);b=bundle(rows,p,0)
    b['rounds'][0]['recognition']['receipt']['response_text']='{}'
    with pytest.raises(ValueError):g.replay_target(rows,p,0,b)
    q=g.prepare_recognition(rows,p,0)
    with pytest.raises(g.RecognitionContentError):g.validate_recognition('{"status":"complete","status":"partial"}',q,provenance(q))


@pytest.mark.parametrize('fault',['parent_object','visual_kind_object','relation_kind_object','forward_note_schema','forward_visual_schema'])
def test_malformed_content_is_typed_actionable_not_terminal_internal_error(fault):
    rows=assets();q=g.prepare_recognition(rows,g.prepare_execution_plan(rows),0);v=response(q)
    if fault=='parent_object':v['tables'][0]['headers'][0]['parents']=[{}]
    elif fault=='forward_note_schema':
        v['tables'][0]['units']=[{'at':{'note':0},'quote':'a','regions':[],'consumers':[{'table':0}]}];v['notes'][0]={}
    else:
        v['visuals']=[{'kind':{},'description':'Visible axis','precision':'qualitative','literals':[],'relations':[]}]
        if fault=='relation_kind_object':v['visuals'][0].update(kind='axis',relations=[{'kind':{},'to':[{'table':0}]}])
        elif fault=='forward_visual_schema':
            v['visuals']=[{'kind':'series','description':'Curve','precision':'qualitative','literals':[],
                'relations':[{'kind':'axis','to':[{'visual':1}]}]},{}]
    with pytest.raises(g.RecognitionContentError)as error:g.validate_recognition(json.dumps(v),q,provenance(q))
    assert error.value.issues[0]['path'].startswith('$')


def test_request_bound_and_native_response_identity_are_integrity_checks(monkeypatch):
    rows=assets();plan=g.prepare_execution_plan(rows)
    monkeypatch.setattr(g,'MAX_REQUEST_TEXT_BYTES',5)
    with pytest.raises(ValueError,match='request_limit'):g.prepare_recognition(rows,plan,0)
    monkeypatch.undo();q=g.prepare_recognition(rows,plan,0);p=provenance(q);p.pop('response_id',None)
    with pytest.raises(ValueError,match='model_provenance'):g.validate_recognition(json.dumps(response(q)),q,p)


def test_v9_prompt_has_unambiguous_json_shapes_and_strict_blank_header_guidance():
    rows=assets();q=g.prepare_recognition(rows,g.prepare_execution_plan(rows),0)
    assert g.REQUEST_VERSION=='regulation-graphics-request-v9'
    for literal in ('{"text":0}','{"note":0}','{"table":0,"row":0,"column":0}'):
        assert literal in g._RECOGNITION_SYSTEM and literal in g._REVIEW_SYSTEM
    assert 'MUST be JSON null'in q.system and 'never colon-delimited strings'in q.system
    assert 'double-quoted'in q.system and 'Do not copy these examples'in q.system
    assert 'hierarchical grouping'in q.system and 'parents'in q.system
    assert 'A heading atop a stub/label column is still a column header'in q.system
    assert 'Do not invent headings'in q.system
    assert 'location={text:i}'not in q.system and 'at:location'not in g._REVIEW_SYSTEM


@pytest.mark.parametrize('fault',['empty_string_blank','colon_string_location'])
def test_observed_v8_shape_failures_stay_rejected_with_actionable_feedback(fault):
    rows=assets();q=g.prepare_recognition(rows,g.prepare_execution_plan(rows),0);v=response(q)
    if fault=='empty_string_blank':v['tables'][0]['matrix'][1][1]=''
    else:v['reading_order'][0]='text:0'
    with pytest.raises(g.RecognitionContentError)as error:g.validate_recognition(json.dumps(v),q,provenance(q))
    issue=error.value.issues[0]
    if fault=='empty_string_blank':
        assert issue['path']=='$.tables[0].matrix[1][1]'and'JSON null'in issue['issue']
    else:assert issue['path']=='$.reading_order[0]'and'JSON object'in issue['issue']


def test_v9_review_mask_examples_are_real_json_and_not_answer_inventory():
    for raw in ('{"cells":[[true]],"spans":[],"headers":[],"units":[]}',
                '{"before":[],"after":[]}','{"object":true,"relations":[]}'):
        assert raw in g._REVIEW_SYSTEM
        assert type(json.loads(raw))is dict
    assert 'NOT counts/answers to copy'in g._REVIEW_SYSTEM
    assert 'include them in target text ONLY if actually printed'in g._RECOGNITION_SYSTEM
