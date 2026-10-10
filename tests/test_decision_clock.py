"""A prospective decision has a real clock, separate from reference prices."""
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from tradingagents.strategies.orchestration import decision_clock as clock


FRI = date(2026, 10, 9)
SAT = datetime(2026, 10, 10, 18, tzinfo=timezone.utc)
CONFIG = {'autoresearch': {'decision_clock_policy': 'prospective_next_open_v1'}}


def test_weekend_prospective_clock_keeps_friday_prices_and_monday_open():
    context = clock.create_context(CONFIG, FRI, acquired_at=SAT, cutoff=SAT,
                                   source_digest='a' * 64, enrichment_digest='b' * 64)
    assert context['reference_session'] == '2026-10-09'
    assert context['cutoff'] == SAT.isoformat()
    assert context['eligible_session'] == '2026-10-12'
    assert clock.validate_context(CONFIG, FRI, context) == context
    assert clock.mutable_vintage(CONFIG, FRI, now=SAT) == '2026-10-10'


@pytest.mark.parametrize('now', [datetime(2026, 10, 9, 19, tzinfo=timezone.utc),
                                datetime(2026, 10, 12, 20, tzinfo=timezone.utc)])
def test_prospective_reference_must_be_latest_completed_session(now):
    with pytest.raises(ValueError, match='latest completed'):
        clock.mutable_vintage(CONFIG, FRI, now=now)


def test_monday_after_open_cannot_stage_a_weekend_reference_at_elapsed_open():
    monday = datetime(2026, 10, 12, 15, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match='next reference-session open'):
        clock.create_context(CONFIG, FRI, acquired_at=SAT, cutoff=monday,
                             source_digest='a' * 64, enrichment_digest='b' * 64)


def test_default_historical_clock_is_unchanged():
    assert clock.mutable_vintage({}, FRI, now=SAT) == '2026-10-09'
    assert clock.resolve_cutoff({}, FRI, None).isoformat() == '2026-10-09T20:00:00+00:00'


@pytest.mark.parametrize('field,value', [('policy', 'session_close_v1'),
    ('reference_session', '2026-10-08'), ('eligible_session', '2026-10-13'),
    ('cutoff', '2026-10-10T18:00:00'), ('cutoff', '2026-10-09T19:00:00+00:00'),
    ('source_digest', ''), ('enrichment_digest', 'wrong')])
def test_context_mismatch_and_unbound_evidence_rejected(field, value):
    context = clock.create_context(CONFIG, FRI, acquired_at=SAT, cutoff=SAT,
                                   source_digest='a' * 64, enrichment_digest='b' * 64)
    context[field] = value
    with pytest.raises(ValueError):
        clock.validate_context(CONFIG, FRI, context)


def test_prospective_does_not_accept_missing_context_or_unknown_policy():
    with pytest.raises(ValueError):
        clock.resolve_cutoff(CONFIG, FRI, None)
    with pytest.raises(ValueError):
        clock.mutable_vintage({'autoresearch': {'decision_clock_policy': 'typo'}}, FRI, now=SAT)


def test_context_replay_uses_original_clock_not_wall_clock():
    context = clock.create_context(CONFIG, FRI, acquired_at=SAT, cutoff=SAT,
                                   source_digest='a' * 64, enrichment_digest='b' * 64)
    assert clock.resolve_cutoff(CONFIG, FRI, context) == SAT


def test_decision_freeze_binds_source_and_enrichment_and_replays_without_acquisition(tmp_path):
    owner = SimpleNamespace(_base_config={'autoresearch': {**CONFIG['autoresearch'], 'state_dir': str(tmp_path)}},
        _metric_epoch_context=SimpleNamespace(generation_id='test', generation_commit='commit'))
    inputs = {'_decision_acquisition': {'policy': clock.PROSPECTIVE, 'reference_session': FRI.isoformat(),
                                      'started_at': SAT.isoformat(), 'vintage_as_of': '2026-10-10'}}
    calls = []
    def fetch():
        calls.append(True)
        return {'profiles': {'AAA': {'sector': 'Technology'}}}
    enrichment, context = clock.prepare_decision_inputs(owner, FRI, inputs, 'a' * 64, fetch, now=lambda: SAT)
    assert calls == [True]
    replay = clock.prepare_decision_inputs(owner, FRI, inputs, 'a' * 64,
        lambda: pytest.fail('replay must not acquire'), now=lambda: pytest.fail('replay must not read clock'))
    assert replay == (enrichment, context)
    with pytest.raises(ValueError, match='source'):
        clock.prepare_decision_inputs(owner, FRI, inputs, 'c' * 64, fetch)


