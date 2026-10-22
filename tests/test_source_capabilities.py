"""Constructed source capability acceptance; no provider/model calls."""
from copy import deepcopy
import pandas as pd
import pytest
from tradingagents.strategies.orchestration import research_resilience as rr
from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
from tradingagents.strategies.data_sources.fred_source import FREDSource, SERIES_MAP
from tradingagents.strategies.data_sources.yfinance_source import YFinanceSource
from tradingagents.strategies.orchestration.source_inputs import SourceInputStore

POLICY = 'regime_strategy_capabilities_v1'


def series(value=4.0):
    result = pd.Series([value, value], index=pd.to_datetime(['2026-10-01', '2026-10-02']))
    result.attrs.update(observation_start='2025-04-06', observation_end='2026-10-06', vintage_date='2026-10-06',
        acquired_at='2026-10-06T22:00:00+00:00', available_at='2026-10-06T22:00:00+00:00',
        availability_basis='forward_acquisition_time', observation_records=[
            {'observation_date': day, 'value': value, 'realtime_start': '2026-10-06', 'realtime_end': '2026-10-06'}
            for day in ['2026-10-01', '2026-10-02']])
    return result


def engine(tmp_path, source, provider='fred'):
    from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine
    from tradingagents.strategies.data_sources.registry import DataSourceRegistry
    registry = DataSourceRegistry(); registry.register(source)
    return MultiStrategyEngine({'autoresearch': {'state_dir': str(tmp_path),
        'research_resilience_policy': rr.POLICY, 'source_capability_policy': POLICY}}, strategies=[], registry=registry)


def fred_payload(tmp_path, failed=()):
    source = FREDSource('offline')
    def fetch(sid, start, end, **kwargs):
        if sid in failed:
            raise SourceFetchError('FRED series fetch failed', reason_code='invalid_response',
                failed_operations={sid: 'invalid_response'}, partial_data={sid: series()})
        return series()
    source.fetch_series = fetch
    return engine(tmp_path, source)._fetch_fred_data('2026-09-01', '2026-10-06')


def project(payload):
    return rr.project_sources({'fred': payload}, ('fred',), capability_policy=POLICY)


def test_typed_maintenance_survives_frozen_capability_validation(tmp_path):
    source = FREDSource('offline')
    def unavailable(sid, *args, **kwargs):
        raise SourceFetchError('FRED provider maintenance', reason_code='provider_maintenance',
                               http_status=200, attempts=3)
    source.fetch_series = unavailable
    payload = engine(tmp_path, source)._fetch_fred_data('2026-09-01', '2026-10-06')
    frozen = SourceInputStore.decode(SourceInputStore.encode(payload))
    selected, scope = project(frozen)
    assert 'fred' not in selected
    assert scope['capabilities']['fred.vix']['status'] == 'excluded'
    outcomes = frozen['_capability_acquisition']['groups']['economic_indicators']['outcomes']
    assert all(row['reason'] == 'provider_maintenance' and row['http_status'] == 200 for row in outcomes.values())


def test_optional_payroll_failure_keeps_shared_regime_and_original(tmp_path):
    original = fred_payload(tmp_path, ['PAYEMS']); before = SourceInputStore.encode(original)
    selected, scope = project(original)
    assert scope['version'] == 'research-source-selection-v2'
    assert set(('BAMLH0A0HYM2', 'T10Y2Y', 'VIXCLS')) <= set(selected['fred'])
    assert 'PAYEMS' not in selected['fred'] and 'payrolls' not in selected['fred']
    assert scope['capabilities']['fred.hy_spread']['status'] == 'usable'
    assert scope['capabilities']['fred.commodity_macro']['status'] == 'usable'
    assert scope['research_coverage_complete'] is False
    assert SourceInputStore.encode(original) == before
    assert 'PAYEMS' in original['error']
    assert scope == project(SourceInputStore.decode(SourceInputStore.encode(original)))[1]


