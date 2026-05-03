"""Backfill equity_snapshots.jsonl for all cohorts of all active generations.

Walks paper_trades.json per cohort, fetches historical close prices via yfinance,
reconstructs daily mark-to-market portfolio value, writes equity_snapshots.jsonl.

Run once: python scripts/backfill_equity_history.py
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from tradingagents.strategies.orchestration.cohort_orchestrator import SIZE_PROFILES  # noqa: E402
from tradingagents.strategies.state.equity_snapshot import (  # noqa: E402
    SNAPSHOT_FILENAME,
    _mark_to_market,
)

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

HORIZONS = ("30d", "3m", "6m", "1y")
SIZES = ("5k", "10k", "50k", "100k")


def trading_days(start: str, end: str) -> list[str]:
    """Weekdays only (no holiday calendar — close enough for backfill)."""
    s = datetime.strptime(start, "%Y-%m-%d").date()
    e = datetime.strptime(end, "%Y-%m-%d").date()
    out: list[str] = []
    d = s
    while d <= e:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def collect_tickers(gen_state_dirs: list[Path]) -> set[str]:
    tickers: set[str] = set()
    for state_dir in gen_state_dirs:
        for h in HORIZONS:
            for s in SIZES:
                pt_path = state_dir / f"horizon_{h}_size_{s}" / "paper_trades.json"
                if not pt_path.exists():
                    continue
                try:
                    trades = json.loads(pt_path.read_text())
                except (json.JSONDecodeError, OSError):
                    continue
                for t in trades:
                    sym = t.get("ticker")
                    if sym:
                        tickers.add(sym)
    return tickers


def fetch_prices(tickers: set[str], start: str, end: str) -> dict[str, pd.DataFrame]:
    """Bulk-fetch close prices via yfinance. Returns {ticker: DataFrame[Close]}."""
    import yfinance as yf

    if not tickers:
        return {}

    # yfinance is happiest with one batch download; convert "/" to "-"
    yf_symbols = [t.replace("/", "-") for t in tickers]
    sym_map = dict(zip(yf_symbols, tickers))

    logger.info("Fetching %d tickers from %s to %s", len(yf_symbols), start, end)
    raw = yf.download(
        yf_symbols,
        start=start,
        end=(datetime.strptime(end, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d"),
        auto_adjust=False,
        progress=False,
        group_by="ticker",
        threads=True,
    )

    out: dict[str, pd.DataFrame] = {}
    if isinstance(raw.columns, pd.MultiIndex):
        for yf_sym in yf_symbols:
            try:
                df = raw[yf_sym][["Close"]].dropna()
            except KeyError:
                continue
            if not df.empty:
                out[sym_map[yf_sym]] = df
    else:
        # Single ticker case
        if "Close" in raw.columns:
            df = raw[["Close"]].dropna()
            if not df.empty:
                out[list(tickers)[0]] = df
    logger.info("Got prices for %d / %d tickers", len(out), len(tickers))
    return out


def price_at(df: pd.DataFrame, target_date: str) -> float | None:
    """Last close on or before target_date."""
    if df is None or df.empty:
        return None
    target = pd.Timestamp(target_date)
    # Index may be tz-aware
    idx = df.index
    if getattr(idx, "tz", None) is not None:
        target = target.tz_localize(idx.tz) if target.tz is None else target
    valid = df.loc[df.index <= target]
    if valid.empty:
        return None
    try:
        return float(valid["Close"].iloc[-1])
    except (IndexError, ValueError):
        return None


def reconstruct_cohort(
    cohort_dir: Path,
    size: str,
    start_date: str,
    end_date: str,
    prices: dict[str, pd.DataFrame],
) -> int:
    """Walk forward and write equity_snapshots.jsonl for one cohort. Returns row count."""
    pt_path = cohort_dir / "paper_trades.json"
    if not pt_path.exists():
        return 0
    try:
        trades = json.loads(pt_path.read_text())
    except (json.JSONDecodeError, OSError):
        return 0

    profile = SIZE_PROFILES.get(size)
    total_capital = profile.total_capital if profile else 10000.0
    days = trading_days(start_date, end_date)

    rows: list[dict] = []
    for d in days:
        # Trades that were open as of end of day d
        open_at_d: list[dict] = []
        closed_by_d: list[dict] = []
        cash_delta = 0.0

        for t in trades:
            entry_date = t.get("entry_date") or ""
            if entry_date > d:
                continue  # not opened yet
            shares = float(t.get("shares", 0) or 0)
            entry = float(t.get("entry_price", 0) or 0)
            direction = t.get("direction", "long")

            # Cash impact at entry
            if direction == "short":
                cash_delta += entry * shares  # short proceeds
            else:
                cash_delta -= entry * shares  # long cost

            exit_date = t.get("exit_date") or ""
            status = t.get("status", "open")
            is_closed_by_d = status == "closed" and exit_date and exit_date <= d
            if is_closed_by_d:
                exit_ = float(t.get("exit_price", 0) or 0)
                if direction == "short":
                    cash_delta -= exit_ * shares  # cover cost
                else:
                    cash_delta += exit_ * shares  # sale proceeds
                closed_by_d.append(t)
            else:
                open_at_d.append(t)

        cash = total_capital + cash_delta

        long_value = 0.0
        short_liability = 0.0
        unrealized = 0.0
        for t in open_at_d:
            df = prices.get(t.get("ticker", ""))
            cur = price_at(df, d) if df is not None else None
            pv, upnl = _mark_to_market(t, cur)
            unrealized += upnl
            if pv >= 0:
                long_value += pv
            else:
                short_liability += -pv

        realized = 0.0
        for t in closed_by_d:
            entry = float(t.get("entry_price", 0) or 0)
            exit_ = float(t.get("exit_price", 0) or 0)
            shares = float(t.get("shares", 0) or 0)
            if t.get("direction") == "short":
                realized += (entry - exit_) * shares
            else:
                realized += (exit_ - entry) * shares

        portfolio_value = cash + long_value - short_liability
        total_return_pct = (
            (portfolio_value - total_capital) / total_capital * 100
            if total_capital > 0 else 0.0
        )
        rows.append({
            "date": d,
            "cash": round(cash, 2),
            "long_value": round(long_value, 2),
            "short_liability": round(short_liability, 2),
            "portfolio_value": round(portfolio_value, 2),
            "realized_pnl": round(realized, 2),
            "unrealized_pnl": round(unrealized, 2),
            "total_pnl": round(realized + unrealized, 2),
            "total_return_pct": round(total_return_pct, 4),
            "n_open": len(open_at_d),
            "n_closed": len(closed_by_d),
            "total_capital": round(total_capital, 2),
        })

    out_path = cohort_dir / SNAPSHOT_FILENAME
    with open(out_path, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return len(rows)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--end-date", default=date.today().isoformat())
    args = p.parse_args()

    manifest_path = REPO / "data" / "generations" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    active = [g for g in manifest["generations"] if g.get("status") == "active"]
    if not active:
        logger.info("No active generations.")
        return

    state_dirs = [Path(g["state_dir"]) for g in active]
    tickers = collect_tickers(state_dirs)

    earliest = min(g["created_at"][:10] for g in active)
    fetch_start = (
        datetime.strptime(earliest, "%Y-%m-%d") - timedelta(days=5)
    ).strftime("%Y-%m-%d")

    prices = fetch_prices(tickers, fetch_start, args.end_date)

    for gen in active:
        gen_id = gen["gen_id"]
        gen_start = gen["created_at"][:10]
        # Use the earlier of created_at or earliest entry_date in cohorts
        state_dir = Path(gen["state_dir"])
        for h in HORIZONS:
            for s in SIZES:
                cohort_dir = state_dir / f"horizon_{h}_size_{s}"
                if not cohort_dir.exists():
                    continue
                # Use min entry_date from trades, fall back to gen start
                pt_path = cohort_dir / "paper_trades.json"
                start = gen_start
                if pt_path.exists():
                    try:
                        trades = json.loads(pt_path.read_text())
                        entry_dates = [t.get("entry_date") for t in trades if t.get("entry_date")]
                        if entry_dates:
                            start = min(min(entry_dates), gen_start)
                    except (json.JSONDecodeError, OSError):
                        pass
                n = reconstruct_cohort(cohort_dir, s, start, args.end_date, prices)
                logger.info("%s %s_%s: wrote %d snapshots", gen_id, h, s, n)


if __name__ == "__main__":
    main()
