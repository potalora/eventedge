"""P1+P2: Earnings call transcript analysis.

P1 — Earnings Call Tone & Deception Detection (Cohen et al. 2012):
Detects hedging language, unusual Q&A evasion, tone shifts between
prepared remarks and Q&A. Negative tone in Q&A predicts -2.7% drift.

P2 — Cross-Referencing Guidance Revisions:
Compares current call guidance to prior quarters and analyst consensus.
Detects stealth downgrades (lowering without flagging).

Data source: Finnhub (earnings_call_transcripts endpoint).
"""
from __future__ import annotations

import logging
from typing import Any

from .base import Candidate

logger = logging.getLogger(__name__)


class EarningsCallStrategy:
    """Paper-trade strategy analyzing earnings call transcripts via LLM."""

    name = "earnings_call"
    track = "paper_trade"
    data_sources = ["finnhub", "yfinance"]

    def get_param_space(self) -> dict[str, tuple]:
        return {
            "hold_days": (5, 30),
            "min_conviction": (0.3, 0.8),
            "max_positions": (2, 6),
            "analyze_qa_only": (True, False),
        }

    def get_default_params(self) -> dict[str, Any]:
        return {
            "hold_days": 7,
            "min_conviction": 0.5,
            "max_positions": 4,
            "analyze_qa_only": False,
        }

    def screen(self, data: dict, date: str, params: dict) -> list[Candidate]:
        """Screen for recent earnings events and generate candidates.

        Produces up to two candidates per event:
        1. EPS surprise candidate (backtestable, quantitative)
        2. Text analysis candidate (paper_only, needs LLM)

        Expects data["finnhub"]["transcripts"] = list of dicts with
        {symbol, year, quarter, transcript_text, text_source,
         eps_actual, eps_estimate}.
        """
        finnhub_data = data.get("finnhub", {})
        transcripts = finnhub_data.get("transcripts", [])

        if not transcripts:
            return []

        candidates = []
        for t in transcripts:
            symbol = t.get("symbol", "")
            if not symbol:
                continue

            # --- EPS surprise candidate (quantitative, backtestable) ---
            eps_actual = t.get("eps_actual")
            eps_estimate = t.get("eps_estimate")
            if eps_actual is not None and eps_estimate is not None and eps_estimate != 0:
                surprise = (eps_actual - eps_estimate) / abs(eps_estimate)
                # Map surprise magnitude to score: ±10% surprise → 0.7 score
                eps_score = min(abs(surprise) * 7.0, 1.0)
                eps_direction = "long" if surprise > 0 else "short"
                candidates.append(
                    Candidate(
                        ticker=symbol,
                        date=date,
                        direction=eps_direction,
                        score=round(eps_score, 3),
                        metadata={
                            "signal_tier": "backtestable",
                            "eps_actual": eps_actual,
                            "eps_estimate": eps_estimate,
                            "eps_surprise_pct": round(surprise * 100, 2),
                            "year": t.get("year"),
                            "quarter": t.get("quarter"),
                            "analysis_type": "earnings_call",
                        },
                    )
                )

            # --- Text analysis candidate (qualitative, paper_only) ---
            text = t.get("transcript_text", "")
            if not text:
                continue
            text_source = t.get("text_source", "earnings_news")

            candidates.append(
                Candidate(
                    ticker=symbol,
                    date=date,
                    direction="long",  # LLM will determine
                    score=0.5,
                    metadata={
                        "signal_tier": "paper_only",
                        "analysis_text": text[:5000],
                        "text_source": text_source,
                        "year": t.get("year"),
                        "quarter": t.get("quarter"),
                        "needs_llm_analysis": True,
                        "analysis_type": "earnings_call",
                    },
                )
            )

        return candidates[: params.get("max_positions", 4)]

    def check_exit(
        self,
        ticker: str,
        entry_price: float,
        current_price: float,
        holding_days: int,
        params: dict,
        data: dict,
    ) -> tuple[bool, str]:
        hold_days = params.get("hold_days", 10)
        if holding_days >= hold_days:
            return True, "hold_period"
        # Stop loss at 5%
        pnl_pct = (current_price - entry_price) / entry_price
        if pnl_pct < -0.05:
            return True, "stop_loss"
        return False, ""

    def build_propose_prompt(self, context: dict) -> str:
        current = context.get("current_params", self.get_default_params())
        return f"""You are optimizing an Earnings Call analysis strategy that uses LLM
to detect tone shifts, deception, and guidance revisions in transcripts.

Current parameters: {current}

Parameter ranges:
- hold_days: 5-30 (post-earnings drift window)
- min_conviction: 0.3-0.8
- max_positions: 2-6
- analyze_qa_only: true/false (Q&A section is more informative per research)

Suggest 3 parameter combinations. Return JSON array of 3 param dicts."""