@pytest.mark.parametrize('required', ['BAMLH0A0HYM2', 'T10Y2Y', 'VIXCLS'])
def test_failed_series_prefix_never_becomes_accepted(tmp_path, required):
    selected, scope = project(fred_payload(tmp_path, [required]))
    assert required not in selected['fred']
    name = {'BAMLH0A0HYM2': 'hy_spread', 'T10Y2Y': 'yield_curve', 'VIXCLS': 'vix'}[required]
    assert scope['capabilities']['fred.' + name]['status'] == 'excluded'


@pytest.mark.parametrize('corruption', ['extra_error', 'alias', 'unknown_operation', 'contradictory', 'clock'])
def test_manifest_integrity_is_not_an_optional_failure(tmp_path, corruption):
    payload = fred_payload(tmp_path)
    if corruption == 'extra_error': payload['error'] = 'forged error'
    if corruption == 'alias': payload['hy_spread'] = series(99.0)
    if corruption == 'unknown_operation': payload['_capability_acquisition']['groups']['credit_spreads']['requested'].append('UNKNOWN')
    if corruption == 'contradictory': payload['_capability_acquisition']['groups']['credit_spreads']['status'] = 'failed'
    if corruption == 'clock': payload['_capability_acquisition']['finished_at'] = 'bad-clock'
    with pytest.raises(ValueError, match='source_capability'):
        project(payload)


def test_missing_manifest_is_unavailable_and_policy_absence_is_unchanged():
    payload = {'BAMLH0A0HYM2': series()}
    assert project(payload)[0] == {}
    old, scope = rr.project_sources({'fred': payload}, ('fred',))
    assert old['fred'] is payload and scope['version'] == 'research-source-selection-v1'


@pytest.mark.parametrize('value', ['unknown', True, {}, 3])
def test_invalid_capability_policy_fails_before_state_creation(tmp_path, value):
    from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine
    state = tmp_path/'not-created'
    with pytest.raises(ValueError, match='source_capability_policy'):
        MultiStrategyEngine({'autoresearch': {'state_dir': str(state), 'source_capability_policy': value}})
    assert not state.exists()


def test_capability_requires_dependency_isolation(tmp_path):
    from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine
    with pytest.raises(ValueError, match='source_capability_policy'):
        MultiStrategyEngine({'autoresearch': {'state_dir': str(tmp_path/'none'), 'source_capability_policy': POLICY}})


@pytest.mark.parametrize('bad', [float('nan'), float('inf')])
def test_current_nonfinite_observation_never_falls_back(tmp_path, bad):
    payload = fred_payload(tmp_path); payload['BAMLH0A0HYM2'].iloc[-1] = bad
    selected, scope = project(payload)
    assert 'BAMLH0A0HYM2' not in selected['fred']


def yahoo_engine(tmp_path, *, price_failure=False, vix_failure=False):
    source = YFinanceSource()
    def prices(tickers, start, end):
        frame = pd.DataFrame({('Close', ticker): [20., 21.] for ticker in tickers},
            index=pd.to_datetime(['2026-10-01', '2026-10-02']))
        if price_failure:
            raise SourceFetchError('Yahoo symbol history incomplete', reason_code='invalid_response',
                failed_operations={ticker: 'invalid_response' for ticker in tickers}, partial_data={'prices': frame})
        return frame
    source.fetch_prices = prices
    def vix(start, end):
        if vix_failure: raise SourceFetchError('Yahoo VIX history failed', reason_code='timeout')
        return pd.DataFrame({'Close': [18., 19.]}, index=pd.to_datetime(['2026-10-01', '2026-10-02']))
    source.fetch_vix = vix
    return engine(tmp_path, source)


def test_yahoo_failed_basket_over_diagnostic_cap_preserves_only_vix_and_no_cache(tmp_path):
    owner = yahoo_engine(tmp_path, price_failure=True)
    payload = owner._fetch_yfinance_data('2026-09-01', '2026-10-06')
    selected, scope = rr.project_sources({'yfinance': payload}, ['yfinance'], capability_policy=POLICY)
    assert selected['yfinance']['vix'].iloc[-1]['Close'] == 19.
    assert selected['yfinance'].get('prices', {}) == {}
    assert not owner._price_cache
    assert scope['capabilities']['yfinance.research_prices']['status'] == 'excluded'
    assert scope['capabilities']['yfinance.vix']['status'] == 'usable'
    assert scope['research_coverage_complete'] is False


