"""Owner-thread planning of bounded ownership acquisitions, with shared memos.

The ordinary hydration executor owns workers, inherited deadlines and receipt
acceptance. This adapter neither starts pools nor guesses an archive CIK from an
accession number. External SEC URLs can locate additional history domains; an
unknown narrative reference remains an explicit unclosed obligation.
"""
from __future__ import annotations

from functools import partial
import re
from urllib.parse import urlsplit

from .filing_ownership_lineage import ownership_lineage
from .edgar_source import normalize_filing_form
from .request_policy import provider_timeout

ALL_EXHIBITS = '__all_native_exhibits__'
MAX_ROUNDS = 258  # At most the existing finite edge bound plus initial metadata.


def _history_rows(histories, archives):
    for cik, history in sorted(histories.items()):
        if 'failure' not in history:
            for row in history.get('filings', []):
                yield cik, row
    for (cik, _), archive in sorted(archives.items()):
        if 'failure' not in archive:
            for row in archive.get('filings', []):
                yield cik, row


def _metadata(accession, histories, archives):
    matches = [(cik, row) for cik, row in _history_rows(histories, archives)
               if row.get('accession_number') == accession]
    if not matches:
        return None
    rows = [row for _, row in matches]
    if any(row != rows[0] for row in rows):
        raise ValueError('conflicting_ownership_target_metadata')
    return matches[0]


def _reference_ciks(obligation):
    """Only a native exact SEC URL supplies a new directory/history CIK."""
    accession = obligation.get('accession')
    if not isinstance(accession, str):
        return []
    results = set()
    for url in re.findall(r'https://www\.sec\.gov/Archives/edgar/data/[^\s<>"\']+', obligation.get('quote', '')):
        parsed = urlsplit(url.rstrip('.,);'))
        match = re.fullmatch(r'/Archives/edgar/data/([1-9][0-9]{0,9})/' + accession.replace('-', '')
            + r'/[A-Za-z0-9][A-Za-z0-9_.-]{0,254}', parsed.path)
        if match and not parsed.query and not parsed.fragment:
            results.add(match[1].zfill(10))
    return sorted(results)


def hydrate_ownership_dependencies(source, current_accessions, *, corpus, histories, archives,
        specs, failures, facts, lineages, run_tasks, accept, body_task, max_workers,
        account_metadata, acquisition_failures):
    """Close actionable duties, reusing all once-acquired bodies and histories.

    Mutations run only in the calling hydration owner. New requests use the same
    bounded executor and source methods. Existing bodies are never re-fetched
    to obtain a later-discovered exhibit: without retained-original reparse
    authority, that obligation must remain a failure.
    """
    attempted = set()
    attempted_bodies = set()
    current_accessions = sorted(current_accessions)
    latest = {}
    for _ in range(MAX_ROUNDS):
        provider_timeout('edgar')
        tasks = {}
        for accession in current_accessions:
            current = corpus.get(accession)
            if current is None or 'material_quarantine' in current:
                continue
            receipt = ownership_lineage(current, facts, corpus, histories,
                {cik + '/' + name: value for (cik, name), value in archives.items()},
                check=lambda: provider_timeout('edgar'))
            latest[accession] = receipt
            if receipt['status'] != 'acquisition_required':
                continue
            for obligation in receipt['obligations']:
                kind = obligation['kind']
                if kind == 'submission_history':
                    cik = obligation['cik']
                    key = ('history', cik)
                    if cik not in histories:
                        tasks[key] = (key, partial(source.get_company_submission_history, cik))
                elif kind == 'submission_archive':
                    cik, descriptor = obligation['cik'], obligation['descriptor']
                    key = ('archive', (cik, descriptor['name']))
                    if key[1] not in archives:
                        tasks[key] = (key, partial(source.get_company_submission_archive, cik, descriptor))
                elif kind in {'complete_submission', 'incorporated_submission', 'incorporated_exhibit'}:
                    target = obligation['accession']
                    if target in corpus or target in failures:
                        continue
                    try:
                        metadata = _metadata(target, histories, archives)
                    except ValueError:
                        failures[target] = {'code': 'conflicting_ownership_target_metadata', 'reason_code': 'invalid_response'}
                        continue
                    if target not in specs and metadata is None:
                        # A referenced exact URL may identify another issuer's
                        # index. Its history provides form/date; accession
                        # numbering never supplies those facts.
                        locator_ciks = set(_reference_ciks(obligation))
                        if kind.startswith('incorporated_'):
                            subject = facts.get(accession, {}).get('subject_cik')
                            if subject:
                                locator_ciks.add(subject)
                        for cik in sorted(locator_ciks):
                            if cik not in histories:
                                key = ('history', cik)
                                tasks[key] = (key, partial(source.get_company_submission_history, cik))
                            elif 'failure' not in histories[cik]:
                                for descriptor in histories[cik].get('archives', []):
                                    if descriptor['filingFrom'] > current['filing_date']:
                                        continue
                                    key = ('archive', (cik, descriptor['name']))
                                    if key[1] not in archives:
                                        tasks[key] = (key, partial(source.get_company_submission_archive, cik, descriptor))
                        continue
                    if target not in specs:
                        cik, row = metadata
                        specs[target] = {'form': normalize_filing_form(row['form']), 'date': row['filing_date'],
                            'url': f'https://www.sec.gov/Archives/edgar/data/{int(cik)}/{target.replace("-", "")}/{target}-index.htm',
                            'source_ciks': {cik}, 'required_exhibits': set(), 'requires_prior': False}
                    spec = specs[target]
                    if metadata is not None:
                        _, row = metadata
                        if spec['form'] != normalize_filing_form(row['form']) or spec['date'] != row['filing_date']:
                            failures[target] = {'code': 'conflicting_ownership_target_metadata', 'reason_code': 'invalid_response'}
                            continue
                    required = set(obligation.get('required_exhibits', [])) - {target + '.txt'}
                    if kind == 'incorporated_exhibit':
                        required.add(obligation['filename'])
                    if kind == 'incorporated_submission' and not required and re.search(
                            r'agreement|contract|exhibit', obligation.get('quote', ''), re.I):
                        required.add(ALL_EXHIBITS)
                    spec['required_exhibits'].update(required)
                    key = ('body', target)
                    tasks[key] = body_task(target)
        ready = [tasks[key] for key in sorted(tasks) if key not in attempted]
        if not ready:
            break
        attempted.update(key for key, _ in ready)
        attempted_bodies.update(key[1] for key, _ in ready if key[0] == 'body')
        run_tasks(ready, max_workers, accept)
        # The caller updates facts from every body accepted by that executor.
    else:
        # Outer recursion exhaustion cannot be reclassified as ambiguity.
        for accession in latest:
            failures[accession] = {'code': 'ownership_acquisition_round_limit',
                'reason_code': 'invalid_response'}
    for target in sorted(attempted_bodies & set(failures)):
        required_by = []
        for current, receipt in sorted(latest.items()):
            obligations = [duty for duty in receipt['obligations'] if duty.get('accession') == target]
            candidate = target in receipt['candidate_accessions']
            if candidate or obligations:
                required_by.append({'current_accession': current, 'candidate': candidate,
                    'obligations': obligations})
        value = {'failure': dict(failures[target]), 'required_by': required_by}
        if account_metadata({'ownership_acquisition_failures': {target: value}}):
            acquisition_failures[target] = value
    for accession, receipt in sorted(latest.items()):
        provider_timeout('edgar')
        if account_metadata({'ownership_lineages': {accession: receipt}}):
            lineages[accession] = receipt
    return latest
