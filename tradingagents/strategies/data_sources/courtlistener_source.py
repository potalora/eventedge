"""CourtListener data source for federal litigation tracking.

CourtListener authenticated default limits: 5/minute, 50/hour and 125/day.
Actual account entitlement requires the official Usage API; it is not inferred here.
Used by P10 (pre-filing litigation/investigation detection).
"""
from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timezone
import os
import time
from typing import Any
from urllib.parse import urlsplit, parse_qs

from .fetch_errors import SourceFetchError, source_fetch_error, source_text, source_date, source_number

from .evidence import CoverageRecords, bounded_coverage, collection_envelope
from .request_policy import provider_request, provider_timeout, read_bounded_response, provider_budget, current_provider_deadline, provider_subbudget, provider_clock_time

logger = logging.getLogger(__name__)

BASE_URL = "https://www.courtlistener.com/api/rest/v4"
_RATE_DELAY = 0.5
# Upstream ESCursorPagination uses exact parent-docket hits below this limit;
# at/above it, `count` is an approximate distinct-docket cardinality.
_EXACT_DOCKET_COUNT_LIMIT = 10000


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
    def finite_float(value):
        parsed=float(value)
        if not math.isfinite(parsed):raise ValueError("nonfinite JSON")
        return parsed
    try:
        return json.loads(raw, object_pairs_hook=unique_object, parse_constant=invalid_constant, parse_float=finite_float)
    except (ValueError, UnicodeError, RecursionError):
        raise SourceFetchError("Provider page JSON invalid", reason_code="invalid_response") from None


class _FocusedAcquisition:
    def __init__(self, cap):
        self.cap=cap;self.requests=0;self.used_bytes=0;self.exhausted=False

    @property
    def remaining_bytes(self):
        from .courtlistener_scope import MAX_BYTES
        return MAX_BYTES-self.used_bytes

    def before_request(self):
        provider_timeout('courtlistener')
        if self.requests>=self.cap:
            self.exhausted=True
            raise SourceFetchError('Focused Court request cap exhausted',reason_code='invalid_response')

    def request(self,url,**kwargs):
        import requests
        self.before_request();self.requests+=1
        return requests.get(url,**kwargs)

    def consume(self,raw):
        self.used_bytes+=len(raw)
        if self.remaining_bytes<0:
            raise SourceFetchError('Focused Court response byte limit exhausted',reason_code='invalid_response')
        provider_timeout('courtlistener')


