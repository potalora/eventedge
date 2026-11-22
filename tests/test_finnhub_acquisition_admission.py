"""Acquisition bounds disclose the population before screen/analysis budgets."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine


DEFAULT_PQC = ['CRWD', 'PANW', 'ZS', 'FTNT', 'IBM', 'CSCO', 'MSFT', 'IONQ', 'RGTI', 'COIN']


class Provider:
    def __init__(self, reverse=False, earnings_count=13):
        self.reverse = reverse
        self.earnings = [{'symbol': f'T{i:02}', 'date': '2026-10-08', 'year': 2026,
                          'quarter': 3, 'epsActual': 1.2, 'epsEstimate': 1.} for i in range(earnings_count)]
        self.earnings_calls, self.company_calls = [], []

    def new_workflow_deadline(self, **kwargs):
        return 123.

    def fetch_recent_earnings(self, *args, **kwargs):
        return deepcopy(list(reversed(self.earnings)) if self.reverse else self.earnings)

    def news(self, symbol):
        records = [{'id': f'{symbol}-{i}', 'headline': f'Quantum guidance {i}', 'summary': 'Raised earnings guidance',
                    'source': 'fixture', 'published_at': f'2026-10-08T12:0{i}:00+00:00'} for i in range(7)]
        return list(reversed(records)) if self.reverse else records

    def fetch_earnings_news(self, symbol, edate, **kwargs):
        self.earnings_calls.append((symbol, edate))
        return self.news(symbol)

    def fetch_company_news(self, symbol, *args, **kwargs):
        self.company_calls.append(symbol)
        return self.news(symbol)

    def fetch_supply_chains(self, *args, **kwargs):
        return {}


def engine(source, **settings):
    result = object.__new__(MultiStrategyEngine)
    result.registry = SimpleNamespace(get=lambda name: source if name == 'finnhub' else None)
    result.config = {'autoresearch': {'finnhub_acquisition': settings}}
    result.ar_config = result.config['autoresearch']
    return result


def test_earnings_budget_is_order_independent():
    first_source, reverse_source = Provider(), Provider(reverse=True)
    first = engine(first_source)._fetch_finnhub_data('2026-10-09')
    reverse = engine(reverse_source)._fetch_finnhub_data('2026-10-09')
    assert first_source.earnings_calls == reverse_source.earnings_calls
    assert len(first_source.earnings_calls) == 10
    assert first['coverage']['contract'] == 'finnhub-acquisition-v2'
    assert first['coverage']['earnings_discovered'] == 13
    assert first['coverage']['earnings_news_queried'] == 10
    assert first['coverage']['earnings_emitted'] == 13
    assert first['coverage'] == reverse['coverage']
    assert first['transcripts'] == reverse['transcripts']


def test_supply_chain_universe_is_queried_once_without_pqc():
    first_source, reverse_source = Provider(), Provider(reverse=True)
    from tradingagents.strategies.modules.supply_chain import get_supply_chain_universe
    expected_sc = list(get_supply_chain_universe({}))
    first = engine(first_source)._fetch_finnhub_data('2026-10-09')
    assert first_source.company_calls == expected_sc
    assert 'pqc_news' not in first
    assert first['coverage']['supply_chain_query_universe'] == expected_sc


def test_article_evidence_bound_caps_transcript_articles():
    result = engine(Provider(earnings_count=1))._fetch_finnhub_data('2026-10-09')
    assert result['coverage']['earnings_emitted'] == 1
    text = result['transcripts'][0]['transcript_text']
    assert 'Quantum guidance 0' not in text and 'Quantum guidance 1' not in text
    for i in range(2, 7):
        assert f'Quantum guidance {i}' in text


def test_zero_acquisition_budgets_retain_numeric_eps_without_news_requests():
    from tradingagents.strategies.modules.supply_chain import get_supply_chain_universe
    source = Provider()
    result = engine(source, earnings_news_budget=0)._fetch_finnhub_data('2026-10-09')
    assert source.earnings_calls == []
    assert len(source.company_calls) == len(get_supply_chain_universe({}))
    assert result['coverage']['earnings_discovered'] == 13
    assert result['coverage']['earnings_news_queried'] == 0
    assert result['coverage']['earnings_emitted'] == 13
    assert all(t['text_source'] == 'numeric_eps_surprise' for t in result['transcripts'])
    assert 'error' not in result


@pytest.mark.parametrize('key,value', [('earnings_news_budget', True), ('earnings_news_budget', -1),
                                      ('earnings_article_budget', 1.5)])
def test_invalid_budget_configuration_fails_before_provider_io(key, value):
    source = Provider()
    with pytest.raises(ValueError, match='nonnegative integer'):
        engine(source, **{key: value})._fetch_finnhub_data('2026-10-09')
    assert source.earnings_calls == [] and source.company_calls == []


def test_acquisition_coverage_reaches_native_strategy_health():
    from tradingagents.strategies.modules.earnings_call import EarningsCallStrategy
    instance = engine(Provider())
    payload = instance._fetch_finnhub_data('2026-10-09')
    instance.paper_trade_strategies = [EarningsCallStrategy()]
    instance._on_event = lambda *args, **kwargs: None
    instance._build_regime_model = lambda data: {}
    instance._analyzer = SimpleNamespace(analyze_earnings_call=lambda *args, **kwargs: {
        'direction': 'long', 'conviction': .8, 'rationale': 'retained guidance evidence'})
    _signals, _regime, health = instance.screen_and_enrich('2026-10-09', {'finnhub': payload},
        epoch_id='epoch', policy_id='policy')
    coverage = health[0].evidence['source_coverage']['finnhub']
    assert coverage['earnings_discovered'] == 13
    assert coverage['earnings_news_queried'] == 10
