from __future__ import annotations

import logging
from typing import Any

import pandas as pd

from .base import Candidate

logger = logging.getLogger(__name__)

# Regional bank / retailer ETFs as proxies for state economic conditions
REGIONAL_ETFS = {
    "regional_banks": "KRE",  # SPDR S&P Regional Banking ETF
    "small_cap_value": "IWN",  # iShares Russell 2000 Value ETF
    "retail": "XRT",  # SPDR S&P Retail ETF
    "real_estate": "IYR",  # iShares US Real Estate ETF
    "homebuilders": "XHB",  # SPDR S&P Homebuilders ETF
    "homebuilders_focused": "ITB",  # iShares US Home Construction ETF
    "broad_reit": "VNQ",  # Vanguard Real Estate ETF
    "semiconductors": "SOXX",  # iShares Semiconductor ETF
    "industrials": "XLI",  # Industrial Select Sector SPDR
    "real_estate_sector": "XLRE",  # Real Estate Select Sector SPDR
}


class StateEconomicsStrategy:
    """Retired unsupported state-event proxy; retained for historical exits."""

    retirement_reason = "unsupported_state_event_proxy"

    name = "state_economics"
    track = "paper_trade"
    data_sources = ["yfinance", "fred", "openbb"]

    def get_param_space(self, horizon: str = "30d") -> dict[str, tuple]:
        return {}

    def get_default_params(self, horizon: str = "30d") -> dict[str, Any]:
        from tradingagents.strategies.orchestration.cohort_orchestrator import HORIZON_PARAMS
        return {"rebalance_days": HORIZON_PARAMS.get(horizon, HORIZON_PARAMS["30d"])["hold_days_default"]}

    def screen(self, data: dict, date: str, params: dict) -> list[Candidate]:
        """Screen for regional ETFs using economic indicators + momentum composite.

        Combines FRED economic indicators with ETF momentum for a
        composite signal. Falls back to pure momentum if FRED unavailable.
        """
        # Retired: national FRED indicators plus sector ETF momentum do not
        # establish a state-specific economic catalyst or attributable issuer.
        return []

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
        """Exit on rebalance schedule."""
        rebalance_days = params.get("rebalance_days", 30)
        if holding_days >= rebalance_days:
            return True, "rebalance"
        return False, ""

    def build_propose_prompt(self, context: dict) -> str:
        raise RuntimeError("state_economics is retired: unsupported_state_event_proxy")
