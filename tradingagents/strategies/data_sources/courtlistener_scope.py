"""Pure identities and evidence checks for the declared focused Court scope.

Issuer verification references are supplied by the source-bound caller; this
module validates their shape, not the truth of an arbitrary caller assertion.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
import hashlib
import json
import math
import re

POLICY = 'focused_litigation_v1'
CONTENT_KIND = 'docket_metadata_only'
MAX_BYTES = 8 * 1024 * 1024
ROLES = frozenset({'held', 'pending', 'shortlist', 'watchlist'})


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
        ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def canonical_scope(scope):
    if (not isinstance(scope, dict) or set(scope) != {'policy','issuers','case_ids'}
            or scope['policy'] != POLICY or not isinstance(scope['issuers'], list)
            or not isinstance(scope['case_ids'], list) or len(scope['issuers']) > 250
            or len(scope['case_ids']) > 250):
        raise ValueError('invalid_focused_court_scope')
    issuers = {}; tickers = {}
    for row in scope['issuers']:
        if not isinstance(row, dict) or set(row) != {'ticker','issuer_cik','legal_name','verification','roles'}:
            raise ValueError('invalid_focused_court_issuer')
        ticker, cik, name, verification, roles = (row[k] for k in ('ticker','issuer_cik','legal_name','verification','roles'))
        if (not isinstance(ticker,str) or not re.fullmatch(r'[A-Z0-9][A-Z0-9.\-]{0,31}',ticker)
                or not isinstance(cik,str) or not re.fullmatch(r'[0-9]{10}',cik) or int(cik)==0
                or not isinstance(name,str) or not name.strip() or len(name)>512
                or name != name.strip() or any(ord(c)<32 for c in name)
                or not isinstance(verification,dict) or set(verification) != {'source','sha256'}
                or verification['source'] not in ('sec_company_map','sec_submission_header')
                or not isinstance(verification['sha256'],str) or not re.fullmatch(r'[0-9a-f]{64}',verification['sha256'])
                or not isinstance(roles,list) or not roles or any(not isinstance(x,str) or x not in ROLES for x in roles)):
            raise ValueError('invalid_focused_court_issuer')
        key = (ticker,cik,name,verification['source'],verification['sha256'])
        if ticker in tickers and tickers[ticker] != key:
            raise ValueError('conflicting_focused_court_issuer')
        tickers[ticker] = key
        if key not in issuers:
            issuers[key] = {**row,'verification':dict(verification),'roles':set()}
        issuers[key]['roles'].update(roles)
    cases = scope['case_ids']
    if any(type(value) is not int or value <= 0 for value in cases):
        raise ValueError('invalid_focused_court_case_id')
    normalized = []
    for key in sorted(issuers):
        row=issuers[key];normalized.append({**row,'roles':sorted(row['roles'])})
    return {'policy':POLICY,'issuers':normalized,'case_ids':sorted(set(cases))}


def declared_queries(scope):
    groups = {}
    for row in scope['issuers']:
        groups.setdefault(row['legal_name'],[]).append(row)
    result=[]
    for name,rows in sorted(groups.items()):
        escaped=name.replace('\\','\\\\').replace('"','\\"')
        result.append({'kind':'issuer_search','query':f'caseName:"{escaped}"',
            'issuer_ciks':sorted({r['issuer_cik'] for r in rows}), 'tickers':sorted(r['ticker'] for r in rows)})
    result.extend({'kind':'known_case','docket_id':value} for value in scope['case_ids'])
    return result


def validate_window(start,end):
    try:
        if not isinstance(start,str) or not isinstance(end,str) or date.fromisoformat(start).isoformat()!=start or date.fromisoformat(end).isoformat()!=end or start>end:
            raise ValueError
    except (TypeError,ValueError):
        raise ValueError('invalid_focused_court_window') from None


def evidence_digest(evidence):
    coverage={k:v for k,v in evidence['coverage'].items() if k!='evidence_sha256'}
    return digest({'dockets':evidence['dockets'],'coverage':coverage})


def _aware(value):
    if not isinstance(value,str):raise ValueError('invalid_focused_court_clock')
    parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
    if parsed.tzinfo is None or parsed.utcoffset() is None:raise ValueError('invalid_focused_court_clock')
    return parsed


def validate_focused_litigation(payload, *, expected_scope, date_filed_after,
        date_filed_before, now=None, max_evidence_age_seconds=3600):
    """Validate complete exact-scope metadata; never assert global litigation coverage."""
    selected=canonical_scope(expected_scope);validate_window(date_filed_after,date_filed_before)
    check_now=now if now is not None else (datetime.now(timezone.utc) if max_evidence_age_seconds is not None else None)
    if ((check_now is not None and (not isinstance(check_now,datetime) or check_now.tzinfo is None or check_now.utcoffset() is None))
            or (max_evidence_age_seconds is not None and (type(max_evidence_age_seconds) not in (int,float) or not math.isfinite(max_evidence_age_seconds)
            or not 0<=max_evidence_age_seconds<=86400))):
        raise ValueError('invalid_focused_court_validation_clock')
    try:
        if not isinstance(payload,dict) or set(payload)-{'dockets','coverage','reuse','_request_diagnostics','courtlistener_scope_policy'}:
            raise ValueError('invalid_focused_court_evidence')
        if payload.get('courtlistener_scope_policy',POLICY)!=POLICY:raise ValueError('invalid_focused_court_policy')
        rows=payload['dockets'];c=payload['coverage']
        if (not isinstance(rows,list) or not isinstance(c,dict) or c.get('policy')!=POLICY
                or c.get('mode')!='declared_focused_scope' or c.get('content_kind')!=CONTENT_KIND
                or c.get('scope')!=selected or c.get('scope_sha256')!=digest(selected)
                or c.get('date_filed_after')!=date_filed_after or c.get('date_filed_before')!=date_filed_before
                or c.get('complete') is not True or c.get('budget_exhausted') is not False
                or c.get('request_cap_exhausted') is not False or type(c.get('returned')) is not int
                or c['returned']!=len(rows) or c.get('evidence_sha256')!=evidence_digest(payload)):
            raise ValueError('invalid_focused_court_evidence')
        started=_aware(c['acquisition_started_at']);observed=_aware(c['acquired_at'])
        if started>observed:raise ValueError('invalid_focused_court_clock')
        if check_now is not None:
            age=(check_now-observed).total_seconds()
            if age<0:raise ValueError('future_focused_court_evidence')
            if max_evidence_age_seconds is not None and age>max_evidence_age_seconds:raise ValueError('stale_focused_court_evidence')
        if (type(c.get('requests')) is not int or type(c.get('request_cap')) is not int
                or not 0<=c['requests']<=c['request_cap']<=50 or c['request_cap']<1
                or type(c.get('response_bytes')) is not int or not 0<=c['response_bytes']<=MAX_BYTES
                or type(c.get('absolute_deadline')) not in (int,float) or not math.isfinite(c['absolute_deadline'])
                or type(c.get('subbudget_seconds')) not in (int,float) or not math.isfinite(c['subbudget_seconds'])
                or not 0<c['subbudget_seconds']<=120):
            raise ValueError('invalid_focused_court_accounting')
        expected=declared_queries(selected);queries=c['queries'];all_ids=set();search_ids=set();pages=0
        if not isinstance(queries,list) or len(queries)!=len(expected):raise ValueError('invalid_focused_court_queries')
        for query,want in zip(queries,expected):
            if (not isinstance(query,dict) or any(query.get(k)!=v for k,v in want.items())
                    or query.get('status')!='complete' or not isinstance(query.get('coverage'),dict)
                    or query['coverage'].get('complete') is not True or not isinstance(query.get('docket_ids'),list)
                    or any(type(i) is not int or i<=0 for i in query['docket_ids'])
                    or len(set(query['docket_ids']))!=len(query['docket_ids'])
                    or type(query.get('returned')) is not int or query['returned']!=len(query['docket_ids'])):
                raise ValueError('invalid_focused_court_queries')
            native=query['coverage']
            if type(native.get('pages')) is not int or native['pages']<1:raise ValueError('invalid_focused_court_queries')
            pages+=native['pages'];all_ids.update(query['docket_ids'])
            if want['kind']=='known_case':
                if query['docket_ids']!=[want['docket_id']] or native.get('termination')!='exact_native_docket_id':
                    raise ValueError('invalid_focused_court_queries')
            else:
                if (native.get('mode')!='exhaustive_window' or native.get('query')!=want['query']
                        or native.get('date_filed_after')!=date_filed_after or native.get('date_filed_before')!=date_filed_before
                        or native.get('termination')!='null_next' or native.get('has_next') is not False):
                    raise ValueError('invalid_focused_court_queries')
                search_ids.update(query['docket_ids'])
        if pages>c['requests'] or (not expected and c['requests']!=0):raise ValueError('invalid_focused_court_accounting')
        seen=set()
        def check_native(row,native):
            if not isinstance(native,dict):raise ValueError('invalid_focused_court_native_identity')
            search='docket_id' in native
            mapping={'docket_id':'docket_id' if search else 'id','case_name':'caseName' if search else 'case_name',
                'court':'court' if search else 'court_id','date_filed':'dateFiled' if search else 'date_filed'}
            if type(native.get(mapping['docket_id'])) is not int:
                raise ValueError('invalid_focused_court_native_identity')
            for key,field in [('cause','cause'),('nature_of_suit','suitNature' if search else 'nature_of_suit'),
                    ('jury_demand','juryDemand' if search else 'jury_demand'),
                    ('date_terminated','dateTerminated' if search else 'date_terminated')]:
                value=native.get(field,None if key=='date_terminated' else '')
                if (value is not None and not isinstance(value,str)) or row.get(key)!=value:
                    raise ValueError('invalid_focused_court_native_metadata')
            if any(native.get(field)!=row[key] for key,field in mapping.items()):
                raise ValueError('invalid_focused_court_native_identity')
        for row in rows:
            if (not isinstance(row,dict) or type(row.get('docket_id')) is not int or row['docket_id']<=0
                    or row['docket_id'] in seen or row.get('content_kind')!=CONTENT_KIND
                    or any(not isinstance(row.get(k),str) or not row[k].strip() for k in ('case_name','court','date_filed'))):
                raise ValueError('invalid_focused_court_native_identity')
            filed=date.fromisoformat(row['date_filed'][:10]).isoformat()
            if row['docket_id'] in search_ids and not date_filed_after<=filed<=date_filed_before:
                raise ValueError('invalid_focused_court_native_window')
            check_native(row,row['native_record'])
            additional=row.get('additional_native_records',[])
            if not isinstance(additional,list):raise ValueError('invalid_focused_court_native_identity')
            for native in additional:check_native(row,native)
            seen.add(row['docket_id'])
        if seen!=all_ids:raise ValueError('invalid_focused_court_query_population')
        return payload
    except (KeyError,TypeError,OverflowError) as error:
        raise ValueError('invalid_focused_court_evidence') from None


def reusable_evidence(prior, scope, start, end, now, maximum_age):
    if prior is None:return None,'not_supplied'
    try:
        validate_focused_litigation(prior,expected_scope=scope,date_filed_after=start,
            date_filed_before=end,now=now,max_evidence_age_seconds=maximum_age)
        return json.loads(json.dumps({'dockets':prior['dockets'],'coverage':prior['coverage']},allow_nan=False)),'exact_prior_evidence'
    except (TypeError,ValueError) as error:
        code=str(error)
        return None,('stale' if code=='stale_focused_court_evidence' else 'future' if code=='future_focused_court_evidence' else 'invalid')
