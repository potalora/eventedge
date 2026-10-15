"""Regulations.gov data source for proposed federal rules.

Free API key from api.data.gov. 1,000 requests/hour.
Used by P5 (regulatory pipeline → affected companies).
"""
from __future__ import annotations

import json
import logging
import os
import time
from contextvars import ContextVar
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from .evidence import current_session_date, CoverageRecords, bounded_coverage, collection_envelope
from .request_policy import provider_request, provider_timeout, read_bounded_response, provider_budget, current_provider_deadline
from .fetch_errors import SourceFetchError, source_fetch_error, source_text, source_date, source_number

logger = logging.getLogger(__name__)

BASE_URL = "https://api.regulations.gov/v4"
_RATE_DELAY = 0.5  # Conservative rate limiting
MAX_REGULATION_SOURCE_BYTES = 32 * 1024**2
_CONTENT_BUDGET = ContextVar('regulations_content_bytes', default=None)


def _read_response(response, maximum, *, retained=None):
    """Charge discovery, details and originals to one opt-in source byte owner."""
    owner = _CONTENT_BUDGET.get()
    if owner is not None:
        maximum = min(maximum, owner['limit'] - owner['used'] - owner['reserved'])
        if owner['exhausted'] or maximum <= 0:
            raise SourceFetchError('Regulations source byte limit reached', reason_code='invalid_response')
    readable = response
    if retained is not None:
        class CapturedResponse:
            headers = response.headers
            def iter_content(self, chunk_size):
                for chunk in response.iter_content(chunk_size=chunk_size):
                    if isinstance(chunk, bytes) and len(retained) + len(chunk) <= maximum:
                        retained.extend(chunk)
                    yield chunk
        readable = CapturedResponse()
    try:
        raw = read_bounded_response(readable, provider='regulations', max_bytes=maximum)
    except Exception:
        # The bounded reader does not expose partial consumed bytes. Reserve the
        # remaining capacity on any failed stream rather than recycling it.
        if owner is not None:
            if retained is not None:
                owner['used'] += len(retained)
            owner.update(reserved=owner['limit'] - owner['used'], exhausted=True)
        raise
    if owner is not None:
        owner['used'] += len(raw)
    return raw


