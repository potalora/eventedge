"""Prospective projection preserves the denominator and all independent failures."""
from copy import deepcopy

import pytest
from test_filing_supported_policy import source, pdf_source
from tradingagents.strategies.data_sources import filing_supported_policy as policy
from tradingagents.strategies.orchestration.filing_supported_scope import build_supported_dispositions
from tradingagents.strategies.orchestration.filing_material_validation import (
    build_material_scope, validate_filing_material_policy, signal_edgar,
)

CONFIG = {'filing_material_policy': policy.POLICY, 'filing_evidence_policy':'complete_submission_v1',
    'filing_acquisition_policy':'bounded_original_submission_v1', 'filing_parser_policy':'two_processes_v1'}


def graph_case(*, visual=False, mixed=False, duplicate=False, row_failure=None):
    # Mixed font sizes retain a real v2 capability gap; plain native text plus
    # an inventoried image no longer has the historical extraction defect.
    evidence, raw, inputs = pdf_source(image=True, text=b'' if not mixed else b'BT /F1 12 Tf 72 700 Td (Text.) Tj /F1 10 Tf 0 -30 Td (Small.) Tj ET') if visual else source()
    acc = evidence['accession']
    evidence['supported_document_policy'] = policy.POLICY
    evidence.setdefault('pdf_evidence', {})
    proof = policy.build_local_source_proof(evidence,original=inputs['originals'][acc],raw_submission=raw)
    record = {'adsh':acc,'form_type':evidence['form'],'file_date':evidence['filing_date'],
        'file_url':evidence['source_url'],'requires_prior':False,'filing_evidence_ref':acc,
        'filing_evidence_status':'complete','text_status':'available','issuer_binding':{'status':'verified'}}
    if row_failure:
        record['filing_failure'] = row_failure
    collections = {'filings':[record], 'activist_13d':[],'passive_13g':[],
        'pqc_filings':[deepcopy(record)] if duplicate else []}
    graph = {'policy':'complete_submission_v1','corpus':{acc:evidence},
        'supported_source_proofs':{acc:proof},'supported_originals':inputs['originals'],
        'coverage':{'material_policy':policy.POLICY,'required_rows':1+int(duplicate),
            'complete':not(visual or row_failure),'failed_rows':(1+int(duplicate)) if visual or row_failure else 0,
            'deadline_exhausted':False,'discovery_complete':True},
        'history_corpus':{},'archive_corpus':{}}
    graph['supported_dispositions'] = build_supported_dispositions(graph,collections)
    for rows in collections.values():
        for row in rows:
            row['filing_material_disposition'] = graph['supported_dispositions'][acc]['status']
    graph['material_scope'] = build_material_scope(graph,collections)
    for key in ('scoped_complete','scoped_failed_rows','quarantined_rows'):
        graph['coverage'][key] = graph['material_scope'][key]
    edgar = {**collections,'filing_evidence':graph}
    if not graph['material_scope']['scoped_complete']:
        edgar['error'] = 'full filing evidence incomplete'
    return edgar


def test_prospective_admitted_scope_has_no_model_acceptance_claim():
    edgar = graph_case()
    scope = validate_filing_material_policy({'edgar':edgar},CONFIG)
    assert scope['scoped_complete'] is True
    assert scope['admitted_rows'] == 1 and scope['deferred_rows'] == 0
    assert scope['prospective_scope_broadened'] is True
    assert scope['original_full_analysis_coverage'] is False


def test_visual_deferred_keeps_strict_denominator_and_removes_every_occurrence():
    edgar = graph_case(visual=True,duplicate=True)
    original = deepcopy(edgar)
    scope = validate_filing_material_policy({'edgar':edgar},CONFIG)
    assert scope['required_rows'] == scope['strict_failed_rows'] == scope['deferred_rows'] == 2
    assert scope['strict_complete'] is False and scope['scoped_complete'] is True
    assert scope['scoped_failed_rows'] == 0
    assert scope['supported_dispositions'][0]['occurrences'] == [
        {'collection':'filings','index':0},{'collection':'pqc_filings','index':0}]
    projected = signal_edgar(edgar,scope)
    assert projected['filings'] == projected['pqc_filings'] == []
    assert projected['filing_evidence']['corpus'] == {}
    assert edgar == original


