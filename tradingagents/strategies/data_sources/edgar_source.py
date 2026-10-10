from __future__ import annotations

import logging
import hashlib
import json
import time
from datetime import datetime, timezone
from typing import Any

from .evidence import current_session_date, CoverageRecords, collection_envelope
from .request_policy import (provider_request, provider_budget, current_provider_deadline,
                             provider_timeout, read_bounded_response)
from .fetch_errors import SourceFetchError, source_fetch_error, source_text, source_date, source_number

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


def normalize_filing_form(value: str) -> str:
    """Canonicalize ownership form spelling while preserving amendment identity."""
    form = " ".join(value.upper().split())
    if re.fullmatch(r"(?:SC|SCHEDULE) 13[DG](?:/A)?", form):
        return form.replace("SC ", "SCHEDULE ", 1)
    return form


def filing_form_family(value: str) -> str:
    form = normalize_filing_form(value)
    return form.removesuffix("/A") if form.startswith("SCHEDULE 13") else form


def filing_search_forms(value: str) -> str:
    """Ownership searches include current and historical labels and amendments."""
    family = filing_form_family(value)
    if family in {"SCHEDULE 13D", "SCHEDULE 13G"}:
        # EFTS base names already include amendments. Explicit /A filters
        # narrow a mixed query to amendments and would lose initial filings.
        return ",".join(f"{prefix} {family.split()[-1]}" for prefix in ("SCHEDULE", "SC"))
    return normalize_filing_form(value)


def _search_failure(message: str, branch: str, offset: int, records: list,
                    **diagnostic: Any) -> SourceFetchError:
    """Retain fixed branch labels and numeric/boolean diagnostics, never raw pages."""
    return SourceFetchError(message, reason_code="invalid_response", partial_data={
        "filings": records,
        "coverage": {"mode": "exhaustive_window", "complete": False,
                     "diagnostic": {"branch": branch, "offset": offset, **diagnostic}},
    })


def _valid_search_cik(value: Any) -> bool:
    try:
        return source_text(value) and value.isdigit() and int(value) > 0
    except ValueError:
        # Unicode digit categories and excessive integer strings need a safe
        # invalid-field outcome, preserving any earlier valid page records.
        return False


def _history_cik(cik):
    if not isinstance(cik, str) or not re.fullmatch(r'[0-9]{1,10}', cik) or int(cik) == 0:
        raise SourceFetchError('Invalid SEC history CIK', reason_code='invalid_response')
    return cik.zfill(10)


def _history_archive(cik, descriptor):
    if (not isinstance(descriptor, dict)
            or not isinstance(descriptor.get('name'), str)
            or not re.fullmatch(r'CIK' + cik + r'-submissions-[0-9]{3,6}\.json', descriptor['name'])
            or type(descriptor.get('filingCount')) is not int
            or not 0 < descriptor['filingCount'] <= 50_000
            or not source_date(descriptor.get('filingFrom'))
            or not source_date(descriptor.get('filingTo'))
            or len(descriptor['filingFrom']) != 10 or len(descriptor['filingTo']) != 10
            or descriptor['filingFrom'] > descriptor['filingTo']):
        raise SourceFetchError('Invalid SEC archive descriptor', reason_code='invalid_response')
    return {key: descriptor[key] for key in ('name', 'filingCount', 'filingFrom', 'filingTo')}


def _history_rows(data):
    keys = ('form', 'filingDate', 'accessionNumber', 'primaryDocument')
    if (not isinstance(data, dict) or any(not isinstance(data.get(key), list) for key in keys)
            or len(data['form']) > 50_000
            or any(not isinstance(value, list) or len(value) != len(data['form'])
                   for value in data.values())):
        raise ValueError('invalid history arrays')
    rows, seen = [], set()
    for form, filed, accession, document in zip(*(data[key] for key in keys)):
        if (not source_text(form) or len(form) > 32 or not source_date(filed) or len(filed) != 10
                or not isinstance(accession, str)
                or not re.fullmatch(r'[0-9]{10}-[0-9]{2}-[0-9]{6}', accession)
                or not isinstance(document, str)
                or len(document) > 512 or not 1 <= len(document.split('/')) <= 8
                or any(not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,254}', part)
                       for part in document.split('/'))
                or accession in seen):
            raise ValueError('invalid history row')
        seen.add(accession)
        rows.append({'accession_number': accession, 'form': normalize_filing_form(form),
                     'filing_date': filed, 'primary_document': document})
    return rows


