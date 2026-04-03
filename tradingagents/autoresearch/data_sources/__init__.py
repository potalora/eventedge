from __future__ import annotations

from tradingagents.autoresearch.data_sources.registry import (
    DataSource,
    DataSourceRegistry,
    build_default_registry,
)
from tradingagents.autoresearch.data_sources.yfinance_source import YFinanceSource
from tradingagents.autoresearch.data_sources.edgar_source import EDGARSource
from tradingagents.autoresearch.data_sources.usaspending_source import USASpendingSource
from tradingagents.autoresearch.data_sources.congress_source import CongressSource
from tradingagents.autoresearch.data_sources.fred_source import FREDSource
from tradingagents.autoresearch.data_sources.finnhub_source import FinnhubSource
from tradingagents.autoresearch.data_sources.regulations_source import RegulationsSource
from tradingagents.autoresearch.data_sources.courtlistener_source import CourtListenerSource
from tradingagents.autoresearch.data_sources.openbb_source import OpenBBSource

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
]
