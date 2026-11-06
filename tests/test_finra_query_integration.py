"""Public API routing, accepted provenance and immutable committee replay."""
from copy import deepcopy
from datetime import datetime
import json
from types import SimpleNamespace as N

import pytest
import requests

from tradingagents.strategies.data_sources import finra_query, openbb_source
from tradingagents.strategies.data_sources.finra_bulk import validated_acquisition
from tradingagents.strategies.data_sources.request_policy import provider_budget


def row(symbol, **changes):
    return {'symbolCode': symbol, 'settlementDate': '2026-10-15', 'accountingYearMonthNumber': 20261015,
            'currentShortPositionQuantity': 400, 'daysToCoverQuantity': 4,
            'averageDailyVolumeQuantity': 100} | changes


@pytest.fixture
def query(monkeypatch):
    state = N(rows=[row('AAPL'), row('MSFT')], calls=[], hook=None)
    monkeypatch.setattr(openbb_source, 'acquisition_time', lambda: '2026-10-31T09:00:00+00:00')
    class Session:
        trust_env = True
        def post(self, url, **kwargs):
            state.calls.append(kwargs)
            assert self.trust_env is False and kwargs['allow_redirects'] is False
            if state.hook:
                state.hook()
            symbols = kwargs['json']['domainFilters'][0]['values']
            rows = [r for r in state.rows if r['symbolCode'] in symbols]
            content = json.dumps(rows).encode()
            headers = requests.structures.CaseInsensitiveDict({'Content-Length': str(len(content)),
                'Content-Type': 'application/json', 'Record-Total': str(len(rows)),
                'Record-Offset': '0', 'Record-Limit': str(kwargs['json']['limit'])})
            return N(status_code=200, headers=headers,
                     iter_content=lambda **_: iter([content]), close=lambda: None)
        def close(self):
            pass
    monkeypatch.setattr(finra_query.requests, 'Session', Session)
    state.source = openbb_source.OpenBBSource()
    monkeypatch.setattr(state.source, '_get_obb', lambda: pytest.fail('API route initialized SDK'))
    return state


def test_public_batch_and_scalar_share_api_cache_without_native_sdk(query):
    first = query.source.fetch_short_interest(['MSFT', 'AAPL', 'MSFT'])
    assert list(first['short_interest']) == ['MSFT', 'AAPL'] and not first['errors']
    assert first['acquisition']['schema_version'] == 4
    assert validated_acquisition(first['acquisition']) == first['acquisition']
    assert query.source.fetch({'method': 'equity_short_interest', 'ticker': 'AAPL'}) == first['short_interest']['AAPL']
    assert len(query.calls) == 1
    cached = query.source.fetch_short_interest(['AAPL', 'MSFT'])['acquisition']
    assert cached['cached_count'] == 2 and cached['attempts'] == []


def test_partial_query_retries_only_missing_symbols_and_retains_provenance(query):
    query.rows = [row('AAPL')]
    first = query.source.fetch_short_interest(['AAPL', 'MSFT'])
    assert list(first['short_interest']) == ['AAPL'] and list(first['errors']) == ['MSFT']
    query.rows.append(row('MSFT'))
    second = query.source.fetch_short_interest(['AAPL', 'MSFT'])
    assert second['errors'] == {} and second['acquisition']['cached_count'] == 1
    coverage = second['acquisition']['attempts'][-1]['query_coverage']
    assert coverage['requested_symbols'] == coverage['accepted_symbols'] == ['MSFT']
    assert query.calls[-1]['json']['domainFilters'][0]['values'] == ['MSFT']


def test_cache_rechecks_current_publication_and_reference_session(query, monkeypatch):
    query.source.fetch_short_interest(['AAPL'])
    monkeypatch.setattr(openbb_source, 'acquisition_time', lambda: '2026-11-11T09:00:00+00:00')
    query.rows = []
    result = query.source.fetch_short_interest(['AAPL'])
    assert result['short_interest'] == {} and 'AAPL' in result['errors']
    assert 'equity_short_interest|AAPL' not in query.source._cache
    query.rows = [row('AAPL')]
    earlier = query.source.fetch_short_interest(['AAPL'], as_of=datetime.fromisoformat('2026-10-31T09:00:00+00:00'))
    assert earlier['short_interest']['AAPL']['date'] == '2026-10-15'


def test_transient_query_retries_with_original_clock_and_separate_receipts(query):
    clock = [0.0]
    def fail_once():
        if len(query.calls) == 1:
            raise requests.Timeout('PRIVATE')
    query.hook = fail_once
    with provider_budget('openbb', 10, clock=lambda: clock[0], sleep=lambda n: clock.__setitem__(0, clock[0]+n),
                         random_fn=lambda: 0, limits=(), max_attempts=5):
        result = query.source.fetch_short_interest(['AAPL'])
    assert len(query.calls) == 2 and clock[0] == .5
    assert [a['status'] for a in result['acquisition']['attempts']] == ['error', 'success']
    assert 'PRIVATE' not in json.dumps(result)


