"""Real original bytes and native receipts, with rehashed adversarial proofs."""
from copy import deepcopy
import hashlib
import json
import mmap
from pathlib import Path

import pytest
from test_filing_evidence import submission, parse, ACCESSION, OBSERVED
from test_filing_pdf import pdf_bytes
from tradingagents.strategies.data_sources.filing_evidence import build_evidence, parse_submission
from tradingagents.strategies.data_sources.filing_pdf import extract_pdf_evidence
from tradingagents.strategies.data_sources import filing_supported_policy as policy
from tradingagents.strategies.data_sources.filing_publisher_facts import publisher_facts


def source(raw=None):
    raw = raw or submission()
    parsed = parse(raw)
    evidence = build_evidence(parsed)
    pdfs = {}
    for issue in list(evidence['issues']):
        if issue['code'] != 'unsupported_document_format':
            continue
        doc = next(d for d in parsed['documents'] if d['filename'] == issue['filename'])
        result = extract_pdf_evidence(doc['body'], **policy.PDF_LIMITS)
        pdfs[doc['filename']] = result
        evidence['units'].append({**{k:v for k,v in doc.items() if k != 'body'},
            'text': result['text'], 'text_sha256': result['text_sha256'],
            'text_start': 0, 'text_end': len(result['text']),
            'representation': 'native_pdf_text', 'selection': 'dependency'})
        evidence['issues'].remove(issue)
    evidence['structural_status'] = 'complete' if not evidence['issues'] else 'insufficient'
    if pdfs:
        evidence['pdf_evidence'] = pdfs
    evidence['source_url'] = f'https://www.sec.gov/Archives/edgar/data/2065397/{ACCESSION.replace("-", "")}/{ACCESSION}.txt'
    original = {'identity': {k: evidence[k] for k in ('accession', 'form', 'filing_date', 'source_url')},
        'observed_at': OBSERVED, 'size': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(), 'complete': True}
    proof = policy.build_local_source_proof(evidence, original=original, raw_submission=raw)
    inputs = {'source_proofs': {ACCESSION: proof}, 'originals': {ACCESSION: original},
        'occurrences': [{'collection': 'filings', 'index': 0}]}
    return evidence, raw, inputs


def pdf_source(*, image=False, text=b'BT /F1 12 Tf 72 700 Td (Hello.) Tj ET'):
    raw = submission([('8-K','main.htm','<a href="selected.pdf">Exhibit</a>'), ('EX-99','selected.pdf','PLACEHOLDER')])
    return source(raw.replace(b'PLACEHOLDER', b'<PDF>\n' + pdf_bytes((text,), image=image) + b'</PDF>'))


def run(case, **overrides):
    evidence, raw, inputs = case
    return policy.evaluate_supported_disposition(evidence, **{**inputs, **overrides})


def audit(case, receipt):
    evidence, raw, inputs = case
    return policy.validate_supported_disposition(receipt, evidence, **inputs, raw_submissions={ACCESSION: raw})


def reseal(proof):
    proof['source_proof_sha256'] = policy._digest({k:v for k,v in proof.items() if k != 'source_proof_sha256'})


def test_plain_source_admission_does_not_certify_model_or_orders():
    case = source()
    receipt = run(case)
    assert receipt['status'] == 'admitted'
    assert receipt['assessment_status'] == 'not_certified'
    assert receipt['order_authority'] is False
    assert receipt['original_full_analysis_coverage'] is False
    assert audit(case, receipt) == receipt
    json.dumps(receipt, allow_nan=False)


def test_pdf_plain_text_is_admitted_with_complete_pages():
    case = pdf_source()
    receipt = run(case)
    assert receipt['status'] == 'admitted', receipt['residual_failures']
    assert case[2]['source_proofs'][ACCESSION]['pdf']['selected.pdf']['page_count'] == 1
    assert audit(case, receipt) == receipt


def test_image_only_deferred_without_interpreting_materiality():
    case = pdf_source(image=True, text=b'')
    receipt = run(case)
    assert receipt['status'] == 'deferred', receipt['residual_failures']
    reason = receipt['reasons'][0]
    assert reason['code'] == 'unsupported_selected_visual_content'
    assert reason['visual_meaning'] == 'unresolved'
    assert reason['materiality'] == 'not_inferred_selected_content_obligation'
    assert audit(case, receipt) == receipt


