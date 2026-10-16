"""Independent offline adversarial review of supported disposition boundaries."""
import base64
from copy import deepcopy

import pytest

from test_filing_assessment import binding
from test_filing_ownership_lineage import filing, lineage
from test_filing_supported_policy import ownership_sources, policy
from test_filing_supported_scope import CONFIG
from tradingagents.strategies.data_sources.filing_assessment import prepare_request
from tradingagents.strategies.data_sources.filing_material_policy import TEN_POLICY, policy_manifest
from tradingagents.strategies.orchestration.filing_material_validation import (
    build_material_scope, signal_edgar, validate_filing_material_policy,
)
from tradingagents.strategies.orchestration.filing_supported_scope import (
    build_supported_dispositions, ownership_bundle,
)


def test_review_prospective_policy_preserves_every_exact_ten_identity():
    previous = policy_manifest(TEN_POLICY)
    prospective = policy_manifest(policy.POLICY)
    assert prospective['identities'] == previous['identities']
    assert len(prospective['identities']) == 10
    assert prospective['supported_scope']['categories'] == [
        'unsupported_selected_visual_content', 'ambiguous_ownership_lineage',
        'unresolved_publisher_facts',
    ]


@pytest.mark.parametrize('field', [
    'aggregateAmountOwned', 'percentOfClass', 'isAggregateExcludeShares', 'dateOfEvent',
])
def test_review_mixed_publisher_field_cannot_be_laundered_as_contradiction(field):
    record = filing(21, '2026-09-30')
    body = record[2].replace(b'<securitiesClassTitle>',
        b'<dateOfEvent>09/30/2026</dateOfEvent><securitiesClassTitle>')
    body = body.replace(b'</reportingPersonInfo>',
        b'<aggregateAmountOwned>0</aggregateAmountOwned><percentOfClass>10</percentOfClass>'
        b'<isAggregateExcludeShares>N</isAggregateExcludeShares></reportingPersonInfo>')
    ending = ('</' + field + '>').encode()
    body = body.replace(ending, b'<clarification>100</clarification>' + ending)
    evidence, raw, original, proof = _policy_source(record, body)
    assert evidence['structural_status'] == 'insufficient'
    assert proof['residual_failures'], proof
    inputs = dict(source_proofs={evidence['accession']: proof},
        originals={evidence['accession']: original},
        occurrences=[{'collection': 'activist_13d', 'index': 0}])
    receipt = policy.evaluate_supported_disposition(evidence, **inputs)
    assert receipt['status'] == 'failed', receipt
    assert policy.validate_supported_disposition(receipt, evidence,
        raw_submissions={evidence['accession']: raw}, **inputs) == receipt


