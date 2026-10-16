import copy
from datetime import date, datetime, timezone
import json
import hashlib
import time
from types import SimpleNamespace

import pytest

from tradingagents.strategies.execution.session_activity import (
    AlpacaSessionActivitySource, evaluate_new_entry_eligibility as native_evaluate,
    resolve_new_entry_eligibility, load_new_entry_eligibility, POLICY,
)
import tradingagents.strategies.execution.session_activity as activity

SESSION = date(2026, 10, 9)
NOW = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)


def proofs(symbols):
    from tradingagents.strategies.data_sources.equity_universe import normalize_assets
    listing = normalize_assets([{'class': 'us_equity', 'symbol': s, 'exchange': 'NASDAQ',
        'status': 'active', 'tradable': True} for s in symbols], observed_at=NOW,
        response_sha256='a'*64)
    raw = json.dumps({'bars': {}, 'next_page_token': None})
    daily = {'source': 'alpaca-sip-1d-raw', 'session': SESSION.isoformat(), 'symbols': symbols,
        'observed_at': NOW.isoformat(), 'complete': True, 'pages': [{
            'url': 'https://data.alpaca.markets/v2/stocks/bars', 'status': 200,
            'params': {'symbols': ','.join(symbols), 'feed': 'sip', 'asof': '-', 'currency': 'USD',
                'adjustment': 'raw', 'timeframe': '1Day', 'sort': 'asc', 'limit': 201,
                'start': '2026-10-09T04:00:00+00:00', 'end': '2026-10-10T03:59:59.999999+00:00'},
            'body': raw, 'sha256': hashlib.sha256(raw.encode()).hexdigest()}]}
    return {'listing_snapshot': listing, 'daily_evidence': {s: daily for s in symbols},
        'daily_attempts': {s: [{'ticker': s, 'session': SESSION, 'source': 'alpaca-sip-1d-raw',
            'fetched_at': NOW, 'validation_error': f'missing {s}/{SESSION}'}] for s in symbols}}


def evaluate_new_entry_eligibility(evidence, session, candidates, protected, **kwargs):
    return native_evaluate(evidence, session, candidates, protected,
                           **{**proofs(candidates), **kwargs})


def trade(**changes):
    return dict(t='2026-10-09T20:00:00.524436883Z', x='Q', p=12.5,
                s=1, i=7, z='C', c=['@', 'I'], **changes) if not changes else {
                    **trade(), **changes}


class Response:
    status_code = 200
    headers = {}
    def __init__(self, body):
        self.body = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.closed = False
    def iter_content(self, chunk_size):
        yield self.body
    def close(self):
        self.closed = True


def acquire(monkeypatch, pages, symbols=('XYZ',)):
    monkeypatch.setenv('ALPACA_API_KEY', 'test')
    monkeypatch.setenv('ALPACA_SECRET_KEY', 'test')
    responses = [Response(p) for p in pages]
    calls = []
    def get(url, **kwargs):
        calls.append((url, kwargs))
        return responses[len(calls)-1]
    evidence = AlpacaSessionActivitySource(get=get, now=lambda: NOW).fetch_session_activity(
        list(symbols), SESSION)
    assert all(r.closed for r in responses[:len(calls)])
    return evidence, calls


def test_complete_non_qualifying_tape_preserves_colliding_ids_and_absent_symbol(monkeypatch):
    rows = [trade(), trade(t='2026-10-09T20:00:01.000000001Z', c=['M'])]
    evidence, calls = acquire(monkeypatch, [{'trades': {'XYZ': rows}, 'next_page_token': None}],
                              ('XYZ', 'EMPTY'))
    decisions = evaluate_new_entry_eligibility(evidence, SESSION, ['XYZ', 'EMPTY'], [])
    assert {d['status'] for d in decisions.values()} == {'excluded_from_new_entry'}
    assert evidence['row_count'] == 2
    assert json.loads(evidence['pages'][0]['body'])['trades']['XYZ'] == rows
    params = calls[0][1]['params']
    assert params['feed'] == 'sip' and params['asof'] == '-'
    assert params['end'] == '2026-10-10T03:59:59.999999999Z'


@pytest.mark.parametrize('row', [trade(c=['@']), trade(c=['@', 'P'])])
def test_qualifying_activity_does_not_excuse_missing_bar(monkeypatch, row):
    evidence, _ = acquire(monkeypatch, [{'trades': {'XYZ': [row]}, 'next_page_token': None}])
    assert evaluate_new_entry_eligibility(evidence, SESSION, ['XYZ'], [])['XYZ']['status'] == 'unresolved'


@pytest.mark.parametrize('row', [trade(c=['?', 'I']), trade(z='O'), trade(p=0),
    trade(s=0), trade(i=True), trade(t='2026-10-10T04:00:00Z'),
    trade(c=[]), trade(x=''), trade(t='2026-10-09T20:00:00.1234567890Z')])
