"""Paper eligibility requires an independent reviewed scope, not model assertions."""
from copy import deepcopy
import hashlib
import importlib
import json

import pytest


def seal(value):
    return {**value,'receipt_sha256':hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False).encode()).hexdigest()}


def fixture():
    sha='a'*64
    original={'media_type':'application/pdf','filename':'source.pdf','sha256':sha,'size_bytes':42}
    attachment={'media_type':'image/png','filename':'page.png','sha256':'b'*64,'size_bytes':31,
                'source_sha256':sha,'pages':[2],'render_identity':'retained-render-v1'}
    claim={'id':'count','printed_value':'1,204.50','unit':'shares','row':'Total holdings',
           'column':'Current','citation':'Total holdings 1,204.50 shares'}
    context={'policy':'bounded_document_research_v1','document_id':'source-1','as_of':'2026-10-12T20:01:00+00:00',
       'original':original,'original_pages':3,'selected_pages':[2],'attachments':[attachment],
       'probes':[{'id':'count','page':2,'question':'Current total holdings?'}],
       'residual_failures':['unsupported_visual_content'],'recognition_request_version':'bounded-document-recognition-request-v2'}
    context['request_identity_sha256']=hashlib.sha256(json.dumps(context,sort_keys=True,separators=(',',':'),ensure_ascii=True).encode()).hexdigest()
    recognition=seal({'version':'bounded-document-recognition-v1','validation_status':'structurally_valid',
        'extraction_accuracy':'independently_unverified','assessment_scope':'selected_pages','source_identity':context,
        'pages':[{'page':2,'status':'readable','transcription':'Current\nTotal holdings 1,204.50 shares',
                  'probes':[claim],'uncertainties':[]}], 'unassessed_pages':[1,3],
        'residual_failures':['unsupported_visual_content','whole_document_coverage_unresolved'],
        'full_document_quality_accepted':False,'trading_authority':False,'response_sha256':'c'*64})
    scope={'policy':'verified_scoped_claims_v1','ticker':'ACME','issuer':{'name':'Acme Corporation','cik':'0000000123'},
        'document_id':'source-1','original_sha256':sha,'as_of':context['as_of'],
        'effective_from':'2026-10-12T20:00:00+00:00','effective_until':'2026-10-14T20:00:00+00:00',
        'required_pages':[2],'required_claim_ids':['count'],'purpose':'Compare the reviewed current holdings count only.',
        'economic_date':'2026-10-12','context_limitations':['Only this issuer, security class and holdings cell are reviewed.'],
        'material_context_failures':[]}
    scope['independent_review']=seal({'version':'independent_claim_review_v1',
        'reviewer':{'kind':'human','identity':'reviewer-1'},'reviewed_at':'2026-10-12T20:10:00+00:00',
        'ticker':scope['ticker'],'issuer':deepcopy(scope['issuer']),
        'document_id':'source-1','original_sha256':sha,'as_of':context['as_of'],
        'render_hashes':[{'sha256':'b'*64,'pages':[2],'render_identity':'retained-render-v1'}],
        'claims':[dict(claim,page=2)],
        'reviewed_context':{**{k:scope[k] for k in ('purpose','economic_date','context_limitations')},'scope_complete':True}})
    return recognition,scope


def assess(recognition,scope,**kwargs):
    return importlib.import_module('tradingagents.strategies.document_decision_eligibility').assess_decision_eligibility(
        recognition,scope,ticker=kwargs.get('ticker','ACME'),session_as_of=kwargs.get('session_as_of','2026-10-12'))


def reseal(scope):
    review=scope['independent_review'];scope['independent_review']=seal({k:v for k,v in review.items() if k!='receipt_sha256'})


def test_independent_review_allows_only_bound_paper_scope_and_preserves_residuals():
    recognition,scope=fixture();receipt=assess(recognition,scope)
    assert receipt['eligible'] is True
    assert receipt['execution_authority']=='ordinary_committee_and_risk_gates'
    assert receipt['verified_claims']==scope['independent_review']['claims']
    assert receipt['paper_decision_eligible'] is True
    assert receipt['authority']=='paper_scoped_claims'
    assert receipt['full_document_quality_accepted'] is False
    assert receipt['production_authority'] is False
    assert receipt['recognition_receipt_sha256']==recognition['receipt_sha256']
    assert receipt['independent_review_sha256']==scope['independent_review']['receipt_sha256']
    assert receipt['issuer']==scope['issuer'] and receipt['ticker']=='ACME'
    assert receipt['residual_failures']==recognition['residual_failures']
    assert receipt==seal({k:v for k,v in receipt.items() if k!='receipt_sha256'})
    scope['purpose']='mutated'
    assert receipt['purpose']!='mutated'


