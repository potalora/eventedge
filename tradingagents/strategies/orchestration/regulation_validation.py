"""Recompute regulatory source/assessment bindings without new provider calls."""
from __future__ import annotations

import json
import hashlib
import re

from tradingagents.strategies.data_sources.regulation_evidence import digest
from .regulation_inputs import validate_regulation_policy


def _attempt(row, original, universe, company_map):
    """Rebuild a retained request and bind its actual bounded response text."""
    from tradingagents.strategies.data_sources.regulation_assessment import (
        MAX_RESPONSE_BYTES, prepare_request, validate_assessment, validate_target_bindings,
    )
    attempt = row.get('model_attempt')
    if attempt is None:
        if row.get('assessment') is not None or row.get('model_provenance'):
            raise ValueError('regulatory model attempt missing')
        return None
    keys = {'version', 'source_provenance', 'model_provenance', 'model_call_started',
        'response_received', 'response_text', 'response_sha256', 'response_bytes',
        'response_encoding', 'response_over_limit', 'validation_status', 'failure_reason'}
    if (type(attempt) is not dict or set(attempt) != keys
            or attempt['version'] != 'regulation-model-attempt-v1'
            or type(attempt['model_provenance']) is not dict
            or attempt['model_provenance'] != (row.get('model_provenance') or {})
            or any(type(attempt[key]) is not bool for key in
                ('model_call_started', 'response_received', 'response_over_limit'))
            or attempt['validation_status'] not in ('failed', 'validated')):
        raise ValueError('invalid regulatory model attempt')
    valid = attempt['validation_status'] == 'validated'
    if ((valid and attempt['failure_reason'] is not None)
            or (not valid and (type(attempt['failure_reason']) is not str
                or not re.fullmatch(r'(?:invalid_regulation_[a-z0-9_]+|model_deadline_exhausted|model_unavailable|analysis_unavailable)',
                    attempt['failure_reason'])))):
        raise ValueError('invalid regulatory attempt outcome')
    provenance = attempt['source_provenance']
    request = None
    if provenance is not None:
        if original is None or type(provenance) is not dict:
            raise ValueError('unbound regulatory attempt')
        targets = validate_target_bindings(original, provenance['target_bindings'], universe, company_map)
        request = prepare_request(original, target_bindings=targets, regime_context=provenance['regime_context'])
        if digest(provenance) != digest(json.loads(request.context.provenance_json)):
            raise ValueError('regulatory attempt request changed')
    elif attempt['model_call_started'] or attempt['model_provenance'] or valid:
        raise ValueError('regulatory attempt lacks request')
    if not attempt['response_received']:
        if (valid or attempt['response_over_limit'] or any(attempt[key] is not None for key in
                ('response_text', 'response_sha256', 'response_bytes', 'response_encoding'))):
            raise ValueError('regulatory response invented')
        return None
    if (not attempt['model_call_started'] or request is None
            or type(attempt['response_bytes']) is not int or attempt['response_bytes'] < 0
            or type(attempt['response_sha256']) is not str
            or not re.fullmatch('[a-f0-9]{64}', attempt['response_sha256'])
            or attempt['response_encoding'] not in ('utf-8', 'utf-8-surrogatepass')
            or attempt['response_over_limit'] is not (attempt['response_bytes'] > MAX_RESPONSE_BYTES)):
        raise ValueError('invalid regulatory response receipt')
    if attempt['response_over_limit']:
        if (valid or attempt['response_text'] is not None
                or attempt['failure_reason'] not in (
                    'invalid_regulation_response_limit', 'model_deadline_exhausted')):
            raise ValueError('regulatory oversized response relabeled')
        return None
    if type(attempt['response_text']) is not str:
        raise ValueError('regulatory original response missing')
    errors = 'strict' if attempt['response_encoding'] == 'utf-8' else 'surrogatepass'
    raw = attempt['response_text'].encode('utf-8', errors=errors)
    if len(raw) != attempt['response_bytes'] or hashlib.sha256(raw).hexdigest() != attempt['response_sha256']:
        raise ValueError('regulatory original response changed')
    if attempt['response_encoding'] != 'utf-8':
        # A response can arrive after the deadline and also have invalid bytes.
        # Preserve the original transport failure and the separate byte receipt;
        # neither condition can become a valid assessment during replay.
        if valid or attempt['failure_reason'] not in (
                'invalid_regulation_response_encoding', 'model_deadline_exhausted'):
            raise ValueError('invalid regulatory encoding accepted')
        return None
    try:
        result = validate_assessment(attempt['response_text'], request.context)
    except ValueError:
        if valid:
            raise ValueError('invalid regulatory response accepted') from None
        return None
    return result


