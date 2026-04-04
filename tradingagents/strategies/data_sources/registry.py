from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class DataSource(Protocol):
    """Protocol that all data sources must implement."""

    @property
    def name(self) -> str:
        """Unique identifier for this data source."""
        ...

    @property
    def requires_api_key(self) -> bool:
        """Whether this source needs an API key to function."""
        ...

    def fetch(self, params: dict[str, Any]) -> dict[str, Any]:
        """Generic fetch dispatcher. Params should include 'method' key."""
        ...

    def is_available(self) -> bool:
        """Check whether this source can currently serve requests."""
        ...


class DataSourceRegistry:
    """Central registry of all data sources available to the autoresearch system."""

    def __init__(self) -> None:
        self._sources: dict[str, DataSource] = {}

    def register(self, source: DataSource) -> None:
        """Register a data source instance."""
        self._sources[source.name] = source
        logger.info("Registered data source: %s", source.name)

    def get(self, name: str) -> DataSource | None:
        """Get a registered data source by name."""
        return self._sources.get(name)

    def available_sources(self) -> list[str]:
        """Return names of all sources that are currently available."""
        available: list[str] = []
        for name, source in self._sources.items():
            try:
                if source.is_available():
                    available.append(name)
            except Exception:
                logger.warning("Error checking availability of %s", name, exc_info=True)
        return available

    def fetch_all(
        self, source_names: list[str], **kwargs: Any
    ) -> dict[str, dict[str, Any]]:
        """Fetch data from multiple sources in sequence.

        Args:
            source_names: List of source names to fetch from.
            **kwargs: Passed as params to each source's fetch() method.

        Returns:
            Dict mapping source name to its fetch result.
        """
        results: dict[str, dict[str, Any]] = {}
        for name in source_names:
            source = self._sources.get(name)
            if source is None:
                logger.warning("Source '%s' not registered, skipping", name)
                continue
            try:
                if not source.is_available():
                    logger.warning("Source '%s' not available, skipping", name)
                    continue
                results[name] = source.fetch(kwargs)
            except Exception:
                logger.error("Error fetching from %s", name, exc_info=True)
                results[name] = {"error": f"fetch failed for {name}"}
        return results


def build_default_registry(config: dict[str, Any] | None = None) -> DataSourceRegistry:
    """Create a registry pre-loaded with all default data sources.

    Args:
        config: Optional config dict (autoresearch section).

    Returns:
        A DataSourceRegistry with available sources registered.
    """
    from tradingagents.autoresearch.data_sources.yfinance_source import YFinanceSource
    from tradingagents.autoresearch.data_sources.edgar_source import EDGARSource
    from tradingagents.autoresearch.data_sources.usaspending_source import USASpendingSource
    from tradingagents.autoresearch.data_sources.congress_source import CongressSource
    from tradingagents.autoresearch.data_sources.fred_source import FREDSource
    from tradingagents.autoresearch.data_sources.finnhub_source import FinnhubSource
    from tradingagents.autoresearch.data_sources.regulations_source import RegulationsSource
    from tradingagents.autoresearch.data_sources.courtlistener_source import CourtListenerSource

    config = config or {}
    registry = DataSourceRegistry()

    registry.register(YFinanceSource())

    edgar_ua = config.get("edgar_user_agent", "TradingAgents research@example.com")
    registry.register(EDGARSource(user_agent=edgar_ua))

    registry.register(USASpendingSource())
    registry.register(CongressSource())

    # API-key sources (free keys)
    registry.register(FREDSource(api_key=config.get("fred_api_key")))
    registry.register(FinnhubSource(api_key=config.get("finnhub_api_key")))
    registry.register(RegulationsSource(api_key=config.get("regulations_api_key")))
    registry.register(CourtListenerSource(token=config.get("courtlistener_token")))

    from tradingagents.autoresearch.data_sources.noaa_source import NOAASource
    registry.register(NOAASource(token=config.get("noaa_cdo_token")))

    from tradingagents.autoresearch.data_sources.usda_source import USDASource
    registry.register(USDASource(api_key=config.get("usda_nass_api_key")))

    from tradingagents.autoresearch.data_sources.drought_monitor_source import DroughtMonitorSource
    registry.register(DroughtMonitorSource())

    # OpenBB Platform (optional — graceful skip if not installed)
    try:
        from tradingagents.autoresearch.data_sources.openbb_source import OpenBBSource

        registry.register(OpenBBSource(fmp_api_key=config.get("fmp_api_key")))
    except ImportError:
        logger.info("openbb not installed — OpenBBSource skipped")

    return registry
