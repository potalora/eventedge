from copy import deepcopy
from types import SimpleNamespace
import json

import pytest

from test_form4_evidence import original, bindings
from tradingagents.strategies.data_sources.form4_evidence import qualify_form4_window, POLICY
from tradingagents.strategies.orchestration.form4_inputs import form4_analysis_inputs, prepare_form4_request
from tradingagents.strategies.modules.insider_activity import InsiderActivityStrategy
from tradingagents.strategies.modules.base import Candidate


def window():
    value = original()
    return qualify_form4_window([value], security_bindings=bindings(value), date_from='2026-10-01', date_to='2026-10-22')


def test_actual_strategy_prospective_candidate_keeps_all_filing_context():
    value = window()
    candidates = InsiderActivityStrategy().screen({'edgar': {'form4_qualification': value}}, '2026-10-22',
        {'min_sell_threshold': 2, 'min_cluster_size': 2, 'max_positions': 3})
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.journal_only
    assert candidate.metadata['form4_evidence_policy'] == POLICY
    context = candidate.metadata['form4_context']
    inputs = form4_analysis_inputs(candidate, {'edgar': {'form4_qualification': value}})
    assert inputs == context
    request = prepare_form4_request(context, ticker='AAPL', cluster_type='sell_pattern')
    rendered = json.loads(request['user'])
    assert len(rendered['documents'][0]['transactions']) == 11
    assert len(rendered['documents'][0]['footnotes']) == 11
    assert 'February 6, 2026' in request['user']
    assert 'original_base64' not in rendered['documents'][0]
    assert 'do not establish exchange execution' in request['system']
    assert 'modification' in request['system']


@pytest.mark.parametrize('damage', ['selected_row', 'source', 'window', 'ticker', 'missing_context'])
def test_consumer_cannot_use_changed_or_missing_source(damage):
    value = window()
    candidate = InsiderActivityStrategy().screen({'edgar': {'form4_qualification': value}}, '2026-10-22', {'min_sell_threshold': 2})[0]
    if damage == 'selected_row': candidate.metadata['filings'][0]['shares'] += 1
    elif damage == 'source': candidate.metadata['form4_context']['window']['documents'][0]['footnotes'][0]['text'] = 'changed'
    elif damage == 'window': value['admitted_transactions'].clear()
    elif damage == 'ticker': candidate.ticker = 'OTHER'
    else: candidate.metadata.pop('form4_context')
    with pytest.raises(ValueError, match='invalid_form4'):
        form4_analysis_inputs(candidate, {'edgar': {'form4_qualification': value}})


def test_qualified_llm_consumer_uses_full_request_and_legacy_stays_unchanged(monkeypatch):
    from tradingagents.strategies.learning.llm_analyzer import LLMAnalyzer
    value = window()
    candidate = InsiderActivityStrategy().screen({'edgar': {'form4_qualification': value}}, '2026-10-22', {'min_sell_threshold': 2})[0]
    analyzer = object.__new__(LLMAnalyzer)
    analyzer._prompt_overrides = {}
    captured = []
    analyzer._call_llm = lambda system, user, **kwargs: captured.append((system, user)) or '{"direction":"neutral","conviction":0}'
    analyzer.analyze_10b5_1_plan(candidate.metadata['filings'], 'AAPL', form4_context=candidate.metadata['form4_context'])
    assert len(json.loads(captured[-1][1])['documents'][0]['transactions']) == 11
    assert 'modification' in captured[-1][0]
    analyzer._regime_suffix = lambda context: ''
    analyzer.analyze_10b5_1_plan([{'old': index} for index in range(12)], 'AAPL')
    assert '"old": 9' in captured[-1][1] and '"old": 10' not in captured[-1][1]


def test_qualification_survives_real_source_codec_without_changing_disabled_policy():
    from tradingagents.strategies.orchestration.source_inputs import SourceInputStore
    from tradingagents.strategies.orchestration.filing_research_policy import configured, sentinel
    data = {'edgar': {'form4_qualification': window()}}
    assert SourceInputStore.decode(SourceInputStore.encode(data)) == data
    assert configured({'filing_research_policy': 'disabled_v1', 'research_resilience_policy': 'dependency_isolation_v1'})
    assert sentinel()['error'] == 'filing_research_disabled'