def validate_regulation_health(data, records, policy_ids, strategy_sources, config):
    """Check every horizon, retaining failed acquisition/analysis as failures.

    Completed no-thesis assessments must validate against every source unit.
    A source-complete record without every discovered document assessment cannot
    claim healthy coverage. This validates provenance/schema, not thesis truth.
    """
    try:
        scope = validate_regulation_policy(data, config)
        if scope is None:
            from .research_resilience import configured as resilience_configured
            source = data.get('regulations', {})
            if (resilience_configured(config) and config.get('regulation_evidence_policy') is not None
                    and isinstance(source.get('error'), str) and source['error'].strip()):
                by_identity = {}
                for row in records:
                    value = row if isinstance(row, dict) else vars(row)
                    identity = value['policy_id'], value['strategy']
                    if identity in by_identity:
                        raise ValueError('duplicate regulatory failure health')
                    by_identity[identity] = value
                if not policy_ids:
                    raise ValueError('missing regulatory failure horizons')
                for policy_id in policy_ids:
                    for strategy, sources in strategy_sources.items():
                        record = by_identity.get((policy_id, strategy))
                        evidence = record.get('evidence', {}) if record is not None else {}
                        if (record is None or record['status'] != 'data_failure' or record['signal_count'] != 0
                                or evidence.get('candidate_count') != 0
                                or evidence.get('data_sources') != sorted(sources)
                                or 'regulation_source_coverage' not in evidence
                                or evidence['regulation_source_coverage'] is not None
                                or evidence.get('regulation_assessments') != []
                                or evidence.get('provider_errors', {}).get('regulations') != source['error']):
                            raise ValueError('unbound regulatory acquisition failure')
                return None
            if any('regulation_source_coverage' in (row if isinstance(row, dict) else vars(row)).get('evidence', {})
                   for row in records):
                raise ValueError('unbound regulatory health')
            return None
        if not policy_ids:
            raise ValueError('missing regulatory horizons')
        source = data['regulations']
        graph = source['regulation_evidence']
        expected_rows = {}
        for row in graph['rows']:
            expected_rows.setdefault(row['document_id'], []).append(row)
        by_identity = {}
        for record in records:
            value = record if isinstance(record, dict) else vars(record)
            key = (value['policy_id'], value['strategy'])
            if key in by_identity:
                raise ValueError('duplicate regulatory health')
            by_identity[key] = value
        from tradingagents.strategies.data_sources.equity_universe import EquityUniverse
        from tradingagents.strategies.data_sources.regulation_assessment import (
            prepare_request, validate_assessment, validate_target_bindings, _RESPONSE_KEYS,
        )
        company_map = data.get('edgar', {}).get('company_tickers', {})
        snapshot = data.get('equity_universe', {}).get('snapshot')
        # An independent SEC outage can leave no company map, including the
        # reporter's empty-map projection. Match native asset-only construction;
        # retain the original map for every regulatory target-binding check.
        universe_map = None if type(company_map) is dict and not company_map else company_map
        universe = EquityUniverse(snapshot, company_map=universe_map) if snapshot is not None else None
        counts, context_consumers = [], []
        for policy_id in sorted(policy_ids):
            for strategy, sources in sorted(strategy_sources.items()):
                record = by_identity.get((policy_id, strategy))
                if record is None:
                    raise ValueError('missing regulatory health')
                evidence = record['evidence']
                if (digest(evidence.get('regulation_source_coverage')) != digest(scope)
                        or evidence.get('data_sources') != sorted(sources)
                        or type(evidence.get('regulation_assessments')) is not list):
                    raise ValueError('regulatory source health differs')
                errors = evidence.get('provider_errors', {})
                if not scope['content_complete'] and (record['status'] != 'data_failure'
                        or errors.get('regulations') != source['error']):
                    raise ValueError('regulatory acquisition failure erased')
                assessments = evidence['regulation_assessments']
                # The commodity module consumes source context; the regulatory
                # pipeline owns the complete document assessment obligation.
                # Both consumers must retain identical acquisition evidence.
                if strategy != 'regulatory_pipeline':
                    if assessments:
                        raise ValueError('unexpected regulatory document assessments')
                    context_consumers.append({'policy_id': policy_id, 'strategy': strategy,
                        'acquisition_complete': scope['content_complete']})
                    continue
                seen, completed, failed = set(), 0, 0
                for row in assessments:
                    docid = row['document_id']
                    if (docid in seen or docid not in expected_rows
                            or row['regulation_evidence_ref'] != docid
                            or row['source_rows'] != expected_rows[docid]
                            or row['source_manifest_sha256'] != graph['manifest_sha256']
                            or row['evidence_sha256'] != graph['corpus'].get(docid, {}).get('manifest_sha256')):
                        raise ValueError('regulatory assessment source differs')
                    seen.add(docid)
                    assessment = row.get('assessment')
                    attempt_assessment = _attempt(row, graph['corpus'].get(docid), universe, company_map)
                    if assessment is None:
                        if (row.get('analysis_status') != 'failed' or row.get('journal_only') is not True
                                or not isinstance(row.get('analysis_failure_reason'), str)
                                or not row['analysis_failure_reason'].strip()):
                            raise ValueError('missing regulatory failed attempt')
                        failed += 1
                        continue
                    original = graph['corpus'][docid]
                    provenance = assessment['source_provenance']
                    bindings = validate_target_bindings(original, provenance['target_bindings'], universe, company_map)
                    request = prepare_request(original, target_bindings=bindings, regime_context=provenance['regime_context'])
                    response = {key: assessment[key] for key in _RESPONSE_KEYS}
                    expected = validate_assessment(json.dumps(response, allow_nan=False), request.context)
                    if (digest(expected) != digest(assessment) or digest(attempt_assessment) != digest(assessment)
                            or row['document_assessment'] != expected['document_assessment']):
                        raise ValueError('regulatory assessment differs from originals')
                    sufficient = expected['regulation_evidence_status'] == 'sufficient'
                    if sufficient:
                        if (row['analysis_status'] != 'validated' or row.get('analysis_failure_reason') is not None
                                or row['regulation_evidence_status'] != 'sufficient'
                                or row['model_attempt']['validation_status'] != 'validated'):
                            raise ValueError('regulatory assessment status differs')
                        if expected['document_assessment'] == 'assessed_no_directional_thesis':
                            if row['ticker'] != '' or row['journal_only'] is not True:
                                raise ValueError('regulatory no-thesis has order authority')
                        elif row['ticker'] != expected['target_ticker']:
                            raise ValueError('regulatory target differs')
                        completed += 1
                    else:
                        if row['analysis_status'] != 'failed' or row['journal_only'] is not True:
                            raise ValueError('regulatory insufficiency erased')
                        failed += 1
                missing = len(set(expected_rows) - seen)
                excluded = evidence.get('admission_manifest', {}).get('excluded', [])
                complete = bool(scope['content_complete'] and not missing and not failed and not excluded)
                if not complete and record['status'] != 'data_failure':
                    raise ValueError('incomplete regulatory analysis has healthy status')
                counts.append({'policy_id': policy_id, 'strategy': strategy,
                    'required_documents': len(expected_rows), 'assessed_documents': completed,
                    'failed_documents': failed, 'missing_documents': missing,
                    'excluded_candidates': len(excluded), 'analysis_complete': complete})
        return {'policy': scope['policy'], 'source_manifest_sha256': scope['manifest_sha256'],
            'acquisition_complete': scope['content_complete'],
            'analysis_complete': bool(counts) and all(row['analysis_complete'] for row in counts),
            'horizons': counts, 'source_context_consumers': context_consumers}
    except (KeyError, TypeError, AttributeError, ValueError, OverflowError) as error:
        raise ValueError('invalid_regulation_health') from error