def test_prospective_bridge_requires_matching_persisted_clock(tmp_path, monkeypatch):
    from test_execution_bridge_shorts import _bridge, _signal, _recommendation, FRIDAY, MONDAY
    saturday = datetime(2026, 8, 1, 18, tzinfo=timezone.utc)
    monkeypatch.setattr(clock, 'utc_now', lambda: saturday)
    bridge, ledger = _bridge(tmp_path)
    bridge.config['autoresearch']['decision_clock_policy'] = clock.PROSPECTIVE
    signal = _signal(observed_at=saturday, event_at=saturday, decision_at=saturday)
    ledger.record_signal(signal)
    try:
        with pytest.raises(ValueError, match='context'):
            bridge.stage_intent(_recommendation(), (signal,), ledger.account_state(), saturday, MONDAY)
        context = clock.create_context(bridge.config, FRIDAY, acquired_at=saturday, cutoff=saturday,
            source_digest='a'*64, enrichment_digest='b'*64)
        ledger.bind_policy_session_context(FRIDAY, epoch_id='epoch', policy_version='test', policy_config={},
            context={'decision_clock': context}, bound_at=saturday)
        before = ledger.account_state()
        result = bridge.stage_intent(_recommendation(), (signal,), before, saturday, MONDAY)
        assert result.eligible_session == MONDAY and result.created_at == saturday
        assert ledger.account_state() == before
        with pytest.raises(ValueError, match='cutoff'):
            bridge.stage_intent(_recommendation(), (signal,), before, saturday.replace(hour=19), MONDAY)
    finally:
        ledger.close()


def test_staging_binds_prospective_clock_and_rejects_changed_replay(tmp_path, monkeypatch):
    from test_session_executor import _policy_enabled_staging_fixture, FRIDAY
    saturday = datetime(2026, 8, 1, 18, tzinfo=timezone.utc)
    monkeypatch.setattr(clock, 'utc_now', lambda: saturday)
    ledger, engine, call = _policy_enabled_staging_fixture(tmp_path)
    engine.config['autoresearch']['decision_clock_policy'] = clock.PROSPECTIVE
    call['annualized_volatility_evidence'] = {'AAPL': .31}
    call['shared_signals'][0]['metadata']['observed_at'] = saturday.isoformat()
    context = clock.create_context(engine.config, FRIDAY, acquired_at=saturday, cutoff=saturday,
        source_digest='a'*64, enrichment_digest='b'*64)
    call['data']['_decision_context'] = context
    try:
        first = engine.screen_and_stage(**call)
        assert first['cutoff_late'] == []
        assert ledger.read_policy_session_context(FRIDAY)['context']['decision_clock'] == context
        assert engine.screen_and_stage(**call)['replayed'] is True
        call['data']['_decision_context'] = {**context, 'source_digest': 'c'*64}
        with pytest.raises(Exception, match='decision clock'):
            engine.screen_and_stage(**call)
    finally:
        ledger.close()



def _late_publication_clock(monkeypatch):
    """Only wall-clock reads move; frozen aware values keep their real type."""
    from tradingagents.strategies.orchestration import multi_strategy_engine as engine_module
    from tradingagents.strategies.trading import execution_bridge as bridge_module
    real_datetime = datetime
    class AcceptRealDatetime(type):
        def __instancecheck__(cls, value):
            return isinstance(value, real_datetime)
    class LateDatetime(real_datetime, metaclass=AcceptRealDatetime):
        @classmethod
        def now(cls, tz=None):
            value = real_datetime(2026, 8, 3, 13, 31, tzinfo=timezone.utc)
            return value if tz is None else value.astimezone(tz)
    for module in (clock, engine_module, bridge_module):
        monkeypatch.setattr(module, 'datetime', LateDatetime)


