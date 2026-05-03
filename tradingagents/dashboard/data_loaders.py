"""Cached data loading layer for the autoresearch dashboard.

Bridges JSON state files in data/generations/ to Streamlit pages using
@st.cache_data for efficient caching (data changes once per day).
"""
from __future__ import annotations

import json
import os
from datetime import datetime
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
    """Load full comparison metrics for a generation's 16 cohorts."""
    from tradingagents.strategies.orchestration.cohort_comparison import CohortComparison

    dirs = _cohort_dirs(gen_state_dir)
    # Only include dirs that actually exist
    existing = {k: v for k, v in dirs.items() if Path(v).exists()}
    if not existing:
        return {"cohorts": {}, "per_strategy": {}}

    cmp = CohortComparison(existing)
    return cmp.compare()


@st.cache_data(ttl=3600)
def load_cohort_heatmap(gen_id: str, gen_state_dir: str, metric: str) -> dict[str, dict[str, float | None]]:
    """Load horizon x size heatmap for a single metric."""
    from tradingagents.strategies.orchestration.cohort_comparison import CohortComparison

    dirs = _cohort_dirs(gen_state_dir)
    existing = {k: v for k, v in dirs.items() if Path(v).exists()}
    if not existing:
        return {}

    cmp = CohortComparison(existing)
    return cmp.heatmap(metric)


# ------------------------------------------------------------------
# Trades
# ------------------------------------------------------------------