def test_review_deferred_negative_candidate_stays_available_to_admitted_lineage():
    base = filing(11, '2026-01-01')
    other = filing(12, '2026-05-01', amendment=1,
        people=(('Other Person', '0000000042'),))
    current = filing(13, '2026-09-30', amendment=1)
    current_evidence, originals, inputs = ownership_sources(base, other, current)
    source = inputs['ownership']
    corpus = source['submissions']
    for accession, evidence in corpus.items():
        evidence.update(supported_document_policy=policy.POLICY, pdf_evidence={},
            ownership_facts=source['facts_by_accession'][accession],
            ownership_primary_body=base64.b64encode(source['primary_bodies'][accession]).decode())
        inputs['source_proofs'][accession] = policy.build_local_source_proof(evidence,
            original=inputs['originals'][accession], raw_submission=originals[accession])
    graph = dict(policy='complete_submission_v1', corpus=corpus,
        supported_source_proofs=inputs['source_proofs'], supported_originals=inputs['originals'],
        ownership_facts=source['facts_by_accession'], history_corpus=source['histories'],
        archive_corpus=source['archives'], ownership_lineages={})
    rows = []
    for accession in (other[0]['accession'], current_evidence['accession']):
        evidence = corpus[accession]
        graph['ownership_lineages'][accession] = lineage.ownership_lineage(evidence,
            source['facts_by_accession'], corpus, source['histories'], source['archives'])
        rows.append(dict(adsh=accession, form_type=evidence['form'],
            file_date=evidence['filing_date'], file_url=evidence['source_url'],
            requires_prior=False, filing_evidence_ref=accession,
            filing_evidence_status='complete', text_status='available',
            issuer_binding=dict(status='verified', execution_status='verified')))
    collections = dict(filings=[], activist_13d=rows, passive_13g=[], pqc_filings=[])
    graph['coverage'] = dict(material_policy=policy.POLICY, required_rows=2, failed_rows=1,
        complete=False, deadline_exhausted=False, discovery_complete=True)
    graph['supported_dispositions'] = build_supported_dispositions(graph, collections)
    assert graph['supported_dispositions'][other[0]['accession']]['status'] == 'deferred'
    assert graph['supported_dispositions'][current_evidence['accession']]['status'] == 'admitted'
    for row in rows:
        row['filing_material_disposition'] = graph['supported_dispositions'][row['adsh']]['status']
    graph['material_scope'] = build_material_scope(graph, collections)
    for key in ('scoped_complete', 'scoped_failed_rows', 'quarantined_rows'):
        graph['coverage'][key] = graph['material_scope'][key]
    edgar = dict(**collections, filing_evidence=graph)
    before = deepcopy(edgar)
    summary = validate_filing_material_policy(dict(edgar=edgar), CONFIG)
    projected = signal_edgar(edgar, summary)
    assert edgar == before
    assert [row['adsh'] for row in projected['activist_13d']] == [current_evidence['accession']]
    assert other[0]['accession'] in projected['filing_evidence']['corpus']
    bundle = ownership_bundle(projected['filing_evidence'], current_evidence['accession'])
    request = prepare_request('activist_stake', current_evidence,
        issuer_binding=binding(current_evidence, corpus[base[0]['accession']]),
        ownership_bundles={current_evidence['accession']: bundle})
    assert request.context.source_provenance['unit_count'] == 2


@pytest.mark.parametrize('damage', ['nested_cusip', 'split_incorporation'])
def test_review_ownership_mixed_structure_cannot_omit_obligations(damage):
    record = filing(31, '2026-09-30', cusips=('44812J104', '123456789'))
    body = record[2]
    if damage == 'nested_cusip':
        body = body.replace(b'<issuerCusipNumber>44812J104</issuerCusipNumber>',
            b'<issuerCusipNumber>44812J104<clarification>discarded</clarification></issuerCusipNumber>')
        body = body.replace(b'<securitiesClassTitle>',
            b'<dateOfEvent>09/30/2026</dateOfEvent><securitiesClassTitle>')
        body = body.replace(b'</reportingPersonInfo>',
            b'<aggregateAmountOwned>0</aggregateAmountOwned><percentOfClass>10</percentOfClass>'
            b'<isAggregateExcludeShares>N</isAggregateExcludeShares></reportingPersonInfo>')
    else:
        body = body.replace(b'No external agreement.',
            b'The agreement in accession 0001193125-26-000099 is incorporated '
            b'<clarification>by reference.</clarification>')
    evidence, raw, original, proof = _policy_source(record, body)
    assert proof['residual_failures'], proof
    assert evidence['structural_status'] == 'insufficient'


def _policy_source(record, body):
    import hashlib
    from test_filing_evidence import ACCESSION, OBSERVED, role, submission
    from test_filing_ownership_lineage import FILER, SUBJECT
    from tradingagents.strategies.data_sources.filing_evidence import (
        frame_submission, select_primary, build_evidence_from_buffer,
    )
    accession = record[0]['accession']
    raw = submission([('SCHEDULE 13D', 'primary.xml', body.decode())], form='SCHEDULE 13D',
        roles=role('FILED-BY', FILER, 'Source Filer') + role('SUBJECT-COMPANY', SUBJECT, 'Subject'))
    raw = raw.replace(ACCESSION.encode(), accession.encode()).replace(b'<TYPE>',
        b'<ACCEPTANCE-DATETIME>20260930120000\r<TYPE>', 1)
    frame = frame_submission(raw, expected_accession=accession, expected_form='SCHEDULE 13D',
        expected_date='2026-09-30', observed_at=OBSERVED, max_submission_bytes=len(raw))
    evidence = build_evidence_from_buffer(select_primary(frame, raw), raw,
        supported_document_policy=policy.POLICY)
    evidence['source_url'] = f'https://www.sec.gov/Archives/edgar/data/2/{accession.replace("-", "")}/{accession}.txt'
    original = dict(identity={key: evidence[key] for key in ('accession', 'form', 'filing_date', 'source_url')},
        observed_at=OBSERVED, size=len(raw), sha256=hashlib.sha256(raw).hexdigest(), complete=True)
    proof = policy.build_local_source_proof(evidence, original=original, raw_submission=raw)
    return evidence, raw, original, proof


