"""Durable containment must not be inferred from a historical issue receipt."""
import pytest

from test_candidate_input_containment import forbid_network
from test_shared_policy_volatility_evidence import SESSION, _history, _run_staging_matrix


@pytest.mark.parametrize('damage', ['missing', 'corrupt', 'stale_attempt'])
def test_generic_skip_replay_requires_durable_acceptance(tmp_path, monkeypatch, damage):
    owner, results, calls = _run_staging_matrix(
        tmp_path, monkeypatch,
        {'OPEN': _history(.021), 'PENDING': _history(.027),
         'NCL': _history(.033), 'UI': _history(.031).iloc[:0]},
        signal_tickers=('NCL', 'UI'))
    try:
        assert all(row['staging_valid'] for row in results.values())
        conn = owner.cohorts[0]['ledger']._connection
        if damage in {'missing', 'stale_attempt'}:
            conn.execute("DELETE FROM schema_metadata WHERE metadata_key LIKE 'candidate_containment:v1:%'")
            if damage == 'stale_attempt':
                conn.execute('DELETE FROM staging_runs')
        else:
            conn.execute("UPDATE schema_metadata SET metadata_value='{}' WHERE metadata_key LIKE 'candidate_containment:v1:%'")
        conn.commit()
        before = list(calls)
        replay = owner.run_daily(SESSION.isoformat())
        assert calls == before
        assert any(not row['staging_valid'] for row in replay.values())
        from tradingagents.strategies.orchestration.operational_report import _candidate_acceptance
        issues = owner._metric_store.read_session_candidate_input_issues(owner._epoch_id, SESSION)
        diagnostics = []
        assert not _candidate_acceptance(tmp_path, owner.cohorts[0]['config'].name,
                                         issues[0].reference(), lambda *args: diagnostics.append(args))
        assert diagnostics
        assert all('candidate_input_acceptance' in row for row in results.values())
        from copy import deepcopy
        from tradingagents.strategies.orchestration.generation_manager import _valid_daily_cohort_results
        assert _valid_daily_cohort_results(results, SESSION.isoformat())
        unproven = deepcopy(results)
        for row in unproven.values():
            row.pop('candidate_input_acceptance')
        assert not _valid_daily_cohort_results(unproven, SESSION.isoformat())
    finally:
        for cohort in owner.cohorts:
            cohort['ledger'].close()


def _insert_obligation(conn, *, status='pending', side='sell', ticker='UI',
                       created='2026-07-30T20:00:00+00:00', event='historical-other'):
    conn.execute("INSERT INTO signals VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 ('excluded-historical-signal', 'old-epoch', 'old-policy', event,
                  'fixture_strategy', ticker, 'long', None,
                  '2026-07-30T19:00:00+00:00', '2026-07-30', '100',
                  '2026-07-30T19:00:00+00:00', 'fixture'))
    conn.execute("INSERT INTO order_intents VALUES (?,?,?,?,?,?,?,?,?,?)",
                 ('excluded-intent', 'horizon_30d_size_5k', side, 1, created,
                  '2026-08-03', 'next_session_open', status, None, 'broker-order'))
    conn.execute("INSERT INTO intent_signals VALUES (?,?,?,?)",
                 ('horizon_30d_size_5k', 'excluded-intent', 'excluded-historical-signal', 0))
    conn.commit()


