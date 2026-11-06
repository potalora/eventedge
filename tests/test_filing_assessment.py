"""Strict full-evidence assessment contract; native-derived source fixtures, no model calls."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from tradingagents.strategies.data_sources.filing_evidence import build_evidence,parse_submission
from test_filing_evidence import submission,role,ACCESSION,OBSERVED


def module():
    from tradingagents.strategies.data_sources import filing_assessment
    return filing_assessment


def evidence(filename='native_8k_primary.nc',accession=ACCESSION,form='8-K',day='2026-09-30'):
    raw=(Path(__file__).parent/'fixtures'/'filing_evidence'/filename).read_bytes()
    return build_evidence(parse_submission(raw,expected_accession=accession,expected_form=form,
                                          expected_date=day,observed_at=OBSERVED))


def synthetic(form='10-K',*,prior=False,dependencies=False):
    # Native framing builder; synthetic annual/proxy content is not real report parity.
    documents=[(form,'main.htm','<p>Full narrative current tail.</p>')]
    if dependencies:
        documents=[(form,'main.htm','<p>Full narrative current tail.</p><a href="ex99.htm">Release</a>'),
                   ('EX-99.1','ex99.htm','<p>Full dependency trailing conclusion.</p>')]
    raw=submission(documents,form=form,roles=role('SUBJECT-COMPANY','2065397','Issuer') if form.startswith('SCHEDULE 13') else None)
    accession=ACCESSION
    day='2026-09-30'
    if prior:
        accession='0001193125-25-409112';day='2025-09-30'
        raw=raw.replace(ACCESSION.encode(),accession.encode()).replace(b'20260930',b'20250930')
    return build_evidence(parse_submission(raw,expected_accession=accession,expected_form=form,
                                          expected_date=day,observed_at=OBSERVED))


def binding(*corpora):
    return {'status':'verified',
            'issuers':{c['accession']:c['issuer_candidates'][0]['cik'] for c in corpora},
            'corpus_sha256':{c['accession']:module().evidence_digest(c) for c in corpora}}


def target(ticker='AAPL'):
    return {'status':'verified','ticker':ticker,'binding_sha256':'f'*64}


def prepared(current=None,*,kind='material_event',prior=None,**kwargs):
    current=evidence() if current is None else current
    corpora=current if isinstance(current,list) else [current]
    if kind=='filing_change' and prior and 'comparison_binding' not in kwargs:
        kwargs['comparison_binding']=comparison(current,prior)
    return module().prepare_request(kind,current,prior_evidence=prior,
        issuer_binding=binding(*(corpora+([prior] if prior else []))),**kwargs)


def response(request,*,status='sufficient',direction='neutral'):
    ctx=request.context
    value={'contract_version':'filing-assessment-v1','filing_evidence_status':status,
        'direction':direction if status=='sufficient' else None,
        'conviction':.7 if status=='sufficient' else None,
        'rationale':'Completed analysis in supplied source scope.',
        'evidence_claim':'The source discloses the cited statement.',
        'citations':[{'unit_id':unit_id,'quote':text[-min(80,len(text)):]} for unit_id,text in ctx.unit_texts.items()],
        'unresolved_material_dependencies':[]}
    if ctx.analysis_type=='quantum_readiness':
        value.update(issuer_ciks=list(ctx.issuer_ciks),target_ticker=ctx.target_ticker,
                     regime_signal='neutral' if status=='sufficient' else None,
                     regime_confidence=.5 if status=='sufficient' else None,
                     pqc_readiness='aware' if status=='sufficient' else None,
                     crypto_dependency='unknown' if status=='sufficient' else None)
    else:value['issuer_cik']=ctx.issuer_ciks[0]
    return value


def validate(request,value):
    return module().validate_assessment(json.dumps(value),request.context)


@pytest.mark.parametrize('field', ['conviction', 'regime_confidence'])
def test_extreme_json_integer_has_typed_invalid_response(field):
    req = prepared([evidence()], kind='quantum_readiness', target_binding=target())
    value = response(req)
    value[field] = 10**400
    with pytest.raises(ValueError, match='invalid_filing_response'):
        validate(req, value)


def test_native_full_text_including_tail_and_every_selected_dependency_is_rendered():
    current=evidence();req=prepared(current)
    for unit in current['units']:
        assert unit['text'] in req.user
        assert module().unit_id(current,unit) in req.user
    assert len(current['units'][0]['text'])>3000
    assert current['units'][0]['text'][-300:] in req.user
    payload=json.loads(req.user)
    assert payload['current'][0]['form']=='8-K'
    assert payload['current'][0]['dependencies']==current['dependencies']
    assert payload['current'][0]['document_inventory']==current['document_inventory']
    assert payload['current'][0]['roles']==current['roles']
    assessment=validate(req,response(req))
    assert assessment['document_assessment']=='assessed_no_directional_thesis'
    assert assessment['security_resolution']=='unresolved'
    assert '"text":' not in json.dumps(assessment['source_provenance'])


@pytest.mark.parametrize('kind,form,task',[('filing_change','10-K','same-issuer prior same-form'),
    ('filing_change','10-Q','same-issuer prior same-form'),('exec_comp','DEF 14A','compensation structure'),
    ('material_event','8-K','disclosed event'),('activist_stake','SCHEDULE 13D','purpose and control'),
    ('passive_stake','SCHEDULE 13G','passive ownership')])
def test_form_aware_tasks_do_not_default_every_form_to_annual_comparison(kind,form,task):
    current=synthetic(form)
    prior=synthetic(form,prior=True) if kind=='filing_change' else None
    req=prepared(current,kind=kind,prior=prior)
    assert task in req.system
    assert form in req.user
    value=validate(req,response(req))
    assert value['filing_evidence_status']=='sufficient'
    if prior:assert prior['units'][0]['text'] in req.user


def test_actual_native_subject_is_not_reporter_or_accession_prefix():
    current=evidence('native_13d.nc','0001193125-26-409121','SCHEDULE 13D')
    req=prepared(current,kind='activist_stake')
    assert req.context.issuer_ciks==('0000064996',)
    value=response(req);value['issuer_cik']='0001193125'
    with pytest.raises(ValueError,match='^invalid_filing_issuer$'):validate(req,value)


@pytest.mark.parametrize('change',[
    lambda c:c['units'][0].update(text=c['units'][0]['text']+'tampered'),
    lambda c:c['units'][0].update(text_start=1),
    lambda c:c['units'][0].update(text_end=1),
    lambda c:c.update(structural_status='insufficient'),
    lambda c:c.update(issues=[{'code':'missing_required_dependency'}]),
    lambda c:c['issuer_candidates'][0].update(cik='0000000001'),
    lambda c:c['document_inventory'][0].update(body_sha256='0'*64),
    lambda c:c.update(accession='bad'),
    lambda c:c.update(unknown='invented'),
])
def test_malformed_input_or_hash_mismatch_fails_before_model(change):
    current=evidence();b=binding(current);change(current)
    with pytest.raises(ValueError,match='^invalid_filing_'):
        module().prepare_request('material_event',current,issuer_binding=b)


def test_wrong_corpus_binding_hash_or_extra_issuer_binding_fails():
    current=evidence();b=binding(current);b['corpus_sha256'][current['accession']]='0'*64
    with pytest.raises(ValueError,match='^invalid_filing_binding$'):
        module().prepare_request('material_event',current,issuer_binding=b)
    b=binding(current);b['issuers']['0000000001-26-000001']='0000000001'
    with pytest.raises(ValueError,match='^invalid_filing_binding$'):
        module().prepare_request('material_event',current,issuer_binding=b)


@pytest.mark.parametrize('kind',['missing','same','future','wrong_form','wrong_issuer'])
def test_comparator_is_exact_earlier_same_form_same_source_issuer(kind):
    current=synthetic();prior=synthetic(prior=True)
    if kind=='missing':prior=None
    elif kind=='same':prior=deepcopy(current)
    elif kind=='future':prior['filing_date']='2027-09-30'
    elif kind=='wrong_form':prior=synthetic('10-Q',prior=True)
    else:
        for row in prior['roles']+prior['issuer_candidates']:row['cik']='0000000001'
    with pytest.raises(ValueError,match='^invalid_filing_comparator$'):
        prepared(current,kind='filing_change',prior=prior)


def test_joint_or_unknown_source_issuer_is_not_completed_by_neutral_model():
    raw=submission(roles=role('FILER','764622','Parent')+role('FILER','7286','Subsidiary'))
    current=build_evidence(parse_submission(raw,expected_accession=ACCESSION,expected_form='8-K',expected_date='2026-09-30',observed_at=OBSERVED))
    with pytest.raises(ValueError,match='^invalid_filing_issuer$'):prepared(current)


def test_required_material_reference_must_resolve_to_selected_exact_unit():
    current=synthetic('8-K',dependencies=True)
    unit=current['units'][1];uid=module().unit_id(current,unit)
    declarations=[{'accession':current['accession'],'reference':'ex99.htm','resolved_unit_id':uid}]
    req=prepared(current,required_material_dependencies=declarations)
    assert json.loads(req.user)['required_material_dependencies']==declarations
    value=response(req);value['citations'].pop()
    with pytest.raises(ValueError,match='^invalid_filing_citations$'):validate(req,value)
    declarations[0]['resolved_unit_id']=None
    with pytest.raises(ValueError,match='^invalid_filing_material_dependency$'):
        prepared(current,required_material_dependencies=declarations)


def test_unclassified_external_or_local_links_remain_inventory_not_automatic_materiality():
    current=synthetic('8-K');current['dependencies']=[{'href':'logo.png','label':'Logo','resolution':'unresolved_local','filename':'logo.png'}]
    req=prepared(current)
    assert 'logo.png' in req.user and 'assess every dependency' in req.system
    value=response(req);value['unresolved_material_dependencies']=[{'reference':'logo.png','reason':'Required substantive exhibit is unavailable.'}]
    with pytest.raises(ValueError,match='^invalid_filing_material_dependency$'):validate(req,value)
    value['filing_evidence_status']='insufficient';value['direction']=None;value['conviction']=None
    result=validate(req,value)
    assert result['document_assessment']=='insufficient'


@pytest.mark.parametrize('change',[
    lambda v:v.update(unknown=1),lambda v:v.pop('rationale'),lambda v:v.update(conviction=True),
    lambda v:v.update(conviction='0.7'),lambda v:v.update(conviction=float('nan')),
    lambda v:v.update(conviction=1.1),lambda v:v.update(direction='buy'),
    lambda v:v.update(filing_evidence_status='not_applicable'),lambda v:v.update(rationale=' '),
    lambda v:v.update(evidence_claim=''),lambda v:v.update(contract_version='old'),
    lambda v:v.update(citations=[]),lambda v:v['citations'][0].update(quote='invented quote'),
    lambda v:v['citations'][0].update(unit_id='invented_unit'),
    lambda v:v['citations'].append(deepcopy(v['citations'][0])),
    lambda v:v['citations'][0].update(extra='bad'),
])
def test_response_exact_schema_types_identity_and_quotes(change):
    req=prepared();value=response(req);change(value)
    with pytest.raises(ValueError,match='^invalid_filing_'):validate(req,value)


@pytest.mark.parametrize('raw',['{} trailing','```json\n{}\n```','{"x":1,"x":2}','[{}]'])
def test_no_repaired_json_or_duplicate_json_fields(raw):
    req=prepared()
    with pytest.raises(ValueError,match='^invalid_filing_response$'):
        module().validate_assessment(raw,req.context)


def test_insufficient_is_typed_null_not_neutral_and_directional_requires_target():
    req=prepared();value=response(req,status='insufficient')
    assert validate(req,value)['filing_evidence_status']=='insufficient'
    value['direction']='neutral'
    with pytest.raises(ValueError,match='^invalid_filing_response$'):validate(req,value)
    value=response(req,direction='long')
    with pytest.raises(ValueError,match='^invalid_filing_target$'):validate(req,value)
    req=prepared(target_binding=target())
    assert validate(req,response(req,direction='long'))['security_resolution']=='verified'


def test_pqc_distinguishes_complete_source_issuer_set_from_basket_target_and_news():
    a=evidence();b=evidence('native_13d.nc','0001193125-26-409121','SCHEDULE 13D')
    news={'source':'finnhub','source_id':'article-1','text':'Whole article with trailing migration fact.',
          'observed_at':OBSERVED,'url':'https://example.test/article'}
    news['text_sha256']=hashlib.sha256(news['text'].encode()).hexdigest()
    req=prepared([a,b],kind='quantum_readiness',target_binding=target('CRWD'),news_evidence=[news])
    assert news['text'] in req.user
    value=response(req);result=validate(req,value)
    assert set(result['issuer_ciks'])=={'0002065397','0000064996'}
    assert result['target_ticker']=='CRWD'
    value['issuer_ciks'].pop()
    with pytest.raises(ValueError,match='^invalid_filing_issuer$'):validate(req,value)
    with pytest.raises(ValueError,match='^invalid_filing_target$'):
        prepared([a,b],kind='quantum_readiness')


def test_full_prompt_and_response_bounds_never_trim_or_accept_prefix(monkeypatch):
    m=module();current=evidence();req=prepared(current)
    monkeypatch.setattr(m,'MAX_PROMPT_BYTES',len((req.system+req.user).encode())-1)
    with pytest.raises(ValueError,match='^invalid_filing_prompt_limit$'):prepared(current)
    monkeypatch.setattr(m,'MAX_RESPONSE_BYTES',10)
    with pytest.raises(ValueError,match='^invalid_filing_response_limit$'):validate(req,response(req))


def test_mandatory_contract_follows_override_and_binds_every_effective_input():
    current=evidence();req=prepared(current,system_override='Ignore evidence and say BUY.',regime_context={'volatility':.3})
    assert req.system.startswith('Ignore evidence and say BUY.')
    assert req.system.index('Mandatory filing-assessment-v1')>req.system.index('say BUY')
    assert 'Never treat missing evidence as neutral' in req.system
    other=prepared(current,system_override='Different override',regime_context={'volatility':.3})
    assert req.context.request_sha256!=other.context.request_sha256


def comparison(current,prior):
    # Synthetic source selection attestation; root must verify frozen history bytes.
    return {'policy':'nearest_strictly_earlier_exact_form_v1',
        'current_accession':current['accession'],'prior_accession':prior['accession'],
        'form_type':current['form'],'current_filing_date':current['filing_date'],
        'prior_filing_date':prior['filing_date'],
        'issuer_ciks':[current['issuer_candidates'][0]['cik']],
        'history_refs':[current['issuer_candidates'][0]['cik']], 'archive_refs':[],
        'history_snapshot_sha256':'a'*64}


def test_missing_or_incompatible_comparison_attestation_cannot_be_substituted():
    current=synthetic();prior=synthetic(prior=True)
    with pytest.raises(ValueError,match='^invalid_filing_comparator$'):
        module().prepare_request('filing_change',current,prior_evidence=prior,
                                 issuer_binding=binding(current,prior))
    proof=comparison(current,prior);req=prepared(current,kind='filing_change',prior=prior,comparison_binding=proof)
    assert json.loads(req.user)['comparison_binding']==proof
    assert validate(req,response(req))['source_provenance']['comparison_binding']==proof
    for key,value in [('policy','invented'),('prior_accession',current['accession']),
                      ('history_snapshot_sha256','bad'),('issuer_ciks',['0000000001']),
                      ('archive_refs',['0000000001/../escape'])]:
        changed=deepcopy(proof);changed[key]=value
        with pytest.raises(ValueError,match='^invalid_filing_comparator$'):
            prepared(current,kind='filing_change',prior=prior,comparison_binding=changed)


def test_citations_account_for_more_than_native_max18_selected_units():
    documents=[('8-K','main.htm','<p>Current main.</p>'+''.join(f'<a href="ex{i}.htm">EX{i}</a>' for i in range(40)))]
    documents += [('EX-99.1',f'ex{i}.htm',f'<p>Complete exhibit {i} trailing content.</p>') for i in range(40)]
    raw=submission(documents)
    current=build_evidence(parse_submission(raw,expected_accession=ACCESSION,expected_form='8-K',expected_date='2026-09-30',observed_at=OBSERVED))
    req=prepared(current);value=response(req)
    assert len(value['citations'])==41
    assert len(validate(req,value)['citations'])==41
    value['citations'].pop()
    with pytest.raises(ValueError,match='^invalid_filing_citations$'):validate(req,value)


def test_actual_native_13g_amendment_is_available_for_pqc():
    current=evidence('native_13g_direct.txt','0002042926-26-000016','SCHEDULE 13G/A','2026-10-09')
    req=prepared([current],kind='quantum_readiness',target_binding=target('CRWD'))
    assert 'SCHEDULE 13G/A' in req.user
    assert current['units'][0]['text']==json.loads(req.user)['current'][0]['units'][0]['text']
    assert validate(req,response(req))['filing_evidence_status']=='sufficient'


@pytest.mark.parametrize('path',[('units',0,'filename'),('units',0,'body_start'),
    ('dependencies',0,'filename'),('dependencies',0,'resolution')])
def test_malformed_nested_input_type_has_stable_valueerror(path):
    current=synthetic('8-K',dependencies=True)
    current[path[0]][path[1]][path[2]]=[]
    with pytest.raises(ValueError,match='^invalid_filing_'):prepared(current)


def test_unresolved_declaration_id_type_and_wrong_local_reference_fail_closed():
    current=synthetic('8-K',dependencies=True);uid=module().unit_id(current,current['units'][0])
    for resolved,reference in [([], 'ex99.htm'),(uid,'missing.htm')]:
        with pytest.raises(ValueError,match='^invalid_filing_material_dependency$'):
            prepared(current,required_material_dependencies=[{'accession':current['accession'],
                'reference':reference,'resolved_unit_id':resolved}])


def test_prepared_provenance_does_not_expose_mutable_internal_state():
    req=prepared();first=req.context.source_provenance
    first['units'].clear()
    assert req.context.source_provenance['units']


def test_response_bad_unicode_is_stable_failure():
    req=prepared()
    with pytest.raises(ValueError,match='^invalid_filing_response$'):
        module().validate_assessment('\ud800',req.context)


def test_real_acquisition_shape_preserves_canonical_source_url_without_network(monkeypatch):
    from test_filing_acquisition import Response,mock_transport,call,COMPLETE
    from tradingagents.strategies.data_sources.edgar_source import EDGARSource
    native=Response();calls=mock_transport(monkeypatch,native)
    current=call(EDGARSource())
    req=prepared(current)
    assert json.loads(req.user)['current'][0]['source_url']==COMPLETE
    assert validate(req,response(req))['source_provenance']['source_urls']=={current['accession']:COMPLETE}
    assert len(calls)==1 and native.closed
    for bad in [COMPLETE+'?token=PRIVATE',COMPLETE.replace('www.sec.gov','other.invalid'),
                COMPLETE.replace(current['accession']+'.txt','0000000001-26-000001.txt')]:
        altered=deepcopy(current);altered['source_url']=bad
        with pytest.raises(ValueError,match='^invalid_filing_source_url$'):prepared(altered)


def test_pqc_news_only_has_no_invented_filing_issuer_and_zero_evidence_fails():
    news={'source':'finnhub','source_id':'one','text':'Whole news-only quantum migration evidence.',
          'text_sha256':hashlib.sha256(b'Whole news-only quantum migration evidence.').hexdigest(),
          'observed_at':OBSERVED,'url':'https://example.test/one'}
    req=module().prepare_request('quantum_readiness',[],issuer_binding={'status':'verified','issuers':{},'corpus_sha256':{}},
                                target_binding=target('CRWD'),news_evidence=[news])
    assert req.context.issuer_ciks==()
    assert validate(req,response(req))['issuer_ciks']==[]
    with pytest.raises(ValueError,match='^invalid_filing_input$'):
        module().prepare_request('quantum_readiness',[],issuer_binding={'status':'verified','issuers':{},'corpus_sha256':{}},target_binding=target())
    conflicting=deepcopy(news);conflicting['text']+=' changed';conflicting['text_sha256']=hashlib.sha256(conflicting['text'].encode()).hexdigest()
    with pytest.raises(ValueError,match='^invalid_filing_input$'):
        module().prepare_request('quantum_readiness',[],issuer_binding={'status':'verified','issuers':{},'corpus_sha256':{}},target_binding=target(),news_evidence=[news,conflicting])


def test_actual_schedule_13g_amendment_supports_existing_passive_stake_task():
    current=evidence('native_13g_direct.txt','0002042926-26-000016','SCHEDULE 13G/A','2026-10-09')
    req=prepared(current,kind='passive_stake')
    assert validate(req,response(req))['filing_evidence_status']=='sufficient'


def test_pqc_news_preserves_actual_publication_and_observation_provenance():
    news={'source':'finnhub','source_id':'native-one','text':'Exact retained headline and summary.',
        'text_sha256':hashlib.sha256(b'Exact retained headline and summary.').hexdigest(),
        'observed_at':OBSERVED,'published_at':'2026-09-30T20:00:00+00:00','url':'https://example.test/one'}
    req=prepared([],kind='quantum_readiness',target_binding=target(),news_evidence=[news])
    entry=validate(req,response(req))['source_provenance']['units'][0]
    assert entry['observed_at']==news['observed_at'] and entry['published_at']==news['published_at']
    assert entry['url']==news['url']
