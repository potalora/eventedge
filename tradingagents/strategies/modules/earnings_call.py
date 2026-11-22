"""Earnings-event research using numerical surprises and sourced news.

The active Finnhub path supplies the earnings calendar and admitted company-news
summaries. It does not acquire complete call transcripts, Q&A or prior-quarter
comparators. Those capabilities require separately supplied original evidence;
the analyzer selects its prompt from the explicit text_source identity.
"""

from __future__ import annotations

from datetime import date as calendar_date
import hashlib
import logging
import math
from typing import Any, Iterable, Mapping

from .base import Candidate
from .admission import admit_candidates

logger = logging.getLogger(__name__)


def _finite_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    return num if math.isfinite(num) else None


def _eps_surprise_magnitude(item: Mapping[str, Any]) -> float | None:
    """Return fractional absolute EPS surprise magnitude, or None if unavailable."""
    pct = _finite_float(item.get("eps_surprise_pct"))
    if pct is not None:
        return abs(pct) / 100.0
    eps_actual = _finite_float(
        item.get("eps_actual") if item.get("eps_actual") is not None else item.get("epsActual")
    )
    eps_estimate = _finite_float(
        item.get("eps_estimate") if item.get("eps_estimate") is not None else item.get("epsEstimate")
    )
    if eps_actual is not None and eps_estimate is not None:
        denom = max(abs(eps_estimate), 0.10) if "epsActual" in item else abs(eps_estimate)
        if denom > 0.0:
            return abs(eps_actual - eps_estimate) / denom
    meta = item.get("metadata")
    if isinstance(meta, Mapping):
        return _eps_surprise_magnitude(meta)
    return None


def _liquidity_metric(item: Mapping[str, Any]) -> float:
    """Extract market cap / dollar volume liquidity signal (0.0 if absent)."""
    keys = (
        "marketCap",
        "market_cap",
        "revenueActual",
        "revenue_actual",
        "revenueEstimate",
        "avg_dollar_volume",
        "dollar_volume",
        "avgVolume",
        "avg_volume",
        "liquidity",
        "volume",
    )
    for key in keys:
        val = _finite_float(item.get(key))
        if val is not None and val > 0:
            return val
    meta = item.get("metadata")
    if isinstance(meta, Mapping):
        for key in keys:
            val = _finite_float(meta.get(key))
            if val is not None and val > 0:
                return val
    return 0.0


def _recency_ordinal(item: Mapping[str, Any]) -> int:
    for key in ("report_date", "date", "published_at"):
        raw = item.get(key)
        if isinstance(raw, str) and len(raw) >= 10:
            try:
                return calendar_date.fromisoformat(raw[:10]).toordinal()
            except ValueError:
                continue
    return 0


def _deterministic_symbol_hash(symbol: str) -> str:
    """Stable non-alphabetical tie-breaker so A-tickers never beat Z-tickers."""
    canonical = str(symbol or "").strip().upper()
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


def earnings_priority_key(item: Mapping[str, Any]) -> tuple[Any, ...]:
    """Deterministic economic rank key: EPS surprise -> liquidity -> recency -> hash.

    Never sorts alphabetically by symbol.
    """
    surprise = _eps_surprise_magnitude(item)
    has_surprise = 0 if surprise is not None else 1
    surprise_mag = -(surprise if surprise is not None else 0.0)
    liquidity = -_liquidity_metric(item)
    recency = -_recency_ordinal(item)
    symbol = str(item.get("symbol") or item.get("ticker") or "").strip().upper()
    return (has_surprise, surprise_mag, liquidity, recency, _deterministic_symbol_hash(symbol))


