"""Comprehensive regression & contract tests for EventEdge gen_003 orchestration-ops."""

from __future__ import annotations

from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd
import pytest

from tradingagents.strategies.config import GenerationConfig
from tradingagents.strategies.modules.base import Candidate
from tradingagents.strategies.orchestration import (
    data_acquisition as da,
    operating_contract,
    run_evidence,
    source_inputs,
)
from tradingagents.strategies.orchestration.cohort_orchestrator import (
    CohortConfig,
    CohortOrchestrator,
)
from tradingagents.strategies.orchestration.data_acquisition import (
    DataAcquisitionService,
    YAHOO_RESEARCH_CORE_TICKERS,
    evaluate_strategy_circuit_breaker,
)
from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine
from tradingagents.strategies.orchestration.source_inputs import SourceInputStore

REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# 1. Strict Tier-1 vs Tier-2 Fault Isolation
# ---------------------------------------------------------------------------


def test_tier2_circuit_breaker_isolates_failed_source_to_dependent_strategy_only(tmp_path):
    class GoodStrategy:
        name = "govt_contracts"
        data_sources = ["usaspending"]

        def get_default_params(self, horizon="30d"):
            return {}

        def screen(self, data, trading_date, params):
            return [
                Candidate(
                    ticker="LMT",
                    date=trading_date,
                    direction="long",
                    score=0.82,
                    metadata={"thesis": "Major DoD award"},
                    event_key="award-1",
                )
            ]

    class AffectedStrategy:
        name = "weather_ag"
        data_sources = ["noaa", "usda", "drought_monitor", "yfinance"]

        def get_default_params(self, horizon="30d"):
            return {}

        def screen(self, data, trading_date, params):
            raise AssertionError("affected strategy must be circuit-broken, not screened")

    class DummyRegistry:
        def available_sources(self):
            return ["usaspending", "noaa", "usda", "drought_monitor", "yfinance"]

        def get(self, name):
            return None

    engine = MultiStrategyEngine(
        config={"autoresearch": {"state_dir": str(tmp_path / "state")}},
        strategies=[GoodStrategy(), AffectedStrategy()],
        registry=DummyRegistry(),
    )
    data = {
        "usaspending": {"data": {"contracts": [{"award_id": "A1"}]}},
        "noaa": {"error": "NCEI 503", "_coverage": {"status": "unavailable"}},
        "usda": {"crop_progress": {}},
        "drought_monitor": {"composite_score": 1.0, "states": {}},
        "yfinance": {"prices": {}, "vix": pd.DataFrame()},
        # Optional enrichment failure must NEVER break any strategy
        "finra_short_interest": {"error": "timeout"},
        "ken_french": {"error": "503"},
    }
    signals, regime, health = engine.screen_and_enrich(
        "2026-08-10", data, horizon="30d", epoch_id="ep1", policy_id="pol1"
    )

    by_strategy = {rec.strategy: rec for rec in health}
    assert by_strategy["weather_ag"].status == "data_failure"
    assert by_strategy["weather_ag"].evidence["circuit_breaker"] == "unavailable"
    assert by_strategy["weather_ag"].evidence["failed_sources"] == ["noaa"]
    assert by_strategy["govt_contracts"].status == "signals"
    assert len(signals) == 1 and signals[0]["ticker"] == "LMT"
    assert regime["strategy_circuit_breaker"]["govt_contracts"]["available"] is True
    assert regime["strategy_circuit_breaker"]["weather_ag"]["available"] is False


def test_tier1_accounting_source_rejected_by_tier2_acquisition_service():
    svc = DataAcquisitionService({"autoresearch": {}}, registry=SimpleNamespace(available_sources=lambda: [], get=lambda _: None))
    breaker = evaluate_strategy_circuit_breaker(
        {"yfinance_profile": {"error": "down"}, "ken_french": {"error": "down"}},
        [SimpleNamespace(name="earnings_call", data_sources=["finnhub", "yfinance_profile"])],
    )
    assert breaker["earnings_call"]["available"] is True
    assert breaker["earnings_call"]["optional_enrichment_failures"] == ["ken_french", "yfinance_profile"]
    with pytest.raises(ValueError, match="Tier-1"):
        svc.fetch_all_data(
            "2026-07-10",
            "2026-08-10",
            needed_sources={"alpaca_sip"},
            fetcher_overrides={"alpaca_sip": lambda *a: {}},
        )
    # Tier-1 never downgraded in _io.SOURCE_TAXONOMY
    assert da.source_tier("alpaca_sip") == "tier1_accounting"
    assert da.source_tier("yfinance_actions_benchmarks") == "tier1_accounting"
    assert da.source_tier("finnhub") == "tier2_strategy"
    assert da.source_tier("ken_french") == "tier2_optional_enrichment"


