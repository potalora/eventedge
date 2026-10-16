"""Lossless compact codec; image truth remains the original review's job."""
import importlib,json
from copy import deepcopy
import pytest


def material():
    return {'id':'graphic','sha256':'a'*64,
        'content':{'text':['Net\nAssets α','Exact caption $1.20'],
            'tables':[{'matrix':[['Heading',None],['$2.00','—']],
                'spans':[],'headers':[],'units':[]}],
            'notes':[{'marker':'a','body':'Raised note\nsecond line'}],
            'continuations':[{'before':[{'image_position':2,'quote':'Foreign prefix\nline'}]}],
            'visuals':[{'kind':'axis','description':'Approximate curve'}],
            'reading_order':[{'text':0},{'text':1},{'table':0},{'note':0}]},
        'sources':[{'image_position':1,'id':'graphic','filename':'original.png','sha256':'a'*64},
            {'image_position':2,'id':'context','filename':'context.png','sha256':'b'*64}]}


def codec():return importlib.import_module('tradingagents.strategies.data_sources.regulation_graphics_material')


def test_readable_material_roundtrip_preserves_exact_printing_and_structure():
    c=codec();value=material();before=deepcopy(value)
    text=c.encode_material(value)
    assert 'Net\\nAssets α' in text and '$1.20' in text
    assert text.count('$1.20')==1 and 'transcript' not in text
    assert c.decode_material(text)==value==before
    assert c.encode_material(c.decode_material(text))==text
    assert json.loads(text)['graphic']['content']['tables'][0]['matrix']==[['Heading',None],['$2.00','—']]
    assert json.loads(text)['version']=='regulation-graphic-compact-material-v1'
    assert json.loads(text)['graphic']['sources']==value['sources']


@pytest.mark.parametrize('damage',['extra','missing','nonfinite','oversize','expanded','bad_sources','mismatched_target'])
def test_codec_rejects_ambiguous_or_unbounded_material(damage):
    c=codec();value=material()
    if damage=='extra':value['transcript']='duplicate printing'
    if damage=='missing':value.pop('sources')
    if damage=='nonfinite':value['content']['tables'][0]['matrix'][0][0]=float('nan')
    if damage=='oversize':value['content']['text'][0]='x'*c.MAX_BYTES
    if damage=='expanded':value['content']['blocks']=[]
    if damage=='bad_sources':value['sources'][1]['image_position']=1
    if damage=='mismatched_target':value['sources'][0]['sha256']='c'*64
    with pytest.raises(ValueError):c.encode_material(value)


def test_codec_rejects_noncanonical_serialization_and_duplicate_keys():
    c=codec();text=c.encode_material(material())
    with pytest.raises(ValueError):c.decode_material(text+' ')
    with pytest.raises(ValueError):c.decode_material(text.replace('"version":','"version":"bad","version":'))


def test_assessment_serializes_replayed_compact_content_and_binds_entire_round_chain(monkeypatch):
    """Exercise the Sol projection after replay, without pretending to review pixels."""
    import hashlib
    from test_regulation_evidence import build
    from tradingagents.strategies.data_sources import regulation_graphics_inputs, regulation_graphics_grid
    from tradingagents.strategies.data_sources.regulation_assessment import prepare_request
    value=material();evidence=build();plan={'constructed':'already replayed boundary'}
    bundle={'target_index':0,'rounds':[{'failed_original':'retained raw bytes'},
        {'accepted_original':'retained final bytes'}],'accepted_round':1}
    monkeypatch.setattr(regulation_graphics_inputs,'graphics_assets',lambda _:[{'id':value['id']}])
    monkeypatch.setattr(regulation_graphics_grid,'replay_document',lambda *args:[deepcopy(value)])
    request=prepare_request(evidence,graphics_plan=plan,graphics_batches=[bundle],
        prompt_budget_policy='o200k_base_planning_v1')
    unit=json.loads(request.user)['units'][-1]
    assert codec().decode_material(unit['text'])==value
    assert unit['text'].count('$1.20')==1
    provenance=json.loads(request.context.provenance_json)
    proof=provenance['prompt_budget_proof']
    assert proof['status']=='measured' and proof['within_planning_limit'] is True
    assert proof['planning_limit']==475000
    assert proof['user_sha256']==hashlib.sha256(request.user.encode()).hexdigest()
    serialization=provenance['graphics_material_serialization']
    assert serialization['version']==codec().VERSION
    assert serialization['materials'][0]['serialized_text_sha256']==unit['text_sha256']
    # Even when accepted content is unchanged, a different retained first-round
    # failure changes the supplied unit identity and measured complete request.
    changed=deepcopy(bundle);changed['rounds'][0]['failed_original']='different raw failure'
    other=prepare_request(evidence,graphics_plan=plan,graphics_batches=[changed],
        prompt_budget_policy='o200k_base_planning_v1')
    other_unit=json.loads(other.user)['units'][-1]
    assert other_unit['text']==unit['text'] and other_unit['unit_id']!=unit['unit_id']
    assert json.loads(other.context.provenance_json)['prompt_budget_proof']['user_sha256']!=proof['user_sha256']
