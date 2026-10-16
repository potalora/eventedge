"""Bounded image material contracts; constructed transport responses, no models."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from tradingagents.strategies.data_sources import regulation_graphics as graphics
from tradingagents.strategies.plan_document_inputs import document_input_metadata, encode_document_inputs


def assets():
    data = (Path(__file__).parent / 'fixtures/regulation_graphics_demo.png').read_bytes()
    return [{'id':'DEMO.001', 'filename':'DEMO.001.png',
             'sha256':hashlib.sha256(data).hexdigest(), 'data':data,
             'context':'Table 1. Production counts; years are unitless.'}]


def provenance(request):
    return {'configured_model':'gpt-6-luna','returned_model':'gpt-6-luna',
        'reasoning_effort':'high','billing_mode':'chatgpt_plan','role':'bounded',
        'response_id':'constructed-only',
        'document_inputs':document_input_metadata(encode_document_inputs(request.document_inputs))}


def recognition_response(request):
    context = json.loads(request.context_json)
    def element(ident,kind,text,refs=(),precision='not_numeric',source='image'):
        return {'id':ident,'kind':kind,'text':text,'references':list(refs),
                'precision':precision,'source':source}
    return {'version':graphics.RECOGNITION_VERSION,
        'request_identity_sha256':context['request_identity_sha256'],
        'graphics':[{'id':assets()[0]['id'],'sha256':assets()[0]['sha256'],'status':'complete',
            'kind':'table','transcript':'Production counts\nYear\nCount\n2024\n12\nunits\nCounts are reported as units.',
            'elements':[element('title','heading','Production counts'),
                element('year','column','Year'),element('count','column','Count'),
                element('row','row','2024',precision='printed'),element('unit','unit','units'),
                element('y','cell','2024',('row','year'),precision='printed'),
                element('n','cell','12',('row','count','unit'),precision='printed'),
                element('note','footnote','Counts are reported as units.')],
            'unresolved':[]}]}


def review_response(request, recognition):
    context = json.loads(request.context_json)
    return {'version':graphics.REVIEW_VERSION,
        'request_identity_sha256':context['request_identity_sha256'],
        'recognition_receipt_sha256':recognition['receipt_sha256'],
        'graphics':[{'id':assets()[0]['id'],'sha256':assets()[0]['sha256'],
            'material_complete':True,
            'checked_element_ids':[e['id']for e in recognition['result']['graphics'][0]['elements']],
            'checks':{name:True for name in graphics.REVIEW_CHECKS},'errors':[]}]}


def bundle():
    request = graphics.prepare_graphics_recognition(assets())
    recognized = graphics.validate_graphics_recognition(json.dumps(recognition_response(request)),
        request, provenance(request))
    reviewed_request = graphics.prepare_graphics_review(assets(), recognized)
    reviewed = graphics.validate_graphics_review(json.dumps(review_response(reviewed_request, recognized)),
        reviewed_request, recognized, provenance(reviewed_request))
    return {'recognition':recognized,'review':reviewed}


def reseal(receipt):
    receipt['receipt_sha256'] = hashlib.sha256(json.dumps(
        {k:v for k,v in receipt.items()if k!='receipt_sha256'},sort_keys=True,
        separators=(',',':'),ensure_ascii=True,allow_nan=False).encode()).hexdigest()


def test_exact_image_backed_requests_and_complete_machine_review_replay():
    request = graphics.prepare_graphics_recognition(assets())
    assert request.document_inputs[0].data == assets()[0]['data']
    assert 'years are unitless' in request.user
    value = bundle()
    materials = graphics.replay_graphics_receipt(assets(), value)
    assert materials == [{k:v for k,v in value['recognition']['result']['graphics'][0].items()
                          if k in {'id','sha256','transcript','elements'}}]
    for receipt in value.values():
        assert receipt['machine_assessed'] is True
        assert receipt['correlated_model_risk'] is True
        assert receipt['guarantees_accuracy'] is False
        assert receipt['trading_authority'] is False
        assert receipt['response_text'] and receipt['model_request']['request_sha256']
    text = graphics.normalized_material_text(assets(), value)
    assert 'Production counts' in text and 'Counts are reported as units.' in text
    assert '"unit"' in text and '"2024"' in text


def test_review_has_original_images_and_all_transcript_but_no_trading_authority():
    value = bundle();request = graphics.prepare_graphics_review(assets(), value['recognition'])
    assert request.document_inputs[0].data == assets()[0]['data']
    assert value['recognition']['result']['graphics'][0]['transcript'] in json.loads(request.user)['recognition']['graphics'][0]['transcript']
    assert 'independent image-backed review' in request.system.lower()
    assert 'not blind' in request.system.lower()
    assert 'trading' in request.system and 'issuer' in request.system


@pytest.mark.parametrize('fault',['missing','extra','duplicate','reordered','hash','partial','unreadable',
    'unresolved','invented_literal','missing_row','missing_column','dangling_link','duplicate_element',
    'false_numeric_precision','precise_visual_number','empty_elements','unknown_key','context_invention'])
def test_missing_ambiguous_or_malformed_material_never_becomes_complete(fault):
    request = graphics.prepare_graphics_recognition(assets());raw = recognition_response(request)
    graphic = raw['graphics'][0]
    if fault=='missing':raw['graphics']=[]
    elif fault=='extra':raw['graphics'].append(dict(graphic,id='extra'))
    elif fault=='duplicate':raw['graphics'].append(deepcopy(graphic))
    elif fault=='reordered':graphic['id']='other'
    elif fault=='hash':graphic['sha256']='0'*64
    elif fault in {'partial','unreadable'}:graphic['status']=fault
    elif fault=='unresolved':graphic['unresolved']=['Clipped continuation column.']
    elif fault=='invented_literal':graphic['elements'][6]['text']='13'
    elif fault=='missing_row':graphic['elements'][6]['references']=['count','unit']
    elif fault=='missing_column':graphic['elements'][6]['references']=['row','unit']
    elif fault=='dangling_link':graphic['elements'][6]['references'].append('unknown')
    elif fault=='duplicate_element':graphic['elements'].append(deepcopy(graphic['elements'][0]))
    elif fault=='false_numeric_precision':graphic['elements'][6]['precision']='not_numeric'
    elif fault=='precise_visual_number':graphic['elements'][6].update(source='visual',text='13',precision='printed')
    elif fault=='empty_elements':graphic['elements']=[]
    elif fault=='unknown_key':graphic['approved']=True
    else:graphic['elements'][6].update(source='context',text='not in retained context')
    with pytest.raises(ValueError,match='invalid_regulation_graphics_'):
        graphics.validate_graphics_recognition(json.dumps(raw),request,provenance(request))


@pytest.mark.parametrize('fault',['incomplete','false_check','missed_element','extra_element',
    'duplicate_element','errors','wrong_recognition','wrong_hash','wrong_id','bool_string'])
def test_review_must_cover_every_material_element_without_any_error(fault):
    value=bundle();recognition=value['recognition'];request=graphics.prepare_graphics_review(assets(),recognition)
    raw=review_response(request,recognition);row=raw['graphics'][0]
    if fault=='incomplete':row['material_complete']=False
    elif fault=='false_check':row['checks']['printed_text']=False
    elif fault=='missed_element':row['checked_element_ids'].pop()
    elif fault=='extra_element':row['checked_element_ids'].append('unknown')
    elif fault=='duplicate_element':row['checked_element_ids'].append(row['checked_element_ids'][0])
    elif fault=='errors':row['errors']=[{'element_id':'n','issue':'Value is clipped.'}]
    elif fault=='wrong_recognition':raw['recognition_receipt_sha256']='0'*64
    elif fault=='wrong_hash':row['sha256']='0'*64
    elif fault=='wrong_id':row['id']='other'
    else:row['material_complete']='true'
    with pytest.raises(ValueError,match='invalid_regulation_graphics_'):
        graphics.validate_graphics_review(json.dumps(raw),request,recognition,provenance(request))


@pytest.mark.parametrize('fault',['model','returned_model','effort','billing','role','images','request'])
def test_exact_luna_high_plan_request_and_image_provenance_required(fault):
    request=graphics.prepare_graphics_recognition(assets());p=provenance(request)
    key={'model':'configured_model','returned_model':'returned_model','effort':'reasoning_effort',
         'billing':'billing_mode','role':'role'}.get(fault)
    if key:p[key]='wrong'
    elif fault=='images':p['document_inputs'][0]['sha256']='0'*64
    else:p['model_request']={'request_sha256':'0'*64}
    with pytest.raises(ValueError,match='invalid_regulation_graphics_'):
        graphics.validate_graphics_recognition(json.dumps(recognition_response(request)),request,p)


@pytest.mark.parametrize('fault',['response','request','provenance','recognition_link','flags','reorder_assets','context'])
def test_resealed_bundle_tampering_cannot_pass_pure_replay(fault):
    value=bundle();input_assets=assets();receipt=value['review']
    if fault=='response':receipt['response_text']=receipt['response_text'].replace('true','false',1)
    elif fault=='request':receipt['model_request']['prompt_sha256']='0'*64
    elif fault=='provenance':receipt['model_provenance']['reasoning_effort']='low'
    elif fault=='recognition_link':receipt['request_identity']['recognition_receipt_sha256']='0'*64
    elif fault=='flags':receipt['guarantees_accuracy']=True
    elif fault=='reorder_assets':input_assets[0]['id']='other'
    else:input_assets[0]['context']='different surrounding retained text'
    reseal(receipt)
    with pytest.raises(ValueError,match='invalid_regulation_graphics_'):
        graphics.replay_graphics_receipt(input_assets,value)


def test_input_and_response_bounds_do_not_clip_or_partially_accept():
    for value in ([],assets()*5):
        with pytest.raises(ValueError,match='invalid_regulation_graphics_'):
            graphics.prepare_graphics_recognition(value)
    request=graphics.prepare_graphics_recognition(assets())
    for raw in ('{"version":1,"version":2}', 'NaN', '['*1200+'0'+']'*1200,
                ' '*(graphics.MAX_RESPONSE_BYTES+1)):
        with pytest.raises(ValueError,match='invalid_regulation_graphics_'):
            graphics.validate_graphics_recognition(raw,request,provenance(request))


def test_continuation_headers_can_reference_another_original_in_the_same_batch():
    inputs=assets()
    data=(Path(__file__).parent/'fixtures/regulation_graphics_continuation_demo.png').read_bytes()
    inputs.append({'id':'DEMO.002','filename':'DEMO.002.png','data':data,
                   'sha256':hashlib.sha256(data).hexdigest()})
    request=graphics.prepare_graphics_recognition(inputs);raw=recognition_response(request)
    second=deepcopy(raw['graphics'][0]);second.update(id=inputs[1]['id'],sha256=inputs[1]['sha256'],
        transcript='2025\n13 units')
    second['elements']=[e for e in second['elements']if e['id']not in {'title','note'}]
    for e in second['elements']:
        if e['id']in {'year','count','unit'}:e['source']='image:DEMO.001'
        if e['text']=='2024':e['text']='2025'
        if e['text']=='12':e['text']='13'
    raw['graphics'].append(second)
    recognized=graphics.validate_graphics_recognition(json.dumps(raw),request,provenance(request))
    assert recognized['result']['graphics'][1]['elements'][0]['source']=='image:DEMO.001'
    second['elements'][0]['source']='image:OUTSIDE.BATCH'
    with pytest.raises(ValueError,match='invalid_regulation_graphics_'):
        graphics.validate_graphics_recognition(json.dumps(raw),request,provenance(request))


def test_chart_geometry_preserves_approximate_precision_and_axis_links():
    request=graphics.prepare_graphics_recognition(assets());raw=recognition_response(request);row=raw['graphics'][0]
    row['kind']='chart';row['elements']=[
        {'id':'x','kind':'axis','text':'Year','references':[],'source':'image','precision':'not_numeric'},
        {'id':'y','kind':'axis','text':'Count','references':[],'source':'image','precision':'not_numeric'},
        {'id':'series','kind':'series','text':'Approximately 13 units; estimated bar height.',
         'references':['x','y'],'source':'visual','precision':'approximate'}]
    recognized=graphics.validate_graphics_recognition(json.dumps(raw),request,provenance(request))
    assert recognized['result']['graphics'][0]['elements'][-1]['precision']=='approximate'
    row['elements'][-1]['precision']='printed'
    with pytest.raises(ValueError,match='invalid_regulation_graphics_numeric_precision'):
        graphics.validate_graphics_recognition(json.dumps(raw),request,provenance(request))
    row['elements'][-1].update(precision='approximate',references=[])
    with pytest.raises(ValueError,match='invalid_regulation_graphics_chart_links'):
        graphics.validate_graphics_recognition(json.dumps(raw),request,provenance(request))


def test_cross_batch_continuation_context_is_derived_only_from_replayed_material():
    prior=bundle();base=assets();base[0]['id']='DEMO.NEXT';base[0]['filename']='DEMO.NEXT.png'
    augmented=graphics.augment_batch_context(base,assets(),prior)
    assert base[0]['context']=='Table 1. Production counts; years are unitless.'
    carried=augmented[0]['context'][len(base[0]['context']):]
    assert 'Year' in carried and 'Count' in carried and 'units' in carried
    assert 'DEMO.001' in carried and assets()[0]['sha256'] in carried
    assert prior['recognition']['receipt_sha256'] in carried
    assert prior['review']['receipt_sha256'] in carried
    assert '"kind":"row"' not in carried and '"kind":"cell"' not in carried
    assert '"element_id":"n"' not in carried
    assert 'Applicability' in carried
    assert graphics.prepare_graphics_recognition(augmented).document_inputs[0].data==base[0]['data']
    prior['review']['result']['graphics'][0]['material_complete']=False;reseal(prior['review'])
    with pytest.raises(ValueError,match='invalid_regulation_graphics_'):
        graphics.augment_batch_context(base,assets(),prior)


def test_continuation_context_limit_rejects_without_truncating_verified_footnotes():
    request=graphics.prepare_graphics_recognition(assets());raw=recognition_response(request);row=raw['graphics'][0]
    for n in range(9):
        text='Footnote '+str(n)+': '+'x'*4000
        row['transcript']+='\n'+text
        row['elements'].append({'id':'extra_note_'+str(n),'kind':'footnote','text':text,
            'source':'image','references':[],'precision':'printed'})
    recognized=graphics.validate_graphics_recognition(json.dumps(raw),request,provenance(request))
    review_request=graphics.prepare_graphics_review(assets(),recognized)
    reviewed=graphics.validate_graphics_review(json.dumps(review_response(review_request,recognized)),
        review_request,recognized,provenance(review_request))
    with pytest.raises(ValueError,match='invalid_regulation_graphics_continuation_context_limit'):
        graphics.augment_batch_context(assets(),assets(),{'recognition':recognized,'review':reviewed})


def test_four_asset_batch_order_and_request_payload_are_exact():
    from dataclasses import replace
    inputs=[dict(assets()[0],id='DEMO.'+str(n),filename='DEMO.'+str(n)+'.png')for n in range(4)]
    request=graphics.prepare_graphics_recognition(inputs);raw=recognition_response(request)
    row=raw['graphics'][0];raw['graphics']=[dict(deepcopy(row),id=asset['id'],sha256=asset['sha256'])for asset in inputs]
    recognized=graphics.validate_graphics_recognition(json.dumps(raw),request,provenance(request))
    assert len(recognized['result']['graphics'])==4
    raw['graphics'].reverse()
    with pytest.raises(ValueError,match='invalid_regulation_graphics_identity'):
        graphics.validate_graphics_recognition(json.dumps(raw),request,provenance(request))
    raw['graphics'].reverse()
    for malformed in (replace(request,system=request.system+'changed'),
                      replace(request,user=request.user+'changed'),
                      replace(request,context_json=request.context_json.replace('DEMO.0','WRONG',1))):
        with pytest.raises(ValueError,match='invalid_regulation_graphics_'):
            graphics.validate_graphics_recognition(json.dumps(raw),malformed,provenance(request))


def test_retained_official_original_sizes_and_continuation_images_reach_native_inputs():
    folder=Path(__file__).parent/'fixtures/regulation_graphics'
    manifest=json.loads((folder/'manifest.json').read_text());inputs=[]
    for record in manifest['graphics']:
        data=(folder/record['filename']).read_bytes()
        assert len(data)==record['size']
        assert hashlib.sha256(data).hexdigest()==record['sha256']
        inputs.append({'id':record['id'],'filename':record['filename'],'sha256':record['sha256'],'data':data})
    request=graphics.prepare_graphics_recognition(inputs)
    assert [item.data for item in request.document_inputs]==[asset['data']for asset in inputs]
    assert {r['case']for r in manifest['graphics']}=={
        'small_complete_table','large_header_table','large_table_continuation'}
    # Transport/source fixture only: this test makes no native model accuracy or
    # material-completeness claim about these genuine graphics.


@pytest.mark.parametrize('fault',['oversize','wrong_hash','wrong_magic','unknown_asset_key','bad_context'])
def test_invalid_source_assets_fail_before_model_transport(fault):
    value=assets()
    if fault=='oversize':value[0]['data']=b'\x89PNG\r\n\x1a\n'+b'x'*(graphics.MAX_ASSET_BYTES)
    elif fault=='wrong_hash':value[0]['sha256']='0'*64
    elif fault=='wrong_magic':value[0]['data']=b'not png'
    elif fault=='unknown_asset_key':value[0]['issuer']='not source material'
    else:value[0]['context']='\ud800'
    with pytest.raises(ValueError,match='invalid_regulation_graphics_'):
        graphics.prepare_graphics_recognition(value)