@pytest.mark.parametrize('unit',['Number of Shares','  NUMBER   OF SHARES  ','Total Number of Shares'])
def test_finite_generic_share_count_aliases(unit):
    recognition,scope=fixture();scope['independent_review']['claims'][0]['unit']=unit;reseal(scope)
    assert assess(recognition,scope)['paper_decision_eligible'] is True


@pytest.mark.parametrize('fault',['value','precision','unit','currency','scale','row_prefix','column_prefix','citation',
    'wrong_page','missing_claim','extra_claim','unknown_key','render_sha','source_sha','issuer','ticker',
    'acquisition','review_context','reviewer_model','review_time_future','review_time_before_acquisition','review_hash',
    'scope_hash_source','expired','not_yet_effective','window_infinite','window_long','context_failure','scope_extra',
    'recognition_hash','recognition_not_valid','page_uncertain','required_page','required_claim','duplicate_claim',
    'context_incomplete','context_complete_missing'])
def test_unverified_or_mismatched_claims_never_qualify(fault):
    recognition,scope=fixture();review=scope['independent_review'];claim=review['claims'][0]
    if fault=='value': claim['printed_value']='1,205.50'
    elif fault=='precision': claim['printed_value']='1,204.5'
    elif fault=='unit': claim['unit']=None
    elif fault=='currency': claim['unit']='USD'
    elif fault=='scale': claim['unit']='millions of shares'
    elif fault=='row_prefix': claim['row']='Total'
    elif fault=='column_prefix': claim['column']='Curr'
    elif fault=='citation': claim['citation']='Total holdings 1,204.50'
    elif fault=='wrong_page': claim['page']=1
    elif fault=='missing_claim': review['claims']=[]
    elif fault=='extra_claim': review['claims'].append(dict(claim,id='unexpected'))
    elif fault=='duplicate_claim': review['claims'].append(deepcopy(claim))
    elif fault=='unknown_key': claim['trusted']=True
    elif fault=='render_sha': review['render_hashes'][0]['sha256']='d'*64
    elif fault=='source_sha': review['original_sha256']='d'*64
    elif fault=='issuer': review['issuer']['cik']='0000000999'
    elif fault=='ticker': review['ticker']='OTHER'
    elif fault=='acquisition': review['as_of']='2026-10-11T20:01:00+00:00'
    elif fault=='review_context': review['reviewed_context']['purpose']='All issuer investments'
    elif fault=='context_incomplete': review['reviewed_context']['scope_complete']=False
    elif fault=='context_complete_missing': del review['reviewed_context']['scope_complete']
    elif fault=='reviewer_model': review['reviewer']['kind']='model'
    elif fault=='review_time_future': review['reviewed_at']='2026-10-13T20:01:00+00:00'
    elif fault=='review_time_before_acquisition': review['reviewed_at']='2026-10-11T20:01:00+00:00'
    elif fault=='review_hash': review['receipt_sha256']='0'*64
    elif fault=='scope_hash_source': scope['original_sha256']='d'*64
    elif fault=='expired': scope['effective_until']='2026-10-12T21:00:00+00:00'
    elif fault=='not_yet_effective': scope['effective_from']='2026-10-13T20:00:00+00:00'
    elif fault=='window_infinite': scope['effective_until']='never'
    elif fault=='window_long': scope['effective_until']='2027-10-14T20:00:00+00:00'
    elif fault=='context_failure': scope['material_context_failures']=['missing_prior_period_context']
    elif fault=='scope_extra': scope['unlimited']=True
    elif fault=='recognition_hash': recognition['receipt_sha256']='0'*64
    elif fault=='recognition_not_valid': recognition['validation_status']='failed';recognition=seal({k:v for k,v in recognition.items() if k!='receipt_sha256'})
    elif fault=='page_uncertain': recognition['pages'][0]['status']='uncertain';recognition=seal({k:v for k,v in recognition.items() if k!='receipt_sha256'})
    elif fault=='required_page': scope['required_pages']=[1]
    elif fault=='required_claim': scope['required_claim_ids']=['unreviewed']
    if fault!='review_hash': reseal(scope)
    with pytest.raises(ValueError,match='invalid_document_decision_'):
        assess(recognition,scope)


