"""P10: Pre-filing Litigation/Investigation Detection.

Monitors federal court dockets for new lawsuits and investigations
against public companies. Securities class actions, FTC investigations,
DOJ probes, and patent trolling all create predictable price impacts.

Academic basis: Karpoff et al. (2008, JFE) show enforcement actions
lead to -38% loss in market-adjusted value. Early detection = edge.

Data source: CourtListener (free API, 5,000 req/hour).
"""
from __future__ import annotations

import logging
import re
from typing import Any

from .base import Candidate

logger = logging.getLogger(__name__)

_PLAINTIFF_SUFFIXES = re.compile(
    r"\s*(Securities Litigation|Class Action|Derivative Action|Shareholder Litigation)\s*$",
    re.IGNORECASE,
)

# High-signal nature of suit codes (federal civil)
SIGNAL_NATURES = {
    "Securities/Commodities/Exchange",
    "Antitrust",
    "RICO",
    "Patent",
    "Environmental Matters",
    "Consumer Credit",
    "Fraud",
}


class LitigationStrategy:
    """Paper-trade strategy detecting pre-filing litigation signals."""

    name = "litigation"
    track = "paper_trade"
    data_sources = ["courtlistener", "yfinance", "openbb"]

    def get_param_space(self) -> dict[str, tuple]:
        return {
            "hold_days": (20, 45),
            "min_conviction": (0.3, 0.8),
            "max_positions": (2, 5),
            "lookback_days": (7, 30),
        }

    def get_default_params(self) -> dict[str, Any]:
        return {
            "hold_days": 25,
            "min_conviction": 0.5,
            "max_positions": 3,
            "lookback_days": 14,
        }

    def screen(self, data: dict, date: str, params: dict) -> list[Candidate]:
        """Screen for new litigation signals.

        Expects data["courtlistener"]["dockets"] = list of dicts with
        {docket_id, case_name, court, date_filed, nature_of_suit, cause, ...}.
        """
        cl_data = data.get("courtlistener", {})
        dockets = cl_data.get("dockets", [])

        if not dockets:
            return []

        candidates = []
        for docket in dockets:
            nature = docket.get("nature_of_suit", "")
            case_name = docket.get("case_name", "")

            # Score boost for high-signal case types (was a hard gate)
            is_high_signal = any(s.lower() in nature.lower() for s in SIGNAL_NATURES)
            is_class_action = self._is_class_action(case_name)

            # Gate: any case with a resolvable company ticker
            ticker = self._extract_ticker(case_name)
            if not ticker and not is_high_signal and not is_class_action:
                continue

            base_score = 0.7 if is_high_signal else 0.5
            if is_class_action:
                base_score = max(base_score, 0.6)

            candidates.append(
                Candidate(
                    ticker=ticker,
                    date=date,
                    direction="short",
                    score=base_score,
                    metadata={
                        "docket_id": docket.get("docket_id", ""),
                        "case_name": case_name,
                        "court": docket.get("court", ""),
                        "date_filed": docket.get("date_filed", ""),
                        "nature_of_suit": nature,
                        "cause": docket.get("cause", ""),
                        "is_high_signal_nature": is_high_signal,
                        "is_class_action": is_class_action,
                        "needs_llm_analysis": True,
                        "analysis_type": "litigation",
                    },
                )
            )

        # Merge SEC enforcement actions (higher signal quality)
        openbb_data = data.get("openbb", {})
        sec_lit = openbb_data.get("sec_litigation", {})
        sec_releases = sec_lit.get("releases", []) if isinstance(sec_lit, dict) else []
        for release in sec_releases:
            title = release.get("title", "")
            candidates.append(
                Candidate(
                    ticker="",  # LLM will resolve from title
                    date=date,
                    direction="short",  # Enforcement = bearish for target
                    score=0.8,  # High base score for SEC actions
                    metadata={
                        "source": "sec_enforcement",
                        "title": title[:200],
                        "url": release.get("url", ""),
                        "release_date": release.get("date", ""),
                        "needs_llm_analysis": True,
                    },
                )
            )

        return candidates[: params.get("max_positions", 3)]

    def _is_class_action(self, case_name: str) -> bool:
        """Check if case name suggests a class action."""
        lower = case_name.lower()
        return "class action" in lower or "securities" in lower or " v. " in lower

    def _extract_ticker(self, case_name: str) -> str:
        """Best-effort extraction of defendant company ticker.

        Parses patterns like "Smith v. Apple Inc." or "In re Apple Inc. Securities".
        Returns empty string if not resolvable -- LLM analyzer can refine.
        """
        defendant = ""

        # "X v. Y" or "X vs. Y" — take the defendant (second part)
        for sep in (" v. ", " vs. ", " v ", " vs "):
            if sep in case_name:
                defendant = case_name.split(sep, 1)[1].strip()
                break

        # "In re X" pattern
        if not defendant:
            m = re.match(r"(?i)in\s+re\s+(.+)", case_name)
            if m:
                defendant = m.group(1).strip()

        if not defendant:
            return ""

        # Strip litigation suffixes
        defendant = _PLAINTIFF_SUFFIXES.sub("", defendant).strip(" .,")

        if not defendant:
            return ""

        # Resolve via SEC company_tickers.json
        try:
            from tradingagents.autoresearch.data_sources.edgar_source import EDGARSource
            source = EDGARSource()
            ticker = source.name_to_ticker(defendant)
            if ticker:
                return ticker
        except Exception:
            logger.debug("name_to_ticker lookup failed for %r", defendant, exc_info=True)

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
        hold_days = params.get("hold_days", 25)
        if holding_days >= hold_days:
            return True, "hold_period"
        return False, ""

    def build_propose_prompt(self, context: dict) -> str:
        current = context.get("current_params", self.get_default_params())
        return f"""You are optimizing a Litigation Detection strategy that monitors
federal court dockets for new lawsuits/investigations against public companies.

Investment horizon: 30 days. Court filing impact + analyst reaction takes
~1 month. Cases don't resolve in 30 days, but sentiment shift does.

Current parameters: {current}

Parameter ranges:
- hold_days: 20-45 (target ~25-30 days)
- min_conviction: 0.3-0.8
- max_positions: 2-5
- lookback_days: 7-30

Suggest 3 parameter combinations. Return JSON array of 3 param dicts."""