def test_mixed_visual_plus_text_capability_gap_stays_failed():
    receipt = run(pdf_source(image=True))
    assert receipt['status'] == 'failed'
    assert receipt['reasons'][0]['code'] == 'unsupported_selected_visual_content'
    assert receipt['residual_failures'][0]['failure']['code'] == 'pdf_capability_limit'


@pytest.mark.parametrize('code', ['invalid_filing_citations','source_deadline','model_deadline',
    'missing_agreement','invalid_model_response','spool_cleanup_failure'])
def test_visual_does_not_launder_independent_failure(code):
    receipt = run(pdf_source(image=True, text=b''), residual_failures=[{'code': code}])
    assert receipt['status'] == 'failed'
    assert receipt['reasons']
    assert receipt['residual_failures'][0]['failure']['code'] == code


def test_missing_selected_pdf_receipt_fails_closed():
    evidence, raw, inputs = pdf_source(image=True, text=b'')
    evidence['pdf_evidence'].clear()
    inputs['source_proofs'][ACCESSION] = policy.build_local_source_proof(evidence,
        original=inputs['originals'][ACCESSION], raw_submission=raw)
    assert run((evidence, raw, inputs))['status'] == 'failed'


def test_mutated_raw_rejected_by_independent_audit():
    case = source()
    receipt = run(case)
    evidence, raw, inputs = case
    with pytest.raises(policy.SupportedDispositionError, match='invalid_raw_local_source_proof'):
        policy.validate_supported_disposition(receipt, evidence, **inputs,
            raw_submissions={ACCESSION: raw.replace(b'narrative', b'fabricate')})


def test_rehashed_invented_model_semantic_claim_rejected_by_raw_audit():
    case = source()
    proof = case[2]['source_proofs'][ACCESSION]
    proof['reasons'] = [{'code': 'unresolved_publisher_facts', 'facts': [{'model_said': 'inconsistent'}]}]
    reseal(proof)
    provisional = run(case)
    assert provisional['authority'] == 'bounded_parser_attestation_requires_raw_capture_audit'
    with pytest.raises(policy.SupportedDispositionError, match='invalid_raw_local_source_proof'):
        audit(case, provisional)


def test_rehashed_stripped_capability_failure_rejected_by_parent_binding():
    case = pdf_source(image=True)
    proof = case[2]['source_proofs'][ACCESSION]
    proof['residual_failures'] = []
    reseal(proof)
    assert run(case)['status'] == 'failed'


def test_omitted_duplicate_and_changed_occurrences():
    case = source()
    assert run(case, occurrences=[])['status'] == 'failed'
    assert run(case, occurrences=[{'row':1},{'row':1}])['status'] == 'failed'
    receipt = run(case)
    receipt['occurrences'] = [{'collection':'filings','index':1}]
    receipt['manifest_sha256'] = policy._digest({k:v for k,v in receipt.items() if k != 'manifest_sha256'})
    with pytest.raises(policy.SupportedDispositionError):
        audit(case, receipt)


def test_readonly_mmap_original_has_no_full_copy_requirement(tmp_path):
    evidence, raw, inputs = source()
    path = tmp_path/'original'
    path.write_bytes(raw)
    with path.open('rb') as file, mmap.mmap(file.fileno(), 0, access=mmap.ACCESS_READ) as mapped:
        proof = policy.build_local_source_proof(evidence,
            original=inputs['originals'][ACCESSION], raw_submission=mapped)
    assert proof == inputs['source_proofs'][ACCESSION]


def test_unresolved_local_dependency_remains_failure():
    case = source(submission([('8-K','main.htm','<a href="absent.htm">Agreement</a>')]))
    assert run(case)['status'] == 'failed'