def test_scope_cannot_retarget_session_or_ticker():
    recognition,scope=fixture()
    with pytest.raises(ValueError,match='invalid_document_decision_'): assess(recognition,scope,ticker='OTHER')
    with pytest.raises(ValueError,match='invalid_document_decision_'): assess(recognition,scope,session_as_of='2026-10-20')


def test_model_self_transcription_is_not_the_independent_reference():
    recognition,scope=fixture()
    recognition['pages'][0]['probes'][0].update(printed_value='2,000.00',citation='Total holdings 2,000.00 shares')
    recognition['pages'][0]['transcription']='Current\nTotal holdings 2,000.00 shares'
    recognition=seal({k:v for k,v in recognition.items() if k!='receipt_sha256'})
    with pytest.raises(ValueError,match='invalid_document_decision_'): assess(recognition,scope)


def test_full_labels_allow_case_whitespace_only_without_changing_retained_review():
    recognition,scope=fixture();claim=scope['independent_review']['claims'][0]
    claim.update(row=' TOTAL   HOLDINGS ',column='current')
    reseal(scope)
    receipt=assess(recognition,scope)
    assert receipt['verified_claims'][0]['row']==' TOTAL   HOLDINGS '


@pytest.mark.parametrize('left,right',[('USD millions','USD billions'),('USD','EUR'),
    ('shares','securities'),('shares','thousands of shares'),('USD','US dollars')])
def test_scale_currency_and_count_domains_are_distinct(left,right):
    recognition,scope=fixture()
    recognition['pages'][0]['probes'][0]['unit']=left
    recognition=seal({k:v for k,v in recognition.items() if k!='receipt_sha256'})
    scope['independent_review']['claims'][0]['unit']=right;reseal(scope)
    with pytest.raises(ValueError,match='invalid_document_decision_'): assess(recognition,scope)


def test_actual_structural_validator_receipt_is_compatible():
    import base64
    from tradingagents.strategies.bounded_document_research import prepare_recognition,validate_recognition
    recognition,scope=fixture();data=b'%PDF-1.7\nretained'
    wire={'media_type':'application/pdf','filename':'source.pdf',
          'sha256':hashlib.sha256(data).hexdigest(),'data_base64':base64.b64encode(data).decode()}
    evidence={'policy':'bounded_document_research_v1','document_id':scope['document_id'],'as_of':scope['as_of'],
       'original':wire,'original_pages':3,'selected_pages':[2],
       'attachments':[dict(wire,source_sha256=wire['sha256'],pages=[2],render_identity='native-original-v1')],
       'probes':[{'id':'count','page':2,'question':'Current holdings?'}],'residual_failures':['unsupported_visual_content']}
    request=prepare_recognition(evidence,session_as_of='2026-10-12');context=json.loads(request.context_json)
    raw={'version':'bounded-document-recognition-v1','document_id':scope['document_id'],'as_of':scope['as_of'],
       'original_sha256':wire['sha256'],'request_identity_sha256':context['request_identity_sha256'],
       'pages':recognition['pages']}
    actual=validate_recognition(json.dumps(raw),request)
    scope['original_sha256']=scope['independent_review']['original_sha256']=wire['sha256']
    scope['independent_review']['render_hashes']=[{'sha256':wire['sha256'],'pages':[2],'render_identity':'native-original-v1'}]
    reseal(scope)
    assert assess(actual,scope)['recognition_receipt_sha256']==actual['receipt_sha256']


@pytest.mark.parametrize('fault',['nonfinite','extra_review','bool_page','oversize','naive_time','zero_window'])
def test_strict_bounded_review_shape(fault):
    recognition,scope=fixture()
    if fault=='nonfinite': scope['purpose']=float('nan')
    elif fault=='extra_review': scope['independent_review']['model_asserted_accuracy']=True;reseal(scope)
    elif fault=='bool_page': scope['required_pages']=[True]
    elif fault=='oversize': scope['purpose']='x'*1025
    elif fault=='naive_time': scope['effective_from']='2026-10-12T20:00:00'
    else: scope['effective_until']=scope['effective_from']
    with pytest.raises(ValueError,match='invalid_document_decision_'): assess(recognition,scope)