def test_visual_with_independent_failure_is_never_subtracted():
    edgar = graph_case(visual=True,row_failure={'code':'source_failure','reason_code':'timeout'})
    scope = validate_filing_material_policy({'edgar':edgar},CONFIG)
    assert scope['scoped_complete'] is False and scope['scoped_failed_rows'] == 1
    assert scope['disposition_failed_rows'] == 1
    assert scope['supported_dispositions'][0]['reasons']


def test_pdf_capability_failure_is_not_deferred():
    edgar = graph_case(visual=True,mixed=True)
    scope = validate_filing_material_policy({'edgar':edgar},CONFIG)
    assert scope['deferred_rows'] == 0 and scope['disposition_failed_rows'] == 1
    receipt = next(iter(edgar['filing_evidence']['supported_dispositions'].values()))
    assert receipt['residual_failures'][0]['failure']['code'] == 'pdf_capability_limit'
    assert scope['scoped_complete'] is False


@pytest.mark.parametrize('damage',['omit_occurrence','change_receipt','missing_proof','duplicate_occurrence','source_url'])
def test_recomputation_rejects_disposition_and_denominator_tampering(damage):
    edgar = graph_case(visual=True,duplicate=True)
    graph = edgar['filing_evidence']
    acc = edgar['filings'][0]['adsh']
    if damage == 'omit_occurrence':edgar['pqc_filings'].clear()
    elif damage == 'change_receipt':graph['supported_dispositions'][acc]['status']='admitted'
    elif damage == 'missing_proof':graph['supported_source_proofs'].clear()
    elif damage == 'duplicate_occurrence':edgar['pqc_filings'].append(deepcopy(edgar['pqc_filings'][0]))
    else:edgar['filings'][0]['file_url']='https://example.com/not-sec.txt'
    with pytest.raises(ValueError,match='invalid_filing_material_policy'):
        validate_filing_material_policy({'edgar':edgar},CONFIG)


def test_global_deadline_remains_failed_with_visual_deferral():
    edgar = graph_case(visual=True)
    graph = edgar['filing_evidence']
    graph['coverage']['deadline_exhausted'] = True
    scope = build_material_scope(graph,{k:edgar[k] for k in ('filings','activist_13d','passive_13g','pqc_filings')})
    assert scope['deferred_rows'] == 1 and scope['scoped_complete'] is False


def test_receipt_budget_callback_omits_before_retaining_and_cannot_pass_scope():
    edgar = graph_case()
    graph = edgar['filing_evidence']
    collections = {k:edgar[k] for k in ('filings','activist_13d','passive_13g','pqc_filings')}
    called=[]
    def refuse(acc,receipt):
        called.append(acc)
        return False
    assert build_supported_dispositions(graph,collections,on_receipt=refuse) == {}
    acc=called[0]
    graph['supported_disposition_failures']={acc:{'code':'evidence_byte_limit'}}
    graph['supported_dispositions']={}
    edgar['filings'][0].update(filing_failure={'code':'filing_evidence_failure','reason_code':'evidence_byte_limit'},filing_material_disposition='failed')
    graph['coverage'].update(complete=False,failed_rows=1)
    scope=build_material_scope(graph,collections)
    assert scope['scoped_complete'] is False and scope['scoped_failed_rows'] == 1


def test_failed_prospective_scope_cannot_be_projected_for_signals():
    edgar = graph_case(visual=True,mixed=True)
    scope = validate_filing_material_policy({'edgar':edgar},CONFIG)
    with pytest.raises(ValueError,match='invalid_filing_material_policy'):
        signal_edgar(edgar,scope)