# ---------------------------------------------------------------------------
# 2. Single Shared DataAcquisitionService, Trimmed Yahoo Core, Finnhub Ranking
# ---------------------------------------------------------------------------


def test_cohort_orchestrator_instantiates_shared_data_acquisition_once(tmp_path):
    base_config = {
        "execution": {"mode": "paper", "execution_enabled": False},
        "autoresearch": {"state_dir": str(tmp_path / "state")},
    }
    cohorts = [
        CohortConfig(f"horizon_{h}_size_{s}", str(tmp_path / "state" / f"horizon_{h}_size_{s}"), h, s, use_llm=False)
        for h in ("30d", "3m")
        for s in ("5k", "10k")
    ]
    orch = CohortOrchestrator(
        cohorts,
        base_config,
        generation_id="gen_003",
        generation_commit="a" * 40,
        price_source=MagicMock(),
    )
    try:
        acq_ids = {id(c["engine"].data_acquisition) for c in orch.cohorts}
        assert len(acq_ids) == 1
        assert next(iter(acq_ids)) == id(orch.data_acquisition)
    finally:
        for c in orch.cohorts:
            c["ledger"].close()


def test_yahoo_core_tickers_trimmed_to_13_live_tickers():
    assert len(YAHOO_RESEARCH_CORE_TICKERS) == 13
    assert set(YAHOO_RESEARCH_CORE_TICKERS) == {
        "SPY", "SHY", "TLT",
        "DBA", "WEAT", "CORN", "MOO", "SOYB",
        "ADM", "BG", "CTVA", "DE", "FMC",
    }
    # Dead retired tickers must be gone
    for dead in ("KRE", "IWN", "XRT", "LMT", "RTX", "NOC", "PLTR", "TDG"):
        assert dead not in YAHOO_RESEARCH_CORE_TICKERS


def test_finnhub_earnings_prioritizes_by_surprise_and_retains_zero_news_numeric_surprises():
    class FakeFinnhub:
        def new_workflow_deadline(self, max_budget_s=None):
            return 9999999999.0

        def fetch_recent_earnings(self, date_from, date_to, deadline=None):
            return [
                # Alphabetically first, tiny surprise
                {"symbol": "A", "date": "2026-08-10", "year": 2026, "quarter": 2, "epsActual": 1.01, "epsEstimate": 1.00, "revenueActual": 1e9},
                # Alphabetically last, huge 80% surprise, 0 news articles!
                {"symbol": "ZTS", "date": "2026-08-10", "year": 2026, "quarter": 2, "epsActual": 1.80, "epsEstimate": 1.00, "revenueActual": 5e9},
                # Medium surprise, has news
                {"symbol": "MSFT", "date": "2026-08-10", "year": 2026, "quarter": 2, "epsActual": 3.50, "epsEstimate": 3.00, "revenueActual": 60e9},
            ]

        def fetch_earnings_news(self, symbol, edate, deadline=None):
            if symbol == "ZTS":
                return []  # Zero news articles!
            return [{"source": "Reuters", "headline": f"{symbol} beats", "summary": "strong", "published_at": "2026-08-10T21:00:00Z"}]

        def fetch_company_news(self, symbol, date_from, date_to, deadline=None):
            return []

        def fetch_supply_chains(self, symbols, deadline=None):
            return {}

    fake = FakeFinnhub()
    svc = DataAcquisitionService(
        {"autoresearch": {"finnhub_acquisition": {"earnings_news_budget": 2}, "supply_chain_universe": ["AAPL"]}},
        registry=SimpleNamespace(available_sources=lambda: ["finnhub"], get=lambda n: fake if n == "finnhub" else None),
    )
    res = svc.fetch_finnhub_data("2026-08-10")
    transcripts = res["transcripts"]
    symbols = [t["symbol"] for t in transcripts]
    # ZTS (80% surprise) MUST rank #1 ahead of MSFT (16.7%) and A (1%) — NEVER alphabetical!
    assert symbols[0] == "ZTS"
    assert symbols[1] == "MSFT"
    # ZTS had 0 news articles, yet MUST NOT be dropped because it has valid numeric EPS surprise!
    assert transcripts[0]["text_source"] == "numeric_eps_surprise"
    assert transcripts[0]["eps_actual"] == 1.80
    assert transcripts[0]["eps_estimate"] == 1.00
    # Dead quantum_readiness pqc_news must not be fetched
    assert "pqc_news" not in res


