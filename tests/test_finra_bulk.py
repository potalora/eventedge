"""Synthetic SQLite/native-shape tests; not actual installed-model parity proof."""
from copy import deepcopy
from datetime import date
import hashlib
import importlib
from importlib import metadata
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace as N
from typing import ClassVar

import pytest
from pydantic import BaseModel, ConfigDict, Field
import requests

from tradingagents.strategies.data_sources.openbb_source import OpenBBSource
from tradingagents.strategies.data_sources.request_policy import provider_budget

ALIASES = {'symbol':'symbolCode','issue_name':'issueName','market_class':'marketClassCode',
 'current_short_position':'currentShortPositionQuantity','previous_short_position':'previousShortPositionQuantity',
 'avg_daily_volume':'averageDailyVolumeQuantity','days_to_cover':'daysToCoverQuantity',
 'change_pct':'changePercent','change':'changePreviousNumber','settlement_date':'settlementDate'}
COLUMNS = list(ALIASES.values())


class NativeShape(BaseModel):
    __alias_dict__: ClassVar[dict] = ALIASES
    model_config = ConfigDict(populate_by_name=True)
    symbol: str = Field(alias='symbolCode')
    issue_name: str = Field(alias='issueName')
    market_class: str = Field(alias='marketClassCode')
    current_short_position: float = Field(alias='currentShortPositionQuantity')
    previous_short_position: float = Field(alias='previousShortPositionQuantity')
    avg_daily_volume: float = Field(alias='averageDailyVolumeQuantity')
    days_to_cover: float = Field(alias='daysToCoverQuantity')
    change_pct: float = Field(alias='changePercent')
    change: float = Field(alias='changePreviousNumber')
    settlement_date: date = Field(alias='settlementDate')


def raw(symbol='AAPL', **changes):
    return dict(zip(COLUMNS, [symbol,'Company','Q',400,300,100,4,33.33,100,'2026-09-30'])) | changes