def test_unknown_or_invalid_tape_fails_closed(monkeypatch, row):
    evidence, _ = acquire(monkeypatch, [{'trades': {'XYZ': [row]}, 'next_page_token': None}])
    assert evaluate_new_entry_eligibility(evidence, SESSION, ['XYZ'], [])['XYZ']['status'] == 'unresolved'


@pytest.mark.parametrize('payload', [
    {'trades': None, 'next_page_token': None}, {'trades': {}},
    {'trades': {'OTHER': []}, 'next_page_token': None},
    b'{"trades":{},"trades":{},"next_page_token":null}',
])
def test_bad_envelope_cannot_prove_absence(monkeypatch, payload):
    evidence, _ = acquire(monkeypatch, [payload])
    assert evaluate_new_entry_eligibility(evidence, SESSION, ['XYZ'], [])['XYZ']['status'] == 'unresolved'


def test_paging_terminal_and_protected_obligation(monkeypatch):
    evidence, calls = acquire(monkeypatch, [
        {'trades': {'XYZ': [trade()]}, 'next_page_token': 'page2'},
        {'trades': {}, 'next_page_token': None}])
    assert calls[1][1]['params']['page_token'] == 'page2'
    assert evaluate_new_entry_eligibility(evidence, SESSION, ['XYZ'], ['XYZ'])['XYZ']['status'] == 'protected_obligation'
    changed = copy.deepcopy(evidence)
    changed['pages'][0]['body'] = '{}'
    assert evaluate_new_entry_eligibility(changed, SESSION, ['XYZ'], [])['XYZ']['status'] == 'unresolved'


def test_frozen_scope_replay_never_refetches(monkeypatch, tmp_path):
    evidence, _ = acquire(monkeypatch, [{'trades': {}, 'next_page_token': None}])
    owner = SimpleNamespace(_base_config={'autoresearch': {'state_dir': str(tmp_path),
        'new_entry_eligibility_policy': POLICY}}, _metric_epoch_context=SimpleNamespace(
            generation_id='gen_test', generation_commit='a'*40))
    class Source:
        def fetch_session_activity(self, symbols, session, **kwargs):
            return evidence
    first = resolve_new_entry_eligibility(owner, SESSION, ['XYZ'], [], source=Source(), **proofs(['XYZ']))
    class Forbidden:
        def fetch_session_activity(self, *a):
            raise AssertionError('replay refetched')
    assert resolve_new_entry_eligibility(owner, SESSION, ['XYZ'], [], source=Forbidden(), **proofs(['XYZ'])) == first
    with pytest.raises(ValueError, match='scope'):
        resolve_new_entry_eligibility(owner, SESSION, ['OTHER'], [], source=Forbidden(), **proofs(['OTHER']))
    assert load_new_entry_eligibility(owner, SESSION, ['XYZ', 'GOOD'], [],
        listing_snapshot=proofs(['XYZ'])['listing_snapshot']) == first


@pytest.mark.parametrize('conditions,tape', [(['I'], 'A'), (['Q'], 'B'), (['M'], 'C'),
    (['B'], 'A'), (['W'], 'C'), (['@', '4', 'I'], 'C'), (['T'], 'A')])
def test_documented_daily_no_price_conditions(monkeypatch, conditions, tape):
    evidence, _ = acquire(monkeypatch, [{'trades': {'XYZ': [trade(c=conditions, z=tape)]},
                                        'next_page_token': None}])
    assert evaluate_new_entry_eligibility(evidence, SESSION, ['XYZ'], [])['XYZ']['status'] == 'excluded_from_new_entry'


@pytest.mark.parametrize('conditions,tape', [(['@', '4'], 'C'), ([' '], 'A'),
    (['B'], 'C'), (['9'], 'B'), (['G'], 'C')])
def test_documented_possible_daily_price_updates_keep_obligation(monkeypatch, conditions, tape):
    evidence, _ = acquire(monkeypatch, [{'trades': {'XYZ': [trade(c=conditions, z=tape)]},
                                        'next_page_token': None}])
    assert evaluate_new_entry_eligibility(evidence, SESSION, ['XYZ'], [])['XYZ']['status'] == 'unresolved'


@pytest.mark.parametrize('mutate', [
    lambda e: e.update(complete=False),
    lambda e: e.update(symbols=['OTHER']),
    lambda e: e.update(session='2026-10-08'),
    lambda e: e.update(observed_at='2999-01-01T00:00:00+00:00'),
    lambda e: e.update(observed_at='2026-10-09T22:00:00+00:00'),
    lambda e: e.update(row_count=True),
    lambda e: e['pages'][0]['params'].update(feed='iex'),
    lambda e: e['pages'][0].update(sha256='0'*64),
    lambda e: e['pages'].append(copy.deepcopy(e['pages'][0])),
])
def test_replay_rejects_altered_provenance_and_completeness(monkeypatch, mutate):
    evidence, _ = acquire(monkeypatch, [{'trades': {'XYZ': [trade()]}, 'next_page_token': None}])
    mutate(evidence)
    assert evaluate_new_entry_eligibility(evidence, SESSION, ['XYZ'], [])['XYZ']['status'] == 'unresolved'


