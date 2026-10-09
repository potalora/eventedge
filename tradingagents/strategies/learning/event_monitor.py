"""Event monitor for paper-trade strategies.

Polls data sources (EDGAR, etc.) for new events relevant to
paper-trade strategies. Each poll returns a list of events that
strategies can analyze for trading signals.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from tradingagents.strategies.orchestration.trading_calendar import exchange_date
from typing import Any

from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError, source_fetch_error
from tradingagents.strategies.data_sources.evidence import CoverageRecords

logger = logging.getLogger(__name__)


class EventMonitor:
    """Polls data sources for actionable events."""

    def __init__(self, registry: Any) -> None:
        """
        Args:
            registry: DataSourceRegistry instance.
        """
        self.registry = registry
        self.as_of: str | None = None
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
        if source is None:
            return []
        if not source.is_available():
            raise SourceFetchError("Required source access unavailable", reason_code="provider_error")

        date_from = (datetime.fromisoformat(self.as_of or exchange_date().isoformat()) - timedelta(days=days_back)).strftime("%Y-%m-%d")
        date_to = self.as_of or exchange_date().isoformat()

        all_filings = []
        failures, statuses, scopes = {}, {}, {}
        for index, form_type in enumerate(form_types):
            operation = f"form_{index}"
            try:
                filings = source.search_filings(form_type=form_type, date_from=date_from, date_to=date_to)
                all_filings.extend(filings)
                if hasattr(filings, "coverage"):
                    scopes[form_type] = filings.coverage
            except SourceFetchError as exc:
                all_filings.extend(exc.partial_data.get("filings", []))
                failures[operation] = exc.reason_code
                if exc.http_status is not None:
                    statuses[operation] = exc.http_status

        # Count attempts against the budget and isolate each filing failure.
        text_forms = {"10-K", "10-Q", "DEF 14A", "8-K", "SC 13D", "SC 13G"}
        attempted = 0
        if fetch_text:
            for index, filing in enumerate(all_filings):
                form = filing.get("form_type", "")
                url = filing.get("primary_document_url") or filing.get("file_url", "")
                if form not in text_forms:
                    continue
                if not url or attempted >= max_text_fetches:
                    filing["text_status"] = "missing_document_url" if not url else "text_budget_exhausted"
                    continue
                attempted += 1
                try:
                    if hasattr(source, "get_primary_document_url"):
                        url = source.get_primary_document_url(url, form_type=form)
                        filing["primary_document_url"] = url
                    raw_text = source.get_filing_text(url)
                    clean_text = self._strip_html(raw_text) if raw_text else ""
                    filing["text_status"] = "available" if clean_text else "unavailable"
                    if form == "DEF 14A":
                        filing["proxy_text"] = clean_text[:5000]
                    else:
                        filing["current_text"] = clean_text[:5000]
                        if form in {"10-K", "10-Q"} and clean_text:
                            filing["prior_text"] = self._fetch_prior_filing_text(source, filing, form)
                except Exception as exc:
                    error = source_fetch_error("EDGAR filing text unavailable", exc)
                    filing["text_status"] = "unavailable"
                    failures[f"filing_text_{index}"] = error.reason_code
                    if error.http_status is not None:
                        statuses[f"filing_text_{index}"] = error.http_status
        all_filings = CoverageRecords(all_filings, coverage={"mode": "exhaustive_windows", "complete": not failures, "windows": scopes})
        if failures:
            raise SourceFetchError("EDGAR filing coverage incomplete", reason_code="batch_failure",
                                   failed_operations=failures, failed_http_statuses=statuses,
                                   partial_data={"filings": all_filings})

        self._last_poll["edgar"] = datetime.now(timezone.utc).isoformat()
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
        except SourceFetchError:
            raise
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
        if source is None:
            return []
        if not source.is_available():
            raise SourceFetchError("Required source access unavailable", reason_code="provider_error")
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
        if source is None:
            return []
        if not source.is_available():
            raise SourceFetchError("Required source access unavailable", reason_code="provider_error")

        date_from = (datetime.fromisoformat(self.as_of or exchange_date().isoformat()) - timedelta(days=days_back)).strftime("%Y-%m-%d")
        date_to = self.as_of or exchange_date().isoformat()

        seen_urls: set[str] = set()
        all_filings: list[dict] = []

        failures, statuses, scopes = {}, {}, {}
        for form_index, form_type in enumerate(form_types):
            for keyword_index, keyword in enumerate(keywords):
                operation = f"keyword_{form_index}_{keyword_index}"
                try:
                    filings = source.search_filings(form_type=form_type, date_from=date_from,
                                                   date_to=date_to, keyword=keyword)
                except SourceFetchError as exc:
                    filings = exc.partial_data.get("filings", [])
                    failures[operation] = exc.reason_code
                    if exc.http_status is not None:
                        statuses[operation] = exc.http_status
                if hasattr(filings, "coverage"):
                    scopes[operation] = filings.coverage
                for f in filings:
                    url = f.get("file_url", "")
                    if url and url not in seen_urls:
                        seen_urls.add(url)
                        f["matched_keyword"] = keyword
                        all_filings.append(f)

        attempted = 0
        if fetch_text:
            for index, filing in enumerate(all_filings):
                if attempted >= max_text_fetches:
                    filing["text_status"] = "text_budget_exhausted"
                    continue
                url = filing.get("primary_document_url") or filing.get("file_url", "")
                if not url:
                    filing["text_status"] = "missing_document_url"
                    continue
                attempted += 1
                try:
                    if hasattr(source, "get_primary_document_url"):
                        url = source.get_primary_document_url(url, form_type=filing.get("form_type"))
                        filing["primary_document_url"] = url
                    raw_text = source.get_filing_text(url)
                    filing["filing_text"] = self._strip_html(raw_text)[:5000] if raw_text else ""
                    filing["text_status"] = "available" if filing["filing_text"] else "unavailable"
                except Exception as exc:
                    error = source_fetch_error("EDGAR keyword text unavailable", exc)
                    filing["text_status"] = "unavailable"
                    failures[f"filing_text_{index}"] = error.reason_code
        all_filings = CoverageRecords(all_filings, coverage={"mode": "exhaustive_windows", "complete": not failures, "windows": scopes})
        if failures:
            raise SourceFetchError("EDGAR keyword coverage incomplete", reason_code="batch_failure",
                                   failed_operations=failures, failed_http_statuses=statuses,
                                   partial_data={"pqc_filings": all_filings})

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
        if source is None:
            return {}
        if not source.is_available():
            raise SourceFetchError("Required source access unavailable", reason_code="provider_error")

        from tradingagents.strategies.data_sources.evidence import CoverageMapping

        issuer_coverage = {}
        results = CoverageMapping(coverage={
            "mode": "bounded_sample", "complete": False,
            "requested_tickers": list(tickers), "issuers": issuer_coverage,
        })
        failures, statuses = {}, {}
        for ticker in tickers:
            try:
                if self.as_of:
                    filings = source.get_recent_form4(ticker, days_back=days_back, as_of=self.as_of)
                else:
                    filings = source.get_recent_form4(ticker, days_back=days_back)
                issuer_coverage[ticker] = dict(getattr(filings, "coverage", {
                    "mode": "unavailable", "complete": False,
                }))
                if filings:
                    results[ticker] = filings
            except SourceFetchError as exc:
                partial = exc.partial_data.get("form4_filings")
                if partial:
                    results[ticker] = partial
                issuer_coverage[ticker] = dict(getattr(partial, "coverage", {
                    "mode": "unavailable", "complete": False, "reason": exc.reason_code,
                }))
                failures[ticker] = exc.reason_code
                if exc.http_status is not None:
                    statuses[ticker] = exc.http_status
        if failures:
            raise SourceFetchError("EDGAR Form 4 coverage incomplete", reason_code="batch_failure",
                                   failed_operations=failures, failed_http_statuses=statuses,
                                   partial_data={"form4": results})
        return results

    def poll_large_contracts(
        self, min_amount: float = 50_000_000, days_back: int = 14
    ) -> list[dict]:
        """Poll USAspending for recent large contract awards."""
        source = self.registry.get("usaspending")
        if source is None:
            return []
        if not source.is_available():
            raise SourceFetchError("Required source access unavailable", reason_code="provider_error")
        return source.get_recent_large_contracts(
            min_amount=min_amount, days_back=days_back
        )

    def poll_congressional_trades(self, days_back: int = 30) -> list[dict]:
        """Poll for recent congressional stock trades."""
        source = self.registry.get("congress")
        if source is None:
            return []
        if not source.is_available():
            raise SourceFetchError("Required source access unavailable", reason_code="provider_error")
        return source.get_recent_trades(days_back=days_back)

    def poll_proposed_rules(
        self, agencies: list[str] | None = None, days_back: int = 14,
    ) -> list[dict]:
        """Poll regulations.gov for recently proposed rules."""
        source = self.registry.get("regulations")
        if source is None:
            return []
        if not source.is_available():
            raise SourceFetchError("Required source access unavailable", reason_code="provider_error")

        options = {"agencies": agencies, "days_back": days_back}
        if self.as_of:
            options["as_of"] = self.as_of
        rules = source.get_recent_proposed_rules(**options)
        self._last_poll["regulations"] = datetime.now(timezone.utc).isoformat()
        logger.info("Regulations.gov poll: %d proposed rules", len(rules))
        return rules

    def poll_court_dockets(
        self, query: str = "securities", days_back: int = 14,
    ) -> list[dict]:
        """Poll CourtListener for recent court dockets."""
        source = self.registry.get("courtlistener")
        if source is None:
            return []
        if not source.is_available():
            raise SourceFetchError("Required source access unavailable", reason_code="provider_error")

        date_from = (datetime.fromisoformat(self.as_of or exchange_date().isoformat()) - timedelta(days=days_back)).strftime("%Y-%m-%d")
        dockets = source.search_dockets(
            query=query, date_filed_after=date_from,
            date_filed_before=self.as_of or exchange_date().isoformat(),
        )
        self._last_poll["courtlistener"] = datetime.now(timezone.utc).isoformat()
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
