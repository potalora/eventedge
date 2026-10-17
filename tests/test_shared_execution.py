"""Constructed sixteen-book restart evidence; external IO is forbidden."""
from datetime import datetime, timezone
import pytest

from test_source_reliability_pipeline import pipeline, SESSION, InterruptedWorker
from test_runtime_recovery import prospective
from tradingagents.strategies.orchestration import runtime_recovery as recovery
from tradingagents.strategies.orchestration.session_executor import PHASES


def enabled(pipeline, monkeypatch):
    fixture, owner, config, repo = prospective(pipeline, monkeypatch)
    for target in (config, owner._base_config, *(c['executor'].config for c in owner.cohorts)):
        target['autoresearch']['stabilization_profile_policy'] = 'supported_sixteen_books_v1'
    monkeypatch.setenv('EVENTEDGE_GENERATION_CONFIG_SHA256', 'b'*64)
    return fixture, owner


def fingerprint(owner):
    tables = ('fills', 'cash_events', 'fee_events', 'dividend_events', 'borrow_accruals',
              'financing_accruals', 'marks', 'account_snapshots', 'order_status_transitions',
              'session_execution_contexts', 'session_phases', 'staging_runs', 'accounting_state',
              'lots', 'order_intents', 'intent_signals', 'intent_policy_provenance',
              'policy_session_contexts', 'committee_decisions', 'ticker_quarantines',
              'corporate_actions', 'dividend_receivables',
              'dividend_payment_terms', 'dividend_payment_observations')
    return [[tuple(tuple(r) for r in c['ledger'].connection.execute('SELECT * FROM '+t+' ORDER BY 1'))
             for t in tables] for c in owner.cohorts]


@pytest.mark.parametrize('ordinal', [0, 7, 15])
@pytest.mark.parametrize('boundary', ['context', 'snapshot'])
def test_sealed_first_middle_last_resume_zero_io(pipeline, monkeypatch, ordinal, boundary):
    fixture, owner = enabled(pipeline, monkeypatch)
    target = owner.cohorts[ordinal]
    if boundary == 'context':
        obj, name = target['executor'], 'execute_open_and_mark'
        original = getattr(obj, name)
        def stop(*args, **kwargs):
            raise InterruptedWorker('before initial context')
    else:
        obj, name = target['executor'], '_phase'
        original = getattr(obj, name)
        def stop(session, phase, *args, **kwargs):
            if phase == 'snapshot_account':
                raise InterruptedWorker('before snapshot')
            return original(session, phase, *args, **kwargs)
    monkeypatch.setattr(obj, name, stop)
    with pytest.raises(InterruptedWorker):
        owner.run_daily(SESSION.isoformat())
    monkeypatch.setattr(obj, name, original)
    from tradingagents.strategies.orchestration import shared_execution as shared
    store, identity = shared._store(owner, SESSION)
    seal_bytes = store._path(identity, frozen=True).read_bytes()
    before_calls, before_models = fixture.calls.copy(), fixture.model_calls.copy()
    fixture.block_all = True
    result = recovery.recover_owner(owner, SESSION, now=fixture.now)
    assert result['reason_code'] == 'checkpoint_missing', result
    assert all(c['ledger'].phase_completed(SESSION, p) for c in owner.cohorts for p in PHASES)
    done = fingerprint(owner)
    from tradingagents.strategies.orchestration.shared_execution import validate_shared_execution, load_shared_execution
    seal = load_shared_execution(owner, SESSION)
    validate_shared_execution(owner, seal, SESSION, owner._epoch_id)
    assert recovery.recover_owner(owner, SESSION, now=fixture.now) == result
    assert fingerprint(owner) == done
    assert store._path(identity, frozen=True).read_bytes() == seal_bytes
    assert fixture.calls == before_calls and fixture.model_calls == before_models
    assert not fixture.blocked_calls


def interrupt_before_first(owner, monkeypatch):
    executor = owner.cohorts[0]['executor']
    original = executor.execute_open_and_mark
    monkeypatch.setattr(executor, 'execute_open_and_mark', lambda *a, **k: (_ for _ in ()).throw(InterruptedWorker()))
    with pytest.raises(InterruptedWorker):
        owner.run_daily(SESSION.isoformat())
    monkeypatch.setattr(executor, 'execute_open_and_mark', original)


