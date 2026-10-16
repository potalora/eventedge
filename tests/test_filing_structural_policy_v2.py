"""Prospective opt-in structural-only PDF receipts never admit analysis."""
from copy import deepcopy
import hashlib

import pytest

from test_filing_evidence import ACCESSION, OBSERVED, submission
from test_filing_pdf import pdf_bytes
from test_filing_pdf_visual import image_pdf
from tradingagents.strategies.data_sources import filing_supported_policy as policy
from tradingagents.strategies.data_sources.filing_evidence import frame_submission, select_primary, build_evidence_from_buffer
from tradingagents.strategies.data_sources.filing_assessment import _validate_evidence

V2 = 'verified_supported_documents_v2'


def case(*, selected_pdf=None, extra=None, selected_policy=V2):
    pdf = selected_pdf if selected_pdf is not None else image_pdf()
    docs = [('8-K', 'main.htm', '<a href="selected.pdf">Exhibit</a>' + ('<a href="broken.htm">Other</a>' if extra else '')),
            ('EX-99', 'selected.pdf', 'PLACEHOLDER')]
    if extra:
        docs.append(('EX-100', 'broken.htm', extra))
    raw = submission(docs).replace(b'PLACEHOLDER', b'<PDF>\n'+pdf+b'</PDF>')
    framed = frame_submission(raw, expected_accession=ACCESSION, expected_form='8-K',
        expected_date='2026-09-30', observed_at=OBSERVED, max_submission_bytes=policy.MAX_SUBMISSION_BYTES)
    evidence = build_evidence_from_buffer(select_primary(framed, raw), raw, supported_document_policy=selected_policy)
    evidence['source_url'] = f'https://www.sec.gov/Archives/edgar/data/2065397/{ACCESSION.replace("-", "")}/{ACCESSION}.txt'
    original = {'identity': {k: evidence[k] for k in ('accession','form','filing_date','source_url')},
        'size':len(raw), 'sha256':hashlib.sha256(raw).hexdigest(), 'observed_at':OBSERVED, 'complete':True}
    proof = policy.build_local_source_proof(evidence, original=original, raw_submission=raw)
    inputs = dict(source_proofs={ACCESSION:proof}, originals={ACCESSION:original},
        occurrences=[{'collection':'filings','index':0}])
    return evidence, raw, inputs


def test_v2_structural_visual_receipt_defers_without_native_text_or_image_validity():
    evidence, raw, inputs = case()
    assert evidence['structural_status'] == 'complete'
    unit = evidence['units'][1]
    pdf = evidence['pdf_evidence']['selected.pdf']
    assert unit['representation'] == 'structural_visual_pdf' and unit['text'] == ''
    assert pdf['native_text_complete'] is False
    assert pdf['encoded_image_validity'] == pdf['native_unicode'] == pdf['text_visibility'] == 'unverified'
    proof = inputs['source_proofs'][ACCESSION]
    assert proof['policy'] == V2 and proof['version'] == 'supported-document-disposition-v2'
    assert proof['residual_failures'] == []
    receipt = policy.evaluate_supported_disposition(evidence, **inputs)
    assert receipt['status'] == 'deferred' and receipt['order_authority'] is False
    assert receipt['policy'] == V2 and receipt['version'] == 'supported-document-disposition-v2'
    assert policy.validate_supported_disposition(receipt, evidence, **inputs, raw_submissions={ACCESSION:raw}) == receipt
    _validate_evidence(evidence, allow_unsupported_documents=True)
    with pytest.raises(ValueError):
        _validate_evidence(evidence)


def test_v2_plain_pdf_still_requires_native_text_and_is_admitted():
    evidence, raw, inputs = case(selected_pdf=pdf_bytes())
    assert evidence['units'][1]['representation'] == 'native_pdf_text'
    assert evidence['pdf_evidence']['selected.pdf']['native_text_complete'] is True
    assert policy.evaluate_supported_disposition(evidence, **inputs)['status'] == 'admitted'
    _validate_evidence(evidence)


def test_v2_structural_proof_does_not_clear_other_selected_failure():
    evidence, raw, inputs = case(extra='<html></html>')
    result = policy.evaluate_supported_disposition(evidence, **inputs)
    assert result['status'] == 'failed' and result['reasons']
    assert inputs['source_proofs'][ACCESSION]['residual_failures']


def test_v2_structural_parser_error_remains_failure():
    evidence, raw, inputs = case(selected_pdf=b'%PDF-1.7\ninvalid\n%%EOF')
    assert evidence['structural_status'] == 'insufficient'
    assert policy.evaluate_supported_disposition(evidence, **inputs)['status'] == 'failed'


