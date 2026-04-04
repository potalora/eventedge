from __future__ import annotations

import logging
from typing import Any

from .base import Candidate

logger = logging.getLogger(__name__)

# Major public companies with common WARN notice filings
KNOWN_EMPLOYERS: dict[str, str] = {
    "amazon": "AMZN", "google": "GOOGL", "alphabet": "GOOGL",
    "meta": "META", "facebook": "META", "microsoft": "MSFT",
    "apple": "AAPL", "tesla": "TSLA", "ford": "F",
    "general motors": "GM", "boeing": "BA", "lockheed": "LMT",
    "intel": "INTC", "walmart": "WMT", "target": "TGT",
    "disney": "DIS", "netflix": "NFLX", "uber": "UBER",
    "lyft": "LYFT", "salesforce": "CRM", "cisco": "CSCO",
    "ibm": "IBM", "dell": "DELL", "hp ": "HPQ",
    "goldman sachs": "GS", "morgan stanley": "MS",
    "jpmorgan": "JPM", "citigroup": "C", "wells fargo": "WFC",
}


class FilingAnalysisStrategy:
    """Unified filing analysis strategy (merges P3 filing_changes + P8 warn_act + P9 exec_comp).

    Processes all EDGAR filing types in a single pass:
    - 10-K/10-Q: material changes analysis (from P3)
    - DEF 14A: executive compensation analysis (from P9)
    - Any filing from a known WARN employer: layoff risk signal (from P8)

    Academic basis:
    - Cohen et al. (2020, JoF "Lazy Prices"): 10-K/10-Q language changes
      predict 3.5-4.5%/year underperformance.
    - Core et al. (1999, JFE), Bebchuk & Fried (2004): compensation design
      linked to firm value.
    - WARN Act: 60-day advance layoff notice is public but under-monitored.
    """

    name = "filing_analysis"
    track = "paper_trade"
    data_sources = ["edgar", "yfinance", "openbb"]

    def get_param_space(self) -> dict[str, tuple]:
        return {
            "hold_days": (20, 45),
            "min_conviction": (0.3, 0.7),
            "max_positions": (3, 8),
            "forms_to_analyze": (
                ["10-K", "10-Q"],
                ["10-K", "10-Q", "DEF 14A", "8-K", "SC 13D", "SC 13G"],
            ),
        }

    def get_default_params(self) -> dict[str, Any]:
        return {
            "hold_days": 25,
            "min_conviction": 0.5,
            "max_positions": 5,
            "forms_to_analyze": ["10-K", "10-Q", "DEF 14A", "8-K", "SC 13D", "SC 13G"],
        }

    def screen(self, data: dict, date: str, params: dict) -> list[Candidate]:
        """Screen EDGAR filings for material changes, exec comp shifts, and WARN signals."""
        edgar_data = data.get("edgar", {})
        filings = edgar_data.get("filings", [])

        if not filings:
            return []

        forms_to_analyze = params.get("forms_to_analyze", ["10-K", "10-Q", "DEF 14A"])
        candidates = []

        for filing in filings:
            form_type = filing.get("form_type", "")
            entity_name = filing.get("entity_name", "")
            ticker = filing.get("ticker", "")

            # Check for WARN employer match (from P8)
            warn_ticker = self._resolve_warn_ticker(entity_name)
            if warn_ticker:
                candidates.append(
                    Candidate(
                        ticker=warn_ticker,
                        date=date,
                        direction="short",
                        score=0.3,
                        metadata={
                            "form_type": form_type,
                            "entity_name": entity_name,
                            "file_date": filing.get("file_date", ""),
                            "needs_llm_analysis": False,
                            "analysis_type": "warn_act",
                            "signal_source": "edgar_filing_proxy",
                        },
                    )
                )

            # 10-K / 10-Q → material changes analysis (from P3)
            if form_type in ("10-K", "10-Q") and form_type in forms_to_analyze:
                current_text = filing.get("current_text", "")
                has_text = bool(current_text.strip())
                candidates.append(
                    Candidate(
                        ticker=ticker,
                        date=date,
                        direction="long",  # LLM analyzer will determine direction
                        score=0.5,
                        metadata={
                            "form_type": form_type,
                            "entity_name": entity_name,
                            "file_date": filing.get("file_date", ""),
                            "file_url": filing.get("file_url", ""),
                            "current_text": current_text,
                            "prior_text": filing.get("prior_text", ""),
                            "needs_llm_analysis": has_text,
                            "analysis_type": "filing_change",
                        },
                    )
                )

            # DEF 14A → exec comp analysis (from P9)
            elif form_type == "DEF 14A" and form_type in forms_to_analyze:
                proxy_text = filing.get("proxy_text", "")
                has_text = bool(proxy_text.strip())
                candidates.append(
                    Candidate(
                        ticker=ticker,
                        date=date,
                        direction="long",  # LLM analyzer will determine direction
                        score=0.5,
                        metadata={
                            "form_type": form_type,
                            "entity_name": entity_name,
                            "file_date": filing.get("file_date", ""),
                            "file_url": filing.get("file_url", ""),
                            "proxy_text": proxy_text,
                            "needs_llm_analysis": has_text,
                            "analysis_type": "exec_comp",
                        },
                    )
                )

            # 8-K → material event announcement
            elif form_type == "8-K" and form_type in forms_to_analyze:
                event_text = filing.get("current_text", "")
                has_text = bool(event_text.strip())
                candidates.append(
                    Candidate(
                        ticker=ticker,
                        date=date,
                        direction="long",  # LLM will determine
                        score=0.6,  # 8-Ks are time-sensitive
                        metadata={
                            "form_type": form_type,
                            "entity_name": entity_name,
                            "file_date": filing.get("file_date", ""),
                            "file_url": filing.get("file_url", ""),
                            "current_text": event_text[:5000],
                            "needs_llm_analysis": has_text,
                            "analysis_type": "material_event",
                        },
                    )
                )

            # SC 13D/13G → activist or large passive stake
            elif form_type in ("SC 13D", "SC 13G") and form_type in forms_to_analyze:
                stake_text = filing.get("current_text", "")
                has_text = bool(stake_text.strip())
                is_activist = form_type == "SC 13D"
                candidates.append(
                    Candidate(
                        ticker=ticker,
                        date=date,
                        direction="long",  # Activist stakes are typically bullish
                        score=0.7 if is_activist else 0.4,
                        metadata={
                            "form_type": form_type,
                            "entity_name": entity_name,
                            "file_date": filing.get("file_date", ""),
                            "file_url": filing.get("file_url", ""),
                            "current_text": stake_text[:5000],
                            "needs_llm_analysis": has_text,
                            "analysis_type": "activist_stake" if is_activist else "passive_stake",
                        },
                    )
                )

        # Deduplicate by ticker (keep highest score)
        by_ticker: dict[str, Candidate] = {}
        for c in candidates:
            if not c.ticker:
                continue
            if c.ticker not in by_ticker or c.score > by_ticker[c.ticker].score:
                by_ticker[c.ticker] = c
        unique = sorted(by_ticker.values(), key=lambda c: c.score, reverse=True)

        # Enrich with analyst consensus for contradiction detection
        openbb_data = data.get("openbb", {})
        estimates = openbb_data.get("estimates", {})
        profile_data = openbb_data.get("profile", {})
        for candidate in unique:
            ticker = candidate.ticker
            if isinstance(estimates, dict) and ticker in estimates:
                est = estimates[ticker]
                candidate.metadata["consensus_eps"] = est.get("consensus_eps")
                candidate.metadata["consensus_revenue"] = est.get("consensus_revenue")
                candidate.metadata["price_target_mean"] = est.get("price_target_mean")
                if est.get("num_analysts", 0) >= 5:
                    candidate.score = min(candidate.score * 1.1, 1.0)
            if isinstance(profile_data, dict) and ticker in profile_data:
                candidate.metadata["sector"] = profile_data[ticker].get("sector", "")

        return unique[: params.get("max_positions", 5)]

    def _resolve_warn_ticker(self, entity_name: str) -> str:
        """Best-effort match of entity name to ticker via KNOWN_EMPLOYERS."""
        entity_lower = entity_name.lower()
        for name, ticker in KNOWN_EMPLOYERS.items():
            if name in entity_lower:
                return ticker
        return ""

    def check_exit(
        self,
        ticker: str,
        entry_price: float,
        current_price: float,
        holding_days: int,
        params: dict,
        data: dict,
    ) -> tuple[bool, str]:
        """Exit on hold period."""
        hold_days = params.get("hold_days", 25)
        if holding_days >= hold_days:
            return True, "hold_period"
        return False, ""

    def build_propose_prompt(self, context: dict) -> str:
        current = context.get("current_params", self.get_default_params())
        return f"""You are optimizing a unified Filing Analysis strategy that processes
EDGAR filings: 10-K/10-Q (material changes), DEF 14A (exec comp),
8-K (material events), SC 13D (activist stakes), SC 13G (large passive stakes).

Investment horizon: 30 days. Filing implications unfold over weeks as
analysts digest. SC 13D activist stakes are well-documented alpha sources.

Current parameters: {current}

Parameter ranges:
- hold_days: 20-45 (target ~25-30 days)
- min_conviction: 0.3-0.7
- max_positions: 3-8
- forms_to_analyze: subset of ["10-K", "10-Q", "DEF 14A", "8-K", "SC 13D", "SC 13G"]

Suggest 3 parameter combinations. Return JSON array of 3 param dicts."""
