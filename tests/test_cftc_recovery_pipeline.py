"""An optional provider may recover next session without rewriting history."""
from datetime import date, datetime, timezone

from test_source_reliability_pipeline import (
    SESSION, accepted_input_bytes, pipeline, run_campaign,
)
from tradingagents.strategies.data_sources.cftc_source import CFTCSource
from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError


def test_cftc_failure_recovers_next_session_without_revising_accepted_evidence(pipeline, monkeypatch):
    fixture, owner, config, _ = pipeline
    config['autoresearch'].update(filing_research_policy='disabled_v1',
                                 research_resilience_policy='dependency_isolation_v1')
    original = CFTCSource._fetch_raw_report
    failed = True

    def acquire(self, *args, **kwargs):
        if failed:
            raise SourceFetchError('CFTC unavailable', reason_code='http_error', http_status=403)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(CFTCSource, '_fetch_raw_report', acquire)
    first, wire, report = run_campaign(pipeline)
    assert first['outcome'] == 'degraded' and not first['input_coverage_valid']
    assert report['accounting_valid'] and report['staging_complete']
    assert all(row['execution_valid'] and row['staging_valid'] for row in wire.values())
    health = owner._metric_store.read_strategy_health(session=SESSION)
    commodity = [row for row in health if row.strategy == 'commodity_macro']
    assert len(commodity) == 4 and all(row.status == 'data_failure' for row in commodity)
    assert any(row.status == 'signals' for row in health if row.strategy == 'earnings_call')
    first_inputs = accepted_input_bytes(config)
    assert first_inputs

    failed = False
    fixture.session = date(2026, 10, 5)
    fixture.now = datetime(2026, 10, 5, 22, tzinfo=timezone.utc)
    second, second_wire, second_report = run_campaign(pipeline, fixture.session)
    assert second['outcome'] == 'degraded'  # EDGAR stays deliberately disabled.
    assert second_report['accounting_valid'] and second_report['staging_complete']
    assert all(row['execution_valid'] and row['staging_valid'] for row in second_wire.values())
    recovered = [row for row in owner._metric_store.read_strategy_health(session=fixture.session)
                 if row.strategy == 'commodity_macro']
    assert len(recovered) == 4
    assert all(row.status in {'signals', 'legitimate_no_event'} for row in recovered)
    second_inputs = accepted_input_bytes(config)
    assert all(second_inputs[name] == data for name, data in first_inputs.items())

    def ledgers():
        return {cohort['config'].name: tuple(cohort['ledger']._connection.iterdump())
                for cohort in owner.cohorts}

    before_replay = ledgers()
    calls, models = fixture.calls.copy(), fixture.model_calls.copy()
    fixture.block_all = True
    replay, _, replay_report = run_campaign(pipeline, fixture.session)
    assert replay['outcome'] == 'degraded'
    assert replay_report['accounting_valid'] and replay_report['staging_complete']
    assert accepted_input_bytes(config) == second_inputs
    assert ledgers() == before_replay
    assert fixture.calls == calls and fixture.model_calls == models
    assert not fixture.blocked_calls
