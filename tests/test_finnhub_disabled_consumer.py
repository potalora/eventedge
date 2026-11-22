"""Partial Finnhub endpoint failure isolates siblings while flagging partial coverage."""
from test_finnhub_acquisition_admission import Provider, engine
from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError


class FailingCompanyNews(Provider):
    def fetch_company_news(self, symbol, *args, **kwargs):
        self.company_calls.append(symbol)
        if symbol == 'NVDA':
            raise SourceFetchError('Unavailable fixture route', reason_code='http_error', http_status=403)
        return self.news(symbol)


def test_supply_chain_news_failure_marks_partial_without_dropping_earnings():
    result = engine(FailingCompanyNews(earnings_count=1))._fetch_finnhub_data('2026-10-09')
    assert result['error'] and result['coverage']['status'] == 'partial'
    assert result['transcripts'] and result['disruption_news']


def test_earnings_failure_marks_partial_without_blocking_supply_chain():
    class FailedEarnings(Provider):
        def fetch_recent_earnings(self, *args, **kwargs):
            raise SourceFetchError('Unavailable earnings', reason_code='timeout')

    result = engine(FailedEarnings())._fetch_finnhub_data('2026-10-09')
    assert result['error'] and result['coverage']['status'] == 'partial'
    assert result['disruption_news']
