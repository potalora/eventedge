"""Frozen source observations, not candidate display strings, bind assessments."""
from copy import deepcopy

import pytest

from test_filing_hydration import Source, row, hydrate, history_row
from test_filing_assessment import prepared
from tradingagents.strategies.modules.base import Candidate
from tradingagents.strategies.data_sources.equity_universe import EquityUniverse, normalize_assets
from test_source_inputs import NOW


def bundle(*, annual=False):
    current = row(1, '10-K' if annual else '8-K')
    prior = row(2, '10-K', '2025-09-30')
    company = {'0': {'cik_str': 1, 'ticker': 'ABC'}}
    snapshot = normalize_assets([{'class': 'us_equity', 'symbol': 'ABC', 'exchange': 'NASDAQ',
                                 'status': 'active', 'tradable': True}], observed_at=NOW, response_sha256='f'*64)
    universe = EquityUniverse(snapshot, company_map=company)
    source = Source([current, prior], {'0000000001': {'filings': [history_row(prior)], 'archives': []}})
    graph = hydrate(source, {'filings': [current]}, equity_universe=universe, company_map=company)
    collections = graph.pop('collections')
    data = {'edgar': {**collections, 'filing_evidence': graph, 'company_tickers': company},
            'equity_universe': {'snapshot': snapshot}}
    accepted = collections['filings'][0]
    candidate = Candidate(ticker='ABC', date='2026-10-09', direction='long', score=.5,
        metadata={'analysis_type': 'filing_change' if annual else 'material_event',
                  'accession_number': current['adsh'], 'full_filing_evidence_policy': 'complete_submission_v1',
                  **{key: accepted[key] for key in ('filing_evidence_ref', 'prior_evidence_ref', 'comparison_binding') if key in accepted}})
    return data, universe, candidate


def inputs(data, universe, candidate):
    from tradingagents.strategies.orchestration.filing_inputs import filing_analysis_inputs
    return filing_analysis_inputs(candidate, data, universe)


def test_inputs_use_whole_frozen_corpus_and_real_target_binding():
    data, universe, candidate = bundle()
    candidate.metadata['current_text'] = 'untrusted candidate excerpt'
    result = inputs(data, universe, candidate)
    assert result['current_evidence']['units'][0]['text'].endswith('COMPLETE END')
    assert len(result['current_evidence']['units'][0]['text']) > 5000
    assert result['target_binding']['ticker'] == 'ABC'
    assert result['issuer_binding']['issuers'] == {candidate.metadata['filing_evidence_ref']: '0000000001'}


def test_display_ticker_cannot_replace_actual_filing_issuer():
    data, universe, candidate = bundle()
    candidate.ticker = 'SPY'
    with pytest.raises(ValueError, match='invalid_filing_target'):
        inputs(data, universe, candidate)


@pytest.mark.parametrize('second_symbol,assets', [
    ('BBB', [('BBB', 'NASDAQ'), ('BBB', 'NASDAQ')]),
    ('BBB-A', [('BBB.A', 'NASDAQ')]),
])
def test_one_eligible_class_does_not_hide_an_ambiguous_issuer_security(second_symbol, assets):
    data, _, candidate = bundle()
    company = {'0': {'cik_str': 1, 'ticker': 'ABC'}, '1': {'cik_str': 1, 'ticker': second_symbol}}
    snapshot = normalize_assets([{'class': 'us_equity', 'symbol': symbol, 'exchange': exchange,
        'status': 'active', 'tradable': True} for symbol, exchange in [('ABC', 'NASDAQ'), *assets]],
        observed_at=NOW, response_sha256='f'*64)
    universe = EquityUniverse(snapshot, company_map=company)
    data['edgar']['company_tickers'] = company
    current = row()
    graph = hydrate(Source([current]), {'filings': [current]}, equity_universe=universe, company_map=company)
    assert graph['collections']['filings'][0]['issuer_binding']['execution_status'] == 'unresolved'
    with pytest.raises(ValueError, match='invalid_filing_target'):
        inputs(data, universe, candidate)


def test_prior_proof_must_match_frozen_history_and_selected_accession():
    data, universe, candidate = bundle(annual=True)
    assert inputs(data, universe, candidate)['prior_evidence']['filing_date'] == '2025-09-30'
    data['edgar']['filing_evidence']['history_corpus']['0000000001']['filings'][0]['filing_date'] = '2024-09-30'
    with pytest.raises(ValueError, match='invalid_filing_comparator'):
        inputs(data, universe, candidate)


def test_missing_current_ref_cannot_fall_back_to_excerpt():
    data, universe, candidate = bundle()
    data['edgar']['filing_evidence']['corpus'].clear()
    candidate.metadata['current_text'] = 'complete-looking excerpt'
    with pytest.raises(ValueError, match='invalid_filing_source'):
        inputs(data, universe, candidate)


