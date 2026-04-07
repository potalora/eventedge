"""Quantum Readiness -- regime-switching PQC migration strategy.

Monitors the post-quantum cryptography (PQC) migration landscape and
trades the relative winners/losers depending on which regime dominates:

  Bull (CRQC accelerating):  Long PQC vendors, short crypto-exposed laggards.
  Bear (CRQC stalling):      Short quantum hardware pure-plays, long crypto unwind.
  Neutral (uncertain):        Minimal positions, tight stops.

The key asymmetry: centralized systems push updates in a quarter; decentralized
systems need consensus upgrades, hard forks, and years of wallet migration.

Academic basis: Filippo Valsorda (2026) CRQC timeline analysis; Google
(Adkins/Schmieg) 2029 deadline; NIST FIPS 203/204/205 finalization.

Data sources: EDGAR (PQC-keyword filings), Finnhub (PQC news), OpenBB (profiles).
"""
from __future__ import annotations

import logging
from typing import Any

from .base import Candidate

logger = logging.getLogger(__name__)

# -- Keyword sets for signal detection --

PQC_KEYWORDS = [
    "post-quantum", "post quantum", "quantum-resistant", "quantum resistant",
    "quantum-safe", "quantum safe", "pqc", "crqc",
    "ml-kem", "ml-dsa", "slh-dsa", "crystals-kyber", "crystals-dilithium",
    "fips 203", "fips 204", "fips 205", "cryptographic agility",
]

QUANTUM_THREAT_KEYWORDS = [
    "quantum computing risk", "quantum threat", "quantum vulnerability",
    "shor's algorithm", "ecdsa vulnerability", "rsa vulnerability",
]

BULL_NEWS_KEYWORDS = [
    "quantum milestone", "qubit", "error correction breakthrough",
    "quantum supremacy", "quantum advantage", "nist mandate",
    "pqc deadline", "quantum-safe migration",
]

BEAR_NEWS_KEYWORDS = [
    "quantum delay", "qubit shortage", "decoherence",
    "quantum setback", "years away", "overhyped",
]

# -- Ticker baskets --

# PQC vendors / cybersecurity (long on bull regime)
PQC_VENDORS = ["CRWD", "PANW", "ZS", "FTNT", "NET", "CSCO", "IBM", "MSFT"]

# Quantum hardware pure-plays (short on bear regime -- priced on timeline compression)
QUANTUM_HARDWARE = ["IONQ", "RGTI", "QBTS"]

# High crypto-dependency (short on bull regime -- migration laggards)
CRYPTO_EXPOSED = ["COIN", "MARA", "RIOT", "MSTR", "HUT"]


