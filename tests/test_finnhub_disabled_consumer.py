"""A disabled PQC consumer cannot consume quota or fail active Finnhub work."""
from test_finnhub_acquisition_admission import Provider, engine, DEFAULT_PQC
from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError


class FailingPQC(Provider):
    def fetch_company_news(self, symbol, *args, **kwargs):
        self.company_calls.append(symbol)
        if symbol == 'COIN':
            raise SourceFetchError('Unavailable fixture route', reason_code='http_error', http_status=403)
        return self.news(symbol)


def test_disabled_pqc_makes_no_queries_and_preserves_active_source_research():
    source = FailingPQC(earnings_count=1)
    owner = engine(source)
    owner.ar_config['disabled_strategies'] = {'quantum_readiness': 'filing_research_disabled'}
    result = owner._fetch_finnhub_data('2026-10-09')
    assert source.company_calls == ['AAPL', 'TSLA', 'NVDA', 'AMZN', 'BA', 'CAT', 'DE']
    assert 'error' not in result and result['transcripts'] and result['disruption_news']
    assert result['transcripts'][0]['text_source'] == 'earnings_news'
    assert 'pqc_news' not in result
    scope = result['coverage']['acquisition_admission']['pqc_news']
    assert scope['discovered_count'] == scope['excluded_count'] == len(DEFAULT_PQC)
    assert scope['admitted_count'] == 0 and scope['budget'] == 6
    assert {row['reason'] for row in scope['excluded']} == {'strategy_disabled'}
    assert scope['consumer'] == 'quantum_readiness'
    assert result['coverage']['acquisition_admission']['pqc_articles'] == []


def test_enabled_pqc_failure_still_prevents_complete_source_claim():
    result = engine(FailingPQC(earnings_count=1))._fetch_finnhub_data('2026-10-09')
    assert result['error'] and result['coverage']['status'] == 'partial'


def test_disabled_pqc_does_not_hide_active_earnings_failure():
    class FailedEarnings(Provider):
        def fetch_recent_earnings(self, *args, **kwargs):
            raise SourceFetchError('Unavailable earnings', reason_code='timeout')
    owner = engine(FailedEarnings())
    owner.ar_config['disabled_strategies'] = {'quantum_readiness': 'filing_research_disabled'}
    result = owner._fetch_finnhub_data('2026-10-09')
    assert result['error'] and result['coverage']['status'] == 'partial'