def cbl():
    path = Path(__file__).parent / 'fixtures' / 'filing_evidence'
    raw = (path/'native_13d_cbl_publisher.txt').read_bytes()
    metadata = next(r for r in json.loads((path/'publisher_fact_sources.json').read_text()) if r['accession'] == '0001193125-26-418126')
    assert hashlib.sha256(raw).hexdigest() == metadata['sha256']
    parsed = parse_submission(raw, expected_accession=metadata['accession'], expected_form='SCHEDULE 13D/A',
        expected_date='2026-10-08', observed_at=metadata['observed_at'])
    return build_evidence(parsed), parsed['documents'][parsed['primary_index']]['body']


def test_cbl_raw_zero_aggregate_positive_percentage_is_typed_same_reporter_fact():
    evidence, body = cbl()
    findings = publisher_facts(evidence, body)
    assert len(findings) == 3
    assert all(f['fields'][0]['value'] == '0' and f['fields'][1]['value'] == '19.7' for f in findings)
    assert len({f['reporter']['cik'] for f in findings}) == 3
    assert all(f['event_date'] == '10/06/2026' for f in findings)
    assert findings[0]['fields'][0]['path'].endswith('reportingPersonInfo[1]/aggregateAmountOwned')


def test_publisher_findings_require_hash_bound_original():
    evidence, body = cbl()
    with pytest.raises(ValueError):
        publisher_facts(evidence, body.replace(b'<aggregateAmountOwned>0', b'<aggregateAmountOwned>1'))


def ownership_sources(*filings, visual_current=False):
    from test_filing_ownership_lineage import case as lineage_case, FILER, SUBJECT
    from test_filing_evidence import role
    from tradingagents.strategies.data_sources.filing_ownership_lineage import ownership_facts, ownership_lineage
    enriched, raws = [], {}
    for index, (evidence, facts, body) in enumerate(filings):
        evidence = deepcopy(evidence)
        docs = [(evidence['form'], 'primary.xml', body.decode().strip('\r'))]
        visual = visual_current and index == len(filings) - 1
        if visual:
            docs.append(('EX-99','selected.pdf','PLACEHOLDER'))
        raw = submission(docs, form=evidence['form'],
            roles=role('FILED-BY', FILER, 'Source Filer') + role('SUBJECT-COMPANY', SUBJECT, 'Subject'))
        raw = raw.replace(ACCESSION.encode(), evidence['accession'].encode()).replace(b'20260930', evidence['filing_date'].replace('-','').encode())
        raw = raw.replace(b'<TYPE>', b'<ACCEPTANCE-DATETIME>'+evidence['accepted_at'].encode()+b'\r<TYPE>',1)
        if visual:
            raw = raw.replace(b'PLACEHOLDER', b'<PDF>\n'+pdf_bytes((b'',), image=True)+b'</PDF>')
        parsed = parse_submission(raw, expected_accession=evidence['accession'], expected_form=evidence['form'],
            expected_date=evidence['filing_date'], observed_at=evidence['observed_at'])
        evidence = build_evidence(parsed)
        if visual:
            doc = parsed['documents'][1]
            pdf = extract_pdf_evidence(doc['body'], **policy.PDF_LIMITS)
            evidence['units'].append({**{k:v for k,v in doc.items() if k != 'body'},
                'text':pdf['text'],'text_sha256':pdf['text_sha256'],'text_start':0,'text_end':len(pdf['text']),
                'selection':'dependency','representation':'native_pdf_text'})
            evidence['pdf_evidence'] = {'selected.pdf':pdf}
            evidence['issues'] = []
            evidence['structural_status'] = 'complete'
        acc = evidence['accession']
        evidence['source_url'] = f'https://www.sec.gov/Archives/edgar/data/2/{acc.replace("-", "")}/{acc}.txt'
        body = parsed['documents'][0]['body']
        enriched.append((evidence, ownership_facts(evidence, body), body))
        raws[acc] = raw
    submissions, facts, histories, archives, bodies = lineage_case(*enriched)
    current = enriched[-1][0]
    originals, proofs = {}, {}
    for acc, evidence in submissions.items():
        originals[acc] = {'identity':{k:evidence[k] for k in ('accession','form','filing_date','source_url')},
            'observed_at':evidence['observed_at'],'complete':True,'size':len(raws[acc]),'sha256':hashlib.sha256(raws[acc]).hexdigest()}
        proofs[acc] = policy.build_local_source_proof(evidence, original=originals[acc], raw_submission=raws[acc])
    ownership = {'receipt':ownership_lineage(current, facts, submissions, histories, archives),
        'facts_by_accession':facts,'submissions':submissions,'histories':histories,'archives':archives,'primary_bodies':bodies}
    inputs = {'source_proofs':proofs,'originals':originals,'ownership':ownership,
        'occurrences':[{'collection':'activist_13d','index':0}]}
    return current, raws, inputs