class QuantumReadinessStrategy:
    """Regime-switching strategy for the PQC migration trade."""

    name = "quantum_readiness"
    track = "paper_trade"
    data_sources = ["edgar", "finnhub", "openbb"]

    def get_param_space(self, horizon: str = "30d") -> dict[str, tuple]:
        from tradingagents.strategies.orchestration.cohort_orchestrator import HORIZON_PARAMS
        hp = HORIZON_PARAMS.get(horizon, HORIZON_PARAMS["30d"])
        return {
            "hold_days": hp["hold_days_range"],
            "min_conviction": (0.3, 0.8),
            "max_positions": (2, 6),
            "filing_lookback_days": (7, 30),
            "news_lookback_days": (3, 14),
            "regime_threshold": (0.2, 0.5),
        }

    def get_default_params(self, horizon: str = "30d") -> dict[str, Any]:
        from tradingagents.strategies.orchestration.cohort_orchestrator import HORIZON_PARAMS
        hp = HORIZON_PARAMS.get(horizon, HORIZON_PARAMS["30d"])
        return {
            "hold_days": hp["hold_days_default"],
            "min_conviction": 0.5,
            "max_positions": 4,
            "filing_lookback_days": 14,
            "news_lookback_days": 7,
            "regime_threshold": 0.3,
        }

    # ------------------------------------------------------------------
    # Screening
    # ------------------------------------------------------------------

    def screen(self, data: dict, date: str, params: dict) -> list[Candidate]:
        """Screen for PQC migration signals and select regime basket.

        Expects:
            data["edgar"]["pqc_filings"]  — keyword-matched SEC filings
            data["finnhub"]["pqc_news"]   — filtered PQC-related news
        """
        # Gather signals
        edgar_data = data.get("edgar", {})
        pqc_filings = edgar_data.get("pqc_filings", [])

        finnhub_data = data.get("finnhub", {})
        pqc_news = finnhub_data.get("pqc_news", [])

        # Compute regime score from signal balance
        regime_score = self._compute_regime_score(pqc_filings, pqc_news)
        threshold = params.get("regime_threshold", 0.3)

        candidates: list[Candidate] = []

        if regime_score > threshold:
            # BULL regime: CRQC timeline compressing
            candidates = self._bull_basket(pqc_filings, pqc_news, date, regime_score)
        elif regime_score < -threshold:
            # BEAR regime: CRQC stalling, quantum hype fading
            candidates = self._bear_basket(pqc_news, date, regime_score)
        else:
            # NEUTRAL: sparse signals, sit tight
            logger.info("Quantum readiness: neutral regime (score=%.2f), no trades", regime_score)
            return []

        # Enrich with OpenBB sector data
        openbb_data = data.get("openbb", {})
        profile_data = openbb_data.get("profile", {})
        if isinstance(profile_data, dict):
            for c in candidates:
                if c.ticker in profile_data:
                    c.metadata["sector"] = profile_data[c.ticker].get("sector", "")
                    c.metadata["industry"] = profile_data[c.ticker].get("industry", "")

        return candidates[:params.get("max_positions", 4)]

    def _compute_regime_score(
        self, filings: list[dict], news: list[dict],
    ) -> float:
        """Compute regime score from -1.0 (bear) to +1.0 (bull).

        Bull signals: PQC filings increasing, positive vendor news, milestones.
        Bear signals: no filings, quantum delay news, skeptic coverage.
        """
        score = 0.0

        # Filing signal: each PQC-mentioning filing is a bull signal
        # (companies are taking it seriously → timeline is real)
        filing_count = len(filings)
        if filing_count >= 5:
            score += 0.4
        elif filing_count >= 2:
            score += 0.2
        elif filing_count >= 1:
            score += 0.1

        # News signal balance
        bull_count = 0
        bear_count = 0
        for article in news:
            text = (
                article.get("headline", "") + " " + article.get("summary", "")
            ).lower()
            if any(kw in text for kw in BULL_NEWS_KEYWORDS):
                bull_count += 1
            if any(kw in text for kw in BEAR_NEWS_KEYWORDS):
                bear_count += 1

        total_news = bull_count + bear_count
        if total_news > 0:
            news_balance = (bull_count - bear_count) / total_news  # -1 to +1
            score += news_balance * 0.4

        # Check for quantum threat language in filings (bull: companies worried)
        threat_count = sum(
            1 for f in filings
            if any(
                kw in f.get("filing_text", "").lower()
                for kw in QUANTUM_THREAT_KEYWORDS
            )
        )
        if threat_count >= 2:
            score += 0.2
        elif threat_count >= 1:
            score += 0.1

        return max(-1.0, min(1.0, score))

    def _bull_basket(
        self,
        filings: list[dict],
        news: list[dict],
        date: str,
        regime_score: float,
    ) -> list[Candidate]:
        """Bull regime: long PQC vendors, short crypto-exposed laggards."""
        candidates: list[Candidate] = []

        # Long PQC vendors with news catalysts
        mentioned_vendors = set()
        for article in news:
            symbol = article.get("symbol", "")
            if symbol in PQC_VENDORS:
                mentioned_vendors.add(symbol)

        for ticker in mentioned_vendors:
            candidates.append(
                Candidate(
                    ticker=ticker,
                    date=date,
                    direction="long",
                    score=0.6 + (regime_score * 0.2),
                    metadata={
                        "basket": "pqc_vendor",
                        "regime": "bull",
                        "regime_score": regime_score,
                        "needs_llm_analysis": True,
                        "analysis_type": "quantum_readiness",
                    },
                )
            )

        # If filing signals are strong, also long IBM/MSFT as PQC leaders
        if len(filings) >= 3 and not mentioned_vendors:
            for ticker in ["IBM", "MSFT"]:
                candidates.append(
                    Candidate(
                        ticker=ticker,
                        date=date,
                        direction="long",
                        score=0.5 + (regime_score * 0.2),
                        metadata={
                            "basket": "pqc_vendor",
                            "regime": "bull",
                            "regime_score": regime_score,
                            "needs_llm_analysis": True,
                            "analysis_type": "quantum_readiness",
                            "filing_count": len(filings),
                        },
                    )
                )

        # Short crypto-exposed names (migration laggards)
        for ticker in CRYPTO_EXPOSED[:3]:
            candidates.append(
                Candidate(
                    ticker=ticker,
                    date=date,
                    direction="short",
                    score=0.5 + (regime_score * 0.15),
                    metadata={
                        "basket": "crypto_exposed",
                        "regime": "bull",
                        "regime_score": regime_score,
                        "needs_llm_analysis": True,
                        "analysis_type": "quantum_readiness",
                        "rationale": "centralized systems migrate faster than decentralized",
                    },
                )
            )

        return candidates

    def _bear_basket(
        self,
        news: list[dict],
        date: str,
        regime_score: float,
    ) -> list[Candidate]:
        """Bear regime: short quantum hardware (overvalued on timeline compression)."""
        candidates: list[Candidate] = []

        for ticker in QUANTUM_HARDWARE:
            candidates.append(
                Candidate(
                    ticker=ticker,
                    date=date,
                    direction="short",
                    score=0.5 + (abs(regime_score) * 0.2),
                    metadata={
                        "basket": "quantum_hardware",
                        "regime": "bear",
                        "regime_score": regime_score,
                        "needs_llm_analysis": True,
                        "analysis_type": "quantum_readiness",
                        "rationale": "physics bottleneck skeptics winning, timeline compression unpriced",
                    },
                )
            )

        return candidates

    # ------------------------------------------------------------------
    # Exit logic
    # ------------------------------------------------------------------

    def check_exit(
        self,
        ticker: str,
        entry_price: float,
        current_price: float,
        holding_days: int,
        params: dict,
        data: dict,
    ) -> tuple[bool, str]:
        hold_days = params.get("hold_days", 25)
        if holding_days >= hold_days:
            return True, "hold_period"

        pnl_pct = (current_price - entry_price) / entry_price
        # Take profit at 10% (structural shift = larger moves)
        if abs(pnl_pct) > 0.10:
            return True, "take_profit"
        # Stop loss at 7%
        if pnl_pct < -0.07:
            return True, "stop_loss"

        return False, ""

    # ------------------------------------------------------------------
    # Parameter proposal
    # ------------------------------------------------------------------

    def build_propose_prompt(self, context: dict) -> str:
        current = context.get("current_params", self.get_default_params())
        return f"""You are optimizing a Quantum Readiness strategy that trades the
post-quantum cryptography migration. It is a regime-switching strategy:

- Bull regime (CRQC accelerating): long PQC vendors, short crypto-exposed.
- Bear regime (CRQC stalling): short quantum hardware pure-plays.
- Neutral: no trades.

The regime_threshold controls how much evidence is needed before committing
to a basket. Too low = whipsaws, too high = misses moves.

Current parameters: {current}

Parameter ranges:
- hold_days: 20-45
- min_conviction: 0.3-0.8
- max_positions: 2-6
- filing_lookback_days: 7-30
- news_lookback_days: 3-14
- regime_threshold: 0.2-0.5

Suggest 3 parameter combinations. Return JSON array of 3 param dicts."""
