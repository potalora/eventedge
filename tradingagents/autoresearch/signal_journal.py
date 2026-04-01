"""Append-only signal journal for autoresearch paper trading.

Logs every signal (traded or not) as a JSONL line. Outcome fields
(5d/10d/30d returns) are back-filled on subsequent runs once enough
calendar days have elapsed.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class JournalEntry:
    """One signal observation."""

    timestamp: str
    strategy: str
    ticker: str
    direction: str
    score: float
    llm_conviction: float = 0.0
    regime: str = ""
    traded: bool = False
    entry_price: float = 0.0
    return_5d: float | None = None
    return_10d: float | None = None
    return_30d: float | None = None
    prompt_version: str = ""
    metadata: dict = field(default_factory=dict)


class SignalJournal:
    """Append-only JSONL signal log.

    File lives at ``{state_dir}/signal_journal.jsonl``.
    """

    def __init__(self, state_dir: str) -> None:
        self._path = Path(state_dir) / "signal_journal.jsonl"
        self._path.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def log_signal(self, entry: JournalEntry) -> None:
        """Append a single signal entry."""
        with open(self._path, "a") as f:
            f.write(json.dumps(asdict(entry), default=str) + "\n")

    def log_signals(self, entries: list[JournalEntry]) -> None:
        """Append signal entries, skipping duplicates (same strategy+ticker+timestamp)."""
        existing_keys: set[tuple[str, str, str]] = set()
        if self._path.exists():
            for line in self._path.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                    key = (e.get("strategy", ""), e.get("ticker", ""), e.get("timestamp", ""))
                    existing_keys.add(key)
                except json.JSONDecodeError:
                    continue

        new_entries = []
        for entry in entries:
            d = asdict(entry)
            key = (d["strategy"], d["ticker"], d["timestamp"])
            if key not in existing_keys:
                new_entries.append(d)
                existing_keys.add(key)

        if new_entries:
            with open(self._path, "a") as f:
                for d in new_entries:
                    f.write(json.dumps(d, default=str) + "\n")

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get_entries(
        self,
        strategy: str | None = None,
        ticker: str | None = None,
        since: str | None = None,
    ) -> list[dict]:
        """Read journal entries, optionally filtered."""
        if not self._path.exists():
            return []

        cutoff = datetime.fromisoformat(since) if since else None
        results: list[dict] = []

        for line in self._path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            if strategy and entry.get("strategy") != strategy:
                continue
            if ticker and entry.get("ticker", "").upper() != ticker.upper():
                continue
            if cutoff:
                try:
                    ts = datetime.fromisoformat(entry["timestamp"])
                    if ts < cutoff:
                        continue
                except (KeyError, ValueError):
                    continue

            results.append(entry)

        return results

    def get_convergence_signals(
        self, date: str, min_strategies: int = 2
    ) -> list[dict]:
        """Find tickers where multiple strategies agree on direction on the same date.

        Returns list of dicts: {ticker, direction, strategies, count, avg_score}.
        """
        entries = self.get_entries(since=date)

        # Group by (ticker, direction)
        groups: dict[tuple[str, str], list[dict]] = {}
        for e in entries:
            ts = e.get("timestamp", "")
            if not ts.startswith(date):
                continue
            key = (e["ticker"], e["direction"])
            groups.setdefault(key, []).append(e)

        results = []
        for (ticker, direction), signals in groups.items():
            unique_strategies = {s["strategy"] for s in signals}
            if len(unique_strategies) >= min_strategies:
                avg_score = sum(s["score"] for s in signals) / len(signals)
                results.append({
                    "ticker": ticker,
                    "direction": direction,
                    "strategies": sorted(unique_strategies),
                    "count": len(unique_strategies),
                    "avg_score": avg_score,
                })

        results.sort(key=lambda x: x["count"], reverse=True)
        return results

    # ------------------------------------------------------------------
    # Knowledge gaps (smart exploration budget)
    # ------------------------------------------------------------------

    def get_knowledge_gaps(
        self, regime: str | None = None,
    ) -> list[dict]:
        """Rank strategies by observation count (fewest first).

        Strategies with fewer completed observations are knowledge gaps
        and should receive exploration budget priority.

        Returns list of dicts: {strategy, total_signals, traded_count,
        with_outcomes, regime_match}.
        """
        entries = self.get_entries()

        strategy_stats: dict[str, dict] = {}
        for e in entries:
            strat = e.get("strategy", "")
            if not strat:
                continue
            if strat not in strategy_stats:
                strategy_stats[strat] = {
                    "strategy": strat,
                    "total_signals": 0,
                    "traded_count": 0,
                    "with_outcomes": 0,
                }
            stats = strategy_stats[strat]
            stats["total_signals"] += 1
            if e.get("traded"):
                stats["traded_count"] += 1
            if e.get("return_5d") is not None:
                stats["with_outcomes"] += 1

        results = sorted(strategy_stats.values(), key=lambda x: x["with_outcomes"])
        return results

    # ------------------------------------------------------------------
    # Outcome tracking (Phase 3)
    # ------------------------------------------------------------------

    def fill_outcomes(self, price_cache: dict[str, Any], today: str) -> int:
        """Back-fill return fields for past entries where enough time has elapsed.

        Args:
            price_cache: {ticker: DataFrame with "Close" column}
            today: Current date string (YYYY-MM-DD).

        Returns:
            Number of entries updated.
        """
        if not self._path.exists():
            return 0

        today_dt = datetime.strptime(today, "%Y-%m-%d")
        lines = self._path.read_text().splitlines()
        updated_count = 0
        new_lines: list[str] = []

        for line in lines:
            line = line.strip()
            if not line:
                new_lines.append(line)
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                new_lines.append(line)
                continue

            entry_price = entry.get("entry_price", 0)
            ticker = entry.get("ticker", "")
            ts = entry.get("timestamp", "")

            if not entry_price or entry_price <= 0 or not ticker or not ts:
                new_lines.append(line)
                continue

            try:
                signal_dt = datetime.fromisoformat(ts)
            except ValueError:
                new_lines.append(line)
                continue

            days_elapsed = (today_dt - signal_dt).days
            ticker_prices = price_cache.get(ticker)
            changed = False

            if ticker_prices is not None and not ticker_prices.empty:
                close_series = ticker_prices["Close"]
                direction = entry.get("direction", "long")
                sign = 1 if direction == "long" else -1

                for period, field_name in [(5, "return_5d"), (10, "return_10d"), (30, "return_30d")]:
                    if days_elapsed >= period and entry.get(field_name) is None:
                        target_dt = signal_dt + timedelta(days=period)
                        try:
                            target_prices = close_series.loc[:target_dt.strftime("%Y-%m-%d")]
                            if not target_prices.empty:
                                target_price = float(target_prices.iloc[-1])
                                if target_price > 0 and entry_price > 0:
                                    ret = sign * (target_price - entry_price) / entry_price
                                    entry[field_name] = round(ret, 6)
                                    changed = True
                        except (KeyError, IndexError):
                            continue

            if changed:
                updated_count += 1
                new_lines.append(json.dumps(entry, default=str))
            else:
                new_lines.append(line)

        if updated_count > 0:
            # Atomic rewrite
            fd, tmp = tempfile.mkstemp(
                dir=self._path.parent, suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w") as f:
                    f.write("\n".join(new_lines) + "\n")
                os.replace(tmp, self._path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise

        logger.info("Signal journal: updated %d entries with outcome data", updated_count)
        return updated_count

    # ------------------------------------------------------------------
    # High-conviction failures (for prompt optimization)
    # ------------------------------------------------------------------

    def get_high_conviction_failures(
        self, strategy: str, limit: int = 10, min_conviction: float = 0.6,
    ) -> list[dict]:
        """Return recent high-conviction signals where direction was wrong.

        Used by the prompt optimizer to identify what the current prompt
        gets wrong, so it can propose targeted modifications.
        """
        entries = self.get_entries(strategy=strategy)
        failures = []

        for e in entries:
            conviction = e.get("llm_conviction", 0)
            ret_5d = e.get("return_5d")

            if conviction < min_conviction or ret_5d is None:
                continue

            direction = e.get("direction", "long")
            correct = (direction == "long" and ret_5d > 0) or (
                direction == "short" and ret_5d < 0
            )

            if not correct:
                failures.append(e)

        # Most recent first
        failures.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
        return failures[:limit]