@pytest.fixture
def native(tmp_path, monkeypatch):
    # Fake dependency boundary intentionally mirrors complete native model fields.
    # No installed OpenBB preparation/refresh or provider operation is invoked.
    path = tmp_path/'finra.db'
    conn = sqlite3.connect(path)
    conn.execute('CREATE TABLE short_interest ("index" INTEGER, symbolCode TEXT, issueName TEXT, marketClassCode TEXT, currentShortPositionQuantity INTEGER, previousShortPositionQuantity INTEGER, averageDailyVolumeQuantity INTEGER, daysToCoverQuantity REAL, changePercent REAL, changePreviousNumber INTEGER, settlementDate TEXT)')
    conn.commit();conn.close()
    state = N(path=path, prepare_calls=0, transforms=[], queries=[], prepare_hook=None, transform_hook=None)
    def add(rows):
        conn = sqlite3.connect(path)
        conn.executemany('INSERT INTO short_interest VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                         [(i,*[row[name] for name in COLUMNS]) for i,row in enumerate(rows)])
        conn.commit();conn.close()
    state.add = add
    def prepare():
        state.prepare_calls += 1
        if state.prepare_hook:
            state.prepare_hook()
    class Fetcher:
        @staticmethod
        def transform_query(params):
            state.queries.append(params)
            return N(symbol=params['symbol'])
        @staticmethod
        def transform_data(query, rows):
            state.transforms.append((query.symbol, deepcopy(rows)))
            result = [NativeShape.model_validate(row) for row in rows]
            if state.transform_hook:
                state.transform_hook(query, result)
            return result
    storage = N(prepare_data=prepare,get_db_path=lambda:path,
                get_short_interest_dates=lambda:['20260915','20260930'])
    model = N(FinraShortInterestFetcher=Fetcher,FinraShortInterestData=NativeShape)
    original_import = importlib.import_module
    def imported(name, *args, **kwargs):
        if name=='openbb_finra.utils.data_storage':return storage
        if name=='openbb_finra.models.equity_short_interest':return model
        return original_import(name,*args,**kwargs)
    monkeypatch.setattr(importlib,'import_module',imported)
    original_version=metadata.version
    def installed_version(name):
        versions={'openbb-finra':'1.6.1','openbb-core':'1.6.13'}
        return versions[name] if name in versions else original_version(name)
    monkeypatch.setattr(metadata,'version',installed_version)
    state.model = model
    state.storage = storage
    state.source = OpenBBSource()
    state.source._obb = N()  # already initialized; no real SDK initialization
    return state


def fetch(native, symbols):
    return native.source.fetch_short_interest(symbols)


def test_full_866_population_uses_one_prepare_and_one_full_field_query(native, monkeypatch):
    symbols=[f'T{i:04}' for i in range(866)]
    native.add([raw(s) for s in reversed(symbols)])
    before=hashlib.sha256(native.path.read_bytes()).hexdigest()
    sql=[]; connect=sqlite3.connect
    def observed(*args,**kwargs):
        assert 'mode=ro' in args[0] and kwargs['uri'] is True
        connection=connect(*args,**kwargs);connection.set_trace_callback(sql.append);return connection
    monkeypatch.setattr(sqlite3,'connect',observed)
    result=fetch(native,symbols+[symbols[0]])
    assert list(result['short_interest'])==symbols and result['errors']=={}
    assert native.prepare_calls==1 and len(native.transforms)==866
    assert len([query for query in sql if 'WHERE symbolCode IN' in query])==1
    assert result['short_interest']['T0000']=={'short_interest':400.0,'short_pct_of_float':None,'days_to_cover':4.0,'date':'2026-09-30'}
    attempt=result['acquisition']['attempts'][0]
    assert attempt['row_count']==866 and attempt['missing_archive_dates']==['2026-09-15']
    assert len(attempt['rowset_sha256'])==64 and attempt['cache_path']==str(native.path)
    assert before==hashlib.sha256(native.path.read_bytes()).hexdigest()


def test_all_866_missing_have_complete_partition_and_no_success_cache(native):
    symbols=[f'T{i:04}' for i in range(866)]
    result=fetch(native,symbols)
    assert result['short_interest']=={} and list(result['errors'])==symbols
    assert native.prepare_calls==1 and len(native.transforms)==866
    assert native.source._cache=={}


def test_partial_success_caches_same_scalar_keys_and_retries_only_missing(native):
    native.add([raw('A'),raw('B',settlementDate='bad')])
    first=fetch(native,['A','B','C'])
    assert list(first['short_interest'])==['A'] and set(first['errors'])=={'B','C'}
    assert native.source.fetch({'method':'equity_short_interest','ticker':'A'})==first['short_interest']['A']
    assert native.prepare_calls==1
    conn=sqlite3.connect(native.path);conn.execute('DELETE FROM short_interest WHERE symbolCode=?',('B',));conn.commit();conn.close()
    native.add([raw('B'),raw('C')]);native.transforms.clear()
    second=fetch(native,['A','B','C'])
    assert list(second['short_interest'])==['A','B','C'] and second['errors']=={}
    assert [symbol for symbol,_ in native.transforms]==['B','C']
    fetch(native,['A','B','C']);assert native.prepare_calls==2


@pytest.mark.parametrize('rows',[
    [raw('A',settlementDate='bad'),raw('A')],
    [raw('A'),raw('A',currentShortPositionQuantity=500)],
    [raw('A',daysToCoverQuantity=-1)],
    [raw('A',daysToCoverQuantity=float('inf'))],
])
def test_bad_symbol_history_never_hides_healthy_sibling(native,rows):
    native.add(rows+[raw('B')])
    result=fetch(native,['A','B'])
    assert list(result['short_interest'])==['B'] and list(result['errors'])==['A']
    assert result['errors']['A']['reason_code']=='invalid_response'
    assert 'equity_short_interest|A' not in native.source._cache


def test_unordered_history_zero_and_identical_latest_duplicates_keep_scalar_semantics(native):
    native.add([raw('A',daysToCoverQuantity=0,averageDailyVolumeQuantity=0),
                raw('A',settlementDate='2021-07-15',currentShortPositionQuantity=10),
                raw('A',daysToCoverQuantity=0,averageDailyVolumeQuantity=0)])
    result=fetch(native,['A'])
    assert result['short_interest']['A']['days_to_cover']==0
    assert result['short_interest']['A']['short_pct_of_float'] is None


@pytest.mark.parametrize('kind',['version','aliases','schema','variable_limit'])
def test_unsupported_compatibility_fails_every_input_without_scalar_fallback(native,monkeypatch,kind):
    if kind=='version':
        import importlib.metadata
        monkeypatch.setattr(importlib.metadata,'version',lambda name:'9.9.9')
    elif kind=='aliases':
        class WrongShape(NativeShape):
            __alias_dict__: ClassVar[dict] = ALIASES|{'avg_daily_volume':'wrong'}
        native.model.FinraShortInterestData=WrongShape
    elif kind=='schema':
        conn=sqlite3.connect(native.path);conn.execute('ALTER TABLE short_interest ADD COLUMN unexpected TEXT');conn.close()
    else:
        original=sqlite3.connect
        def limited(*args,**kwargs):
            conn=original(*args,**kwargs);conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER,1);return conn
        monkeypatch.setattr(sqlite3,'connect',limited)
    result=fetch(native,['A','B'])
    assert result['short_interest']=={} and set(result['errors'])=={'A','B'}
    assert all(v['reason_code']=='invalid_response' for v in result['errors'].values())
    assert native.source._cache=={}
    if kind in ('version','aliases'):assert native.prepare_calls==0