def test_pqc_uses_every_ref_and_news_observation_without_narrative_metadata():
    data, universe, candidate = bundle()
    data['edgar']['pqc_filings'] = data['edgar']['filings']
    data['finnhub'] = {'pqc_news': [{'id': '42', 'source': 'wire', 'headline': 'Post-quantum',
        'summary': 'Full news end', 'url': 'https://example.test/42', 'observed_at': NOW.isoformat()}]}
    candidate.metadata.update(analysis_type='quantum_readiness',
        filing_evidence_refs=[candidate.metadata['filing_evidence_ref']], news_evidence_refs=['FINNHUB:42'])
    result = inputs(data, universe, candidate)
    assert len(result['current_evidence']) == 1
    assert result['news_evidence'][0]['text'].endswith('Full news end')
    assert result['news_evidence'][0]['observed_at'] == NOW.isoformat()
    candidate.metadata['filing_evidence_refs'] = []
    with pytest.raises(ValueError, match='invalid_filing_source'):
        inputs(data, universe, candidate)


def test_same_news_acquired_in_two_queries_keeps_one_real_observation():
    data, universe, candidate = bundle()
    data['edgar']['pqc_filings'] = []
    article = {'id': '42', 'headline': 'Post-quantum', 'summary': 'Same content',
               'url': 'https://example.test/42', 'observed_at': '2026-10-06T20:30:00+00:00'}
    data['finnhub'] = {'pqc_news': [article, dict(article, observed_at='2026-10-06T20:31:00+00:00')]}
    candidate.metadata.update(analysis_type='quantum_readiness', filing_evidence_refs=[], news_evidence_refs=['FINNHUB:42'])
    result = inputs(data, universe, candidate)
    assert len(result['news_evidence']) == 1
    assert result['news_evidence'][0]['observed_at'] == article['observed_at']
    data['finnhub']['pqc_news'][1]['summary'] = 'Changed content under same identity'
    with pytest.raises(ValueError, match='invalid_filing_source'):
        inputs(data, universe, candidate)


@pytest.mark.parametrize('locators,expected', [
    ({'article_id': 0, 'id': 'different', 'url': 'https://example.test/0'}, '0'),
    ({'article_id': True, 'id': 42, 'url': 'https://example.test/42'}, '42'),
    ({'article_id': {'bad': 'id'}, 'id': [], 'url': 'https://example.test/fallback'}, 'https://example.test/fallback'),
    ({'article_id': '  ', 'id': 0, 'url': 'https://example.test/0'}, '0'),
])
def test_news_locator_uses_first_strict_identity_including_zero(locators, expected):
    import hashlib
    from tradingagents.strategies.orchestration.filing_inputs import _news
    article = {'headline': '  Full headline\n', 'summary': 'Untrimmed summary  ',
               'observed_at': NOW.isoformat(), **locators}
    original = deepcopy(article)
    result = _news([article], ['FINNHUB:' + expected])
    assert len(result) == 1
    assert result[0]['source_id'] == expected
    assert result[0]['text'] == '  Full headline\n Untrimmed summary  '
    assert result[0]['text_sha256'] == hashlib.sha256(b'  Full headline\n Untrimmed summary  ').hexdigest()
    assert result[0]['observed_at'] == NOW.isoformat()
    assert article == original


@pytest.mark.parametrize('locators', [
    {}, {'article_id': True}, {'article_id': {}}, {'article_id': []},
    {'article_id': ' \n ', 'id': False, 'url': {'bad': 'url'}},
])
def test_news_all_invalid_locators_fail_without_synthetic_identity(locators):
    from tradingagents.strategies.orchestration.filing_inputs import _news
    article = {'headline': 'Full headline', 'summary': 'Full summary', **locators}
    with pytest.raises(ValueError, match='^invalid_filing_source$'):
        _news([article], [])


@pytest.mark.parametrize('field', ['headline', 'summary'])
@pytest.mark.parametrize('invalid', [None, 123, True, [], {'text': 'not a source string'}])
def test_news_malformed_text_is_not_coerced_into_model_evidence(field, invalid):
    from tradingagents.strategies.orchestration.filing_inputs import _news
    article = {'article_id': 'native-id', 'headline': 'Full headline', 'summary': 'Full summary',
               'observed_at': NOW.isoformat(), 'url': 'https://example.test/native-id', field: invalid}
    original = deepcopy(article)
    with pytest.raises(ValueError, match='^invalid_filing_source$'):
        _news([article], ['FINNHUB:native-id'])
    assert article == original


def test_unresolved_security_keeps_document_binding_but_no_target_attestation():
    data, universe, candidate = bundle()
    candidate.ticker = ''
    result = inputs(data, universe, candidate)
    assert result['issuer_binding']['status'] == 'verified'
    assert result['target_binding'] is None