def descriptive_count_fixture(suffix='of same class as Rule 144/145 Securities owned by Pledgor'):
    recognition,scope=fixture()
    label='Total number of securities '+suffix
    observed=recognition['pages'][0]['probes'][0]
    observed.update(unit=label,row=label,citation=label+' 1,204.50')
    recognition['pages'][0]['transcription']='Current\n'+observed['citation']
    recognition=seal({k:v for k,v in recognition.items() if k!='receipt_sha256'})
    scope['independent_review']['claims'][0].update(unit='securities',row=label,citation=observed['citation'])
    reseal(scope)
    return recognition,scope


def test_descriptive_count_label_can_use_independently_bound_full_row():
    recognition,scope=descriptive_count_fixture()
    assert assess(recognition,scope)['eligible'] is True


@pytest.mark.parametrize('fault',['wrong_class','short_row','unbound_descriptor','million','per_unit','currency','unknown_currency','unknown_scale'])
def test_descriptive_count_label_does_not_discard_unverified_meaning(fault):
    suffix={'million':'in millions','per_unit':'per unit','currency':'in USD',
            'unknown_currency':'denominated in XYZ','unknown_scale':'scaled by 1000'}.get(fault)
    recognition,scope=descriptive_count_fixture(suffix) if suffix else descriptive_count_fixture()
    if fault=='wrong_class':
        scope['independent_review']['claims'][0]['row']=scope['independent_review']['claims'][0]['row'].replace('same class','another class')
    elif fault=='short_row': scope['independent_review']['claims'][0]['row']='Securities owned by Pledgor'
    elif fault=='unbound_descriptor':
        recognition['pages'][0]['probes'][0]['row']='Total holdings'
        scope['independent_review']['claims'][0]['row']='Total holdings'
        recognition=seal({k:v for k,v in recognition.items() if k!='receipt_sha256'})
    reseal(scope)
    with pytest.raises(ValueError,match='invalid_document_decision_'): assess(recognition,scope)


def test_independent_visual_review_is_truthfully_distinct_from_human_review():
    recognition,scope=fixture()
    scope['independent_review']['reviewer']={'kind':'independent_visual','identity':'root-separate-original-image-review'}
    reseal(scope)
    receipt=assess(recognition,scope)
    assert receipt['reviewer']['kind']=='independent_visual'


def automatic_fixture():
    import base64
    from tradingagents.strategies.bounded_document_research import prepare_recognition,validate_recognition
    old,scope=fixture();scope.pop('independent_review');data=b'%PDF-1.7\nblind retained source'
    wire={'media_type':'application/pdf','filename':'source.pdf','sha256':hashlib.sha256(data).hexdigest(),
          'data_base64':base64.b64encode(data).decode()}
    scope['original_sha256']=wire['sha256']
    evidence={'policy':'bounded_document_research_v1','document_id':scope['document_id'],'as_of':scope['as_of'],
        'original':wire,'original_pages':3,'selected_pages':[2],
        'attachments':[dict(wire,source_sha256=wire['sha256'],pages=[2],render_identity='native-original-v1')],
        'probes':[{'id':'count','page':2,'question':'Current total holdings?'}],'residual_failures':['unsupported_visual_content']}
    request=prepare_recognition(evidence,session_as_of='2026-10-12');context=json.loads(request.context_json)
    raw={'version':'bounded-document-recognition-v1','document_id':scope['document_id'],'as_of':scope['as_of'],
        'original_sha256':wire['sha256'],'request_identity_sha256':context['request_identity_sha256'],'pages':old['pages']}
    primary=validate_recognition(json.dumps(raw),request)
    blind=deepcopy(raw)
    blind['pages'][0]['transcription']+='\nTotal holdings 1,204.50 shares verified line'
    blind['pages'][0]['probes'][0]['citation']='Total holdings 1,204.50 shares verified line'
    wrapper={'recognition':blind,'context_sufficient':True,'material_context_failures':[]}
    provenance={'configured_model':'gpt-6-luna','returned_model':'gpt-6-luna','returned_revision':None,
        'identity_status':'unpinned','response_id':'blind-response-2','reasoning_effort':'high',
        'role':'bounded','billing_mode':'chatgpt_plan','effective_output_limit':None,
        'document_inputs':[{'media_type':'application/pdf','filename':'source.pdf','sha256':wire['sha256'],'size_bytes':len(data)}]}
    return primary,wrapper,request,scope,provenance