@pytest.mark.parametrize('mutation', ['scope', 'roster', 'profile', 'epoch', 'code', 'session',
    'missing_bar', 'action_coverage', 'stale_state', 'last_context', 'last_volatility', 'newer_session', 'corrupt_envelope'])
def test_bad_last_book_rejects_all_before_any_effect(pipeline, monkeypatch, mutation):
    from copy import deepcopy
    from tradingagents.strategies.orchestration import shared_execution as shared
    fixture, owner = enabled(pipeline, monkeypatch)
    interrupt_before_first(owner, monkeypatch)
    payload = deepcopy(shared.load_shared_execution(owner, SESSION))
    last = sorted(payload['books'])[-1]
    book = payload['books'][last]
    if mutation == 'scope':
        book['scope'] += ('ZZZ',)
    elif mutation == 'roster':
        del payload['books'][last]
    elif mutation == 'profile':
        payload['profile_sha256'] = 'c'*64
    elif mutation == 'epoch':
        payload['epoch']['cost_model_version'] = 'changed'
    elif mutation == 'code':
        payload['epoch']['generation_commit'] = 'f'*40
    elif mutation == 'session':
        payload['session'] = SESSION.replace(day=1)
    elif mutation == 'missing_bar':
        payload['bundle']['bars'].pop(next(iter(payload['bundle']['bars'])))
        payload['bundle_sha256'] = shared._digest(payload['bundle'])
    elif mutation == 'action_coverage':
        payload['bundle']['action_coverage'][SESSION] = ('SPY',)
        payload['bundle_sha256'] = shared._digest(payload['bundle'])
    elif mutation == 'stale_state':
        owner.cohorts[-1]['ledger'].connection.execute("UPDATE accounting_state SET cash='23'")
    elif mutation == 'last_volatility':
        book['prepared']['volatility']['sources'] = ['missing-stage-binding']
    elif mutation in {'last_context', 'newer_session'}:
        cohort = next(c for c in owner.cohorts if c['config'].name == last)
        plan = next(p for p in shared.validate_shared_execution(owner, payload, SESSION, owner._epoch_id)
                    if p.cohort['config'].name == last)
        cohort['executor'].execute_open_and_mark(SESSION, owner._epoch_id, plan.bundle,
            dict(plan.borrow_rates), fixture.now, sealed_precondition=plan.precondition)
        if mutation == 'last_context':
            cohort['ledger'].connection.execute("UPDATE session_execution_contexts SET market_digest='corrupt'")
        else:
            cohort['ledger'].connection.execute("UPDATE session_execution_contexts SET session='2026-10-05'")
    elif mutation == 'corrupt_envelope':
        store, identity = shared._store(owner, SESSION)
        store._path(identity, frozen=True).write_text('{broken')
    if mutation != 'corrupt_envelope':
        monkeypatch.setattr(shared, 'load_shared_execution', lambda *a: payload)
    before, before_calls, before_models = fingerprint(owner), fixture.calls.copy(), fixture.model_calls.copy()
    fixture.block_all = True
    result = recovery.recover_owner(owner, SESSION, now=fixture.now)
    assert result['disposition'] == 'requires_user', result
    assert fingerprint(owner) == before
    assert fixture.calls == before_calls and fixture.model_calls == before_models
    assert not fixture.blocked_calls


def test_ordinary_reentry_with_seal_never_reacquires_or_creates_research(pipeline, monkeypatch):
    fixture, owner = enabled(pipeline, monkeypatch)
    interrupt_before_first(owner, monkeypatch)
    before_calls, before_models = fixture.calls.copy(), fixture.model_calls.copy()
    fixture.block_all = True
    result = owner.run_daily(SESSION.isoformat())
    assert all(row['execution_valid'] and not row['staging_valid'] for row in result.values())
    assert all(row['recovery']['reason_code'] == 'checkpoint_missing' for row in result.values())
    assert fixture.calls == before_calls and fixture.model_calls == before_models
    assert not fixture.blocked_calls


