#!/usr/bin/env python3
"""One retained original through real plan Luna recognition/blind check and Sol.

Run from an externally supervised, private evidence directory. No discovery,
retries, expected answers, provider/API fallback, decisions or orders. Native
transport retains its 120-second per-call cap; all stages share 350 seconds.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import platform
import sys
import time

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def prepare_smoke(registry, session_as_of, auth_path):
    from tradingagents.default_config import DEFAULT_CONFIG
    from tradingagents.strategies.bounded_document_research import _cutoff, prepare_recognition
    from tradingagents.strategies.document_decision_eligibility import prepare_automatic_verification
    from tradingagents.strategies.orchestration.bounded_document_inputs import load_retained_documents
    if type(registry) is not list or len(registry) != 1:
        raise ValueError('one_retained_document_required')
    config = deepcopy(DEFAULT_CONFIG)
    config['execution'] = {'mode':'paper', 'execution_enabled':False}
    config['autoresearch'].update(bounded_document_research_policy='bounded_document_research_v1',
        bounded_document_decision_policy='verified_scoped_claims_v1', bounded_document_registry=registry,
        llm_billing_mode='chatgpt_plan', chatgpt_plan_auth_path=auth_path,
        autoresearch_model='gpt-6-luna', thesis_model='gpt-6.1-sol', llm_effort='high', thesis_effort='high')
    source = load_retained_documents(config['autoresearch'], session_as_of)
    if not source.get('coverage', {}).get('complete'):
        raise ValueError('source_intake_failed')
    record = source['bounded_documents'][0]
    request = prepare_recognition(record['evidence'], session_as_of=session_as_of)
    # Strict answer-free scope validation occurs before constructing any client.
    prepare_automatic_verification(request, record.get('decision_scope'))
    scope = record['decision_scope']
    session = _cutoff(session_as_of)  # Same NY end-of-day semantics as intake.
    start = datetime.fromisoformat(scope['effective_from'].replace('Z', '+00:00'))
    until = datetime.fromisoformat(scope['effective_until'].replace('Z', '+00:00'))
    if not start <= session <= until:
        raise ValueError('scope_window_excludes_session')
    return config, record


def summarize(attempt, result, error_class):
    attempt = attempt or {}
    stages = {}
    checks = []
    for name, model, role in [('recognition_attempt','gpt-6-luna','bounded'),
            ('verification_attempt','gpt-6-luna','bounded'), ('thesis_attempt','gpt-6.1-sol','thesis')]:
        stage = attempt.get(name) or {}
        provenance = stage.get('model_provenance') or {}
        returned = provenance.get('returned_model')
        identity_ok = (returned is None and provenance.get('identity_status') == 'unpinned') or (
            type(returned) is str and (returned == model or returned.startswith(model + '-')))
        ok = (stage.get('model_call_started') is True and stage.get('response_received') is True
            and stage.get('validation_status') == 'validated' and provenance.get('configured_model') == model
            and identity_ok and provenance.get('reasoning_effort') == 'high'
            and provenance.get('role') == role and provenance.get('billing_mode') == 'chatgpt_plan')
        checks.append(bool(ok))
        stages[name] = {key:stage.get(key) for key in ('model_call_started','response_received','validation_status','response_sha256')}
        stages[name]['model_provenance'] = provenance
    eligibility = attempt.get('decision_eligibility') or {}
    passed = (all(checks) and attempt.get('validation_status') == 'validated'
        and (attempt.get('automatic_review') or {}).get('version') == 'automatic_review_v1'
        and eligibility.get('eligible') is True and eligibility.get('verification_basis') == 'automatic_review_v1'
        and type(result) is dict and result.get('analysis_status') == 'validated' and error_class is None)
    return {'version':'native-document-smoke-v1', 'status':'passed' if passed else 'failed',
        'scope':'local_selected_page_native_integration', 'error_class':error_class,
        'failure_reason':attempt.get('failure_reason'), 'stages':stages,
        'native_calls_attempted':sum(bool(value.get('model_call_started')) for value in stages.values()),
        'eligibility_receipt_sha256':eligibility.get('receipt_sha256'),
        'automatic_review_sha256':(attempt.get('automatic_review') or {}).get('receipt_sha256'),
        'sol_outcome':{key:result.get(key) for key in ('direction','score','rationale','context_sufficient','used_claim_ids')} if result else None,
        'source_network_calls':0, 'full_document_quality_accepted':False, 'deployed':False,
        'orders_attempted':0, 'correlated_model_risk':True, 'guarantees_accuracy':False}


def save(path, value):
    with path.open('x') as handle:
        json.dump(value, handle, indent=2, sort_keys=True, ensure_ascii=False)
        handle.write('\n')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--registry', type=Path, required=True)
    parser.add_argument('--auth-path', required=True)
    parser.add_argument('--as-of', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    previous_logging = logging.root.manager.disable
    previous_umask = os.umask(0o077)
    try:
        logging.disable(logging.CRITICAL)
        args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
        registry_bytes = args.registry.read_bytes()
        config, record = prepare_smoke(json.loads(registry_bytes), args.as_of, args.auth_path)
        from tradingagents.strategies.learning.llm_analyzer import LLMAnalyzer
        from tradingagents.strategies.runtime_deadline import model_budget
        analyzer = LLMAnalyzer(config)
        started = time.monotonic()
        result = None
        error_class = None
        try:
            with model_budget(started + 350):
                result = analyzer.analyze_bounded_document_evidence(record['evidence'], session_as_of=args.as_of,
                    ticker=record['ticker'], decision_scope=record['decision_scope'])
        except Exception as error:
            error_class = type(error).__name__
        attempt = analyzer.last_bounded_document_attempt
        save(args.output / 'analyzer-attempt.json', attempt)
        if result is not None:
            save(args.output / 'analysis-result.json', result)
        summary = summarize(attempt, result, error_class)
        summary.update(elapsed_seconds=time.monotonic()-started,
            completed_at=datetime.now(timezone.utc).isoformat(),
            registry_sha256=hashlib.sha256(registry_bytes).hexdigest(),
            registry_unchanged=args.registry.read_bytes() == registry_bytes,
            original_sha256=record['evidence']['original']['sha256'],
            runtime={'system':platform.system(),'machine':platform.machine(),
                'python_version':platform.python_version(),'executable':sys.executable})
        if not summary['registry_unchanged']:
            summary['status'] = 'failed'
        save(args.output / 'smoke-receipt.json', summary)
        print(json.dumps({key:summary[key] for key in ('status','native_calls_attempted','error_class','elapsed_seconds')}))
        return int(summary['status'] != 'passed')
    finally:
        logging.disable(previous_logging)
        os.umask(previous_umask)


if __name__ == '__main__':
    raise SystemExit(main())