@pytest.mark.parametrize('kind', ['pages', 'bytes', 'rows', 'cycle'])
def test_bound_exhaustion_and_paging_cycle_are_unresolved(monkeypatch, kind):
    if kind == 'pages':
        monkeypatch.setattr(activity, 'MAX_PAGES', 1)
        pages = [{'trades': {}, 'next_page_token': 'p2'}]
    elif kind == 'bytes':
        monkeypatch.setattr(activity, 'MAX_BYTES', 1)
        pages = [{'trades': {}, 'next_page_token': None}]
    elif kind == 'rows':
        monkeypatch.setattr(activity, 'MAX_ROWS', 0)
        pages = [{'trades': {'XYZ': [trade()]}, 'next_page_token': None}]
    else:
        pages = [{'trades': {}, 'next_page_token': 'p2'}] * 2
    evidence, _ = acquire(monkeypatch, pages)
    assert evidence['complete'] is False
    assert evaluate_new_entry_eligibility(evidence, SESSION, ['XYZ'], [])['XYZ']['status'] == 'unresolved'


def test_expired_inherited_deadline_prevents_native_call(monkeypatch):
    from tradingagents.strategies.data_sources.request_policy import provider_budget
    monkeypatch.setenv('ALPACA_API_KEY', 'test')
    monkeypatch.setenv('ALPACA_SECRET_KEY', 'test')
    calls = []
    source = AlpacaSessionActivitySource(get=lambda *a, **k: calls.append(a), now=lambda: NOW)
    with provider_budget('alpaca', time.monotonic()-1):
        evidence = source.fetch_session_activity(['XYZ'], SESSION)
    assert not calls and evidence['complete'] is False


def test_inherited_clock_advancing_during_get_cannot_publish_complete(monkeypatch):
    from tradingagents.strategies.data_sources.request_policy import provider_budget
    monkeypatch.setenv('ALPACA_API_KEY', 'test')
    monkeypatch.setenv('ALPACA_SECRET_KEY', 'test')
    start = time.monotonic()
    clock_now = [start]
    response = Response({'trades': {}, 'next_page_token': None})
    def get(*a, **k):
        clock_now[0] = start + 101
        return response
    diagnostics = []
    source = AlpacaSessionActivitySource(get=get, now=lambda: NOW)
    with provider_budget('alpaca', start+100, clock=lambda: clock_now[0],
                         sleep=lambda _: None, limits=(), diagnostics=diagnostics):
        evidence = source.fetch_session_activity(['XYZ'], SESSION)
    assert evidence['complete'] is False
    assert response.closed
    assert diagnostics


def test_transport_failure_cannot_be_exclusion(monkeypatch):
    from tradingagents.strategies.data_sources.request_policy import provider_budget
    monkeypatch.setenv('ALPACA_API_KEY', 'test')
    monkeypatch.setenv('ALPACA_SECRET_KEY', 'test')
    calls = []
    def broken(*a, **k):
        calls.append(a)
        raise OSError('private error')
    with provider_budget('alpaca', time.monotonic()+60, max_attempts=2,
                         sleep=lambda _: None, limits=()):
        evidence = AlpacaSessionActivitySource(get=broken, now=lambda: NOW).fetch_session_activity(['XYZ'], SESSION)
    assert len(calls) == 2
    assert 'private error' not in str(evidence)
    assert evaluate_new_entry_eligibility(evidence, SESSION, ['XYZ'], [])['XYZ']['status'] == 'unresolved'


def test_default_disabled_policy_and_expired_freeze_do_not_fetch(tmp_path):
    owner = SimpleNamespace(_base_config={'autoresearch': {'state_dir': str(tmp_path)}})
    with pytest.raises(ValueError, match='not enabled'):
        resolve_new_entry_eligibility(owner, SESSION, ['XYZ'], [])
    owner._base_config['autoresearch']['new_entry_eligibility_policy'] = POLICY
    with pytest.raises(TimeoutError, match='deadline'):
        resolve_new_entry_eligibility(owner, SESSION, ['XYZ'], [], deadline=time.monotonic()-1)
    assert not list(tmp_path.iterdir())