@pytest.mark.parametrize('limit',['MAX_ROWS','MAX_BYTES'])
def test_global_scan_bound_failure_never_publishes_valid_prefix(native,monkeypatch,limit):
    from tradingagents.strategies.data_sources import finra_bulk
    native.add([raw('A'),raw('B')]);monkeypatch.setattr(finra_bulk,limit,1)
    result=fetch(native,['A','B'])
    assert result['short_interest']=={} and set(result['errors'])=={'A','B'}
    assert native.source._cache=={} and native.transforms==[]


@pytest.mark.parametrize('phase',['prepare','transform'])
def test_late_phase_cannot_publish_or_reset_60_second_child_budget(native,phase):
    clock=[0.0];native.add([raw('A'),raw('B')])
    if phase=='prepare':native.prepare_hook=lambda:clock.__setitem__(0,61)
    else:native.transform_hook=lambda *args:clock.__setitem__(0,61)
    with provider_budget('openbb',1000,clock=lambda:clock[0],sleep=lambda n:clock.__setitem__(0,clock[0]+n),limits=()):
        result=fetch(native,['A','B'])
    assert result['short_interest']=={} and set(result['errors'])=={'A','B'}
    assert all(x['reason_code']=='timeout' for x in result['errors'].values())
    assert native.prepare_calls==1 and native.source._cache=={}
    attempt=result['acquisition']['attempts'][0]
    assert attempt['inventory_before_observed'] is True
    assert attempt['inventory_after_observed'] is (phase=='transform')


def test_expired_original_deadline_skips_prepare_and_keeps_every_input(native):
    with provider_budget('openbb',10,clock=lambda:11,limits=()):
        result=fetch(native,['A','B'])
    assert result['short_interest']=={} and set(result['errors'])=={'A','B'}
    assert native.prepare_calls==0 and all(x['reason_code']=='timeout' for x in result['errors'].values())


def test_transient_prepare_retry_is_counted_and_uses_original_clock(native):
    clock=[0.0];native.add([raw('A')])
    def prepare():
        if native.prepare_calls==1:raise requests.Timeout('PRIVATE credentials')
    native.prepare_hook=prepare
    diagnostics=[]
    with provider_budget('openbb',100,clock=lambda:clock[0],sleep=lambda n:clock.__setitem__(0,clock[0]+n),
                         random_fn=lambda:0,max_attempts=2,limits=(),diagnostics=diagnostics):
        result=fetch(native,['A'])
    assert list(result['short_interest'])==['A'] and native.prepare_calls==2
    assert len(result['acquisition']['attempts'])==2 and clock[0]==.5
    assert diagnostics[-1]['attempts']==2 and diagnostics[-1]['recovered'] is True
    assert 'PRIVATE' not in json.dumps(result)


def test_orchestrator_keeps_full_population_and_acquisition_metadata(native,monkeypatch):
    from tradingagents.strategies.orchestration.cohort_orchestrator import CohortOrchestrator
    native.add([raw('A')]);monkeypatch.setattr(native.source,'is_available',lambda:True)
    monkeypatch.setattr(native.source,'fetch_profiles',lambda symbols:{'profiles':{},'errors':{}})
    native.source._obb=N(famafrench=N(factors=lambda **_:N(results=[])))
    owner=object.__new__(CohortOrchestrator);owner.cohorts=[{'engine':N(registry={'openbb':native.source})}]
    result=owner._fetch_openbb_enrichment([{'ticker':'B'},{'ticker':'A'},{'ticker':'A'}])
    assert list(result['short_interest'])==['A'] and set(result['errors']['short_interest'])=={'B'}
    assert result['short_interest_acquisition']['attempts'][0]['row_count']==1
    assert native.prepare_calls==1
    from tradingagents.strategies.orchestration.source_inputs import SourceInputStore
    envelope=SourceInputStore.decode(SourceInputStore.encode(result))
    assert envelope==result


