"""Portable real-SQLite reader lifetime regressions; all state is synthetic."""
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import gc
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from scripts import recover_runtime as controller
from tradingagents.strategies.orchestration import runtime_recovery as recovery


@contextmanager
def observed_readers(monkeypatch):
    """Retain real handles so refcounting/GC cannot masquerade as close()."""
    connect = sqlite3.connect
    readers = []
    enabled = gc.isenabled()
    gc.disable()

    def tracked(*args, **kwargs):
        assert kwargs.get('uri') is True
        assert str(args[0]).endswith('?mode=ro')
        connection = connect(*args, **kwargs)
        readers.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, 'connect', tracked)
    try:
        yield readers
    finally:
        for connection in readers:
            connection.close()
        if enabled:
            gc.enable()


def assert_closed(readers, count):
    assert len(readers) == count
    for connection in readers:
        with pytest.raises(sqlite3.ProgrammingError, match='closed database'):
            connection.execute('SELECT 1')


@pytest.mark.parametrize('broken', [False, True], ids=['sessions', 'sql-error'])
def test_discovery_releases_readers_before_gc(tmp_path, monkeypatch, broken):
    book = tmp_path / 'horizon_test' / 'portfolio.db'
    book.parent.mkdir()
    with closing(sqlite3.connect(book)) as connection, connection:
        connection.execute('CREATE TABLE session_execution_contexts(session TEXT, cohort_id TEXT)')
        if not broken:
            connection.execute('CREATE TABLE staging_runs(session TEXT, cohort_id TEXT)')
        connection.executemany('INSERT INTO session_execution_contexts VALUES (?, ?)', [
            ('2026-11-16', 'a'), ('2026-11-16', 'a'), ('2026-11-17', 'a'), ('2026-10-01', 'a')])
        if not broken:
            connection.execute("INSERT INTO staging_runs VALUES ('2026-11-17', 'a')")
    before = book.read_bytes()
    with observed_readers(monkeypatch) as readers:
        if broken:
            with pytest.raises(sqlite3.OperationalError, match='no such table: staging_runs'):
                controller._discover_sessions({'state_dir': str(tmp_path)}, datetime(2026, 11, 18, tzinfo=timezone.utc))
        else:
            assert controller._discover_sessions({'state_dir': str(tmp_path)}, datetime(2026, 11, 18, tzinfo=timezone.utc)) == ['2026-11-16']
        assert book.read_bytes() == before
        assert_closed(readers, 1)


SESSION = '2026-11-16'
EPOCH = 'synthetic-epoch'
GENERATION = 'gen_999'
ECONOMICS = ('fills', 'cash_events', 'marks', 'borrow_accruals', 'financing_accruals',
             'dividend_events', 'fee_events', 'benchmark_observations', 'session_runs', 'session_invalidations')
METRICS = ('strategy_health', 'candidate_bar_recoveries', 'governed_bar_recoveries',
           'candidate_signal_identity_bindings', 'candidate_input_issues')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def insert_rows(connection, table, rows):
    columns = tuple(rows[0])
    connection.execute('CREATE TABLE ' + table + ' (' + ', '.join(columns) + ')')
    connection.executemany('INSERT INTO ' + table + ' VALUES (' + ','.join('?' for _ in columns) + ')',
                           [tuple(row[column] for column in columns) for row in rows])