def test_prospective_first_staging_cannot_publish_after_eligible_open(tmp_path, monkeypatch):
    from test_session_executor import _policy_enabled_staging_fixture, FRIDAY
    saturday = datetime(2026, 8, 1, 18, tzinfo=timezone.utc)
    ledger, engine, call = _policy_enabled_staging_fixture(tmp_path)
    engine.config['autoresearch']['decision_clock_policy'] = clock.PROSPECTIVE
    call['annualized_volatility_evidence'] = {'AAPL': .31}
    call['shared_signals'][0]['metadata']['observed_at'] = saturday.isoformat()
    context = clock.create_context(engine.config, FRIDAY, acquired_at=saturday, cutoff=saturday,
        source_digest='a'*64, enrichment_digest='b'*64)
    call['data']['_decision_context'] = context
    _late_publication_clock(monkeypatch)
    try:
        with pytest.raises(ValueError, match='open'):
            engine.screen_and_stage(**call)
        assert not ledger.staging_completed(FRIDAY, 'epoch', 'foundation-30d')
    finally:
        ledger.close()


def test_prospective_direct_bridge_cannot_create_intent_after_eligible_open(tmp_path, monkeypatch):
    from test_execution_bridge_shorts import _bridge, _signal, _recommendation, FRIDAY, MONDAY
    saturday = datetime(2026, 8, 1, 18, tzinfo=timezone.utc)
    bridge, ledger = _bridge(tmp_path)
    bridge.config['autoresearch']['decision_clock_policy'] = clock.PROSPECTIVE
    signal = _signal(observed_at=saturday, event_at=saturday, decision_at=saturday)
    ledger.record_signal(signal)
    context = clock.create_context(bridge.config, FRIDAY, acquired_at=saturday, cutoff=saturday,
        source_digest='a'*64, enrichment_digest='b'*64)
    ledger.bind_policy_session_context(FRIDAY, epoch_id='epoch', policy_version='test', policy_config={},
        context={'decision_clock': context}, bound_at=saturday)
    _late_publication_clock(monkeypatch)
    before = ledger.account_state()
    try:
        with pytest.raises(ValueError, match='open'):
            bridge.stage_intent(_recommendation(), (signal,), before, saturday, MONDAY)
        assert ledger.account_state() == before
    finally:
        ledger.close()


@pytest.mark.parametrize('cross_during', ['committee', 'publication'])
def test_prospective_cross_open_during_work_rolls_back_publication(tmp_path, monkeypatch, cross_during):
    from test_session_executor import _policy_enabled_staging_fixture, FRIDAY
    from tradingagents.strategies.trading.portfolio_committee import PortfolioCommittee
    saturday = datetime(2026, 8, 1, 18, tzinfo=timezone.utc)
    times = [datetime(2026, 8, 3, 13, 29, tzinfo=timezone.utc)]
    monkeypatch.setattr(clock, 'utc_now', lambda: times[0])
    ledger, engine, call = _policy_enabled_staging_fixture(tmp_path)
    engine.config['autoresearch']['decision_clock_policy'] = clock.PROSPECTIVE
    call['annualized_volatility_evidence'] = {'AAPL': .31}
    call['shared_signals'][0]['metadata']['observed_at'] = saturday.isoformat()
    call['data']['_decision_context'] = clock.create_context(engine.config, FRIDAY,
        acquired_at=saturday, cutoff=saturday, source_digest='a'*64, enrichment_digest='b'*64)
    crossed = []
    def cross():
        crossed.append(True)
        times[0] = datetime(2026, 8, 3, 13, 31, tzinfo=timezone.utc)
    if cross_during == 'committee':
        original = PortfolioCommittee.synthesize
        def synthesize(self, *args, **kwargs):
            result = original(self, *args, **kwargs)
            cross()
            return result
        monkeypatch.setattr(PortfolioCommittee, 'synthesize', synthesize)
    else:
        original = ledger.record_policy_staging_audit_manifest
        def manifest(*args, **kwargs):
            result = original(*args, **kwargs)
            cross()
            return result
        monkeypatch.setattr(ledger, 'record_policy_staging_audit_manifest', manifest)
    before = ledger.account_state()
    try:
        with pytest.raises(ValueError, match='open'):
            engine.screen_and_stage(**call)
        assert crossed == [True]
        assert not ledger.staging_completed(FRIDAY, 'epoch', 'foundation-30d')
        assert ledger.account_state() == before
        assert ledger._connection.execute('SELECT count(*) FROM order_intents').fetchone()[0] == 0
        assert ledger._connection.execute('SELECT count(*) FROM policy_staging_audit_manifests').fetchone()[0] == 0
    finally:
        ledger.close()