def test_sql_progress_deadline_cancels_snapshot_and_closes_connection(native,monkeypatch):
    native.add([raw(f'T{i}') for i in range(1000)])
    clock=[0.0];original=sqlite3.connect;closed=[]
    class Connection:
        def __init__(self,conn):self.conn=conn
        def __getattr__(self,name):return getattr(self.conn,name)
        def set_progress_handler(self,callback,steps):
            if callback is None:
                self.conn.set_progress_handler(None,0);return
            def expire():
                clock[0]=61
                return callback()
            self.conn.set_progress_handler(expire,1)
        def close(self):closed.append(True);self.conn.close()
    monkeypatch.setattr(sqlite3,'connect',lambda *a,**k:Connection(original(*a,**k)))
    with provider_budget('openbb',1000,clock=lambda:clock[0],limits=()):
        result=fetch(native,['T0','T1'])
    assert set(result['errors'])=={'T0','T1'}
    assert all(v['reason_code']=='timeout' for v in result['errors'].values())
    assert closed==[True] and native.source._cache=={} and native.transforms==[]


def test_transformed_identity_mismatch_fails_only_its_symbol(native):
    native.add([raw('A'),raw('B')])
    def mismatch(query,rows):
        if query.symbol=='A':rows[0].symbol='OTHER'
    native.transform_hook=mismatch
    result=fetch(native,['A','B'])
    assert list(result['short_interest'])==['B']
    assert result['errors']['A']['reason_code']=='invalid_response'


def test_rowset_digest_preserves_duplicates_but_ignores_row_order(native):
    rows=[raw('A'),raw('B'),raw('A')];native.add(rows)
    first=fetch(native,['A','B'])['acquisition']['attempts'][0]['rowset_sha256']
    conn=sqlite3.connect(native.path);conn.execute('DELETE FROM short_interest');conn.commit();conn.close()
    native.add(list(reversed(rows)));native.source.clear_cache()
    second=fetch(native,['A','B'])['acquisition']['attempts'][0]['rowset_sha256']
    assert first==second
    conn=sqlite3.connect(native.path);conn.execute('DELETE FROM short_interest WHERE rowid=(SELECT min(rowid) FROM short_interest)');conn.commit();conn.close()
    native.source.clear_cache()
    assert fetch(native,['A','B'])['acquisition']['attempts'][0]['rowset_sha256']!=first


@pytest.mark.parametrize('change',[
    lambda v:v.update(raw_rows=['PRIVATE']),
    lambda v:v.update(schema_version=True),
    lambda v:v.update(cached_count=99),
    lambda v:v['attempts'][0].update(credentials='PRIVATE'),
    lambda v:v['attempts'][0].update(versions={'openbb-finra':'9'}),
    lambda v:v['attempts'][0].update(cache_path='https://PRIVATE'),
    lambda v:v['attempts'][0].update(rowset_sha256='PRIVATE'),
    lambda v:v['attempts'][0].update(cached_archive_dates=['bad']),
    lambda v:v['attempts'][0].update(row_count=True),
    lambda v:v['attempts'][0].update(reason_code='PRIVATE'),
    lambda v:v.update(attempts=v['attempts']*6),
])
def test_durable_metadata_rejects_unrecognized_or_unbounded_content(native,change):
    from tradingagents.strategies.data_sources.finra_bulk import validated_acquisition
    native.add([raw('A')]);value=fetch(native,['A'])['acquisition'];change(value)
    with pytest.raises(ValueError,match='Invalid FINRA acquisition metadata'):
        validated_acquisition(value)


def test_attempt_records_exact_population_clock_and_before_after_archives(native):
    symbols=['B','A'];native.add([raw('A',settlementDate='2026-09-15')]);clock=[3.0]
    def prepare():
        clock[0]=7.0
        native.add([raw('B')])
    native.prepare_hook=prepare
    with provider_budget('openbb',100,clock=lambda:clock[0],limits=()):
        result=fetch(native,symbols)
    attempt=result['acquisition']['attempts'][0]
    assert attempt['prepare_started_at']==3.0
    assert attempt['prepare_finished_at']==7.0 and attempt['prepare_elapsed_seconds']==4.0
    assert attempt['cached_archive_dates_before']==['2026-09-15']
    assert attempt['missing_archive_dates_before']==['2026-09-30']
    assert attempt['cached_archive_dates']==['2026-09-15','2026-09-30']
    assert attempt['missing_archive_dates']==[]
    expected=hashlib.sha256(json.dumps(sorted(symbols),separators=(',',':')).encode()).hexdigest()
    assert attempt['query_population_sha256']==expected
    assert result['acquisition']['population_sha256']==expected


def test_query_identity_drift_fails_individual_symbol_before_transform(native):
    native.add([raw('A'),raw('B')])
    native.model.FinraShortInterestFetcher.transform_query=lambda params:N(symbol='OTHER' if params['symbol']=='A' else 'B')
    result=fetch(native,['A','B'])
    assert list(result['short_interest'])==['B'] and result['errors']['A']['reason_code']=='invalid_response'
    assert [symbol for symbol,_ in native.transforms]==['B']