def _page_json(raw: bytes):
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result
    def invalid_constant(value):
        raise ValueError("nonfinite JSON")
    try:
        return json.loads(raw, object_pairs_hook=unique_object, parse_constant=invalid_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise SourceFetchError("Provider page JSON invalid", reason_code="invalid_response") from None


class RegulationsSource:
    """Data source for federal regulations from regulations.gov."""

    name: str = "regulations"
    requires_api_key: bool = True

    def __init__(self, api_key: str | None = None, *, pdf_parser=None) -> None:
        self._api_key = api_key or os.environ.get("REGULATIONS_API_KEY", "")
        self._cache: dict[str, Any] = {}
        # Optional bounded-child adapter: (body, *, check) -> PDF evidence.
        # Never import or invoke pypdf inside the acquisition parent.
        if pdf_parser is not None:
            raise ValueError('regulation_pdf_parser_unavailable')
        self._pdf_parser = None

    def fetch(self, params: dict[str, Any]) -> dict[str, Any]:
        method = params.get("method", "search_documents")
        dispatch = {
            "search_documents": self._dispatch_search,
            "recent_proposed_rules": self._dispatch_recent_proposed,
        }
        handler = dispatch.get(method)
        if handler is None:
            return {"error": f"Unknown method '{method}'"}
        try:
            return handler(params)
        except SourceFetchError as exc:
            return {**exc.partial_data, "error": str(exc)}
        except Exception:
            logger.error("RegulationsSource.fetch(%s) failed", method)
            return {"error": f"{method} fetch failed"}

    def is_available(self) -> bool:
        if not self._api_key:
            return False
        try:
            import requests  # noqa: F401
            return True
        except ImportError:
            return False

    def search_documents(
        self, search_term: str | None = None, agency_id: str | None = None,
        document_type: str = "Proposed Rule", posted_date_from: str | None = None,
        page_size: int = 100, *, posted_date_to: str | None = None,
    ) -> list[dict]:
        """Read a complete sorted date window, or an explicitly bounded latest page.

        The broken server-side postedDate filter remains unused. Every raw date
        is checked before a strictly older row can prove the window boundary.
        """
        if current_provider_deadline("regulations") is None:
            with provider_budget("regulations", time.monotonic()+60):
                return self.search_documents(search_term, agency_id, document_type,
                    posted_date_from, page_size, posted_date_to=posted_date_to)
        if not self._api_key:
            raise SourceFetchError("Regulations access missing", reason_code="provider_error")
        if (type(page_size) is not int or not 1 <= page_size <= 250
                or (posted_date_from is not None and (not source_date(posted_date_from) or len(posted_date_from) != 10))
                or (posted_date_to is not None and (not source_date(posted_date_to) or len(posted_date_to) != 10))
                or (posted_date_from and posted_date_to and posted_date_from > posted_date_to)):
            raise SourceFetchError("Regulations window invalid", reason_code="invalid_response")
        params = {"filter[documentType]": document_type, "page[size]": page_size,
                  "sort": "-postedDate"}
        if search_term:
            params["filter[searchTerm]"] = search_term
        if agency_id:
            params["filter[agencyId]"] = agency_id
        results, seen, previous, used_bytes = [], set(), None, 0
        pagination_metadata = None
        coverage = {"mode": "exhaustive_window" if posted_date_from else "bounded_sample",
                    "complete": False, "agency": agency_id,
                    "posted_date_from": posted_date_from, "posted_date_to": posted_date_to,
                    "pages": 0}
        try:
            for page in range(1, 201):
                response = None
                try:
                    provider_timeout('regulations')
                    response = provider_request("regulations", "GET", f"{BASE_URL}/documents",
                        params={**params, "page[number]": page},
                        headers={"X-Api-Key": self._api_key}, timeout=provider_timeout("regulations"),
                        stream=True, allow_redirects=False)
                    if response.status_code != 200:
                        raise SourceFetchError("Regulations request failed", reason_code="http_error", http_status=response.status_code)
                    raw = _read_response(response, 32*1024*1024-used_bytes)
                    used_bytes += len(raw)
                    data = _page_json(raw)
                    provider_timeout("regulations")
                finally:
                    if response is not None:
                        response.close()
                coverage["pages"] = page
                if not isinstance(data, dict) or not isinstance(data.get("data"), list):
                    raise SourceFetchError("Regulations response invalid", reason_code="invalid_response")
                meta = data.get("meta")
                total_pages = meta.get("totalPages") if isinstance(meta, dict) else None
                total_elements = meta.get("totalElements") if isinstance(meta, dict) else None
                metadata = (total_pages, "totalElements" in meta, total_elements) if isinstance(meta, dict) else None
                if (type(total_pages) is not int or total_pages < 0
                        or (total_pages and page > total_pages)
                        or ("totalElements" in meta and (type(total_elements) is not int or total_elements < 0))
                        or (total_pages == 0 and (data["data"] or total_elements not in (None, 0)))
                        or (pagination_metadata is not None and metadata != pagination_metadata)):
                    raise SourceFetchError("Regulations pagination invalid", reason_code="invalid_response")
                pagination_metadata = metadata
                older = False
                for item in data["data"]:
                    attrs = item.get("attributes") if isinstance(item, dict) else None
                    if (not isinstance(attrs, dict) or not source_text(item.get("id"))
                            or not all(source_text(attrs.get(k)) for k in ("title", "agencyId", "documentType"))
                            or not source_date(attrs.get("postedDate"))
                            or (agency_id and attrs["agencyId"] != agency_id)
                            or attrs["documentType"] != document_type):
                        raise SourceFetchError("Regulations document invalid", reason_code="invalid_response")
                    day = attrs["postedDate"][:10]
                    if previous is not None and day > previous:
                        raise SourceFetchError("Regulations ordering invalid", reason_code="invalid_response")
                    previous = day
                    if item["id"] in seen:
                        raise SourceFetchError("Regulations repeated document", reason_code="invalid_response")
                    seen.add(item["id"])
                    if len(seen) > 50000:
                        raise SourceFetchError("Regulations row limit reached", reason_code="invalid_response")
                    older |= bool(posted_date_from and day < posted_date_from)
                    if ((posted_date_from and day < posted_date_from)
                            or (posted_date_to and day > posted_date_to)):
                        continue
                    results.append({"document_id": item["id"], "title": attrs["title"],
                        "agency_id": attrs["agencyId"], "document_type": attrs["documentType"],
                        "posted_date": attrs["postedDate"], "comment_end_date": attrs.get("commentEndDate", ""),
                        "summary": (attrs.get("summary") or "")[:500], "docket_id": attrs.get("docketId", "")})
                terminal = page >= total_pages
                if (total_elements is not None and
                        (len(seen) > total_elements or (terminal and len(seen) != total_elements))):
                    raise SourceFetchError("Regulations pagination totals inconsistent", reason_code="invalid_response")
                coverage.update(total=meta.get("totalElements"), has_next=not terminal)
                if older or terminal or not posted_date_from:
                    coverage.update(complete=bool(posted_date_from),
                                    termination="older_than_window" if older else "terminal_page" if terminal else "bounded_page")
                    provider_timeout("regulations")
                    return CoverageRecords(results, coverage=coverage)
                if not data["data"]:
                    raise SourceFetchError("Regulations nonterminal empty page", reason_code="invalid_response")
            raise SourceFetchError("Regulations page limit reached", reason_code="invalid_response")
        except Exception as exc:
            error = source_fetch_error("Regulations search failed", exc)
            error.partial_data = {"proposed_rules": results, "coverage": {**coverage, "complete": False}}
            raise error from None

    def get_recent_proposed_rules(
        self,
        agencies: list[str] | None = None,
        days_back: int = 30,
        *, as_of: str | None = None, evidence_policy: str | None = None,
        retrieval_policy: str | None = None,
    ) -> list[dict]:
        """Get recently proposed rules, optionally filtered by agency."""
        from datetime import datetime, timedelta

        if retrieval_policy is not None:
            from .regulation_government_copy import POLICIES as RETRIEVAL_POLICIES
            if (type(retrieval_policy) is not str or retrieval_policy not in RETRIEVAL_POLICIES
                    or evidence_policy != 'complete_document_text_v1'):
                raise ValueError('invalid_regulation_retrieval_policy')
        if evidence_policy is not None:
            from .regulation_evidence import POLICY
            if evidence_policy != POLICY:
                raise ValueError('invalid_regulation_evidence_policy')
            if current_provider_deadline('regulations') is None:
                with provider_budget('regulations', time.monotonic() + 60):
                    return self.get_recent_proposed_rules(agencies, days_back, as_of=as_of,
                                                         evidence_policy=evidence_policy, retrieval_policy=retrieval_policy)
            return self._complete_recent_rules(agencies, days_back, as_of=as_of,
                                               retrieval_policy=retrieval_policy)

        date_from = ((datetime.fromisoformat(as_of) if as_of else datetime.fromisoformat(current_session_date())) - timedelta(days=days_back)).strftime("%Y-%m-%d")
        results = []

        failures, statuses, coverages = {}, {}, {}
        for agency in agencies or [None]:
            try:
                docs = self.search_documents(agency_id=agency, document_type="Proposed Rule",
                                             posted_date_from=date_from, posted_date_to=as_of or current_session_date())
                coverages[agency or "all_agencies"] = getattr(docs, "coverage", {})
                if as_of:
                    docs = [row for row in docs if (row.get("posted_date") or "")[:10] <= as_of]
                results.extend(docs)
            except SourceFetchError as exc:
                partial = exc.partial_data.get("proposed_rules", [])
                if as_of:
                    partial = [row for row in partial if (row.get("posted_date") or "")[:10] <= as_of]
                results.extend(partial)
                identity = agency or "all_agencies"
                coverages[identity] = exc.partial_data.get("coverage", {"complete": False})
                failures[identity] = exc.reason_code
                if exc.http_status is not None:
                    statuses[identity] = exc.http_status
        if failures:
            raise SourceFetchError("Regulations agency coverage incomplete", reason_code="batch_failure",
                                   failed_operations=failures, failed_http_statuses=statuses,
                                   partial_data={"proposed_rules": results, "coverage":{"mode":"exhaustive_window", "complete":False, "samples":coverages}})

        return CoverageRecords(results, coverage={"mode":"exhaustive_window", "complete":True, "samples":coverages, "as_of":as_of})

    def _content_request(self, url, *, maximum, api=False, capture=None):
        response, raw, complete = None, bytearray(), False
        try:
            provider_timeout('regulations')
            response = provider_request('regulations', 'GET', url,
                **({'params': {'include': 'attachments'}} if api else {}),
                **({'return_statuses': (403, 404, 410)} if capture is not None else {}),
                headers={'X-Api-Key': self._api_key} if api else {},
                timeout=provider_timeout('regulations'), stream=True, allow_redirects=False)
            if response.status_code != 200:
                if capture is not None:
                    _read_response(response, maximum, retained=raw)
                    complete = True
                raise SourceFetchError('Regulations content request failed', reason_code='http_error',
                                       http_status=response.status_code)
            body = _read_response(response, maximum, retained=raw if capture is not None else None)
            complete = True
            return body
        finally:
            if response is not None:
                if capture is not None:
                    from .regulation_evidence import original_receipt
                    capture.update(http_status=response.status_code, complete=complete,
                        receipt=original_receipt(bytes(raw), observed_at=datetime.now(timezone.utc).isoformat(),
                                                 url=url, allow_empty=True))
                response.close()

    def _complete_recent_rules(self, agencies, days_back, *, as_of, retrieval_policy=None):
        from .regulation_evidence import (
            POLICY, MAX_DETAIL_BYTES, MAX_BODY_BYTES, build_regulation_evidence,
            detail_plan, digest, original_receipt,
        )
        if _CONTENT_BUDGET.get() is not None:
            raise ValueError('nested_regulation_content_scope')
        owner = {'limit': MAX_REGULATION_SOURCE_BYTES, 'used': 0, 'reserved': 0, 'exhausted': False}
        token = _CONTENT_BUDGET.set(owner)
        try:
            discovery_error = None
            try:
                discovered = self.get_recent_proposed_rules(agencies, days_back, as_of=as_of)
                discovery = deepcopy(discovered.coverage)
                rows = deepcopy(list(discovered))
            except SourceFetchError as error:
                discovery_error = error
                discovery = deepcopy(error.partial_data.get('coverage', {'complete': False}))
                rows = deepcopy(error.partial_data.get('proposed_rules', []))
            corpus, failures, identities = {}, {}, {}
            for row in rows:
                docid = row['document_id']
                row['regulation_evidence_ref'] = docid
                if docid in identities and identities[docid] != row:
                    retained = corpus.get(docid)
                    previous_failure = deepcopy(failures.get(docid))
                    failures[docid] = {'identity': deepcopy(row),
                                      'detail_receipt': retained['detail_receipt'] if retained else None,
                                      'resources': retained['resources'] if retained else [],
                                      'body_receipts': {unit['resource_id']: unit['receipt'] for unit in retained['units']} if retained else {},
                                      'failure': {'code': 'invalid_regulation_identity',
                                      'reason_code': 'invalid_response', 'http_status': None}}
                    if retrieval_policy is not None:
                        if previous_failure is not None:
                            failures[docid] = {**previous_failure, 'failure': failures[docid]['failure']}
                        else:
                            failures[docid]['identity'] = deepcopy(identities[docid])
                            failures[docid]['retrieval_attempts'] = []
                            if retained is not None:
                                failures[docid]['body_receipts'] = {}
                                for unit in retained['units']:
                                    if 'government_copy' in unit:
                                        failures[docid]['retrieval_attempts'].extend([
                                            deepcopy(unit['primary_failure']),
                                            {'resource_id': unit['resource_id'], 'http_status': 200,
                                             'complete': True, 'receipt': deepcopy(unit['receipt'])}])
                                        if 'crosswalk_receipt' in unit['government_copy']:
                                            failures[docid]['retrieval_attempts'].append({
                                                'resource_id': unit['resource_id'], 'http_status': 200,
                                                'complete': True, 'receipt': deepcopy(
                                                    unit['government_copy']['crosswalk_receipt'])})
                                    else:
                                        failures[docid]['body_receipts'][unit['resource_id']] = deepcopy(unit['receipt'])
                    corpus.pop(docid, None)
                if docid not in identities:
                    identities[docid] = deepcopy(row)
                    detail_receipt, body_receipts, resources = None, {}, []
                    retrieval_attempts, government_copies = [], {}
                    try:
                        provider_timeout('regulations')
                        # Identity grammar is enforced before constructing an API URL.
                        from .regulation_evidence import _ID
                        if not isinstance(docid, str) or not _ID.fullmatch(docid):
                            raise ValueError('invalid_regulation_identity')
                        raw_detail = self._content_request(BASE_URL + '/documents/' + docid,
                                                           maximum=MAX_DETAIL_BYTES, api=True)
                        observed = datetime.now(timezone.utc).isoformat()
                        detail_receipt = original_receipt(raw_detail, observed_at=observed,
                            url=BASE_URL + '/documents/' + docid + '?include=attachments')
                        plan = detail_plan(row, raw_detail, observed_at=observed)
                        resources = plan['resources']
                        bodies, observed_bodies, pdfs = {}, {}, {}
                        for resource in resources:
                            rid, selected = resource['resource_id'], resource['selected']
                            attempt = {} if retrieval_policy is not None else None
                            try:
                                body = self._content_request(selected['fileUrl'], maximum=MAX_BODY_BYTES,
                                                             capture=attempt)
                            except Exception:
                                if attempt:
                                    retrieval_attempts.append({'resource_id': rid, **attempt})
                                if not (retrieval_policy is not None and resource['role'] == 'primary'
                                        and selected['format'] in {'html', 'htm', 'txt', 'text'}
                                        and attempt and attempt['complete'] is True
                                        and attempt['http_status'] in {403, 404, 410}):
                                    raise
                                from .regulation_government_copy import (
                                    POLICY_V2, government_copy_plan, government_crosswalk_url, extract_government_copy,
                                )
                                primary_failure = deepcopy(retrieval_attempts[-1])
                                government_plan = government_copy_plan(raw_detail, retrieval_policy=retrieval_policy)
                                government_url = government_plan['url']
                                government_attempt = {}
                                try:
                                    body = self._content_request(government_url, maximum=MAX_BODY_BYTES,
                                                                 capture=government_attempt)
                                finally:
                                    if government_attempt:
                                        retrieval_attempts.append({'resource_id': rid, **government_attempt})
                                government_copies[rid] = {'primary_failure': primary_failure}
                                if retrieval_policy == POLICY_V2 and government_plan['identity']['agency_id'] == 'FTC':
                                    try:
                                        extract_government_copy(raw_detail, body, retrieval_policy=retrieval_policy,
                                            check=lambda: provider_timeout('regulations'))
                                    except ValueError as error:
                                        if str(error) != 'missing_regulation_government_crosswalk':
                                            raise
                                        metadata_url = government_crosswalk_url(raw_detail, retrieval_policy=retrieval_policy)
                                        metadata_attempt = {}
                                        try:
                                            self._content_request(metadata_url, maximum=MAX_DETAIL_BYTES,
                                                capture=metadata_attempt)
                                        finally:
                                            if metadata_attempt:
                                                retrieval_attempts.append({'resource_id': rid, **metadata_attempt})
                                        government_copies[rid]['crosswalk_receipt'] = metadata_attempt['receipt']
                                stamp = government_attempt['receipt']['observed_at']
                            else:
                                stamp = datetime.now(timezone.utc).isoformat()
                                body_receipts[rid] = original_receipt(body, observed_at=stamp,
                                                                     url=selected['fileUrl'])
                            bodies[rid], observed_bodies[rid] = body, stamp
                            if selected['format'] == 'pdf':
                                raise ValueError('regulation_pdf_parser_unavailable')
                                provider_timeout('regulations')
                        corpus[docid] = build_regulation_evidence(row, raw_detail, bodies,
                            observed_at=observed, body_observed_at=observed_bodies, pdf_results=pdfs,
                            check=lambda: provider_timeout('regulations'),
                            retrieval_policy=retrieval_policy, government_copies=government_copies)
                    except Exception as error:
                        failure = source_fetch_error('Regulations document evidence failed', error)
                        code = str(error) if isinstance(error, ValueError) and str(error).startswith(
                            ('invalid_regulation_', 'missing_regulation_', 'unsupported_regulation_',
                             'restricted_regulation_', 'empty_regulation_', 'ambiguous_regulation_', 'regulation_')) else failure.reason_code
                        failures[docid] = {'identity': deepcopy(row), 'detail_receipt': detail_receipt,
                            'resources': resources, 'body_receipts': body_receipts,
                            'failure': {'code': code, 'reason_code': failure.reason_code,
                                        'http_status': failure.http_status}}
                        if retrieval_policy is not None:
                            failures[docid]['retrieval_attempts'] = retrieval_attempts
            for row in rows:
                docid = row['document_id']
                row['regulation_evidence_status'] = 'complete' if docid in corpus else 'failed'
                if docid in failures:
                    row['regulation_failure'] = deepcopy(failures[docid]['failure'])
            complete_rows = sum(row['regulation_evidence_status'] == 'complete' for row in rows)
            graph = {'policy': POLICY, 'corpus': corpus, 'acquisition_failures': failures,
                'rows': [{'index': index, 'document_id': row['document_id'], 'row_sha256': digest(row)}
                         for index, row in enumerate(rows)],
                'coverage': {'discovery_complete': discovery.get('complete') is True,
                    'content_complete': discovery.get('complete') is True and not failures and not owner['exhausted'],
                    'required_rows': len(rows), 'complete_rows': complete_rows,
                    'failed_rows': len(rows) - complete_rows, 'consumed_bytes': owner['used'],
                    'reserved_bytes': owner['reserved'],
                    'byte_limit': owner['limit'], 'byte_budget_exhausted': owner['exhausted']}}
            if retrieval_policy is not None:
                graph['retrieval_policy'] = retrieval_policy
            graph['manifest_sha256'] = digest(graph)
            if discovery_error is not None or not graph['coverage']['content_complete']:
                raise SourceFetchError('Regulations required content incomplete', reason_code='batch_failure',
                    partial_data={'proposed_rules': rows, 'coverage': discovery, 'regulation_evidence': graph})
            result = CoverageRecords(rows, coverage=discovery)
            result.regulation_evidence = graph
            return result
        finally:
            _CONTENT_BUDGET.reset(token)

    def clear_cache(self) -> None:
        self._cache.clear()

    def _dispatch_search(self, params: dict[str, Any]) -> dict[str, Any]:
        return collection_envelope(self.search_documents(
            search_term=params.get("search_term"),
            agency_id=params.get("agency_id"),
            document_type=params.get("document_type", "Proposed Rule"),
            posted_date_from=params.get("posted_date_from"),
            page_size=params.get("page_size",100),
            posted_date_to=params.get("posted_date_to"),
        ))

    def _dispatch_recent_proposed(self, params: dict[str, Any]) -> dict[str, Any]:
        rules = self.get_recent_proposed_rules(
            agencies=params.get("agencies"),
            days_back=params.get("days_back", 30),
            as_of=params.get("as_of"),
            evidence_policy=params.get('evidence_policy'),
            retrieval_policy=params.get('retrieval_policy'),
        )
        result = collection_envelope(rules)
        if hasattr(rules, 'regulation_evidence'):
            result['regulation_evidence'] = rules.regulation_evidence
        return result
