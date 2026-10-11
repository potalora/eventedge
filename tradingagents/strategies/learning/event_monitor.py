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
from tradingagents.strategies.data_sources.edgar_source import filing_form_family, normalize_filing_form

logger = logging.getLogger(__name__)


class EventMonitor:
    """Polls data sources for actionable events."""

    def __init__(self, registry: Any, *, filing_policy: str | None = None,
                 comparator_policy: str | None = None,
                 parser_policy: str | None = None,
                 attribution_policy: str | None = None,
                 acquisition_policy: str | None = None, spool_root=None,
                 material_policy: str | None = None) -> None:
        """
        Args:
            registry: DataSourceRegistry instance.
        """
        if filing_policy not in (None, 'complete_submission_v1'):
            raise ValueError('Unknown filing acquisition policy')
        if comparator_policy is not None:
            from tradingagents.strategies.data_sources.filing_comparison_policy import CURRENT_ONLY_POLICY
            if comparator_policy != CURRENT_ONLY_POLICY or filing_policy != 'complete_submission_v1':
                raise ValueError('Invalid filing comparator policy')
        if parser_policy is not None:
            from tradingagents.strategies.data_sources.filing_parser_dispatch import POLICY
            if parser_policy != POLICY or filing_policy != 'complete_submission_v1':
                raise ValueError('Invalid filing parser policy')
        if attribution_policy is not None:
            from tradingagents.strategies.data_sources.filing_attribution_policy import POLICY
            if attribution_policy != POLICY or filing_policy != 'complete_submission_v1':
                raise ValueError('Invalid filing attribution policy')
        from pathlib import Path
        from tradingagents.strategies.orchestration.filing_acquisition_validation import configured
        acquisition_enabled = configured({'filing_acquisition_policy': acquisition_policy,
            'filing_evidence_policy': filing_policy, 'filing_parser_policy': parser_policy})
        if acquisition_enabled and (spool_root is None or not Path(spool_root).is_absolute()):
            raise ValueError('An absolute private filing spool directory is required')
        from tradingagents.strategies.data_sources.filing_material_policy import configured as material_configured
        material_configured({'filing_material_policy': material_policy,
            'filing_evidence_policy': filing_policy, 'filing_acquisition_policy': acquisition_policy,
            'filing_parser_policy': parser_policy})
        self.material_policy = material_policy
        self.acquisition_policy = acquisition_policy
        self.spool_root = Path(spool_root) if acquisition_enabled else None
        self.registry = registry
        self.filing_policy = filing_policy
        self.comparator_policy = comparator_policy
        self.parser_policy = parser_policy
        self.attribution_policy = attribution_policy
        self.as_of: str | None = None
        self.equity_universe = None
        self._last_poll: dict[str, str] = {}  # source -> last poll timestamp

    def hydrate_collections(self, collections: dict, *, company_map=None, max_workers=16) -> dict:
        """Opt-in full acquisition, once after all fetch_text=False discoveries.

        Individual discovery methods retain their legacy defaults. The complete
        policy caller gathers all declared scopes before invoking this wrapper,
        so cross-scope bodies and prior comparisons share one frozen corpus.
        """
        if self.filing_policy != 'complete_submission_v1':
            raise ValueError('Full filing acquisition policy is not enabled')
        source = self.registry.get('edgar')
        if source is None or not source.is_available():
            raise SourceFetchError('Required source access unavailable', reason_code='provider_error')
        from tradingagents.strategies.data_sources.filing_hydration import hydrate_filings
        from contextlib import nullcontext
        from tradingagents.strategies.data_sources.filing_parser_dispatch import parser_scope
        from tradingagents.strategies.data_sources.filing_spool import submission_spool_scope
        from tradingagents.strategies.data_sources.request_policy import current_provider_deadline
        from tradingagents.strategies.orchestration.filing_acquisition_validation import acquisition_scope
        result, owner = None, None
        spool = (submission_spool_scope(self.spool_root, original_deadline=current_provider_deadline('edgar'))
                 if self.acquisition_policy is not None else nullcontext())
        try:
            with spool as owner:
                with parser_scope() if self.parser_policy is not None else nullcontext():
                    result = hydrate_filings(source, collections, equity_universe=self.equity_universe,
                                             company_map=company_map, max_workers=max_workers,
                                             comparator_policy=self.comparator_policy,
                                             attribution_policy=self.attribution_policy,
                                             material_policy=self.material_policy)
        except SourceFetchError as error:
            # Preserve completed evidence when deadline-bound workers have not
            # closed yet. The graph remains failed and cannot claim cleanup.
            if result is None or owner is None:
                raise
            result['coverage']['complete'] = False
            result['coverage']['scope_failure'] = {'code': 'filing_scope_closure_failure',
                                                   'reason_code': error.reason_code}
        if owner is not None:
            result['coverage'].update(acquisition_policy=self.acquisition_policy,
                                      parser_policy=self.parser_policy)
            result['acquisition_scope'] = acquisition_scope(owner)
        if self.material_policy:
            from tradingagents.strategies.orchestration.filing_material_validation import build_material_scope
            result['material_scope'] = build_material_scope(result, result['collections'])
            for key in ('scoped_complete', 'scoped_failed_rows', 'quarantined_rows'):
                result['coverage'][key] = result['material_scope'][key]
        return result

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
                if "coverage" in exc.partial_data:
                    scopes[form_type] = exc.partial_data["coverage"]
                failures[operation] = exc.reason_code
                if exc.http_status is not None:
                    statuses[operation] = exc.http_status

        deduplicated, seen = [], set()
        for filing in all_filings:
            identity = filing.get("adsh") or filing.get("accession_number") or filing.get("file_url")
            if identity and identity in seen:
                continue
            if identity:
                seen.add(identity)
            deduplicated.append(filing)
        all_filings = deduplicated

        # Count attempts against the budget and isolate each filing failure.
        text_forms = {"10-K", "10-Q", "DEF 14A", "8-K", "SCHEDULE 13D", "SCHEDULE 13G"}
        attempted = 0
        if fetch_text:
            for index, filing in enumerate(all_filings):
                form = filing.get("form_type", "")
                url = filing.get("primary_document_url") or filing.get("file_url", "")
                if filing_form_family(form) not in text_forms:
                    continue
                if self.equity_universe is not None:
                    membership = self.equity_universe.filing_decision(filing.get("ciks", []))
                    filing["universe_membership"] = membership
                    if membership["status"] == "excluded":
                        filing["text_status"] = "outside_declared_equity_universe"
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
        """Extract visible filing text before applying any excerpt length limit.

        Inline-XBRL headers contain large machine-only context blocks. Keeping
        their text can exhaust an excerpt before the actual filing begins.
        This cleanup does not certify substantive section or entity coverage.
        """
        import re
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(text, "html.parser")
        tags = soup.find_all()
        inline_prefixes, instance_prefixes = {"ix"}, {"xbrli"}
        for tag in tags:
            for name, value in tag.attrs.items():
                if not name.startswith("xmlns:") or not isinstance(value, str):
                    continue
                prefix = name.split(":", 1)[1]
                if value in {"http://www.xbrl.org/2008/inlineXBRL", "http://www.xbrl.org/2013/inlineXBRL"}:
                    inline_prefixes.add(prefix)
                elif value == "http://www.xbrl.org/2003/instance":
                    instance_prefixes.add(prefix)
        invisible = {"head", "script", "style", "template", "noscript"}
        invisible.update(f"{prefix}:{name}" for prefix in inline_prefixes
                         for name in ("header", "hidden", "references", "resources"))
        invisible.update(f"{prefix}:{name}" for prefix in instance_prefixes
                         for name in ("context", "unit"))
        hidden_style = re.compile(r"(?:^|;)\s*(?:display\s*:\s*none|visibility\s*:\s*hidden)\s*(?:!important\s*)?(?:;|$)", re.I)
        # Descendants first: decomposing a parent invalidates its child objects.
        for tag in reversed(tags):
            if (tag.name in invisible or tag.has_attr("hidden")
                    or hidden_style.search(str(tag.get("style", "")))):
                tag.decompose()
        return re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()

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
        complete_policy = self.filing_policy == 'complete_submission_v1'
        by_accession = {}
        all_filings: list[dict] = []

        failures, statuses, scopes = {}, {}, {}
        for form_index, form_type in enumerate(form_types):
            for keyword_index, keyword in enumerate(keywords):
                operation = f"keyword_{form_index}_{keyword_index}"
                query = {'form_type': form_type, 'keyword': keyword,
                         'date_from': date_from, 'date_to': date_to, 'operation': operation}
                try:
                    filings = source.search_filings(form_type=form_type, date_from=date_from,
                                                   date_to=date_to, keyword=keyword)
                except SourceFetchError as exc:
                    filings = exc.partial_data.get("filings", [])
                    if "coverage" in exc.partial_data:
                        scopes[operation] = exc.partial_data["coverage"]
                    failures[operation] = exc.reason_code
                    if exc.http_status is not None:
                        statuses[operation] = exc.http_status
                if hasattr(filings, "coverage"):
                    scopes[operation] = filings.coverage
                if complete_policy:
                    scopes[operation] = {**scopes.get(operation, {'complete': operation not in failures}), **query}
                for f in filings:
                    if complete_policy:
                        accession = f.get('adsh') or f.get('accession_number')
                        # Missing identities must survive discovery and fail
                        # explicitly at hydration, rather than disappear here.
                        identity = accession or f'missing_{operation}_{len(all_filings)}'
                        if identity not in by_accession:
                            retained = dict(f, matched_keyword=keyword, matched_queries=[])
                            by_accession[identity] = retained
                            all_filings.append(retained)
                        retained = by_accession[identity]
                        if query not in retained['matched_queries']:
                            retained['matched_queries'].append(dict(query))
                        if (normalize_filing_form(retained.get('form_type', '')), retained.get('file_date')) != (
                                normalize_filing_form(f.get('form_type', '')), f.get('file_date')):
                            retained['discovery_identity_conflict'] = True
                            retained.setdefault('discovery_conflicts', []).append({
                                'form_type': f.get('form_type'), 'file_date': f.get('file_date'),
                                'operation': operation})
                            failures[operation] = 'invalid_response'
                            scopes[operation]['complete'] = False
                        continue
                    url = f.get("file_url", "")
                    if url and url not in seen_urls:
                        seen_urls.add(url)
                        f["matched_keyword"] = keyword
                        all_filings.append(f)

        attempted = 0
        if fetch_text and not complete_policy:
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
            "mode": "exhaustive_window", "complete": False,
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
                if issuer_coverage[ticker].get("complete") is not True:
                    failures[ticker] = "invalid_response"
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
        results.coverage["complete"] = not failures and set(issuer_coverage) == set(tickers)
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
            form_types=["SCHEDULE 13D", "4", "10-K", "10-Q", "DEF 14A"],
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
