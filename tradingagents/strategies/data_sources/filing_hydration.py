"""Bounded full-filing acquisition with one corpus and explicit missing evidence.

Call once after all declared discovery scopes. This does not certify substantive
model adequacy. Active Python workers cannot be killed; the native supervisor
still owns hard process closure. Results completed after the original deadline
are never accepted, including when a blocked worker eventually returns.
"""
from __future__ import annotations

from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextvars import copy_context
from functools import partial
import hashlib
import json
import re

from .edgar_source import normalize_filing_form, filing_form_family
from .equity_universe import EquityUniverse
from .filing_comparison_policy import (CURRENT_ONLY_POLICY, NO_UNIQUE_PRIOR,
    checked_history, history_comparison, current_only_binding)
from .fetch_errors import SourceFetchError, source_date, source_fetch_error
from .request_policy import current_provider_deadline, provider_clock_time, provider_timeout

POLICY = 'complete_submission_v1'
PRIOR_POLICY = 'nearest_strictly_earlier_exact_form_v1'
_ORDINARY = frozenset({'10-K', '10-Q', '8-K', 'DEF 14A'})
_OWNERSHIP = frozenset({'SCHEDULE 13D', 'SCHEDULE 13G'})


def _failure(code, reason='invalid_response', status=None):
    result = {'code': code, 'reason_code': reason}
    if status is not None:
        result['http_status'] = status
    return result


def _checked_call(call):
    provider_timeout('edgar')
    value = call()
    provider_timeout('edgar')
    return value


def _run_tasks(tasks, max_workers, on_result):
    """At most max_workers futures; owner-only ordered memo acceptance."""
    todo, pending, failures = deque(tasks), {}, {}
    pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix='filing-hydration')
    try:
        while todo or pending:
            try:
                provider_timeout('edgar')
            except SourceFetchError:
                break
            while todo and len(pending) < max_workers:
                key, call = todo.popleft()
                context = copy_context()  # A distinct Context for every dispatch.
                pending[pool.submit(context.run, _checked_call, call)] = key
            remaining = current_provider_deadline('edgar') - provider_clock_time('edgar')
            done, _ = wait(pending, timeout=max(0, remaining), return_when=FIRST_COMPLETED)
            if not done:
                break
            for future in sorted(done, key=lambda f: pending[f]):
                key = pending.pop(future)
                try:
                    value = future.result()
                    provider_timeout('edgar')  # Worker completion alone is insufficient.
                    on_result({key: value}, {})
                except Exception as exc:
                    error = source_fetch_error('Filing acquisition failed', exc)
                    failures[key] = _failure('source_failure', error.reason_code, error.http_status)
    finally:
        for future, key in pending.items():
            future.cancel()
            failures[key] = _failure('deadline_exhausted', 'timeout')
        for key, _ in todo:
            failures[key] = _failure('deadline_exhausted', 'timeout')
        pool.shutdown(wait=False, cancel_futures=True)
    on_result({}, failures)


def _ciks(values):
    return sorted({value.zfill(10) for value in values if isinstance(value, str)
                   and re.fullmatch(r'[0-9]{1,10}', value) and int(value) > 0})


def _role_ciks(evidence, role):
    return _ciks([entry.get('cik') for entry in evidence.get('roles', [])
                  if entry.get('role') == role])


def _nearest(rows, form, date):
    identities = {}
    for row in rows:
        acc = row['accession_number']
        if acc in identities and identities[acc] != row:
            return None, 'conflicting_history_metadata'
        identities[acc] = row
    matching = [row for row in rows if row['form'] == form and row['filing_date'] < date]
    if not matching:
        return None, 'missing_prior'
    latest = max(row['filing_date'] for row in matching)
    nearest = {row['accession_number']: row for row in matching if row['filing_date'] == latest}
    if len(nearest) != 1:
        return None, 'ambiguous_prior_date'
    return next(iter(nearest.values())), 'selected'