def test_yahoo_failed_vix_keeps_complete_research_basket(tmp_path):
    owner = yahoo_engine(tmp_path, vix_failure=True)
    payload = owner._fetch_yfinance_data('2026-09-01', '2026-10-06')
    selected, scope = rr.project_sources({'yfinance': payload}, ['yfinance'], capability_policy=POLICY)
    assert len(selected['yfinance']['prices']) == 37
    assert 'vix' not in selected['yfinance']
    assert scope['capabilities']['yfinance.research_prices']['status'] == 'usable'


def capability_screen(tmp_path, *, failed=(), strategy='good', dependency='good_source', yahoo=None):
    from test_research_resilience import _screen_engine
    owner, strategies = _screen_engine(tmp_path/'screen')
    owner.ar_config['source_capability_policy'] = POLICY
    strategies[0].name, strategies[0].data_sources = strategy, [dependency]
    data = {'good_source': {'event': 'accepted'}, 'bad_source': {'error': 'failed'},
        'fred': fred_payload(tmp_path/'fred', failed)}
    if yahoo is not None:
        data['yfinance'] = yahoo
    signals, regime, health = owner.screen_and_enrich('2026-10-06', data, epoch_id='epoch', policy_id='policy')
    return owner, strategies, data, signals, regime, health


def test_optional_failure_screening_and_zero_io_projection_replay(tmp_path):
    from tradingagents.strategies.orchestration.research_resilience import validate_projection
    owner, strategies, data, signals, regime, health = capability_screen(tmp_path, failed=['PAYEMS'])
    assert signals[0]['strategy'] == 'good'
    assert regime['research_hold_reasons'] == []
    assert 'PAYEMS' not in strategies[0].seen[0]['fred']
    assert regime['credit_spread_bps'] == 400.
    assert next(row for row in health if row.strategy == 'good').status == 'signals'
    frozen = SourceInputStore.decode(SourceInputStore.encode(data))
    # Reconstruction needs only retained bytes, not registry or adapters.
    owner.registry = None
    replay = validate_projection(owner, frozen, regime)
    assert rr.digest(replay) == regime['research_projection_sha256']
    forged = deepcopy(regime); forged['research_source_scope']['capabilities']['fred.hy_spread']['sha256'] = '0'*64
    with pytest.raises(ValueError, match='invalid_research_source_projection'):
        validate_projection(owner, frozen, forged)


@pytest.mark.parametrize('required', ['BAMLH0A0HYM2', 'T10Y2Y'])
def test_required_failure_retains_shared_hold_for_healthy_strategy(tmp_path, required):
    _, _, _, signals, regime, health = capability_screen(tmp_path, failed=[required])
    assert regime['research_hold_reasons'] == ['shared_regime_unavailable']
    assert all(row['journal_only'] for row in signals)
    assert next(row for row in health if row.strategy == 'good').status == 'data_failure'


def test_commodity_compartment_does_not_require_optional_payroll(tmp_path):
    _, strategies, _, signals, regime, health = capability_screen(tmp_path,
        failed=['PAYEMS'], strategy='commodity_macro', dependency='fred')
    assert strategies[0].seen and signals[0]['strategy'] == 'commodity_macro'
    assert next(row for row in health if row.strategy == 'commodity_macro').status == 'signals'
    assert rr.strategy_capability_holds(regime['research_source_scope'], 'unreviewed', ['fred']) == ['fred.complete']


def test_commodity_failure_does_not_poison_shared_regime(tmp_path):
    _, strategies, _, signals, regime, health = capability_screen(tmp_path,
        failed=['CPIAUCSL'], strategy='commodity_macro', dependency='fred')
    assert not strategies[0].seen and not signals
    assert regime['research_hold_reasons'] == []
    assert next(row for row in health if row.strategy == 'commodity_macro').status == 'data_failure'


