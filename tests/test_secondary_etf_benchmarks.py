"""Fixed broad-ETF diagnostics use the same retained ledger dates."""
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.strategies.execution.models import BenchmarkObservation
from tradingagents.strategies.metrics.calendar import XNYSCalendar
from tradingagents.strategies.metrics.models import METRIC_SCHEMA_VERSION, MetricEpoch
from tradingagents.strategies.metrics.research import research_diagnostics
from tradingagents.strategies.metrics.service import MetricsService
from tradingagents.strategies.state.portfolio_ledger import PortfolioLedger


SYMBOLS = ("SPY", "BIL", "VTI", "VT")
COHORT = "horizon_30d_size_100k"


def supplied_series(count=41):
    calendar = XNYSCalendar()
    session = date(2026, 8, 3)
    history = []
    benchmarks = {symbol: [] for symbol in SYMBOLS}
    daily_rates = {"SPY": .0012, "BIL": .0001, "VTI": .0005, "VT": .0008}
    for index in range(count):
        equity = 100_000 * 1.001 ** index
        history.append({"session": session.isoformat(), "net_equity": equity,
                        "gross_exposure": equity * .5, "net_exposure": equity * .5,
                        "cumulative_costs": dict.fromkeys(
                            ("slippage", "commission", "other_fees", "borrow", "financing"), 0.)})
        for symbol in SYMBOLS:
            benchmarks[symbol].append({"session": session.isoformat(),
                "close": 100 * (1 + daily_rates[symbol]) ** index,
                "return_basis": "paired_total_return_index_v2"})
        session = calendar.next_session(session)
    return {"net_equity_history": history, "benchmarks": benchmarks,
            "benchmark_unavailable_reason": None}


def test_default_fixed_benchmarks_include_us_total_market_and_global_equities():
    assert DEFAULT_CONFIG["autoresearch"]["paper_ledger"]["benchmark_symbols"] == list(SYMBOLS)


def test_secondary_cagr_comparisons_use_zero_hurdle_and_no_acceptance_decision():
    report = research_diagnostics(supplied_series())
    result = report["benchmark_excess_uncertainty"]
    assert result["SPY"]["annualized_hurdle"] == .05
    assert result["SPY"]["decision"] == "inconclusive"
    for symbol, rate in (("VTI", .0005), ("VT", .0008)):
        comparison = result[symbol]
        assert comparison["status"] == "available"
        assert comparison["annualized_hurdle"] == 0.
        assert comparison["annualized_excess_return"] == pytest.approx(1.001**252 - (1 + rate)**252)
        assert comparison["total_excess_return"] == pytest.approx(1.001**40 - (1 + rate)**40)
        assert comparison["return_count"] == 40
        assert comparison["pairing"] == "same_session_same_resample_indices"
        assert comparison["decision"] == "descriptive_only"
        assert not comparison["primary_acceptance_criterion"]
    assert report["benchmark_comparison_policy"]["primary"] == "SPY"
    assert report["benchmark_comparison_policy"]["selection_policy"] == "fixed_ex_ante_no_winner_selection"


@pytest.mark.parametrize("fault", ["missing", "duplicate", "legacy", "nan"])
def test_bad_secondary_evidence_does_not_erase_primary_or_other_secondary(fault):
    data = supplied_series()
    rows = data["benchmarks"]["VT"]
    if fault == "missing":
        rows.pop(8)
    elif fault == "duplicate":
        rows.append(dict(rows[8]))
    elif fault == "legacy":
        rows[8]["return_basis"] = "legacy"
    else:
        rows[8]["close"] = float("nan")
    result = research_diagnostics(data)["benchmark_excess_uncertainty"]
    assert result["VT"]["status"] == "insufficient_evidence"
    assert all(result[symbol]["status"] == "available" for symbol in ("SPY", "BIL", "VTI"))


def test_missing_secondary_history_is_explicit_before_two_snapshots():
    report = research_diagnostics({"net_equity_history": [], "benchmarks": {}})
    for symbol in ("VTI", "VT"):
        comparison = report["benchmark_excess_uncertainty"][symbol]
        assert comparison["status"] == "insufficient_evidence"
        assert comparison["annualized_hurdle"] == 0.
        assert comparison["decision"] == "descriptive_only"