@pytest.mark.parametrize('phrase', [
    '<b>incorporated</b> by reference', 'incorporated&nbsp;by reference',
    'incorporated <span>by reference</span>',
    'incorporated by this reference',
    'incorporated into this Schedule 13D by reference',
])
def test_review_html_scalar_incorporation_cannot_be_omitted(phrase):
    target = '0001193125-26-000099'
    record = filing(41, '2026-09-30',
        text=f'The agreement in accession {target} is {phrase}.')
    current, raw, inputs = ownership_sources(record)
    facts = inputs['ownership']['facts_by_accession'][current['accession']]
    assert facts['incorporations'], facts
    assert facts['incorporations'][0]['target_accessions'] == [target]
    assert inputs['ownership']['receipt']['status'] == 'acquisition_required'
    receipt = policy.evaluate_supported_disposition(current, **inputs)
    assert receipt['status'] == 'failed', receipt
    assert policy.validate_supported_disposition(receipt, current,
        raw_submissions=raw, **inputs) == receipt


@pytest.mark.parametrize('body,missing_acquisition', [
    ('<p>Agreement terms:</p><img src="https://www.sec.gov/Archives/edgar/data/2065397/terms.png">', True),
    ('<p>Agreement terms:</p><img src="missing.png">', True),
    ('<table><tr><th>Year</th><th>2025</th><th>2026</th></tr>'
     '<tr><th>Profit</th><td>20</td><td>0</td></tr></table>', False),
    ('<p>Company will <s>not</s> repay the debt.</p>', False),
    ('<p>Debt terms:</p><svg><path d="M 0 0 L 100 0 L 0 100 Z"/></svg>', False),
    ('<div style="background-image:url(missing.png)">Agreement terms.</div>', True),
    ('<p>Company will <span style="text-decoration-line:line-through">not</span> repay the debt.</p>', False),
    ('<p hidden>Debt was repaid.</p><p>Debt remains outstanding.</p>', False),
    ('<p style="display:none">Debt was repaid.</p><p>Debt remains outstanding.</p>', False),
])
def test_review_lossy_html_projection_cannot_certify_supported_evidence(body, missing_acquisition):
    from test_filing_evidence import submission
    from test_filing_supported_policy import source, run, audit
    case = source(submission([('8-K', 'main.htm', body)]))
    receipt = run(case)
    if missing_acquisition:
        assert receipt['status'] == 'failed', receipt
        assert receipt['residual_failures']
    else:
        assert receipt['status'] in {'deferred', 'failed'}, receipt
    assert audit(case, receipt) == receipt


@pytest.mark.parametrize('text,missing_acquisition', [
    ('Company will <s>not</s> repay the debt.', False),
    ('The agreement is shown here: <img src="missing.png">', True),
])
def test_review_embedded_ownership_html_cannot_hide_visual_evidence(text, missing_acquisition):
    current, raw, inputs = ownership_sources(filing(61, '2026-09-30', text=text))
    receipt = policy.evaluate_supported_disposition(current, **inputs)
    if missing_acquisition:
        assert receipt['status'] == 'failed', receipt
    else:
        assert receipt['status'] in {'deferred', 'failed'}, receipt
    assert policy.validate_supported_disposition(receipt, current,
        raw_submissions=raw, **inputs) == receipt


def test_review_unproven_cusip_wrapper_remains_failed():
    record = filing(71, '2026-09-30')
    body = record[2].replace(b'issuerCusips', b'up20CUSIPs')
    evidence, raw, original, proof = _policy_source(record, body)
    assert evidence['structural_status'] == 'insufficient'
    assert proof['residual_failures']
    inputs = dict(source_proofs={evidence['accession']: proof},
        originals={evidence['accession']: original},
        occurrences=[{'collection': 'activist_13d', 'index': 0}])
    receipt = policy.evaluate_supported_disposition(evidence, **inputs)
    assert receipt['status'] == 'failed'
    assert policy.validate_supported_disposition(receipt, evidence,
        raw_submissions={evidence['accession']: raw}, **inputs) == receipt
