"""Source attribution must preserve supported visual deferrals without analysis authority."""
from copy import deepcopy
from datetime import datetime, timezone

import pytest

from test_filing_html_structural import v3_source, resource
from test_filing_structural_policy_v2 import case as pdf_source
from test_filing_ownership_hydration import NativeSource
from test_filing_hydration import hydrate
from tradingagents.strategies.data_sources.filing_attribution_policy import (
    POLICY, attribution_binding, validate_attribution,
)
from tradingagents.strategies.data_sources.filing_assessment import _validate_evidence
from tradingagents.strategies.data_sources.equity_universe import EquityUniverse, normalize_assets


def supported_case(monkeypatch, kind, policy):
    if kind == 'pdf':
        evidence, raw, _ = pdf_source(selected_policy=policy)
    else:
        body, resources = ('<table><tr><td>Retained material</td></tr></table>', ()) if kind == 'table' else ('<img src="scan.jpg">', (resource(),))
        evidence, raw, _ = v3_source(body, resources)
    record = {'adsh':evidence['accession'], 'form_type':evidence['form'],
              'file_date':evidence['filing_date'], 'ciks':['2065397'],
              'file_url':evidence['source_url']}
    monkeypatch.setattr('test_filing_ownership_hydration.POLICY', policy)
    source = NativeSource(extra=((record, raw),))
    company_map = {'0':{'cik_str':2065397, 'ticker':'TEST', 'title':'Source Issuer'}}
    snapshot = normalize_assets([{'class':'us_equity','symbol':'TEST','exchange':'NASDAQ',
        'status':'active','tradable':True}], observed_at=datetime(2026,10,10,tzinfo=timezone.utc),
        response_sha256='a'*64)
    return source, record, company_map, EquityUniverse(snapshot, company_map)


@pytest.mark.parametrize('policy',['verified_supported_documents_v3','verified_supported_documents_v4'])
@pytest.mark.parametrize('kind',['table','image','pdf'])
def test_actual_hydration_attribution_preserves_complete_deferred_population(monkeypatch,policy,kind):
    source, record, company_map, universe = supported_case(monkeypatch,kind,policy)
    graph = hydrate(source, {'filings':[record,dict(record,query_ref='duplicate')],
        'pqc_filings':[dict(record,matched_keyword='quantum')]}, company_map=company_map,
        equity_universe=universe, attribution_policy=POLICY, material_policy=policy)
    acc=record['adsh']
    assert len(source.body_calls)==1 and graph['coverage']['acquired_accessions']==1
    assert graph['coverage']['required_rows']==3 and graph['coverage']['complete'] is False
    assert graph['supported_dispositions'][acc]['status']=='deferred'
    assert graph['supported_dispositions'][acc]['order_authority'] is False
    assert len(graph['supported_dispositions'][acc]['occurrences'])==3
    evidence=graph['corpus'][acc]
    assert all(r['filing_material_disposition']=='deferred' for rows in graph['collections'].values() for r in rows)
    original=deepcopy(evidence)
    with pytest.raises(ValueError):_validate_evidence(evidence)
    assert attribution_binding(evidence,universe,EquityUniverse.company_symbols(company_map))['execution_status']=='verified'
    assert evidence==original
    edgar={**graph['collections'],'company_tickers':company_map,'filing_evidence':graph}
    summary=validate_attribution(edgar,universe)
    assert summary['total_rows']==summary['verified_target_rows']==3


@pytest.mark.parametrize('kind',['table','image','pdf'])
@pytest.mark.parametrize('attack',['role','body_hash','missing_inventory','unknown_policy'])
def test_source_attribution_still_rejects_corrupt_structural_evidence(monkeypatch,kind,attack):
    source,record,company_map,universe=supported_case(monkeypatch,kind,'verified_supported_documents_v4')
    evidence=source.get_complete_submission(record['file_url'],accession=record['adsh'],
        form_type=record['form_type'],filing_date=record['file_date'])
    evidence.pop('supported_source_proof')
    if attack=='role':evidence['roles'][0]['cik']='invalid'
    elif attack=='body_hash':evidence['units'][-1]['body_sha256']='0'*64
    elif attack=='missing_inventory':evidence['document_inventory'].pop()
    else:evidence['supported_document_policy']='unknown'
    with pytest.raises(ValueError,match='invalid_filing_attribution'):
        attribution_binding(evidence,universe,EquityUniverse.company_symbols(company_map))