def test_overdue_cancellation_rolls_back_before_initial_context_and_recovers(pipeline, monkeypatch):
    from datetime import date
    from test_session_executor import _intent
    from tradingagents.strategies.orchestration import shared_execution as shared
    fixture, owner = enabled(pipeline, monkeypatch)
    ledger = owner.cohorts[0]['ledger']
    overdue = _intent(ledger, 'AAPL', 'buy', date(2026, 10, 1), 1, reference_session=date(2026, 9, 30))
    original = ledger.bind_session_execution_context
    def crash(*args, **kwargs):
        assert ledger.intent(overdue.intent_id).status == 'cancelled'
        raise InterruptedWorker('actual gap after cancellation before initial binding')
    monkeypatch.setattr(ledger, 'bind_session_execution_context', crash)
    with pytest.raises(InterruptedWorker):
        owner.run_daily(SESSION.isoformat())
    assert ledger.intent(overdue.intent_id).status == 'pending'
    assert ledger.session_execution_context(SESSION) is None
    assert not ledger.connection.execute("SELECT 1 FROM order_status_transitions WHERE status='cancelled'").fetchone()
    monkeypatch.setattr(ledger, 'bind_session_execution_context', original)
    store, identity = shared._store(owner, SESSION)
    accepted_bytes = store._path(identity, frozen=True).read_bytes()
    before_calls, before_models = fixture.calls.copy(), fixture.model_calls.copy()
    fixture.block_all = True
    result = recovery.recover_owner(owner, SESSION, now=fixture.now)
    assert result['reason_code'] == 'checkpoint_missing', result
    assert ledger.intent(overdue.intent_id).status == 'cancelled'
    done = fingerprint(owner)
    assert recovery.recover_owner(owner, SESSION, now=fixture.now) == result
    assert fingerprint(owner) == done
    assert store._path(identity, frozen=True).read_bytes() == accepted_bytes
    assert fixture.calls == before_calls and fixture.model_calls == before_models
    assert not fixture.blocked_calls


def test_second_session_with_real_staged_intents_resumes_mixed_books_and_preserves_volatility(pipeline, monkeypatch):
    from datetime import date
    from tradingagents.strategies.orchestration import shared_execution as shared
    fixture, owner, _, _ = prospective(pipeline, monkeypatch)
    first = owner.run_daily(SESSION.isoformat())
    assert all(not row['error'] for row in first.values())
    fixture, owner = enabled(pipeline, monkeypatch)
    session = date(2026, 10, 5)
    assert all(c['ledger'].pending_intents(session) for c in owner.cohorts)
    fixture.session, fixture.now = session, datetime(2026, 10, 5, 22, tzinfo=timezone.utc)
    executor = owner.cohorts[7]['executor']
    original = executor._phase
    def stop(day, phase, *args, **kwargs):
        if phase == 'snapshot_account':
            raise InterruptedWorker()
        return original(day, phase, *args, **kwargs)
    monkeypatch.setattr(executor, '_phase', stop)
    with pytest.raises(InterruptedWorker):
        owner.run_daily(session.isoformat())
    monkeypatch.setattr(executor, '_phase', original)
    seal = shared.load_shared_execution(owner, session)
    assert all(row['prepared']['volatility']['annualized_volatility'] for row in seal['books'].values())
    before_calls, before_models = fixture.calls.copy(), fixture.model_calls.copy()
    fixture.block_all = True
    result = recovery.recover_owner(owner, session, now=fixture.now)
    assert result['reason_code'] == 'checkpoint_missing', result
    assert all(c['ledger'].phase_completed(session, phase) for c in owner.cohorts for phase in PHASES)
    assert all(len(c['ledger'].read_fills(session, session)) == 1 for c in owner.cohorts)
    done = fingerprint(owner)
    assert recovery.recover_owner(owner, session, now=fixture.now) == result
    assert fingerprint(owner) == done
    assert fixture.calls == before_calls and fixture.model_calls == before_models
    assert not fixture.blocked_calls