@pytest.mark.parametrize('fred_vix,yahoo_vix', [(False, False), (True, False), (False, True), (True, True)])
def test_vix_precedence_and_fallback(tmp_path, fred_vix, yahoo_vix):
    owner = yahoo_engine(tmp_path/'yahoo', vix_failure=yahoo_vix)
    yahoo = owner._fetch_yfinance_data('2026-09-01', '2026-10-06')
    _, _, _, _, regime, _ = capability_screen(tmp_path/'screen', failed=['VIXCLS'] if fred_vix else [], yahoo=yahoo)
    assert regime['vix_level'] == (None if fred_vix and yahoo_vix else 4. if yahoo_vix else 19.)
    assert regime['research_hold_reasons'] == (['shared_regime_unavailable'] if fred_vix and yahoo_vix else [])


def test_unscoped_batch_error_cannot_reuse_partial_series(tmp_path):
    source = FREDSource('offline')
    source.fetch_credit_spreads = lambda *args: {'BAMLH0A0HYM2': series(), 'BAMLC0A4CBBB': series()}
    def unscoped(*args):
        raise SourceFetchError('FRED unavailable', reason_code='timeout', partial_data={
            key: series() for key in ['UNRATE', 'CPIAUCSL', 'PAYEMS', 'ICSA', 'FEDFUNDS', 'VIXCLS', 'T10Y2Y']})
    source.fetch_economic_indicators = unscoped
    payload = engine(tmp_path, source)._fetch_fred_data('2026-09-01', '2026-10-06')
    selected, scope = project(payload)
    assert 'BAMLH0A0HYM2' in selected['fred']
    assert 'T10Y2Y' not in selected['fred']
    assert scope['capabilities']['fred.yield_curve']['status'] == 'excluded'


@pytest.mark.parametrize('corruption', ['duplicate_index', 'bounds', 'records', 'vintage', 'acquisition_date'])
def test_accepted_series_lineage_corruption_raises(tmp_path, corruption):
    payload = fred_payload(tmp_path)
    value = payload['BAMLH0A0HYM2']
    if corruption == 'duplicate_index': value.index = pd.to_datetime(['2026-10-01']*2)
    if corruption == 'bounds': value.attrs['observation_end'] = '2027-01-01'
    if corruption == 'records': value.attrs['observation_records'][-1]['value'] = 90.
    if corruption == 'vintage': payload['vintage']['as_of'] = '2026-10-07'
    if corruption == 'acquisition_date': value.attrs['acquired_at'] = 'bad'
    with pytest.raises(ValueError, match='source_capability'):
        project(payload)


@pytest.mark.parametrize('corruption', ['unexpected_coverage', 'future_clock', 'noncanonical_date'])
def test_accepted_capability_cannot_hide_contradictory_coverage_or_clock(tmp_path, corruption):
    payload = fred_payload(tmp_path)
    if corruption == 'unexpected_coverage': payload['coverage'] = {'complete': False}
    if corruption == 'future_clock':
        payload['BAMLH0A0HYM2'].attrs.update(acquired_at='2099-01-01T00:00:00+00:00', available_at='2099-01-01T00:00:00+00:00')
    if corruption == 'noncanonical_date': payload['_capability_acquisition']['observation_end'] = '20261006'
    with pytest.raises(ValueError, match='source_capability'):
        project(payload)


def test_state_compartment_retains_accepted_snapshot_independent_of_payroll(tmp_path):
    from test_state_economics_policy import snapshot, config
    payload = fred_payload(tmp_path, ['PAYEMS'])
    payload.update(state_unemployment=snapshot(), state_exposure_registry=config()['state_exposure_manifest'],
        state_policy_settings={'policy': 'state_unemployment_v1', 'release_lookback_days': 7})
    payload['coverage'] = {'state_unemployment': payload['state_unemployment']['coverage']}
    selected, scope = project(payload)
    assert selected['fred']['state_unemployment'] == payload['state_unemployment']
    assert rr.strategy_capability_holds(scope, 'state_economics', ['fred', 'edgar']) == []
    assert scope['research_coverage_complete'] is False


