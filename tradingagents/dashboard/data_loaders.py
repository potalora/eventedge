"""Cached data loading layer for the autoresearch dashboard.

Bridges JSON state files in data/generations/ to Streamlit pages using
@st.cache_data for efficient caching (data changes once per day).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import streamlit as st

HORIZONS = ("30d", "3m", "6m", "1y")
SIZES = ("5k", "10k", "50k", "100k")

_BASE_DIR = Path(os.path.dirname(__file__)).parent.parent / "data" / "generations"


def _manifest_path() -> Path:
    return _BASE_DIR / "manifest.json"


def _cohort_dirs(gen_state_dir: str) -> dict[str, str]:
    """Build {cohort_name: path} dict for all 16 cohorts of a generation."""
    dirs: dict[str, str] = {}
    for h in HORIZONS:
        for s in SIZES:
            name = f"horizon_{h}_size_{s}"
            dirs[name] = str(Path(gen_state_dir) / name)
    return dirs


def _open_metrics_service(gen_state_dir: str):
    """Open the exact existing v2 ledgers for one cached dashboard read."""
    from tradingagents.strategies.metrics.service import MetricsService
    from tradingagents.strategies.state.portfolio_ledger import PortfolioLedger

    root = Path(gen_state_dir)
    metrics_path = root / "metrics_v2.sqlite3"
    ledger_paths = {
        cohort_id: Path(cohort_dir) / "portfolio.db"
        for cohort_id, cohort_dir in _cohort_dirs(gen_state_dir).items()
        if (Path(cohort_dir) / "portfolio.db").is_file()
    }
    if not ledger_paths:
        return None, ()
    if not metrics_path.is_file():
        raise FileNotFoundError(f"missing v2 metric store: {metrics_path}")
    ledgers = []
    try:
        bindings = {}
        for cohort_id, path in ledger_paths.items():
            ledger = PortfolioLedger.open_existing(path)
            ledgers.append(ledger)
            if ledger.cohort_id != cohort_id:
                raise ValueError(
                    f"cohort directory {cohort_id!r} contains ledger {ledger.cohort_id!r}"
                )
            bindings[cohort_id] = ledger
        return (
            MetricsService(root, bindings, read_only=True),
            tuple(ledgers),
        )
    except BaseException:
        for ledger in ledgers:
            ledger.close()
        raise


def cohort_metric_books(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return the cohort-level metric-v2 books without legacy aliases."""
    headline = dict(report.get("headline_books", {}) or {})
    stress = dict(report.get("stress_tests", {}) or {})
    duplicates = sorted(set(headline) & set(stress))
    if duplicates:
        raise ValueError("duplicate cohort metric books: " + ", ".join(duplicates))
    return {**headline, **stress}


# ------------------------------------------------------------------
# Manifest / generation metadata
# ------------------------------------------------------------------


@st.cache_data(ttl=3600)
def get_active_generations() -> list[dict[str, Any]]:
    """Return metadata for all active generations."""
    path = _manifest_path()
    if not path.exists():
        return []
    data = json.loads(path.read_text())
    return [g for g in data.get("generations", []) if g.get("status") == "active"]


@st.cache_data(ttl=3600)
def get_all_generations() -> list[dict[str, Any]]:
    """Return metadata for all generations (active + retired)."""
    path = _manifest_path()
    if not path.exists():
        return []
    data = json.loads(path.read_text())
    return data.get("generations", [])


# ------------------------------------------------------------------
# Cohort comparison metrics
# ------------------------------------------------------------------


@st.cache_data(ttl=3600)
def load_cohort_metrics(gen_id: str, gen_state_dir: str) -> dict[str, Any]:
    """Compatibility projection of the authoritative generation report."""
    return load_generation_metrics(gen_id, gen_state_dir)


@st.cache_data(ttl=3600)
def load_generation_metrics(gen_id: str, gen_state_dir: str) -> dict[str, Any]:
    """Load the one immutable metric-v2 report used by dashboard surfaces."""
    service, ledgers = _open_metrics_service(gen_state_dir)
    if service is None:
        return {
            "metric_schema_version": 2,
            "epoch": None,
            "headline_books": {},
            "scenario_panel": None,
            "scenario_panel_available": False,
            "scenario_panel_unavailable_reason": "no_current_epoch",
            "missing_headline_books": [
                "horizon_1y_size_100k",
                "horizon_30d_size_100k",
                "horizon_3m_size_100k",
                "horizon_6m_size_100k",
            ],
            "stress_tests": {},
            "cohort_series": {},
            "candidate_bar_recoveries": [],
            "dependent_scenarios": True,
        }
    try:
        report = service.generation_report()
        epoch = report.get("epoch") if isinstance(report, dict) else None
        if isinstance(epoch, dict):
            if epoch.get("generation_id") != gen_id:
                raise ValueError("generation identity mismatch")
        return report
    finally:
        for ledger in ledgers:
            ledger.close()


