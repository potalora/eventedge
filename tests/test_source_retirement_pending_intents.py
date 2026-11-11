"""Original pending-risk evidence survives only the proved Congress amendment."""
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_source_retirement_amendment import deployed_amendment
from test_portfolio_policy_execution_invariants import _signal, _staging_context
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.strategies.execution import OrderIntent, stable_id
from tradingagents.strategies.metrics.models import MetricEpoch
from tradingagents.strategies.metrics.store import MetricStore
from tradingagents.strategies.orchestration.cohort_orchestrator import SIZE_PROFILES
from tradingagents.strategies.orchestration.generation_config import apply_generation_config
from tradingagents.strategies.orchestration.session_executor import SessionExecutor, SessionInputBundle
from tradingagents.strategies.orchestration.source_retirement_profile import make_retired_profile
from tradingagents.strategies.orchestration import shared_execution as shared
from tradingagents.strategies.orchestration.source_inputs import (source_configuration_fingerprint,
    configuration_fingerprint, daily_source_store, public_source_configuration_declaration)
from tradingagents.strategies.execution.price_source import AdjustedClose
from tradingagents.strategies.orchestration.operating_contract import ROSTER
from tradingagents.strategies.state.portfolio_ledger import PortfolioLedger, LedgerConflictError

REFERENCE = date(2026, 7, 31)
EXECUTION = date(2026, 8, 3)
ACCEPTED = datetime(2026, 7, 31, 22, tzinfo=timezone.utc)