def _issuer_binding(evidence, universe, company_symbols):
    role = 'SUBJECT-COMPANY' if evidence['form'].startswith('SCHEDULE 13') else 'FILER'
    ciks = _role_ciks(evidence, role)
    symbols = sorted({symbol for cik in ciks for symbol in company_symbols.get(str(int(cik)), set())
                      if universe is not None and universe.decision(symbol) == 'eligible'})
    binding = {'status': 'unresolved', 'role': role, 'issuer_ciks': ciks,
               'submission_sha256': evidence['submission_sha256'],
               'header_sha256': evidence['header_sha256'],
               'role_sha256s': [entry['sha256'] for entry in evidence['roles'] if entry['role'] == role],
               'eligible_symbols': symbols, 'execution_status': 'unresolved'}
    if evidence.get('structural_status') == 'complete' and len(ciks) == 1:
        binding.update(status='verified', issuer_cik=ciks[0])
        if universe is not None:
            binding['equity_membership'] = universe.filing_decision(ciks)
            if binding['equity_membership']['status'] == 'excluded':
                binding['execution_status'] = 'outside_declared_equity_universe'
    # Unknown co-subjects or multiple listed classes cannot be ignored. Source
    # issuer proof can be valid while an execution security remains unresolved.
    known = bool(ciks) and all(company_symbols.get(str(int(cik))) for cik in ciks)
    decisions_resolved = all(decision in {'eligible', 'inactive_asset', 'absent_from_asset_master',
        'outside_sip_exchange_universe'} for decision in binding.get('equity_membership', {}).get('symbols', {}).values())
    if evidence.get('structural_status') == 'complete' and known and len(ciks) == 1 and len(symbols) == 1 and decisions_resolved:
        binding.update(execution_status='verified', ticker=symbols[0])
    return binding


