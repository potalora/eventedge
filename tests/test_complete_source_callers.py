"""Required orchestration must retain complete-window evidence from adapters."""
from types import SimpleNamespace

import pytest

from tradingagents.strategies.data_sources.evidence import CoverageRecords
from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
from tradingagents.strategies.learning.event_monitor import EventMonitor
from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine


class Form4Source:
    def __init__(self, returns):
        self.returns = returns

    def is_available(self):
        return True

    def get_recent_form4(self, ticker, **kwargs):
        result = self.returns[ticker]
        if isinstance(result, Exception):
            raise result
        return result


def monitor(returns):
    source = Form4Source(returns)
    result = EventMonitor(SimpleNamespace(get=lambda name: source))
    result.as_of = '2026-10-09'
    return result


def test_form4_monitor_requires_every_issuer_window_including_empty_windows():
    complete = {'mode': 'exhaustive_window', 'complete': True}
    result = monitor({
        'AAA': CoverageRecords([{'accession': 'filing-a'}], coverage=complete),
        'BBB': CoverageRecords([], coverage=complete),
    }).poll_form4_filings(['AAA', 'BBB'])
    assert result == {'AAA': [{'accession': 'filing-a'}]}
    assert result.coverage['complete'] is True
    assert result.coverage['mode'] == 'exhaustive_window'
    assert set(result.coverage['issuers']) == {'AAA', 'BBB'}


@pytest.mark.parametrize('incomplete', [[], CoverageRecords([], coverage={'complete': False})])
def test_form4_monitor_does_not_accept_an_unproven_empty_issuer(incomplete):
    with pytest.raises(SourceFetchError) as failure:
        monitor({'AAA': incomplete}).poll_form4_filings(['AAA'])
    assert failure.value.partial_data['form4'].coverage['complete'] is False
    assert failure.value.failed_operations == {'AAA': 'invalid_response'}


def test_form4_partial_failure_retains_other_issuers_and_whole_scope():
    complete = CoverageRecords([{'accession': 'filing-a'}], coverage={'complete': True})
    with pytest.raises(SourceFetchError) as failure:
        monitor({'AAA': complete, 'BBB': SourceFetchError('unavailable', reason_code='timeout')})\
            .poll_form4_filings(['AAA', 'BBB'])
    result = failure.value.partial_data['form4']
    assert result['AAA'] == [{'accession': 'filing-a'}]
    assert result.coverage['complete'] is False
    assert result.coverage['requested_tickers'] == ['AAA', 'BBB']


def test_engine_congress_path_requests_complete_window():
    class CongressSource:
        def get_recent_trades(self, *, days_back, as_of, complete_window=False):
            return CoverageRecords([], coverage={'complete': complete_window, 'date_to': as_of})

    source = CongressSource()
    engine = object.__new__(MultiStrategyEngine)
    engine.registry = SimpleNamespace(get=lambda name: source)
    result = engine._fetch_congress_data('2026-10-09')
    assert result == {'recent_trades': [], 'coverage': {'complete': True, 'date_to': '2026-10-09'}}