def test_opt_in_daily_capture_keeps_zero_row_invalid_and_old_attempt_shape(monkeypatch):
    from dataclasses import asdict
    from datetime import timedelta
    from tradingagents.strategies.execution.alpaca_daily_bar import AlpacaHistoricalSIPSource
    from tradingagents.strategies.execution.price_source import AlpacaSIPPriceSource
    monkeypatch.setenv('ALPACA_API_KEY', 'test')
    monkeypatch.setenv('ALPACA_SECRET_KEY', 'test')
    body = {'bars': {'XYZ': [{'t': '2026-10-09T04:00:00Z', 'o': 2., 'h': 2.,
        'l': 2., 'c': 2., 'v': 0, 'n': 0, 'vw': 0}]}, 'next_page_token': None}
    responses = []
    def get(*a, **k):
        response = Response(body)
        responses.append(response)
        return response
    source = AlpacaSIPPriceSource(sip_source=AlpacaHistoricalSIPSource(get=get),
        now=lambda: NOW, capture_eligibility=True)
    result = source.resolve_candidate_daily_bars(['XYZ'], SESSION, NOW, timedelta(hours=24))
    assert not result.bars and result.quarantined_tickers == frozenset({'XYZ'})
    assert set(asdict(result.attempts[0])) == {'ticker', 'session', 'attempt', 'source',
        'fetched_at', 'open', 'high', 'low', 'close', 'validation_error'}
    raw = result.eligibility_evidence['XYZ']['pages'][0]['body']
    assert json.loads(raw) == body
    assert all(r.closed for r in responses)
    tape, _ = acquire(monkeypatch, [{'trades': {'XYZ': [trade()]}, 'next_page_token': None}])
    bound = proofs(['XYZ'])
    bound['daily_attempts'] = {'XYZ': [asdict(result.attempts[0])]}
    bound['daily_evidence'] = dict(result.eligibility_evidence)
    decision = native_evaluate(tape, SESSION, ['XYZ'], [], **bound)['XYZ']
    assert decision['status'] == 'excluded_from_new_entry'
    assert decision['daily_failure_reason'] == 'zero_activity'


@pytest.mark.parametrize('field,value', [('v', None), ('v', True), ('v', -1), ('v', 1),
    ('n', None), ('n', True), ('n', -1), ('n', 1), ('o', 0), ('h', 1)])
def test_malformed_daily_bar_never_becomes_no_activity_exclusion(monkeypatch, field, value):
    tape, _ = acquire(monkeypatch, [{'trades': {}, 'next_page_token': None}])
    bound = proofs(['XYZ'])
    row = {'t': '2026-10-09T04:00:00Z', 'o': 2, 'h': 2, 'l': 2, 'c': 2, 'v': 0, 'n': 0}
    row[field] = value
    page = bound['daily_evidence']['XYZ']['pages'][0]
    page['body'] = json.dumps({'bars': {'XYZ': [row]}, 'next_page_token': None})
    page['sha256'] = hashlib.sha256(page['body'].encode()).hexdigest()
    assert native_evaluate(tape, SESSION, ['XYZ'], [], **bound)['XYZ']['status'] == 'unresolved'


@pytest.mark.parametrize('mutate', [
    lambda p: p.update(listing_snapshot=None),
    lambda p: p.update(daily_evidence=None),
    lambda p: p.update(daily_attempts=None),
    lambda p: p['daily_attempts']['XYZ'][-1].update(validation_error='transport_error XYZ'),
    lambda p: p['daily_attempts']['XYZ'][-1].update(ticker='OTHER'),
    lambda p: p['daily_evidence']['XYZ'].update(complete=False),
    lambda p: p['daily_evidence']['XYZ']['pages'][0].update(sha256='0'*64),
    lambda p: p['listing_snapshot']['assets'][0].__setitem__(1, 'OTC'),
])
def test_listing_and_daily_evidence_are_required_independent_proofs(monkeypatch, mutate):
    tape, _ = acquire(monkeypatch, [{'trades': {}, 'next_page_token': None}])
    bound = proofs(['XYZ'])
    mutate(bound)
    assert native_evaluate(tape, SESSION, ['XYZ'], [], **bound)['XYZ']['status'] == 'unresolved'


def test_absent_or_ambiguous_current_listing_stays_unresolved(monkeypatch):
    from tradingagents.strategies.data_sources.equity_universe import normalize_assets
    tape, _ = acquire(monkeypatch, [{'trades': {}, 'next_page_token': None}])
    for names in (['OTHER'], ['XYZ', 'XYZ']):
        bound = proofs(['XYZ'])
        bound['listing_snapshot'] = normalize_assets([{'class': 'us_equity', 'symbol': s,
            'exchange': 'NASDAQ', 'status': 'active', 'tradable': True} for s in names],
            observed_at=NOW, response_sha256='a'*64)
        assert native_evaluate(tape, SESSION, ['XYZ'], [], **bound)['XYZ']['status'] == 'unresolved'