def test_incomplete_state_compartment_holds_state_only_and_corruption_raises(tmp_path):
    from test_state_economics_policy import snapshot, config
    payload = fred_payload(tmp_path)
    state = snapshot(); state['states'].pop('AL')
    state['coverage'] = {'expected_states': 51, 'accepted_states': 50,
        'failed_states': {'AL': 'timeout'}, 'complete': False}
    payload.update(state_unemployment=state, state_exposure_registry=config()['state_exposure_manifest'],
        state_policy_settings={'policy': 'state_unemployment_v1', 'release_lookback_days': 7},
        coverage={'state_unemployment': state['coverage']})
    selected, scope = project(payload)
    assert 'state_unemployment' not in selected['fred']
    assert rr.strategy_capability_holds(scope, 'state_economics', ['fred']) == ['fred.state_unemployment']
    assert rr.strategy_capability_holds(scope, 'commodity_macro', ['fred']) == []
    payload['coverage']['state_unemployment'] = {'complete': True}
    with pytest.raises(ValueError):
        project(payload)


def test_capability_policy_rotates_source_and_execution_identity(tmp_path):
    from decimal import Decimal
    from tradingagents.strategies.orchestration.source_inputs import source_configuration_fingerprint
    from tradingagents.strategies.orchestration.session_executor import SessionExecutor
    from tradingagents.strategies.state.portfolio_ledger import PortfolioLedger
    from test_metric_epoch_runtime import _context, _policy
    config = {'autoresearch': {'research_resilience_policy': rr.POLICY}}
    before = source_configuration_fingerprint(config)
    ledger = PortfolioLedger(tmp_path/'ledger.db', 'test', Decimal('5000'))
    try:
        old = SessionExecutor(ledger, config).semantic_policy_document()
        config['autoresearch']['source_capability_policy'] = POLICY
        new = SessionExecutor(ledger, config).semantic_policy_document()
        assert source_configuration_fingerprint(config) != before
        assert new['source_capability_policy'] == POLICY
        assert _context(cohort_policies=(_policy(execution_policy=old),)).config_hash != \
            _context(cohort_policies=(_policy(execution_policy=new),)).config_hash
    finally:
        ledger.close()


@pytest.mark.parametrize('identity', ['NOT_REQUESTED', 'unknown'])
@pytest.mark.parametrize('frozen', [False, True])
def test_unscoped_manifest_rejects_unknown_failure_identity_before_partial_reuse(tmp_path, identity, frozen):
    payload = fred_payload(tmp_path)
    error = SourceFetchError('unscoped', reason_code='timeout', failed_operations={identity: 'timeout'})
    payload['_capability_acquisition']['groups']['economic_indicators'] = rr.acquisition_group(
        rr.FRED_GROUPS['economic_indicators'], error=error, independent=True)
    payload['error'] = str(error)
    if frozen:
        payload = SourceInputStore.decode(SourceInputStore.encode(payload))
    with pytest.raises(ValueError, match='invalid_source_capability_acquisition'):
        project(payload)


def test_rejected_yahoo_vix_prefix_survives_immutable_original_but_never_projection(tmp_path):
    owner = yahoo_engine(tmp_path, vix_failure=True)
    partial = pd.DataFrame({'Close': [17.0]}, index=pd.to_datetime(['2026-10-01']))
    partial.attrs['evidence'] = 'original rejected VIX prefix'
    def failed_vix(start, end):
        raise SourceFetchError('Yahoo VIX history failed', reason_code='invalid_response', partial_data={'vix': partial})
    owner.registry.get('yfinance').fetch_vix = failed_vix
    payload = owner._fetch_yfinance_data('2026-09-01', '2026-10-06')
    assert 'vix' in payload
    pd.testing.assert_frame_equal(payload['vix'], partial)
    frozen = SourceInputStore.decode(SourceInputStore.encode(payload))
    pd.testing.assert_frame_equal(frozen['vix'], partial)
    assert frozen['vix'].attrs == partial.attrs
    selected, scope = rr.project_sources({'yfinance': frozen}, ['yfinance'], capability_policy=POLICY)
    assert 'vix' not in selected['yfinance']
    assert scope['capabilities']['yfinance.vix']['status'] == 'excluded'
    assert scope['capabilities']['yfinance.vix']['sha256'] == rr.digest(frozen['vix'])
    assert '^VIX' not in owner._price_cache
    assert owner._build_regime_model(selected)['vix_level'] is None
