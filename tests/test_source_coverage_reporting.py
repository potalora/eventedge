from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from scripts.run_cohorts import _cohort_run_exit_status
from tradingagents.strategies.metrics.health import classify_strategy_run
from tradingagents.strategies.orchestration.daily_pipeline import DailyRunState, summarize_cohort_results

SESSION = date(2026, 10, 6)
EPOCH = 'gen_018-2026-10-06-' + 'a' * 16


def state_with_health(*, failed=False, missing=False):
    strategies = frozenset({'sec_filings', 'weather_ag'})
    cohorts = [
        {'config': SimpleNamespace(name=f'book-{horizon}', horizon=horizon)}
        for horizon in ('30d', '90d')
    ]
    rows = [
        classify_strategy_run(
            epoch_id=EPOCH, session=SESSION, policy_id=f'foundation-{horizon}',
            strategy=strategy, data_sources=('edgar',) if strategy == 'sec_filings' else ('noaa',),
            candidates=[], provider_errors={'edgar': 'secret must never enter result'}
            if failed and strategy == 'sec_filings' and horizon == '30d' else {}, exception=None,
        )
        for horizon in ('30d', '90d') for strategy in sorted(strategies)
        if not (missing and horizon == '30d' and strategy == 'sec_filings')
    ]
    def read_health(epoch, *, session, limit=1000):
        assert (epoch, session) == (EPOCH, SESSION)
        return rows
    owner = SimpleNamespace(
        _active_strategy_names=strategies, cohorts=cohorts,
        _policy_id_for_horizon=lambda horizon: f'foundation-{horizon}',
        _metric_store=SimpleNamespace(read_strategy_health=read_health, read_session_candidate_input_issues=lambda *args: []),
    )
    state = DailyRunState(owner, SESSION.isoformat(), SESSION, datetime(2026, 10, 6, 22, tzinfo=timezone.utc), epoch_id=EPOCH)
    results = {cohort['config'].name: {'error': False, 'execution_valid': True, 'staging_valid': True} for cohort in cohorts}
    return state, results


def test_durable_source_failure_degrades_only_affected_cohort_on_completed_resume():
    state, results = state_with_health(failed=True)
    # No in-memory horizon_signals: this is also the fully completed resume path.
    result = state.finalize(results)
    assert result['book-30d']['degraded'] is True
    assert result['book-30d']['execution_valid'] is True
    assert result['book-30d']['staging_valid'] is True
    assert result['book-30d']['input_coverage_valid'] is False
    assert result['book-90d']['input_coverage_valid'] is True
    assert result['book-90d']['degraded'] is False
    reference = result['book-30d']['source_health_failures'][0]
    assert reference['sources'] == ['edgar']
    assert reference['strategy'] == 'sec_filings'
    assert reference['affected_cohorts'] == ['book-30d']
    assert 'secret' not in str(result)
    summary = summarize_cohort_results(result, SESSION.isoformat())
    assert summary.outcome == 'degraded'
    assert summary.input_coverage_valid is False
    assert summary.degradation_label == 'source coverage incomplete'
    code, message = _cohort_run_exit_status(result, trading_date=SESSION.isoformat())
    assert code == 0
    assert 'source coverage incomplete' in message
    assert 'edgar' in message


def test_successful_empty_is_complete_coverage():
    state, results = state_with_health()
    result = state.finalize(results)
    assert all(item['input_coverage_valid'] for item in result.values())
    assert summarize_cohort_results(result, SESSION.isoformat()).outcome == 'clean'


def test_missing_health_cannot_be_clean():
    state, results = state_with_health(missing=True)
    result = state.finalize(results)
    assert result['book-30d']['input_coverage_valid'] is False
    assert result['book-30d']['source_health_failures'][0]['status'] == 'missing_health'
    assert summarize_cohort_results(result, SESSION.isoformat()).outcome == 'degraded'


def test_contradictory_source_health_carrier_is_rejected():
    state, results = state_with_health(failed=True)
    result = state.finalize(results)
    result['book-30d']['input_coverage_valid'] = True
    with pytest.raises(ValueError, match='source coverage'):
        summarize_cohort_results(result, SESSION.isoformat())


def test_generation_worker_preserves_coverage_failure(tmp_path):
    from test_cohort_failure_reporting import _clean_daily_results, _worker_stdout, _run_with_proc, _FakeProc
    from tradingagents.strategies.orchestration.generation_manager import _daily_history_entry
    state, small = state_with_health(failed=True)
    reference = state.finalize(small)['book-30d']['source_health_failures'][0]
    results = _clean_daily_results()
    for result in results.values():
        result['input_coverage_valid'] = True
        result['source_health_failures'] = []
    name = sorted(results)[0]
    reference['affected_cohorts'] = [name]
    reference['session'] = '2026-08-10'
    reference['epoch_id'] = 'gen_001-2026-08-10-' + 'b' * 16
    results[name].update(degraded=True, input_coverage_valid=False, source_health_failures=[reference])
    result = _run_with_proc(tmp_path, _FakeProc(0, _worker_stdout(results)))
    assert result['outcome'] == 'degraded'
    assert result['execution_valid'] is True
    assert result['input_coverage_valid'] is False
    assert result['source_health_failures'] == [reference]
    history = _daily_history_entry(result, '2026-08-10')
    assert history['source_health_failures'] == [reference]
    assert history['input_coverage_valid'] is False


def test_generation_rejects_missing_or_contradictory_coverage():
    from test_cohort_failure_reporting import _clean_daily_results
    from tradingagents.strategies.orchestration.generation_manager import _valid_daily_cohort_results
    results = _clean_daily_results()
    for result in results.values():
        result['input_coverage_valid'] = True
        result['source_health_failures'] = []
    assert _valid_daily_cohort_results(results, '2026-08-10')
    first = results[sorted(results)[0]]
    del first['input_coverage_valid']
    assert not _valid_daily_cohort_results(results, '2026-08-10')
    first['input_coverage_valid'] = False
    assert not _valid_daily_cohort_results(results, '2026-08-10')


def test_policy_disabled_strategy_is_explicitly_excluded_not_empty_or_failed():
    from dataclasses import replace
    state, results = state_with_health()
    original = state.owner._metric_store.read_strategy_health
    state.owner._disabled_strategies = {'weather_ag': 'unsupported_test_proxy'}
    state.owner._metric_store.read_strategy_health = lambda *args, **kwargs: [
        replace(row, status='disabled_by_policy', evidence={'reason': 'unsupported_test_proxy'})
        if row.strategy == 'weather_ag' else row
        for row in original(*args, **kwargs)
    ]
    result = state.finalize(results)
    assert all(row['input_coverage_valid'] for row in result.values())
    assert all(row['disabled_strategies'] == {'weather_ag': 'unsupported_test_proxy'} for row in result.values())
    # A disabled assertion not bound to the configured policy must fail closed.
    state.owner._disabled_strategies = {}
    with pytest.raises(ValueError, match='disabled'):
        state.finalize(results)