def test_resolve_signals_preserves_disagreement_and_evidence():
    signals = [
        {"strategy": "supply_chain", "ticker": "NVDA", "direction": "long", "score": 0.85, "metadata": {"thesis": "AI server surge"}},
        {"strategy": "supply_chain", "ticker": "NVDA", "direction": "long", "score": 0.75, "metadata": {"thesis": "TSMC capacity expansion"}},
        {"strategy": "supply_chain", "ticker": "NVDA", "direction": "short", "score": 0.60, "metadata": {"thesis": "Export curb risk"}},
    ]
    resolved = MultiStrategyEngine._resolve_signals(signals)
    assert len(resolved) == 1
    item = resolved[0]
    assert item["ticker"] == "NVDA"
    assert item["direction"] == "long"
    assert item["long_count"] == 2
    assert item["short_count"] == 1
    assert item["disagreement"] is True
    assert item["net_conviction"] == pytest.approx((0.85 + 0.75 - 0.60) / 3.0, rel=1e-4)
    assert item["bull_evidence"] == ["AI server surge", "TSMC capacity expansion"]
    assert item["bear_evidence"] == ["Export curb risk"]


def test_dead_learning_methods_removed_from_multi_strategy_engine():
    for dead_attr in (
        "run_learning_loop",
        "_compute_strategy_confidence",
        "_read_strategy_outcomes",
        "_should_trigger_learning_loop",
        "set_cycle_tracker",
    ):
        assert not hasattr(MultiStrategyEngine, dead_attr)


# ---------------------------------------------------------------------------
# 3. Parquet + Plain JSON SourceInputStore (no 500k node cap crashes!)
# ---------------------------------------------------------------------------


def test_source_inputs_parquet_roundtrips_large_dataframe_without_node_cap(tmp_path):
    store = SourceInputStore(tmp_path / "cache", accepted_dir=tmp_path / "accepted")
    # 150,000 rows x 5 cols = 750,000 cells (would have crashed the old 500k node cap!)
    n = 150_000
    df = pd.DataFrame(
        {
            "Open": [100.0] * n,
            "High": [105.0] * n,
            "Low": [99.0] * n,
            "Close": [102.5] * n,
            "Volume": list(range(n)),
        },
        index=pd.date_range("2020-01-01", periods=n, freq="h", tz="UTC", name="ts"),
    )
    identity = {"generation": "gen_003", "session": "2026-08-10", "commit": "a" * 40, "configuration": "c" * 64}
    now = datetime(2026, 8, 10, 21, 0, tzinfo=timezone.utc)
    restored = store.freeze(identity, {"yfinance": {"prices": {"SPY": df}}}, acquired_at=now)
    pd.testing.assert_frame_equal(restored["yfinance"]["prices"]["SPY"], df)
    # Verify Parquet sidecar file was actually written to disk!
    parquet_files = list((tmp_path / "accepted").rglob("*.parquet"))
    assert len(parquet_files) == 1


def test_source_inputs_gzip_json_fallback_when_pyarrow_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(source_inputs, "_has_pyarrow", lambda: False)
    store = SourceInputStore(tmp_path / "cache", accepted_dir=tmp_path / "accepted")
    df = pd.DataFrame(
        {"Close": [10.0, float("nan")], "Vol": [100, 200]},
        index=pd.date_range("2026-08-01", periods=2, tz="UTC", name="day"),
    )
    identity = {"generation": "gen_003", "session": "2026-08-10", "commit": "a" * 40, "configuration": "c" * 64}
    now = datetime(2026, 8, 10, 21, 0, tzinfo=timezone.utc)
    restored = store.freeze(identity, {"prices": df}, acquired_at=now)
    pd.testing.assert_frame_equal(restored["prices"], df)
    assert list((tmp_path / "accepted").rglob("*.json.gz"))


