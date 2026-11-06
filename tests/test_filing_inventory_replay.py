"""Required acquisition attestations survive projection and reject stripping."""
from copy import deepcopy
import time

import pytest

from test_filing_comparison_wiring import Registry
from test_filing_hydration import Source
from test_filing_inventory_acquisition import setup_transport
from test_filing_inventory_reconciliation import ACCESSION, IDENTITY, mismatched
from tradingagents.strategies.learning.event_monitor import EventMonitor
from tradingagents.strategies.data_sources import filing_acquisition
from tradingagents.strategies.data_sources.request_policy import provider_budget
from tradingagents.strategies.orchestration.filing_acquisition_validation import POLICY, validate_filing_acquisition_policy
from tradingagents.strategies.orchestration.source_inputs import SourceInputStore

CONFIG={'filing_evidence_policy':'complete_submission_v1','filing_parser_policy':'two_processes_v1','filing_acquisition_policy':POLICY}


def graph(tmp_path,monkeypatch,*,max_evidence_bytes=512*1024**2):
    setup_transport(monkeypatch,mismatched())
    row={'adsh':ACCESSION,'form_type':'8-K','file_date':'2026-09-30','ciks':['2065397'],'file_url':IDENTITY['source_url']}
    source=Source([row]);source.is_available=lambda:True
    source.get_complete_submission=lambda url,**kwargs:filing_acquisition.acquire_complete_submission('offline',url,**kwargs)
    monitor=EventMonitor(Registry(source),filing_policy='complete_submission_v1',parser_policy='two_processes_v1',acquisition_policy=POLICY,spool_root=tmp_path)
    if max_evidence_bytes!=512*1024**2:
        from tradingagents.strategies.data_sources import filing_hydration
        original=filing_hydration.hydrate_filings
        monkeypatch.setattr(filing_hydration,'hydrate_filings',lambda *a,**k:original(*a,**k,max_evidence_bytes=max_evidence_bytes))
    with provider_budget('edgar',time.monotonic()+15,limits=()):
        return monitor.hydrate_collections({'filings':[row]},max_workers=1)


def test_required_manifest_and_full_proofs_roundtrip_and_revalidate(tmp_path,monkeypatch):
    result=graph(tmp_path,monkeypatch)
    manifest=result['acquisition_scope'].get('inventory_reconciliations')
    assert isinstance(manifest,dict) and list(manifest)==[ACCESSION]
    assert manifest[ACCESSION]['proof']==result['corpus'][ACCESSION]['inventory_reconciliation']
    data={'edgar':{'filing_evidence':result}}
    restored=SourceInputStore.decode(SourceInputStore.encode(data,max_bytes=512*1024**2,max_nodes=8_000_000),max_bytes=512*1024**2,max_nodes=8_000_000)
    assert restored==data
    assert validate_filing_acquisition_policy(restored,CONFIG)['reconciled_originals']==1


@pytest.mark.parametrize('damage',['proof','manifest','map_entry','digest','sha','framing','prior_only_proof','body'])
def test_source_replay_rejects_stripped_or_tampered_proof_even_for_prior_only(tmp_path,monkeypatch,damage):
    result=graph(tmp_path,monkeypatch);manifest=result['acquisition_scope'].get('inventory_reconciliations')
    assert manifest is not None,'actual acquisition must record mandatory reconciliation manifest'
    if damage in ('proof','prior_only_proof'):
        result['corpus'][ACCESSION].pop('inventory_reconciliation')
        if damage=='prior_only_proof':result['collections']['filings']=[]
    elif damage=='manifest':result['acquisition_scope'].pop('inventory_reconciliations')
    elif damage=='map_entry':manifest.clear()
    elif damage=='digest':manifest[ACCESSION]['proof_sha256']='0'*64
    elif damage=='sha':manifest[ACCESSION]['submission_sha256']='0'*64
    elif damage=='framing':manifest[ACCESSION]['framing']['document_inventory'][0]['body_end']=1
    else:manifest[ACCESSION]['proof']['metadata']['directory']['body']='{}'
    with pytest.raises(ValueError,match='invalid_filing_acquisition_policy'):
        validate_filing_acquisition_policy({'edgar':{'filing_evidence':result}},CONFIG)


def test_reconciled_result_rejected_by_aggregate_cap_keeps_full_failure_attestation(tmp_path,monkeypatch):
    result=graph(tmp_path,monkeypatch,max_evidence_bytes=1)
    assert result['coverage']['complete'] is False and not result['corpus']
    assert ACCESSION in result['acquisition_scope'].get('inventory_reconciliations',{}),'rejected result must not prune actual acquisition proof'
    with pytest.raises(ValueError):validate_filing_acquisition_policy({'edgar':{'filing_evidence':result}},CONFIG)
    data={'edgar':{'filing_evidence':result,'error':'full filing evidence incomplete'}}
    assert validate_filing_acquisition_policy(data,CONFIG)['reconciled_originals']==1