def test_missing_actual_last_book_staging_volatility_rejects_before_good_books_advance(pipeline, monkeypatch):
    from datetime import date
    fixture, owner, _, _ = prospective(pipeline, monkeypatch)
    owner.run_daily(SESSION.isoformat())
    fixture, owner = enabled(pipeline, monkeypatch)
    session = date(2026, 10, 5)
    fixture.session, fixture.now = session, datetime(2026, 10, 5, 22, tzinfo=timezone.utc)
    executor = owner.cohorts[0]['executor']
    original = executor.execute_open_and_mark
    monkeypatch.setattr(executor, 'execute_open_and_mark', lambda *a, **k: (_ for _ in ()).throw(InterruptedWorker()))
    with pytest.raises(InterruptedWorker):
        owner.run_daily(session.isoformat())
    monkeypatch.setattr(executor, 'execute_open_and_mark', original)
    # Remove the actual source of the sealed entry-risk inputs, keeping the
    # immutable seal intact. It must never silently default missing volatility.
    owner.cohorts[-1]['ledger'].connection.execute("DELETE FROM policy_session_contexts WHERE binding_kind='staging'")
    before, before_calls, before_models = fingerprint(owner), fixture.calls.copy(), fixture.model_calls.copy()
    fixture.block_all = True
    result = recovery.recover_owner(owner, session, now=fixture.now)
    assert result['reason_code'] == 'checkpoint_invalid', result
    assert fingerprint(owner) == before
    assert fixture.calls == before_calls and fixture.model_calls == before_models
    assert not fixture.blocked_calls


def test_recovery_interrupted_again_days_later_retains_original_acceptance_clock(pipeline, monkeypatch):
    fixture, owner = enabled(pipeline, monkeypatch)
    interrupt_before_first(owner, monkeypatch)
    executor = owner.cohorts[0]['executor']
    original = executor._phase
    def stop(day, phase, *args, **kwargs):
        if phase == 'snapshot_account':
            raise InterruptedWorker()
        return original(day, phase, *args, **kwargs)
    monkeypatch.setattr(executor, '_phase', stop)
    late = datetime(2026, 10, 6, 22, tzinfo=timezone.utc)
    fixture.block_all = True
    with pytest.raises(InterruptedWorker):
        recovery.recover_owner(owner, SESSION, now=late)
    monkeypatch.setattr(executor, '_phase', original)
    result = recovery.recover_owner(owner, SESSION, now=late)
    assert result['reason_code'] == 'missed_staging', result
    assert all(c['ledger'].phase_completed(SESSION, phase) for c in owner.cohorts for phase in PHASES)
    assert not fixture.blocked_calls


def test_transactional_precondition_rechecks_policy_after_all_book_validation(pipeline, monkeypatch):
    from decimal import Decimal
    from tradingagents.strategies.orchestration import shared_execution as shared
    fixture, owner = enabled(pipeline, monkeypatch)
    interrupt_before_first(owner, monkeypatch)
    plans = shared.validate_shared_execution(owner, shared.load_shared_execution(owner, SESSION), SESSION, owner._epoch_id)
    plan = plans[0]
    executor = plan.cohort['executor']
    executor.borrow_reject_above = Decimal('0.07')
    before = fingerprint(owner)
    result = executor.execute_open_and_mark(SESSION, owner._epoch_id, plan.bundle,
        dict(plan.borrow_rates), fixture.now, sealed_precondition=plan.precondition)
    assert not result.valid
    assert fingerprint(owner) == before


@pytest.mark.parametrize('mutation', ['class_tag', 'unknown_bar_field', 'nan', 'naive_clock',
    'wrong_embedded_identity', 'source_wrong_type', 'duplicate_ticker', 'duplicate_action'])
def test_named_bundle_codec_rejects_invalid_accepted_documents(pipeline, monkeypatch, mutation):
    from copy import deepcopy
    from decimal import Decimal
    from tradingagents.strategies.orchestration import shared_execution as shared
    fixture, owner = enabled(pipeline, monkeypatch)
    interrupt_before_first(owner, monkeypatch)
    seal = shared.load_shared_execution(owner, SESSION)
    document = deepcopy(seal['bundle'])
    bar = next(iter(document['bars'].values()))
    if mutation == 'class_tag':
        document['class'] = 'arbitrary.Constructor'
    elif mutation == 'unknown_bar_field':
        bar['adjustment_hint'] = 'trust-me'
    elif mutation == 'nan':
        bar['close'] = Decimal('NaN')
    elif mutation == 'naive_clock':
        bar['fetched_at'] = bar['fetched_at'].replace(tzinfo=None)
    elif mutation == 'wrong_embedded_identity':
        bar['ticker'] = 'ZZZ'
    elif mutation == 'source_wrong_type':
        bar['source'] = SESSION
    elif mutation == 'duplicate_ticker':
        document['tickers'] += (document['tickers'][0],)
    elif mutation == 'duplicate_action':
        action = {'action_id': 'same-id', 'ticker': document['tickers'][0], 'session': SESSION,
            'action_type': 'split', 'ratio': Decimal('2'), 'cash_per_share': None,
            'source': 'fixture', 'fetched_at': bar['fetched_at'], 'verified': True,
            'payment_date': None, 'payment_source': '', 'payment_reference': '', 'payment_observed_at': None}
        document['actions'] = (action, action.copy())
    with pytest.raises(shared.SourceInputError):
        shared.deserialize_execution_bundle(document, seal['accepted_at'])


