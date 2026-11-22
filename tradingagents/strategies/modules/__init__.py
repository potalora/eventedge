from __future__ import annotations

from .base import (
    BacktestResult,
    BacktestTrade,
    Candidate,
    StrategyModule,
    StrategyParams,
)
from .earnings_call import EarningsCallStrategy
from .supply_chain import SupplyChainStrategy
from .govt_contracts import GovtContractsStrategy
from .weather_ag import WeatherAgStrategy
from .commodity_macro import CommodityMacroStrategy

__all__ = [
    # Protocol and data classes
    "BacktestResult",
    "BacktestTrade",
    "Candidate",
    "StrategyModule",
    "StrategyParams",
    # Active paper-trade strategies (5)
    "EarningsCallStrategy",
    "SupplyChainStrategy",
    "GovtContractsStrategy",
    "WeatherAgStrategy",
    "CommodityMacroStrategy",
    # Helper
    "get_all_strategies",
    "get_paper_trade_strategies",
]


def get_all_strategies() -> list[StrategyModule]:
    """Return instances of all active strategy modules."""
    return get_paper_trade_strategies()


def get_paper_trade_strategies() -> list[StrategyModule]:
    """Return the 5 active paper-trade strategy instances."""
    return [
        EarningsCallStrategy(),
        SupplyChainStrategy(),
        GovtContractsStrategy(),
        WeatherAgStrategy(),
        CommodityMacroStrategy(),
    ]
