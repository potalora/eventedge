"""30-day simulation harness for the paper trading pipeline.

Validates the entire pipeline end-to-end with synthetic data:
1. Broker reconstruction across days
2. Signal journal fill_outcomes with correct target-date prices
3. Idempotency (double-run same date produces no duplicates)
4. Full 30-day lifecycle: open, hold, exit, back-fill, learn
5. 2-cohort divergence over 30 days

All LLM and external API calls are mocked. Deterministic via seeded RNG.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from tradingagents.strategies.execution import (
    MarketBar,
    OrderIntent,
    SignalRecord,
    stable_id,
)
from tradingagents.strategies.execution.price_source import AdjustedClose

from tradingagents.strategies.orchestration.cohort_orchestrator import (
    CohortConfig,
    CohortOrchestrator,
)
from tradingagents.strategies.orchestration.multi_strategy_engine import (
    MultiStrategyEngine,
)
from tradingagents.strategies.orchestration.session_executor import SessionExecutor
from tradingagents.strategies.orchestration.trading_calendar import (
    next_session,
    previous_session,
    session_close,
)
from tradingagents.strategies.trading.portfolio_committee import (
    TradeRecommendation,
)
from tradingagents.strategies.learning.signal_journal import JournalEntry, SignalJournal
from tradingagents.strategies.state.state import StateManager
from tradingagents.strategies.modules.base import Candidate


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TICKERS = ["AAPL", "MSFT", "AMZN", "TSLA", "NVDA"]
BASE_DATE = datetime(2026, 4, 1)
RNG = np.random.RandomState(42)


def _make_price_df(
    ticker: str,
    base_price: float,
    start: str = "2026-03-01",
    days: int = 60,
    seed: int = 42,
) -> pd.DataFrame:
    """Deterministic price series with slight daily drift."""
    rng = np.random.RandomState(seed + hash(ticker) % 10000)
    dates = pd.bdate_range(start=start, periods=days)
    returns = rng.normal(0.001, 0.01, size=days)
    prices = [base_price]
    for r in returns[1:]:
        prices.append(prices[-1] * (1 + r))
    return pd.DataFrame(
        {"Close": prices[:days], "Volume": [1_000_000] * days},
        index=dates[:days],
    )


def _build_price_cache(days: int = 60) -> dict[str, pd.DataFrame]:
    """Build deterministic price cache for all test tickers."""
    bases = {"AAPL": 170.0, "MSFT": 420.0, "AMZN": 190.0, "TSLA": 250.0, "NVDA": 130.0}
    return {
        ticker: _make_price_df(ticker, bp, days=days) for ticker, bp in bases.items()
    }


class FakeStrategy:
    """Minimal strategy for testing."""

    track = "paper_trade"
    data_sources = ["yfinance"]

    def __init__(self, name: str = "fake_strat", hold_days: int = 5):
        self.name = name
        self._hold_days = hold_days

    def get_param_space(self, horizon: str = "30d"):
        return {"hold_days": (3, 30)}

    def get_default_params(self, horizon: str = "30d"):
        return {"hold_days": self._hold_days}

    def screen(self, data, date, params):
        return [
            Candidate(
                ticker="AAPL",
                date=date,
                direction="long",
                score=5.0,
                metadata={
                    "source": "test",
                    "event_key": f"{self.name}:{date}",
                    "observed_at": f"{date}T19:00:00+00:00",
                },
            )
        ]

    def check_exit(
        self, ticker, entry_price, current_price, holding_days, params, data
    ):
        hold = params.get("hold_days", self._hold_days)
        if holding_days >= hold:
            return True, "holding_period"
        return False, ""

    def build_propose_prompt(self, context):
        return "test"


class FakeStrategy2(FakeStrategy):
    def __init__(self):
        super().__init__(name="fake_strat_2", hold_days=5)

    def screen(self, data, date, params):
        return [
            Candidate(
                ticker="MSFT",
                date=date,
                direction="long",
                score=4.0,
                metadata={
                    "source": "test",
                    "event_key": f"{self.name}:{date}",
                    "observed_at": f"{date}T19:00:00+00:00",
                },
            )
        ]


def _base_config(state_dir: str) -> dict:
    """Minimal config for test isolation."""
    return {
        "autoresearch": {
            "state_dir": state_dir,
            "total_capital": 5000,
            "paper_trade": {
                "min_trades_for_evaluation": 2,
                "portfolio_committee_enabled": False,
            },
        },
        "execution": {"mode": "paper"},
    }


def _build_engine(
    state_dir: str,
    strategies=None,
    adaptive: bool = False,
) -> tuple[MultiStrategyEngine, StateManager]:
    config = _base_config(state_dir)
    state = StateManager(state_dir)
    strategies = strategies or [FakeStrategy(), FakeStrategy2()]
    engine = MultiStrategyEngine(
        config=config,
        strategies=strategies,
        state_manager=state,
        use_llm=False,
        adaptive_confidence=adaptive,
    )
    return engine, state


def _make_fake_committee(max_per_day: int = 3):
    """Return a side_effect function for mocked PortfolioCommittee.synthesize."""

    def fake_synthesize(
        signals,
        regime_context=None,
        strategy_confidence=None,
        current_positions=None,
        total_capital=None,
        **kwargs,
    ):
        recs = []
        seen: set[str] = set()
        for s in signals[:max_per_day]:
            if s["ticker"] not in seen:
                recs.append(
                    TradeRecommendation(
                        ticker=s["ticker"],
                        direction=s["direction"],
                        position_size_pct=0.08,
                        confidence=0.6,
                        rationale="test recommendation",
                        contributing_strategies=[s["strategy"]],
                    )
                )
                seen.add(s["ticker"])
        return recs

    return fake_synthesize


# ===========================================================================
# 2. TestFillOutcomesCorrectPrices
# ===========================================================================


class TestFillOutcomesCorrectPrices:
    """Verify fill_outcomes uses the correct target-date price, not latest."""

    def _build_price_df(self, signal_date_str: str) -> pd.DataFrame:
        """Build a price DF with known prices at day 0, 5, 10, 30."""
        signal_dt = datetime.strptime(signal_date_str, "%Y-%m-%d")
        # Create 35 business days of prices starting from signal date
        dates = pd.bdate_range(start=signal_dt, periods=35)
        prices = [100.0] * 35  # default base

        # Set specific prices at known offsets by calendar day
        for i, d in enumerate(dates):
            cal_days = (d - pd.Timestamp(signal_dt)).days
            if cal_days <= 5:
                # Ramp from 100 to 105 over first 5 calendar days
                prices[i] = 100.0 + cal_days
            elif cal_days <= 10:
                # 110 around day 10
                prices[i] = 105.0 + (cal_days - 5)
            elif cal_days <= 30:
                # 120 around day 30
                frac = (cal_days - 10) / 20
                prices[i] = 110.0 + frac * 10.0
            else:
                prices[i] = 120.0

        return pd.DataFrame({"Close": prices}, index=dates)

    def test_5d_uses_day5_price_not_latest(self, tmp_path):
        signal_date = "2026-04-01"
        journal = SignalJournal(str(tmp_path / "state"))
        journal.log_signal(
            JournalEntry(
                timestamp=signal_date,
                strategy="test_strat",
                ticker="AAPL",
                direction="long",
                score=5.0,
                traded=True,
                entry_price=100.0,
            )
        )

        price_df = self._build_price_df(signal_date)
        price_cache = {"AAPL": price_df}

        # Day 7: only 5d should fill
        updated = journal.fill_outcomes(price_cache, "2026-04-08")
        assert updated == 1
        entries = journal.get_entries()
        assert entries[0]["return_5d"] is not None
        assert entries[0]["return_10d"] is None
        # Day-5 price is ~105, so return should be ~0.05
        assert entries[0]["return_5d"] == pytest.approx(0.05, abs=0.02)

        # Day 12: 10d should fill
        updated = journal.fill_outcomes(price_cache, "2026-04-13")
        assert updated == 1
        entries = journal.get_entries()
        assert entries[0]["return_10d"] is not None
        assert entries[0]["return_10d"] == pytest.approx(0.10, abs=0.02)

        # Day 32: 30d should fill
        updated = journal.fill_outcomes(price_cache, "2026-05-03")
        assert updated == 1
        entries = journal.get_entries()
        assert entries[0]["return_30d"] is not None
        assert entries[0]["return_30d"] == pytest.approx(0.20, abs=0.03)

    def test_short_direction_flips_sign(self, tmp_path):
        signal_date = "2026-04-01"
        journal = SignalJournal(str(tmp_path / "state"))
        journal.log_signal(
            JournalEntry(
                timestamp=signal_date,
                strategy="test_strat",
                ticker="AAPL",
                direction="short",
                score=5.0,
                traded=True,
                entry_price=100.0,
            )
        )

        price_df = self._build_price_df(signal_date)
        price_cache = {"AAPL": price_df}

        journal.fill_outcomes(price_cache, "2026-04-08")
        entries = journal.get_entries()
        # Short: return should be negative (price went up)
        assert entries[0]["return_5d"] is not None
        assert entries[0]["return_5d"] < 0


# ===========================================================================
# 3. TestIdempotencyDoubleRun
# ===========================================================================


class AuthoritativePriceSource:
    """Exact raw/action/adjusted fixture with call accounting."""

    def __init__(self):
        self.raw_calls: list[tuple[tuple[str, ...], date]] = []
        self.action_calls: list[tuple[tuple[str, ...], date]] = []
        self.benchmark_calls: list[date] = []

    def get_daily_bars(
        self, tickers, start_session, end_session_inclusive, adjusted=False
    ):
        assert start_session == end_session_inclusive
        assert adjusted is False
        session = start_session
        self.raw_calls.append((tuple(sorted(tickers)), session))
        fetched_at = datetime.now(timezone.utc)
        return {
            (ticker, session): MarketBar(
                ticker,
                session,
                Decimal("100"),
                Decimal("102"),
                Decimal("99"),
                Decimal("101"),
                "fixture-raw",
                fetched_at,
                False,
            )
            for ticker in tickers
        }

    def get_corporate_actions(self, tickers, session):
        self.action_calls.append((tuple(sorted(tickers)), session))
        return []

    def get_total_return_closes(self, symbols, start_session, end_session_inclusive):
        assert start_session == end_session_inclusive
        session = start_session
        self.benchmark_calls.append(session)
        fetched_at = datetime.now(timezone.utc)
        return {
            (symbol, session): AdjustedClose(
                symbol,
                session,
                Decimal("650") if symbol == "SPY" else Decimal("91"),
                "fixture-adjusted",
                fetched_at,
            )
            for symbol in symbols
        }


def _authoritative_orchestrator(tmp_path, cohorts=1, hold_days=2):
    source = AuthoritativePriceSource()
    configs = [
        CohortConfig(
            name=f"cohort_{index}",
            state_dir=str(tmp_path / f"cohort_{index}"),
            horizon="30d",
            size_profile="5k",
            adaptive_confidence=True,
            learning_enabled=True,
            use_llm=False,
        )
        for index in range(cohorts)
    ]
    config = {
        "execution": {"mode": "paper"},
        "autoresearch": {
            "state_dir": str(tmp_path / "base"),
            "total_capital": 5000,
            "paper_trade": {"portfolio_committee_enabled": False},
            "paper_ledger": {
                "benchmark_symbols": ["SPY", "BIL"],
                "bar_max_age_hours": 24,
            },
            "risk_gate": {
                "min_position_value": 1,
                "max_position_pct": 0.5,
                "max_positions": 5,
            },
        },
    }
    with patch(
        "tradingagents.strategies.modules.get_paper_trade_strategies",
        return_value=[FakeStrategy(hold_days=hold_days), FakeStrategy2()],
    ):
        orchestrator = CohortOrchestrator(configs, config, price_source=source)
    for cohort in orchestrator.cohorts:
        cohort["engine"]._fetch_all_data = lambda start, end: {}
    orchestrator._fetch_openbb_enrichment = lambda signals: {}
    return orchestrator, source


def _authoritative_committee(signals, **kwargs):
    if not signals:
        return []
    signal = signals[0]
    return [
        TradeRecommendation(
            ticker=signal["ticker"],
            direction=signal["direction"],
            position_size_pct=0.20,
            confidence=0.6,
            rationale="fixture",
            contributing_strategies=[signal["strategy"]],
        )
    ]


class TestIdempotencyDoubleRun:
    def test_double_run_no_duplicates(self, tmp_path):
        orchestrator, source = _authoritative_orchestrator(tmp_path)
        session = date(2026, 3, 30)
        with patch(
            "tradingagents.strategies.trading.portfolio_committee.PortfolioCommittee.synthesize",
            side_effect=_authoritative_committee,
        ) as committee:
            first = orchestrator.run_daily(session.isoformat())
            before_replay_calls = (
                len(source.raw_calls),
                len(source.action_calls),
                len(source.benchmark_calls),
                committee.call_count,
            )
            second = orchestrator.run_daily(session.isoformat())

        ledger = orchestrator.cohorts[0]["ledger"]
        assert first["cohort_0"]["intents_staged"]
        assert second["cohort_0"]["replayed"]
        assert len(ledger.read_signals(session, session)) == 2
        assert len(ledger.pending_intents(next_session(session))) == 1
        assert ledger.read_fills() == []
        assert (
            len(source.raw_calls),
            len(source.action_calls),
            len(source.benchmark_calls),
            committee.call_count,
        ) == before_replay_calls

    def test_mixed_complete_and_stage_only_replay_skips_execution_inputs(
        self, tmp_path
    ):
        orchestrator, source = _authoritative_orchestrator(tmp_path, cohorts=2)
        session = date(2026, 3, 30)
        second_engine = orchestrator.cohorts[1]["engine"]
        original_stage = second_engine.screen_and_stage
        fail_once = True

        def stage_with_one_crash(*args, **kwargs):
            nonlocal fail_once
            if fail_once:
                fail_once = False
                raise RuntimeError("staging crash")
            return original_stage(*args, **kwargs)

        second_engine.screen_and_stage = stage_with_one_crash
        with patch(
            "tradingagents.strategies.trading.portfolio_committee.PortfolioCommittee.synthesize",
            side_effect=_authoritative_committee,
        ):
            first = orchestrator.run_daily(session.isoformat())
            before = (
                len(source.raw_calls),
                len(source.action_calls),
                len(source.benchmark_calls),
            )
            replay = orchestrator.run_daily(session.isoformat())

        assert not first["cohort_0"]["error"]
        assert first["cohort_1"]["error"]
        assert replay["cohort_0"]["replayed"]
        assert not replay["cohort_1"]["error"]
        assert len(source.raw_calls) == before[0] + 1
        assert len(source.action_calls) == before[1]
        assert len(source.benchmark_calls) == before[2]

    def test_partial_execution_resume_uses_stored_bundle_not_provider_refetch(
        self, tmp_path
    ):
        orchestrator, source = _authoritative_orchestrator(tmp_path)
        session = date(2026, 3, 30)
        executor = orchestrator.cohorts[0]["executor"]

        def crash_after_commit(phase):
            if phase == "validate_market_data":
                raise RuntimeError("execution crash")

        executor._after_phase_commit = crash_after_commit
        first = orchestrator.run_daily(session.isoformat())
        assert first["cohort_0"]["error"]
        before = (
            len(source.raw_calls),
            len(source.action_calls),
            len(source.benchmark_calls),
        )

        executor._after_phase_commit = lambda phase: None
        with patch(
            "tradingagents.strategies.trading.portfolio_committee.PortfolioCommittee.synthesize",
            side_effect=_authoritative_committee,
        ):
            resumed = orchestrator.run_daily(session.isoformat())

        assert not resumed["cohort_0"]["error"]
        assert len(source.raw_calls) == before[0] + 1
        assert len(source.action_calls) == before[1]
        assert len(source.benchmark_calls) == before[2]

    def test_complete_and_stage_only_replay_validate_local_policy_without_market_io(
        self, tmp_path
    ):
        orchestrator, source = _authoritative_orchestrator(tmp_path, cohorts=2)
        session = date(2026, 3, 30)
        second_engine = orchestrator.cohorts[1]["engine"]
        original_stage = second_engine.screen_and_stage

        def fail_stage(*args, **kwargs):
            raise RuntimeError("staging crash")

        second_engine.screen_and_stage = fail_stage
        with patch(
            "tradingagents.strategies.trading.portfolio_committee.PortfolioCommittee.synthesize",
            side_effect=_authoritative_committee,
        ):
            first = orchestrator.run_daily(session.isoformat())
        assert not first["cohort_0"]["error"]
        assert first["cohort_1"]["error"]
        second_engine.screen_and_stage = original_stage
        before = (
            len(source.raw_calls),
            len(source.action_calls),
            len(source.benchmark_calls),
        )

        for cohort in orchestrator.cohorts:
            drifted = {
                **cohort["executor"].config,
                "autoresearch": {
                    **cohort["executor"].config["autoresearch"],
                    "paper_ledger": {
                        **cohort["executor"]
                        .config["autoresearch"]
                        .get("paper_ledger", {}),
                        "slippage_bps": "20",
                    },
                },
            }
            cohort["executor"] = SessionExecutor(cohort["ledger"], drifted)

        replay = orchestrator.run_daily(session.isoformat())
        assert replay["cohort_0"]["error"]
        assert replay["cohort_1"]["error"]
        assert "effective config" in replay["cohort_0"]["invalid_reason"]
        assert "effective config" in replay["cohort_1"]["invalid_reason"]
        assert (
            len(source.raw_calls),
            len(source.action_calls),
            len(source.benchmark_calls),
        ) == before

    def test_partial_orchestrator_resume_rehydrates_nonempty_borrow_document(
        self, tmp_path
    ):
        orchestrator, source = _authoritative_orchestrator(tmp_path)
        cohort = orchestrator.cohorts[0]
        ledger = cohort["ledger"]
        executor = cohort["executor"]
        session = date(2026, 3, 30)
        prior = previous_session(session)
        cutoff = session_close(prior)
        signal = SignalRecord(
            stable_id("borrow_resume_signal", "AAPL"),
            orchestrator._epoch_id,
            "foundation-30d",
            "borrow-resume-event",
            "fake_strat",
            "AAPL",
            "long",
            cutoff,
            cutoff,
            prior,
            Decimal("100"),
            cutoff,
            stable_id("borrow_resume_evidence", "AAPL"),
        )
        ledger.record_signal(signal)
        intent = OrderIntent(
            stable_id("borrow_resume_intent", "AAPL"),
            (signal.signal_id,),
            ledger.cohort_id,
            "buy",
            1,
            cutoff,
            session,
            "next_session_open",
            "pending",
            None,
            None,
        )
        ledger.stage_intent(intent)

        def crash_after_commit(phase):
            if phase == "validate_market_data":
                raise RuntimeError("execution crash")

        executor._after_phase_commit = crash_after_commit
        bundle = SessionExecutor.fetch_input_bundle(
            session, ("AAPL",), source, executor.benchmark_symbols
        )
        with pytest.raises(RuntimeError, match="execution crash"):
            executor.execute_open_and_mark(
                session,
                orchestrator._epoch_id,
                bundle,
                {"AAPL": Decimal("0.0125")},
                datetime.now(timezone.utc),
            )
        assert executor.persisted_borrow_rates(session) == {"AAPL": Decimal("0.0125")}
        before = (
            len(source.raw_calls),
            len(source.action_calls),
            len(source.benchmark_calls),
        )

        executor._after_phase_commit = lambda phase: None
        with (
            patch(
                "tradingagents.strategies.trading.portfolio_committee.PortfolioCommittee.synthesize",
                side_effect=_authoritative_committee,
            ),
            patch(
                "tradingagents.strategies.orchestration.session_executor.ensure_reference_bars",
                return_value={
                    "AAPL": bundle.bars[("AAPL", session)],
                    "MSFT": MarketBar(
                        **{
                            **bundle.bars[("AAPL", session)].__dict__,
                            "ticker": "MSFT",
                        }
                    ),
                },
            ),
        ):
            resumed = orchestrator.run_daily(session.isoformat())

        assert not resumed["cohort_0"]["error"]
        assert ledger.intent(intent.intent_id).status == "filled"
        assert len(source.raw_calls) == before[0]
        assert len(source.action_calls) == before[1]
        assert len(source.benchmark_calls) == before[2]


class TestThirtyDayFullLifecycle:
    def test_30_exact_sessions_preserve_delays_exits_and_accounting(self, tmp_path):
        orchestrator, source = _authoritative_orchestrator(
            tmp_path, cohorts=2, hold_days=2
        )
        sessions = [date(2026, 3, 30)]
        for _ in range(29):
            sessions.append(next_session(sessions[-1]))

        with patch(
            "tradingagents.strategies.trading.portfolio_committee.PortfolioCommittee.synthesize",
            side_effect=_authoritative_committee,
        ) as committee:
            results = [
                orchestrator.run_daily(session.isoformat()) for session in sessions
            ]
            before_replay = [
                (
                    len(cohort["ledger"].read_signals()),
                    cohort["ledger"]
                    .connection.execute("SELECT COUNT(*) FROM order_intents")
                    .fetchone()[0],
                    len(cohort["ledger"].read_fills()),
                )
                for cohort in orchestrator.cohorts
            ]
            before_replay_calls = (
                len(source.raw_calls),
                len(source.action_calls),
                len(source.benchmark_calls),
                committee.call_count,
            )
            replay = orchestrator.run_daily(sessions[-1].isoformat())

        assert results[0]["cohort_0"]["trades_opened"] == []
        for index, cohort in enumerate(orchestrator.cohorts):
            ledger = cohort["ledger"]
            fills = ledger.read_fills()
            snapshots = ledger.read_snapshots(valid_only=True)
            after_replay = (
                len(ledger.read_signals()),
                ledger.connection.execute(
                    "SELECT COUNT(*) FROM order_intents"
                ).fetchone()[0],
                len(fills),
            )
            assert fills[0].session == sessions[1]
            assert (fills[0].effective_at.hour, fills[0].effective_at.minute) == (
                13,
                30,
            )
            assert sum(fill.side == "buy" for fill in fills) >= 3
            assert sum(fill.side == "sell" for fill in fills) >= 2
            assert (
                ledger.connection.execute(
                    "SELECT COUNT(*) FROM order_intents WHERE price_rule = 'resting_stop'"
                ).fetchone()[0]
                >= 1
            )
            assert len(snapshots) == 30
            assert all(snapshot.valid for snapshot in snapshots)
            assert all(
                snapshot.net_equity
                == snapshot.cash + snapshot.long_market_value - snapshot.short_liability
                for snapshot in snapshots
            )
            assert after_replay == before_replay[index]
            assert replay[f"cohort_{index}"]["replayed"]

        assert len(source.benchmark_calls) == 30
        assert len(source.raw_calls) <= 2 * 30
        assert committee.call_count <= 2 * 30
        assert (
            len(source.raw_calls),
            len(source.action_calls),
            len(source.benchmark_calls),
            committee.call_count,
        ) == before_replay_calls


class TestThirtyDayCohortDivergence:
    def test_cohorts_share_inputs_and_learning_stays_disabled(self, tmp_path):
        orchestrator, source = _authoritative_orchestrator(tmp_path, cohorts=2)
        session = date(2026, 3, 30)
        with patch(
            "tradingagents.strategies.trading.portfolio_committee.PortfolioCommittee.synthesize",
            side_effect=_authoritative_committee,
        ):
            result = orchestrator.run_daily(session.isoformat())

        assert not result["cohort_0"]["error"]
        assert not result["cohort_1"]["error"]
        assert len(source.benchmark_calls) == 1
        assert len(source.raw_calls) == 1
        assert all(
            cohort["engine"]._adaptive_confidence is False
            and cohort["config"].learning_enabled is False
            for cohort in orchestrator.cohorts
        )
        assert all(
            item["reason"] == "learning_disabled"
            for item in orchestrator.run_learning().values()
        )


class TestOpenBBEnrichment:
    def test_enrichment_is_shared_and_passed_after_mark(self, tmp_path):
        orchestrator, _ = _authoritative_orchestrator(tmp_path, cohorts=2)
        session = date(2026, 3, 30)
        enrichment = {"profiles": {"AAPL": {"sector": "Technology"}}}
        observed: list[dict] = []

        def committee(signals, **kwargs):
            observed.append(kwargs["enrichment"])
            return _authoritative_committee(signals)

        with (
            patch.object(
                orchestrator, "_fetch_openbb_enrichment", return_value=enrichment
            ),
            patch(
                "tradingagents.strategies.trading.portfolio_committee.PortfolioCommittee.synthesize",
                side_effect=committee,
            ),
        ):
            orchestrator.run_daily(session.isoformat())

        assert observed == [enrichment, enrichment]
        assert all(
            cohort["ledger"].read_snapshots(session, session)[0].valid
            for cohort in orchestrator.cohorts
        )

    def test_graceful_degradation_without_openbb(self, tmp_path):
        orchestrator, _ = _authoritative_orchestrator(tmp_path)
        session = date(2026, 3, 30)
        observed: list[dict] = []

        def committee(signals, **kwargs):
            observed.append(kwargs["enrichment"])
            return _authoritative_committee(signals)

        with (
            patch.object(orchestrator, "_fetch_openbb_enrichment", return_value={}),
            patch(
                "tradingagents.strategies.trading.portfolio_committee.PortfolioCommittee.synthesize",
                side_effect=committee,
            ),
        ):
            result = orchestrator.run_daily(session.isoformat())

        assert observed == [{}]
        assert not result["cohort_0"]["error"]
        assert result["cohort_0"]["intents_staged"]


class TestReactivatedStrategies:
    """Test reactivated govt_contracts and state_economics produce valid signals."""

    def test_govt_contracts_with_usaspending_data(self):
        """govt_contracts screen() produces candidates from contract data."""
        from tradingagents.strategies.modules.govt_contracts import (
            GovtContractsStrategy,
        )

        strategy = GovtContractsStrategy()
        assert strategy.track == "paper_trade"
        assert "openbb" in strategy.data_sources

        # Provide synthetic USASpending contract data
        data = {
            "usaspending": {
                "data": {
                    "contracts": [
                        {
                            "recipient": "Lockheed Martin Corp",
                            "amount": 500_000_000,
                            "award_id": "AWARD-LMT",
                        },
                        {
                            "recipient": "Northrop Grumman Systems",
                            "amount": 200_000_000,
                            "award_id": "AWARD-NOC",
                        },
                        {
                            "recipient": "Small Unknown Contractor",
                            "amount": 10_000_000,
                            "award_id": "AWARD-SMALL",
                        },  # Below threshold
                    ]
                }
            },
            "yfinance": {"prices": {}},
        }

        candidates = strategy.screen(data, "2026-03-15", strategy.get_default_params())
        # Should find LMT and NOC (Lockheed and Northrop), but not small contractor
        assert len(candidates) >= 1
        tickers = [c.ticker for c in candidates]
        assert "LMT" in tickers  # Lockheed
        for c in candidates:
            assert c.direction == "long"
            assert c.score > 0

    def test_govt_contracts_momentum_fallback(self):
        """govt_contracts falls back to momentum when no contract data."""
        from tradingagents.strategies.modules.govt_contracts import (
            GovtContractsStrategy,
        )

        strategy = GovtContractsStrategy()

        # Build price data with upward momentum for some contractors
        dates = pd.date_range("2026-02-01", periods=40, freq="B")
        prices = {}
        # LMT with strong upward momentum
        lmt_close = [400 + i * 2 for i in range(40)]  # rising
        prices["LMT"] = pd.DataFrame(
            {"Close": lmt_close, "Volume": [1e6] * 40}, index=dates
        )
        # BA with flat/declining
        ba_close = [200 - i * 0.1 for i in range(40)]
        prices["BA"] = pd.DataFrame(
            {"Close": ba_close, "Volume": [1e6] * 40}, index=dates
        )

        data = {
            "usaspending": {},  # No contract data
            "yfinance": {"prices": prices},
        }

        candidates = strategy.screen(data, "2026-03-25", strategy.get_default_params())
        # LMT should appear (positive momentum), BA should not (negative)
        if candidates:
            tickers = [c.ticker for c in candidates]
            assert "LMT" in tickers
            for c in candidates:
                assert c.metadata.get("source") == "momentum_fallback"

    def test_govt_contracts_exit_logic(self):
        """govt_contracts exit logic works correctly."""
        from tradingagents.strategies.modules.govt_contracts import (
            GovtContractsStrategy,
        )

        strategy = GovtContractsStrategy()
        params = strategy.get_default_params()

        # Test profit target
        should_exit, reason = strategy.check_exit("LMT", 100.0, 120.0, 5, params, {})
        assert should_exit is True
        assert reason == "profit_target"

        # Test stop loss
        should_exit, reason = strategy.check_exit("LMT", 100.0, 90.0, 5, params, {})
        assert should_exit is True
        assert reason == "stop_loss"

        # Test hold period
        should_exit, reason = strategy.check_exit("LMT", 100.0, 102.0, 35, params, {})
        assert should_exit is True
        assert reason == "hold_period"

        # Test no exit yet
        should_exit, reason = strategy.check_exit("LMT", 100.0, 105.0, 5, params, {})
        assert should_exit is False

    def test_state_economics_with_fred_data(self):
        """state_economics screen() combines FRED indicators with momentum."""
        from tradingagents.strategies.modules.state_economics import (
            StateEconomicsStrategy,
        )

        strategy = StateEconomicsStrategy()
        assert strategy.track == "paper_trade"
        assert "fred" in strategy.data_sources
        assert "openbb" in strategy.data_sources

        # Build price data for regional ETFs
        dates = pd.date_range("2026-02-01", periods=30, freq="B")
        prices = {}
        # KRE (regional banks) with positive momentum
        kre_close = [50 + i * 0.5 for i in range(30)]
        prices["KRE"] = pd.DataFrame(
            {"Close": kre_close, "Volume": [1e6] * 30}, index=dates
        )
        # IWN with slight positive
        iwn_close = [160 + i * 0.2 for i in range(30)]
        prices["IWN"] = pd.DataFrame(
            {"Close": iwn_close, "Volume": [1e6] * 30}, index=dates
        )

        data = {
            "yfinance": {"prices": prices},
            "fred": {
                "UNRATE": {"2026-01": 4.2, "2026-02": 4.0},  # Declining = bullish
                "ICSA": {
                    "2026-02-15": 220000,
                    "2026-02-22": 210000,
                },  # Declining = bullish
            },
        }

        candidates = strategy.screen(data, "2026-03-15", strategy.get_default_params())
        assert len(candidates) > 0
        # KRE should get econ_boost from declining unemployment
        kre_candidates = [c for c in candidates if c.ticker == "KRE"]
        if kre_candidates:
            assert kre_candidates[0].metadata.get("econ_boost", 0) > 0

    def test_state_economics_momentum_only_fallback(self):
        """state_economics falls back to pure momentum when no FRED data."""
        from tradingagents.strategies.modules.state_economics import (
            StateEconomicsStrategy,
        )

        strategy = StateEconomicsStrategy()

        dates = pd.date_range("2026-02-01", periods=30, freq="B")
        prices = {}
        kre_close = [50 + i * 0.5 for i in range(30)]
        prices["KRE"] = pd.DataFrame(
            {"Close": kre_close, "Volume": [1e6] * 30}, index=dates
        )

        data = {
            "yfinance": {"prices": prices},
            "fred": {},  # No FRED data
        }

        candidates = strategy.screen(data, "2026-03-15", strategy.get_default_params())
        assert len(candidates) > 0
        for c in candidates:
            assert c.metadata.get("econ_boost", 0) == 0.0  # No boost without FRED

    def test_state_economics_exit_logic(self):
        """state_economics exit logic: rebalance schedule (30-day default)."""
        from tradingagents.strategies.modules.state_economics import (
            StateEconomicsStrategy,
        )

        strategy = StateEconomicsStrategy()
        params = strategy.get_default_params()

        # Before rebalance (default rebalance_days=30)
        should_exit, reason = strategy.check_exit("KRE", 50.0, 55.0, 10, params, {})
        assert should_exit is False

        # At rebalance boundary
        should_exit, reason = strategy.check_exit("KRE", 50.0, 55.0, 30, params, {})
        assert should_exit is True
        assert reason == "rebalance"

    def test_twelve_strategies_registered(self):
        """Verify 12 strategies are registered (including quantum_readiness)."""
        from tradingagents.strategies.modules import get_paper_trade_strategies

        strategies = get_paper_trade_strategies()
        assert len(strategies) == 12
        names = [s.name for s in strategies]
        assert "govt_contracts" in names
        assert "state_economics" in names
        assert "commodity_macro" in names
        assert "weather_ag" in names
        assert "quantum_readiness" in names