def test_overdue_signal_association_order_is_part_of_precondition(pipeline, monkeypatch):
    from datetime import date
    from test_session_executor import _intent
    fixture, owner = enabled(pipeline, monkeypatch)
    ledger = owner.cohorts[-1]['ledger']
    _intent(ledger, 'AAPL', 'buy', date(2026, 10, 1), 1, reference_session=date(2026, 9, 30))
    interrupt_before_first(owner, monkeypatch)
    ledger.connection.execute('UPDATE intent_signals SET signal_order=5')
    before = fingerprint(owner)
    fixture.block_all = True
    result = recovery.recover_owner(owner, SESSION, now=fixture.now)
    assert result['reason_code'] == 'checkpoint_invalid', result
    assert fingerprint(owner) == before
    assert not fixture.blocked_calls


def test_quarantined_required_ticker_in_last_book_prevents_all_resumed_effects(pipeline, monkeypatch):
    from datetime import date
    fixture, owner, _, _ = prospective(pipeline, monkeypatch)
    owner.run_daily(SESSION.isoformat())
    fixture, owner = enabled(pipeline, monkeypatch)
    session = date(2026, 10, 5)
    fixture.session, fixture.now = session, datetime(2026, 10, 5, 22, tzinfo=timezone.utc)
    executor = owner.cohorts[0]['executor']
    original = executor.execute_open_and_mark
    monkeypatch.setattr(executor, 'execute_open_and_mark', lambda *a, **k: (_ for _ in ()).throw(InterruptedWorker()))
    with pytest.raises(InterruptedWorker):
        owner.run_daily(session.isoformat())
    monkeypatch.setattr(executor, 'execute_open_and_mark', original)
    last = owner.cohorts[-1]
    ticker = last['executor'].required_tickers(session, owner._epoch_id)[0]
    last['ledger'].quarantine_ticker(ticker, 'review quarantine')
    before, calls, models = fingerprint(owner), fixture.calls.copy(), fixture.model_calls.copy()
    fixture.block_all = True
    result = recovery.recover_owner(owner, session, now=fixture.now)
    assert fingerprint(owner) == before
    assert not any(c['ledger'].read_fills(session, session) for c in owner.cohorts)
    assert result['reason_code'] == 'checkpoint_invalid', result
    assert fixture.calls == calls and fixture.model_calls == models
    assert not fixture.blocked_calls


