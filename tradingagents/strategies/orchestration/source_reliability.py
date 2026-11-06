"""Bounded observational learning from canonical, already validated reports.

build_source_reliability(report, catalog) is pure and never acquires data, writes
catalogs, changes incidents or grants trading/repair authority. Catalog v1 has
only version/cases; each exact case schema is CASE_FIELDS below. A known error
pattern offers previous learning, never proof of the current cause. Current
attribution requires its exact accepted observation ID, binding generation,
commit, session, configuration, acquisition time, payload digest and pattern.

load_source_reliability(report, repo_root) reads only the explicit root-relative
config/source-reliability-v1.json. Missing/invalid catalogs or source identities
remain unavailable/review-required. No physical-attempt, replay or outage rates
are computed. Canonical reports retain the durable observation; reviewed Git
catalog changes retain selected sanitized learning, with no additional database.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from .source_incidents import _PROVIDERS, _REASONS

VERSION = 'source_reliability_learning_v1'
CATALOG_VERSION = 'source_reliability_catalog_v1'
MAX_CATALOG_BYTES = 64 * 1024
MAX_CASES = 64
MAX_PATTERNS = 64
MAX_FAILURE_ROWS = 4096
CASE_FIELDS = frozenset({'case_id', 'source', 'provider', 'operation', 'reason_code',
    'http_status', 'classification', 'provenance', 'evidence_kind', 'evidence_sha256',
    'accepted_observation_id', 'lesson_code'})
_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}')
_CONFIG = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,191}')
_SHA = re.compile(r'[0-9a-f]{64}')
_COMMIT = re.compile(r'[0-9a-f]{40}')
_GENERATION = re.compile(r'gen_[0-9]{3,}')
_CLASSIFICATIONS = frozenset({'code_defect', 'confirmed_upstream', 'source_or_network',
                            'configuration_access', 'unresolved', 'harness'})
# Controls here are actual outbound requests, never simulated responses/replay.
_EMPIRICAL = frozenset({'native_observation', 'documented_control', 'observed_request_control'})
_PROVENANCE = _EMPIRICAL | {'controlled_mutation', 'harness'}
_EVIDENCE = frozenset({'retained_diagnostic', 'retained_capture', 'verified_regression',
                      'harness_diagnostic', 'provider_confirmation', 'operator_verified'})
_EMPIRICAL_EVIDENCE = _EVIDENCE - {'verified_regression', 'harness_diagnostic'}
_LESSONS = frozenset({'retain_unresolved_transport_evidence',
                     'validate_optional_consumed_numbers', 'separate_harness_from_source_health'})
_PATTERN_FIELDS = ('source', 'provider', 'operation', 'reason_code', 'http_status')


def _matches(pattern, value):
    return type(value) is str and pattern.fullmatch(value) is not None


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     allow_nan=False).encode()).hexdigest()


def _pattern(row):
    if type(row) is not dict:
        raise ValueError('invalid failure metadata')
    source = row.get('source', row.get('provider'))
    provider = row.get('provider')
    operation = row.get('operation', 'unspecified')
    reason, status = row.get('reason_code'), row.get('http_status')
    if (source not in _PROVIDERS or provider not in _PROVIDERS
            or not _matches(_ID, operation) or reason not in _REASONS
            or reason == 'missing_source_evidence'
            or status is not None and (type(status) is not int or not 100 <= status <= 599)):
        raise ValueError('invalid or unavailable failure metadata')
    return dict(zip(_PATTERN_FIELDS, (source, provider, operation, reason, status)))


def validate_catalog(catalog):
    """Validate reviewed metadata; provider confirmation cannot be fault injection."""
    if (type(catalog) is not dict or set(catalog) != {'version', 'cases'}
            or catalog['version'] != CATALOG_VERSION or type(catalog['cases']) is not list
            or len(catalog['cases']) > MAX_CASES):
        raise ValueError('invalid reliability catalog')
    ids, observations, result = set(), set(), []
    for row in catalog['cases']:
        if (type(row) is not dict or set(row) != CASE_FIELDS
                or not _matches(_ID, row['case_id']) or row['case_id'] in ids
                or row['classification'] not in _CLASSIFICATIONS
                or row['provenance'] not in _PROVENANCE or row['evidence_kind'] not in _EVIDENCE
                or not _matches(_SHA, row['evidence_sha256']) or row['lesson_code'] not in _LESSONS
                or row['accepted_observation_id'] is not None
                and (not _matches(_SHA, row['accepted_observation_id'])
                     or row['accepted_observation_id'] in observations)):
            raise ValueError('invalid reliability case')
        _pattern(row)
        classification, provenance, evidence = (row[key] for key in
                                               ('classification', 'provenance', 'evidence_kind'))
        if (classification == 'confirmed_upstream'
                and (evidence != 'provider_confirmation' or provenance not in _EMPIRICAL)
                or classification == 'code_defect' and evidence != 'verified_regression'
                or classification == 'harness' and (provenance != 'harness' or evidence != 'harness_diagnostic')
                or provenance == 'harness' and classification != 'harness'
                or evidence == 'harness_diagnostic' and classification != 'harness'
                or evidence == 'verified_regression' and classification != 'code_defect'
                or provenance == 'controlled_mutation' and classification not in {'code_defect', 'unresolved'}):
            raise ValueError('unsupported reliability attribution')
        ids.add(row['case_id'])
        if row['accepted_observation_id'] is not None:
            observations.add(row['accepted_observation_id'])
        result.append(dict(row))
    return sorted(result, key=lambda row: row['case_id'])


def _accepted_identity(report):
    sources = report['sources']
    acquired = datetime.fromisoformat(sources['acquired_at'])
    session = date.fromisoformat(report['session'])
    if (not _matches(_GENERATION, report['generation_id'])
            or not _matches(_COMMIT, report['generation_commit'])
            or session.isoformat() != report['session'] or acquired.utcoffset() is None
            or not _matches(_CONFIG, sources['configuration'])
            or not _matches(_SHA, sources['observation_sha256'])):
        raise ValueError('accepted source identity unavailable')
    return [VERSION, report['generation_id'], report['generation_commit'], report['session'],
            sources['configuration'], acquired.astimezone(timezone.utc).isoformat(),
            sources['observation_sha256']]


def _empirical(row):
    return (row['provenance'] in _EMPIRICAL and row['evidence_kind'] in _EMPIRICAL_EVIDENCE
            and row['classification'] not in {'code_defect', 'harness'})


def _unbound_review_diagnostics(report):
    """Keep canonical preflight/health review obligations without inventing IDs."""
    result = []
    for field, diagnostic in (
            ('preflight_incidents', 'preflight_failure_requires_review'),
            ('source_health_failures', 'source_health_failure_requires_review')):
        rows = report.get(field, []) if type(report) is dict else []
        if type(rows) is not list or len(rows) > MAX_FAILURE_ROWS:
            result.append('review_evidence_unavailable')
            continue
        for row in rows:
            if type(row) is not dict:
                continue
            if field == 'preflight_incidents':
                failures = row.get('failures', [])
                relevant = (type(failures) is list and len(failures) <= MAX_PATTERNS and any(
                    type(item) is dict and type(item.get('source')) is str
                    and item['source'] in _PROVIDERS and type(item.get('reason_code')) is str
                    and item['reason_code'] in _REASONS for item in failures))
            else:
                sources = row.get('sources', [])
                relevant = (row.get('status') in ('data_failure', 'strategy_defect')
                    and type(sources) is list and len(sources) <= MAX_PATTERNS
                    and any(type(source) is str and source in _PROVIDERS for source in sources))
            if relevant:
                result.append(diagnostic)
                break
    return result


def build_source_reliability(report, catalog=None):
    """Project unique failures; exact identity, not signature, unlocks attribution.

    Input is the existing validated-source report projection, not an admission
    API for untrusted source bodies. Empirical flags describe reviewed evidence
    provenance and never claim provider-wide availability or a denominator.
    """
    status, diagnostics = 'available', []
    try:
        cases = validate_catalog(catalog)
    except (ValueError, TypeError, KeyError, RecursionError):
        cases = []
        status = 'missing' if catalog is None else 'invalid'
    try:
        identity = _accepted_identity(report)
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        identity = None
        diagnostics.append('accepted_observation_unavailable')
    diagnostics.extend(_unbound_review_diagnostics(report))
    patterns = {}
    sources = report.get('sources', {}) if type(report) is dict else {}
    rows = sources.get('unresolved', []) if type(sources) is dict else None
    if type(rows) is not list or len(rows) > MAX_FAILURE_ROWS:
        rows = []
        diagnostics.append('failure_metadata_unavailable')
    for row in rows:
        try:
            pattern = _pattern(row)
        except (ValueError, TypeError):
            if 'failure_metadata_unavailable' not in diagnostics:
                diagnostics.append('failure_metadata_unavailable')
            continue
        pattern_id = _digest([VERSION, pattern])
        patterns[pattern_id] = pattern
    truncated = len(patterns) > MAX_PATTERNS
    if truncated:
        diagnostics.append('pattern_bound_exceeded')
    result = []
    for pattern_id, pattern in sorted(patterns.items())[:MAX_PATTERNS]:
        observation_id = _digest([identity, pattern_id]) if identity is not None else None
        learned = [row for row in cases if _pattern(row) == pattern]
        exact = next((row for row in learned if observation_id is not None
                      and row['accepted_observation_id'] == observation_id), None)
        result.append(dict(pattern, pattern_id=pattern_id, observation_id=observation_id,
            known_pattern=bool(learned), current_attribution=exact['classification'] if exact else 'unresolved',
            current_empirical_evidence=_empirical(exact) if exact else False,
            previous_learning=[{key: row[key] for key in
                ('case_id', 'classification', 'provenance', 'evidence_kind', 'evidence_sha256', 'lesson_code')}
                | {'empirical_evidence': _empirical(row)} for row in learned]))
    unavailable = status != 'available' or bool(diagnostics)
    return {'version': VERSION, 'assessment_status': 'unavailable' if unavailable else 'available',
        'catalog_status': status, 'review_required': unavailable or any(
            row['current_attribution'] == 'unresolved' for row in result),
        'selection_truncated': truncated, 'patterns': result, 'diagnostics': diagnostics}


def _catalog_bytes(repo_root):
    """Reject symlinks/special files before reads, including a FIFO with no writer."""
    directory = os.open(Path(repo_root)/'config',
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        fd = os.open('source-reliability-v1.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=directory)
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_CATALOG_BYTES:
                raise ValueError('reliability catalog not a bounded regular file')
            payload = bytearray()
            while len(payload) <= MAX_CATALOG_BYTES:
                chunk = os.read(fd, min(65536, MAX_CATALOG_BYTES + 1 - len(payload)))
                if not chunk:
                    break
                payload.extend(chunk)
            after = os.fstat(fd)
            if (len(payload) != before.st_size or len(payload) > MAX_CATALOG_BYTES
                    or any(getattr(before, key) != getattr(after, key) for key in
                           ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns'))):
                raise ValueError('reliability catalog changed or exceeded bound')
            return bytes(payload)
        finally:
            os.close(fd)
    finally:
        os.close(directory)


def load_source_reliability(report, repo_root):
    """Load one bounded, explicit root-relative reviewed catalog; never write it."""
    try:
        payload = _catalog_bytes(repo_root)
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError('duplicate catalog field')
                result[key] = value
            return result
        def invalid_constant(_):
            raise ValueError('nonfinite catalog value')
        catalog = json.loads(payload, object_pairs_hook=unique, parse_constant=invalid_constant)
    except FileNotFoundError:
        catalog = None
    except (OSError, ValueError, TypeError, RecursionError):
        catalog = {}
    return build_source_reliability(report, catalog)
