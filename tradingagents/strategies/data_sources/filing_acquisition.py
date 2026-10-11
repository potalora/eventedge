"""Bounded acquisition of an exact complete SEC submission.

This boundary retains all selected evidence and structural issues. It does not
assign a trading issuer, certify model adequacy, or choose a prior comparator.
"""
from __future__ import annotations

from datetime import datetime, timezone
import re
import time
from urllib.parse import urlsplit

from .fetch_errors import SourceFetchError
from .filing_evidence import EvidenceError, build_evidence, parse_submission
from .filing_spool import current_submission_spool, spool_response
from .request_policy import (
    current_provider_deadline, provider_budget, provider_request, provider_timeout,
    read_bounded_response,
)


def completed_submission(record):
    """Observe a closed original before any framing, selection, or parsing."""
    from .filing_spool import completed_submission as observe
    observe(record)


def _acquire_spooled(response, target, *, accession, form_type, filing_date, required_exhibits):
    record = spool_response(response, identity=dict(accession=accession, form=form_type,
        filing_date=filing_date, source_url=target))
    try:
        completed_submission(record)
        provider_timeout('edgar')
        from .filing_parser_dispatch import current_dispatcher, dispatch_evidence
        if current_dispatcher() is None:
            raise SourceFetchError('SEC parser scope is required', reason_code='provider_error')
        result = dispatch_evidence(record, expected_accession=accession, expected_form=form_type,
            expected_date=filing_date, observed_at=record.observed_at,
            max_submission_bytes=512 * 1024 * 1024, required_exhibits=required_exhibits)
        provider_timeout('edgar')
        result['source_url'] = target
        return result
    finally:
        record.owner.discard(record)


def complete_submission_url(url: str, accession: str) -> str:
    """Bind an index/submission location to an exact accession, never guess its CIK."""
    if (not isinstance(accession, str)
            or not re.fullmatch(r'[0-9]{10}-[0-9]{2}-[0-9]{6}', accession)
            or not isinstance(url, str) or len(url) > 2048):
        raise SourceFetchError('Invalid SEC submission identity', reason_code='invalid_response')
    try:
        parsed = urlsplit(url)
    except ValueError:
        raise SourceFetchError('Invalid SEC submission location', reason_code='invalid_response') from None
    path = re.fullmatch(
        r'(/Archives/edgar/data/[1-9][0-9]{0,9}/' + re.escape(accession.replace('-', ''))
        + r')/' + re.escape(accession) + r'(?:-index\.html?|\.txt)', parsed.path)
    if (parsed.scheme != 'https' or parsed.netloc != 'www.sec.gov' or parsed.query
            or parsed.fragment or not path or url != parsed.geturl()):
        raise SourceFetchError('Invalid SEC submission location', reason_code='invalid_response')
    return 'https://www.sec.gov' + path[1] + '/' + accession + '.txt'


def acquire_complete_submission(user_agent: str, url: str, *, accession: str,
                                form_type: str, filing_date: str,
                                required_exhibits=(),
                                max_submission_bytes=64 * 1024 * 1024) -> dict:
    """Fetch once, validate full framing, and reject any late parse/acceptance.

    Retries use the inherited provider policy. The native supervisor supplies
    hard process closure; socket and parser deadlines are cooperative here.
    """
    target = complete_submission_url(url, accession)
    if type(max_submission_bytes) is not int or not 1 <= max_submission_bytes <= 64 * 1024 * 1024:
        raise ValueError('Invalid SEC submission byte limit')
    if current_provider_deadline('edgar') is None:
        owner = current_submission_spool()
        deadline = owner.original_deadline if owner is not None else time.monotonic() + 60
        with provider_budget('edgar', deadline):
            return acquire_complete_submission(
                user_agent, url, accession=accession, form_type=form_type,
                filing_date=filing_date, required_exhibits=required_exhibits,
                max_submission_bytes=max_submission_bytes)
    provider_timeout('edgar')
    response = provider_request(
        'edgar', 'GET', target, headers={'User-Agent': user_agent}, timeout=30,
        operation='complete_submission', stream=True, allow_redirects=False)
    try:
        provider_timeout('edgar')
        if response.status_code != 200 or response.url != target:
            raise SourceFetchError('Unexpected SEC submission response', reason_code='invalid_response')
        if current_submission_spool() is not None:
            # spool_response owns response closure, including all read failures.
            spooled_response, response = response, None
            return _acquire_spooled(spooled_response, target, accession=accession,
                form_type=form_type, filing_date=filing_date, required_exhibits=required_exhibits)
        raw = read_bounded_response(response, provider='edgar', max_bytes=max_submission_bytes)
        observed_at = datetime.now(timezone.utc).isoformat()
    finally:
        if response is not None:
            response.close()
    provider_timeout('edgar')
    try:
        from .filing_parser_dispatch import current_dispatcher, dispatch_evidence
        if current_dispatcher() is not None:
            result = dispatch_evidence(
                raw, expected_accession=accession, expected_form=form_type,
                expected_date=filing_date, observed_at=observed_at,
                max_submission_bytes=max_submission_bytes,
                required_exhibits=required_exhibits)
        else:
            parsed = parse_submission(
                raw, expected_accession=accession, expected_form=form_type,
                expected_date=filing_date, observed_at=observed_at,
                max_submission_bytes=max_submission_bytes)
            provider_timeout('edgar')
            result = build_evidence(parsed, required_exhibits=required_exhibits)
    except EvidenceError:
        raise SourceFetchError('SEC submission evidence invalid', reason_code='invalid_response') from None
    provider_timeout('edgar')
    result['source_url'] = target
    return result