@pytest.fixture
def pending_retirement(deployed_amendment):
    repo, row = deployed_amendment
    state = Path(row['state_dir'])
    previous_text = Path(row['source_retirement']['previous_config_file']).read_text()
    retired = make_retired_profile(previous_text, EXECUTION.isoformat())
    body = json.dumps(retired, indent=2) + '\n'
    Path(row['config_file']).write_text(body)
    row['config_sha256'] = row['source_retirement']['config_sha256'] = hashlib.sha256(body.encode()).hexdigest()
    row['source_retirement']['effective_session'] = EXECUTION.isoformat()
    manifest = state.parent / 'manifest.json'
    manifest.write_text(json.dumps({'generations': [row]}))
    config = apply_generation_config(DEFAULT_CONFIG, row['config_file'], row['config_sha256'])
    name = 'horizon_30d_size_5k'
    config['autoresearch'].update(state_dir=str(state / name), horizon='30d')
    profile = SIZE_PROFILES['5k']
    config['autoresearch']['risk_gate'].update(max_positions=profile.max_positions,
        max_position_pct=profile.max_position_pct, min_position_value=profile.min_position_value,
        cash_reserve_pct=profile.cash_reserve_pct, long_only=not profile.short_eligible)
    ledger = PortfolioLedger(state / name / 'portfolio.db', name, Decimal('5000'))
    store = MetricStore(state / 'metrics_v2.sqlite3')
    previous_epoch = MetricEpoch('old-epoch', row['gen_id'], row['source_retirement']['previous_commit'],
        'old-behavior', 'old-configuration', 2, 'clock-v1', 'pricing-v1', 'cost-v1', REFERENCE, None, 'open', 'initial')
    store.save_epoch(previous_epoch)
    store.close_epoch(previous_epoch.epoch_id, REFERENCE, 'semantic_hash_changed')
    current_epoch = replace(previous_epoch, epoch_id='new-epoch', generation_commit=row['git_commit'],
        behavior_hash='new-behavior', config_hash='new-configuration', start_session=EXECUTION,
        boundary_reason='semantic_hash_changed')
    store.save_epoch(current_epoch)
    executor = SessionExecutor(ledger, config, size_profile=profile, metric_store=store)
    signal = replace(_signal('AAPL', REFERENCE, suffix='retirement'), epoch_id=previous_epoch.epoch_id,
                     policy_id=config['autoresearch']['paper_ledger']['policy_id'])
    ledger.record_signal(signal)
    binding = ledger.bind_policy_session_context(REFERENCE, binding_kind='staging',
        epoch_id=previous_epoch.epoch_id, policy_version=executor.portfolio_policy_config.version,
        policy_config=executor.portfolio_policy_document(),
        context=_staging_context(executor, volatility={'AAPL': 0.31}), bound_at=ACCEPTED)
    provenance = dict(policy_version=executor.portfolio_policy_config.version, event_key=signal.event_key,
        source_event_keys=('original-source',), strategy_tags=(signal.strategy,), risk_tags=('original-risk',),
        sector='Technology', journal_only=False, order_eligible=True, decision='accepted', reason_codes=('accepted',),
        bound_context_digest=binding['context_digest'], captured_at=ACCEPTED)
    ledger.record_signal_policy_provenance(signal.signal_id, **provenance)
    intent = OrderIntent('original-pending-intent', (signal.signal_id,), name, 'buy', 1,
                         signal.decision_at, EXECUTION, 'next_session_open', 'pending', None, None)
    ledger.stage_intent(intent)
    ledger.record_intent_policy_provenance(intent.intent_id, signal_ids=intent.signal_ids, **provenance)
    old_config = apply_generation_config(DEFAULT_CONFIG, row['source_retirement']['previous_config_file'],
                                         row['source_retirement']['previous_config_sha256'])
    old_config['autoresearch']['state_dir'] = str(state)
    owner = SimpleNamespace(_base_config=old_config, _metric_epoch_context=previous_epoch)
    policy = executor.semantic_policy_document()
    policy['congress_audit_continuity_policy'] = old_config['autoresearch']['congress_audit_continuity_policy']
    identity = dict(cohort_id=name, horizon='30d', size_profile='5k', ledger_cohort_id=name,
        policy_id=signal.policy_id, semantic_config_digest=stable_id('session_execution_config', policy))
    bundle = shared.serialize_execution_bundle(SessionInputBundle(REFERENCE, (), {}, (), {
        (symbol, REFERENCE): AdjustedClose(symbol, REFERENCE, Decimal('100'), 'fixture', ACCEPTED)
        for symbol in executor.benchmark_symbols}))
    prepared = dict(required_tickers=(), market={}, config=policy, borrow={}, provenance={},
        volatility={'annualized_volatility': {}, 'source_bindings': []})
    books = {}
    for cohort_name in ROSTER:
        selected_identity = {**identity, 'cohort_id': cohort_name, 'ledger_cohort_id': cohort_name,
                             'horizon': cohort_name.split('_')[1], 'size_profile': cohort_name.split('_')[-1]}
        books[cohort_name] = dict(identity=selected_identity, scope=tuple(sorted(executor.benchmark_symbols)),
            benchmarks=tuple(sorted(executor.benchmark_symbols)), prepared=deepcopy(prepared),
            starting_state={}, starting_state_digest='original-state', overdue={}, precondition_digest='original-precondition',
            post_cancellation_digest='original-post-cancellation')
    payload = dict(version=shared.VERSION, session=REFERENCE, epoch={key: getattr(previous_epoch, key) for key in shared.EPOCH},
        configuration=source_configuration_fingerprint(old_config), effective_configuration=configuration_fingerprint(old_config),
        profile_sha256=row['source_retirement']['previous_config_sha256'], accepted_at=ACCEPTED,
        bundle=bundle, bundle_sha256=shared._digest(bundle), books=books)
    accepted_store, accepted_identity = shared._store(owner, REFERENCE)
    accepted_store.freeze(accepted_identity, payload, acquired_at=ACCEPTED)
    original_store, original_identity = daily_source_store(owner, REFERENCE.isoformat())
    original_store.freeze(original_identity, public_source_configuration_declaration(old_config), acquired_at=ACCEPTED)
    try:
        yield repo, row, executor, previous_epoch, current_epoch, accepted_store, accepted_identity
    finally:
        ledger.close()