@pytest.mark.parametrize('ticker,direction', [('ABC', 'long'), ('', 'neutral')])
def test_engine_full_evidence_route_keeps_document_and_security_status_separate(tmp_path, monkeypatch, ticker, direction):
    import json
    from test_source_inputs import _engine
    from test_filing_assessment import response, module
    from tradingagents.strategies.runtime_deadline import model_budget
    from tradingagents.strategies.learning.llm_analyzer import LLMAnalyzer
    data, universe, candidate = bundle()
    candidate.ticker = ticker
    candidate.metadata['needs_llm_analysis'] = True
    engine = _engine(tmp_path)
    engine._analyzer = LLMAnalyzer()
    engine.ar_config['filing_evidence_policy'] = 'complete_submission_v1'
    arguments = inputs(data, universe, candidate)
    request = module().prepare_request('material_event', **arguments)
    calls = []
    def model(system, user, **kwargs):
        calls.append(user)
        return json.dumps(response(request, direction=direction))
    monkeypatch.setattr(engine._analyzer, '_call_llm', model)
    with model_budget(1e12):
        engine._analyze_candidate(candidate, 'filing_analysis', None, engine._analyzer,
                                  filing_data=data, universe=universe)
    assert len(calls) == 1 and 'COMPLETE END' in calls[0]
    assert candidate.metadata['analysis_status'] == 'validated'
    assert candidate.direction == direction and candidate.ticker == ticker
    assert candidate.metadata['llm_analysis']['source_provenance']['corpus_sha256']
    if not ticker:
        assert candidate.journal_only is True
        assert candidate.metadata['non_actionable_reason'] == 'equity_universe_unresolved'
        assert candidate.metadata['document_assessment'] == 'assessed_no_directional_thesis'


@pytest.mark.parametrize('sufficient', [True, False])
def test_blank_security_assessment_survives_health_persistence(tmp_path, monkeypatch, sufficient):
    import json
    from tradingagents.strategies.modules.filing_analysis import FilingAnalysisStrategy
    from tradingagents.strategies.learning.llm_analyzer import LLMAnalyzer
    from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine
    from tradingagents.strategies.metrics.store import MetricStore
    from tradingagents.strategies.data_sources.equity_universe import POLICY
    data, _, _ = bundle()
    data['edgar']['company_tickers'] = {'0': {'cik_str': 2, 'ticker': 'ABC'}}
    source = data['edgar']['filings'][0]
    source['ticker'] = 'UNTRUSTEDDISPLAY'
    source['issuer_binding'].update(execution_status='unresolved', eligible_symbols=[])
    source['issuer_binding'].pop('ticker', None)
    data['yfinance'] = {}
    engine = MultiStrategyEngine(config={'autoresearch': {'state_dir': str(tmp_path),
        'equity_universe_policy': POLICY, 'filing_evidence_policy': 'complete_submission_v1'}},
        strategies=[FilingAnalysisStrategy()])
    engine._analyzer = LLMAnalyzer()
    monkeypatch.setattr(engine, '_build_regime_model', lambda data: {})
    calls = []
    def model(system, user, **kwargs):
        payload = json.loads(user)
        calls.append(payload)
        return json.dumps({'contract_version': 'filing-assessment-v1',
            'filing_evidence_status': 'sufficient' if sufficient else 'insufficient',
            'issuer_cik': '0000000001', 'direction': 'neutral' if sufficient else None,
            'conviction': .7 if sufficient else None, 'rationale': 'Source assessment retained.',
            'evidence_claim': 'Full narrative disclosed.', 'unresolved_material_dependencies': [],
            'citations': [{'unit_id': unit['unit_id'], 'quote': unit['text'][-30:]}
                          for item in payload['current'] for unit in item['units']]})
    monkeypatch.setattr(engine._analyzer, '_call_llm', model)
    signals, _, health = engine.screen_and_enrich('2026-10-09', data, epoch_id='epoch', policy_id='policy')
    assert calls and signals == []
    assert health[0].status == 'data_failure'
    store = MetricStore(tmp_path/'metrics.db')
    store.save_strategy_health(health[0])
    reloaded = store.load_strategy_health(health[0].health_id)
    records = reloaded.evidence['filing_assessments']
    assert len(records) == reloaded.evidence['admitted_count'] == 1
    assert records[0]['ticker'] == ''
    assert records[0]['analysis_status'] == ('validated' if sufficient else 'failed')
    assert records[0]['assessment']['filing_evidence_status'] == ('sufficient' if sufficient else 'insufficient')
    assert records[0]['assessment']['source_provenance']['corpus_sha256']
    assert 'Source assessment retained.' in json.dumps(records)
    assert source['issuer_binding']['submission_sha256'] in json.dumps(reloaded.evidence)