def exact_supported_owner(pipeline, monkeypatch):
    """Load the checked-in profile; substitute only offline credentials/state."""
    import hashlib
    from pathlib import Path
    from tradingagents.strategies.orchestration.generation_config import apply_generation_config
    from tradingagents.strategies.orchestration.cohort_orchestrator import CohortOrchestrator, build_default_cohorts
    from test_source_reliability_pipeline import GENERATION, COMMIT
    fixture, historical, base, repo = prospective(pipeline, monkeypatch)
    profile = Path(__file__).parents[1] / 'config/profiles/stabilization-v1.json'
    digest = hashlib.sha256(profile.read_bytes()).hexdigest()
    config = apply_generation_config(base, profile, digest)
    ar = config['autoresearch']
    ar['state_dir'] = str(repo / 'supported-profile-state')
    for name in ('finnhub_api_key', 'fred_api_key', 'courtlistener_token', 'usda_nass_api_key', 'fmp_api_key'):
        ar[name] = base['autoresearch'].get(name, '')
    ar['finnhub_reliability']['rate_delay_s'] = 0
    # The historical transport fixture predates capability acquisition metadata.
    # Supply the same explicit observation/availability records as the provider
    # adapter; production capability validation remains active.
    from tradingagents.strategies.data_sources.fred_source import FREDSource
    def fred_series(_source, series_id, **params):
        values = fixture.fred(series_id, **params)
        vintage = params['realtime_start']
        captured = fixture.now.isoformat()
        values.attrs.update(acquired_at=captured, available_at=captured,
            availability_basis='forward_acquisition_time', observation_records=[
                {'observation_date': day.date().isoformat(), 'value': float(value),
                 'realtime_start': vintage, 'realtime_end': vintage} for day, value in values.items()])
        return values
    monkeypatch.setattr(FREDSource, '_get_series', fred_series)
    from tradingagents.strategies.orchestration import daily_pipeline
    from tradingagents.strategies.data_sources import courtlistener_source, usaspending_source
    monkeypatch.setattr(courtlistener_source, 'datetime', daily_pipeline.datetime)
    monkeypatch.setattr(usaspending_source, 'datetime', daily_pipeline.datetime)
    monkeypatch.setenv('EVENTEDGE_GENERATION_CONFIG_FILE', str(profile))
    monkeypatch.setenv('EVENTEDGE_GENERATION_CONFIG_SHA256', digest)
    owner = CohortOrchestrator(build_default_cohorts(config), config,
                              generation_id=GENERATION, generation_commit=COMMIT)
    return fixture, owner


def test_exact_profile_sealed_committee_resumes_staging_without_io_or_duplicates(pipeline, monkeypatch):
    fixture, owner = exact_supported_owner(pipeline, monkeypatch)
    try:
        ledger = owner.cohorts[-1]['ledger']
        original = ledger.complete_staging
        monkeypatch.setattr(ledger, 'complete_staging', lambda *a, **k: (_ for _ in ()).throw(InterruptedWorker()))
        with pytest.raises(InterruptedWorker):
            owner.run_daily(SESSION.isoformat())
        monkeypatch.setattr(ledger, 'complete_staging', original)
        assert all(c['ledger'].phase_completed(SESSION, p) for c in owner.cohorts for p in PHASES)
        assert sum(c['ledger'].staging_completed(SESSION, owner._epoch_id,
            c['engine'].ar_config['paper_ledger']['policy_id']) for c in owner.cohorts) == 15
        calls, models = fixture.calls.copy(), fixture.model_calls.copy()
        fixture.block_all = True
        result = recovery.recover_owner(owner, SESSION, now=fixture.now)
        assert result['reason_code'] == 'complete', result
        assert all(c['ledger'].staging_completed(SESSION, owner._epoch_id,
            c['engine'].ar_config['paper_ledger']['policy_id']) for c in owner.cohorts)
        done = fingerprint(owner)
        assert recovery.recover_owner(owner, SESSION, now=fixture.now) == result
        assert fingerprint(owner) == done
        assert fixture.calls == calls and fixture.model_calls == models
        assert not fixture.blocked_calls
    finally:
        for cohort in owner.cohorts:
            cohort['ledger'].close()


def test_preexisting_required_ticker_quarantine_cannot_be_sealed(pipeline, monkeypatch):
    from datetime import date
    from tradingagents.strategies.orchestration import shared_execution as shared
    fixture, owner, _, _ = prospective(pipeline, monkeypatch)
    owner.run_daily(SESSION.isoformat())
    fixture, owner = enabled(pipeline, monkeypatch)
    session = date(2026, 10, 5)
    fixture.session, fixture.now = session, datetime(2026, 10, 5, 22, tzinfo=timezone.utc)
    last = owner.cohorts[-1]
    ticker = last['executor'].required_tickers(session, owner._epoch_id)[0]
    last['ledger'].quarantine_ticker(ticker, 'preexisting review quarantine')
    before = fingerprint(owner)
    with pytest.raises(ValueError, match='quarantined'):
        owner.run_daily(session.isoformat())
    assert shared.load_shared_execution(owner, session) is None
    assert fingerprint(owner) == before
    assert not any(c['ledger'].read_fills(session, session) for c in owner.cohorts)
