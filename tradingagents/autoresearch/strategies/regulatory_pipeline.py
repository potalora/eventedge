"""P5: Regulatory Pipeline -- proposed rules to affected companies.

Maps newly proposed federal regulations to publicly traded companies
most likely affected. Uses regulations.gov API for rule discovery,
then LLM to identify impacted sectors/tickers.

Academic basis: regulatory announcements create predictable price
impacts on affected firms (Binder 1985, JFQA).
"""
from __future__ import annotations

import logging
from typing import Any

from .base import Candidate

logger = logging.getLogger(__name__)


class RegulatoryPipelineStrategy:
    """Paper-trade strategy mapping proposed regulations to affected stocks."""

    name = "regulatory_pipeline"
    track = "paper_trade"
    data_sources = ["regulations", "yfinance", "openbb"]

    def get_param_space(self) -> dict[str, tuple]:
        return {
            "hold_days": (20, 90),
            "min_conviction": (0.3, 0.8),
            "max_positions": (2, 5),
            "agencies": (
                ["SEC", "EPA", "FDA"],
                ["SEC", "EPA", "FDA", "DOL", "FTC", "CFPB"],
            ),
            "days_lookback": (7, 30),
        }

    def get_default_params(self) -> dict[str, Any]:
        return {
            "hold_days": 30,
            "min_conviction": 0.5,
            "max_positions": 3,
            "agencies": ["SEC", "EPA", "FDA", "FTC"],
            "days_lookback": 14,
        }

    def screen(self, data: dict, date: str, params: dict) -> list[Candidate]:
        """Screen recently proposed rules for trading signals.

        Expects data["regulations"]["proposed_rules"] = list of dicts with
        {document_id, title, agency_id, summary, ...}.
        """
        reg_data = data.get("regulations", {})
        rules = reg_data.get("proposed_rules", [])

        if not rules:
            return []

        agencies = set(params.get("agencies", ["SEC", "EPA", "FDA", "FTC"]))
        candidates = []

        for rule in rules:
            agency = rule.get("agency_id", "")
            if agency not in agencies:
                continue

            candidates.append(
                Candidate(
                    ticker="",  # LLM will map to tickers
                    date=date,
                    direction="short",  # Regulation typically bearish for affected
                    score=0.5,
                    metadata={
                        "document_id": rule.get("document_id", ""),
                        "title": rule.get("title", ""),
                        "agency_id": agency,
                        "summary": rule.get("summary", "")[:2000],
                        "posted_date": rule.get("posted_date", ""),
                        "needs_llm_analysis": True,
                        "analysis_type": "regulation",
                    },
                )
            )

        # Validate ticker-to-regulation mapping with sector data
        openbb_data = data.get("openbb", {})
        profile_data = openbb_data.get("profile", {})
        if isinstance(profile_data, dict):
            for candidate in candidates:
                ticker = candidate.ticker
                if ticker in profile_data:
                    candidate.metadata["sector"] = profile_data[ticker].get("sector", "")
                    candidate.metadata["industry"] = profile_data[ticker].get("industry", "")

        return candidates[: params.get("max_positions", 3)]

    def check_exit(
        self,
        ticker: str,
        entry_price: float,
        current_price: float,
        holding_days: int,
        params: dict,
        data: dict,
    ) -> tuple[bool, str]:
        hold_days = params.get("hold_days", 45)
        if holding_days >= hold_days:
            return True, "hold_period"
        return False, ""

    def build_propose_prompt(self, context: dict) -> str:
        current = context.get("current_params", self.get_default_params())
        return f"""You are optimizing a Regulatory Pipeline strategy that maps
proposed federal regulations to affected publicly traded companies.

Current parameters: {current}

Parameter ranges:
- hold_days: 20-90 (regulatory impact unfolds slowly)
- min_conviction: 0.3-0.8
- max_positions: 2-5
- agencies: subset of [SEC, EPA, FDA, DOL, FTC, CFPB]
- days_lookback: 7-30

Suggest 3 parameter combinations. Return JSON array of 3 param dicts."""