class CourtListenerSource:
    """Data source for federal court dockets and opinions."""

    name: str = "courtlistener"
    requires_api_key: bool = True

    def __init__(self, token: str | None = None) -> None:
        self._token = token or os.environ.get("COURTLISTENER_TOKEN", "")
        self._cache: dict[str, Any] = {}

    def fetch(self, params: dict[str, Any]) -> dict[str, Any]:
        method = params.get("method", "search_dockets")
        dispatch = {
            "search_dockets": self._dispatch_search_dockets,
            "search_opinions": self._dispatch_search_opinions,
            "focused_litigation": self._dispatch_focused_litigation,
        }
        handler = dispatch.get(method)
        if handler is None:
            return {"error": f"Unknown method '{method}'"}
        try:
            return handler(params)
        except SourceFetchError as exc:
            return {**exc.partial_data, "error": str(exc)}
        except Exception:
            logger.error("CourtListenerSource.fetch(%s) failed", method)
            return {"error": f"{method} fetch failed"}

    def is_available(self) -> bool:
        if not self._token:
            return False
        try:
            import requests  # noqa: F401
            return True
        except ImportError:
            return False

    def search_dockets(
        self,
        query: str,
        court: str | None = None,
        date_filed_after: str | None = None,
        page_size: int = 20,
        *, date_filed_before: str | None = None, _acquisition=None,
    ) -> list[dict]:
        """Search federal court dockets.

        Args:
            query: Search text (company name, case type, etc.).
            court: Court identifier (e.g. "cacd" for Central District of CA).
            date_filed_after: YYYY-MM-DD filter.
            page_size: Max results.

        Returns:
            List of docket dicts.
        """
        import requests

        if current_provider_deadline("courtlistener") is None:
            with provider_budget("courtlistener", time.monotonic()+60):
                return self.search_dockets(query, court, date_filed_after, page_size,
                                           date_filed_before=date_filed_before, _acquisition=_acquisition)
        if (type(page_size) is not int or not 1 <= page_size <= 250
                or any(value is not None and (not source_date(value) or len(value) != 10)
                       for value in (date_filed_after, date_filed_before))
                or (date_filed_after and date_filed_before and date_filed_after > date_filed_before)):
            raise SourceFetchError("CourtListener window invalid", reason_code="invalid_response")
        if not self._token:
            raise SourceFetchError("CourtListener access missing", reason_code="provider_error")
        params: dict[str, Any] = {
            "q": query,
            "type": "d" if _acquisition is not None else "r",  # Focused docket metadata only
            "page_size": page_size,
            "order_by": "dateFiled desc",
        }
        if court:
            params["court"] = court
        if date_filed_after:
            params["filed_after"] = date_filed_after
        if date_filed_before:
            params["filed_before"] = date_filed_before

        results, seen, visited, used_bytes = [], set(), set(), 0
        url = f"{BASE_URL}/search/"
        request_params = params
        coverage = {"mode": "exhaustive_window", "complete": False, "query": query,
                    "date_filed_after": date_filed_after, "date_filed_before": date_filed_before, "pages": 0}
        try:
            for page in range(1, 1001):
                response = None
                try:
                    if _acquisition is not None: _acquisition.before_request()
                    response = provider_request("courtlistener", "GET", url, params=request_params,
                        headers={"Authorization": f"Token {self._token}"},
                        timeout=provider_timeout("courtlistener"), stream=True, allow_redirects=False,
                        **({"transport":_acquisition.request,"before_attempt":_acquisition.before_request}
                           if _acquisition is not None else {}))
                    if response.status_code != 200:
                        raise SourceFetchError("CourtListener request failed", reason_code="http_error", http_status=response.status_code)
                    raw = read_bounded_response(response, provider="courtlistener", max_bytes=min(32*1024*1024-used_bytes,
                        _acquisition.remaining_bytes if _acquisition is not None else 32*1024*1024))
                    if _acquisition is not None: _acquisition.consume(raw)
                    used_bytes += len(raw)
                    data = _page_json(raw)
                    provider_timeout("courtlistener")
                finally:
                    if response is not None:
                        response.close()
                coverage["pages"] = page
                if (not isinstance(data, dict) or not isinstance(data.get("results"), list)
                        or type(data.get("count")) is not int or data["count"] < 0
                        or "next" not in data or (data["next"] is not None and not source_text(data["next"]))):
                    raise SourceFetchError("CourtListener response invalid", reason_code="invalid_response")
                for item in data["results"]:
                    if (not isinstance(item, dict) or not source_text(item.get("caseName"))
                            or type(item.get("docket_id")) is not int or item["docket_id"] <= 0
                            or not source_date(item.get("dateFiled")) or not source_text(item.get("court"))
                            or (date_filed_after and item["dateFiled"][:10] < date_filed_after)
                            or (date_filed_before and item["dateFiled"][:10] > date_filed_before)
                            or item["docket_id"] in seen):
                        raise SourceFetchError("CourtListener docket invalid", reason_code="invalid_response")
                    seen.add(item["docket_id"])
                    if len(seen) > 20000:
                        raise SourceFetchError("CourtListener row limit reached", reason_code="invalid_response")
                    results.append({"docket_id": item["docket_id"], "case_name": item["caseName"],
                        "court": item["court"], "date_filed": item["dateFiled"],
                        "date_terminated": item.get("dateTerminated"), "cause": item.get("cause", ""),
                        "nature_of_suit": item.get("suitNature", ""), "jury_demand": item.get("juryDemand", ""),
                        **({"native_record":item,"content_kind":"docket_metadata_only"} if _acquisition is not None else {})})
                coverage["total"] = data.get("count")
                if data["next"] is None:
                    # A genuinely complete population below the provider's hit
                    # threshold must have an exact count. Do not select this
                    # branch using the reported estimate, which may undercount.
                    if len(seen) < _EXACT_DOCKET_COUNT_LIMIT:
                        if data["count"] != len(seen):
                            raise SourceFetchError("CourtListener terminal count inconsistent", reason_code="invalid_response")
                        coverage["count_validation"] = "exact_population_match"
                    else:
                        coverage["count_validation"] = "approximate_not_compared"
                    coverage.update(complete=True, has_next=False, termination="null_next")
                    provider_timeout("courtlistener")
                    return CoverageRecords(results, coverage=coverage)
                nxt = data["next"]
                parsed, expected = urlsplit(nxt), urlsplit(f"{BASE_URL}/search/")
                query_params = parse_qs(parsed.query, keep_blank_values=True)
                if (parsed.scheme != expected.scheme or parsed.netloc != expected.netloc
                        or parsed.path != expected.path or parsed.fragment
                        or any(query_params.get(k) != [str(v)] for k, v in params.items())
                        or any(k not in {*params, "cursor", "page"} for k in query_params)
                        or not any(query_params.get(k) for k in ("cursor", "page"))
                        or nxt in visited or not data["results"]):
                    raise SourceFetchError("CourtListener pagination invalid", reason_code="invalid_response")
                visited.add(nxt)
                url, request_params = nxt, None
            raise SourceFetchError("CourtListener page limit reached", reason_code="invalid_response")
        except Exception as exc:
            error = source_fetch_error("CourtListener search_dockets failed", exc)
            error.partial_data = {"dockets": results, "coverage": {**coverage, "complete": False}}
            raise error from None

    def fetch_focused_litigation(self, scope, *, date_filed_after, date_filed_before,
            absolute_deadline, request_cap=10, subbudget_seconds=120,
            prior_evidence=None, max_evidence_age_seconds=3600):
        """Complete only the caller's declared issuer/case metadata scope.

        Completeness describes these exact searches and case lookups, never
        all litigation or substantive legal-document analysis. Prior evidence
        must already be source-bound by the caller's immutable store.
        """
        from .courtlistener_scope import (POLICY, CONTENT_KIND, canonical_scope,
            declared_queries, digest, evidence_digest, reusable_evidence, validate_window, validate_focused_litigation)
        selected=canonical_scope(scope);validate_window(date_filed_after,date_filed_before)
        if (type(request_cap) is not int or not 1<=request_cap<=50
                or type(subbudget_seconds) not in (int,float) or not math.isfinite(subbudget_seconds) or not 0<subbudget_seconds<=120
                or type(absolute_deadline) not in (int,float) or not math.isfinite(absolute_deadline)
                or type(max_evidence_age_seconds) not in (int,float) or not math.isfinite(max_evidence_age_seconds) or not 0<=max_evidence_age_seconds<=86400):
            raise ValueError('invalid_focused_court_budget')
        if current_provider_deadline('courtlistener') is None:
            with provider_budget('courtlistener',absolute_deadline):
                return self.fetch_focused_litigation(selected,date_filed_after=date_filed_after,
                    date_filed_before=date_filed_before,absolute_deadline=absolute_deadline,
                    request_cap=request_cap,subbudget_seconds=subbudget_seconds,
                    prior_evidence=prior_evidence,max_evidence_age_seconds=max_evidence_age_seconds)
        parent=current_provider_deadline('courtlistener')
        allowance=min(subbudget_seconds,min(parent,absolute_deadline)-provider_clock_time('courtlistener'))
        queries=[{**row,'status':'not_attempted','returned':0} for row in declared_queries(selected)]
        coverage={'policy':POLICY,'mode':'declared_focused_scope','complete':False,
            'scope':selected,'scope_sha256':digest(selected),'date_filed_after':date_filed_after,
            'date_filed_before':date_filed_before,'queries':queries,'requests':0,'request_cap':request_cap,
            'budget_exhausted':False,'request_cap_exhausted':False,'returned':0,
            'content_kind':CONTENT_KIND,'acquisition_started_at':datetime.now(timezone.utc).isoformat(),
            'acquired_at':None,'absolute_deadline':min(parent,absolute_deadline),
            'subbudget_seconds':subbudget_seconds,'prior_reuse_status':'not_supplied'}
        result={'dockets':[],'coverage':coverage}
        def acquire():
            acquisition=_FocusedAcquisition(request_cap)
            coverage['absolute_deadline']=current_provider_deadline('courtlistener')
            prior,status=reusable_evidence(prior_evidence,selected,date_filed_after,date_filed_before,
                datetime.now(timezone.utc),max_evidence_age_seconds)
            coverage['prior_reuse_status']=status
            if prior is not None:
                provider_timeout('courtlistener')
                prior['reuse']={'status':status,'checked_at':datetime.now(timezone.utc).isoformat(),
                    'requests':0,'original_acquired_at':prior['coverage']['acquired_at']}
                return prior
            seen={}
            for query in queries:
                try:
                    if acquisition.requests>=request_cap:
                        acquisition.exhausted=True
                        break
                    acquisition.before_request()
                    if query['kind']=='issuer_search':
                        rows=self.search_dockets(query['query'],date_filed_after=date_filed_after,
                            date_filed_before=date_filed_before,_acquisition=acquisition)
                        query['coverage']=rows.coverage
                    else:
                        rows=[self._focused_known_case(query['docket_id'],acquisition)]
                        query['coverage']={'complete':True,'termination':'exact_native_docket_id','pages':1}
                    for row in rows:
                        key=row['docket_id'];existing=seen.get(key)
                        if existing is not None:
                            if any(existing.get(k)!=row.get(k) for k in ('case_name','court','date_filed','cause','nature_of_suit')):
                                raise SourceFetchError('Focused Court docket conflict',reason_code='invalid_response')
                            existing.setdefault('additional_native_records',[]).append(row['native_record'])
                        else:
                            seen[key]=row;result['dockets'].append(row)
                    query.update(status='complete',returned=len(rows),docket_ids=[row['docket_id'] for row in rows])
                    provider_timeout('courtlistener')
                except Exception as error:
                    safe=source_fetch_error('Focused Court query failed',error)
                    query.update(status='failed',reason_code=safe.reason_code,http_status=safe.http_status,
                        coverage={**safe.partial_data.get('coverage',{}),'complete':False})
                    # Preserve valid partial rows; they do not become complete scope evidence.
                    for row in safe.partial_data.get('dockets',[]):
                        if row['docket_id'] not in seen:
                            seen[row['docket_id']]=row;result['dockets'].append(row)
                    coverage['budget_exhausted'] |= safe.reason_code=='timeout'
                    if acquisition.exhausted or coverage['budget_exhausted']:break
            coverage.update(requests=acquisition.requests,request_cap_exhausted=acquisition.exhausted,
                returned=len(result['dockets']),acquired_at=datetime.now(timezone.utc).isoformat(),
                response_bytes=acquisition.used_bytes,
                complete=all(row['status']=='complete' for row in queries))
            try:provider_timeout('courtlistener')
            except SourceFetchError:coverage.update(complete=False,budget_exhausted=True)
            if not coverage['complete']:result['error']='Focused Court declared scope incomplete'
            coverage['evidence_sha256']=evidence_digest(result)
            if coverage['complete']:
                try:
                    validate_focused_litigation(result,expected_scope=selected,
                        date_filed_after=date_filed_after,date_filed_before=date_filed_before,
                        max_evidence_age_seconds=None)
                except ValueError:
                    coverage.update(complete=False,validation_error='invalid_response')
                    result['error']='Focused Court declared scope incomplete'
                    coverage['evidence_sha256']=evidence_digest(result)
            try:provider_timeout('courtlistener')
            except SourceFetchError:
                coverage.update(complete=False,budget_exhausted=True)
                result['error']='Focused Court declared scope incomplete'
                coverage['evidence_sha256']=evidence_digest(result)
            return result
        if allowance<=0:
            coverage.update(budget_exhausted=True,acquired_at=datetime.now(timezone.utc).isoformat())
            result['error']='Focused Court declared scope incomplete'
            coverage['evidence_sha256']=evidence_digest(result)
            return result
        # All query errors are collected inside acquire; retries never restart
        # the entire population. Nested HTTP calls keep the parent's retry policy.
        with provider_subbudget('courtlistener',maximum_seconds=allowance,absolute_deadline=absolute_deadline):
            return acquire()

    def _focused_known_case(self, docket_id, acquisition):
        if not self._token:
            raise SourceFetchError('CourtListener access missing',reason_code='provider_error')
        response=None
        try:
            acquisition.before_request()
            response=provider_request('courtlistener','GET',f'{BASE_URL}/dockets/{docket_id}/',
                transport=acquisition.request,before_attempt=acquisition.before_request,
                headers={'Authorization':f'Token {self._token}'},
                timeout=provider_timeout('courtlistener'),stream=True,allow_redirects=False)
            if response.status_code!=200:
                raise SourceFetchError('Focused Court docket request failed',reason_code='http_error',http_status=response.status_code)
            raw=read_bounded_response(response,provider='courtlistener',max_bytes=acquisition.remaining_bytes)
            acquisition.consume(raw);item=_page_json(raw)
            if (not isinstance(item,dict) or type(item.get('id')) is not int or item['id']!=docket_id
                    or not source_text(item.get('case_name')) or not source_text(item.get('court_id'))
                    or not source_date(item.get('date_filed'))):
                raise SourceFetchError('Focused Court docket identity invalid',reason_code='invalid_response')
            row={'docket_id':item['id'],'case_name':item['case_name'],'court':item['court_id'],
                'date_filed':item['date_filed'],'date_terminated':item.get('date_terminated'),
                'cause':item.get('cause',''),'nature_of_suit':item.get('nature_of_suit',''),
                'jury_demand':item.get('jury_demand',''),'native_record':item,
                'content_kind':'docket_metadata_only'}
            provider_timeout('courtlistener')
            return row
        finally:
            if response is not None:response.close()

    def _dispatch_focused_litigation(self, params):
        return self.fetch_focused_litigation(params['scope'],
            date_filed_after=params['date_filed_after'],date_filed_before=params['date_filed_before'],
            absolute_deadline=params['absolute_deadline'],request_cap=params.get('request_cap',10),
            subbudget_seconds=params.get('subbudget_seconds',120),prior_evidence=params.get('prior_evidence'),
            max_evidence_age_seconds=params.get('max_evidence_age_seconds',3600))

    def search_opinions(
        self,
        query: str,
        date_filed_after: str | None = None,
        page_size: int = 20,
        *, date_filed_before: str | None = None,
    ) -> list[dict]:
        """Search opinion clusters, retaining their distinct nested opinion references."""
        import requests

        if not self._token:
            raise SourceFetchError("CourtListener access missing", reason_code="provider_error")
        params: dict[str, Any] = {
            "q": query,
            "type": "o",  # opinions
            "page_size": page_size,
            "order_by": "dateFiled desc",
        }
        if date_filed_after:
            params["filed_after"] = date_filed_after
        if date_filed_before:
            params["filed_before"] = date_filed_before

        try:
            resp = provider_request("courtlistener", "GET",
                f"{BASE_URL}/search/",
                params=params,
                headers={"Authorization": f"Token {self._token}"},
                timeout=15,
            )
            if resp.status_code != 200:
                logger.warning("CourtListener opinions returned %d", resp.status_code)
                raise SourceFetchError("CourtListener request failed", reason_code="http_error", http_status=resp.status_code)

            data = resp.json()
            if not isinstance(data, dict) or not isinstance(data.get("results"), list):
                raise SourceFetchError("CourtListener response invalid", reason_code="invalid_response")
            results = []
            for item in data["results"]:
                if (not isinstance(item, dict) or not source_text(item.get("caseName"))
                        or type(item.get("cluster_id")) is not int or item["cluster_id"] <= 0
                        or not source_date(item.get("dateFiled"))
                        or not isinstance(item.get("opinions"), list) or not item["opinions"]
                        or not all(isinstance(opinion, dict) and type(opinion.get("id")) is int
                                   and opinion["id"] > 0 for opinion in item["opinions"])
                        or len({opinion["id"] for opinion in item["opinions"]}) != len(item["opinions"])):
                    raise SourceFetchError("CourtListener opinion record invalid", reason_code="invalid_response",
                                           partial_data={"opinions": results})
                results.append({
                    "cluster_id": item["cluster_id"],
                    "docket_id": item.get("docket_id"),
                    "opinion_ids": [opinion["id"] for opinion in item["opinions"]],
                    "opinions": [{"opinion_id": opinion["id"], "type": opinion.get("type", ""),
                                  "snippet": opinion.get("snippet", ""),
                                  "download_url": opinion.get("download_url"),
                                  "local_path": opinion.get("local_path"),
                                  "author_id": opinion.get("author_id")}
                                 for opinion in item["opinions"]],
                    "case_name": item.get("caseName", ""),
                    "date_filed": item.get("dateFiled", ""),
                    "court": item.get("court", ""),
                    "type": item.get("type", ""),
                })
            return CoverageRecords(results, coverage=bounded_coverage(returned=len(results), limit=page_size,
                total=data.get('count'), has_next=bool(data['next']) if 'next' in data else None,
                query=query, date_filed_after=date_filed_after, date_filed_before=date_filed_before,
                unit='opinion_clusters'))
        except Exception as exc:
            safe_error = source_fetch_error("CourtListener search_opinions failed", exc)
            logger.error("%s", safe_error)
            raise safe_error from None

    def clear_cache(self) -> None:
        self._cache.clear()

    def _dispatch_search_dockets(self, params: dict[str, Any]) -> dict[str, Any]:
        return collection_envelope(self.search_dockets(
            query=params.get("query", ""),
            court=params.get("court"),
            date_filed_after=params.get("date_filed_after"),
            page_size=params.get("page_size",20),
            date_filed_before=params.get("date_filed_before"),
        ))

    def _dispatch_search_opinions(self, params: dict[str, Any]) -> dict[str, Any]:
        return collection_envelope(self.search_opinions(
            query=params.get("query", ""),
            date_filed_after=params.get("date_filed_after"),
            page_size=params.get("page_size",20),
            date_filed_before=params.get("date_filed_before"),
        ))
