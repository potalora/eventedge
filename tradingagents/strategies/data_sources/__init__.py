from __future__ import annotations

from tradingagents.strategies.data_sources.registry import (
    DataSource,
    DataSourceRegistry,
    build_default_registry,
)
from tradingagents.strategies.data_sources.yfinance_source import YFinanceSource
from tradingagents.strategies.data_sources.usaspending_source import USASpendingSource
from tradingagents.strategies.data_sources.fred_source import FREDSource
from tradingagents.strategies.data_sources.finnhub_source import FinnhubSource
from tradingagents.strategies.data_sources.noaa_yearly_source import NOAAYearlySource
from tradingagents.strategies.data_sources.enrichment_source import EnrichmentSource
from tradingagents.strategies.data_sources.usda_source import USDASource
from tradingagents.strategies.data_sources.drought_monitor_source import DroughtMonitorSource
from tradingagents.strategies.data_sources.cftc_source import CFTCSource

__all__ = [
    "DataSource",
    "DataSourceRegistry",
    "build_default_registry",
    "YFinanceSource",
    "USASpendingSource",
    "FREDSource",
    "FinnhubSource",
    "NOAAYearlySource",
    "EnrichmentSource",
    "USDASource",
    "DroughtMonitorSource",
    "CFTCSource",
]