@pytest.mark.parametrize('status,side,created,event,blocked', [
    ('pending', 'sell', '2026-07-30T20:00:00+00:00', 'historical-other', True),
    ('pending', 'cover', '2026-07-30T20:00:00+00:00', 'historical-other', True),
    ('cancelled', 'sell', '2026-07-31T20:00:00+00:00', 'historical-other', True),
    ('filled', 'buy', '2026-07-30T20:00:00+00:00', 'event-ui', True),
    ('filled', 'sell', '2026-07-30T20:00:00+00:00', 'historical-other', False),
    ('unlinked', 'cover', '2026-07-30T20:00:00+00:00', 'historical-other', True),
    ('filled', 'sell', 'not-a-timestamp', 'historical-other', True),
    ('session_fill', 'sell', '2026-07-30T20:00:00+00:00', 'historical-other', True),
])
def test_excluded_ticker_obligations_checked_before_any_staging(
        tmp_path, monkeypatch, status, side, created, event, blocked):
    from tradingagents.strategies.orchestration import daily_pipeline
    original = daily_pipeline._finish_volatility_validation
    def inject(state):
        original(state)
        conn = state.owner.cohorts[0]['ledger']._connection
        _insert_obligation(conn, status=('pending' if status == 'unlinked' else
                                        'filled' if status == 'session_fill' else status),
                           side=side, created=created, event=event)
        if status == 'session_fill':
            conn.execute('INSERT INTO fills (fill_id,cohort_id,intent_id,side,session,effective_at,processed_at,reference_price,fill_price,quantity,slippage,commission,other_fees) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                         ('excluded-fill', state.owner.cohorts[0]['config'].name, 'excluded-intent',
                          side, SESSION.isoformat(), SESSION.isoformat()+'T20:00:00+00:00',
                          SESSION.isoformat()+'T20:00:00+00:00', '100', '100', 1, '0', '0', '0'))
            conn.commit()
        if status == 'unlinked':
            conn.execute("DELETE FROM intent_signals WHERE intent_id='excluded-intent'")
            conn.commit()
    monkeypatch.setattr(daily_pipeline, '_finish_volatility_validation', inject)
    staged = {}
    owner, results, _ = _run_staging_matrix(
        tmp_path, monkeypatch,
        {'OPEN': _history(.021), 'PENDING': _history(.027),
         'NCL': _history(.033), 'UI': _history(.031).iloc[:0]},
        signal_tickers=('NCL', 'UI'), stage_observations=staged)
    try:
        assert bool(staged) is not blocked
        assert all(row['staging_valid'] is not blocked for row in results.values())
    finally:
        for cohort in owner.cohorts:
            cohort['ledger'].close()


def test_durable_obligation_invalidates_existing_proof_on_replay_and_report(tmp_path, monkeypatch):
    from tradingagents.strategies.orchestration.operational_report import _candidate_acceptance
    owner, results, calls = _run_staging_matrix(
        tmp_path, monkeypatch,
        {'OPEN': _history(.021), 'PENDING': _history(.027),
         'NCL': _history(.033), 'UI': _history(.031).iloc[:0]},
        signal_tickers=('NCL', 'UI'))
    try:
        assert all(row['staging_valid'] for row in results.values())
        issue = owner._metric_store.read_session_candidate_input_issues(owner._epoch_id, SESSION)[0]
        name = owner.cohorts[0]['config'].name
        assert _candidate_acceptance(tmp_path, name, issue.reference(), lambda *_: None)
        _insert_obligation(owner.cohorts[0]['ledger']._connection)
        before = list(calls)
        replay = owner.run_daily(SESSION.isoformat())
        assert calls == before
        assert all(not row['staging_valid'] for row in replay.values())
        assert not _candidate_acceptance(tmp_path, name, issue.reference(), lambda *_: None)
    finally:
        for cohort in owner.cohorts:
            cohort['ledger'].close()


def test_short_history_without_acceptance_proof_fails_closed(tmp_path, monkeypatch):
    from tradingagents.strategies.orchestration.operational_report import _candidate_acceptance
    owner, results, _ = _run_staging_matrix(
        tmp_path, monkeypatch,
        {'OPEN': _history(.021), 'PENDING': _history(.027),
         'NCL': _history(.033), 'UI': _history(.031).tail(12)},
        signal_tickers=('NCL', 'UI'))
    try:
        assert all(row['staging_valid'] for row in results.values())
        issue = owner._metric_store.read_session_candidate_input_issues(owner._epoch_id, SESSION)[0]
        assert issue.reason_code == 'insufficient_history'
        conn = owner.cohorts[0]['ledger']._connection
        conn.execute("DELETE FROM schema_metadata WHERE metadata_key LIKE 'candidate_containment:v1:%'")
        conn.commit()
        replay = owner.run_daily(SESSION.isoformat())
        assert all(not row['staging_valid'] for row in replay.values())
        assert not _candidate_acceptance(tmp_path, owner.cohorts[0]['config'].name,
                                     issue.reference(), lambda *_: None)
    finally:
        for cohort in owner.cohorts:
            cohort['ledger'].close()
