"""Display/audit snapshots never become legacy congressional trading input."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import importlib
import io
import json
from pathlib import Path
import zipfile

import pytest
import requests

from tradingagents.strategies.data_sources import request_policy
from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError

BASE = Path(__file__).resolve().parents[1] / 'data/forward-readiness/resume-free-congress-public-evidence'
REVISION = 'b10790b' + '0' * 33  # Synthetic revision API response, not a live revision claim.
NOW = datetime(2026, 10, 10, 18, tzinfo=timezone.utc)
NATIVE_SESSION_REQUEST = requests.sessions.Session.request


class Response:
    def __init__(self, body=b'', status=200, headers=None):
        self.body, self.status_code, self.headers = body, status, headers or {}
        self.closed = False

    def iter_content(self, chunk_size):
        for start in range(0, len(self.body), chunk_size):
            yield self.body[start:start + chunk_size]

    def close(self):
        self.closed = True


def module():
    # Feature absence should be a direct assertion, rather than collection error.
    import importlib.util
    name = 'tradingagents.strategies.data_sources.congress_disclosure_audit'
    assert importlib.util.find_spec(name) is not None, 'free display/audit adapter is missing'
    return importlib.import_module(name)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError('test attempted real transport')
    monkeypatch.setattr(requests.sessions.Session, 'request', blocked)
    monkeypatch.setattr(requests.sessions.Session, 'send', blocked)


@pytest.fixture
def retained():
    if not BASE.exists():
        pytest.skip('optional retained public research fixture not present')
    return {name: (BASE / name).read_bytes() for name in
            ('snapshot.json', 'filings-2026.parquet', 'trades-2026.parquet', 'house-2026FD.ZIP')}


def install_transport(monkeypatch, m, raw, *, changed=None):
    calls, responses = [], []
    metadata = json.dumps({'sha': REVISION, 'id': m.DATASET}).encode()
    def get(url, **kwargs):
        assert kwargs['stream'] is True and kwargs['allow_redirects'] is False
        assert set(kwargs['headers']) == {'User-Agent'}
        calls.append(url)
        if '/revision/main' in url:
            body = metadata
        elif url.endswith('/snapshot.json'):
            body = raw['snapshot.json']
        elif url.endswith('2026FD.ZIP'):
            body = raw['house-2026FD.ZIP']
        elif 'political_filings/' in url:
            body = raw['filings-2026.parquet']
        elif 'political_trades/' in url:
            body = raw['trades-2026.parquet']
        else:
            raise AssertionError('unexpected request')
        result = changed(url, body) if changed else Response(body)
        responses.append(result)
        return result
    monkeypatch.setattr(requests, 'get', get)
    monkeypatch.setattr(m, '_utcnow', lambda: NOW)
    return calls, responses


def fetch(m, **kwargs):
    with request_policy.provider_budget('congress', 1000, clock=lambda: 1, limits=()):
        return m.fetch_audit_snapshot(date_filed_after='2026-09-09',
            date_filed_before='2026-10-09', absolute_deadline=1000, **kwargs)


def test_retained_house_discovery_is_exact_and_every_raw_row_survives(monkeypatch, retained):
    m = module()
    calls, responses = install_transport(monkeypatch, m, retained)
    payload = fetch(m)
    snapshot = payload['audit_snapshot']
    assert payload['coverage']['status'] == 'audit_snapshot_complete'
    assert payload['coverage']['stock_disclosure_complete'] is False
    assert snapshot['policy'] == 'display_audit_only_v1'
    assert snapshot['signals_enabled'] is snapshot['model_context_enabled'] is False
    assert len(snapshot['tables']['political_filings']) == 560
    assert len(snapshot['tables']['political_trades']) == 11489
    assert snapshot['scope_evidence']['house_reconciliation']['matched_count'] == 37
    assert snapshot['scope_evidence']['house_reconciliation']['missing_ids'] == []
    assert snapshot['scope_evidence']['house_reconciliation']['additional_ids'] == []
    assert snapshot['scope_evidence']['window_filing_counts'] == {'house': 37, 'senate': 11}
    assert snapshot['scope_evidence']['senate_coverage'] == 'publisher_only_unverified'
    assert snapshot['acquired_at'] == NOW.isoformat()
    assert 'trades' not in payload and 'recent_trades' not in payload
    assert len(calls) == 5 and all(r.closed for r in responses)
    m.validate_audit_snapshot(payload, date_filed_after='2026-09-09', date_filed_before='2026-10-09', now=NOW)
    summary = m.audit_summary(payload)
    assert summary['house_matched_filings'] == 37
    assert summary['signals_enabled'] is False


@pytest.mark.parametrize('fault', ['body', 'hash', 'revision', 'house_missing'])
def test_tampering_and_house_gaps_never_claim_complete(monkeypatch, fault):
    retained = synthetic_raw()
    m = module()
    if fault == 'body':
        retained['trades-2026.parquet'] += b'altered'
    elif fault == 'hash':
        manifest = json.loads(retained['snapshot.json'])
        manifest['tables']['political_trades']['manifests'][-1]['files'][0]['sha256'] = '0' * 64
        retained['snapshot.json'] = json.dumps(manifest).encode()
    elif fault == 'house_missing':
        source = zipfile.ZipFile(io.BytesIO(retained['house-2026FD.ZIP']))
        xml = source.read('2026FD.xml').replace(b'<DocID>12345</DocID>', b'<DocID>99999999</DocID>')
        out = io.BytesIO()
        with zipfile.ZipFile(out, 'w') as target:
            target.writestr('2026FD.xml', xml)
            target.writestr('2026FD.txt', source.read('2026FD.txt'))
        retained['house-2026FD.ZIP'] = out.getvalue()
    changed = (lambda u, b: Response(json.dumps({'sha': 'main'}).encode()) if '/revision/main' in u else Response(b)) if fault == 'revision' else None
    _, responses = install_transport(monkeypatch, m, retained, changed=changed)
    with pytest.raises(SourceFetchError) as error:
        fetch(m)
    assert error.value.reason_code == 'invalid_response'
    assert all(r.closed for r in responses)
    if fault == 'house_missing':
        assert error.value.partial_data['coverage']['complete'] is False
        assert error.value.partial_data['audit_snapshot']['scope_evidence']['house_reconciliation']['missing_ids']


def test_timeout_has_one_physical_attempt_and_no_retry_sleep(monkeypatch):
    m = module()
    calls = []
    def get(*args, **kwargs):
        calls.append(1)
        raise requests.Timeout()
    monkeypatch.setattr(requests, 'get', get)
    with request_policy.provider_budget('congress', 1000, clock=lambda: 1,
            sleep=lambda _: pytest.fail('retried'), limits=(), max_attempts=3):
        with pytest.raises(SourceFetchError) as error:
            m.fetch_audit_snapshot(date_filed_after='2026-09-09', date_filed_before='2026-10-09', absolute_deadline=1000)
    assert error.value.reason_code == 'timeout'
    assert calls == [1]


def test_replay_validation_rejects_rehashed_rows_and_future_or_stale_acquisition(monkeypatch):
    retained = synthetic_raw()
    m = module()
    install_transport(monkeypatch, m, retained)
    payload = fetch(m)
    for field, value in [('docId', 'invented'), ('rawSha256', '0' * 64)]:
        bad = deepcopy(payload)
        bad['audit_snapshot']['tables']['political_filings'][0][field] = value
        bad['audit_snapshot']['content_sha256'] = m._snapshot_digest(bad['audit_snapshot'])
        with pytest.raises(ValueError):
            m.validate_audit_snapshot(bad, date_filed_after='2026-09-09', date_filed_before='2026-10-09', now=NOW)
    with pytest.raises(ValueError):
        m.validate_audit_snapshot(payload, date_filed_after='2026-09-09', date_filed_before='2026-10-09', now=datetime(2026,10,9,tzinfo=timezone.utc))
    with pytest.raises(ValueError):
        m.validate_audit_snapshot(payload, date_filed_after='2026-09-09', date_filed_before='2026-10-09', now=datetime(2026,10,13,tzinfo=timezone.utc))
    m.validate_audit_snapshot(payload, date_filed_after='2026-09-09', date_filed_before='2026-10-09', max_evidence_age_seconds=None)


def synthetic_raw():
    """Explicit synthetic full native schema; no public personal data required."""
    import pyarrow as pa
    import pyarrow.parquet as pq
    from datetime import date
    filing_names = ('chamber docId filerFirst filerLast filerSuffix stateDistrict filingDate availableAt availabilitySource sourceUrl rawArchiveKey rawSha256 parseMethod extractionStatus failureReason extractedRows extractionModel contractVersion ocrArchiveKey amendedReportDate reportDate processedAt filerKey memberId displayName identitySource').split()
    trade_names = ('chamber docId rowIndex sourceTransactionId filerFirst filerLast owner ownerCodeRaw action partialSale actionCodeRaw transactionDate notificationDate filingDate availableAt availabilitySource assetDescription printedTicker resolvedTicker resolutionStatus resolutionReason assetTypeCode assetTypeLabel amountBracket amountLow amountHigh capGainsOver200 comment filingStatus sourceUrl rawArchiveKey rawSha256 filerKey memberId displayName identitySource').split()
    common = dict(chamber='house', docId='12345', filingDate=date(2026,10,1),
        sourceUrl='https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2026/12345.pdf',
        rawArchiveKey='raw/house/12345.pdf',rawSha256='a'*64)
    filing = dict.fromkeys(filing_names)
    filing.update(common, extractionStatus='ok',extractedRows=1)
    trade = dict.fromkeys(trade_names)
    trade.update(common,rowIndex=0,sourceTransactionId='12345:0',printedTicker=None,
        resolvedTicker=None,resolutionStatus='unresolved',amountLow=0.0,amountHigh=None)
    types = {n:pa.string() for n in filing_names+trade_names}
    types.update({n:pa.date32() for n in ('filingDate','transactionDate','notificationDate','amendedReportDate','reportDate')})
    types.update({n:pa.timestamp('us') for n in ('availableAt','processedAt')})
    types.update({n:pa.int32() for n in ('extractedRows','rowIndex')})
    types.update({n:pa.bool_() for n in ('partialSale','capGainsOver200')})
    types.update({n:pa.float64() for n in ('amountLow','amountHigh')})
    raw, tables = {}, {}
    for table,names,row,name in [('political_filings',filing_names,filing,'filings-2026.parquet'),
                                 ('political_trades',trade_names,trade,'trades-2026.parquet')]:
        out=io.BytesIO();pq.write_table(pa.Table.from_pylist([row],schema=pa.schema([pa.field(n,types[n]) for n in names])),out)
        raw[name]=out.getvalue()
        tables[table]={'manifests':[{'year':2026,'files':[{'publicPath':f'data/{table}/2026-00000-of-00001.parquet',
            'size':len(raw[name]),'sha256':hashlib.sha256(raw[name]).hexdigest()}]}]}
    raw['snapshot.json']=json.dumps({'schemaVersion':2,'dataset':'austin-starks/congressional-stock-trades',
        'generatedAt':'2026-10-10T12:00:00Z','lastSourceCheckAt':'2026-10-10T12:00:00Z',
        'audit':{'passed':True},'tables':tables,'refreshPolicy':{'targetIntervalHours':20}}).encode()
    out=io.BytesIO()
    with zipfile.ZipFile(out,'w') as z:
        z.writestr('2026FD.xml','<FinancialDisclosure><Member><DocID>12345</DocID><FilingType>P</FilingType><FilingDate>10/1/2026</FilingDate></Member></FinancialDisclosure>')
        z.writestr('2026FD.txt','synthetic')
    raw['house-2026FD.ZIP']=out.getvalue()
    return raw


def test_synthetic_full_native_shape_retains_nulls_zero_and_artifact_bytes(monkeypatch):
    import base64
    m=module();raw=synthetic_raw();install_transport(monkeypatch,m,raw)
    payload=fetch(m);s=payload['audit_snapshot']
    row=s['tables']['political_trades'][0]
    assert row['printedTicker'] is None and row['resolvedTicker'] is None
    assert row['amountLow']==0.0 and row['amountHigh'] is None
    assert row['filingDate']=='2026-10-01'
    assert s['scope_evidence']['house_reconciliation']['matched_count']==1
    for a in s['raw_artifacts']:
        if a['kind']=='parquet':
            name='filings-2026.parquet' if 'political_filings/' in a['source_url'] else 'trades-2026.parquet'
            assert base64.b64decode(a['body_base64'])==raw[name]


@pytest.mark.parametrize('location', ['http://huggingface.co/bad','https://evil.test/file',
    'https://user:secret@huggingface.co/file','https://huggingface.co/datasets/austin-starks/congressional-stock-trades/resolve/main/snapshot.json'])
def test_redirect_host_credentials_and_mutable_revision_fail_closed(monkeypatch,location):
    m=module();calls=[];responses=[]
    def get(url,**kwargs):
        calls.append(url);r=Response(status=302,headers={'Location':location});responses.append(r);return r
    monkeypatch.setattr(requests,'get',get);monkeypatch.setattr(m,'_utcnow',lambda:NOW)
    with pytest.raises(SourceFetchError):fetch(m)
    assert len(calls)==1 and responses[0].closed


def test_pinned_manifest_redirect_is_manual_and_exact(monkeypatch):
    m=module();raw=synthetic_raw();calls,responses=install_transport(monkeypatch,m,raw)
    original=requests.get
    redirects=[]
    def get(url,**kwargs):
        if url.endswith('/snapshot.json') and '/resolve/' in url:
            r=Response(status=302,headers={'Location':f'https://huggingface.co/api/resolve-cache/datasets/{m.DATASET}/{REVISION}/snapshot.json'})
            redirects.append(r);return r
        return original(url,**kwargs)
    monkeypatch.setattr(requests,'get',get)
    assert fetch(m)['coverage']['complete'] is True
    assert len(redirects)==1 and redirects[0].closed
    assert all(r.closed for r in responses)


@pytest.mark.parametrize('fault',['duplicate_json','nonfinite_json','missing_part','dtype','expanded','utf16_entity','late_body','http503'])
def test_invalid_metadata_expansion_xml_and_late_body_never_publish(monkeypatch,fault):
    m=module();raw=synthetic_raw()
    if fault=='duplicate_json':raw['snapshot.json']=b'{"schemaVersion":2,"schemaVersion":2}'
    if fault=='nonfinite_json':raw['snapshot.json']=b'{"x":NaN}'
    if fault=='missing_part':
        manifest=json.loads(raw['snapshot.json']);manifest['tables']['political_filings']['manifests'][0]['files'][0]['publicPath']='data/political_filings/2026-00000-of-00002.parquet';raw['snapshot.json']=json.dumps(manifest).encode()
    if fault=='dtype':
        import pyarrow as pa
        import pyarrow.parquet as pq
        table=pq.read_table(io.BytesIO(raw['filings-2026.parquet']),use_threads=False)
        index=table.schema.get_field_index('filingDate');table=table.set_column(index,'filingDate',pa.array(['2026-10-01']))
        out=io.BytesIO();pq.write_table(table,out);raw['filings-2026.parquet']=out.getvalue()
        manifest=json.loads(raw['snapshot.json']);file=manifest['tables']['political_filings']['manifests'][0]['files'][0];file['size']=len(out.getvalue());file['sha256']=hashlib.sha256(out.getvalue()).hexdigest();raw['snapshot.json']=json.dumps(manifest).encode()
    if fault=='expanded':monkeypatch.setattr(m,'MAX_UNCOMPRESSED_BYTES',1)
    if fault=='utf16_entity':
        out=io.BytesIO()
        with zipfile.ZipFile(out,'w') as z:
            z.writestr('2026FD.xml','<?xml version="1.0" encoding="utf-16"?><!DOCTYPE a [<!ENTITY x "12345">]><FinancialDisclosure><Member><DocID>&x;</DocID><FilingType>P</FilingType><FilingDate>10/1/2026</FilingDate></Member></FinancialDisclosure>'.encode('utf-16'))
            z.writestr('2026FD.txt','synthetic')
        raw['house-2026FD.ZIP']=out.getvalue()
    clock=[1]
    class Late(Response):
        def iter_content(self,chunk_size):
            yield self.body;clock[0]=1001
    changed=(lambda u,b: Late(b)) if fault=='late_body' else ((lambda u,b:Response(b,status=503)) if fault=='http503' else None)
    calls,responses=install_transport(monkeypatch,m,raw,changed=changed)
    with request_policy.provider_budget('congress',1000,clock=lambda:clock[0],limits=()):
        with pytest.raises(SourceFetchError):m.fetch_audit_snapshot(date_filed_after='2026-09-09',date_filed_before='2026-10-09',absolute_deadline=1000)
    assert all(r.closed for r in responses)
    if fault=='http503':assert len(calls)==1


def test_late_parquet_decode_rejected_under_original_deadline(monkeypatch):
    m=module();install_transport(monkeypatch,m,synthetic_raw());clock=[1]
    original=m._rows
    def rows(*args,**kwargs):
        result=original(*args,**kwargs);clock[0]=1001;return result
    monkeypatch.setattr(m,'_rows',rows)
    with request_policy.provider_budget('congress',1000,clock=lambda:clock[0],limits=()):
        with pytest.raises(SourceFetchError) as error:
            m.fetch_audit_snapshot(date_filed_after='2026-09-09',date_filed_before='2026-10-09',absolute_deadline=1000)
    assert error.value.reason_code=='timeout'


def test_public_audit_policy_needs_no_fmp_and_preserves_legacy_availability(monkeypatch):
    from tradingagents.strategies.data_sources.congress_source import CongressSource
    m=module();monkeypatch.delenv('FMP_API_KEY',raising=False)
    source=CongressSource(disclosure_policy=m.POLICY)
    assert source.requires_api_key is False and source.is_available() is True
    install_transport(monkeypatch,m,synthetic_raw())
    with request_policy.provider_budget('congress',1000,clock=lambda:1,limits=()):
        assert source.get_audit_snapshot(date_filed_after='2026-09-09',date_filed_before='2026-10-09',absolute_deadline=1000)['audit_snapshot']['mode']=='display_audit_only'
    legacy=CongressSource()
    assert legacy.requires_api_key is True and legacy.is_available() is False
    assert CongressSource(fmp_api_key='synthetic-configured-key').is_available() is True
    with pytest.raises(ValueError):CongressSource(disclosure_policy='unknown')


def test_cross_year_window_downloads_both_years_and_keeps_out_of_window_rows(monkeypatch):
    import pyarrow as pa
    import pyarrow.parquet as pq
    from datetime import date
    m=module();raw=synthetic_raw();manifest=json.loads(raw['snapshot.json']);extra={}
    for table,name in [('political_filings','filings-2026.parquet'),('political_trades','trades-2026.parquet')]:
        original=pq.read_table(io.BytesIO(raw[name]),use_threads=False);row=original.to_pylist()[0]
        row.update(docId='54321',filingDate=date(2025,12,31),sourceUrl='https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2025/54321.pdf',rawArchiveKey='raw/house/54321.pdf')
        if table=='political_trades':row['sourceTransactionId']='54321:0'
        output=io.BytesIO();pq.write_table(pa.Table.from_pylist([row],schema=original.schema),output);body=output.getvalue()
        path=f'data/{table}/2025-00000-of-00001.parquet';extra[path]=body
        manifest['tables'][table]['manifests'].append({'year':2025,'files':[{'publicPath':path,'size':len(body),'sha256':hashlib.sha256(body).hexdigest()}]})
    raw['snapshot.json']=json.dumps(manifest).encode()
    out=io.BytesIO()
    with zipfile.ZipFile(out,'w') as z:
        z.writestr('2025FD.xml','<FinancialDisclosure><Member><DocID>54321</DocID><FilingType>P</FilingType><FilingDate>12/31/2025</FilingDate></Member></FinancialDisclosure>')
        z.writestr('2025FD.txt','synthetic')
    calls,responses=install_transport(monkeypatch,m,raw);original=requests.get
    def get(url,**kwargs):
        for suffix,body in extra.items():
            if url.endswith(suffix):r=Response(body);responses.append(r);calls.append(url);return r
        if url.endswith('2025FD.ZIP'):r=Response(out.getvalue());responses.append(r);calls.append(url);return r
        return original(url,**kwargs)
    monkeypatch.setattr(requests,'get',get)
    with request_policy.provider_budget('congress',1000,clock=lambda:1,limits=()):
        payload=m.fetch_audit_snapshot(date_filed_after='2025-12-30',date_filed_before='2026-01-02',absolute_deadline=1000)
    assert payload['audit_snapshot']['scope_evidence']['selected_years']==[2025,2026]
    assert payload['audit_snapshot']['scope_evidence']['window_filing_ids']=={'house':['54321'],'senate':[]}
    assert len(payload['audit_snapshot']['tables']['political_filings'])==2
    assert len(payload['audit_snapshot']['tables']['political_trades'])==2
    assert len(calls)==8 and all(r.closed for r in responses)


def test_stale_generated_snapshot_is_not_freshened_by_source_check(monkeypatch):
    m=module();raw=synthetic_raw();manifest=json.loads(raw['snapshot.json']);manifest['generatedAt']='2026-10-01T12:00:00Z';raw['snapshot.json']=json.dumps(manifest).encode()
    install_transport(monkeypatch,m,raw)
    with pytest.raises(SourceFetchError):fetch(m)


def test_rehashed_wrong_raw_artifact_kind_rejected(monkeypatch):
    m=module();install_transport(monkeypatch,m,synthetic_raw());payload=fetch(m)
    payload['audit_snapshot']['raw_artifacts'][0]['kind']='parquet'
    payload['audit_snapshot']['content_sha256']=m._snapshot_digest(payload['audit_snapshot'])
    with pytest.raises(ValueError):m.validate_audit_snapshot(payload,date_filed_after='2026-09-09',date_filed_before='2026-10-09',now=NOW)


def test_canonical_public_summary_rejects_actionable_or_unbounded_fields(monkeypatch):
    m=module();install_transport(monkeypatch,m,synthetic_raw());payload=fetch(m)
    summary=m.audit_summary(payload)
    assert hasattr(m,'canonical_audit_summary'),'pure public summary validator missing'
    assert m.canonical_audit_summary(summary)==summary
    for key,value in [('signals_enabled',True),('stock_disclosure_complete',True),('printed_row_count',True),('content_sha256','bad'),('unexpected_rows',[])]:
        bad=deepcopy(summary);bad[key]=value
        with pytest.raises(ValueError):m.canonical_audit_summary(bad)
    result=m.canonical_audit_summary(summary);result['filing_counts']['house']=999
    assert summary['filing_counts']['house']==1


def test_later_http_failure_retains_already_captured_artifacts(monkeypatch):
    m=module();raw=synthetic_raw()
    def changed(url,body):return Response(body,status=503) if 'political_filings/' in url else Response(body)
    calls,responses=install_transport(monkeypatch,m,raw,changed=changed)
    with pytest.raises(SourceFetchError) as error:fetch(m)
    assert error.value.http_status==503 and len(calls)==3
    assert len(error.value.partial_data['raw_audit_artifacts'])==2
    assert error.value.partial_data['coverage']['complete'] is False
    assert all(r.closed for r in responses)


@pytest.mark.parametrize('xml',['<Error>unavailable</Error>',
    '<FinancialDisclosure><Member><DocID>12345</DocID><FilingDate>10/1/2026</FilingDate></Member></FinancialDisclosure>'])
def test_malformed_house_index_is_not_a_valid_empty_discovery(xml):
    m=module();out=io.BytesIO()
    with zipfile.ZipFile(out,'w') as z:z.writestr('2026FD.xml',xml);z.writestr('2026FD.txt','synthetic')
    with pytest.raises(ValueError):m._house_index(out.getvalue(),2026,'2026-09-09','2026-10-09')


def test_anonymous_request_suppresses_ambient_netrc_auth(monkeypatch):
    m=module();prepared=[]
    # Exercise real Requests preparation, retaining an inert send boundary.
    monkeypatch.setattr(requests.sessions.Session,'request',NATIVE_SESSION_REQUEST)
    monkeypatch.setattr(requests.sessions,'get_netrc_auth',lambda url:('ambient-user','ambient-secret'))
    def send(self,request,**kwargs):prepared.append(request);return Response(b'{}')
    monkeypatch.setattr(requests.sessions.Session,'send',send)
    with request_policy.provider_budget('congress',1000,clock=lambda:1,limits=()):
        m._download(m.REVISION_URL,limit=100,revision='',shard=False,state={'bytes':0,'requests':0})
    assert len(prepared)==1 and 'Authorization' not in prepared[0].headers


def test_public_summary_requires_house_partition_conservation(monkeypatch):
    m=module();install_transport(monkeypatch,m,synthetic_raw());summary=m.audit_summary(fetch(m))
    for matched,additional,missing in [(0,0,1),(1,0,0)]:
        bad=deepcopy(summary);bad.update(status='partial_audit_gap',house_matched_filings=matched,house_additional_filings=additional,house_missing_filings=missing)
        with pytest.raises(ValueError):m.canonical_audit_summary(bad)


def test_real_hf_relative_immutable_cache_redirect(monkeypatch):
    from urllib.parse import urlencode
    m = module()
    revision = 'b10790bacd904501b3b002d5fa14b2f0acfac8c2'
    path = f'/datasets/{m.DATASET}/resolve/{revision}/snapshot.json'
    location = f'/api/resolve-cache/datasets/{m.DATASET}/{revision}/snapshot.json?' + urlencode({
        path: '', 'etag': '"ef38e73cbcec9b56ed4928cbbee3ad0b542dbc27"'})
    responses = [Response(status=307, headers={'Location': location}), Response(b'{}')]
    calls = []
    def get(url, **kwargs):
        calls.append(url)
        return responses[len(calls)-1]
    monkeypatch.setattr(requests, 'get', get)
    with request_policy.provider_budget('congress', 1000, clock=lambda: 1, limits=()):
        assert m._download(m.HF + path, limit=100, revision=revision, shard=False,
                           state={'bytes': 0, 'requests': 0}) == b'{}'
    assert calls == [m.HF + path, m.HF + location]
    assert all(response.closed for response in responses)
    for bad in [location.replace(revision, '0'*40, 1), location + '&download=true',
                location + '&etag=%22' + 'a'*40 + '%22', location.replace('snapshot.json?', 'other.json?')]:
        assert not m._redirect_allowed(m.HF + bad, original=m.HF + path, revision=revision, shard=False)