def test_complete_ownership_chain_admitted_and_independently_revalidated():
    from test_filing_ownership_lineage import filing
    case = ownership_sources(filing(1,'2026-01-01'), filing(2,'2026-09-30',amendment=1))
    current, raws, inputs = case
    receipt = policy.evaluate_supported_disposition(current, **inputs)
    assert receipt['status'] == 'admitted', receipt['residual_failures']
    assert policy.validate_supported_disposition(receipt,current,raw_submissions=raws,**inputs) == receipt


def test_complete_ambiguous_lineage_defers_with_all_candidates_acquired():
    from test_filing_ownership_lineage import filing
    current, raws, inputs = ownership_sources(filing(1,'2026-01-01'), filing(2,'2026-02-01'), filing(3,'2026-09-30',amendment=1))
    receipt = policy.evaluate_supported_disposition(current, **inputs)
    assert receipt['status'] == 'deferred', receipt['residual_failures']
    assert receipt['reasons'][0]['code'] == 'ambiguous_ownership_lineage'
    assert policy.validate_supported_disposition(receipt,current,raw_submissions=raws,**inputs) == receipt


def test_visual_current_cannot_exempt_missing_ownership_history():
    from test_filing_ownership_lineage import filing
    from tradingagents.strategies.data_sources.filing_ownership_lineage import ownership_lineage
    current, raws, inputs = ownership_sources(filing(1,'2026-01-01'), filing(2,'2026-09-30',amendment=1), visual_current=True)
    bundle = inputs['ownership']
    bundle['histories'].clear()
    bundle['receipt'] = ownership_lineage(current,bundle['facts_by_accession'],bundle['submissions'],bundle['histories'],bundle['archives'])
    receipt = policy.evaluate_supported_disposition(current, **inputs)
    assert receipt['status'] == 'failed'
    assert any(r['code'] == 'unsupported_selected_visual_content' for r in receipt['reasons'])
    assert any(f['code'] == 'unclosed_ownership_lineage' for f in receipt['residual_failures'])


def test_visual_and_ambiguous_are_simultaneous_reasons():
    from test_filing_ownership_lineage import filing
    current, raws, inputs = ownership_sources(filing(1,'2026-01-01'), filing(2,'2026-02-01'), filing(3,'2026-09-30',amendment=1), visual_current=True)
    receipt = policy.evaluate_supported_disposition(current, **inputs)
    assert receipt['status'] == 'deferred', receipt['residual_failures']
    assert {r['code'] for r in receipt['reasons']} == {'unsupported_selected_visual_content','ambiguous_ownership_lineage'}


def test_rehashed_forged_lineage_facts_rejected_from_raw_primary():
    from test_filing_ownership_lineage import filing
    current, raws, inputs = ownership_sources(filing(1,'2026-01-01'))
    facts = inputs['ownership']['facts_by_accession'][current['accession']]
    facts['reporters'][0]['name'] = 'Invented Reporter'
    facts['facts_sha256'] = policy._digest({k:v for k,v in facts.items() if k != 'facts_sha256'})
    assert policy.evaluate_supported_disposition(current, **inputs)['status'] == 'failed'


def test_missing_dependency_original_fails_even_with_ambiguous_lineage():
    from test_filing_ownership_lineage import filing
    current, raws, inputs = ownership_sources(filing(1,'2026-01-01'), filing(2,'2026-02-01'), filing(3,'2026-09-30',amendment=1))
    del inputs['originals']['0001193125-26-000001']
    receipt = policy.evaluate_supported_disposition(current, **inputs)
    assert receipt['status'] == 'failed'
    assert any(r['code'] == 'ambiguous_ownership_lineage' for r in receipt['reasons'])


