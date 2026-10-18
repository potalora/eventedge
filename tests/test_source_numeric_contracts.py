"""Malformed latest numbers cannot create a healthy macro/regime input."""
import pandas as pd
import pytest

from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError, source_number
from tradingagents.strategies.data_sources.fred_source import FREDSource
from tradingagents.strategies.data_sources.yfinance_source import YFinanceSource


@pytest.mark.parametrize('value', [
    pytest.param(10 ** 400, id='oversized-positive-integer'),
    pytest.param(-(10 ** 400), id='oversized-negative-integer'),
])
def test_source_number_rejects_integers_that_overflow_finite_conversion(value):
    assert source_number(value) is False


@pytest.mark.parametrize(('value', 'minimum', 'maximum', 'expected'), [
    (-0.09, None, None, True),
    (0, None, None, True),
    (10 ** 300, None, None, True),
    (-1.0, 0, None, False),
    (101.0, None, 100, False),
    (True, None, None, False),
    ('1.5163', None, None, False),
    (None, None, None, False),
])
def test_source_number_keeps_finite_domain_and_type_contract(value, minimum, maximum, expected):
    assert source_number(value, minimum=minimum, maximum=maximum) is expected


@pytest.mark.parametrize('bad', [float('inf'), float('-inf'), -1.0, 'invalid', float('nan')])
@pytest.mark.parametrize('method', ['prices', 'vix'])
def test_yahoo_mixed_invalid_history_is_partial_not_cached(monkeypatch, method, bad):
    frame = pd.DataFrame({'Close': [20.0, bad]}, index=pd.to_datetime(['2026-10-02', '2026-10-05']))
    monkeypatch.setattr('yfinance.download', lambda *args, **kwargs: frame.copy())
    source = YFinanceSource()
    with pytest.raises(SourceFetchError) as exc:
        if method == 'prices':
            source.fetch_prices(['AAPL'], '2026-10-01', '2026-10-06')
        else:
            source.fetch_vix('2026-10-01', '2026-10-06')
    partial = exc.value.partial_data[method]
    assert len(partial) == 1
    assert float(partial.iloc[0, 0]) == 20.0
    assert not source._cache


@pytest.mark.parametrize('bad', [float('inf'), float('-inf'), 'invalid', float('nan')])
def test_fred_mixed_invalid_series_preserves_partial_through_batch(monkeypatch, bad):
    series = pd.Series([4.0, bad], index=pd.to_datetime(['2026-10-02', '2026-10-05']))
    monkeypatch.setattr('fredapi.Fred.get_series', lambda *args, **kwargs: series.copy())
    source = FREDSource(api_key='offline')
    with pytest.raises(SourceFetchError) as exc:
        source.fetch_multi_series(['UNRATE'], '2026-10-01', '2026-10-06')
    assert exc.value.partial_data['UNRATE'].tolist() == [4.0]
    assert not source._cache


def test_fred_ordinary_interior_missing_value_remains_explicit(monkeypatch):
    series = pd.Series([4.0, float('nan'), 5.0])
    monkeypatch.setattr('fredapi.Fred.get_series', lambda *args, **kwargs: series.copy())
    result = FREDSource(api_key='offline').fetch_series('UNRATE', '2026-10-01', '2026-10-06')
    pd.testing.assert_series_equal(result, series)