class EDGARSource:
    """Data source for SEC EDGAR filings.

    Uses ``requests`` directly (no edgartools dependency).
    Uses the common acquisition budget and SEC request pacing.
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
        except SourceFetchError as exc:
            return {**exc.partial_data, "error": str(exc)}
        except Exception:
            logger.error("EDGARSource.fetch(%s) failed", method)
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

    def search_filings(self, form_type: str, date_from: str | None = None,
                       date_to: str | None = None, ticker: str | None = None,
                       keyword: str | None = None) -> list[dict[str, Any]]:
        """Exhaust the requested EFTS window under the shared provider budget.

        EFTS may cap a query at 10,000 hits; such windows fail explicitly and
        must be narrowed rather than silently claiming complete coverage.
        """
        if current_provider_deadline("edgar") is None:
            with provider_budget("edgar", time.monotonic() + 60):
                return self.search_filings(form_type, date_from, date_to, ticker, keyword)
        records, seen, offset, expected = [], set(), 0, None
        while True:
            try:
                page = self._search_filings_page(form_type, date_from, date_to, ticker, keyword, offset)
            except SourceFetchError as exc:
                exc.partial_data = {"filings": records + exc.partial_data.get("filings", []),
                                    "coverage": {**exc.partial_data.get("coverage", {}),
                                                 "mode": "exhaustive_window", "complete": False}}
                raise
            total = page.coverage["provider_total"]
            if expected is not None and total != expected:
                raise _search_failure("EDGAR search total changed", "total_changed", offset,
                                      records, page_count=len(page), expected_total=expected,
                                      provider_total=total)
            expected = total
            for row in page:
                identity = row['adsh']
                # Full-text hits can include multiple documents per filing.
                if identity not in seen:
                    seen.add(identity)
                    records.append(row)
            offset += len(page)
            if offset >= total:
                return CoverageRecords(records, coverage={"mode":"exhaustive_window", "complete":True,
                    "provider_total":total, "returned":len(records), "date_from":date_from, "date_to":date_to})
            if not page or offset >= 10000:
                raise _search_failure("EDGAR search window incomplete",
                                      "empty_page" if not page else "query_limit", offset - len(page),
                                      records, page_count=len(page), expected_total=expected,
                                      provider_total=total)

    def _search_filings_page(
        self,
        form_type: str,
        date_from: str | None = None,
        date_to: str | None = None,
        ticker: str | None = None,
        keyword: str | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Search EDGAR full-text search for filings.

        Args:
            form_type: SEC form type (e.g. "10-K", "4", "SC 13D").
            date_from: Start date (YYYY-MM-DD).
            date_to: End date (YYYY-MM-DD).
            ticker: Optional ticker to filter by.
            keyword: Optional keyword to search within filing text.

        Returns:
            List of filing metadata dicts with keys:
            file_date, form_type, entity_name, file_url, description.
        """
        import requests

        params: dict[str, Any] = {"forms": filing_search_forms(form_type), "q": keyword or "", "from": offset, "size": 100}
        if ticker:
            cik = self.ticker_to_cik(ticker)
            if not cik:
                return CoverageRecords([], coverage={"provider_total":0})
            params["ciks"] = cik.zfill(10)
        if date_from:
            params["startdt"] = date_from
        if date_to:
            params["enddt"] = date_to

        resp = provider_request("edgar", "GET",
            EDGAR_SEARCH,
            params=params,
            headers={"User-Agent": self._user_agent},
            timeout=15,
        )
        try:
            data = resp.json()
        except Exception:
            raise _search_failure("EDGAR search response invalid", "json_decode", offset, []) from None
        hits_container = data.get("hits") if isinstance(data, dict) else None
        hits = hits_container.get("hits") if isinstance(hits_container, dict) else None
        if not isinstance(hits, list):
            raise _search_failure("EDGAR search response invalid", "hits_shape", offset, [])
        results: list[dict[str, Any]] = []
        for hit_index, hit in enumerate(hits):
            src = hit.get("_source") if isinstance(hit, dict) else None
            fields_valid = {
                "source": isinstance(src, dict),
                "form": isinstance(src, dict) and source_text(src.get("form")),
                "file_date": isinstance(src, dict) and source_date(src.get("file_date")),
                "adsh": isinstance(src, dict) and source_text(src.get("adsh")),
                "display_names": (isinstance(src, dict) and isinstance(src.get("display_names"), list)
                                  and bool(src["display_names"])
                                  and all(source_text(value) for value in src["display_names"])),
                "ciks": (isinstance(src, dict) and isinstance(src.get("ciks"), list) and bool(src["ciks"])
                         and all(_valid_search_cik(value) for value in src["ciks"])),
            }
            if not all(fields_valid.values()):
                raise _search_failure("EDGAR search hit invalid", "hit_fields", offset, results,
                                      hit_index=hit_index, fields_valid=fields_valid)
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
                "form_type": normalize_filing_form(src["form"]),
                "source_form_type": src["form"],
                "entity_name": entity_name,
                "ticker": ticker_str,
                "file_url": file_url,
                "description": entity_name,
                "ciks": ciks,
                "adsh": adsh,
            })
        total_payload = hits_container.get("total")
        total = total_payload.get("value") if isinstance(total_payload, dict) else total_payload
        relation = total_payload.get("relation", "eq") if isinstance(total_payload, dict) else "eq"
        if type(total) is not int or total < offset + len(hits) or relation != "eq":
            raise _search_failure("EDGAR search total unavailable", "total_shape", offset, results,
                                  page_count=len(hits), total_is_integer=type(total) is int,
                                  total_covers_page=type(total) is int and total >= offset + len(hits),
                                  relation_is_exact=relation == "eq")
        return CoverageRecords(results, coverage={"provider_total":total})

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

        resp = provider_request("edgar", "GET",
            url,
            headers={"User-Agent": self._user_agent},
            timeout=15,
        )
        try:
            data = resp.json()
            recent = data["filings"]["recent"]
            forms = recent["form"]
            dates = recent["filingDate"]
            accessions = recent["accessionNumber"]
            documents = recent["primaryDocument"]
            if not all(isinstance(items, list) for items in (forms, dates, accessions, documents)) or not len(forms) == len(dates) == len(accessions) == len(documents):
                raise ValueError("invalid submissions schema")
        except Exception:
            raise SourceFetchError("EDGAR submissions response invalid", reason_code="invalid_response") from None

        results: list[dict[str, Any]] = []
        for i in range(min(len(forms), len(dates))):
            if not source_text(forms[i]) or not source_date(dates[i]) or not source_text(accessions[i]) or not source_text(documents[i]):
                raise SourceFetchError("EDGAR submission record invalid", reason_code="invalid_response",
                                       partial_data={"filings": results})
            if form_types and normalize_filing_form(forms[i]) not in {normalize_filing_form(form) for form in form_types}:
                continue
            results.append({
                "accession_number": accessions[i] if i < len(accessions) else "",
                "form": forms[i],
                "filing_date": dates[i],
                "primary_document": documents[i] if i < len(documents) else "",
            })
            if len(results) >= count:
                break
        matching_total = sum(1 for form in forms if not form_types or normalize_filing_form(form) in {normalize_filing_form(value) for value in form_types})
        archive_files = data.get("filings", {}).get("files")
        coverage = {"mode":"bounded_sample", "complete":False, "limit":count,
                    "count":len(results), "returned":len(results), "source_total":matching_total,
                    "source_recent_total":len(forms), "has_more":matching_total > len(results),
                    "archived_possible":bool(archive_files) if isinstance(archive_files,list) else True,
                    "scope":"latest_matching_forms_in_current_submissions", "cik":cik}
        return CoverageRecords(results, coverage=coverage)

    def _get_submission_metadata(self, filename: str) -> tuple[dict, dict]:
        """One bounded metadata response; caller validates its native schema."""
        url = f'{SUBMISSIONS_BASE}/{filename}'
        response = None
        def unique_fields(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError('duplicate history field')
                result[key] = value
            return result
        try:
            provider_timeout('edgar')
            response = provider_request('edgar', 'GET', url,
                headers={'User-Agent': self._user_agent}, timeout=15,
                operation='submission_history', stream=True, allow_redirects=False)
            if response.status_code != 200 or response.url != url:
                raise ValueError('unexpected history response')
            raw = read_bounded_response(response, provider='edgar', max_bytes=16 * 1024 * 1024)
            data = json.loads(raw, object_pairs_hook=unique_fields)
            if not isinstance(data, dict):
                raise ValueError('invalid history object')
            provider_timeout('edgar')
            provenance = {'source_url': url, 'response_sha256': hashlib.sha256(raw).hexdigest(),
                          'observed_at': datetime.now(timezone.utc).isoformat()}
            return data, provenance
        except SourceFetchError:
            raise
        except (ValueError, TypeError, UnicodeError):
            raise SourceFetchError('SEC history response invalid', reason_code='invalid_response') from None
        finally:
            if response is not None:
                response.close()

    def get_company_submission_history(self, cik: str) -> dict:
        """Complete recent metadata plus archive inventory, not complete filing history."""
        cik = _history_cik(cik)
        if current_provider_deadline('edgar') is None:
            with provider_budget('edgar', time.monotonic() + 60):
                return self.get_company_submission_history(cik)
        data, provenance = self._get_submission_metadata(f'CIK{cik}.json')
        try:
            native_cik = data.get('cik')
            if type(native_cik) is int:
                native_cik = str(native_cik)
            if _history_cik(native_cik) != cik:
                raise ValueError('history CIK mismatch')
            filings = data['filings']
            rows = _history_rows(filings['recent'])
            files = filings['files']
            if not isinstance(files, list) or len(files) > 1000:
                raise ValueError('invalid archive inventory')
            archives = [_history_archive(cik, entry) for entry in files]
            if len({entry['name'] for entry in archives}) != len(archives):
                raise ValueError('duplicate archive')
        except (ValueError, TypeError, KeyError, SourceFetchError):
            raise SourceFetchError('SEC history metadata invalid', reason_code='invalid_response') from None
        provider_timeout('edgar')
        return {'cik': cik, 'filings': rows, 'archives': archives, **provenance,
                'coverage': {'complete': True, 'mode': 'complete_recent_and_archive_inventory'}}

    def get_company_submission_archive(self, cik: str, descriptor: dict) -> dict:
        """Exact native archive arrays with count/range checks, under the same budget."""
        cik = _history_cik(cik)
        descriptor = _history_archive(cik, descriptor)
        if current_provider_deadline('edgar') is None:
            with provider_budget('edgar', time.monotonic() + 60):
                return self.get_company_submission_archive(cik, descriptor)
        data, provenance = self._get_submission_metadata(descriptor['name'])
        try:
            rows = _history_rows(data)
            if (len(rows) != descriptor['filingCount'] or any(
                    not descriptor['filingFrom'] <= row['filing_date'] <= descriptor['filingTo']
                    for row in rows)):
                raise ValueError('archive range/count mismatch')
        except (ValueError, TypeError):
            raise SourceFetchError('SEC archive metadata invalid', reason_code='invalid_response') from None
        provider_timeout('edgar')
        return {'cik': cik, 'filings': rows, 'descriptor': descriptor, **provenance,
                'coverage': {'complete': True, 'mode': 'complete_archive_metadata'}}

    def get_complete_submission(self, url: str, *, accession: str, form_type: str,
                                filing_date: str, required_exhibits=(),
                                max_submission_bytes=64 * 1024 * 1024) -> dict:
        """Acquire full selected filing evidence under the existing source deadline."""
        from .filing_acquisition import acquire_complete_submission
        return acquire_complete_submission(
            self._user_agent, url, accession=accession, form_type=form_type,
            filing_date=filing_date, required_exhibits=required_exhibits,
            max_submission_bytes=max_submission_bytes)

    def get_primary_document_url(self, url: str, form_type: str | None = None) -> str:
        """Resolve the matching main filing document, independently of identity URL."""
        from bs4 import BeautifulSoup
        from urllib.parse import urljoin, urlparse, parse_qs
        if not re.search(r"-index\.html?$", url):
            return url
        key = f"primary_document|{url}|{form_type}"
        if key in self._session_cache:
            return self._session_cache[key]
        resp = provider_request("edgar", "GET", url, headers={"User-Agent":self._user_agent},
                                timeout=15, operation="filing_detail")
        candidates = []
        directory = urlparse(url).path.rsplit("/", 1)[0]
        accession = re.fullmatch(r"/Archives/edgar/data/[0-9]+/([0-9]+)", directory)
        for row in BeautifulSoup(resp.text, "html.parser").select("table.tableFile tr"):
            cells = row.find_all("td")
            if len(cells) < 4 or not cells[2].find("a"):
                continue
            record_form = cells[3].get_text(strip=True)
            if (form_type and normalize_filing_form(record_form) != normalize_filing_form(form_type)) or (not form_type and cells[0].get_text(strip=True) != "1"):
                continue
            href = cells[2].find("a").get("href", "")
            candidate = urljoin(url, href)
            parsed = urlparse(candidate)
            if parsed.path in ("/ix", "/ixviewer/doc/action"):
                documents = parse_qs(parsed.query).get("doc", [])
                if len(documents) != 1:
                    continue
                candidate = urljoin(url, documents[0])
                parsed = urlparse(candidate)
            if parsed.netloc != "www.sec.gov" or not parsed.path.startswith("/Archives/edgar/data/") or re.search(r"-index\.html?$", candidate):
                continue
            document_directory = directory
            if filing_form_family(record_form) in {"SCHEDULE 13D", "SCHEDULE 13G"}:
                # Ownership indices can use the subject CIK while their listed
                # documents use another CIK. Bind to the exact accession, never
                # use a document-directory CIK to infer the subject issuer.
                scope = (re.fullmatch(r"(/Archives/edgar/data/[0-9]+/" + re.escape(accession.group(1))
                                     + r")/(.+)", parsed.path) if accession else None)
                if (not scope or parsed.scheme != "https" or parsed.query or parsed.fragment
                        or re.search(r"-index\.html?$", parsed.path)
                        or "%" in parsed.path or "\\" in parsed.path or parsed.path.endswith("/")
                        or any(part in {".", ".."} for part in parsed.path.split("/"))):
                    continue
                document_directory = scope.group(1)
            elif not parsed.path.startswith(directory + "/"):
                continue
            candidates.append((cells[0].get_text(strip=True), normalize_filing_form(record_form), candidate, document_directory))
        # SEC ownership indices list the same sequence/XML both raw and through
        # a form-specific stylesheet. Coalesce only an actually listed raw pair
        # with the same form, sequence and accepted accession directory.
        canonical = set()
        for sequence, record_form, candidate, document_directory in candidates:
            parsed = urlparse(candidate)
            family = filing_form_family(record_form)
            if family in {"SCHEDULE 13D", "SCHEDULE 13G"}:
                stylesheet = re.escape(family.replace(" ", "_"))
                match = re.fullmatch(re.escape(document_directory) + rf"/xsl{stylesheet}_X[0-9]+/([^/]+\.xml)", parsed.path)
                if match:
                    raw = parsed._replace(path=document_directory + "/" + match.group(1)).geturl()
                    if (sequence, record_form, raw, document_directory) in candidates:
                        candidate = raw
            canonical.add(candidate)
        if len(canonical) != 1:
            raise SourceFetchError("EDGAR primary document ambiguous or unavailable", reason_code="invalid_response")
        document = canonical.pop()
        self._session_cache[key] = document
        return document

    def get_filing_text(self, url: str, *, form_type: str | None = None) -> str:
        """Download the text content of a filing document.

        Args:
            url: Full URL to the filing document on EDGAR.

        Returns:
            Filing text content (may be HTML), or empty string on failure.
        """
        import requests

        url = self.get_primary_document_url(url, form_type)
        resp = provider_request("edgar", "GET", url,
                                headers={"User-Agent": self._user_agent}, timeout=30,
                                operation="filing_text")
        if not isinstance(resp.text, str) or not resp.text.strip():
            raise SourceFetchError("EDGAR filing text invalid", reason_code="invalid_response")
        return resp.text

    def get_recent_form4(
        self, ticker: str, days_back: int = 30, *, as_of: str | None = None
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

        if current_provider_deadline("edgar") is None:
            with provider_budget("edgar", time.monotonic()+60):
                return self.get_recent_form4(ticker, days_back, as_of=as_of)
        as_of = as_of or current_session_date()
        if type(days_back) is not int or days_back < 0 or not source_date(as_of) or len(as_of) != 10:
            raise SourceFetchError("SEC Form4 window invalid", reason_code="invalid_response")
        cutoff = (datetime.fromisoformat(as_of) - timedelta(days=days_back)).strftime("%Y-%m-%d")
        coverage = {"mode": "exhaustive_window", "complete": False, "ticker": ticker,
                    "date_from": cutoff, "date_to": as_of, "archive_files_consulted": [],
                    "matching_filings": 0, "returned_transactions": 0}
        enriched = []
        try:
            cik = self.ticker_to_cik(ticker)
            if not cik:
                raise SourceFetchError("SEC Form4 issuer identity unresolved", reason_code="invalid_response")
            coverage["cik"] = cik
            history = self.get_company_submission_history(cik)
            if history.get("coverage", {}).get("complete") is not True:
                raise SourceFetchError("SEC Form4 history incomplete", reason_code="invalid_response")
            rows = list(history["filings"])
            if len(rows) > 100000:
                raise SourceFetchError("SEC Form4 metadata limit reached", reason_code="invalid_response")
            coverage.update(recent_metadata_count=len(rows), history_response_sha256=history["response_sha256"])
            for descriptor in history["archives"]:
                if descriptor["filingFrom"] <= as_of and descriptor["filingTo"] >= cutoff:
                    archive = self.get_company_submission_archive(cik, descriptor)
                    if archive.get("coverage", {}).get("complete") is not True:
                        raise SourceFetchError("SEC Form4 archive incomplete", reason_code="invalid_response")
                    if len(rows)+len(archive["filings"]) > 100000:
                        raise SourceFetchError("SEC Form4 metadata limit reached", reason_code="invalid_response")
                    rows.extend(archive["filings"])
                    coverage["archive_files_consulted"].append(descriptor["name"])
            unique = {}
            for row in rows:
                provider_timeout("edgar")
                identity = row["accession_number"]
                if identity in unique and unique[identity] != row:
                    raise SourceFetchError("SEC Form4 accession conflict", reason_code="invalid_response")
                unique[identity] = row
            recent = sorted((row for row in unique.values()
                if row["form"] in {"4", "4/A"} and cutoff <= row["filing_date"] <= as_of),
                key=lambda row: (row["filing_date"], row["accession_number"]))
            coverage["matching_filings"] = len(recent)
            for filing in recent:
                provider_timeout("edgar")
                transactions = self._parse_form4_xml(cik, filing)
                provider_timeout("edgar")
                # Identical native transactions remain separate observations.
                if len(enriched)+max(1, len(transactions)) > 100000:
                    raise SourceFetchError("SEC Form4 transaction limit reached", reason_code="invalid_response")
                if transactions:
                    enriched.extend({**filing, **txn} for txn in transactions)
                else:
                    enriched.append(filing)
            provider_timeout("edgar")
            coverage.update(complete=True, returned_transactions=len(enriched))
            return CoverageRecords(enriched, coverage=coverage)
        except Exception as exc:
            error = source_fetch_error("SEC Form4 window incomplete", exc)
            coverage.update(complete=False, returned_transactions=len(enriched))
            error.partial_data = {"form4_filings": enriched, "coverage": coverage}
            raise error from None

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
            raise SourceFetchError("EDGAR Form4 document identity missing", reason_code="invalid_response")

        # Strip XSL prefix (e.g. "xslF345X06/file.xml" → "file.xml")
        # SEC serves transformed HTML at the XSL path; raw XML is at the base.
        if "/" in primary_doc:
            primary_doc = primary_doc.rsplit("/", 1)[-1]

        # Archives uses an integer CIK directory; ten-digit padding belongs to
        # the submissions API. Keep redirect denial and request the native path.
        archive_cik = _history_cik(cik).lstrip("0")
        accession_nodash = accession.replace("-", "")
        url = f"https://www.sec.gov/Archives/edgar/data/{archive_cik}/{accession_nodash}/{primary_doc}"

        response = None
        try:
            response = provider_request("edgar", "GET", url,
                headers={"User-Agent": self._user_agent}, timeout=provider_timeout("edgar"),
                operation="form4_xml", stream=True, allow_redirects=False)
            if response.status_code != 200:
                raise SourceFetchError("EDGAR Form4 request failed", reason_code="http_error", http_status=response.status_code)
            raw = read_bounded_response(response, provider="edgar", max_bytes=16*1024*1024)
            try:
                root = ElementTree.fromstring(raw)
                if root.tag.rsplit("}", 1)[-1] != "ownershipDocument":
                    raise ElementTree.ParseError("not ownership XML")
            except ElementTree.ParseError:
                raise SourceFetchError("EDGAR Form 4 XML invalid", reason_code="invalid_response") from None
            provider_timeout("edgar")
        except Exception as exc:
            raise source_fetch_error("EDGAR Form4 acquisition failed", exc) from None
        finally:
            if response is not None:
                response.close()

        # Handle XML namespaces
        ns = ""
        if root.tag.startswith("{"):
            ns = root.tag.split("}")[0] + "}"

        # SEC Ownership XML spec table 3.5 requires this envelope even for an
        # original or amendment with no transactions. An empty root is not a
        # valid no-event document. Bind the native issuer to the queried issuer;
        # reporting-owner/accession CIKs cannot substitute for that identity.
        def one(parent, name):
            values = parent.findall(f"{ns}{name}")
            if len(values) != 1:
                raise ValueError("missing or ambiguous ownership field")
            return values[0]

        def text(parent, name):
            element = one(parent, name)
            if len(element) or not source_text(element.text):
                raise ValueError("invalid ownership field")
            return element.text.strip()

        try:
            form = text(root, "documentType")
            report_date = text(root, "periodOfReport")
            filed = filing.get("filing_date")
            if (form not in {"4", "4/A"} or filing.get("form", form) != form
                    or len(report_date) != 10 or not source_date(report_date)
                    or (filed is not None and (not source_date(filed) or len(filed) != 10
                                              or report_date > filed))):
                raise ValueError("ownership form or report period mismatch")
            issuer = one(root, "issuer")
            if _history_cik(text(issuer, "issuerCik")) != _history_cik(cik):
                raise ValueError("ownership issuer mismatch")
            owners = root.findall(f"{ns}reportingOwner")
            if not owners:
                raise ValueError("ownership reporting owner missing")
            for owner in owners:
                provider_timeout("edgar")
                _history_cik(text(one(owner, "reportingOwnerId"), "rptOwnerCik"))
        except (ValueError, TypeError, SourceFetchError) as exc:
            if isinstance(exc, SourceFetchError) and exc.reason_code == "timeout":
                raise
            raise SourceFetchError("EDGAR Form4 ownership envelope invalid", reason_code="invalid_response") from None

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
        owner_cik_el = root.find(f".//{ns}reportingOwnerId/{ns}rptOwnerCik")
        owner_cik = (owner_cik_el.text or "").strip() if owner_cik_el is not None else ""
        for txn_tag in (f"{ns}nonDerivativeTransaction", f"{ns}derivativeTransaction"):
            for txn_el in root.findall(f".//{txn_tag}"):
                provider_timeout("edgar")
                if len(transactions) >= 100000:
                    raise SourceFetchError("SEC Form4 XML transaction limit reached", reason_code="invalid_response")
                coding_el = txn_el.find(f".//{ns}transactionCoding")
                tx_code = ""
                if coding_el is not None:
                    code_el = coding_el.find(f"{ns}transactionCode")
                    if code_el is not None and code_el.text:
                        tx_code = code_el.text.strip()

                transaction_type = "other"
                acquired_disposed = ""
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

                    ad_el = amounts_el.find(f".//{ns}transactionAcquiredDisposedCode/{ns}value")
                    acquired_disposed = (ad_el.text or "").strip() if ad_el is not None else ""
                if acquired_disposed in ("A", "D"):
                    transaction_type = "buy" if acquired_disposed == "A" else "sell"
                # Contradictory P/S direction cannot establish a market trade.
                if (tx_code == "P" and acquired_disposed != "A") or (tx_code == "S" and acquired_disposed != "D"):
                    transaction_type = "other"
                open_market = txn_tag == f"{ns}nonDerivativeTransaction" and (
                    (tx_code == "P" and acquired_disposed == "A") or (tx_code == "S" and acquired_disposed == "D"))

                transactions.append({
                    "transaction_type": transaction_type,
                    "transaction_code": tx_code,
                    "acquired_disposed": acquired_disposed,
                    "open_market": open_market,
                    "owner_cik": owner_cik,
                    "transaction_id": f"{accession}:{txn_tag.rsplit('}', 1)[-1]}:{len(transactions)}",
                    "shares": shares,
                    "price_per_share": price,
                    "owner_name": owner_name,
                    "owner_title": owner_title,
                    "is_officer": is_officer,
                    "is_director": is_director,
                })

        provider_timeout("edgar")
        return transactions

    def get_recent_13d(self, days_back: int = 60, *, as_of: str | None = None) -> list[dict[str, Any]]:
        """Get recent SC 13D (activist) filings.

        Args:
            days_back: How many days back to search.

        Returns:
            List of filing metadata dicts.
        """
        from datetime import datetime, timedelta

        reference = datetime.fromisoformat(as_of) if as_of else datetime.fromisoformat(current_session_date())
        date_from = (reference - timedelta(days=days_back)).strftime("%Y-%m-%d")
        date_to = reference.strftime("%Y-%m-%d")
        return self.search_filings("SCHEDULE 13D", date_from=date_from, date_to=date_to)

    def _normalize_name(self, name: str) -> str:
        """Normalize a company name for matching."""
        name = self._NAME_SUFFIXES.sub("", name.lower())
        # Collapse whitespace and strip
        return " ".join(name.split()).strip(" .,")

    @staticmethod
    def _select_company_ticker(tickers: set[str]) -> str | None:
        """Resolve a unique ticker or an unambiguous base-extension pair."""
        if len(tickers) == 1:
            return next(iter(tickers))

        base_tickers = {
            ticker
            for ticker in tickers
            if any(
                other_ticker.startswith(ticker)
                for other_ticker in tickers
                if other_ticker != ticker
            )
        }
        if len(base_tickers) == 1:
            base_ticker = next(iter(base_tickers))
            if all(
                other_ticker.startswith(base_ticker)
                for other_ticker in tickers
                if other_ticker != base_ticker
            ):
                return base_ticker
        return None

    def _ensure_name_map(self) -> dict[str, str]:
        """Build and cache normalized-company-name → ticker mapping."""
        if self._name_to_ticker_cache is not None:
            return self._name_to_ticker_cache

        # Ensure company_tickers.json is downloaded
        self._ensure_company_tickers()
        tickers_data = self._session_cache.get("_company_tickers", {})

        candidates_by_name_and_cik: dict[str, dict[int, set[str]]] = {}
        for entry in tickers_data.values():
            title = entry.get("title")
            ticker = entry.get("ticker")
            cik = entry.get("cik_str")
            if (
                not isinstance(title, str)
                or not isinstance(ticker, str)
                or type(cik) is not int
                or cik <= 0
            ):
                continue
            title = title.strip()
            ticker = ticker.strip().upper()
            if not title or not ticker:
                continue
            normalized_title = self._normalize_name(title)
            if not normalized_title:
                continue
            candidates_by_name_and_cik.setdefault(normalized_title, {}).setdefault(
                cik, set()
            ).add(ticker)

        mapping: dict[str, str] = {}
        for name, tickers_by_cik in candidates_by_name_and_cik.items():
            if len(tickers_by_cik) != 1:
                continue
            tickers = next(iter(tickers_by_cik.values()))
            selected = self._select_company_ticker(tickers)
            if selected is not None:
                mapping[name] = selected
        self._name_to_ticker_cache = mapping
        return mapping

    def _ensure_company_tickers(self) -> None:
        """Download company_tickers.json if not already cached."""
        cache_key = "_company_tickers"
        if cache_key in self._session_cache:
            return

        import requests

        resp = provider_request("edgar", "GET", COMPANY_TICKERS_URL,
                                headers={"User-Agent": self._user_agent}, timeout=15,
                                operation="company_tickers")
        try:
            data = resp.json()
            if not isinstance(data, dict) or not data or not all(isinstance(row, dict) for row in data.values()):
                raise ValueError("invalid ticker schema")
        except Exception:
            raise SourceFetchError("EDGAR company tickers invalid", reason_code="invalid_response") from None
        self._session_cache[cache_key] = data

    def company_ticker_map(self) -> dict:
        """Return a detached native CIK/ticker map for the frozen source evidence."""
        from copy import deepcopy
        self._ensure_company_tickers()
        return deepcopy(self._session_cache["_company_tickers"])

    def name_to_ticker(
        self,
        company_name: str,
        *,
        allow_prefix: bool = True,
    ) -> str | None:
        """Resolve a company name to ticker using SEC's company_tickers.json.

        Exact normalized matches are always accepted. Prefix fallback remains
        the default for compatibility, but strict callers can disable it.
        Returns None if no match found.
        """
        mapping = self._ensure_name_map()
        normalized = self._normalize_name(company_name)

        if not normalized:
            return None

        # Exact match
        if normalized in mapping:
            return mapping[normalized]

        if not allow_prefix:
            return None

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
        return collection_envelope(self.search_filings(
            form_type=params.get("form_type", "10-K"),
            date_from=params.get("date_from"),
            date_to=params.get("date_to"),
            ticker=params.get("ticker"),
            keyword=params.get("keyword"),
        ))

    def _dispatch_company_filings(self, params: dict[str, Any]) -> dict[str, Any]:
        return collection_envelope(self.get_company_filings(
            cik=params.get("cik", ""),
            form_types=params.get("form_types"),
            count=params.get("count", 10),
        ))

    def _dispatch_filing_text(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"data": self.get_filing_text(url=params.get("url", ""))}

    def _dispatch_recent_form4(self, params: dict[str, Any]) -> dict[str, Any]:
        return collection_envelope(self.get_recent_form4(
            ticker=params.get("ticker", ""), days_back=params.get("days_back", 30), as_of=params.get("as_of")))

    def _dispatch_recent_13d(self, params: dict[str, Any]) -> dict[str, Any]:
        return collection_envelope(self.get_recent_13d(days_back=params.get("days_back", 60), as_of=params.get("as_of")))

    def _dispatch_ticker_to_cik(self, params: dict[str, Any]) -> dict[str, Any]:
        cik = self.ticker_to_cik(params.get("ticker", ""))
        return {"data": cik}