# ---------------------------------------------------------------------------
# 4. Operating Contract + Worker IPC + Idle Recovery Short-Circuit + Dead CLI
# ---------------------------------------------------------------------------


def test_operating_contract_validates_gen003_clean_profile():
    profile_path = REPO_ROOT / "config" / "profiles" / "gen003-clean.json"
    raw = profile_path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    payload = json.loads(raw.decode("utf-8"))

    assert operating_contract.configured(payload) is True
    profile = operating_contract.profile_contract(payload, profile_sha256=digest)
    assert profile.version == operating_contract.POLICY
    assert profile.profile_sha256 == digest
    assert len(profile.roster) == 16
    assert isinstance(profile.generation_config, GenerationConfig)
    assert profile.supported_strategies == (
        "earnings_call",
        "supply_chain",
        "govt_contracts",
        "weather_ag",
        "commodity_macro",
    )


def test_recover_runtime_idle_tick_performs_zero_git_sqlite_and_fsync(tmp_path, monkeypatch):
    from scripts import recover_runtime as controller
    from tradingagents.strategies.orchestration import runtime_recovery

    repo = tmp_path
    manifest = repo / "data" / "generations" / "manifest.json"
    manifest.parent.mkdir(parents=True)
    row = {
        "gen_id": "gen_003",
        "git_commit": "a" * 40,
        "config_sha256": "b" * 64,
        "state_dir": str(repo / "state"),
        "worktree_path": str(repo / "worktree"),
        "config_file": str(repo / "profile.json"),
        "status": "active",
    }
    manifest.write_text(json.dumps({"generations": [row]}))

    def forbid(*_args, **_kwargs):
        raise AssertionError("idle tick must not fork git, open sqlite, or write files")

    monkeypatch.setattr(controller, "identity_for", forbid)
    monkeypatch.setattr(controller, "_discover_sessions", forbid)
    monkeypatch.setattr(controller, "atomic_json", forbid)
    monkeypatch.setattr(runtime_recovery, "atomic_json", forbid)
    monkeypatch.setattr(controller, "canonical_runtime_lock_path", lambda r: r / "runtime.lock")

    # 14:00 ET (18:00 UTC) on a weekday — BEFORE 18:00 ET trading window, 0 pending incidents!
    idle_time = datetime(2026, 8, 10, 18, 0, tzinfo=timezone.utc)
    result = controller.tick(repo, idle_time, clock=lambda: idle_time)
    assert result["status"] == "healthy"
    assert result["controller_reason_code"] is None
    assert not (repo / "data" / "runtime_recovery" / "journal.json").exists()
    assert not (repo / "data" / "runtime_recovery" / "status.json").exists()


def test_dead_cli_stubs_removed():
    from scripts import run_cohorts, run_generations

    cohorts_parser = run_cohorts._build_parser()
    cohorts_flags = set(cohorts_parser._option_string_actions)
    assert "--learning" not in cohorts_flags
    assert "--reset" not in cohorts_flags
    assert "--result-file" in cohorts_flags

    src = (REPO_ROOT / "scripts" / "run_generations.py").read_text(encoding="utf-8")
    assert "promotion-status" not in src
    assert "run-learning" not in src


def test_run_cohorts_result_file_ipc_decoupled_from_stdout(tmp_path):
    from scripts import run_cohorts
    from tradingagents.strategies.orchestration._io import load_json_strict

    result_file = tmp_path / "ipc" / "worker_result.json"
    payload = {"wire_version": 1, "cohort_results": {"horizon_30d_size_5k": {"error": False}}}
    run_cohorts._write_result_file(str(result_file), payload)
    assert result_file.is_file()
    assert load_json_strict(result_file) == payload


