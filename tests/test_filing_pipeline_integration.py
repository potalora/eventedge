"""The opt-in filing route discovers every scope before one shared hydration."""
from tradingagents.strategies.data_sources.evidence import CoverageRecords


def test_engine_gathers_all_scopes_before_one_hydration(tmp_path, monkeypatch):
    from test_source_inputs import _engine
    from tradingagents.strategies.learning.event_monitor import EventMonitor
    events = []
    def poll(self, forms, *args, fetch_text=True, **kwargs):
        assert self.filing_policy == 'complete_submission_v1'
        assert fetch_text is False
        events.append(('discover', tuple(forms)))
        return CoverageRecords([{'accession_number': str(len(events))}], coverage={'complete': True})
    def hydrate(self, collections, **kwargs):
        assert len(events) == 4
        assert set(collections) == {'filings', 'activist_13d', 'passive_13g', 'pqc_filings'}
        assert all(rows.coverage['complete'] for rows in collections.values())
        events.append(('hydrate',))
        return {'collections': {key: [{'filing_evidence_ref': key}] for key in collections},
                'corpus': {'full': {'text': 'unclipped'}}, 'coverage': {'complete': True}}
    monkeypatch.setattr(EventMonitor, 'poll_edgar_filings', poll)
    monkeypatch.setattr(EventMonitor, 'poll_keyword_filings', poll)
    monkeypatch.setattr(EventMonitor, 'poll_form4_filings', lambda *a, **k: {})
    monkeypatch.setattr(EventMonitor, 'hydrate_collections', hydrate)
    engine = _engine(tmp_path)
    engine.ar_config['filing_evidence_policy'] = 'complete_submission_v1'
    result = engine._fetch_edgar_events('2026-10-09')
    assert len(events) == 5
    assert result['filing_evidence']['corpus']['full']['text'] == 'unclipped'
    assert 'collections' not in result['filing_evidence']
    assert result['filings'] == [{'filing_evidence_ref': 'filings'}]


def test_incomplete_full_corpus_is_never_successful_source(tmp_path, monkeypatch):
    from test_source_inputs import _engine
    from tradingagents.strategies.learning.event_monitor import EventMonitor
    from tradingagents.strategies.orchestration.source_inputs import successful_source
    monkeypatch.setattr(EventMonitor, 'poll_edgar_filings', lambda *a, **k: [])
    monkeypatch.setattr(EventMonitor, 'poll_keyword_filings', lambda *a, **k: [])
    monkeypatch.setattr(EventMonitor, 'poll_form4_filings', lambda *a, **k: {})
    monkeypatch.setattr(EventMonitor, 'hydrate_collections', lambda *a, **k: {
        'collections': {}, 'corpus': {}, 'coverage': {'complete': False}})
    engine = _engine(tmp_path)
    engine.ar_config['filing_evidence_policy'] = 'complete_submission_v1'
    result = engine._fetch_edgar_events('2026-10-09')
    assert result['filing_evidence']['coverage']['complete'] is False
    assert not successful_source(result)
