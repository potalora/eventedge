"""Only a closed numeric mismatch acquires bounded independent metadata."""
import time

import pytest

from test_filing_evidence import ACCESSION, submission
from test_filing_inventory_reconciliation import IDENTITY, metadata_responses, mismatched
from tradingagents.strategies.data_sources import filing_acquisition as acquisition
from tradingagents.strategies.data_sources import filing_parser_dispatch as ipc
from tradingagents.strategies.data_sources import filing_spool as spool
from tradingagents.strategies.data_sources import request_policy
from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError


class Response:
    status_code=200
    headers={}
    def __init__(self,url,raw):self.url=url;self.raw=raw;self.closed=False
    def iter_content(self,chunk_size):yield self.raw
    def close(self):self.closed=True


def setup_transport(monkeypatch,raw,change=None):
    bodies={IDENTITY['source_url']:raw}
    for row in metadata_responses(raw).values():bodies[row['source_url']]=row['body'].encode()
    calls=[];responses=[]
    def request(url,**kwargs):
        calls.append((url,kwargs));response=Response(url,bodies[url]);responses.append(response)
        if change is not None:change(response)
        return response
    monkeypatch.setattr(request_policy.requests,'get',request)
    return calls,responses


def acquire():
    return acquisition.acquire_complete_submission('offline',IDENTITY['source_url'],accession=ACCESSION,
        form_type='8-K',filing_date='2026-09-30')


def test_real_original_dispatch_closed_then_three_counted_metadata_requests_then_same_record(tmp_path,monkeypatch):
    raw=mismatched();calls,responses=setup_transport(monkeypatch,raw);seen=[]
    original_dispatch=ipc.dispatch_evidence
    def dispatch(record,**kwargs):
        seen.append(record)
        if len(seen)==2:
            assert len(calls)==4 and record.owner.stats()['active_child_copies']==0
            assert record.path.read_bytes()==raw
        return original_dispatch(record,**kwargs)
    monkeypatch.setattr(ipc,'dispatch_evidence',dispatch)
    deadline=time.monotonic()+15
    with request_policy.provider_budget('edgar',deadline,limits=()) as diagnostics:
        with spool.submission_spool_scope(tmp_path,original_deadline=deadline) as owner:
            with ipc.parser_scope() as children:
                pids=[child.pid for child in children.children];result=acquire()
                assert [child.pid for child in children.children]==pids and len(pids)==2
            assert owner.stats()['completed_objects']==1 and owner.stats()['total_bytes']==0
    assert result['structural_status']=='complete' and 'inventory_reconciliation' in result
    assert len(calls)==len(diagnostics)==4 and len(seen)==2 and seen[0] is seen[1]
    assert all(r.closed for r in responses)
    assert all(c[1]['stream'] and c[1]['allow_redirects'] is False for c in calls)


@pytest.mark.parametrize('raw',[submission(),b'invalid',mismatched().replace(b'<PUBLIC-DOCUMENT-COUNT>2',b'<PUBLIC-DOCUMENT-COUNT>no')])
def test_other_outcomes_do_not_request_metadata(tmp_path,monkeypatch,raw):
    calls,responses=setup_transport(monkeypatch,raw);deadline=time.monotonic()+15
    with request_policy.provider_budget('edgar',deadline,limits=()):
        with spool.submission_spool_scope(tmp_path,original_deadline=deadline):
            with ipc.parser_scope():
                if raw==submission():assert acquire()['structural_status']=='complete'
                else:
                    with pytest.raises(SourceFetchError):acquire()
    assert len(calls)==1 and all(r.closed for r in responses)


@pytest.mark.parametrize('kind',['redirect','status','non_utf8','oversized'])
def test_metadata_rejection_discards_owned_original_without_refetch(tmp_path,monkeypatch,kind):
    def change(response):
        if response.url==IDENTITY['source_url']:return
        if kind=='redirect':response.url+='?unexpected'
        elif kind=='status':response.status_code=302
        elif kind=='non_utf8':response.raw=b'\xff'
        else:response.raw=b'X'*(1024**2+1)
    calls,responses=setup_transport(monkeypatch,mismatched(),change);deadline=time.monotonic()+15
    with request_policy.provider_budget('edgar',deadline,limits=()):
        with spool.submission_spool_scope(tmp_path,original_deadline=deadline) as owner:
            with ipc.parser_scope():
                with pytest.raises(SourceFetchError):acquire()
            assert owner.stats()['total_bytes']==0 and owner.stats()['active_child_copies']==0
    assert len([url for url,_ in calls if url==IDENTITY['source_url']])==1
    assert all(r.closed for r in responses)


def test_budget_expiry_after_mismatch_starts_no_metadata_request(tmp_path,monkeypatch):
    calls,_=setup_transport(monkeypatch,mismatched());clock=[time.monotonic()];deadline=clock[0]+15
    original=ipc.dispatch_evidence
    def expire(record,**kwargs):
        try:return original(record,**kwargs)
        except ipc.SubmissionDocumentCountMismatch:clock[0]=deadline;raise
    monkeypatch.setattr(ipc,'dispatch_evidence',expire)
    with request_policy.provider_budget('edgar',deadline,limits=(),clock=lambda:clock[0]):
        with spool.submission_spool_scope(tmp_path,original_deadline=deadline):
            with ipc.parser_scope():
                with pytest.raises(SourceFetchError) as caught:acquire()
    assert caught.value.reason_code=='timeout' and len(calls)==1
