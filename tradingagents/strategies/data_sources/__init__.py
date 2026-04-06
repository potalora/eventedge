from __future__ import annotations

from tradingagents.strategies.data_sources.registry import (
    DataSource,
    DataSourceRegistry,
    build_default_registry,
)
from tradingagents.strategies.data_sources.yfinance_source import YFinanceSource
from tradingagents.strategies.data_sources.edgar_source import EDGARSource
from tradingagents.strategies.data_sources.usaspending_source import USASpendingSource
from tradingagents.strategies.data_sources.congress_source import CongressSource
from tradingagents.strategies.data_sources.fred_source import FREDSource
from tradingagents.strategies.data_sources.finnhub_source import FinnhubSource
from tradingagents.strategies.data_sources.regulations_source import RegulationsSource
from tradingagents.strategies.data_sources.courtlistener_source import CourtListenerSource
from tradingagents.strategies.data_sources.openbb_source import OpenBBSource
from tradingagents.strategies.data_sources.usda_source import USDASource
from tradingagents.strategies.data_sources.drought_monitor_source import DroughtMonitorSource
from tradingagents.strategies.data_sources.cftc_source import CFTCSource

__all__ = [
    "DataSource",
    "DataSourceRegistry",
    "build_default_registry",
    "YFinanceSource",
    "EDGARSource",
    "USASpendingSource",
    "CongressSource",
    "FREDSource",
    "FinnhubSource",
    "RegulationsSource",
    "CourtListenerSource",
    "OpenBBSource",
    "USDASource",
    "DroughtMonitorSource",
    "CFTCSource",
]