def build_auto(primary,wrapper,request,scope,provenance,**kwargs):
    module=importlib.import_module('tradingagents.strategies.document_decision_eligibility')
    return module.build_automatic_review(primary,json.dumps(wrapper),request,scope,ticker='ACME',
        session_as_of='2026-10-12',verified_at=kwargs.get('verified_at','2026-10-12T20:10:00+00:00'),model_provenance=provenance)


def test_blind_prompt_contains_declared_scope_and_source_but_no_primary_or_expected_answers():
    primary,wrapper,request,scope,provenance=automatic_fixture()
    module=importlib.import_module('tradingagents.strategies.document_decision_eligibility')
    system,user=module.prepare_automatic_verification(request,scope)
    assert 'Independent blind source verification' in system
    assert scope['purpose'] in user and scope['original_sha256'] in user
    assert '1,204.50' not in system+user
    assert primary['pages'][0]['transcription'] not in system+user
    assert 'independent_review' not in user
    assert 'data_base64' not in user
    assert 'context_sufficient' in system


def test_automatic_blind_agreement_builds_admissible_truthful_review_without_entered_values():
    primary,wrapper,request,scope,provenance=automatic_fixture()
    review=build_auto(primary,wrapper,request,scope,provenance)
    assert review['version']=='automatic_review_v1'
    assert review['reviewer']['kind']=='automatic_visual'
    assert review['reviewed_at']=='2026-10-12T20:10:00+00:00'
    automatic=review['automatic_verification']
    assert automatic['correlated_model_risk'] is True and automatic['guarantees_accuracy'] is False
    assert automatic['primary_receipt_sha256']==primary['receipt_sha256']
    assert automatic['verification_response_sha256']==hashlib.sha256(json.dumps(wrapper).encode()).hexdigest()
    assert automatic['model_provenance_sha256']==seal(provenance)['receipt_sha256']
    assert automatic['verification_recognition']['receipt_sha256']==automatic['verification_receipt_sha256']
    receipt=assess(primary,dict(scope,independent_review=review))
    assert receipt['eligible'] is True and receipt['verification_basis']=='automatic_review_v1'
    assert receipt['correlated_model_risk'] is True and receipt['guarantees_accuracy'] is False
    assert receipt['verified_claims'][0]['citation']==primary['pages'][0]['probes'][0]['citation']


@pytest.mark.parametrize('fault',['value','precision','unit_null','unit_scale','row','column','source','request_hash',
    'missing_probe','uncertain','detached_citation','not_context_sufficient','context_failure','extra_wrapper',
    'wrong_model','wrong_returned_model','wrong_role','wrong_effort','wrong_billing','wrong_attachment',
    'future_verification','before_acquisition','primary_changed','scope_source','scope_review'])