def test_existing_empty_native_database_can_be_prepared(native):
    conn=sqlite3.connect(native.path);conn.execute('DROP TABLE short_interest');conn.close()
    def prepare():
        conn=sqlite3.connect(native.path)
        conn.execute('CREATE TABLE short_interest ("index" INTEGER, symbolCode TEXT, issueName TEXT, marketClassCode TEXT, currentShortPositionQuantity INTEGER, previousShortPositionQuantity INTEGER, averageDailyVolumeQuantity INTEGER, daysToCoverQuantity REAL, changePercent REAL, changePreviousNumber INTEGER, settlementDate TEXT)')
        conn.commit();conn.close();native.add([raw('A')])
    native.prepare_hook=prepare
    result=fetch(native,['A'])
    assert list(result['short_interest'])==['A'] and native.prepare_calls==1
    assert result['acquisition']['attempts'][0]['cached_archive_dates_before']==[]


def test_incomplete_query_closes_response_and_cannot_publish_prefix(native,monkeypatch):
    native.add([raw('A'),raw('B')]);original=sqlite3.connect;closed=[]
    class Cursor:
        def __init__(self,cursor):self.cursor=cursor;self.calls=0
        def fetchmany(self,size):
            self.calls+=1
            if self.calls>1:raise sqlite3.OperationalError('PRIVATE')
            return self.cursor.fetchmany(size)
        def close(self):closed.append('cursor');self.cursor.close()
    class Connection:
        def __init__(self,conn):self.conn=conn
        def __getattr__(self,name):return getattr(self.conn,name)
        def execute(self,sql,*args):
            cursor=self.conn.execute(sql,*args)
            return Cursor(cursor) if 'WHERE symbolCode IN' in sql else cursor
        def close(self):closed.append('connection');self.conn.close()
    monkeypatch.setattr(sqlite3,'connect',lambda *a,**k:Connection(original(*a,**k)))
    result=fetch(native,['A','B'])
    assert set(result['errors'])=={'A','B'} and result['short_interest']=={}
    assert native.transforms==[] and native.source._cache=={}
    assert closed==['connection','cursor','connection']
    assert 'PRIVATE' not in json.dumps(result)


def test_durable_metadata_isolation_and_cached_call_identity(native):
    from tradingagents.strategies.data_sources.finra_bulk import validated_acquisition
    native.add([raw('A')]);result=fetch(native,['A']);original=deepcopy(result['acquisition'])
    copied=validated_acquisition(original)
    copied['attempts'][0]['cached_archive_dates'].clear()
    assert original==result['acquisition']
    cached=fetch(native,['A'])
    assert cached['acquisition']['attempts']==[] and cached['acquisition']['cached_count']==1
    assert cached['acquisition']['population_sha256']==original['population_sha256']
    assert native.prepare_calls==1


def test_actual_batch_caller_metadata_is_durable_on_later_different_acquisition(native,tmp_path):
    from test_session_executor import _policy_enabled_staging_fixture, FRIDAY
    native.add([raw('AAPL')])
    first_batch=fetch(native,['AAPL'])
    ledger,engine,call=_policy_enabled_staging_fixture(tmp_path/'staging')
    call['annualized_volatility_evidence']={'AAPL':.31}
    call['enrichment']['short_interest_acquisition']=deepcopy(first_batch['acquisition'])
    try:
        first=engine.screen_and_stage(**call)
        expected=deepcopy(first['committee_decision_status']['short_interest_acquisition'])
        native.add([raw('AAPL',settlementDate='2026-10-01')]);native.source.clear_cache()
        later=fetch(native,['AAPL'])
        assert later['acquisition']!=first_batch['acquisition']
        call['enrichment']['short_interest_acquisition']=later['acquisition']
        replay=engine.screen_and_stage(**call)
        assert replay['replayed'] is True
        assert replay['committee_decision_status']['short_interest_acquisition']==expected
        assert ledger.committee_decision(FRIDAY,'epoch','foundation-30d')['status']['short_interest_acquisition']==expected
    finally:
        ledger.close()


def test_incomplete_success_metadata_cannot_certify_an_acquisition():
    from tradingagents.strategies.data_sources.finra_bulk import validated_acquisition,new_attempt
    value={'schema_version':1,'requested_count':1,'cached_count':0,'population_sha256':'a'*64,
           'attempts':[new_attempt()|{'status':'success','reason_code':None}]}
    with pytest.raises(ValueError,match='Invalid FINRA acquisition metadata'):
        validated_acquisition(value)
