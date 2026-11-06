"""Prospective v4 producer scope; genuine uncertain v3 output stays failed."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from test_bounded_document_research import analyzer, evidence, recognition
from test_document_decision_eligibility import automatic_fixture
from tradingagents.strategies import bounded_document_research as source
from tradingagents.strategies import document_decision_eligibility as decision

pytestmark=pytest.mark.scheduled_acceptance
NATIVE=json.loads((Path(__file__).parent/'fixtures/bounded_document_scoped_readability_20261015.json').read_text())
V4='bounded-document-recognition-request-v4'


def test_exact_native_signature_uncertainty_is_retained_as_a_failed_primary_attempt():
    row=NATIVE
    assert hashlib.sha256(row['primary_response'].encode()).hexdigest()==row['primary_response_sha256']
    raw=json.loads(row['primary_response']);page=raw['pages'][0]
    assert page['status']=='uncertain'
    assert page['uncertainties']==['The handwritten signature is not legible.']
    assert [(p['id'],p['printed_value'])for p in page['probes']]==[
        ('table_shares','4,000,000'),('total_A','15,866,703'),('total_C','15,866,703')]
    assert row['failure_reason']=='invalid_bounded_document' and row['thesis_attempt'] is None
    request=source.RecognitionRequest('','',json.dumps(row['source_identity']),())
    with pytest.raises(ValueError,match='^invalid_bounded_document$'):
        source.validate_recognition(row['primary_response'],request)


def test_v3_primary_and_blind_system_bytes_remain_immutable():
    request=source.prepare_recognition(evidence(),session_as_of='2026-10-12')
    assert json.loads(request.context_json)['recognition_request_version']=='bounded-document-recognition-request-v3'
    assert hashlib.sha256(request.system.encode()).hexdigest()==NATIVE['primary_model_request']['system_sha256']
    _,_,blind_request,scope,_=automatic_fixture()
    system,_=decision.prepare_automatic_verification(blind_request,scope)
    assert hashlib.sha256(system.encode()).hexdigest()==NATIVE['legacy_blind_system_sha256']


def test_v4_is_explicit_opt_in_and_scopes_both_producer_prompts_to_required_material():
    value=evidence();v3=source.prepare_recognition(value,session_as_of='2026-10-12')
    v4=source.prepare_recognition(value,session_as_of='2026-10-12',request_version=V4)
    context=json.loads(v4.context_json)
    assert context['recognition_request_version']==V4
    assert context['request_identity_sha256']!=json.loads(v3.context_json)['request_identity_sha256']
    assert 'unrequested_handwriting_and_authenticity_unassessed' in context['residual_failures']
    _,_,request,scope,_=automatic_fixture()
    old=json.loads(request.context_json);old['recognition_request_version']=V4;old.pop('request_identity_sha256')
    old['request_identity_sha256']=hashlib.sha256(json.dumps(old,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False).encode()).hexdigest()
    request=source.RecognitionRequest('','',json.dumps(old),request.document_inputs)
    blind,_=decision.prepare_automatic_verification(request,scope)
    for prompt in (v4.system,blind):
        assert 'requested probes and all context materially required' in prompt
        assert '[unassessed handwritten mark]' in prompt
        assert 'identity, content and authenticity remain unassessed' in prompt
        assert 'status uncertain' in prompt
        assert 'Never use readable to hide a material uncertainty' in prompt


@pytest.mark.parametrize('fault',['status','uncertainties','value','unit','row','column','citation'])
def test_v4_never_relaxes_requested_claim_or_uncertainty_validation(fault):
    request=source.prepare_recognition(evidence(),session_as_of='2026-10-12',request_version=V4)
    raw=recognition(request);page=raw['pages'][0];probe=page['probes'][0]
    page['transcription']+='\n[unassessed handwritten mark]'
    probe['citation']={'scope':'page'}
    assert source.validate_recognition(json.dumps(raw),request)['validation_status']=='structurally_valid'
    if fault=='status':page['status']='uncertain'
    elif fault=='uncertainties':page['uncertainties']=['Requested count is unreadable.']
    elif fault=='value':probe['printed_value']='1,204.51'
    elif fault=='unit':probe['unit']=None
    elif fault=='row':probe['row']='Reven'
    elif fault=='column':probe['column']='Future'
    else:probe['citation']={'scope':'other'}
    with pytest.raises(ValueError,match='^invalid_bounded_document$'):
        source.validate_recognition(json.dumps(raw),request)


def test_v4_does_not_relabel_the_native_v3_uncertain_response():
    context=deepcopy(NATIVE['source_identity']);context['recognition_request_version']=V4
    context.pop('request_identity_sha256');context['request_identity_sha256']=hashlib.sha256(json.dumps(
        context,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False).encode()).hexdigest()
    raw=json.loads(NATIVE['primary_response']);raw['request_identity_sha256']=context['request_identity_sha256']
    request=source.RecognitionRequest('','',json.dumps(context),())
    # Constructed request-identity control only. The uncertain native content
    # remains uncertain and cannot become successful through a version change.
    with pytest.raises(ValueError,match='^invalid_bounded_document$'):
        source.validate_recognition(json.dumps(raw),request)


def test_analyzer_forwards_explicit_v4_profile_selection_through_real_validation(analyzer):
    worker,calls,faults=analyzer
    worker.config['autoresearch']['bounded_document_recognition_request_version']=V4
    request=source.prepare_recognition(evidence(),session_as_of='2026-10-12',request_version=V4)
    identity=json.loads(request.context_json)['request_identity_sha256']
    # Constructed external-transport response identity; real producer/parser and
    # analyzer remain in place. This establishes wiring, not native recognition.
    faults.append(lambda raw:raw.update(request_identity_sha256=identity))
    result=worker.analyze_bounded_document_evidence(evidence(),session_as_of='2026-10-12')
    assert result['analysis_status']=='validated'
    retained=result['bounded_document_research']
    assert retained['source_identity']['recognition_request_version']==V4
    assert 'unrequested_handwriting_and_authenticity_unassessed' in retained['recognition']['residual_failures']
    assert len(calls)==2 and retained['thesis_attempt']['validation_status']=='validated'
