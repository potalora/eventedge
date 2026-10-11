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
    result.ar_config = {'finnhub_acquisition': settings}
    return result


def test_earnings_budget_is_order_independent_and_retains_every_exclusion():
    first_source, reverse_source = Provider(), Provider(reverse=True)
    first = engine(first_source)._fetch_finnhub_data('2026-10-09')
    reverse = engine(reverse_source)._fetch_finnhub_data('2026-10-09')
    assert first_source.earnings_calls == reverse_source.earnings_calls
    assert len(first_source.earnings_calls) == 10
    manifest = first['coverage']['acquisition_admission']['earnings_news']
    assert manifest == reverse['coverage']['acquisition_admission']['earnings_news']
    assert manifest['discovered_count'] == 13 and manifest['admitted_count'] == 10
    assert manifest['excluded_count'] == 3
    assert {row['reason'] for row in manifest['excluded']} == {'acquisition_budget'}
    assert {row['identity']['symbol'] for row in manifest['excluded']} == {'T10', 'T11', 'T12'}
    assert len({row['discovery_id'] for row in manifest['discovered']}) == 13
    assert first['transcripts'] == reverse['transcripts']


def test_pqc_queries_use_declared_order_independent_universe():
    first_source, reverse_source = Provider(), Provider(reverse=True)
    first = engine(first_source, pqc_universe=DEFAULT_PQC)._fetch_finnhub_data('2026-10-09')
    reverse = engine(reverse_source, pqc_universe=list(reversed(DEFAULT_PQC)))._fetch_finnhub_data('2026-10-09')
    # The first seven calls serve the separately declared supply-chain universe.
    assert first_source.company_calls[7:] == reverse_source.company_calls[7:] == sorted(DEFAULT_PQC)[:6]
    manifest = first['coverage']['acquisition_admission']['pqc_news']
    assert manifest == reverse['coverage']['acquisition_admission']['pqc_news']
    assert manifest['discovered_count'] == 10 and manifest['excluded_count'] == 4
    assert first['pqc_news'] == reverse['pqc_news']


def test_article_evidence_bound_is_declared_before_transcript_materialization():
    result = engine(Provider(earnings_count=1))._fetch_finnhub_data('2026-10-09')
    manifests = result['coverage']['acquisition_admission']['earnings_articles']
    assert len(manifests) == 1
    manifest = manifests[0]
    assert manifest['discovered_count'] == 7 and manifest['admitted_count'] == 5
    assert manifest['excluded_count'] == 2
    text = result['transcripts'][0]['transcript_text']
    assert 'Quantum guidance 0' not in text and 'Quantum guidance 1' not in text
    for i in range(2, 7):
        assert f'Quantum guidance {i}' in text
    assert result['transcripts'][0]['acquisition_discovery_id']


def test_zero_acquisition_budgets_retain_discoveries_without_requests():
    source = Provider()
    result = engine(source, earnings_news_budget=0, pqc_symbol_budget=0)._fetch_finnhub_data('2026-10-09')
    assert source.earnings_calls == []
    assert len(source.company_calls) == 7
    coverage = result['coverage']['acquisition_admission']
    assert coverage['earnings_news']['excluded_count'] == 13
    assert coverage['pqc_news']['excluded_count'] == 10
    assert result.get('transcripts', []) == [] and result.get('pqc_news', []) == []
    assert 'error' not in result


@pytest.mark.parametrize('key,value', [('earnings_news_budget', True), ('pqc_symbol_budget', -1),
                                      ('earnings_article_budget', 1.5)])
def test_invalid_budget_configuration_fails_before_provider_io(key, value):
    source = Provider()
    with pytest.raises(ValueError, match='nonnegative integer'):
        engine(source, **{key: value})._fetch_finnhub_data('2026-10-09')
    assert source.earnings_calls == [] and source.company_calls == []


def test_acquisition_manifest_reaches_native_strategy_health():
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
    coverage = health[0].evidence['source_coverage']['finnhub']['acquisition_admission']
    assert coverage['earnings_news']['discovered_count'] == 13
    assert coverage['earnings_news']['excluded_count'] == 3
