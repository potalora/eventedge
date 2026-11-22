"""Comprehensive gen_003 regression tests for signals, committee, policy, risk, and execution."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.strategies.execution import (
    AccountState,
    Fill,
    MarketBar,
    OrderIntent,
    SignalRecord,
    stable_id,
)
from tradingagents.strategies.execution.cost_model import PaperCostModel
from tradingagents.strategies.modules import get_paper_trade_strategies
from tradingagents.strategies.modules.base import Candidate
from tradingagents.strategies.modules.commodity_macro import CommodityMacroStrategy
from tradingagents.strategies.modules.earnings_call import (
    EarningsCallStrategy,
    prioritize_earnings_candidates,
)
from tradingagents.strategies.modules.govt_contracts import (
    CROSSWALK_SHA256,
    DEFAULT_CROSSWALK_PATH,
    GovtContractsStrategy,
    load_govt_contracts_crosswalk,
    resolve_govt_contract_issuer,
)
from tradingagents.strategies.modules.supply_chain import (
    DEFAULT_SUPPLY_CHAIN_UNIVERSE,
    SupplyChainStrategy,
    get_supply_chain_universe,
)
from tradingagents.strategies.modules.weather_ag import AG_TICKERS_FULL, WeatherAgStrategy
from tradingagents.strategies.orchestration.cohort_orchestrator import SIZE_PROFILES
from tradingagents.strategies.state.portfolio_ledger import PortfolioLedger
from tradingagents.strategies.trading.execution_bridge import ExecutionBridge
from tradingagents.strategies.trading.portfolio_committee import (
    PortfolioCommittee,
    TradeRecommendation,
)
from tradingagents.strategies.trading.portfolio_policy import (
    PortfolioPolicy,
    PortfolioPolicyConfig,
    PortfolioRiskContext,
)
from tradingagents.strategies.trading.risk_gate import RiskGate, RiskGateConfig

UTC = timezone.utc
FRIDAY = date(2026, 7, 31)
MONDAY = date(2026, 8, 3)


class _TrimAwareLedger(PortfolioLedger):
    """Test subclass mirroring Wave 1 @ledger-metrics trimmed-entry _validate_fill contract."""

    def _validate_fill(self, intent: OrderIntent, fill: Fill) -> None:
        if (
            intent.price_rule == "next_session_open"
            and intent.side in {"buy", "short"}
            and 0 < fill.quantity < intent.requested_qty
        ):
            super()._validate_fill(replace(intent, requested_qty=fill.quantity), fill)
            return
        super()._validate_fill(intent, fill)


# ---------------------------------------------------------------------------
# Deliverable 1: Strategy Module Fixes
# ---------------------------------------------------------------------------


def test_active_strategies_have_no_openbb_or_retired_sources():
    strategies = get_paper_trade_strategies()
    assert len(strategies) == 5
    for strat in strategies:
        assert "openbb" not in strat.data_sources
        assert "regulations" not in strat.data_sources
        assert "commodity_futures_curves" not in strat.data_sources
        assert not hasattr(strat, "get_universe")


def test_earnings_call_prioritization_not_alphabetical_and_retains_numeric_only():
    events = [
        {
            "symbol": "AAAA",
            "year": 2026,
            "quarter": 3,
            "eps_actual": 1.05,
            "eps_estimate": 1.00,  # +5% surprise
            "market_cap": 1_000_000_000,
            "transcript_text": "Modest beat headline",
            "news_count": 3,
        },
        {
            "symbol": "ZBRA",
            "year": 2026,
            "quarter": 3,
            "eps_actual": 2.90,
            "eps_estimate": 2.00,  # +45% surprise, zero news!
            "market_cap": 20_000_000_000,
            "transcript_text": "",
            "news_count": 0,
        },
        {
            "symbol": "MSFT",
            "year": 2026,
            "quarter": 3,
            "eps_actual": 3.45,
            "eps_estimate": 3.00,  # +15% surprise, huge market cap
            "market_cap": 3_000_000_000_000,
            "transcript_text": "",
            "news_count": 0,
        },
        {
            "symbol": "AAPL",
            "year": 2026,
            "quarter": 3,
            "eps_actual": 1.15,
            "eps_estimate": 1.00,  # +15% surprise, smaller market cap than MSFT
            "market_cap": 2_000_000_000_000,
            "transcript_text": "",
            "news_count": 0,
        },
    ]

    ranked = prioritize_earnings_candidates(events)
    assert [e["symbol"] for e in ranked] == ["ZBRA", "MSFT", "AAPL", "AAAA"]
    # Input order invariance
    assert [e["symbol"] for e in prioritize_earnings_candidates(reversed(events))] == [
        "ZBRA",
        "MSFT",
        "AAPL",
        "AAAA",
    ]

    strat = EarningsCallStrategy()
    admitted = strat.screen(
        {"finnhub": {"transcripts": events}},
        "2026-10-15",
        {"analysis_budget": 2},
    )
    assert [c.ticker for c in admitted] == ["ZBRA", "MSFT"]
    # Numeric-surprise-only candidates are retained with explicit metadata
    assert admitted[0].metadata["evidence_mode"] == "numeric_surprise_only"
    assert admitted[0].metadata["news_count"] == 0
    assert admitted[0].metadata["has_news_text"] is False
    assert admitted[0].metadata["needs_llm_analysis"] is False
    assert admitted[0].raw_signal_score == admitted[0].score
    assert admitted[0].raw_signal_score > 0.9


def test_supply_chain_check_exit_distinguishes_stop_loss_from_take_profit():
    strat = SupplyChainStrategy()
    params = strat.get_default_params("30d")

    # Long loss -> stop_loss (previously mislabeled "take_profit"!)
    assert strat.check_exit("AAPL", 100.0, 91.0, 5, params, {}, direction="long") == (
        True,
        "stop_loss",
    )
    # Long gain -> take_profit
    assert strat.check_exit("AAPL", 100.0, 109.0, 5, params, {}, direction="long") == (
        True,
        "take_profit",
    )
    # Short loss (price rose +9%) -> stop_loss
    assert strat.check_exit("AAPL", 100.0, 109.0, 5, params, {}, direction="short") == (
        True,
        "stop_loss",
    )
    # Short gain (price dropped -9%) -> take_profit
    assert strat.check_exit("AAPL", 100.0, 91.0, 5, params, {}, direction="short") == (
        True,
        "take_profit",
    )
    # Within bounds -> hold
    assert strat.check_exit("AAPL", 100.0, 95.0, 5, params, {}, direction="long") == (
        False,
        "",
    )


def test_supply_chain_universe_externalized_and_configurable():
    assert len(DEFAULT_SUPPLY_CHAIN_UNIVERSE) >= 20
    assert {"AAPL", "TSLA", "NVDA", "AMZN", "BA", "CAT", "DE"} <= set(
        DEFAULT_SUPPLY_CHAIN_UNIVERSE
    )
    assert "GOOGL" not in DEFAULT_SUPPLY_CHAIN_UNIVERSE
    assert "GOOG" not in DEFAULT_SUPPLY_CHAIN_UNIVERSE

    defaults = SupplyChainStrategy().get_default_params("30d")
    assert defaults["supply_chain_universe"] == list(DEFAULT_SUPPLY_CHAIN_UNIVERSE)
    assert get_supply_chain_universe({"supply_chain_universe": ["hon", "GE", "HON"]}) == [
        "HON",
        "GE",
    ]


def test_govt_contracts_hash_pinned_crosswalk_and_exact_legal_names(tmp_path):
    cw = load_govt_contracts_crosswalk()
    assert cw["_crosswalk_sha256"] == CROSSWALK_SHA256
    assert len(cw["verified_uei_issuers"]) == 16
    assert len(cw["verified_uei_no_listed_target"]) >= 19

    # Tampered crosswalk file fails closed on SHA-256 mismatch
    tampered = tmp_path / "tampered.json"
    doc = json.loads(DEFAULT_CROSSWALK_PATH.read_text(encoding="utf-8"))
    doc["verified_legal_name_issuers"]["FAKE CORP"] = {
        "ticker": "FAKE",
        "cik": "0000000001",
        "issuer_name": "FAKE CORP",
        "reviewed_on": "2026-11-21",
        "provenance": [],
    }
    tampered.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        load_govt_contracts_crosswalk(tampered)

    # Major listed contractors resolve via exact legal name when native UEI is present
    for legal_name, expected_ticker in [
        ("RTX CORPORATION", "RTX"),
        ("NORTHROP GRUMMAN SYSTEMS CORPORATION", "NOC"),
        ("L3HARRIS TECHNOLOGIES, INC.", "LHX"),
        ("HUNTINGTON INGALLS INDUSTRIES, INC.", "HII"),
        ("SCIENCE APPLICATIONS INTERNATIONAL CORPORATION", "SAIC"),
        ("CACI, INC. - FEDERAL", "CACI"),
        ("BOOZ ALLEN HAMILTON INC.", "BAH"),
        ("KRATOS DEFENSE & SECURITY SOLUTIONS, INC.", "KTOS"),
        ("PALANTIR USG, INC.", "PLTR"),
        ("MICROSOFT CORPORATION", "MSFT"),
        ("AMAZON WEB SERVICES, INC.", "AMZN"),
    ]:
        res = resolve_govt_contract_issuer(
            {
                "recipient_uei": "ABC123DEF456",
                "recipient_name": legal_name,
                "recipient_identity_status": "search_recipient",
            }
        )
        assert res["verified"] is True
        assert res["ticker"] == expected_ticker
        assert res["match_method"] == "verified_legal_name_exact"

    # Strict attribution hygiene: substring matches & name-only rows stay unresolved
    for bad_contract in [
        {
            "recipient_uei": "ABC123DEF456",
            "recipient_name": "RAYTHEON SUBCONTRACTOR SERVICES LLC",
            "recipient_identity_status": "search_recipient",
        },
        {
            "recipient_uei": "",
            "recipient_name": "MICROSOFT CORPORATION",
            "recipient_identity_status": "missing_native_recipient",
        },
        {
            "recipient_uei": "ABC123DEF456",
            "recipient_name": "PALANTIR TECHNOLOGIES INC.",
            "recipient_identity_status": "lookup_failed",
        },
    ]:
        res = resolve_govt_contract_issuer(bad_contract)
        assert res["verified"] is False
        assert res["status"] == "unresolved"


def test_weather_ag_handles_noaa_off_season_skipped_cleanly():
    dates = pd.bdate_range("2025-10-01", periods=45)
    prices = {
        ticker: pd.DataFrame({"Close": [100 + i * 0.5 for i in range(45)]}, index=dates)
        for ticker in AG_TICKERS_FULL.values()
    }
    drought = {"composite_score": 1.6, "states": {"IA": {"D2": 25}}}
    usda = {
        "crop_progress": {
            "WINTER_WHEAT": [
                {"week_ending": "2025-11-02", "state": "KS", "good_pct": 50, "excellent_pct": 10},
                {"week_ending": "2025-11-09", "state": "KS", "good_pct": 44, "excellent_pct": 10},
            ]
        }
    }
    noaa_off_season = {
        "off_season_skipped": True,
        "_coverage": {"status": "ok", "reason": "outside_growing_season"},
    }
    strat = WeatherAgStrategy()
    # Oct-Mar: clean & actionable (journal_only=False)
    winter_candidates = strat.screen(
        {
            "yfinance": {"prices": prices},
            "noaa": noaa_off_season,
            "drought_monitor": drought,
            "usda": usda,
        },
        "2025-11-15",
        strat.get_default_params("30d"),
    )
    assert len(winter_candidates) > 0
    assert all(c.journal_only is False for c in winter_candidates)
    assert all(c.metadata["noaa_off_season_skipped"] is True for c in winter_candidates)

    # Apr-Sep: off_season_skipped during growing season fails closed (journal_only=True)
    summer_dates = pd.bdate_range("2025-05-01", periods=45)
    summer_prices = {
        ticker: pd.DataFrame({"Close": [100 + i * 0.5 for i in range(45)]}, index=summer_dates)
        for ticker in AG_TICKERS_FULL.values()
    }
    summer_candidates = strat.screen(
        {
            "yfinance": {"prices": summer_prices},
            "noaa": noaa_off_season,
            "drought_monitor": drought,
            "usda": {
                "crop_progress": {
                    "CORN": [
                        {"week_ending": "2025-06-08", "state": "IA", "good_pct": 55, "excellent_pct": 15},
                        {"week_ending": "2025-06-15", "state": "IA", "good_pct": 50, "excellent_pct": 12},
                    ]
                }
            },
        },
        "2025-06-20",
        strat.get_default_params("30d"),
    )
    assert len(summer_candidates) > 0
    assert all(c.journal_only is True for c in summer_candidates)


def test_commodity_macro_ignores_retired_regulations_uses_finnhub_catalyst():
    strat = CommodityMacroStrategy()
    cot = {
        "gold": {
            "net_position": 100_000,
            "percentile": 0.92,
            "direction_signal": "short",
            "report_id": "CFTC:gold:2026-03-27",
            "window_end": "2026-03-27",
        }
    }
    fred = {"VIXCLS": {pd.Timestamp("2026-03-27"): 22.0}}
    params = strat.get_default_params("3m")

    with_regs_only = strat.screen(
        {"cftc": cot, "fred": fred, "regulations": {"results": [{"title": "gold rule"}]}, "finnhub": {}},
        "2026-04-01",
        params,
    )
    with_finnhub = strat.screen(
        {
            "cftc": cot,
            "fred": fred,
            "finnhub": {"news": [{"headline": "Major gold supply squeeze reported"}]},
        },
        "2026-04-01",
        params,
    )
    assert with_regs_only[0].metadata["catalyst_found"] is False
    assert with_finnhub[0].metadata["catalyst_found"] is True
    assert with_finnhub[0].score > with_regs_only[0].score


# ---------------------------------------------------------------------------
# Deliverable 2: Signal Preservation & PortfolioCommittee
# ---------------------------------------------------------------------------


def test_candidate_separates_raw_signal_score_and_llm_conviction():
    c = Candidate(ticker="NVDA", date="2026-10-15", score=0.92)
    assert c.raw_signal_score == pytest.approx(0.92)
    assert c.llm_conviction is None

    c.set_llm_conviction(0.78)
    assert c.llm_conviction == pytest.approx(0.78)
    assert c.raw_signal_score == pytest.approx(0.92)  # Never overwritten!

    with pytest.raises(ValueError):
        c.set_llm_conviction(1.5)


def test_committee_fixes_score_2_prompt_and_strips_provenance_noise():
    committee = PortfolioCommittee(
        {"autoresearch": {"paper_trade": {"portfolio_committee_enabled": True}}}
    )
    assert not hasattr(committee, "generate_covered_call_overlays")
    assert not hasattr(committee, "_llm_covered_call_overlay")

    noisy_signal = {
        "ticker": "MSFT",
        "direction": "long",
        "score": 0.85,
        "raw_signal_score": 0.85,
        "llm_conviction": 0.76,
        "long_count": 2,
        "short_count": 1,
        "strategy": "earnings_call",
        "event_key": "event_key_secret_internal",
        "metadata": {
            "eps_surprise_pct": 14.2,
            "thesis": "Strong cloud beat and raised guidance",
            "research_admission": {"huge": "blob"},
            "model_provenance": {"response_id": "resp_12345"},
            "equity_universe": {"assets_sha256": "deadbeef"},
            "discovery_id": "discovery_abc123",
            "analysis_admitted": True,
        },
    }
    captured: dict[str, str] = {}

    def fake_call(*, system, prompt, max_tokens):
        captured["system"] = system
        captured["prompt"] = prompt
        return json.dumps(
            [
                {
                    "ticker": "MSFT",
                    "direction": "long",
                    "confidence": 0.78,
                    "rationale": "Strong cloud EPS beat",
                    "contributing_strategies": ["earnings_call"],
                    "regime_alignment": "aligned",
                }
            ]
        )

    with (
        patch.object(committee, "_get_client", return_value=object()),
        patch.object(committee, "_call_llm", side_effect=fake_call) as mock_call,
    ):
        recs = committee.synthesize_horizon(
            "30d",
            [noisy_signal],
            regime={"overall_regime": "benign", "vix_level": 14.5},
            enrichment={"annualized_volatility": {"MSFT": 0.24}},
        )

    assert mock_call.call_count == 1
    assert "2.0" not in captured["system"]
    assert "0.65" in captured["system"]
    assert "position_size_pct" not in captured["system"]

    # Clean economic fields present; internal provenance stripped
    prompt = captured["prompt"]
    for econ in (
        "raw_signal_score",
        "llm_conviction",
        "bull_signal_count",
        "bear_signal_count",
        "Strong cloud beat",
        "realized_volatility",
        "30d",
    ):
        assert econ in prompt
    for noise in (
        "research_admission",
        "model_provenance",
        "equity_universe",
        "discovery_id",
        "event_key_secret_internal",
    ):
        assert noise not in prompt

    assert len(recs) == 1
    assert recs[0].ticker == "MSFT"
    assert recs[0].position_size_pct == 0.0  # Size-agnostic before PortfolioPolicy


def test_rule_based_single_strategy_long_passes_at_realistic_conviction():
    committee = PortfolioCommittee(
        {"autoresearch": {"paper_trade": {"portfolio_committee_enabled": False}}}
    )
    # Without explicit strategy_confidence, realistic 0.70 long passes (no 0.5x halving cliff!)
    recs = committee.synthesize_horizon(
        "30d",
        [
            {
                "ticker": "MSFT",
                "direction": "long",
                "score": 0.70,
                "raw_signal_score": 0.70,
                "llm_conviction": 0.70,
                "strategy": "earnings_call",
                "metadata": {},
            },
            {
                "ticker": "WEAK",
                "direction": "long",
                "score": 0.50,
                "raw_signal_score": 0.50,
                "llm_conviction": 0.50,
                "strategy": "earnings_call",
                "metadata": {},
            },
        ],
    )
    assert [r.ticker for r in recs] == ["MSFT"]
    assert recs[0].position_size_pct == 0.0


def test_one_horizon_synthesis_call_sizes_deterministically_across_four_tiers():
    committee = PortfolioCommittee(
        {"autoresearch": {"paper_trade": {"portfolio_committee_enabled": True}}}
    )
    signals = [
        {
            "ticker": "MSFT",
            "direction": "long",
            "score": 0.85,
            "raw_signal_score": 0.85,
            "llm_conviction": 0.80,
            "strategy": "earnings_call",
            "event_key": "ev-msft",
            "metadata": {},
        },
        {
            "ticker": "BA",
            "direction": "short",
            "score": 0.70,
            "raw_signal_score": 0.70,
            "llm_conviction": 0.75,
            "strategy": "supply_chain",
            "event_key": "ev-ba",
            "metadata": {},
        },
    ]
    llm_payload = json.dumps(
        [
            {
                "ticker": "MSFT",
                "direction": "long",
                "confidence": 0.80,
                "rationale": "Strong earnings beat",
                "contributing_strategies": ["earnings_call"],
                "regime_alignment": "aligned",
            },
            {
                "ticker": "BA",
                "direction": "short",
                "confidence": 0.75,
                "rationale": "Factory disruption",
                "contributing_strategies": ["supply_chain"],
                "regime_alignment": "neutral",
            },
        ]
    )
    with (
        patch.object(committee, "_get_client", return_value=object()),
        patch.object(committee, "_call_llm", return_value=llm_payload) as mock_call,
    ):
        horizon_recs = committee.synthesize_horizon("30d", signals)

    assert mock_call.call_count == 1  # ONE call shared across all 4 size tiers!
    assert [r.ticker for r in horizon_recs] == ["MSFT", "BA"]

    policy = PortfolioPolicy()
    policy_settings = DEFAULT_CONFIG["autoresearch"]["portfolio_policy"]
    tier_results = {}
    for size in ("5k", "10k", "50k", "100k"):
        profile = SIZE_PROFILES[size]
        cfg = PortfolioPolicyConfig.from_size_profile(profile, policy_settings)
        ctx = PortfolioRiskContext(
            portfolio_value=profile.total_capital,
            cash=profile.total_capital,
            positions=(),
            pending_positions=(),
            sectors={"MSFT": "Technology", "BA": "Industrials"},
            annualized_volatility={"MSFT": 0.25, "BA": 0.30},
            earnings_dates={},
            short_interest={},
            borrow_available={"BA": True},
            margin_used=0.0,
            consumed_event_keys=frozenset(),
            config=cfg,
        )
        tier_results[size] = policy.apply(horizon_recs, ctx)

    # 5k & 10k are long-only -> BA short rejected deterministically by PortfolioPolicy
    assert [r.ticker for r in tier_results["5k"]] == ["MSFT"]
    assert [r.ticker for r in tier_results["10k"]] == ["MSFT"]
    # 50k & 100k are short-eligible -> both MSFT long & BA short accepted & sized
    assert [r.ticker for r in tier_results["50k"]] == ["MSFT", "BA"]
    assert [r.ticker for r in tier_results["100k"]] == ["MSFT", "BA"]
    # Deterministic inverse-vol weight for MSFT on 100k: 0.08 * (0.15 / 0.25) * 0.80 = 0.0384
    assert tier_results["100k"][0].position_size_pct == pytest.approx(0.0384)


# ---------------------------------------------------------------------------
# Deliverable 3: Continuous Vol Sizing, T+1 Open Trimming, Unified Risk/Policy
# ---------------------------------------------------------------------------


def test_t1_open_trim_on_moderate_gap_and_slippage_vs_reject_on_excessive_chase(tmp_path):
    config = {
        "execution": {"mode": "paper"},
        "autoresearch": {
            "total_capital": 5000,
            "risk_gate": {
                "long_only": False,
                "min_position_value": 100,
                "max_position_pct": 0.20,  # $1,000 cap on $5,000
            },
            "portfolio_policy": {
                "version": "portfolio_policy_v2",
                "annualized_volatility_floor": 0.15,
                "volatility_lookback_sessions": 60,
                "options_overlays_enabled": False,
                "max_open_chase_pct": 0.05,
            },
            "short_selling": {"borrow_cost_reject_above": "0.05"},
        },
    }
    ledger = _TrimAwareLedger(tmp_path / "trim.db", "cohort", Decimal("5000"))
    try:
        bridge = ExecutionBridge(config, ledger=ledger)
        assert not hasattr(bridge, "broker")  # PaperBroker / adapter removed!
        assert not hasattr(bridge.risk_gate, "compute_position_size")
        assert not hasattr(bridge.risk_gate, "enforce_stop_losses")

        sig = SignalRecord(
            "sig-trim",
            "epoch",
            "policy",
            "ev-trim",
            "earnings_call",
            "AAPL",
            "long",
            datetime(2026, 7, 31, 19, tzinfo=UTC),
            datetime(2026, 7, 31, 19, 30, tzinfo=UTC),
            FRIDAY,
            Decimal("100"),
            datetime(2026, 7, 31, 20, tzinfo=UTC),
            "ev-hash-1",
        )
        ledger.record_signal(sig)
        # Stage exact 20% ($1,000 = 10 shares @ $100)
        intent = bridge.stage_intent(
            TradeRecommendation("AAPL", "long", 0.20, 0.85, "beat", ["earnings_call"]),
            (sig,),
            ledger.account_state(),
            sig.decision_at,
            MONDAY,
        )
        assert intent.requested_qty == 10

        # T+1 open gaps +3% to $103 (<= 5% max_open_chase_pct):
        # 10 * $103.103 = $1,031.03 > $1,000 -> trims deterministically to 9 shares!
        bar_3pct = MarketBar(
            "AAPL",
            MONDAY,
            Decimal("103"),
            Decimal("105"),
            Decimal("102"),
            Decimal("104"),
            "fixture",
            datetime(2026, 8, 3, 22, tzinfo=UTC),
            False,
        )
        result = bridge.execute_due_intent(
            intent,
            bar_3pct,
            ledger.account_state(),
            {
                "processing_at": datetime(2026, 8, 3, 22, tzinfo=UTC),
                "opening_prices": {"AAPL": Decimal("103")},
            },
            PaperCostModel(),
        )
        assert result.status == "filled"
        assert result.fill is not None
        assert result.fill.quantity == 9
        assert "trimmed_at_open:requested=10,filled=9" in result.reason
        assert bridge.last_open_trim is not None
        assert bridge.last_open_trim["trimmed_at_open"] is True
        assert bridge.last_open_trim["requested_qty"] == 10
        assert bridge.last_open_trim["filled_qty"] == 9
        # Direct ledger queries (no PaperBroker)
        assert bridge.get_positions()[0]["ticker"] == "AAPL"
        assert bridge.get_positions()[0]["quantity"] == 9
        assert bridge.get_account().cash < 5000.0
    finally:
        ledger.close()

    # Case 2: Overnight gap +7% (> 5% max_open_chase_pct) -> REJECTED!
    ledger2 = _TrimAwareLedger(tmp_path / "chase.db", "cohort", Decimal("5000"))
    try:
        bridge2 = ExecutionBridge(config, ledger=ledger2)
        sig2 = SignalRecord(
            "sig-chase",
            "epoch",
            "policy",
            "ev-chase",
            "earnings_call",
            "MSFT",
            "long",
            datetime(2026, 7, 31, 19, tzinfo=UTC),
            datetime(2026, 7, 31, 19, 30, tzinfo=UTC),
            FRIDAY,
            Decimal("100"),
            datetime(2026, 7, 31, 20, tzinfo=UTC),
            "ev-hash-2",
        )
        ledger2.record_signal(sig2)
        intent2 = bridge2.stage_intent(
            TradeRecommendation("MSFT", "long", 0.20, 0.85, "beat", ["earnings_call"]),
            (sig2,),
            ledger2.account_state(),
            sig2.decision_at,
            MONDAY,
        )
        bar_7pct = MarketBar(
            "MSFT",
            MONDAY,
            Decimal("107"),
            Decimal("109"),
            Decimal("106"),
            Decimal("108"),
            "fixture",
            datetime(2026, 8, 3, 22, tzinfo=UTC),
            False,
        )
        res2 = bridge2.execute_due_intent(
            intent2,
            bar_7pct,
            ledger2.account_state(),
            {
                "processing_at": datetime(2026, 8, 3, 22, tzinfo=UTC),
                "opening_prices": {"MSFT": Decimal("107")},
            },
            PaperCostModel(),
        )
        assert res2.status == "rejected"
        assert "max_open_chase_pct" in res2.reason
    finally:
        ledger2.close()


def test_t1_open_rejects_when_trimmed_below_one_share_or_hard_gate_fails(tmp_path):
    config = {
        "execution": {"mode": "paper"},
        "autoresearch": {
            "total_capital": 5000,
            "risk_gate": {
                "long_only": False,
                "min_position_value": 100,
                "max_position_pct": 0.20,  # $1,000 cap
            },
            "portfolio_policy": {
                "version": "portfolio_policy_v2",
                "annualized_volatility_floor": 0.15,
                "volatility_lookback_sessions": 60,
                "options_overlays_enabled": False,
                "max_open_chase_pct": 0.05,
            },
            "short_selling": {"borrow_cost_reject_above": "0.05"},
        },
    }
    ledger = _TrimAwareLedger(tmp_path / "one_share.db", "cohort", Decimal("5000"))
    try:
        bridge = ExecutionBridge(config, ledger=ledger)
        sig = SignalRecord(
            "sig-one",
            "epoch",
            "policy",
            "ev-one",
            "earnings_call",
            "BKNG",
            "long",
            datetime(2026, 7, 31, 19, tzinfo=UTC),
            datetime(2026, 7, 31, 19, 30, tzinfo=UTC),
            FRIDAY,
            Decimal("990"),
            datetime(2026, 7, 31, 20, tzinfo=UTC),
            "ev-hash-one",
        )
        ledger.record_signal(sig)
        intent = bridge.stage_intent(
            TradeRecommendation("BKNG", "long", 0.20, 0.85, "beat", ["earnings_call"]),
            (sig,),
            ledger.account_state(),
            sig.decision_at,
            MONDAY,
        )
        assert intent.requested_qty == 1
        # +2% gap -> $1,010, floor($999 / $1,011) = 0 < 1 -> REJECTED!
        bar = MarketBar(
            "BKNG",
            MONDAY,
            Decimal("1010"),
            Decimal("1020"),
            Decimal("1005"),
            Decimal("1015"),
            "fixture",
            datetime(2026, 8, 3, 22, tzinfo=UTC),
            False,
        )
        res = bridge.execute_due_intent(
            intent,
            bar,
            ledger.account_state(),
            {
                "processing_at": datetime(2026, 8, 3, 22, tzinfo=UTC),
                "opening_prices": {"BKNG": Decimal("1010")},
            },
            PaperCostModel(),
        )
        assert res.status == "rejected"
    finally:
        ledger.close()


def test_policy_validate_rejects_excessive_overnight_gap():
    cfg = PortfolioPolicyConfig.from_size_profile(
        SIZE_PROFILES["50k"], DEFAULT_CONFIG["autoresearch"]["portfolio_policy"]
    )
    ctx = PortfolioRiskContext(
        portfolio_value=50_000.0,
        cash=50_000.0,
        positions=(),
        pending_positions=(),
        sectors={"MSFT": "Technology"},
        annualized_volatility={"MSFT": 0.15},
        earnings_dates={},
        short_interest={},
        borrow_available={},
        margin_used=0.0,
        consumed_event_keys=frozenset(),
        config=cfg,
    )
    rec = TradeRecommendation("MSFT", "long", 0.08, 0.8, "beat", ["earnings_call"])
    assert PortfolioPolicy().validate(rec, ctx, overnight_gap_pct=0.04) == (True, "")
    assert PortfolioPolicy().validate(rec, ctx, overnight_gap_pct=0.06) == (
        False,
        "max_open_chase_pct",
    )


def test_unified_policy_and_risk_gate_do_not_duplicate_portfolio_checks():
    cfg = PortfolioPolicyConfig.from_size_profile(
        SIZE_PROFILES["50k"], DEFAULT_CONFIG["autoresearch"]["portfolio_policy"]
    )
    ctx = PortfolioRiskContext(
        portfolio_value=50_000.0,
        cash=50_000.0,
        positions=(),
        pending_positions=(),
        sectors={"MSFT": "Technology"},
        annualized_volatility={"MSFT": 0.15},
        earnings_dates={},
        short_interest={},
        borrow_available={},
        margin_used=0.0,
        consumed_event_keys=frozenset(),
        config=cfg,
    )
    # RiskGate has stricter legacy max_position_pct=0.05, while PortfolioPolicy allows 0.10.
    # With policy_enabled=True, PortfolioPolicy is the single authority for max_position_pct.
    gate = RiskGate(
        RiskGateConfig(
            total_capital=50_000.0,
            max_positions=1,
            max_position_pct=0.05,
            min_position_value=100.0,
            cash_reserve_pct=0.50,
            long_only=True,
        )
    )
    acct = AccountState(
        cohort_id="50k",
        cash=Decimal("50000"),
        long_market_value=Decimal("0"),
        short_liability=Decimal("0"),
        margin_used=Decimal("0"),
        buying_power=Decimal("50000"),
        net_equity=Decimal("50000"),
        high_water_mark=Decimal("50000"),
    )
    rec = TradeRecommendation("MSFT", "long", 0.08, 0.8, "beat", ["earnings_call"])
    passed, reason = gate.check(
        "MSFT",
        "long",
        4_000.0,
        ("earnings_call",),
        authoritative_account=acct,
        policy_enabled=True,
        recommendation=rec,
        portfolio_context=ctx,
    )
    assert passed is True and reason == ""

    # Hard risk gate (cooldown) still enforced when policy_enabled=True!
    gate.set_cooling_tickers({"MSFT"})
    passed_cd, reason_cd = gate.check(
        "MSFT",
        "long",
        4_000.0,
        ("earnings_call",),
        authoritative_account=acct,
        policy_enabled=True,
        recommendation=rec,
        portfolio_context=ctx,
    )
    assert passed_cd is False and "cooldown" in reason_cd
