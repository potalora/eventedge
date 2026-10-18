"""Learning remains observational, evidence-bound, deduplicated and public-safe."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import socket

import pytest


def unit():
    name = 'tradingagents.strategies.orchestration.source_reliability'
    assert importlib.util.find_spec(name) is not None, 'reliability learning projector is missing'
    return __import__(name, fromlist=['build_source_reliability'])


def observation():
    return {'generation_id': 'gen_001', 'generation_commit': 'a' * 40,
        'session': '2026-10-16', 'sources': {
            'configuration': 'b' * 64, 'acquired_at': '2026-10-18T05:00:00+00:00',
            'observation_sha256': 'c' * 64, 'unresolved': [{
                'source': 'noaa', 'provider': 'noaa', 'operation': 'bulk_daily_observations',
                'reason_code': 'timeout', 'http_status': None, 'attempts': 3}]}}


def case(**changes):
    value = {'case_id': 'observed_noaa_timeout', 'source': 'noaa', 'provider': 'noaa',
        'operation': 'bulk_daily_observations', 'reason_code': 'timeout', 'http_status': None,
        'classification': 'source_or_network', 'provenance': 'native_observation',
        'evidence_kind': 'retained_diagnostic', 'evidence_sha256': 'd' * 64,
        'accepted_observation_id': None, 'lesson_code': 'retain_unresolved_transport_evidence'}
    return dict(value, **changes)


def catalog(*cases):
    return {'version': 'source_reliability_catalog_v1', 'cases': list(cases)}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail('learning projection must not acquire provider/model data')
    monkeypatch.setattr(socket, 'create_connection', denied)
    monkeypatch.setattr(socket, 'getaddrinfo', denied)


def test_known_pattern_does_not_attribute_a_new_acquisition_or_recover_source():
    original = observation()
    before = deepcopy(original)
    result = unit().build_source_reliability(original, catalog(case()))
    row = result['patterns'][0]
    assert row['known_pattern'] is True
    assert row['previous_learning'][0]['classification'] == 'source_or_network'
    assert row['current_attribution'] == 'unresolved'
    assert row['current_empirical_evidence'] is False
    assert result['review_required'] is True
    assert original == before


def test_exact_accepted_observation_can_reuse_reviewed_attribution_only():
    module = unit()
    value = observation()
    identity = module.build_source_reliability(value, catalog())['patterns'][0]['observation_id']
    learned = case(accepted_observation_id=identity)
    result = module.build_source_reliability(value, catalog(learned))
    assert result['patterns'][0]['current_attribution'] == 'source_or_network'
    assert result['patterns'][0]['current_empirical_evidence'] is True
    assert result['review_required'] is False
    changed = deepcopy(value)
    changed['sources']['acquired_at'] = '2026-10-18T05:01:00+00:00'
    assert module.build_source_reliability(changed, catalog(learned))['patterns'][0]['current_attribution'] == 'unresolved'


def test_retries_duplicates_replay_and_report_regeneration_do_not_multiply_observations():
    module = unit()
    original = observation()
    expected = module.build_source_reliability(original, catalog(case()))
    repeated = deepcopy(original)
    repeated['sources']['unresolved'] *= 200
    for index, row in enumerate(repeated['sources']['unresolved']):
        repeated['sources']['unresolved'][index] = dict(row, attempts=(index % 5) + 1)
    repeated['attempt_counts'] = {'daily': 99, 'preflight': 300}
    repeated['collected_at_utc'] = '2026-10-19T00:00:00+00:00'
    assert module.build_source_reliability(repeated, catalog(case())) == expected
    assert len(expected['patterns']) == 1
    assert not {'uptime', 'failure_rate', 'success_rate', 'outage_count'} & expected.keys()


@pytest.mark.parametrize('provenance,classification,evidence_kind', [
    ('controlled_mutation', 'code_defect', 'verified_regression'),
    ('harness', 'harness', 'harness_diagnostic'),
])
def test_mutations_and_harness_findings_are_not_empirical_provider_failures(
        provenance, classification, evidence_kind):
    module = unit()
    value = observation()
    identity = module.build_source_reliability(value, catalog())['patterns'][0]['observation_id']
    learned = case(provenance=provenance, classification=classification,
                   evidence_kind=evidence_kind, accepted_observation_id=identity)
    row = module.build_source_reliability(value, catalog(learned))['patterns'][0]
    assert row['current_attribution'] == classification
    assert row['current_empirical_evidence'] is False
    assert row['previous_learning'][0]['empirical_evidence'] is False


@pytest.mark.parametrize('damage', ['missing_digest', 'bad_commit', 'naive_acquired', 'missing_configuration'])
def test_missing_or_invalid_identity_is_unknown_not_an_invented_observation(damage):
    value = observation()
    if damage == 'missing_digest': value['sources'].pop('observation_sha256')
    if damage == 'bad_commit': value['generation_commit'] = 'PRIVATE_SECRET'
    if damage == 'naive_acquired': value['sources']['acquired_at'] = '2026-10-18T05:00:00'
    if damage == 'missing_configuration': value['sources'].pop('configuration')
    result = unit().build_source_reliability(value, catalog(case()))
    assert result['assessment_status'] == 'unavailable'
    assert result['review_required'] is True
    assert result['patterns'][0]['observation_id'] is None
    assert result['patterns'][0]['current_attribution'] == 'unresolved'
    assert 'PRIVATE_SECRET' not in json.dumps(result)


@pytest.mark.parametrize('damage', ['extra', 'oversized', 'duplicate', 'unconfirmed_upstream',
                                   'injected_upstream', 'hidden_harness', 'hidden_regression'])
def test_invalid_catalog_cannot_inject_cause_or_private_text(damage):
    learned = case()
    cases = [learned]
    if damage == 'extra': learned['raw_error'] = 'PRIVATE_SECRET'
    if damage == 'oversized': cases *= 65
    if damage == 'duplicate': cases *= 2
    if damage == 'unconfirmed_upstream': learned['classification'] = 'confirmed_upstream'
    if damage == 'injected_upstream':
        learned.update(classification='confirmed_upstream', provenance='controlled_mutation',
                       evidence_kind='provider_confirmation')
    if damage == 'hidden_harness': learned['evidence_kind'] = 'harness_diagnostic'
    if damage == 'hidden_regression': learned['evidence_kind'] = 'verified_regression'
    result = unit().build_source_reliability(observation(), catalog(*cases))
    assert result['catalog_status'] == 'invalid'
    assert result['assessment_status'] == 'unavailable'
    assert result['review_required'] is True
    assert result['patterns'][0]['previous_learning'] == []
    assert result['patterns'][0]['current_attribution'] == 'unresolved'
    assert 'PRIVATE_SECRET' not in json.dumps(result)


def test_provider_confirmation_is_still_exact_observation_bound():
    learned = case(classification='confirmed_upstream', evidence_kind='provider_confirmation')
    row = unit().build_source_reliability(observation(), catalog(learned))['patterns'][0]
    assert row['known_pattern'] is True
    assert row['current_attribution'] == 'unresolved'


def test_missing_evidence_and_untrusted_failure_fields_never_become_provider_health():
    value = observation()
    value['sources']['unresolved'] += [{
        'provider': 'PRIVATE_SECRET', 'reason_code': 'timeout', 'http_status': None},
        {'provider': 'noaa', 'reason_code': 'missing_source_evidence', 'http_status': None}]
    result = unit().build_source_reliability(value, catalog())
    assert result['assessment_status'] == 'unavailable'
    assert result['review_required'] is True
    assert len(result['patterns']) == 1
    assert 'PRIVATE_SECRET' not in json.dumps(result)


def test_pattern_capacity_exhaustion_is_explicit_and_retains_bounded_selection():
    value = observation()
    baseline = value['sources']['unresolved'][0]
    value['sources']['unresolved'] = [dict(baseline, operation=f'operation_{index}')
                                     for index in range(65)]
    result = unit().build_source_reliability(value, catalog())
    assert len(result['patterns']) == 64
    assert result['selection_truncated'] is True
    assert result['assessment_status'] == 'unavailable'
    assert result['review_required'] is True
    assert result['diagnostics'] == ['pattern_bound_exceeded']


def test_transport_provider_and_observed_source_are_separate_pattern_dimensions():
    value = observation()
    value['sources']['unresolved'] = [
        {'source': source, 'provider': 'alpaca', 'operation': 'bars',
         'reason_code': 'timeout', 'http_status': None}
        for source in ('alpaca', 'equity_universe')]
    result = unit().build_source_reliability(value, catalog())
    assert len(result['patterns']) == 2
    assert len({row['observation_id'] for row in result['patterns']}) == 2
    assert {row['source'] for row in result['patterns']} == {'alpaca', 'equity_universe'}


@pytest.mark.parametrize('scope,diagnostic', [
    ('preflight_incidents', 'preflight_failure_requires_review'),
    ('source_health_failures', 'source_health_failure_requires_review'),
])
def test_unbound_failure_evidence_requires_review_after_later_success_without_invented_ids(scope, diagnostic):
    value = observation()
    value['sources']['unresolved'] = []
    if scope == 'preflight_incidents':
        value[scope] = [{'failures': [{'source': 'noaa', 'reason_code': 'timeout'}]}]
    else:
        value[scope] = [{'status': 'data_failure', 'sources': ['noaa']}]
    before = deepcopy(value)
    result = unit().build_source_reliability(value, catalog())
    assert result['review_required'] is True
    assert diagnostic in result['diagnostics']
    assert result['patterns'] == []
    assert value == before


def test_unrelated_runtime_failures_do_not_masquerade_as_source_learning():
    value = observation()
    value['sources']['unresolved'] = []
    value['preflight_incidents'] = [{'runtime_failures': [{'reason_code': 'PRIVATE_SECRET'}], 'failures': []}]
    value['source_health_failures'] = [{'status': 'legitimate_no_event', 'sources': ['noaa']}]
    result = unit().build_source_reliability(value, catalog())
    assert result['review_required'] is False
    assert result['diagnostics'] == []
    assert 'PRIVATE_SECRET' not in json.dumps(result)


@pytest.mark.parametrize('sources', [None, [], 'PRIVATE_SECRET'])
def test_malformed_source_projection_stays_unavailable_without_raw_forwarding(sources):
    value = observation()
    value['sources'] = sources
    result = unit().build_source_reliability(value, catalog())
    assert result['assessment_status'] == 'unavailable'
    assert result['review_required'] is True
    assert result['patterns'] == []
    assert 'PRIVATE_SECRET' not in json.dumps(result)


def test_explicit_root_catalog_loader_is_bounded_and_keeps_missing_invalid_unknown(tmp_path):
    module = unit()
    assert module.load_source_reliability(observation(), tmp_path)['catalog_status'] == 'missing'
    config = tmp_path/'config/source-reliability-v1.json'
    config.parent.mkdir()
    config.write_text(json.dumps(catalog(case())))
    assert module.load_source_reliability(observation(), tmp_path)['catalog_status'] == 'available'
    config.write_text('{"version":"PRIVATE_SECRET", "version":"PRIVATE_SECRET"}')
    result = module.load_source_reliability(observation(), tmp_path)
    assert result['catalog_status'] == 'invalid'
    assert 'PRIVATE_SECRET' not in json.dumps(result)
    config.write_bytes(b'x' * (module.MAX_CATALOG_BYTES + 1))
    assert module.load_source_reliability(observation(), tmp_path)['catalog_status'] == 'invalid'


def test_catalog_symlink_is_not_followed_even_when_target_looks_valid(tmp_path):
    module = unit()
    target = tmp_path/'untrusted.json'
    target.write_text(json.dumps(catalog(case())))
    config = tmp_path/'config/source-reliability-v1.json'
    config.parent.mkdir()
    config.symlink_to(target)
    result = module.load_source_reliability(observation(), tmp_path)
    assert result['catalog_status'] == 'invalid'
    assert result['assessment_status'] == 'unavailable'
    assert result['review_required'] is True


def test_catalog_fifo_is_rejected_without_waiting_for_a_writer(tmp_path):
    import os
    import subprocess
    import sys
    config = tmp_path/'config/source-reliability-v1.json'
    config.parent.mkdir()
    os.mkfifo(config, 0o600)
    script = '''
import signal, sys
from tradingagents.strategies.orchestration.source_reliability import load_source_reliability
signal.signal(signal.SIGALRM, signal.SIG_DFL)
signal.setitimer(signal.ITIMER_REAL, 2)
try:
 result = load_source_reliability({}, sys.argv[1])
 assert result['catalog_status'] == 'invalid'
 assert result['assessment_status'] == 'unavailable'
 print('catalog_unavailable', flush=True)
finally:
 signal.setitimer(signal.ITIMER_REAL, 0)
'''
    result = subprocess.run([sys.executable, '-B', '-c', script, str(tmp_path)],
                            capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout == b'catalog_unavailable\n'


def test_reviewed_public_catalog_keeps_noaa_uncertain_and_code_findings_separate():
    path = Path(__file__).resolve().parents[1]/'config/source-reliability-v1.json'
    value = json.loads(path.read_text())
    unit().validate_catalog(value)
    assert any(row['source'] == 'noaa' and row['classification'] == 'source_or_network'
               for row in value['cases'])
    assert not any(row['classification'] == 'confirmed_upstream' for row in value['cases'])
    assert any(row['source'] == 'finnhub' and row['classification'] == 'code_defect'
               and row['provenance'] == 'controlled_mutation' for row in value['cases'])


def test_actual_retained_noaa_failure_stays_reviewable_without_fabricated_acquisition_identity():
    import gzip
    import hashlib
    from tradingagents.strategies.orchestration.source_inputs import SourceInputStore
    root = Path(__file__).resolve().parent/'historical'
    manifest = json.loads((root/'manifest.json').read_text())
    receipt = next(row for row in manifest['public_cases']
                   if row['id'] == 'native-reference-20261016-noaa')
    payload_bytes = (root/'public'/receipt['path']).read_bytes()
    assert hashlib.sha256(payload_bytes).hexdigest() == receipt['sha256']
    decoded = gzip.decompress(payload_bytes)
    assert hashlib.sha256(decoded).hexdigest() == receipt['decoded_sha256']
    payload = SourceInputStore.decode(decoded.decode())
    assert receipt['boundary'] == 'normalized_capture'
    assert payload['observations'] == 0 and payload['coverage']['complete'] is False
    assert payload.get('error') and '_coverage' not in payload
    # The original adapter dropped typed diagnostics. Reuse canonical generic
    # failure semantics, never parse its error prose or pretend this selected
    # normalized subset is the complete accepted envelope/payload digest.
    value = {'sources': {'unresolved': [{'provider': 'noaa',
              'reason_code': 'provider_error', 'http_status': None}]}}
    result = unit().load_source_reliability(value, root.parents[1])
    assert result['patterns'][0]['source'] == 'noaa'
    assert result['patterns'][0]['current_attribution'] == 'unresolved'
    assert result['patterns'][0]['observation_id'] is None
    assert result['patterns'][0]['current_empirical_evidence'] is False
    assert result['review_required'] is True


def test_operational_report_integration_uses_validated_payload_digest_and_safe_markdown(tmp_path):
    from tradingagents.strategies.orchestration.operational_report import (
        build_operational_report, render_operational_report,
    )
    # Missing required accepted inputs must leave learning unavailable, without
    # suppressing the report or mutating ledger/accepted source state.
    result = build_operational_report(tmp_path, 'gen_001', '2026-10-16', snapshot_guaranteed=True)
    assert result['source_reliability']['assessment_status'] == 'unavailable'
    assert 'Source reliability learning' in render_operational_report(result)
