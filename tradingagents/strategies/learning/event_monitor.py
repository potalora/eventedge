"""Event monitor for paper-trade strategies.

Polls data sources (EDGAR, etc.) for new events relevant to
paper-trade strategies. Each poll returns a list of events that
strategies can analyze for trading signals.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)


class EventMonitor:
    """Polls data sources for actionable events."""

    def __init__(self, registry: Any) -> None:
        """
        Args:
            registry: DataSourceRegistry instance.
        """
        self.registry = registry
        self._last_poll: dict[str, str] = {}  # source -> last poll timestamp

    def poll_edgar_filings(
        self,
        form_types: list[str],
        days_back: int = 7,
        fetch_text: bool = True,
        max_text_fetches: int = 10,
    ) -> list[dict]:
        """Check EDGAR for new filings since last poll.

        Args:
            form_types: SEC form types to search (e.g. ["SC 13D", "4", "10-K"]).
            days_back: How far back to search.
            fetch_text: If True, fetch filing text for 10-K/10-Q/DEF 14A.
            max_text_fetches: Max number of filings to fetch text for.

        Returns:
            List of filing dicts (enriched with text fields if fetch_text=True).
        """
        source = self.registry.get("edgar")
        if source is None or not source.is_available():
            return []

        date_from = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
        date_to = datetime.now().strftime("%Y-%m-%d")

        all_filings = []
        for form_type in form_types:
            filings = source.search_filings(
                form_type=form_type,
                date_from=date_from,
                date_to=date_to,
            )
            all_filings.extend(filings)

        # Fetch filing text for LLM analysis
        if fetch_text and source:
            text_forms = {"10-K", "10-Q", "DEF 14A"}
            fetched = 0
            for filing in all_filings:
                if fetched >= max_text_fetches:
                    break
                form = filing.get("form_type", "")
                url = filing.get("file_url", "")
                if form not in text_forms or not url:
                    continue

                raw_text = source.get_filing_text(url)
                if raw_text:
                    # Strip HTML tags for cleaner LLM input
                    clean_text = self._strip_html(raw_text)
                    if form == "DEF 14A":
                        filing["proxy_text"] = clean_text[:5000]
                    else:
                        filing["current_text"] = clean_text[:5000]
                        # Fetch prior filing of same type for comparison
                        filing["prior_text"] = self._fetch_prior_filing_text(
                            source, filing, form,
                        )
                    fetched += 1
                    logger.debug("Fetched text for %s %s (%d chars)", form, filing.get("ticker", "?"), len(clean_text))

        self._last_poll["edgar"] = datetime.now().isoformat()
        logger.info("EDGAR poll: %d filings found for %s", len(all_filings), form_types)
        return all_filings

    def _fetch_prior_filing_text(
        self,
        source: Any,
        filing: dict,
        form_type: str,
    ) -> str:
        """Fetch the previous filing of the same type for comparison.

        Uses the CIK from the current filing to look up the company's filing
        history and fetch the most recent prior filing of the same form type.

        Returns:
            Cleaned text (up to 5000 chars), or empty string if unavailable.
        """
        ciks = filing.get("ciks", [])
        if not ciks:
            return ""

        cik = ciks[0]
        current_date = filing.get("file_date", "")

        try:
            # Get recent filings of same type for this company
            company_filings = source.get_company_filings(
                cik=cik, form_types=[form_type], count=5,
            )
            # Find the first filing older than the current one
            prior_doc = None
            for cf in company_filings:
                if cf.get("filing_date", "") < current_date:
                    prior_doc = cf
                    break

            if not prior_doc:
                return ""

            # Build URL for prior filing
            accession = prior_doc.get("accession_number", "")
            primary_doc = prior_doc.get("primary_document", "")
            if not accession or not primary_doc:
                return ""

            cik_num = cik.lstrip("0")
            adsh_nod = accession.replace("-", "")
            prior_url = (
                f"https://www.sec.gov/Archives/edgar/data/"
                f"{cik_num}/{adsh_nod}/{primary_doc}"
            )

            raw = source.get_filing_text(prior_url)
            if raw:
                clean = self._strip_html(raw)
                logger.debug(
                    "Fetched prior %s for %s (%d chars)",
                    form_type, filing.get("ticker", "?"), len(clean),
                )
                return clean[:5000]
        except Exception:
            logger.warning(
                "Failed to fetch prior %s for %s",
                form_type, filing.get("ticker", "?"),
                exc_info=True,
            )
        return ""

    @staticmethod
    def _strip_html(text: str) -> str:
        """Remove HTML tags from filing text."""
        import re
        # Remove script/style blocks
        text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", text, flags=re.DOTALL | re.IGNORECASE)
        # Remove tags
        text = re.sub(r"<[^>]+>", " ", text)
        # Collapse whitespace
        text = re.sub(r"\s+", " ", text).strip()
        return text

    def poll_13d_filings(self, days_back: int = 14) -> list[dict]:
        """Poll for new SC 13D activist filings."""
        source = self.registry.get("edgar")
        if source is None or not source.is_available():
            return []
        return source.get_recent_13d(days_back=days_back)

    def poll_keyword_filings(
        self,
        form_types: list[str],
        keywords: list[str],
        days_back: int = 30,
        fetch_text: bool = True,
        max_text_fetches: int = 10,
    ) -> list[dict]:
        """Search EDGAR filings containing specific keywords.

        Searches across multiple form types and keywords, returning
        deduplicated filings. Useful for thematic strategies that need
        to find filings mentioning specific topics.

        Args:
            form_types: SEC form types to search (e.g. ["8-K", "10-K"]).
            keywords: Keywords to search within filing text.
            days_back: How far back to search.
            fetch_text: If True, fetch filing text for matched filings.
            max_text_fetches: Max number of filings to fetch text for.

        Returns:
            Deduplicated list of filing dicts.
        """
        source = self.registry.get("edgar")
        if source is None or not source.is_available():
            return []

        date_from = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
        date_to = datetime.now().strftime("%Y-%m-%d")

        seen_urls: set[str] = set()
        all_filings: list[dict] = []

        for form_type in form_types:
            for keyword in keywords:
                filings = source.search_filings(
                    form_type=form_type,
                    date_from=date_from,
                    date_to=date_to,
                    keyword=keyword,
                )
                for f in filings:
                    url = f.get("file_url", "")
                    if url and url not in seen_urls:
                        seen_urls.add(url)
                        f["matched_keyword"] = keyword
                        all_filings.append(f)

        # Fetch filing text for LLM analysis
        if fetch_text and source:
            fetched = 0
            for filing in all_filings:
                if fetched >= max_text_fetches:
                    break
                url = filing.get("file_url", "")
                if not url:
                    continue
                raw_text = source.get_filing_text(url)
                if raw_text:
                    filing["filing_text"] = self._strip_html(raw_text)[:5000]
                    fetched += 1

        logger.info(
            "Keyword filing poll: %d filings for %s across %s",
            len(all_filings), keywords, form_types,
        )
        return all_filings

    def poll_form4_filings(
        self, tickers: list[str], days_back: int = 14
    ) -> dict[str, list[dict]]:
        """Poll for new Form 4 (insider transaction) filings.

        Returns:
            Dict mapping ticker to list of Form 4 filings.
        """
        source = self.registry.get("edgar")
        if source is None or not source.is_available():
            return {}

        results: dict[str, list[dict]] = {}
        for ticker in tickers:
            filings = source.get_recent_form4(ticker, days_back=days_back)
            if filings:
                results[ticker] = filings
        return results

    def poll_large_contracts(
        self, min_amount: float = 50_000_000, days_back: int = 14
    ) -> list[dict]:
        """Poll USAspending for recent large contract awards."""
        source = self.registry.get("usaspending")
        if source is None or not source.is_available():
            return []
        return source.get_recent_large_contracts(
            min_amount=min_amount, days_back=days_back
        )

    def poll_congressional_trades(self, days_back: int = 30) -> list[dict]:
        """Poll for recent congressional stock trades."""
        source = self.registry.get("congress")
        if source is None or not source.is_available():
            return []
        return source.get_recent_trades(days_back=days_back)

    def poll_proposed_rules(
        self, agencies: list[str] | None = None, days_back: int = 14,
    ) -> list[dict]:
        """Poll regulations.gov for recently proposed rules."""
        source = self.registry.get("regulations")
        if source is None or not source.is_available():
            return []

        rules = source.get_recent_proposed_rules(
            agencies=agencies, days_back=days_back,
        )
        self._last_poll["regulations"] = datetime.now().isoformat()
        logger.info("Regulations.gov poll: %d proposed rules", len(rules))
        return rules

    def poll_court_dockets(
        self, query: str = "securities", days_back: int = 14,
    ) -> list[dict]:
        """Poll CourtListener for recent court dockets."""
        source = self.registry.get("courtlistener")
        if source is None or not source.is_available():
            return []

        date_from = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
        dockets = source.search_dockets(
            query=query, date_filed_after=date_from,
        )
        self._last_poll["courtlistener"] = datetime.now().isoformat()
        logger.info("CourtListener poll: %d dockets", len(dockets))
        return dockets

    def poll_all(self, config: dict | None = None) -> dict[str, list]:
        """Poll all configured sources for new events.

        Returns:
            Dict mapping event type to list of events.
        """
        events: dict[str, list] = {}

        # EDGAR filings (10-K, 10-Q, DEF 14A, SC 13D, Form 4)
        filings = self.poll_edgar_filings(
            form_types=["SC 13D", "4", "10-K", "10-Q", "DEF 14A"],
            days_back=7,
        )
        if filings:
            events["edgar_filings"] = filings

        # 13D activist filings
        filings_13d = self.poll_13d_filings(days_back=14)
        if filings_13d:
            events["activist_13d"] = filings_13d

        # Large government contracts
        contracts = self.poll_large_contracts(min_amount=50_000_000, days_back=14)
        if contracts:
            events["large_contracts"] = contracts

        # Proposed regulations (P5)
        rules = self.poll_proposed_rules(days_back=14)
        if rules:
            events["proposed_rules"] = rules

        # Court dockets (P10)
        dockets = self.poll_court_dockets(days_back=14)
        if dockets:
            events["court_dockets"] = dockets

        logger.info(
            "Event poll complete: %s",
            {k: len(v) for k, v in events.items()},
        )
        return events