def test_pending_volatility_and_rehydration_use_original_verified_retirement_evidence(pending_retirement):
    _, _, executor, _, current, _, _ = pending_retirement
    before = executor.ledger.path.read_bytes()
    document, values = executor._resolve_staged_volatility_evidence(EXECUTION, current.epoch_id)
    assert values == {'AAPL': 0.31}
    assert document['source_bindings'][0]['reference_session'] == '2026-07-31'
    context = {'session': EXECUTION, 'economic_inputs_json': json.dumps({'portfolio_policy_volatility_evidence': document})}
    assert executor._rehydrate_bound_volatility_evidence(context, current.epoch_id) == (document, values)
    assert executor.ledger.path.read_bytes() == before
    assert executor.ledger.intent('original-pending-intent').status == 'pending'


def test_original_pending_evidence_does_not_depend_on_current_provider_credentials(pending_retirement, monkeypatch):
    _, _, executor, _, current, _, _ = pending_retirement
    monkeypatch.setenv('FRED_API_KEY', 'rotated-synthetic-fixture-key')
    _, values = executor._resolve_staged_volatility_evidence(EXECUTION, current.epoch_id)
    assert values == {'AAPL': 0.31}


@pytest.mark.parametrize('fault', ['no_manifest', 'wrong_previous_commit', 'changed_epoch_configuration',
    'missing_accepted_seal', 'changed_policy_config', 'missing_profile_proof', 'wrong_original_signal_policy',
    'bad_roster', 'bad_bundle', 'bad_book', 'bad_horizon', 'bad_size'])
def test_cross_epoch_volatility_rejects_unproved_or_changed_original_evidence(pending_retirement, fault):
    repo, row, executor, previous, current, accepted_store, accepted_identity = pending_retirement
    if fault == 'no_manifest':
        (repo / 'data/generations/manifest.json').unlink()
    elif fault == 'wrong_previous_commit':
        row['source_retirement']['previous_commit'] = 'e' * 40
        (repo / 'data/generations/manifest.json').write_text(json.dumps({'generations': [row]}))
    elif fault == 'changed_epoch_configuration':
        with executor.metric_store._connect() as connection:
            payload = asdict(executor.metric_store.load_epoch(previous.epoch_id))
            payload['config_hash'] = 'foreign-configuration'
            connection.execute('UPDATE metric_epochs SET payload_json=? WHERE epoch_id=?',
                (json.dumps(payload, default=str), previous.epoch_id))
    elif fault == 'missing_accepted_seal':
        accepted_store._path(accepted_identity, frozen=True).unlink()
    elif fault == 'changed_policy_config':
        executor.portfolio_policy_config = type(executor.portfolio_policy_config).from_size_profile(
            replace(executor.size_profile, max_positions=99), executor.ar_config['portfolio_policy'])
    elif fault == 'missing_profile_proof':
        executor.config.pop('source_retirement')
    elif fault == 'wrong_original_signal_policy':
        executor.ledger.connection.execute("UPDATE signals SET policy_id='foreign-policy'")
    else:
        payload = accepted_store.load_frozen(accepted_identity)
        book = payload['books'][executor.ledger.cohort_id]
        if fault == 'bad_roster':
            payload['books'].pop(next(name for name in ROSTER if name != executor.ledger.cohort_id))
        elif fault == 'bad_bundle':
            payload['bundle'] = {}
            payload['bundle_sha256'] = shared._digest({})
        elif fault == 'bad_book':
            book.pop('starting_state')
        elif fault == 'bad_horizon':
            book['identity']['horizon'] = '1y'
        else:
            book['identity']['size_profile'] = '100k'
        accepted_store._write(accepted_store._path(accepted_identity, frozen=True), accepted_identity,
                              payload, ACCEPTED, exclusive=False)
    before = executor.ledger.path.read_bytes()
    with pytest.raises(LedgerConflictError, match='volatility'):
        executor._resolve_staged_volatility_evidence(EXECUTION, current.epoch_id)
    assert executor.ledger.path.read_bytes() == before