def test_expired_inherited_deadline_starts_no_request_and_caches_nothing(query):
    with provider_budget('openbb', 1, clock=lambda: 2, limits=()):
        result = query.source.fetch_short_interest(['AAPL'])
    assert not query.calls and not query.source._cache
    assert result['errors']['AAPL']['reason_code'] == 'timeout'
    assert validated_acquisition(result['acquisition']) == result['acquisition']


def test_query_timeout_does_not_fall_back_or_exceed_two_attempts(query):
    query.hook = lambda: (_ for _ in ()).throw(requests.Timeout('PRIVATE'))
    clock = [0.0]
    with provider_budget('openbb', 10, clock=lambda: clock[0], sleep=lambda n: clock.__setitem__(0, clock[0]+n),
                         random_fn=lambda: 0, limits=(), max_attempts=5):
        result = query.source.fetch_short_interest(['AAPL'])
    assert len(query.calls) == 2 and len(result['acquisition']['attempts']) == 2
    assert result['short_interest'] == {} and query.source._cache == {}


def test_monitor_retains_full_query_receipt_and_exact_accepted_scope(query):
    from tradingagents.strategies.orchestration.research_resilience import project_enrichment
    from tradingagents.strategies.orchestration.enrichment_monitor import assess_accepted_enrichment
    query.rows = [row('AAPL')]
    result = query.source.fetch_short_interest(['AAPL', 'MSFT'])
    original = {'short_interest': result['short_interest'], 'errors': {'short_interest': result['errors']},
                'short_interest_acquisition': result['acquisition']}
    _, scope = project_enrichment(original)
    observation = assess_accepted_enrichment(original, scope)
    assert observation['short_interest_acquisition'] == result['acquisition']
    assert observation['healthy_scope'] == [{'provider': 'finra', 'operation': 'short_interest', 'symbol': 'AAPL'}]
    assert observation['status'] == 'degraded'
    assert 'archive_coverage' not in json.dumps(observation)


@pytest.mark.parametrize('change', ['missing_error', 'wrong_population', 'wrong_date', 'missing_number', 'boolean_number'])
def test_monitor_rejects_query_receipt_contradicting_accepted_facts(query, change):
    from tradingagents.strategies.orchestration.research_resilience import project_enrichment
    from tradingagents.strategies.orchestration.enrichment_monitor import assess_accepted_enrichment
    query.rows = [row('AAPL')]
    result = query.source.fetch_short_interest(['AAPL', 'MSFT'])
    original = {'short_interest': result['short_interest'], 'errors': {'short_interest': result['errors']},
                'short_interest_acquisition': result['acquisition']}
    if change == 'missing_error':
        original['errors'] = {}
    elif change == 'wrong_population':
        original['short_interest']['OTHER'] = original['short_interest'].pop('AAPL')
    elif change == 'wrong_date':
        original['short_interest']['AAPL']['date'] = '2026-09-30'
    elif change == 'missing_number':
        original['short_interest']['AAPL'].pop('short_interest')
    else:
        original['short_interest']['AAPL']['short_interest'] = True
    _, scope = project_enrichment(original)
    with pytest.raises(ValueError, match='contradicts'):
        assess_accepted_enrichment(original, scope)


def test_committee_replay_keeps_original_schema4_after_different_acquisition(query, tmp_path):
    from test_session_executor import _policy_enabled_staging_fixture, FRIDAY
    from tradingagents.strategies.orchestration.source_inputs import SourceInputStore
    first_batch = query.source.fetch_short_interest(['AAPL'])
    ledger, engine, call = _policy_enabled_staging_fixture(tmp_path / 'staging')
    call['annualized_volatility_evidence'] = {'AAPL': .31}
    call['enrichment']['short_interest_acquisition'] = deepcopy(first_batch['acquisition'])
    assert SourceInputStore.decode(SourceInputStore.encode(first_batch)) == first_batch
    try:
        first = engine.screen_and_stage(**call)
        expected = deepcopy(first['committee_decision_status']['short_interest_acquisition'])
        query.rows = []
        query.source.clear_cache()
        later = query.source.fetch_short_interest(['AAPL'])
        call['enrichment']['short_interest_acquisition'] = later['acquisition']
        replay = engine.screen_and_stage(**call)
        assert replay['replayed'] is True
        assert replay['committee_decision_status']['short_interest_acquisition'] == expected
        assert ledger.committee_decision(FRIDAY, 'epoch', 'foundation-30d')['status']['short_interest_acquisition'] == expected
    finally:
        ledger.close()
