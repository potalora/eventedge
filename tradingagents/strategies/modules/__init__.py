from __future__ import annotations

from .base import (
    BacktestResult,
    BacktestTrade,
    Candidate,
    StrategyModule,
    StrategyParams,
)
# Paper-trade strategies (EDGAR-based, unified)
from .insider_activity import InsiderActivityStrategy
from .filing_analysis import FilingAnalysisStrategy
# Paper-trade strategies (API-key sources)
from .earnings_call import EarningsCallStrategy
from .regulatory_pipeline import RegulatoryPipelineStrategy
from .supply_chain import SupplyChainStrategy
from .litigation import LitigationStrategy
from .congressional_trades import CongressionalTradesStrategy
from .govt_contracts import GovtContractsStrategy
from .state_economics import StateEconomicsStrategy
from .weather_ag import WeatherAgStrategy
from .commodity_macro import CommodityMacroStrategy
from .quantum_readiness import QuantumReadinessStrategy

__all__ = [
    # Protocol and data classes
    "BacktestResult",
    "BacktestTrade",
    "Candidate",
    "StrategyModule",
    "StrategyParams",
    # Paper-trade strategies
    "EarningsCallStrategy",
    "InsiderActivityStrategy",
    "RegulatoryPipelineStrategy",
    "SupplyChainStrategy",
    "FilingAnalysisStrategy",
    "LitigationStrategy",
    "CongressionalTradesStrategy",
    "GovtContractsStrategy",
    "StateEconomicsStrategy",
    "WeatherAgStrategy",
    "CommodityMacroStrategy",
    "QuantumReadinessStrategy",
    # Helper
    "get_all_strategies",
    "get_paper_trade_strategies",
]


def get_all_strategies() -> list[StrategyModule]:
    """Return instances of all available strategy modules."""
    return get_paper_trade_strategies()


def get_paper_trade_strategies() -> list[StrategyModule]:
    """Return all paper-trade strategy instances."""
    return [
        EarningsCallStrategy(),
        InsiderActivityStrategy(),
        RegulatoryPipelineStrategy(),
        SupplyChainStrategy(),
        FilingAnalysisStrategy(),
        LitigationStrategy(),
        CongressionalTradesStrategy(),
        GovtContractsStrategy(),
        StateEconomicsStrategy(),
        WeatherAgStrategy(),
        CommodityMacroStrategy(),
        QuantumReadinessStrategy(),
    ]