def retained_report(tmp_path, *, fault=None, extra_symbol=None):
    ledger = PortfolioLedger(tmp_path / COHORT / "portfolio.db", COHORT, Decimal("100000"))
    calendar, session = XNYSCalendar(), date(2026, 8, 3)
    symbols = SYMBOLS + ((extra_symbol,) if extra_symbol else ())
    try:
        for index in range(41):
            observed = datetime.combine(session, datetime.min.time(), UTC) + timedelta(hours=22)
            ledger.mark(session, {}, "epoch-etf", observed)
            for symbol in symbols:
                if fault == "missing" and symbol == "VT" and index == 8:
                    continue
                ledger.record_benchmark_observation(BenchmarkObservation(
                    observation_id=f"{symbol}-{session}", cohort_id=COHORT, epoch_id="epoch-etf",
                    session=session, symbol=symbol,
                    close=Decimal("100") * (Decimal("1.001") ** index if symbol != "VTI" else 1),
                    return_basis="total_return_adjusted" if fault == "legacy" and symbol == "VT" and index == 8
                                 else "paired_total_return_index_v2",
                    source="retained-four-etf-fixture", observed_at=observed,
                    valid=not (fault == "invalid" and symbol == "VT" and index == 8),
                    invalid_reason="fixture-invalid" if fault == "invalid" and symbol == "VT" and index == 8 else "",
                ))
            session = calendar.next_session(session)
        service = MetricsService(tmp_path, {COHORT: ledger})
        service.store.save_epoch(MetricEpoch(
            epoch_id="epoch-etf", generation_id="gen-etf", generation_commit="fixture",
            behavior_hash="fixture", config_hash="four-fixed-etfs", metric_schema_version=METRIC_SCHEMA_VERSION,
            execution_clock_version="next-open-v1", pricing_version="raw-v1", cost_model_version="cost-v1",
            start_session=date(2026, 8, 3), end_session=None, status="open", boundary_reason="initial",
        ))
        report = service.generation_report("epoch-etf")
        observations = ledger.read_benchmark_observations(epoch_id="epoch-etf")
        return report, observations
    finally:
        ledger.close()


def test_real_ledger_retains_four_etfs_and_service_reports_paired_secondary_comparisons(tmp_path):
    report, observations = retained_report(tmp_path)
    assert {row.symbol for row in observations} == set(SYMBOLS)
    series = report["cohort_series"][COHORT]
    assert set(series["benchmarks"]) == set(SYMBOLS)
    assert all(len(rows) == 41 for rows in series["benchmarks"].values())
    book = report["headline_books"][COHORT]
    assert book["metrics_available"]
    result = book["research_diagnostics"]["benchmark_excess_uncertainty"]
    assert all(result[symbol]["return_count"] == 40 for symbol in SYMBOLS)
    assert result["VTI"]["annualized_excess_return"] == pytest.approx(0., abs=1e-12)
    assert result["VT"]["annualized_excess_return"] == pytest.approx(1 - 1.001**252)
    assert result["SPY"]["annualized_hurdle"] == .05
    assert result["VTI"]["decision"] == result["VT"]["decision"] == "descriptive_only"
    assert len(series["matched_benchmark_returns"]) == 40


@pytest.mark.parametrize("fault", ["missing", "legacy", "invalid"])
def test_real_ledger_secondary_gaps_do_not_block_healthy_primary_metrics(tmp_path, fault):
    report, _ = retained_report(tmp_path, fault=fault)
    book = report["headline_books"][COHORT]
    assert book["metrics_available"]
    result = book["research_diagnostics"]["benchmark_excess_uncertainty"]
    assert result["SPY"]["status"] == result["VTI"]["status"] == "available"
    assert result["VT"]["status"] == "insufficient_evidence"


def test_service_exposes_all_retained_declared_symbols_generically(tmp_path):
    report, _ = retained_report(tmp_path, extra_symbol="IVV")
    series = report["cohort_series"][COHORT]
    assert len(series["benchmarks"]["IVV"]) == 41


def test_service_preserves_retained_secondary_evidence_without_accepted_snapshot():
    observation = BenchmarkObservation(
        observation_id="retained-VT", cohort_id=COHORT, epoch_id="epoch-etf",
        session=date(2026, 8, 3), symbol="VT", close=Decimal("100"),
        return_basis="paired_total_return_index_v2", source="retained-fixture",
        observed_at=datetime(2026, 8, 3, 22, tzinfo=UTC), valid=True, invalid_reason="",
    )
    series = MetricsService._cohort_series_from_inputs(((), (observation,), (), ()))
    assert series["net_equity_history"] == []
    assert len(series["benchmarks"]["VT"]) == 1
    assert series["matched_benchmark_returns"] == []