def test_actual_engine_rebinds_qualified_context_to_frozen_shared_data_before_model():
    from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine
    value = window()
    shared = {'edgar': {'form4_qualification': value}}
    candidate = InsiderActivityStrategy().screen(shared, '2026-10-22', {'min_sell_threshold': 2})[0]
    engine = object.__new__(MultiStrategyEngine)
    engine.ar_config = {}
    calls = []
    analyzer = SimpleNamespace(analyze_10b5_1_plan=lambda *a, **k: calls.append(k) or {'direction': 'neutral', 'conviction': 0, 'rationale': 'Retained context.'})
    # A separately self-consistent candidate window is still not the frozen source.
    raw = __import__('base64').b64decode(value['documents'][0]['original_base64'])
    forged_doc = original(raw=raw.replace(b'<issuerTradingSymbol>AAPL</issuerTradingSymbol>', b'<issuerTradingSymbol>OTHER</issuerTradingSymbol>'))
    forged_bindings = [dict(bindings(forged_doc)[0], ticker='OTHER')]
    from tradingagents.strategies.data_sources.form4_evidence import qualify_form4_window
    forged_window = qualify_form4_window([forged_doc], security_bindings=forged_bindings,
        date_from='2026-10-01', date_to='2026-10-22')
    candidate = InsiderActivityStrategy().screen({'edgar': {'form4_qualification': forged_window}}, '2026-10-22', {'min_sell_threshold': 2})[0]
    engine._analyze_candidate(candidate, 'insider_activity', None, analyzer, filing_data=shared)
    assert not calls
    assert candidate.metadata['analysis_status'] == 'failed'


def test_eligible_ticker_alone_does_not_bind_different_source_issuer():
    from test_source_inputs import NOW
    from tradingagents.strategies.data_sources.equity_universe import EquityUniverse, normalize_assets
    snapshot = normalize_assets([{'class': 'us_equity', 'symbol': 'AAPL', 'exchange': 'NASDAQ',
        'status': 'active', 'tradable': True}], observed_at=NOW, response_sha256='f'*64)
    universe = EquityUniverse(snapshot, company_map={'0': {'cik_str': 1, 'ticker': 'AAPL'}})
    value = window()
    shared = {'edgar': {'form4_qualification': value}}
    candidate = InsiderActivityStrategy().screen(shared, '2026-10-22', {'min_sell_threshold': 2})[0]
    assert universe.decision('AAPL') == 'eligible'
    with pytest.raises(ValueError, match='invalid_form4'):
        form4_analysis_inputs(candidate, shared, universe)


def test_synthetic_qualified_buy_has_economic_count_unknown_venue_and_no_venue_bonus():
    """Synthetic purchases exercise the positive consumer path, not official facts."""
    import base64
    import xml.etree.ElementTree as ET
    root = ET.fromstring(base64.b64decode(original()['original_base64']))
    for transaction in root.findall('.//nonDerivativeTransaction'):
        if transaction.findtext('transactionCoding/transactionCode') == 'S':
            transaction.find('transactionCoding/transactionCode').text = 'P'
            transaction.find('transactionAmounts/transactionAcquiredDisposedCode/value').text = 'A'
    document = original(raw=ET.tostring(root))
    qualified = qualify_form4_window([document], security_bindings=bindings(document),
        date_from='2026-10-01', date_to='2026-10-22')
    shared = {'edgar': {'form4_qualification': qualified}}
    candidate = InsiderActivityStrategy().screen(shared, '2026-10-22', {'min_cluster_size': 1})[0]
    assert candidate.metadata['cluster_type'] == 'buy_cluster'
    assert candidate.score == pytest.approx(.15)  # One officer, no venue premium.
    assert candidate.metadata['economic_purchase_count'] == 5
    assert candidate.metadata['exchange_execution_status'] == 'unknown'
    assert 'open_market_buys' not in candidate.metadata
    assert candidate.journal_only
    context = form4_analysis_inputs(candidate, shared)
    request = prepare_form4_request(context, ticker='AAPL', cluster_type='buy_cluster')
    assert len(json.loads(request['user'])['documents'][0]['transactions']) == 11
    assert len(context['selected_transaction_ids']) == 5
    legacy_rows = [dict(row, open_market=True) for row in qualified['admitted_transactions']]
    legacy = InsiderActivityStrategy().screen({'edgar': {'form4': {'AAPL': legacy_rows}}},
        '2026-10-22', {'min_cluster_size': 1})[0]
    assert legacy.score == pytest.approx(.3)
    assert legacy.metadata['open_market_buys'] == 5
    assert 'economic_purchase_count' not in legacy.metadata