@pytest.mark.parametrize('field,value', [('native_text_complete',True),('encoded_image_validity','verified'),
    ('native_unicode','verified'),('text_visibility','verified'),('order_authority',True),('page_count',2)])
def test_resealed_forged_structural_claim_rejected(field,value):
    evidence, raw, inputs = case()
    evidence['pdf_evidence']['selected.pdf'][field] = value
    proof = inputs['source_proofs'][ACCESSION]
    proof['evidence_sha256'] = policy._digest(evidence)
    proof['pdf']['selected.pdf'] = policy._pdf_binding(evidence['pdf_evidence']['selected.pdf'])
    proof['source_proof_sha256'] = policy._digest({k:v for k,v in proof.items() if k!='source_proof_sha256'})
    assert policy.evaluate_supported_disposition(evidence, **inputs)['status'] == 'failed'
    with pytest.raises(ValueError):
        _validate_evidence(evidence, allow_unsupported_documents=True)


def test_v1_structural_receipt_is_not_accepted_by_relabeling():
    evidence, raw, inputs = case()
    evidence['supported_document_policy'] = policy.POLICY
    proof = policy.build_local_source_proof(evidence, original=inputs['originals'][ACCESSION], raw_submission=raw)
    inputs['source_proofs'][ACCESSION] = proof
    assert policy.evaluate_supported_disposition(evidence, **inputs)['status'] == 'failed'


def test_v2_source_proof_policy_mismatch_fails():
    evidence, raw, inputs = case()
    proof = inputs['source_proofs'][ACCESSION]
    proof.update(policy=policy.POLICY, version=policy.VERSION)
    proof['source_proof_sha256'] = policy._digest({k:v for k,v in proof.items() if k!='source_proof_sha256'})
    assert policy.evaluate_supported_disposition(evidence, **inputs)['status'] == 'failed'


def acquired_case(tmp_path, monkeypatch, *, policy_value=V2, malformed=False):
    """Run the real acquisition owner, parser child and hydration using local bytes."""
    import time
    from test_filing_comparison_wiring import Registry
    from test_filing_hydration import Source
    from tradingagents.strategies.learning.event_monitor import EventMonitor
    from tradingagents.strategies.data_sources import filing_acquisition
    from tradingagents.strategies.data_sources.request_policy import provider_budget
    from test_filing_acquisition_wiring import CONFIG
    _, raw, _ = case(selected_pdf=b'%PDF-1.7\ninvalid\n%%EOF' if malformed else None)
    record = dict(adsh=ACCESSION, form_type='8-K', file_date='2026-09-30', ciks=['2065397'],
        file_url=f'https://www.sec.gov/Archives/edgar/data/2065397/{ACCESSION.replace("-", "")}/{ACCESSION}.txt')
    class LocalResponse:
        status_code = 200
        def __init__(self, url): self.url=url
        def iter_content(self, chunk_size): yield raw
        def close(self): pass
    monkeypatch.setattr(filing_acquisition, 'provider_request', lambda _p,_m,url,**k: LocalResponse(url))
    source = Source([record])
    source.is_available = lambda: True
    source.get_complete_submission = lambda url, **kwargs: filing_acquisition.acquire_complete_submission('offline-fixture',url,**kwargs)
    (tmp_path/'spool').mkdir()
    monitor = EventMonitor(Registry(source), filing_policy=CONFIG['filing_evidence_policy'],
        parser_policy=CONFIG['filing_parser_policy'], acquisition_policy=CONFIG['filing_acquisition_policy'],
        material_policy=policy_value, spool_root=tmp_path/'spool')
    with provider_budget('edgar',time.monotonic()+15,limits=()):
        graph=monitor.hydrate_collections({'filings':[record], 'pqc_filings':[deepcopy(record)]},max_workers=2)
    data={'edgar':{'filing_evidence':graph, **graph['collections'], 'error':'full filing evidence incomplete'}}
    path=tmp_path/'retained.txt';path.write_bytes(raw);path.chmod(0o400)
    return data, {**CONFIG,'filing_material_policy':policy_value}, {ACCESSION:path}


