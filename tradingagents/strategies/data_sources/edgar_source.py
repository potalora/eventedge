from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

# SEC rate limit: 10 requests/sec
_SEC_DELAY = 0.1

import re

EFTS_BASE = "https://efts.sec.gov/LATEST/search-index"
SUBMISSIONS_BASE = "https://data.sec.gov/submissions"
EDGAR_SEARCH = "https://efts.sec.gov/LATEST/search-index"
COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"


_TICKER_RE = re.compile(r"\(([A-Z]{1,5})\)")


def _extract_ticker(display_name: str) -> str:
    """Extract ticker symbol from EDGAR display_name format.

    E.g. 'Victoria\\'s Secret & Co.  (VSCO)  (CIK 0001856437)' -> 'VSCO'
    """
    matches = _TICKER_RE.findall(display_name)
    # First match that isn't "CIK" is the ticker
    for m in matches:
        if m != "CIK":
            return m
    return ""


class EDGARSource:
    """Data source for SEC EDGAR filings.

    Uses ``requests`` directly (no edgartools dependency).
    Respects SEC rate limits (10 req/sec) via ``time.sleep(0.1)`` between
    requests.
    """

    name: str = "edgar"
    requires_api_key: bool = False

    # Suffixes to strip when normalizing company names for matching
    _NAME_SUFFIXES = re.compile(
        r"\b(inc\.?|corp\.?|llc\.?|ltd\.?|co\.?|company|plc\.?|n\.?v\.?|s\.?a\.?|group|holdings?|enterprises?|international|technologies|technology)\b",
        re.IGNORECASE,
    )

    def __init__(self, user_agent: str = "TradingAgents research@example.com") -> None:
        self._user_agent = user_agent
        self._cik_cache: dict[str, str] = {}
        self._session_cache: dict[str, Any] = {}
        self._name_to_ticker_cache: dict[str, str] | None = None

    # ------------------------------------------------------------------
    # Protocol methods
    # ------------------------------------------------------------------

    def fetch(self, params: dict[str, Any]) -> dict[str, Any]:
        """Generic dispatcher.

        Supported params["method"] values:
            search_filings, company_filings, filing_text,
            recent_form4, recent_13d, ticker_to_cik
        """
        method = params.get("method", "search_filings")
        dispatch = {
            "search_filings": self._dispatch_search_filings,
            "company_filings": self._dispatch_company_filings,
            "filing_text": self._dispatch_filing_text,
            "recent_form4": self._dispatch_recent_form4,
            "recent_13d": self._dispatch_recent_13d,
            "ticker_to_cik": self._dispatch_ticker_to_cik,
        }
        handler = dispatch.get(method)
        if handler is None:
            return {"error": f"Unknown method '{method}'"}
        try:
            return handler(params)
        except Exception:
            logger.error("EDGARSource.fetch(%s) failed", method, exc_info=True)
            return {"error": f"{method} fetch failed"}

    def is_available(self) -> bool:
        """EDGAR is available if requests is installed."""
        try:
            import requests  # noqa: F401
            return True
        except ImportError:
            return False

    # ------------------------------------------------------------------
    # Public data methods
    # ------------------------------------------------------------------

    def search_filings(
        self,
        form_type: str,
        date_from: str | None = None,
        date_to: str | None = None,
        ticker: str | None = None,
    ) -> list[dict[str, Any]]:
        """Search EDGAR full-text search for filings.

        Args:
            form_type: SEC form type (e.g. "10-K", "4", "SC 13D").
            date_from: Start date (YYYY-MM-DD).
            date_to: End date (YYYY-MM-DD).
            ticker: Optional ticker to filter by.

        Returns:
            List of filing metadata dicts with keys:
            file_date, form_type, entity_name, file_url, description.
        """
        import requests

        query_parts = [f'form:"{form_type}"']
        if ticker:
            query_parts.append(f'ticker:"{ticker}"')

        params: dict[str, Any] = {
            "q": " AND ".join(query_parts),
        }
        if date_from:
            params["startdt"] = date_from
        if date_to:
            params["enddt"] = date_to

        time.sleep(_SEC_DELAY)
        resp = requests.get(
            EDGAR_SEARCH,
            params=params,
            headers={"User-Agent": self._user_agent},
            timeout=15,
        )
        if resp.status_code != 200:
            logger.warning("EDGAR search returned %d", resp.status_code)
            return []

        data = resp.json()
        hits = data.get("hits", {}).get("hits", [])
        results: list[dict[str, Any]] = []
        for hit in hits:
            src = hit.get("_source", {})
            display_names = src.get("display_names", [])
            entity_name = display_names[0] if display_names else ""
            # Extract ticker from display_name format: "Company Name  (TICK)  (CIK ...)"
            ticker_str = _extract_ticker(entity_name)
            # Build filing URL from accession number
            adsh = src.get("adsh", "")
            ciks = src.get("ciks", [])
            file_url = ""
            if adsh and ciks:
                cik = ciks[0].lstrip("0")
                adsh_nod = adsh.replace("-", "")
                file_url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{adsh_nod}/{adsh}-index.htm"
            results.append({
                "file_date": src.get("file_date", ""),
                "form_type": src.get("form", ""),
                "entity_name": entity_name,
                "ticker": ticker_str,
                "file_url": file_url,
                "description": entity_name,
                "ciks": ciks,
                "adsh": adsh,
            })
        return results

    def get_company_filings(
        self,
        cik: str,
        form_types: list[str] | None = None,
        count: int = 10,
    ) -> list[dict[str, Any]]:
        """Get filing metadata for a company by CIK.

        Args:
            cik: SEC CIK number (zero-padded to 10 digits).
            form_types: Optional list of form types to filter.
            count: Max number of filings to return.

        Returns:
            List of dicts with keys:
            accession_number, form, filing_date, primary_document.
        """
        import requests

        padded_cik = cik.zfill(10)
        url = f"{SUBMISSIONS_BASE}/CIK{padded_cik}.json"

        time.sleep(_SEC_DELAY)
        resp = requests.get(
            url,
            headers={"User-Agent": self._user_agent},
            timeout=15,
        )
        if resp.status_code != 200:
            logger.warning("Company filings returned %d for CIK %s", resp.status_code, cik)
            return []

        data = resp.json()
        recent = data.get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        dates = recent.get("filingDate", [])
        accessions = recent.get("accessionNumber", [])
        documents = recent.get("primaryDocument", [])

        results: list[dict[str, Any]] = []
        for i in range(min(len(forms), len(dates))):
            if form_types and forms[i] not in form_types:
                continue
            results.append({
                "accession_number": accessions[i] if i < len(accessions) else "",
                "form": forms[i],
                "filing_date": dates[i],
                "primary_document": documents[i] if i < len(documents) else "",
            })
            if len(results) >= count:
                break
        return results

    def get_filing_text(self, url: str) -> str:
        """Download the text content of a filing document.

        Args:
            url: Full URL to the filing document on EDGAR.

        Returns:
            Filing text content (may be HTML), or empty string on failure.
        """
        import requests

        time.sleep(_SEC_DELAY)
        try:
            resp = requests.get(
                url,
                headers={"User-Agent": self._user_agent},
                timeout=30,
            )
            if resp.status_code == 200:
                return resp.text
            logger.warning("Filing text returned %d for %s", resp.status_code, url)
            return ""
        except Exception:
            logger.error("get_filing_text failed for %s", url, exc_info=True)
            return ""

    def get_recent_form4(
        self, ticker: str, days_back: int = 30
    ) -> list[dict[str, Any]]:
        """Get recent Form 4 (insider transaction) filings for a ticker.

        Uses the submissions API, then parses each Form 4 XML to extract
        transaction details (buy/sell, shares, price, insider name/title).

        Args:
            ticker: Stock ticker symbol.
            days_back: How many days back to search.

        Returns:
            List of filing dicts enriched with transaction details.
        """
        from datetime import datetime, timedelta

        cik = self.ticker_to_cik(ticker)
        if not cik:
            return []

        filings = self.get_company_filings(cik, form_types=["4", "4/A"], count=40)

        cutoff = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
        recent = [f for f in filings if f.get("filing_date", "") >= cutoff]

        # Enrich with parsed transaction details
        enriched: list[dict[str, Any]] = []
        for filing in recent:
            transactions = self._parse_form4_xml(cik, filing)
            if transactions:
                for txn in transactions:
                    enriched.append({**filing, **txn})
            else:
                enriched.append(filing)

        return enriched

    def _parse_form4_xml(
        self, cik: str, filing: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Parse a Form 4 XML to extract transaction details.

        Returns list of dicts: transaction_type ("buy"|"sell"|"other"),
        transaction_code, shares, price_per_share, owner_name, owner_title,
        is_officer, is_director.
        """
        import requests
        from xml.etree import ElementTree

        accession = filing.get("accession_number", "")
        primary_doc = filing.get("primary_document", "")
        if not accession or not primary_doc:
            return []

        # Strip XSL prefix (e.g. "xslF345X06/file.xml" → "file.xml")
        # SEC serves transformed HTML at the XSL path; raw XML is at the base.
        if "/" in primary_doc:
            primary_doc = primary_doc.rsplit("/", 1)[-1]

        padded_cik = cik.zfill(10)
        accession_nodash = accession.replace("-", "")
        url = f"https://www.sec.gov/Archives/edgar/data/{padded_cik}/{accession_nodash}/{primary_doc}"

        time.sleep(_SEC_DELAY)
        try:
            resp = requests.get(
                url, headers={"User-Agent": self._user_agent}, timeout=15,
            )
            if resp.status_code != 200:
                return []
        except Exception:
            logger.debug("Form 4 XML fetch failed: %s", url)
            return []

        try:
            root = ElementTree.fromstring(resp.text)
        except ElementTree.ParseError:
            return []

        # Handle XML namespaces
        ns = ""
        if root.tag.startswith("{"):
            ns = root.tag.split("}")[0] + "}"

        # Extract reporting owner info
        owner_name = ""
        owner_title = ""
        is_officer = False
        is_director = False

        owner_el = root.find(f".//{ns}reportingOwner")
        if owner_el is not None:
            name_el = owner_el.find(f".//{ns}rptOwnerName")
            if name_el is not None and name_el.text:
                owner_name = name_el.text.strip()

            rel_el = owner_el.find(f".//{ns}reportingOwnerRelationship")
            if rel_el is not None:
                officer_el = rel_el.find(f"{ns}isOfficer")
                is_officer = officer_el is not None and (officer_el.text or "").strip() in ("1", "true")
                director_el = rel_el.find(f"{ns}isDirector")
                is_director = director_el is not None and (director_el.text or "").strip() in ("1", "true")
                title_el = rel_el.find(f"{ns}officerTitle")
                if title_el is not None and title_el.text:
                    owner_title = title_el.text.strip()

        # Extract transactions
        transactions: list[dict[str, Any]] = []
        for txn_tag in (f"{ns}nonDerivativeTransaction", f"{ns}derivativeTransaction"):
            for txn_el in root.findall(f".//{txn_tag}"):
                coding_el = txn_el.find(f".//{ns}transactionCoding")
                tx_code = ""
                if coding_el is not None:
                    code_el = coding_el.find(f"{ns}transactionCode")
                    if code_el is not None and code_el.text:
                        tx_code = code_el.text.strip()

                # P = open market purchase (strongest buy signal)
                # A/J/M/I = other acquisition types
                # S/F/D = sale/disposition types
                if tx_code in ("P", "A", "J", "M", "I"):
                    transaction_type = "buy"
                elif tx_code in ("S", "F", "D"):
                    transaction_type = "sell"
                else:
                    transaction_type = "other"

                shares = 0.0
                price = 0.0
                amounts_el = txn_el.find(f".//{ns}transactionAmounts")
                if amounts_el is not None:
                    shares_el = amounts_el.find(f".//{ns}transactionShares/{ns}value")
                    if shares_el is not None and shares_el.text:
                        try:
                            shares = float(shares_el.text)
                        except ValueError:
                            pass
                    price_el = amounts_el.find(f".//{ns}transactionPricePerShare/{ns}value")
                    if price_el is not None and price_el.text:
                        try:
                            price = float(price_el.text)
                        except ValueError:
                            pass

                    # Fallback: A/D code
                    ad_el = amounts_el.find(f".//{ns}transactionAcquiredDisposedCode/{ns}value")
                    if ad_el is not None and ad_el.text and transaction_type == "other":
                        if ad_el.text.strip() == "A":
                            transaction_type = "buy"
                        elif ad_el.text.strip() == "D":
                            transaction_type = "sell"

                transactions.append({
                    "transaction_type": transaction_type,
                    "transaction_code": tx_code,
                    "shares": shares,
                    "price_per_share": price,
                    "owner_name": owner_name,
                    "owner_title": owner_title,
                    "is_officer": is_officer,
                    "is_director": is_director,
                })

        return transactions

    def get_recent_13d(self, days_back: int = 60) -> list[dict[str, Any]]:
        """Get recent SC 13D (activist) filings.

        Args:
            days_back: How many days back to search.

        Returns:
            List of filing metadata dicts.
        """
        from datetime import datetime, timedelta

        date_from = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
        date_to = datetime.now().strftime("%Y-%m-%d")
        return self.search_filings("SC 13D", date_from=date_from, date_to=date_to)

    def _normalize_name(self, name: str) -> str:
        """Normalize a company name for matching."""
        name = self._NAME_SUFFIXES.sub("", name.lower())
        # Collapse whitespace and strip
        return " ".join(name.split()).strip(" .,")

    def _ensure_name_map(self) -> dict[str, str]:
        """Build and cache normalized-company-name → ticker mapping."""
        if self._name_to_ticker_cache is not None:
            return self._name_to_ticker_cache

        # Ensure company_tickers.json is downloaded
        self._ensure_company_tickers()
        tickers_data = self._session_cache.get("_company_tickers", {})

        mapping: dict[str, str] = {}
        for entry in tickers_data.values():
            title = entry.get("title", "")
            ticker = entry.get("ticker", "")
            if title and ticker:
                mapping[self._normalize_name(title)] = ticker.upper()
        self._name_to_ticker_cache = mapping
        return mapping

    def _ensure_company_tickers(self) -> None:
        """Download company_tickers.json if not already cached."""
        cache_key = "_company_tickers"
        if cache_key in self._session_cache:
            return

        import requests

        time.sleep(_SEC_DELAY)
        try:
            resp = requests.get(
                COMPANY_TICKERS_URL,
                headers={"User-Agent": self._user_agent},
                timeout=15,
            )
            if resp.status_code == 200:
                self._session_cache[cache_key] = resp.json()
            else:
                logger.warning("Company tickers returned %d", resp.status_code)
        except Exception:
            logger.error("company_tickers download failed", exc_info=True)

    def name_to_ticker(self, company_name: str) -> str | None:
        """Resolve a company name to ticker using SEC's company_tickers.json.

        Tries exact match first, then prefix match.
        Returns None if no match found.
        """
        mapping = self._ensure_name_map()
        normalized = self._normalize_name(company_name)

        if not normalized:
            return None

        # Exact match
        if normalized in mapping:
            return mapping[normalized]

        # Prefix match: input is prefix of a known company name
        for name, ticker in mapping.items():
            if name.startswith(normalized):
                return ticker

        return None

    def validate_ticker(self, ticker: str) -> bool:
        """Check if a ticker exists in SEC's company_tickers.json."""
        self._ensure_company_tickers()
        tickers_data = self._session_cache.get("_company_tickers", {})
        upper = ticker.upper()
        return any(
            entry.get("ticker", "").upper() == upper
            for entry in tickers_data.values()
        )

    def ticker_to_cik(self, ticker: str) -> str | None:
        """Resolve a ticker symbol to its SEC CIK number.

        Args:
            ticker: Stock ticker symbol.

        Returns:
            CIK as a string, or None if not found.
        """
        if ticker.upper() in self._cik_cache:
            return self._cik_cache[ticker.upper()]

        self._ensure_company_tickers()
        tickers_data = self._session_cache.get("_company_tickers")
        if not tickers_data:
            return None

        for entry in tickers_data.values():
            if entry.get("ticker", "").upper() == ticker.upper():
                cik = str(entry["cik_str"])
                self._cik_cache[ticker.upper()] = cik
                return cik
        return None

    # ------------------------------------------------------------------
    # Cache management
    # ------------------------------------------------------------------

    def clear_cache(self) -> None:
        """Clear all cached data."""
        self._cik_cache.clear()
        self._session_cache.clear()
        self._name_to_ticker_cache = None

    # ------------------------------------------------------------------
    # Internal dispatch helpers
    # ------------------------------------------------------------------

    def _dispatch_search_filings(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"data": self.search_filings(
            form_type=params.get("form_type", "10-K"),
            date_from=params.get("date_from"),
            date_to=params.get("date_to"),
            ticker=params.get("ticker"),
        )}

    def _dispatch_company_filings(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"data": self.get_company_filings(
            cik=params.get("cik", ""),
            form_types=params.get("form_types"),
            count=params.get("count", 10),
        )}

    def _dispatch_filing_text(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"data": self.get_filing_text(url=params.get("url", ""))}

    def _dispatch_recent_form4(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"data": self.get_recent_form4(
            ticker=params.get("ticker", ""),
            days_back=params.get("days_back", 30),
        )}

    def _dispatch_recent_13d(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"data": self.get_recent_13d(days_back=params.get("days_back", 60))}

    def _dispatch_ticker_to_cik(self, params: dict[str, Any]) -> dict[str, Any]:
        cik = self.ticker_to_cik(params.get("ticker", ""))
        return {"data": cik}