@st.cache_data(ttl=3600)
def load_all_trades(gen_id: str, gen_state_dir: str) -> list[dict[str, Any]]:
    """Load all paper trades across 16 cohorts, annotated with cohort/horizon/size."""
    all_trades: list[dict] = []
    for h in HORIZONS:
        for s in SIZES:
            name = f"horizon_{h}_size_{s}"
            pt_path = Path(gen_state_dir) / name / "paper_trades.json"
            if not pt_path.exists():
                continue
            try:
                trades = json.loads(pt_path.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            for t in trades:
                t["cohort"] = name
                t["horizon"] = h
                t["size"] = s
            all_trades.extend(trades)
    return all_trades


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
    """Aggregate signal stats across horizons (one cohort per horizon to avoid 4x counting).

    Returns:
        {
            "per_strategy": {name: {signals, trades, hit_rate_5d, trade_rate}},
            "total_signals": int,
            "total_traded": int,
            "knowledge_gaps": [...],
        }
    """
    from tradingagents.strategies.learning.signal_journal import SignalJournal

    all_entries: list[dict] = []
    for h in HORIZONS:
        # Use 100k cohort as representative (same signals as 5k/10k/50k for this horizon)
        cohort_dir = str(Path(gen_state_dir) / f"horizon_{h}_size_100k")
        if not Path(cohort_dir).exists():
            continue
        journal = SignalJournal(cohort_dir)
        entries = journal.get_entries()
        for e in entries:
            e["_horizon"] = h
        all_entries.extend(entries)

    # Per-strategy aggregation
    by_strategy: dict[str, list[dict]] = {}
    for e in all_entries:
        by_strategy.setdefault(e.get("strategy", "unknown"), []).append(e)

    per_strategy: dict[str, dict] = {}
    for strat, entries in sorted(by_strategy.items()):
        if not strat:
            continue
        traded = [e for e in entries if e.get("traded")]
        eligible = [e for e in entries if e.get("return_5d") is not None]
        hits = sum(
            1 for e in eligible
            if (e.get("direction") == "long" and e["return_5d"] > 0)
            or (e.get("direction") == "short" and e["return_5d"] < 0)
        )
        per_strategy[strat] = {
            "signals": len(entries),
            "trades": len(traded),
            "hit_rate_5d": hits / len(eligible) if eligible else None,
            "trade_rate": len(traded) / len(entries) if entries else 0,
        }

    # Knowledge gaps from one representative journal
    knowledge_gaps = []
    rep_dir = str(Path(gen_state_dir) / "horizon_30d_size_100k")
    if Path(rep_dir).exists():
        journal = SignalJournal(rep_dir)
        knowledge_gaps = journal.get_knowledge_gaps()

    return {
        "per_strategy": per_strategy,
        "total_signals": len(all_entries),
        "total_traded": sum(1 for e in all_entries if e.get("traded")),
        "knowledge_gaps": knowledge_gaps,
    }


# ------------------------------------------------------------------
# Capital deployment
# ------------------------------------------------------------------

# ------------------------------------------------------------------
# Equity history & live PnL
# ------------------------------------------------------------------

@st.cache_data(ttl=900)
def load_equity_history(gen_id: str, gen_state_dir: str) -> dict[str, list[dict[str, Any]]]:
    """Read equity_snapshots.jsonl per cohort. Returns {cohort_name: [snapshots]}."""
    from tradingagents.strategies.state.equity_snapshot import load_snapshots

    out: dict[str, list[dict[str, Any]]] = {}
    for h in HORIZONS:
        for s in SIZES:
            name = f"horizon_{h}_size_{s}"
            cohort_dir = str(Path(gen_state_dir) / name)
            if not Path(cohort_dir).exists():
                continue
            rows = load_snapshots(cohort_dir)
            if rows:
                out[name] = rows
    return out


@st.cache_data(ttl=900)
def load_current_prices(tickers: tuple[str, ...]) -> dict[str, float]:
    """Fetch latest close per ticker (single batch)."""
    if not tickers:
        return {}
    try:
        import yfinance as yf
    except ImportError:
        return {}

    yf_syms = [t.replace("/", "-") for t in tickers]
    sym_map = dict(zip(yf_syms, tickers))
    try:
        raw = yf.download(
            yf_syms, period="5d", progress=False, group_by="ticker", threads=True,
            auto_adjust=False,
        )
    except Exception:
        return {}

    import pandas as pd
    out: dict[str, float] = {}
    if isinstance(raw.columns, pd.MultiIndex):
        for yf_sym in yf_syms:
            try:
                df = raw[yf_sym][["Close"]].dropna()
                if not df.empty:
                    out[sym_map[yf_sym]] = float(df["Close"].iloc[-1])
            except KeyError:
                continue
    else:
        if "Close" in raw.columns and not raw.empty:
            out[tickers[0]] = float(raw["Close"].dropna().iloc[-1])
    return out


@st.cache_data(ttl=900)
def load_position_pnl(gen_id: str, gen_state_dir: str) -> list[dict[str, Any]]:
    """Compute live P&L per open + closed position across cohorts.

    For open: marks against latest yfinance close.
    For closed: uses recorded exit_price.
    Returns flat list with: cohort, horizon, size, ticker, strategy, direction,
    entry_price, current_price, shares, position_value, pnl, pnl_pct, status, days_held.
    """
    from datetime import datetime

    trades = load_all_trades(gen_id, gen_state_dir)
    open_tickers = tuple(sorted({t["ticker"] for t in trades if t.get("status") == "open" and t.get("ticker")}))
    cur = load_current_prices(open_tickers)

    today = datetime.now().date()
    rows: list[dict[str, Any]] = []
    for t in trades:
        ticker = t.get("ticker", "")
        entry = float(t.get("entry_price", 0) or 0)
        shares = float(t.get("shares", 0) or 0)
        direction = t.get("direction", "long")
        status = t.get("status", "open")

        if status == "closed":
            current = float(t.get("exit_price", 0) or 0)
        else:
            current = cur.get(ticker, entry)

        if direction == "short":
            pnl = (entry - current) * shares
        else:
            pnl = (current - entry) * shares

        cost_basis = entry * shares if entry > 0 else 1
        pnl_pct = (pnl / cost_basis) * 100 if cost_basis > 0 else 0

        days_held = 0
        entry_date = t.get("entry_date", "")
        try:
            ed = datetime.strptime(entry_date, "%Y-%m-%d").date() if entry_date else today
            xd = datetime.strptime(t.get("exit_date", ""), "%Y-%m-%d").date() if t.get("exit_date") else today
            days_held = (xd - ed).days
        except (ValueError, TypeError):
            pass

        rows.append({
            "cohort": t.get("cohort", ""),
            "horizon": t.get("horizon", ""),
            "size": t.get("size", ""),
            "ticker": ticker,
            "strategy": t.get("strategy", ""),
            "direction": direction,
            "entry_price": entry,
            "current_price": current,
            "shares": shares,
            "position_value": shares * current * (1 if direction == "long" else -1),
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "status": status,
            "days_held": days_held,
            "entry_date": entry_date,
        })
    return rows


@st.cache_data(ttl=900)
def load_strategy_pnl(gen_id: str, gen_state_dir: str) -> list[dict[str, Any]]:
    """Aggregate P&L by strategy across all 16 cohorts (deduped per ticker+entry_date+strategy).

    Splits realized vs unrealized and long vs short. Note: same signal lands in
    multiple cohorts; we sum across cohorts to reflect total dollar exposure.
    """
    positions = load_position_pnl(gen_id, gen_state_dir)
    by_strat: dict[str, dict[str, float]] = {}
    for p in positions:
        s = p["strategy"] or "unknown"
        d = by_strat.setdefault(s, {
            "strategy": s,
            "realized_long": 0.0, "realized_short": 0.0,
            "unrealized_long": 0.0, "unrealized_short": 0.0,
            "open_long_count": 0, "open_short_count": 0,
            "closed_count": 0,
        })
        if p["status"] == "closed":
            if p["direction"] == "short":
                d["realized_short"] += p["pnl"]
            else:
                d["realized_long"] += p["pnl"]
            d["closed_count"] += 1
        else:
            if p["direction"] == "short":
                d["unrealized_short"] += p["pnl"]
                d["open_short_count"] += 1
            else:
                d["unrealized_long"] += p["pnl"]
                d["open_long_count"] += 1
    rows = list(by_strat.values())
    for r in rows:
        r["total_pnl"] = (
            r["realized_long"] + r["realized_short"]
            + r["unrealized_long"] + r["unrealized_short"]
        )
    return sorted(rows, key=lambda r: r["total_pnl"], reverse=True)


@st.cache_data(ttl=3600)
def load_capital_deployment(gen_id: str, gen_state_dir: str) -> list[dict[str, Any]]:
    """Compute capital deployment per cohort.

    Returns list of {cohort, horizon, size, total_capital, deployed, pct}.
    """
    from tradingagents.strategies.orchestration.cohort_orchestrator import SIZE_PROFILES

    result: list[dict] = []
    for h in HORIZONS:
        for s in SIZES:
            name = f"horizon_{h}_size_{s}"
            pt_path = Path(gen_state_dir) / name / "paper_trades.json"
            deployed = 0.0
            if pt_path.exists():
                try:
                    trades = json.loads(pt_path.read_text())
                    deployed = sum(
                        t.get("position_value", 0)
                        for t in trades
                        if t.get("status") == "open"
                    )
                except (json.JSONDecodeError, OSError):
                    pass

            profile = SIZE_PROFILES.get(s)
            total_capital = profile.total_capital if profile else 0
            result.append({
                "cohort": name,
                "horizon": h,
                "size": s,
                "total_capital": total_capital,
                "deployed": round(deployed, 2),
                "pct": round(deployed / total_capital * 100, 1) if total_capital > 0 else 0,
            })
    return result