@pytest.mark.parametrize("missing", [False, True])
def test_executor_requires_every_configured_etf_pair_before_acceptance(tmp_path, missing):
    from tradingagents.strategies.execution.price_source import AdjustedClose, BarValidationError
    from tradingagents.strategies.orchestration.session_executor import SessionExecutor, SessionInputBundle
    from tradingagents.strategies.orchestration.trading_calendar import previous_session

    session = date(2026, 8, 3)
    processed = datetime(2026, 8, 3, 22, tzinfo=UTC)
    ledger = PortfolioLedger(tmp_path / "executor.db", "etf-executor", Decimal("100000"))
    config = {"autoresearch": {"paper_ledger": {
        "benchmark_symbols": DEFAULT_CONFIG["autoresearch"]["paper_ledger"]["benchmark_symbols"],
    }}}
    benchmarks = {(symbol, session): AdjustedClose(
        symbol=symbol, session=session, close=Decimal("100"), source="fixture-adjusted",
        fetched_at=processed, previous_session=previous_session(session), previous_close=Decimal("99"),
    ) for symbol in SYMBOLS if not (missing and symbol == "VT")}
    bundle = SessionInputBundle(session, (), {}, (), benchmarks)
    try:
        executor = SessionExecutor(ledger, config)
        if missing:
            with pytest.raises(BarValidationError, match="VT"):
                executor.validate_execution_input_bundle(session, "epoch-etf", bundle, processed)
            assert ledger.read_snapshots() == []
            assert ledger.read_benchmark_observations() == []
        else:
            executor.validate_execution_input_bundle(session, "epoch-etf", bundle, processed)
            result = executor.execute_open_and_mark(session, "epoch-etf", bundle, {}, processed)
            assert result.valid
            assert {row.symbol for row in ledger.read_benchmark_observations()} == set(SYMBOLS)
    finally:
        ledger.close()


def test_native_yahoo_multi_ticker_adjusted_shape_supports_all_four_etf_pairs(monkeypatch):
    import pandas as pd
    from tradingagents.strategies.execution import price_source as prices
    from tradingagents.strategies.orchestration.trading_calendar import previous_session

    session = date(2026, 8, 3)
    prior = previous_session(session)
    observed = datetime(2026, 8, 3, 22, tzinfo=UTC)
    # Native yfinance auto_adjust=True multi-symbol response has Close/ticker
    # columns. These are synthetic values, not evidence of market performance.
    frame = pd.DataFrame(
        [[100., 90., 300., 130.], [101., 90.01, 303., 131.]],
        index=pd.to_datetime([prior, session]),
        columns=pd.MultiIndex.from_product([["Close"], SYMBOLS]),
    )
    calls = []

    def download(symbols, **options):
        calls.append((symbols, options))
        return frame

    monkeypatch.setattr(prices.yf, "download", download)
    closes = prices.YFinancePriceSource(now=lambda: observed).get_total_return_closes(
        list(SYMBOLS), prior, session)
    paired = prices.paired_adjusted_closes(closes, SYMBOLS, session)
    prices.validate_benchmark_pairs(paired, set(SYMBOLS), session)
    assert set(paired) == {(symbol, session) for symbol in SYMBOLS}
    assert paired["VTI", session].close == Decimal("303")
    assert paired["VTI", session].previous_close == Decimal("300")
    assert paired["VT", session].previous_close == Decimal("130")
    assert calls[0][0] == list(SYMBOLS)
    assert calls[0][1]["auto_adjust"] is True
    assert {row.source for row in paired.values()} == {"yfinance-adjusted"}


@pytest.mark.parametrize("fault", [None, "missing"])
def test_existing_dashboard_and_email_show_shared_fixed_etf_table_from_real_report(tmp_path, fault):
    from pathlib import Path
    from tradingagents.dashboard.benchmark_tables import etf_comparison_rows
    from tradingagents.dashboard.email_export import render_dashboard_html

    report, _ = retained_report(tmp_path, fault=fault)
    rows = etf_comparison_rows(report)
    assert len(rows) == 4
    by_symbol = {row["Benchmark"]: row for row in rows}
    assert set(by_symbol) == set(SYMBOLS)
    assert by_symbol["SPY"]["Role"] == "Primary S&P 500; 5 pp annualized hurdle"
    assert by_symbol["VTI"]["Total excess"] == "+0.00 pp"
    assert by_symbol["VTI"]["Annualized excess"] == "+0.00 pp"
    assert "5 sessions: [" in by_symbol["VTI"]["Paired 95% intervals"]
    assert "20 sessions: unavailable" in by_symbol["VTI"]["Paired 95% intervals"]
    if fault:
        assert by_symbol["VT"]["Total excess"] == "Unavailable"
        assert by_symbol["VT"]["Evidence"].startswith("Insufficient evidence")
    else:
        assert by_symbol["VT"]["Evidence"] == "40 paired returns"
    html = render_dashboard_html([{"gen_id": "gen-etf", "metric_report": report}], "2026-08-03")
    assert "Fixed ETF comparisons by $100k book" in html
    assert "Primary S&amp;P 500; 5 pp annualized hurdle" in html
    for symbol in SYMBOLS:
        assert f"<td>{symbol}</td>" in html
    assert "Persisted benchmark observations (SPY/BIL/VTI/VT)" in html
    assert "etf_comparison_rows(report)" in Path("tradingagents/dashboard/pages/returns.py").read_text()
