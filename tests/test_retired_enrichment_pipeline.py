"""A new capability scope preserves real sixteen-book decisions and replay."""
from copy import deepcopy

import pytest

from test_source_reliability_pipeline import pipeline, accepted_input_bytes
from test_retired_enrichment_workflow import POLICY


@pytest.mark.parametrize('failure', [None, 'profiles', 'provider'])
def test_declared_retirement_only_accepts_supported_enrichment(pipeline, monkeypatch, failure):
    from test_research_hold_staging import _enable, _run
    from tradingagents.strategies.orchestration.enrichment_capabilities import declaration
    from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
    fixture, owner, config = _enable(pipeline)
    config['autoresearch'].update(deepcopy(POLICY))
    fixture.fault = 'edgar_partial'
    original = {**declaration(config['autoresearch'], ['NG', 'SI']),
                'profiles': {'AAPL': {'sector': 'Technology'}}}
    if failure == 'profiles':
        original['errors'] = {'profiles': {'AAPL': {'reason_code': 'timeout'}}}
    def fetch(self, _):
        if failure == 'provider':
            raise SourceFetchError('offline provider', reason_code='timeout')
        return deepcopy(original)
    monkeypatch.setattr(type(owner), '_fetch_openbb_enrichment', fetch)
    result, wire, _ = _run(pipeline)
    assert len(wire) == 16 and all(not row['error'] and row['staging_valid'] for row in wire.values()), wire
    for row in wire.values():
        status = row['committee_decision_status']
        assert status['model_coverage'] == {'complete': True}
        assert status['degraded'] is bool(failure)
        scope = status['research_enrichment_scope']
        assert scope['supported_enrichment_complete'] is (failure is None)
        assert scope['research_coverage_complete'] is False
        assert bool(scope['excluded']) is bool(failure)
    frozen = accepted_input_bytes(config)
    calls, models = fixture.calls.copy(), fixture.model_calls.copy()
    fixture.block_all = True
    monkeypatch.setattr(type(owner), '_fetch_openbb_enrichment', lambda *_: pytest.fail('accepted enrichment must replay'))
    repeated, repeated_wire, _ = _run(pipeline)
    assert repeated['outcome'] == result['outcome']
    assert {name: row['committee_decision_status'] for name, row in repeated_wire.items()} == {
        name: row['committee_decision_status'] for name, row in wire.items()}
    assert accepted_input_bytes(config) == frozen
    assert fixture.calls == calls and fixture.model_calls == models and not fixture.blocked_calls