@pytest.fixture
def synthetic_proof(tmp_path):
    from tradingagents.strategies.orchestration.operational_report import EXPECTED_COHORTS
    from tradingagents.strategies.orchestration.session_executor import PHASES
    from tradingagents.strategies.orchestration.source_inputs import configuration_fingerprint

    expected = {'books': {}, 'accepted_inputs': {}, 'metrics': {}}
    for name in EXPECTED_COHORTS:
        path = tmp_path / name / 'portfolio.db'
        path.parent.mkdir()
        context = dict(session=SESSION, cohort_id=name, epoch_id=EPOCH, starting_state_digest='start',
                       input_digest='input', config_digest='config', borrow_digest='borrow', provenance_digest='provenance')
        staging = dict(session=SESSION, cohort_id=name, epoch_id=EPOCH, completed_at='done',
                       staging_run_id='stage-' + name, post_state_digest='staged')
        snapshot = dict(session=SESSION, epoch_id=EPOCH, valid=1, invalid_reason=None)
        committee = dict(session=SESSION, payload_json='{"synthetic":true}')
        phases = {}
        prior = 'start'
        for phase in PHASES:
            phases[phase] = dict(session=SESSION, phase=phase, cohort_id=name, completed_at='done',
                                 pre_state_digest=prior, post_state_digest=phase + '-post')
            prior = phase + '-post'
        economics = {}
        with closing(sqlite3.connect(path)) as connection, connection:
            for table, rows in [('session_execution_contexts', [context]), ('staging_runs', [staging]),
                                ('account_snapshots', [snapshot]), ('committee_decisions', [committee]),
                                ('session_phases', list(phases.values()))]:
                insert_rows(connection, table, rows)
            for table in ECONOMICS:
                key = 'cash_event_id' if table == 'cash_events' else 'id'
                row = {key: '1', 'session': SESSION, 'amount': '12.34'}
                rows = [row]
                if table == 'cash_events':
                    opening = {key: '0', 'session': None, 'amount': '100.00'}
                    rows = [opening, row]
                insert_rows(connection, table, rows)
                economics[table] = digest([row])
            economics['opening_cash_events'] = digest([opening])
        expected['books'][name] = dict(economics=economics, execution_input_digest='input', config_digest='config',
            borrow_digest='borrow', provenance_digest='provenance', context_digest=digest(context),
            phase_chain_digest=digest(phases), staging_run_id=staging['staging_run_id'],
            staging_post_state_digest='staged', staging_digest=digest(staging), snapshot_digest=digest(snapshot),
            committee_digest=digest(committee))
    with closing(sqlite3.connect(tmp_path / 'metrics_v2.sqlite3')) as connection, connection:
        for table in METRICS:
            row = dict(id='1', epoch_id=EPOCH, session=SESSION, payload='synthetic')
            insert_rows(connection, table, [row])
            expected['metrics'][table] = digest([row])
    slot = configuration_fingerprint({'generation': GENERATION, 'session': SESSION}) + '.json'
    for directory in ('source_inputs', 'source_inputs/screening', 'source_inputs/decision_inputs'):
        path = tmp_path / directory / slot
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'{"synthetic":true}')
        expected['accepted_inputs'][str(path.relative_to(tmp_path))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return tmp_path, expected, EXPECTED_COHORTS


@pytest.mark.parametrize('damage,error,opened', [
    ('none', None, 17),
    ('incomplete', 'recovery book incomplete', 1),
    ('identity', 'recovery book identity invalid', 1),
    ('phase', 'recovery phase proof invalid', 1),
    ('sql', 'no such table: fills', 1),
    ('economic-bound', 'recovery economic proof exceeds bounds', 1),
    ('opening-bound', 'recovery opening proof exceeds bounds', 1),
    ('metric-sql', 'no such table: strategy_health', 17),
    ('metric-bound', 'recovery metric proof exceeds bounds', 17),
    ('checkpoint', 'recovery accepted checkpoints missing', 17),
])
def test_proof_releases_readers_preserving_results_and_refusals(synthetic_proof, monkeypatch, damage, error, opened):
    state, expected, cohorts = synthetic_proof
    book = state / cohorts[0] / 'portfolio.db'
    if damage in {'incomplete', 'identity', 'phase', 'sql', 'economic-bound', 'opening-bound'}:
        with closing(sqlite3.connect(book)) as connection, connection:
            if damage == 'incomplete':
                connection.execute('DELETE FROM committee_decisions')
            elif damage == 'identity':
                connection.execute("UPDATE session_execution_contexts SET epoch_id='wrong'")
            elif damage == 'phase':
                connection.execute("UPDATE session_phases SET pre_state_digest='wrong'")
            elif damage == 'sql':
                connection.execute('DROP TABLE fills')
            elif damage == 'economic-bound':
                connection.executemany('INSERT INTO fills VALUES (?, ?, ?)',
                                       [(str(index), SESSION, '1') for index in range(2, 10002)])
            else:
                connection.executemany('INSERT INTO cash_events VALUES (?, NULL, ?)',
                                       [(str(index), '1') for index in range(2, 10002)])
    if damage in {'metric-sql', 'metric-bound'}:
        with closing(sqlite3.connect(state / 'metrics_v2.sqlite3')) as connection, connection:
            if damage == 'metric-sql':
                connection.execute('DROP TABLE strategy_health')
            else:
                connection.executemany('INSERT INTO strategy_health VALUES (?, ?, ?, ?)',
                                       [(str(index), EPOCH, SESSION, 'synthetic') for index in range(2, 10002)])
    if damage == 'checkpoint':
        next((state / 'source_inputs/screening').glob('*.json')).unlink()
    before = {path: path.read_bytes() for path in state.rglob('*') if path.is_file()}
    with observed_readers(monkeypatch) as readers:
        if error:
            exception = sqlite3.OperationalError if damage in {'sql', 'metric-sql'} else ValueError
            with pytest.raises(exception, match='^' + error + '$'):
                recovery.session_recovery_proof(state, GENERATION, SESSION, EPOCH)
        else:
            assert recovery.session_recovery_proof(state, GENERATION, SESSION, EPOCH) == expected
        assert {path: path.read_bytes() for path in state.rglob('*') if path.is_file()} == before
        assert_closed(readers, opened)


def test_local_acquisition_guard_still_refuses():
    assert not recovery.local_recovery()
    with recovery.persisted_only():
        with pytest.raises(recovery.RecoveryUnavailable, match='^recovery_checkpoint_missing:synthetic$'):
            recovery.require_acquisition_allowed('synthetic')
    assert not recovery.local_recovery()