def test_bad_blind_verification_never_produces_review(fault):
    primary,wrapper,request,scope,provenance=automatic_fixture();page=wrapper['recognition']['pages'][0];claim=page['probes'][0]
    if fault in ('value','precision'):
        number='1,205.50' if fault=='value' else '1,204.5'
        claim['printed_value']=number;claim['citation']=claim['citation'].replace('1,204.50',number)
        page['transcription']=page['transcription'].replace('1,204.50',number)
    elif fault=='unit_null': claim['unit']=None
    elif fault=='unit_scale':
        claim['unit']='millions of shares';page['transcription']+='\nmillions of shares'
    elif fault=='row':
        claim['row']='Another holding';claim['citation']=claim['citation'].replace('Total holdings','Another holding')
        page['transcription']=page['transcription'].replace('Total holdings','Another holding')
    elif fault=='column': claim['column']='Prior';page['transcription']+='\nPrior'
    elif fault=='source': wrapper['recognition']['original_sha256']='d'*64
    elif fault=='request_hash': wrapper['recognition']['request_identity_sha256']='d'*64
    elif fault=='missing_probe': page['probes']=[]
    elif fault=='uncertain': page['status']='uncertain'
    elif fault=='detached_citation': claim['citation']='Total holdings 1,204.50 not printed'
    elif fault=='not_context_sufficient': wrapper['context_sufficient']=False
    elif fault=='context_failure': wrapper['material_context_failures']=['missing security-class context']
    elif fault=='extra_wrapper': wrapper['trusted']=True
    elif fault=='wrong_model': provenance['configured_model']='gpt-6.1-sol'
    elif fault=='wrong_returned_model': provenance['returned_model']='gpt-6.1-sol'
    elif fault=='wrong_role': provenance['role']='thesis'
    elif fault=='wrong_effort': provenance['reasoning_effort']='low'
    elif fault=='wrong_billing': provenance['billing_mode']='api'
    elif fault=='wrong_attachment': provenance['document_inputs'][0]['sha256']='d'*64
    elif fault=='future_verification':
        with pytest.raises(ValueError,match='invalid_document_decision_'): build_auto(primary,wrapper,request,scope,provenance,verified_at='2026-10-13T20:10:00+00:00')
        return
    elif fault=='before_acquisition':
        with pytest.raises(ValueError,match='invalid_document_decision_'): build_auto(primary,wrapper,request,scope,provenance,verified_at='2026-10-11T20:10:00+00:00')
        return
    elif fault=='primary_changed': primary['pages'][0]['probes'][0]['printed_value']='2,000'
    elif fault=='scope_source': scope['original_sha256']='d'*64
    else: scope['independent_review']={'claims':['expected-answer']}
    with pytest.raises(ValueError,match='invalid_document_decision_'): build_auto(primary,wrapper,request,scope,provenance)


def test_prepare_blind_rejects_expected_values_instead_of_sending_them():
    primary,wrapper,request,scope,provenance=automatic_fixture()
    module=importlib.import_module('tradingagents.strategies.document_decision_eligibility')
    for key in ('independent_review','expected_claims'):
        damaged=dict(scope,**{key:{'printed_value':'1,204.50'}})
        with pytest.raises(ValueError,match='invalid_document_decision_'): module.prepare_automatic_verification(request,damaged)


@pytest.mark.parametrize('fault',['primary_receipt','blind_receipt','provenance_hash','response_hash','context','model_risk'])
def test_automatic_receipt_rechecked_at_eligibility_boundary(fault):
    primary,wrapper,request,scope,provenance=automatic_fixture();review=build_auto(primary,wrapper,request,scope,provenance)
    automatic=review['automatic_verification']
    if fault=='primary_receipt': automatic['primary_receipt_sha256']='d'*64
    elif fault=='blind_receipt': automatic['verification_recognition']['pages'][0]['probes'][0]['printed_value']='3,000'
    elif fault=='provenance_hash': automatic['model_provenance']['reasoning_effort']='low'
    elif fault=='response_hash': automatic['verification_response_sha256']='not-a-sha'
    elif fault=='context': automatic['context_sufficient']=False
    else: automatic['correlated_model_risk']=False
    scope['independent_review']=seal({k:v for k,v in review.items() if k!='receipt_sha256'})
    with pytest.raises(ValueError,match='invalid_document_decision_'): assess(primary,scope)


@pytest.mark.parametrize('field,value',[('purpose',{'printed_value':'1,204.50'}),
    ('issuer',{'name':'Acme','cik':'0000000123','expected_unit':'shares'}),
    ('context_limitations',[{'expected_value':'1,204.50'}]),('required_claim_ids',[{'expected_value':'1,204.50'}])])
def test_blind_prompt_rejects_malformed_nested_scope_before_sending(field,value):
    primary,wrapper,request,scope,provenance=automatic_fixture();scope[field]=value
    module=importlib.import_module('tradingagents.strategies.document_decision_eligibility')
    with pytest.raises(ValueError,match='invalid_document_decision_'): module.prepare_automatic_verification(request,scope)


def test_configured_native_luna_with_unavailable_returned_identity_remains_truthful():
    primary,wrapper,request,scope,provenance=automatic_fixture()
    provenance['returned_model']=None;provenance['identity_status']='unpinned'
    review=build_auto(primary,wrapper,request,scope,provenance)
    assert review['automatic_verification']['model_provenance']['returned_model'] is None
    provenance['identity_status']='pinned'
    with pytest.raises(ValueError,match='invalid_document_decision_'): build_auto(primary,wrapper,request,scope,provenance)