def prioritize_earnings_candidates(
    items: Iterable[Mapping[str, Any]],
    *,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Rank earnings events by EPS surprise magnitude, liquidity/market cap, and recency.

    Never sorts alphabetically and never drops valid numeric EPS surprises when
    news count is zero.
    """
    ranked = sorted((dict(item) for item in items), key=earnings_priority_key)
    if limit is not None:
        if type(limit) is not int or limit < 0:
            raise ValueError("limit must be a nonnegative integer or None")
        return ranked[:limit]
    return ranked


class EarningsCallStrategy:
    """Paper-trade earnings research with explicitly identified text evidence."""

    name = "earnings_call"
    track = "paper_trade"
    data_sources = ["finnhub", "yfinance"]

    def get_param_space(self, horizon: str = "30d") -> dict[str, tuple]:
        from tradingagents.strategies.orchestration.cohort_orchestrator import (
            HORIZON_PARAMS,
        )

        hp = HORIZON_PARAMS.get(horizon, HORIZON_PARAMS["30d"])
        return {
            "hold_days": hp["hold_days_range"],
            "max_positions": (2, 6),
        }

    def get_default_params(self, horizon: str = "30d") -> dict[str, Any]:
        from tradingagents.strategies.orchestration.cohort_orchestrator import (
            HORIZON_PARAMS,
        )

        hp = HORIZON_PARAMS.get(horizon, HORIZON_PARAMS["30d"])
        return {
            "hold_days": hp["hold_days_default"],
            "max_positions": 4,
        }

    def screen(self, data: dict, date: str, params: dict) -> list[Candidate]:
        """Screen for recent earnings events and generate candidates.

        Combines numerical EPS surprise and available text into one candidate
        per fiscal event. Valid numeric EPS surprises are retained even when
        Finnhub news count is 0 (with explicit numeric-only metadata), while
        still enriching with news text whenever present.
        """
        finnhub_data = data.get("finnhub", {})
        transcripts = list(finnhub_data.get("transcripts", []) or [])

        # Also accept numeric-surprise-only rows supplied via earnings_calendar / earnings
        # if not already represented in transcripts.
        seen_fiscal_events: set[tuple[str, Any, Any]] = set()
        raw_events: list[dict[str, Any]] = []
        for entry in transcripts:
            if not isinstance(entry, Mapping):
                continue
            sym = str(entry.get("symbol") or "").strip().upper()
            yr = entry.get("year")
            qtr = entry.get("quarter")
            if not sym or yr is None or qtr is None:
                continue
            key = (sym, yr, qtr)
            if key in seen_fiscal_events:
                continue
            seen_fiscal_events.add(key)
            raw_events.append(dict(entry, symbol=sym))

        for fallback_key in ("earnings_calendar", "earnings"):
            for entry in finnhub_data.get(fallback_key, []) or []:
                if not isinstance(entry, Mapping):
                    continue
                sym = str(entry.get("symbol") or "").strip().upper()
                yr = entry.get("year")
                qtr = entry.get("quarter")
                if not sym or yr is None or qtr is None:
                    continue
                key = (sym, yr, qtr)
                if key in seen_fiscal_events:
                    continue
                eps_a = _finite_float(entry.get("eps_actual"))
                eps_e = _finite_float(entry.get("eps_estimate"))
                if eps_a is None or eps_e is None or eps_e == 0.0:
                    continue
                seen_fiscal_events.add(key)
                raw_events.append(dict(entry, symbol=sym))

        if not raw_events:
            return []

        prioritized = prioritize_earnings_candidates(raw_events)
        candidates: list[Candidate] = []
        for t in prioritized:
            symbol = t["symbol"]
            year = t["year"]
            quarter = t["quarter"]

            # One fiscal period is one source event. Quantitative and text
            # evidence enrich the same candidate instead of creating colliding
            # ledger identities with different payloads.
            eps_actual = _finite_float(t.get("eps_actual"))
            eps_estimate = _finite_float(t.get("eps_estimate"))
            has_numeric_surprise = (
                eps_actual is not None
                and eps_estimate is not None
                and eps_estimate != 0.0
            )
            text = str(t.get("transcript_text") or "").strip()
            raw_news_count = t.get("news_count")
            news_count = (
                int(raw_news_count)
                if isinstance(raw_news_count, int) and not isinstance(raw_news_count, bool) and raw_news_count >= 0
                else (1 if text else 0)
            )
            metadata: dict[str, Any] = {
                "year": year,
                "quarter": quarter,
                "analysis_type": "earnings_call",
                "news_count": news_count,
                "has_news_text": bool(text),
            }
            if t.get("published_at"):
                metadata["published_at"] = t["published_at"]
            for liq_key in ("market_cap", "avg_dollar_volume", "dollar_volume", "liquidity", "volume"):
                val = _finite_float(t.get(liq_key))
                if val is not None and val > 0:
                    metadata[liq_key] = val

            direction = "long"
            score = 0.5
            if has_numeric_surprise:
                assert eps_actual is not None and eps_estimate is not None
                surprise = (eps_actual - eps_estimate) / abs(eps_estimate)
                score = round(min(abs(surprise) * 7.0, 1.0), 3)
                direction = "long" if surprise > 0 else "short"
                metadata.update(
                    {
                        "signal_tier": "backtestable",
                        "eps_actual": eps_actual,
                        "eps_estimate": eps_estimate,
                        "eps_surprise_pct": round(surprise * 100, 2),
                    }
                )
            if text:
                metadata.update(
                    {
                        "analysis_text": text[:5000],
                        "text_source": t.get("text_source", "earnings_news"),
                        "needs_llm_analysis": True,
                        "evidence_mode": (
                            "numeric_and_news" if has_numeric_surprise else "news_only"
                        ),
                    }
                )
                metadata.setdefault("signal_tier", "paper_only")
            elif has_numeric_surprise:
                metadata.update(
                    {
                        "needs_llm_analysis": False,
                        "evidence_mode": "numeric_surprise_only",
                    }
                )

            if text or has_numeric_surprise:
                candidates.append(
                    Candidate(
                        ticker=symbol,
                        date=date,
                        direction=direction,
                        score=score,
                        raw_signal_score=score,
                        metadata=metadata,
                    )
                )

        def _candidate_rank_key(candidate: Candidate) -> tuple[Any, ...]:
            meta = candidate.metadata
            surprise_pct = _finite_float(meta.get("eps_surprise_pct"))
            has_surprise = 0 if surprise_pct is not None else 1
            mag = -(
                abs(surprise_pct) / 100.0
                if surprise_pct is not None
                else float(
                    candidate.raw_signal_score
                    if candidate.raw_signal_score is not None
                    else candidate.score
                )
            )
            liq = -_liquidity_metric(meta)
            return (has_surprise, mag, liq, _deterministic_symbol_hash(candidate.ticker))

        return admit_candidates(
            self.name,
            candidates,
            params.get("analysis_budget", params.get("max_positions", 4)),
            rank_key=_candidate_rank_key,
            policy="eps_surprise_liquidity_hash_v1",
        )

    def check_exit(
        self,
        ticker: str,
        entry_price: float,
        current_price: float,
        holding_days: int,
        params: dict,
        data: dict,
        direction: str = "long",
    ) -> tuple[bool, str]:
        hold_days = params.get("hold_days", 20)
        if holding_days >= hold_days:
            return True, "hold_period"
        # Stop loss at 5%
        pnl_pct = (1 if direction == "long" else -1) * (current_price - entry_price) / entry_price
        if pnl_pct < -0.05:
            return True, "stop_loss"
        return False, ""

    def build_propose_prompt(self, context: dict) -> str:
        current = context.get("current_params", self.get_default_params())
        return f"""You are optimizing an earnings-event strategy using numerical EPS
surprises and LLM analysis of earnings-news summaries. The active source does
not supply call Q&A or prior-quarter transcripts; do not assume those inputs.

Investment horizon: 30 days. Post-earnings drift is documented over 2-3 weeks.
Exiting after 7 days leaves money on the table. Target 15-20 day holds to
capture the full drift while avoiding noise.

Current parameters: {current}

Parameter ranges:
- hold_days: 20-45 (post-earnings drift window, target ~20 days)
- max_positions: 2-6

Suggest 3 parameter combinations. Return JSON array of 3 param dicts."""