def test_v2_real_acquisition_parser_hydration_raw_replay_and_no_analysis(tmp_path,monkeypatch):
    from tradingagents.strategies.orchestration.filing_supported_audit import validate_supported_raw_capture
    from tradingagents.strategies.orchestration.filing_material_validation import validate_filing_material_policy, signal_edgar
    from tradingagents.strategies.orchestration.source_inputs import source_configuration_fingerprint
    data, config, paths = acquired_case(tmp_path,monkeypatch)
    graph=data['edgar']['filing_evidence']
    assert graph['corpus'][ACCESSION]['supported_document_policy']==V2
    assert graph['supported_dispositions'][ACCESSION]['status']=='deferred'
    scope=validate_filing_material_policy(data,config)
    assert scope['required_rows']==scope['deferred_rows']==scope['strict_failed_rows']==2
    assert scope['scoped_complete'] is True and scope['strict_complete'] is False
    projection=signal_edgar(data['edgar'],scope)
    assert projection['filings']==projection['pqc_filings']==[]
    assert projection['filing_evidence']['corpus']=={}
    audit=validate_supported_raw_capture(data,config,paths,check=lambda:10)
    assert audit['raw_audit_complete'] is True and audit['completed_originals']==1
    assert audit['verified_source_proofs']==1 and audit['order_authority'] is False
    assert source_configuration_fingerprint({'autoresearch':config}) != source_configuration_fingerprint(
        {'autoresearch':{**config,'filing_material_policy':policy.POLICY}})
    with pytest.raises(ValueError):
        validate_supported_raw_capture(data,{**config,'filing_material_policy':policy.POLICY},paths,check=lambda:10)
    with pytest.raises(ValueError):
        validate_filing_material_policy(data,{**config,'filing_material_policy':policy.POLICY})


def test_v2_resealed_original_structural_change_is_detected_by_raw_replay(tmp_path,monkeypatch):
    from tradingagents.strategies.orchestration.filing_supported_audit import validate_supported_raw_capture
    data,config,paths=acquired_case(tmp_path,monkeypatch)
    graph=data['edgar']['filing_evidence']
    # Re-sealing the supplied attestation does not replace independent raw evidence.
    proof=graph['supported_source_proofs'][ACCESSION]
    proof['required_exhibits']=['nonexistent.pdf']
    proof['source_proof_sha256']=policy._digest({k:v for k,v in proof.items() if k!='source_proof_sha256'})
    from tradingagents.strategies.orchestration.filing_supported_scope import build_supported_dispositions
    from tradingagents.strategies.orchestration.filing_material_validation import build_material_scope
    graph['supported_dispositions']=build_supported_dispositions(graph,graph['collections'])
    graph['material_scope']=build_material_scope(graph,graph['collections'])
    with pytest.raises(ValueError,match='invalid_supported_raw_capture'):
        validate_supported_raw_capture(data,config,paths,check=lambda:10)


def test_v1_same_progressive_source_remains_failed_and_retained(tmp_path,monkeypatch):
    from tradingagents.strategies.orchestration.filing_supported_audit import validate_supported_raw_capture
    data,config,paths=acquired_case(tmp_path,monkeypatch,policy_value=policy.POLICY)
    graph=data['edgar']['filing_evidence']
    assert graph['supported_dispositions'][ACCESSION]['status']=='failed'
    assert graph['material_scope']['scoped_complete'] is False
    assert len(graph['acquisition_scope']['originals'])==1
    assert validate_supported_raw_capture(data,config,paths,check=lambda:10)['completed_originals']==1


def test_v2_broken_pdf_replay_keeps_failure_and_original(tmp_path,monkeypatch):
    from tradingagents.strategies.orchestration.filing_supported_audit import validate_supported_raw_capture
    data,config,paths=acquired_case(tmp_path,monkeypatch,malformed=True)
    graph=data['edgar']['filing_evidence']
    assert graph['supported_dispositions'][ACCESSION]['status']=='failed'
    assert graph['material_scope']['scoped_complete'] is False
    assert validate_supported_raw_capture(data,config,paths,check=lambda:10)['completed_originals']==1


@pytest.mark.parametrize('field,value', [('decoded_bytes',16*1024**2+1),
    ('decoded_stream_bytes',32*1024**2+1),('inventory_bytes',32*1024**2+1)])
def test_resealed_structural_receipt_cannot_claim_bounds_above_policy(field,value):
    evidence,raw,inputs=case()
    evidence['pdf_evidence']['selected.pdf'][field]=value
    with pytest.raises(ValueError):
        _validate_evidence(evidence,allow_unsupported_documents=True)


@pytest.mark.parametrize('malformed',[[],{},42])
def test_policy_schema_rejects_non_string_values_as_validation_errors(malformed):
    evidence,raw,inputs=case()
    evidence['supported_document_policy']=malformed
    with pytest.raises(ValueError):
        _validate_evidence(evidence,allow_unsupported_documents=True)
    with pytest.raises(ValueError):
        policy.build_local_source_proof(evidence,original=inputs['originals'][ACCESSION],raw_submission=raw)
