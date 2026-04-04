from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

BASE_URL = "https://api.usaspending.gov/api/v2/"


class USASpendingSource:
    """Data source for federal contract awards via USASpending.gov API.

    No API key required. Provides search over government contract awards
    useful for identifying companies winning large federal contracts.
    """

    name: str = "usaspending"
    requires_api_key: bool = False

    def __init__(self) -> None:
        self._cache: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Protocol methods
    # ------------------------------------------------------------------

    def fetch(self, params: dict[str, Any]) -> dict[str, Any]:
        """Generic dispatcher.

        Supported params["method"] values:
            search_contracts, recent_large_contracts
        """
        method = params.get("method", "search_contracts")
        dispatch = {
            "search_contracts": self._dispatch_search_contracts,
            "recent_large_contracts": self._dispatch_recent_large,
        }
        handler = dispatch.get(method)
        if handler is None:
            return {"error": f"Unknown method '{method}'"}
        try:
            return handler(params)
        except Exception:
            logger.error("USASpendingSource.fetch(%s) failed", method, exc_info=True)
            return {"error": f"{method} fetch failed"}

    def is_available(self) -> bool:
        """USASpending is available if requests is installed."""
        try:
            import requests  # noqa: F401
            return True
        except ImportError:
            return False

    # ------------------------------------------------------------------
    # Public data methods
    # ------------------------------------------------------------------

    def search_contracts(
        self,
        keywords: list[str] | None = None,
        recipient: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        min_amount: float | None = None,
    ) -> list[dict[str, Any]]:
        """Search for federal contract awards.

        Args:
            keywords: Text search terms.
            recipient: Recipient/company name to filter by.
            date_from: Start date (YYYY-MM-DD).
            date_to: End date (YYYY-MM-DD).
            min_amount: Minimum award amount in USD.

        Returns:
            List of contract dicts with keys:
            award_id, recipient_name, amount, agency, start_date, description.
        """
        import requests

        filters: dict[str, Any] = {
            "award_type_codes": ["A", "B", "C", "D"],  # Contract types
        }
        if keywords:
            filters["keywords"] = keywords
        if recipient:
            filters["recipient_search_text"] = [recipient]
        if date_from or date_to:
            time_period = {}
            if date_from:
                time_period["start_date"] = date_from
            if date_to:
                time_period["end_date"] = date_to
            filters["time_period"] = [time_period]
        if min_amount is not None:
            filters["award_amounts"] = [{"lower_bound": min_amount}]

        payload = {
            "filters": filters,
            "fields": [
                "Award ID",
                "Recipient Name",
                "Award Amount",
                "Awarding Agency",
                "Start Date",
                "Description",
            ],
            "page": 1,
            "limit": 50,
            "sort": "Award Amount",
            "order": "desc",
        }

        try:
            resp = requests.post(
                f"{BASE_URL}search/spending_by_award/",
                json=payload,
                timeout=30,
            )
            if resp.status_code != 200:
                logger.warning("USASpending search returned %d", resp.status_code)
                return []

            data = resp.json()
            results: list[dict[str, Any]] = []
            for row in data.get("results", []):
                results.append({
                    "award_id": row.get("Award ID", ""),
                    "recipient_name": row.get("Recipient Name", ""),
                    "amount": row.get("Award Amount", 0),
                    "agency": row.get("Awarding Agency", ""),
                    "start_date": row.get("Start Date", ""),
                    "description": row.get("Description", ""),
                })
            return results
        except Exception:
            logger.error("search_contracts failed", exc_info=True)
            return []

    def get_recent_large_contracts(
        self,
        min_amount: float = 100_000_000,
        days_back: int = 30,
    ) -> list[dict[str, Any]]:
        """Find recent large federal contract awards.

        Args:
            min_amount: Minimum award amount in USD (default $100M).
            days_back: How many days back to search.

        Returns:
            List of contract dicts (same format as search_contracts).
        """
        cache_key = f"recent_large|{min_amount}|{days_back}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        date_from = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
        date_to = datetime.now().strftime("%Y-%m-%d")

        results = self.search_contracts(
            date_from=date_from,
            date_to=date_to,
            min_amount=min_amount,
        )
        self._cache[cache_key] = results
        return results

    # ------------------------------------------------------------------
    # Cache management
    # ------------------------------------------------------------------

    def clear_cache(self) -> None:
        """Clear all cached data."""
        self._cache.clear()

    # ------------------------------------------------------------------
    # Internal dispatch helpers
    # ------------------------------------------------------------------

    def _dispatch_search_contracts(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"data": self.search_contracts(
            keywords=params.get("keywords"),
            recipient=params.get("recipient"),
            date_from=params.get("date_from"),
            date_to=params.get("date_to"),
            min_amount=params.get("min_amount"),
        )}

    def _dispatch_recent_large(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"data": self.get_recent_large_contracts(
            min_amount=params.get("min_amount", 100_000_000),
            days_back=params.get("days_back", 30),
        )}