def hydrate_filings(source, collections: dict, *, equity_universe=None,
                    company_map=None, max_workers=16,
                    max_evidence_bytes=512 * 1024 * 1024, comparator_policy=None,
                    attribution_policy=None, material_policy=None) -> dict:
    """Hydrate all categories once under the caller's unchanged absolute budget.

    Output rows contain corpus references, never duplicated full narratives.
    Discovery rows and their query provenance are retained in original order.
    `coverage.complete` requires selected structural evidence, proven K/Q prior
    comparisons and ownership binding; model adequacy remains not assessed.
    """
    if comparator_policy not in (None, CURRENT_ONLY_POLICY):
        raise ValueError('Unknown filing comparison policy')
    from .filing_attribution_policy import POLICY as ATTRIBUTION_POLICY, attribution_binding, build_scope
    if attribution_policy not in (None, ATTRIBUTION_POLICY):
        raise ValueError('invalid_filing_attribution')
    from .filing_material_policy import POLICY as MATERIAL_POLICY, validate_quarantined_evidence
    if material_policy not in (None, MATERIAL_POLICY):
        raise ValueError('invalid_filing_material_policy')
    if current_provider_deadline('edgar') is None:
        raise ValueError('An inherited EDGAR deadline is required')
    if type(max_workers) is not int or not 1 <= max_workers <= 16:
        raise ValueError('Invalid filing worker bound')
    if type(max_evidence_bytes) is not int or not 1 <= max_evidence_bytes <= 512 * 1024 * 1024:
        raise ValueError('Invalid aggregate filing evidence bound')
    if not isinstance(collections, dict) or any(not isinstance(rows, list) for rows in collections.values()):
        raise ValueError('Invalid filing collections')
    copied = {key: [dict(row) for row in rows] for key, rows in collections.items()}
    discovery_coverage = {key: dict(rows.coverage) for key, rows in collections.items()
                          if hasattr(rows, 'coverage')}
    discovery_complete = all(value.get('complete') is True for value in discovery_coverage.values())
    symbols = EquityUniverse.company_symbols(company_map) if company_map is not None else {}
    refs, specs, failures, corpus, histories, archives = {}, {}, {}, {}, {}, {}
    accepted_bytes, accepted_objects, rejected_bytes, rejected_objects = 0, 0, 0, 0
    resource_exhausted = False
    for category, rows in copied.items():
        for index, row in enumerate(rows):
            row['requires_prior'] = False
            form = normalize_filing_form(row.get('form_type', ''))
            ownership = filing_form_family(form) in _OWNERSHIP
            required = (category == 'pqc_filings' or form in _ORDINARY or ownership)
            if not required:
                row['text_status'] = 'not_required_form'
                continue
            if equity_universe is not None and category != 'pqc_filings':
                row['universe_membership'] = equity_universe.filing_decision(row.get('ciks', []))
            if category != 'pqc_filings' and row.get('universe_membership', {}).get('status') == 'excluded':
                row['text_status'] = 'outside_declared_equity_universe'
                continue
            row['requires_prior'] = category == 'filings' and form in {'10-K', '10-Q'}
            row['prior_requirement'] = ('ordinary_filing_change' if row['requires_prior'] else
                                        'current_thematic_only' if category == 'pqc_filings' else 'not_applicable')
            acc = row.get('adsh') or row.get('accession_number')
            key = acc if isinstance(acc, str) and re.fullmatch(r'[0-9]{10}-[0-9]{2}-[0-9]{6}', acc) else f'invalid_{category}_{index}'
            refs.setdefault(key, []).append(row)
            if row.get('discovery_identity_conflict') is True:
                failures[key] = _failure('conflicting_accession_metadata')
                continue
            date, url = row.get('file_date'), row.get('file_url')
            if key != acc or not source_date(date) or len(date) != 10 or not isinstance(url, str) or not url:
                failures[key] = _failure('missing_or_invalid_filing_identity')
                continue
            if key in specs and (specs[key]['form'], specs[key]['date']) != (form, date):
                failures[key] = _failure('conflicting_accession_metadata')
                continue
            spec = specs.setdefault(key, {'form': form, 'date': date, 'url': url,
                                          'source_ciks': set(), 'required_exhibits': set(),
                                          'requires_prior': False})
            spec['requires_prior'] = spec['requires_prior'] or row['requires_prior']
            spec['source_ciks'].update(_ciks(row.get('ciks', [])))
            required_exhibits = row.get('required_exhibits', ())
            if not isinstance(required_exhibits, (list, tuple)) or any(
                    not isinstance(value, str) or not 1 <= len(value) <= 255 for value in required_exhibits):
                failures[key] = _failure('invalid_required_dependencies')
            else:
                spec['required_exhibits'].update(required_exhibits)

    def body_task(acc):
        spec = specs[acc]
        return (('body', acc), partial(source.get_complete_submission, spec['url'],
            accession=acc, form_type=spec['form'], filing_date=spec['date'],
            required_exhibits=tuple(sorted(spec['required_exhibits'])),
            **({'material_policy': material_policy} if material_policy else {})))

    def accept(results, failed):
        nonlocal accepted_bytes, accepted_objects, rejected_bytes, rejected_objects, resource_exhausted
        for (kind, key), value in results.items():
            if kind == 'body':
                spec = specs[key]
                if (not isinstance(value, dict) or value.get('accession') != key
                        or value.get('form') != spec['form'] or value.get('filing_date') != spec['date']
                        or value.get('structural_status') not in ('complete', 'insufficient')):
                    failures[key] = _failure('mismatched_submission_evidence')
                    continue
                if 'material_quarantine' in value:
                    if not material_policy:
                        raise ValueError('Undeclared filing material quarantine')
                    validate_quarantined_evidence(value)
            provider_timeout('edgar')
            try:
                size = len(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                      ensure_ascii=True, allow_nan=False).encode('utf-8'))
            except (ValueError, TypeError, RecursionError):
                failed[(kind, key)] = _failure('invalid_evidence_serialization')
                continue
            provider_timeout('edgar')
            if resource_exhausted or accepted_bytes + size > max_evidence_bytes:
                resource_exhausted = True
                rejected_objects += 1
                rejected_bytes += size
                failed[(kind, key)] = _failure('evidence_byte_limit')
                continue
            accepted_bytes += size
            accepted_objects += 1
            if kind == 'body':
                corpus[key] = value
            elif kind == 'history':
                histories[key] = value
            else:
                archives[key] = value
        for (kind, key), failure in failed.items():
            if kind == 'body':
                failures[key] = failure
            elif kind == 'history':
                histories[key] = {'failure': failure}
            else:
                archives[key] = {'failure': failure}

    annual = sorted(key for key, spec in specs.items() if spec['requires_prior'] and key not in failures)
    # Metadata and large current documents start first; no speculative first-CIK binding.
    initial_ciks = sorted({cik for key in annual for cik in specs[key]['source_ciks']})
    tasks = [(('history', cik), partial(source.get_company_submission_history, cik)) for cik in initial_ciks]
    tasks += [body_task(key) for key in annual]
    _run_tasks(tasks, max_workers, accept)
    # A validated disabled current filing creates no comparator obligation.
    comparison_annual = [key for key in annual if not corpus.get(key, {}).get('material_quarantine')]
    actual_ciks = {cik for key in comparison_annual if key in corpus for cik in _role_ciks(corpus[key], 'FILER')}
    tasks = [(('history', cik), partial(source.get_company_submission_history, cik))
             for cik in sorted(actual_ciks - set(histories))]
    _run_tasks(tasks, max_workers, accept)

    archive_tasks = {}
    for key in comparison_annual:
        if key not in corpus:
            continue
        spec = specs[key]
        for cik in _role_ciks(corpus[key], 'FILER'):
            history = histories.get(cik, {})
            if 'failure' in history:
                continue
            if comparator_policy == CURRENT_ONLY_POLICY:
                try:
                    checked_history(history, cik)
                except (KeyError, TypeError, AttributeError, ValueError, SourceFetchError):
                    continue
            candidate, _ = _nearest(history.get('filings', []), spec['form'], spec['date'])
            floor = candidate['filing_date'] if candidate else '0001-01-01'
            for descriptor in history.get('archives', []):
                if descriptor['filingFrom'] < spec['date'] and descriptor['filingTo'] >= floor:
                    archive_key = (cik, descriptor['name'])
                    archive_tasks[archive_key] = (('archive', archive_key),
                        partial(source.get_company_submission_archive, cik, descriptor))
    _run_tasks([archive_tasks[key] for key in sorted(archive_tasks)], max_workers, accept)

    # Normal comparisons keep the existing nearest-date acquisition scope.
    # Only proven no-unique candidates request older archives for permission.
    expanded_history = set()
    if comparator_policy == CURRENT_ONLY_POLICY:
        additional = {}
        for key in comparison_annual:
            current = corpus.get(key)
            if current is None or current['structural_status'] != 'complete':
                continue
            first, _ = history_comparison(current, histories,
                {cik + '/' + name: value for (cik, name), value in archives.items()},
                require_full_history=False)
            if first['status'] not in NO_UNIQUE_PRIOR:
                continue
            expanded_history.add(key)
            for cik in first['issuer_ciks']:
                for descriptor in histories[cik]['archives']:
                    archive_key = (cik, descriptor['name'])
                    if descriptor['filingFrom'] < current['filing_date'] and archive_key not in archive_tasks:
                        additional[archive_key] = (('archive', archive_key),
                            partial(source.get_company_submission_archive, cik, descriptor))
        archive_tasks.update(additional)
        _run_tasks([additional[key] for key in sorted(additional)], max_workers, accept)

    comparisons, selected_proofs = {}, {}
    for key in comparison_annual:
        if key not in corpus:
            comparisons[key] = {'status': 'current_evidence_unavailable'}
            continue
        spec, selected = specs[key], []
        filers = _role_ciks(corpus[key], 'FILER')
        if comparator_policy == CURRENT_ONLY_POLICY:
            comparison, proof = history_comparison(corpus[key], histories,
                {cik + '/' + name: value for (cik, name), value in archives.items()},
                require_full_history=key in expanded_history)
            status = comparison['status']
            if status != 'selected':
                if status in NO_UNIQUE_PRIOR and key in expanded_history and corpus[key]['structural_status'] == 'complete':
                    try:
                        provider_timeout('edgar')
                        permission = current_only_binding(corpus[key], comparison, proof)
                        provider_timeout('edgar')
                    except SourceFetchError:
                        comparison['status'] = 'comparison_proof_timeout'
                    else:
                        comparison['binding'] = permission
                comparisons[key] = comparison
                continue
            cik, candidate = filers[0], comparison['candidate']
            selected_proofs[key] = proof
        else:
            status = 'missing_issuer_roles' if not filers else 'selected'
            for cik in filers:
                history = histories.get(cik, {'failure': _failure('missing_history')})
                if 'failure' in history:
                    status = 'unproven_prior'
                    break
                rows = list(history.get('filings', []))
                relevant = [archive_key for archive_key in archive_tasks if archive_key[0] == cik]
                if any('failure' in archives.get(archive_key, {}) for archive_key in relevant):
                    status = 'unproven_prior'
                    break
                for archive_key in relevant:
                    rows.extend(archives[archive_key]['filings'])
                candidate, candidate_status = _nearest(rows, spec['form'], spec['date'])
                if candidate_status != 'selected':
                    status = candidate_status
                    break
                selected.append((cik, candidate))
            if status == 'selected' and len({row['accession_number'] for _, row in selected}) != 1:
                status = 'unresolved_joint_prior'
            if status == 'selected' and any(row != selected[0][1] for _, row in selected):
                status = 'conflicting_history_metadata'
            if status != 'selected':
                comparisons[key] = {'status': status, 'issuer_ciks': filers}
                continue
            cik, candidate = selected[0]  # Every actual FILER selected this exact accession.
        prior_acc = candidate['accession_number']
        comparisons[key] = {'status': 'selected', 'accession': prior_acc, 'issuer_ciks': filers}
        prior_spec = {'form': candidate['form'], 'date': candidate['filing_date'],
            'url': f'https://www.sec.gov/Archives/edgar/data/{int(cik)}/{prior_acc.replace("-", "")}/{prior_acc}-index.htm',
            'source_ciks': set(filers), 'required_exhibits': set(), 'requires_prior': False}
        if prior_acc in specs and (specs[prior_acc]['form'], specs[prior_acc]['date']) != (prior_spec['form'], prior_spec['date']):
            failures[prior_acc] = _failure('conflicting_prior_metadata')
        else:
            specs.setdefault(prior_acc, prior_spec)

    remaining = [body_task(key) for key in sorted(specs) if key not in corpus and key not in failures]
    _run_tasks(remaining, max_workers, accept)
    for key, comparison in comparisons.items():
        if comparison['status'] != 'selected':
            continue
        prior = corpus.get(comparison['accession'])
        if comparison['accession'] in failures or prior is None or prior.get('structural_status') != 'complete':
            comparison['status'] = 'prior_evidence_unavailable'
        elif _role_ciks(prior, 'FILER') != comparison['issuer_ciks']:
            comparison['status'] = 'prior_issuer_role_mismatch'
        else:
            comparison['status'] = 'available'
            history_refs = comparison['issuer_ciks']
            archive_keys = sorted(ref for ref in archive_tasks if ref[0] in history_refs)
            if key in selected_proofs:
                archive_keys = [tuple(ref.split('/', 1)) for ref in sorted(selected_proofs[key]['archives'])]
            archive_refs = [cik + '/' + name for cik, name in archive_keys]
            # Exact digest formula shared with the model boundary: canonical
            # JSON of these once-stored complete observations, UTF-8 SHA-256.
            proof = {'recent': {cik: histories[cik] for cik in history_refs},
                     'archives': {cik + '/' + name: archives[(cik, name)] for cik, name in archive_keys}}
            try:
                provider_timeout('edgar')
                digest = hashlib.sha256(json.dumps(proof, sort_keys=True, separators=(',', ':'),
                    ensure_ascii=True, allow_nan=False).encode('utf-8')).hexdigest()
                provider_timeout('edgar')
            except SourceFetchError:
                comparison['status'] = 'comparison_proof_timeout'
                continue
            comparison['binding'] = {
                'policy': PRIOR_POLICY, 'current_accession': key,
                'prior_accession': comparison['accession'], 'form_type': specs[key]['form'],
                'current_filing_date': specs[key]['date'], 'prior_filing_date': prior['filing_date'],
                'issuer_ciks': history_refs, 'history_refs': history_refs,
                'archive_refs': archive_refs, 'history_snapshot_sha256': digest}

    failed_rows, unresolved_ownership, outside_ownership = 0, 0, 0
    for key, rows in refs.items():
        value = corpus.get(key)
        for row in rows:
            failed = False
            if key in failures or value is None:
                row['text_status'] = 'unavailable'
                row['filing_evidence_status'] = 'unavailable'
                row['filing_failure'] = failures.get(key, _failure('missing_evidence'))
                failed = True
            else:
                row['filing_evidence_ref'] = key
                row['filing_evidence_status'] = value['structural_status']
                row['text_status'] = 'available' if value['structural_status'] == 'complete' else 'insufficient'
                failed = value['structural_status'] != 'complete'
                binding = _issuer_binding(value, equity_universe, symbols)
                attribution_permission = False
                if attribution_policy and value['structural_status'] == 'complete':
                    # This validates joint roles without changing their status.
                    binding = attribution_binding(value, equity_universe, symbols)
                    attribution_permission = True
                row['issuer_binding'] = binding
                if 'material_quarantine' in value:
                    row['filing_material_disposition'] = 'quarantined'
                    row['material_gap_codes'] = list(value['material_quarantine']['gap_codes'])
                    if row.get('requires_prior'):
                        row['prior_status'] = 'not_assessed_material_quarantine'
                failed = failed or (binding['status'] != 'verified' and not attribution_permission)
                if filing_form_family(value['form']) in _OWNERSHIP:
                    row['subject_attribution_verified'] = binding['execution_status'] == 'verified'
                    row['subject_ticker'] = binding.get('ticker', '')
                    row['ticker'] = binding.get('ticker', '')
                    if binding['execution_status'] == 'outside_declared_equity_universe':
                        outside_ownership += 1
                    elif binding['execution_status'] != 'verified':
                        unresolved_ownership += 1
                        failed = failed or not attribution_permission
            if key in comparisons:
                comparison = comparisons[key]
                if row.get('requires_prior'):
                    row['prior_status'] = comparison['status']
                    row['prior_comparison'] = {k: v for k, v in comparison.items() if k != 'binding'}
                if comparison['status'] == 'available':
                    row['prior_evidence_ref'] = comparison['accession']
                    row['comparison_binding'] = comparison['binding']
                elif row.get('requires_prior'):
                    if comparison.get('binding', {}).get('policy') == CURRENT_ONLY_POLICY:
                        row['comparison_binding'] = comparison['binding']
                        row['filing_assessment_scope'] = 'current_only'
                    else:
                        failed = True
            failed_rows += int(failed)
    deadline_exhausted = provider_clock_time('edgar') >= current_provider_deadline('edgar')
    result = {'policy': POLICY, 'collections': copied, 'corpus': dict(sorted(corpus.items())),
            'history_corpus': dict(sorted(histories.items())),
            'archive_corpus': {cik + '/' + name: value for (cik, name), value in sorted(archives.items())},
            'discovery_coverage': discovery_coverage,
            'coverage': {'mode': 'full_required_selected_evidence',
                         'complete': failed_rows == 0 and not deadline_exhausted and discovery_complete,
                         'discovery_complete': discovery_complete,
                         'required_rows': sum(map(len, refs.values())), 'failed_rows': failed_rows,
                         'unique_current_accessions': len(refs), 'acquired_accessions': len(corpus),
                         'prior_policy': PRIOR_POLICY,
                         **({'comparator_policy': comparator_policy,
                             'current_only_rows': sum(row.get('filing_assessment_scope') == 'current_only'
                                 for rows in refs.values() for row in rows),
                             'current_only_absent_rows': sum(row.get('filing_assessment_scope') == 'current_only'
                                 and row.get('prior_status') == 'missing_prior' for rows in refs.values() for row in rows),
                             'current_only_ambiguous_rows': sum(row.get('filing_assessment_scope') == 'current_only'
                                 and row.get('prior_status') == 'ambiguous_prior_date' for rows in refs.values() for row in rows)}
                            if comparator_policy else {}),
                         'prior_required_accessions': len(annual),
                         'prior_required_rows': sum(row.get('requires_prior', False)
                                                    for rows in refs.values() for row in rows),
                         'unresolved_ownership_rows': unresolved_ownership,
                         'resolved_outside_ownership_rows': outside_ownership,
                         'evidence_byte_limit': max_evidence_bytes,
                         'accepted_evidence_bytes': accepted_bytes,
                         'accepted_evidence_objects': accepted_objects,
                         'rejected_evidence_bytes': rejected_bytes,
                         'rejected_evidence_objects': rejected_objects,
                         'deadline_exhausted': deadline_exhausted,
                         'analysis_adequacy': 'not_assessed'}}
    if attribution_policy:
        result['coverage']['attribution_policy'] = attribution_policy
        result['attribution_scope'] = build_scope(result, copied, equity_universe, company_map)
    if material_policy:
        from tradingagents.strategies.orchestration.filing_material_validation import build_material_scope
        result['coverage']['material_policy'] = material_policy
        result['material_scope'] = build_material_scope(result, copied)
        for key in ('scoped_complete', 'scoped_failed_rows', 'quarantined_rows'):
            result['coverage'][key] = result['material_scope'][key]
    return result
