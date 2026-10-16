"""Failed optional facts cannot influence committee or sizing context."""
from copy import deepcopy

import pytest
from test_source_reliability_pipeline import pipeline


def test_failed_optional_symbols_are_removed_and_originals_bound():
    from tradingagents.strategies.orchestration.research_resilience import project_enrichment, digest
    original = {'profiles': {'GOOD': {'sector': 'Technology'}, 'BAD': {'sector': 'Invented'}},
        'factors': {'invented': 10}, 'short_interest': {'BAD': {'short_float': 50}},
        'errors': {'profiles': {'BAD': {'reason_code': 'timeout'}},
                   'factors': {'reason_code': 'invalid_response'},
                   'short_interest': {'BAD': {'reason_code': 'provider_error'}}}}
    before = deepcopy(original)
    projected, receipt = project_enrichment(original)
    assert projected == {'profiles': {'GOOD': {'sector': 'Technology'}}, 'short_interest': {}}
    assert receipt['original_sha256'] == digest(original)
    assert receipt['projection_sha256'] == digest(projected)
    assert receipt['research_coverage_complete'] is False
    assert receipt['order_authority'] is False
    assert original == before


def test_failed_optional_payload_removed_even_without_error_index():
    from tradingagents.strategies.orchestration.research_resilience import project_enrichment
    original = {'profiles': {'BAD': {'sector': 'Invented', 'error': 'failed'}},
                'factors': {'error': 'failed', 'values': [999]}}
    projected, receipt = project_enrichment(original)
    assert projected == {'profiles': {}}
    assert len(receipt['excluded']) == 2


def test_whole_optional_failure_excludes_partial_facts():
    from tradingagents.strategies.orchestration.research_resilience import project_enrichment
    projected, receipt = project_enrichment({'profiles': {'BAD': {'sector': 'invented'}},
        'errors': {'openbb': {'reason_code': 'timeout'}}})
    assert projected == {}
    assert receipt['research_coverage_complete'] is False


def test_skipped_research_has_explicit_hold_no_model_facts():
    from tradingagents.strategies.orchestration.research_resilience import project_enrichment
    projected, receipt = project_enrichment({'research_hold_reasons': ['model_deadline_exhausted']})
    assert projected == {}
    assert receipt['hold_reasons'] == ['model_deadline_exhausted']
    assert receipt['research_coverage_complete'] is False


@pytest.mark.parametrize('value', [None, [], {'errors': []}, {'profiles': []}, {'research_hold_reasons': 'skip'}])
def test_malformed_enrichment_is_an_integrity_failure(value):
    from tradingagents.strategies.orchestration.research_resilience import project_enrichment
    with pytest.raises(ValueError, match='research_enrichment'):
        project_enrichment(value)


def test_failed_optional_facts_never_reach_any_of_16_committees(pipeline, monkeypatch):
    from test_research_hold_staging import _enable, _run
    from tradingagents.strategies.trading.portfolio_committee import PortfolioCommittee
    fixture, owner, _ = _enable(pipeline)
    fixture.fault = 'edgar_partial'
    poison = {'profiles': {'AAPL': {'sector': 'UNTRUSTED'}},
              'errors': {'profiles': {'AAPL': {'reason_code': 'timeout'}}}}
    monkeypatch.setattr(type(owner), '_fetch_openbb_enrichment', lambda self, _: deepcopy(poison))
    original, seen = PortfolioCommittee.synthesize, []
    def synthesize(self, *args, **kwargs):
        seen.append(deepcopy(kwargs['enrichment']))
        return original(self, *args, **kwargs)
    monkeypatch.setattr(PortfolioCommittee, 'synthesize', synthesize)
    result, wire, _ = _run(pipeline)
    assert not any(row['error'] for row in wire.values()), wire
    assert len(seen) == 16 and seen == [{'profiles': {}}] * 16
    assert all(row['committee_decision_status']['research_enrichment_scope']['excluded'] for row in wire.values())
    assert all(row['degraded'] for row in wire.values())
    fixture.block_all = True
    monkeypatch.setattr(type(owner), '_fetch_openbb_enrichment', lambda self, _: pytest.fail('must reuse frozen enrichment'))
    repeated, repeated_wire, _ = _run(pipeline)
    assert repeated['outcome'] == result['outcome']
    assert len(seen) == 16
    assert all(row['committee_decision_status']['research_enrichment_scope']['excluded'] for row in repeated_wire.values())