def test_recover_runtime_terminal_report_never_wedges_on_tier2_unavailability():
    from scripts import recover_runtime as controller

    # Clean Tier-1 accounting + valid staging + Tier-2 strategy source failure
    report = {
        "outcome": "degraded",
        "accounting_valid": True,
        "staging_complete": True,
        "staging_valid": True,
        "evidence_complete": True,
        "sources": {"unresolved": [{"provider": "noaa", "reason_code": "http_error", "http_status": 503}]},
        "source_health_failures": [{"strategy": "weather_ag", "status": "unavailable"}],
        "candidate_input_issues": [],
    }
    res = controller._terminal_report(report)
    assert res is not None
    assert res["disposition"] == "wait_next_session"
    assert res["reason_code"] == "accepted_outcome"
    assert res["disposition"] != "requires_user"


def test_one_committee_call_per_horizon_across_size_tiers(monkeypatch):
    """Verify stage_daily_results calls PortfolioCommittee.synthesize_horizon ONCE per horizon."""
    from tradingagents.strategies.orchestration import daily_pipeline
    from tradingagents.strategies.trading.portfolio_committee import PortfolioCommittee, TradeRecommendation

    calls: list[str] = []

    def fake_synthesize_horizon(self, *, horizon, signals, regime, enrichment=None, model_coverage=None):
        calls.append(horizon)
        return [
            TradeRecommendation(
                ticker="MSFT",
                direction="long",
                position_size_pct=0.0,
                confidence=0.80,
                rationale="Strong earnings surprise",
                contributing_strategies=["earnings_call"],
            )
        ]

    monkeypatch.setattr(PortfolioCommittee, "synthesize_horizon", fake_synthesize_horizon, raising=False)

    staged_calls: list[tuple[str, list]] = []

    class DummyLedger:
        def read_fills(self, *_a):
            return []

    class DummyEngine:
        def __init__(self, name):
            self.name = name
            self.state = SimpleNamespace(save_regime_snapshot=lambda *a, **kw: None)

        def screen_and_stage(self, **kwargs):
            staged_calls.append((self.name, kwargs["horizon_recommendations"]))
            return {"signals": [], "recommendations": [], "intents_staged": []}

    cohorts = []
    for h in ("30d", "3m"):
        for s in ("5k", "10k", "50k", "100k"):
            name = f"horizon_{h}_size_{s}"
            cohorts.append(
                {
                    "config": SimpleNamespace(name=name, horizon=h, size_profile=s),
                    "engine": DummyEngine(name),
                    "ledger": DummyLedger(),
                    "size_profile": SimpleNamespace(name=s, short_eligible=(s != "5k")),
                    "marked_account": SimpleNamespace(),
                }
            )

    owner = SimpleNamespace(
        _base_config={"autoresearch": {}},
        _fetch_openbb_enrichment=lambda _sigs: {},
    )
    state = SimpleNamespace(
        owner=owner,
        trading_date="2026-08-10",
        session=date(2026, 8, 10),
        epoch_id="ep1",
        first_engine=cohorts[0]["engine"],
        valid=cohorts,
        horizon_signals={
            "30d": ([{"ticker": "MSFT", "strategy": "earnings_call", "direction": "long", "score": 0.8}], {}, []),
            "3m": ([{"ticker": "MSFT", "strategy": "earnings_call", "direction": "long", "score": 0.8}], {}, []),
        },
        shared_data={},
        source_digest="d" * 64,
        candidate_bar_quarantines=[],
        volatility_quarantines=[],
        governed_reference_bars={},
        candidate_reference_bars={},
        shared_volatility_evidence={},
        model_coverage={"complete": True},
        model_deadline=None,
        new_entry_eligibility=None,
        ineligible_new_entries=set(),
        governed_summaries_by_cohort={},
        results={},
        finalize=lambda: dict(state.results),
    )

    monkeypatch.setattr(
        "tradingagents.strategies.orchestration.decision_clock.prepare_decision_inputs",
        lambda *a, **kw: ({}, None),
    )

    result = daily_pipeline.stage_daily_results(state)
    # 2 horizons x 4 size tiers = 8 cohorts staged, ONLY 2 committee calls (1 per horizon)!
    assert len(result) == 8
    assert calls == ["30d", "3m"]
    assert len(staged_calls) == 8
    assert all(recs is not None and len(recs) == 1 and recs[0].ticker == "MSFT" for _, recs in staged_calls)