@pytest.mark.parametrize('before,after', [
    (b'<aggregateAmountOwned>0',b'<aggregateAmountOwned>6108794'),
    (b'<percentOfClass>19.7',b'<percentOfClass>0'),
    (b'<percentOfClass>19.7',b'<percentOfClass>101'),
    (b'<isAggregateExcludeShares>N',b'<isAggregateExcludeShares>Y'),
    (b'<dateOfEvent>10/06/2026</dateOfEvent>',b''),
])
def test_publisher_check_does_not_generalize_beyond_defined_pair(before,after):
    evidence, body = cbl()
    body = body.replace(before,after)
    evidence['units'][0]['body_sha256'] = hashlib.sha256(body).hexdigest()
    assert publisher_facts(evidence,body) == []


def test_adur_option_eligibility_is_not_a_publisher_contradiction():
    path = Path(__file__).parent / 'fixtures' / 'filing_evidence'
    raw = (path/'native_13d_adur_options.txt').read_bytes()
    parsed = parse_submission(raw,expected_accession='0001062993-26-005078',expected_form='SCHEDULE 13D/A',
        expected_date='2026-09-25',observed_at=OBSERVED)
    evidence = build_evidence(parsed)
    assert publisher_facts(evidence,parsed['documents'][0]['body']) == []


def test_pure_parser_package_imports_no_provider_stack(monkeypatch):
    import importlib.util
    import sys
    import types
    import builtins
    package = '_supported_policy_pure_test'
    module = types.ModuleType(package)
    module.__path__ = [str(Path(policy.__file__).parent)]
    monkeypatch.setitem(sys.modules, package, module)
    original_import = builtins.__import__
    def guarded(name, *args, **kwargs):
        if name.split('.')[0] in {'requests','httpx','tradingagents'}:
            raise AssertionError('provider import in pure parser boundary: ' + name)
        return original_import(name,*args,**kwargs)
    monkeypatch.setattr(builtins,'__import__',guarded)
    imported = original_import(package+'.filing_supported_policy',fromlist=['build_local_source_proof'])
    assert imported.POLICY == policy.POLICY


def test_parent_rejects_model_claim_without_source_fact_projection():
    case = source()
    proof = case[2]['source_proofs'][ACCESSION]
    proof['reasons'] = [{'code':'unresolved_publisher_facts','facts':[{'assertion':'model claimed inconsistency'}]}]
    reseal(proof)
    assert run(case)['status'] == 'failed'


def test_visual_reason_and_specific_selected_technical_failure_are_both_retained():
    from tradingagents.strategies.data_sources.filing_evidence import frame_submission, select_primary, build_evidence_from_buffer
    raw = submission([('8-K','main.htm','<a href="selected.pdf">Exhibit</a><a href="broken.htm">Exhibit</a>'),
        ('EX-99','selected.pdf','PLACEHOLDER'),('EX-100','broken.htm','<html></html>')])
    raw = raw.replace(b'PLACEHOLDER',b'<PDF>\n'+pdf_bytes((b'',),image=True)+b'</PDF>')
    framed = frame_submission(raw,expected_accession=ACCESSION,expected_form='8-K',expected_date='2026-09-30',
        observed_at=OBSERVED,max_submission_bytes=policy.MAX_SUBMISSION_BYTES)
    evidence = build_evidence_from_buffer(select_primary(framed,raw),raw,supported_document_policy=policy.POLICY)
    evidence['source_url'] = f'https://www.sec.gov/Archives/edgar/data/2065397/{ACCESSION.replace("-", "")}/{ACCESSION}.txt'
    original={'identity':{k:evidence[k] for k in ('accession','form','filing_date','source_url')},
        'sha256':hashlib.sha256(raw).hexdigest(),'size':len(raw),'observed_at':OBSERVED,'complete':True}
    proof=policy.build_local_source_proof(evidence,original=original,raw_submission=raw)
    assert proof['reasons'][0]['code']=='unsupported_selected_visual_content'
    assert any(f.get('issue',{}).get('code')=='empty_selected_document' for f in proof['residual_failures'])
    result=policy.evaluate_supported_disposition(evidence,source_proofs={ACCESSION:proof},originals={ACCESSION:original},
        occurrences=[{'collection':'filings','index':0}])
    assert result['status']=='failed' and result['reasons']