def test_deadline_expires_after_screening_preserves_all_16_books_exits(pipeline, monkeypatch):
    import time
    from test_research_hold_staging import _enable, _run, _seed_books, _assert_safe_hold
    from tradingagents.strategies.orchestration import daily_pipeline
    fixture, owner, _ = _enable(pipeline)
    fixture.fault = 'edgar_partial'
    seeded = _seed_books(owner)
    original = daily_pipeline.run_candidate_volatility_validation
    def expire_after_prices(state):
        result = original(state)
        assert result is None
        assert state.model_coverage['complete'] is True
        state.model_deadline = time.monotonic() - 1
        return result
    monkeypatch.setattr(daily_pipeline, 'run_candidate_volatility_validation', expire_after_prices)
    monkeypatch.setattr(type(owner), '_fetch_openbb_enrichment', lambda self, _: pytest.fail('deadline cannot start optional calls'))
    result, wire, _ = _run(pipeline)
    _assert_safe_hold(owner, seeded, result, wire)
    assert all(row['committee_decision_status']['research_authority']['hold_reasons'] == ['model_deadline_exhausted']
               for row in wire.values())
    fixture.block_all = True
    repeated, repeated_wire, _ = _run(pipeline)
    _assert_safe_hold(owner, seeded, repeated, repeated_wire)


@pytest.mark.parametrize('failure,contained', [('provider', True), ('internal', False)])
def test_only_typed_optional_provider_failures_are_contained(pipeline, monkeypatch, failure, contained):
    from test_research_hold_staging import _enable, _run
    from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
    fixture, owner, _ = _enable(pipeline)
    fixture.fault = 'edgar_partial'
    def failed(self, _):
        if failure == 'provider':
            raise SourceFetchError('offline provider', reason_code='timeout')
        raise RuntimeError('unknown internal defect')
    monkeypatch.setattr(type(owner), '_fetch_openbb_enrichment', failed)
    result, wire, _ = _run(pipeline)
    assert len(wire) == 16
    assert all(bool(row['error']) is not contained for row in wire.values()), wire
    if contained:
        assert all(row['degraded'] and row['staging_valid'] for row in wire.values())
    else:
        assert result['outcome'] == 'failed'


@pytest.mark.parametrize('receipt,field', [('research_authority', 'source_scope_sha256'),
    ('research_authority', 'projection_sha256'), ('research_authority', 'shared_context_sha256'),
    ('research_enrichment_scope', 'original_sha256')])
def test_completed_staging_checks_original_durable_research_receipts(pipeline, monkeypatch, receipt, field):
    import json
    from test_research_hold_staging import _enable, _run
    from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine
    fixture, _, _ = _enable(pipeline)
    fixture.fault = 'edgar_partial'
    original, captured = MultiStrategyEngine.screen_and_stage, []
    def remember(self, **kwargs):
        if not captured:
            captured.append((self, deepcopy(kwargs)))
        return original(self, **kwargs)
    monkeypatch.setattr(MultiStrategyEngine, 'screen_and_stage', remember)
    result, wire, _ = _run(pipeline)
    assert result['outcome'] == 'degraded' and all(not row['error'] for row in wire.values())
    engine, kwargs = captured[0]
    fixture.block_all = True
    assert original(engine, **kwargs)['replayed'] is True
    ledger = engine.ledger
    row = ledger._connection.execute('SELECT session,epoch_id,policy_id,payload_json FROM committee_decisions').fetchone()
    payload = json.loads(row['payload_json'])
    payload['status'][receipt][field] = 'f' * 64
    ledger._connection.execute('UPDATE committee_decisions SET payload_json=? WHERE session=? AND epoch_id=? AND policy_id=?',
        (json.dumps(payload, sort_keys=True, separators=(',', ':')), row['session'], row['epoch_id'], row['policy_id']))
    with pytest.raises(ValueError, match='research_.*conflict'):
        original(engine, **kwargs)
    assert not fixture.blocked_calls


def test_completed_daily_replay_checks_original_durable_source_authority(pipeline):
    import json
    from test_research_hold_staging import _enable, _run
    fixture, owner, _ = _enable(pipeline)
    fixture.fault = 'edgar_partial'
    result, wire, _ = _run(pipeline)
    assert result['outcome'] == 'degraded' and all(not row['error'] for row in wire.values())
    ledger = owner.cohorts[0]['ledger']
    row = ledger._connection.execute('SELECT session,epoch_id,policy_id,payload_json FROM committee_decisions').fetchone()
    payload = json.loads(row['payload_json'])
    payload['status']['research_authority']['source_scope_sha256'] = 'f' * 64
    ledger._connection.execute('UPDATE committee_decisions SET payload_json=? WHERE session=? AND epoch_id=? AND policy_id=?',
        (json.dumps(payload, sort_keys=True, separators=(',', ':')), row['session'], row['epoch_id'], row['policy_id']))
    fixture.block_all = True
    repeated, repeated_wire, _ = _run(pipeline)
    assert repeated['outcome'] == 'failed'
    assert all(row['error'] for row in repeated_wire.values())
    assert not fixture.blocked_calls
