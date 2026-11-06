"""Final source attribution must bind material outcomes without analysis promotion."""
from copy import deepcopy
import hashlib
import json

import pytest

from test_filing_attribution_structural import supported_case
from test_filing_html_structural import v3_source
from test_filing_hydration import hydrate
from tradingagents.strategies.data_sources.filing_attribution_policy import (
    POLICY, validate_attribution, signal_edgar as attributed_edgar,
)
from tradingagents.strategies.data_sources.filing_assessment import _validate_evidence
from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
from tradingagents.strategies.orchestration import filing_material_validation as material


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),
        ensure_ascii=True,allow_nan=False).encode()).hexdigest()


@pytest.mark.parametrize('policy',['verified_supported_documents_v3','verified_supported_documents_v4'])
@pytest.mark.parametrize('outcome',['admitted','deferred','source_failure','missing_proof','body_budget','disposition_budget'])
def test_final_attribution_and_material_projection_bind_every_outcome(monkeypatch,policy,outcome):
    source,record,company_map,universe=supported_case(monkeypatch,'table',policy)
    accession=record['adsh']
    if outcome!='deferred':
        _,raw,_=v3_source('<p>Complete native narrative.</p>')
        source.raws[accession]=raw
    if outcome=='missing_proof':source.omit_proof=True
    if outcome=='source_failure':
        def failed(*args,**kwargs):
            raise SourceFetchError('retained independent source failure',reason_code='timeout')
        monkeypatch.setattr(source,'get_complete_submission',failed)
    collections={'filings':[record,dict(record,query_ref='duplicate')],
        'pqc_filings':[dict(record,matched_keyword='quantum')]}
    options=dict(company_map=company_map,equity_universe=universe,
        attribution_policy=POLICY,material_policy=policy)
    if outcome=='body_budget':options['max_evidence_bytes']=1
    if outcome=='disposition_budget':
        complete=hydrate(source,collections,**options)
        receipt=complete['supported_dispositions'][accession]
        receipt_size=len(json.dumps({'supported_dispositions':{accession:receipt}},
            sort_keys=True,separators=(',',':'),ensure_ascii=True).encode())
        options['max_evidence_bytes']=complete['coverage']['accepted_evidence_bytes']-receipt_size
    graph=hydrate(source,collections,**options)
    edgar={**graph['collections'],'company_tickers':company_map,'filing_evidence':graph}
    summary=validate_attribution(edgar,universe)
    assert summary['total_rows']==3
    for entry in graph['attribution_scope']['rows']:
        row=graph['collections'][entry['collection']][entry['index']]
        assert entry['row_sha256']==digest(row)
    assert graph['coverage']['analysis_adequacy']=='not_assessed'
    projected=attributed_edgar(edgar,summary)
    material_summary=material._summary(graph['material_scope'])
    if outcome in ('admitted','deferred'):
        final=material.signal_edgar(edgar,material_summary,projected_edgar=projected)
        expected=3 if outcome=='admitted' else 0
        assert sum(len(final.get(key,[])) for key in ('filings','pqc_filings'))==expected
        assert bool(final['filing_evidence']['corpus']) is (outcome=='admitted')
        if outcome=='deferred':
            assert graph['supported_dispositions'][accession]['status']=='deferred'
            assert graph['supported_dispositions'][accession]['order_authority'] is False
            with pytest.raises(ValueError):_validate_evidence(graph['corpus'][accession])
        else:
            assert graph['supported_dispositions'][accession]['status']=='admitted'
            _validate_evidence(graph['corpus'][accession])
    else:
        assert graph['coverage']['complete'] is False
        assert graph['material_scope']['scoped_complete'] is False
        with pytest.raises(ValueError,match='invalid_filing_material_policy'):
            material.signal_edgar(edgar,material_summary,projected_edgar=projected)
        if outcome=='disposition_budget':
            assert graph['supported_disposition_failures'][accession]['code']=='evidence_byte_limit'
        if outcome in ('body_budget','disposition_budget'):
            assert graph['collections']['filings'][0]['filing_failure']['code']=='evidence_byte_limit'
    changed=deepcopy(edgar)
    changed['filings'][0]['filing_material_disposition']='forged_final_disposition'
    with pytest.raises(ValueError,match='invalid_filing_attribution'):
        validate_attribution(changed,universe)
