"""Complete aggregate checkpoints retain capacity independently of providers."""
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_research_screening import owner, SESSION, HORIZONS
from tradingagents.strategies.metrics.health import classify_strategy_run
from tradingagents.strategies.modules import get_paper_trade_strategies
from tradingagents.strategies.orchestration import research_screening as screening
from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine
from tradingagents.strategies.orchestration.research_resilience import digest, project_sources
from tradingagents.strategies.orchestration.source_inputs import (
    SourceInputError, SourceInputStore, daily_source_store, source_codec_limits,
)

PRIOR_CAPACITY = {'max_bytes': 512 * 1024 * 1024, 'max_nodes': 8_000_000}


@pytest.fixture
def aggregate(owner):
    strategies = get_paper_trade_strategies()
    owner._active_strategy_names = {strategy.name for strategy in strategies}
    policy = 'generation-config-' + 'a' * 64
    owner._base_config['autoresearch']['paper_ledger'] = {'policy_id': policy}
    owner._policy_id_for_horizon = lambda horizon: policy + ':health:' + horizon
    source = {name: {} for strategy in strategies for name in strategy.data_sources}
    store, identity = daily_source_store(owner, SESSION.isoformat())
    acquired = datetime(2026, 10, 9, 20, tzinfo=timezone.utc)
    store.freeze(identity, source, acquired_at=acquired)
    envelope = SourceInputStore.decode(store._path(identity, frozen=True).read_text())
    projected, scope = project_sources(source, set(source) | {'fred', 'yfinance'})
    engine = MultiStrategyEngine.__new__(MultiStrategyEngine)
    engine.ar_config = owner._base_config['autoresearch']
    regime = {**engine._build_regime_model(projected), 'research_source_scope': scope,
        'filing_research_isolation': None, 'research_projection_sha256': digest(projected),
        'research_hold_reasons': ['shared_regime_unavailable']}
    sample = {}
    for horizon in HORIZONS:
        health = [classify_strategy_run(epoch_id=owner._epoch_id, session=SESSION,
            policy_id=owner._policy_id_for_horizon(horizon), strategy=strategy.name,
            data_sources=strategy.data_sources, candidates=[], provider_errors={}, exception=None)
            for strategy in strategies]
        # Four complete evidence partitions contain >520,000 logical nodes.
        # No corpus or model response is needed to exercise the real codec.
        sample[horizon] = ([], {**deepcopy(regime), 'retained_analysis_evidence': list(range(130_000))}, health)
    return owner, envelope, sample


@pytest.fixture
def retained(aggregate):
    owner, source, sample = aggregate
    # A previously accepted checkpoint uses the former finite capacity. Its
    # readers must work even when provider ingestion keeps the default limit.
    store, identity, binding = screening._context(owner, SESSION, source['digest'])
    store = SourceInputStore(store.cache_dir, accepted_dir=store.accepted_dir, **PRIOR_CAPACITY)
    payload = {'version': screening.VERSION, 'binding': binding, 'model_coverage': {'complete': True},
        'horizons': {h: {'signals': signals, 'regime': regime,
            'health': [asdict(record) for record in health]} for h, (signals, regime, health) in sample.items()}}
    payload['receipt'] = screening._receipt(store, payload)
    store.freeze(identity, payload)
    for _, _, records in sample.values():
        for record in records:
            owner._metric_store.save_strategy_health(record)
    return owner, source, sample, payload, store._path(identity, frozen=True)


def test_complete_aggregate_freezes_and_reuses_without_source_or_model_calls(aggregate):
    owner, source, sample = aggregate
    result = screening.freeze_screening(owner, SESSION, source['digest'], sample, {'complete': True})
    assert result[0] == sample
    path = next((Path(owner._base_config['autoresearch']['state_dir']) / 'source_inputs/screening').glob('*.json'))
    before = path.read_bytes()
    assert screening.load_screening(owner, SESSION, source['digest'], HORIZONS) == result
    assert path.read_bytes() == before
    assert all(result[2]['counts'][h]['health'] == len(owner._active_strategy_names) for h in HORIZONS)


def test_provider_ingestion_retains_default_structural_bound(retained):
    owner, _, _, payload, _ = retained
    assert source_codec_limits(owner._base_config) == {}
    with pytest.raises(SourceInputError, match='structural bounds'):
        SourceInputStore.encode(payload, **source_codec_limits(owner._base_config))
    with pytest.raises(SourceInputError, match='structural bounds'):
        SourceInputStore.decode(SourceInputStore.encode(payload, **PRIOR_CAPACITY))


def test_operational_report_decodes_complete_aggregate_read_only(retained):
    from tradingagents.strategies.orchestration.operational_report import _completion_facts
    owner, source, _, payload, path = retained
    before = path.read_bytes()
    profile = SimpleNamespace(config=owner._base_config,
        horizon_policies=payload['binding']['horizon_policies'], strategies=tuple(sorted(owner._active_strategy_names)),
        supported_strategies=tuple(sorted(owner._active_strategy_names)), roster=())
    diagnostics = []
    facts = _completion_facts(path.parents[2], profile,
        {'epoch_id': owner._epoch_id, 'session': SESSION.isoformat()}, [], source,
        lambda *value: diagnostics.append(value))
    assert facts.get('screening') == payload, diagnostics
    assert ('accepted_screening_invalid',) not in diagnostics
    assert path.read_bytes() == before


def test_local_continuity_decodes_complete_aggregate_read_only(retained):
    from tradingagents.strategies.orchestration.local_continuity_evidence import _screening
    owner, source, sample, payload, path = retained
    before = path.read_bytes()
    wire = {cohort['config'].name: {'research_screening_receipt': payload['receipt']} for cohort in owner.cohorts}
    # The helper reads the real durable health database at this canonical name.
    metrics = path.parents[2] / 'metrics_v2.sqlite3'
    metrics.write_bytes(owner._metric_store.path.read_bytes())
    restored = _screening(path.parents[2], source, owner._epoch_id, owner._base_config,
        owner._active_strategy_names, wire, SESSION.isoformat(), source['acquired_at'], datetime.now(timezone.utc))
    assert restored == sample
    assert path.read_bytes() == before