@st.cache_data(ttl=3600)
def load_cohort_heatmap(
    gen_id: str, gen_state_dir: str, metric: str
) -> dict[str, dict[str, float | None]]:
    """Load horizon x size heatmap for a single metric."""
    report = load_generation_metrics(gen_id, gen_state_dir)
    books = cohort_metric_books(report)
    result: dict[str, dict[str, float | None]] = {
        horizon: {size: None for size in SIZES} for horizon in HORIZONS
    }
    for cohort_id, book in books.items():
        parts = cohort_id.split("_")
        if len(parts) != 4:
            continue
        horizon, size = parts[1], parts[3]
        if horizon in result and size in result[horizon]:
            value = book.get(metric)
            result[horizon][size] = float(value) if value is not None else None
    return result


# ------------------------------------------------------------------
# Trades
# ------------------------------------------------------------------


@st.cache_data(ttl=3600)
def load_all_trades(gen_id: str, gen_state_dir: str) -> list[dict[str, Any]]:
    """Compatibility placeholder until the v2 contract exposes positions."""
    load_generation_metrics(gen_id, gen_state_dir)
    return []


# ------------------------------------------------------------------
# Regime history
# ------------------------------------------------------------------


@st.cache_data(ttl=3600)
def load_regime_history(gen_id: str, gen_state_dir: str) -> list[dict[str, Any]]:
    """Load regime snapshots, deduplicated by date.

    Picks the cohort with the most entries (all record the same market
    regime, just at different run times).
    """
    best: list[dict] = []
    for h in HORIZONS:
        for s in SIZES:
            name = f"horizon_{h}_size_{s}"
            rs_path = Path(gen_state_dir) / name / "regime_snapshots.json"
            if not rs_path.exists():
                continue
            try:
                snapshots = json.loads(rs_path.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            if len(snapshots) > len(best):
                best = snapshots

    # Deduplicate by date (keep latest per day)
    by_date: dict[str, dict] = {}
    for snap in best:
        ts = snap.get("timestamp", "")
        date = ts[:10] if len(ts) >= 10 else ts
        by_date[date] = snap

    return [by_date[d] for d in sorted(by_date)]


# ------------------------------------------------------------------
# Signal stats (deduplicated across size cohorts)
# ------------------------------------------------------------------


@st.cache_data(ttl=3600)
def load_signal_stats(gen_id: str, gen_state_dir: str) -> dict[str, Any]:
    """Project governed decision totals; strategy attribution is unavailable."""
    report = load_generation_metrics(gen_id, gen_state_dir)
    books = dict(report.get("headline_books", {}) or {})
    # V2 records only governed aggregate decision counts.  Do not revive the
    # legacy journal's calendar-day hit-rate or claim a strategy attribution it
    # cannot support.
    total_decisions = sum(
        int(book.get("strategy_decisions", 0)) for book in books.values()
    )
    return {
        "per_strategy": {},
        "total_signals": total_decisions,
        "total_traded": None,
        "knowledge_gaps": [],
    }


# ------------------------------------------------------------------
# Capital deployment
# ------------------------------------------------------------------

# ------------------------------------------------------------------
# Equity history & live PnL
# ------------------------------------------------------------------


@st.cache_data(ttl=900)
def load_equity_history(
    gen_id: str, gen_state_dir: str
) -> dict[str, list[dict[str, Any]]]:
    """Compatibility projection of persisted valid net-equity observations."""
    report = load_generation_metrics(gen_id, gen_state_dir)
    return {
        cohort_id: list(series.get("net_equity_history", []))
        for cohort_id, series in dict(report.get("cohort_series", {})).items()
        if series.get("net_equity_history")
    }


@st.cache_data(ttl=900)
def load_position_pnl(gen_id: str, gen_state_dir: str) -> list[dict[str, Any]]:
    """No position-level report is available from the governed v2 contract yet."""
    load_generation_metrics(gen_id, gen_state_dir)
    return []


@st.cache_data(ttl=900)
def load_strategy_pnl(gen_id: str, gen_state_dir: str) -> list[dict[str, Any]]:
    """No strategy-PnL attribution is available from the governed v2 contract."""
    load_generation_metrics(gen_id, gen_state_dir)
    return []


@st.cache_data(ttl=3600)
def load_capital_deployment(gen_id: str, gen_state_dir: str) -> list[dict[str, Any]]:
    """Compute capital deployment per cohort.

    Returns list of {cohort, horizon, size, total_capital, deployed, pct}.
    """
    report = load_generation_metrics(gen_id, gen_state_dir)
    rows: list[dict[str, Any]] = []
    for cohort_id, series in dict(report.get("cohort_series", {})).items():
        history = list(series.get("net_equity_history", []))
        if not history:
            continue
        latest = history[-1]
        parts = cohort_id.split("_")
        rows.append(
            {
                "cohort": cohort_id,
                "horizon": parts[1] if len(parts) == 4 else "",
                "size": parts[3] if len(parts) == 4 else "",
                "total_capital": history[0]["net_equity"],
                "deployed": latest["gross_exposure"],
                "pct": (
                    latest["gross_exposure"] / history[0]["net_equity"] * 100
                    if history[0]["net_equity"]
                    else 0.0
                ),
            }
        )
    return rows
