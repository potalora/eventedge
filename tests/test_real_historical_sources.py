"""Historical acceptance uses actual retained observations, never invented baselines."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from historical.replay import AcceptanceError, offline_guard, run_acceptance


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / 'tests/historical/manifest.json'
PUBLIC = ROOT / 'tests/historical/public'


def test_actual_public_corpus_runs_native_consumers_and_declares_private_gaps():
    report = run_acceptance(MANIFEST, PUBLIC)
    assert report['public_scope_passed'] is True, report
    assert report['full_supported_scope_passed'] is False
    assert report['coverage_gaps']
    assert len(report['baseline_cases']) == 9
    assert {row['source'] for row in report['baseline_cases']} == {
        'noaa', 'usda', 'drought_monitor', 'cftc'}
    assert all(row['passed'] and row['fixture_sha256'] for row in report['baseline_cases'])
    assert not any(report['io_counters'].values())
    assert report['candidate']['head']
    assert isinstance(report['candidate']['dirty'], bool)
    assert len(report['candidate']['diff_sha256']) == 64
    assert any('WeatherAgStrategy' in api for api in report['native_apis'])


def test_actual_noaa_failure_is_an_expected_observation_not_fake_readiness():
    report = run_acceptance(MANIFEST, PUBLIC)
    cases = {row['id']: row for row in report['baseline_cases']}
    assert cases['native-reference-20261016-noaa']['observed_source_error'] is True
    assert cases['native-reference-20261016-noaa']['passed'] is True
    assert cases['gen022-reference-20261015-noaa']['observed_source_error'] is False
    raw = cases['noaa-official-static-202010']
    assert raw['boundary'] == 'raw_static_fixed_width'
    assert raw['first_two_days']['TMAX'] == [161, 144]
    assert raw['first_two_days']['TMIN'] == [67, 22]
    assert raw['first_two_days']['PRCP'] == [0, 0]


def test_derived_faults_are_separate_from_observed_baselines():
    report = run_acceptance(MANIFEST, PUBLIC)
    faults = report['controlled_mutations']
    assert faults and all(row['passed'] for row in faults)
    assert all(row['classification'] == 'controlled_mutation' for row in faults)
    assert all(row['parent_case'] for row in faults)
    assert any(row['outcome'] == 'invalid_condition' for row in faults)


def test_changed_real_fixture_is_rejected_before_processing(tmp_path):
    public = tmp_path / 'public'
    shutil.copytree(PUBLIC, public)
    target = public / 'noaa-usw00014933-2020-10-original.dly'
    content = target.read_bytes()
    target.write_bytes(bytes([content[0] ^ 1]) + content[1:])
    report = run_acceptance(MANIFEST, public)
    assert report['public_scope_passed'] is False
    assert report['full_supported_scope_passed'] is False
    case = next(row for row in report['baseline_cases'] if row['id'] == 'noaa-official-static-202010')
    assert case['reason'] == 'fixture_hash_mismatch'


def test_manifest_cannot_escape_the_explicit_corpus_directory(tmp_path):
    manifest = json.loads(MANIFEST.read_text())
    manifest['public_cases'][0]['path'] = '../outside.json'
    changed = tmp_path / 'manifest.json'
    changed.write_text(json.dumps(manifest))
    report = run_acceptance(changed, PUBLIC)
    assert report['public_scope_passed'] is False
    assert report['baseline_cases'][0]['reason'] == 'unsafe_fixture_path'


def test_required_full_scope_without_private_corpus_fails_and_writes_honest_report(tmp_path):
    report_path = tmp_path / 'report.json'
    run = subprocess.run([sys.executable, str(ROOT / 'scripts/run_historical_source_acceptance.py'),
        '--require-full-supported-scope', '--report', str(report_path)],
        capture_output=True, text=True, timeout=30)
    assert run.returncode != 0
    report = json.loads(report_path.read_text())
    assert report['public_scope_passed'] is True
    assert report['full_supported_scope_passed'] is False
    assert 'private_corpus_missing' in report['coverage_gaps']


def test_missing_private_members_are_not_a_skipped_success(tmp_path):
    report = run_acceptance(MANIFEST, PUBLIC, private_corpus=tmp_path)
    assert report['full_supported_scope_passed'] is False
    assert any('private_fixture_missing' in gap for gap in report['coverage_gaps'])
    assert report['private_cases'] and not any(row['passed'] for row in report['private_cases'])


@pytest.mark.parametrize('field', ['public_cases', 'private_captures', 'required_sources'])
def test_manifest_cannot_remove_required_scope_and_pass_vacuously(tmp_path, field):
    manifest = json.loads(MANIFEST.read_text())
    manifest[field] = []
    changed = tmp_path / 'manifest.json'
    changed.write_text(json.dumps(manifest))
    report = run_acceptance(changed, PUBLIC)
    assert report['public_scope_passed'] is False
    assert report['full_supported_scope_passed'] is False
    assert 'invalid_manifest' in report['coverage_gaps']


def test_manifest_cannot_relabel_normalized_observations_as_raw_wire(tmp_path):
    manifest = json.loads(MANIFEST.read_text())
    manifest['public_cases'][0]['boundary'] = 'raw_http'
    changed = tmp_path / 'manifest.json'
    changed.write_text(json.dumps(manifest))
    report = run_acceptance(changed, PUBLIC)
    assert report['public_scope_passed'] is False
    assert 'invalid_manifest' in report['coverage_gaps']


def test_verified_original_bytes_are_pinned_before_native_envelope_read(tmp_path, monkeypatch):
    from historical import replay
    from tradingagents.strategies.orchestration.source_inputs import SourceInputStore
    manifest = json.loads(MANIFEST.read_text())
    original = (PUBLIC / 'noaa-usw00014933-2020-10-original.dly').read_bytes()
    external = tmp_path / 'mutable-external-original'
    external.write_bytes(original)
    observed = []
    def replaced_after_hash(directory, case):
        if case['id'] == manifest['private_wire']['id']:
            raise AcceptanceError('controlled_wire_not_under_test')
        # Controlled IO mutation, not a fabricated accepted source baseline.
        external.write_bytes(b'changed after verified read')
        return original, external
    def inspect_native_read(self, path, identity):
        observed.append(Path(path).read_bytes())
        assert Path(path) != external
        raise AcceptanceError('controlled_stop_before_parsing_fixed_width_as_envelope')
    monkeypatch.setattr(replay, '_read_fixture', replaced_after_hash)
    monkeypatch.setattr(SourceInputStore, '_read', inspect_native_read)
    replay._private_cases(manifest, tmp_path, set())
    assert observed == [original] * len(manifest['private_captures'])


def test_candidate_contract_digest_includes_session_and_event_identity():
    from dataclasses import replace
    from historical.replay import _candidate_digest
    from tradingagents.strategies.modules.base import Candidate
    # Explicit contract control, not an upstream observation/baseline fixture.
    control = Candidate(ticker='SPY', date='2026-10-15', direction='long', score=.5)
    assert _candidate_digest([control]) != _candidate_digest([replace(control, date='2026-10-16')])
    assert _candidate_digest([control]) != _candidate_digest([replace(control, event_key='controlled-change')])


def test_healthy_native_projection_cannot_silently_drop_real_inputs(monkeypatch):
    from tradingagents.strategies.orchestration import research_resilience
    monkeypatch.setattr(research_resilience, 'project_sources', lambda *args, **kwargs: ({}, {}))
    report = run_acceptance(MANIFEST, PUBLIC)
    assert report['public_scope_passed'] is False


def test_native_crop_consumer_cannot_replace_real_conditions_with_empty_results(monkeypatch):
    from tradingagents.strategies.data_sources import usda_source
    monkeypatch.setattr(usda_source, 'validated_condition_observations', lambda rows: (None, {}))
    report = run_acceptance(MANIFEST, PUBLIC)
    assert report['public_scope_passed'] is False


def test_cftc_available_at_consumer_cannot_silently_erase_retained_timestamp(monkeypatch):
    from tradingagents.strategies.orchestration import multi_strategy_engine
    monkeypatch.setattr(multi_strategy_engine, '_metadata_timestamp', lambda *args, **kwargs: None)
    report = run_acceptance(MANIFEST, PUBLIC)
    assert report['public_scope_passed'] is False


def test_offline_guard_blocks_actual_native_curl_before_loopback_provider_receives_it():
    from curl_cffi import Curl, CurlOpt
    called = threading.Event()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            called.set()
            self.send_response(200)
            self.end_headers()
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        curl = Curl()
        try:
            curl.setopt(CurlOpt.URL, f'http://127.0.0.1:{server.server_port}'.encode())
            curl.setopt(CurlOpt.TIMEOUT_MS, 1000)
            with offline_guard() as counters:
                with pytest.raises(AcceptanceError, match='offline_io_forbidden'):
                    curl.perform()
            assert counters['network_attempts'] == 1
        finally:
            curl.close()
        assert not called.is_set()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_fifo_fixture_is_rejected_without_waiting_for_writer(tmp_path):
    import os
    manifest = json.loads(MANIFEST.read_text())
    public = tmp_path / 'public'
    shutil.copytree(PUBLIC, public)
    first = public / manifest['public_cases'][0]['path']
    first.unlink()
    os.mkfifo(first)
    changed = tmp_path / 'manifest.json'
    changed.write_text(json.dumps(manifest))
    # The hard post-import guard makes a blocking special-file read decisive.
    code = '''
import signal, sys, json
from pathlib import Path
sys.path.insert(0, str(Path.cwd()/'tests'))
from historical.replay import _read_fixture, AcceptanceError
case=json.loads(Path(sys.argv[1]).read_text())['public_cases'][0]
signal.setitimer(signal.ITIMER_REAL, 2)
try:
    _read_fixture(sys.argv[2], case)
except AcceptanceError as error:
    assert str(error)=='fixture_not_regular'
else:
    raise AssertionError('FIFO accepted')
signal.setitimer(signal.ITIMER_REAL, 0)
'''
    result = subprocess.run([sys.executable, '-B', '-c', code, str(changed), str(public)],
                            cwd=ROOT, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr


def _award_contract_control(monkeypatch, *, legacy):
    """Synthetic boundary control; never represented as provider evidence."""
    import hashlib
    from historical import replay
    from tradingagents.strategies.data_sources import award_attribution_policy as policy
    from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
    def scope(version):
        body = {'crosswalk_version': version, 'counts': {'unresolved': 1}, 'awards': []}
        body['scope_sha256'] = hashlib.sha256(json.dumps(body, sort_keys=True,
            separators=(',', ':')).encode()).hexdigest()
        return body
    retained = scope('reviewed_2026-10-10' if legacy else 'reviewed_2026-10-22')
    current = scope('reviewed_2026-10-22')
    coverage = {'attribution_scope': retained}
    payload = {'coverage': coverage, 'data': {'coverage': coverage, 'contracts': []}}
    expected = {'classification': 'obsolete_proof_rejection' if legacy else 'current_positive',
        'crosswalk_version': retained['crosswalk_version'],
        'retained_scope_sha256': retained['scope_sha256'], 'counts': retained['counts']}
    if legacy:
        expected.update(current_crosswalk_version='reviewed_2026-10-22',
                        current_scope_sha256=current['scope_sha256'])
        def reject(*args, **kwargs):
            raise SourceFetchError('USASpending verified-target scope evidence invalid',
                                   reason_code='invalid_response')
        monkeypatch.setattr(policy, 'validate_attribution_scope', reject)
    else:
        monkeypatch.setattr(policy, 'validate_attribution_scope', lambda *a, **kw: retained)
    monkeypatch.setattr(policy, 'build_attribution_scope', lambda *a, **kw: current)
    return replay, policy, payload, expected


def test_obsolete_award_proof_is_rejected_explicitly_without_rewriting_original(monkeypatch):
    import copy
    replay, _, payload, expected = _award_contract_control(monkeypatch, legacy=True)
    original = copy.deepcopy(payload)
    result = replay._award_contract(payload, '2026-10-15', expected, set())
    assert result['classification'] == 'obsolete_proof_rejection'
    assert result['current_proof_valid'] is False
    assert payload == original


@pytest.mark.parametrize('change', ['unexpected_error', 'changed_original', 'changed_rebuild', 'accepted_old'])
def test_obsolete_award_expectation_cannot_mask_other_failures(monkeypatch, change):
    from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
    replay, policy, payload, expected = _award_contract_control(monkeypatch, legacy=True)
    if change == 'unexpected_error':
        def reject(*a, **kw):
            raise SourceFetchError('different invalid evidence', reason_code='invalid_response')
        monkeypatch.setattr(policy, 'validate_attribution_scope', reject)
    elif change == 'changed_original':
        payload['coverage']['attribution_scope']['counts'] = {'unresolved': 2}
    elif change == 'changed_rebuild':
        monkeypatch.setattr(policy, 'build_attribution_scope', lambda *a, **kw: {})
    else:
        monkeypatch.setattr(policy, 'validate_attribution_scope', lambda *a, **kw: payload['coverage']['attribution_scope'])
    with pytest.raises((AcceptanceError, SourceFetchError)):
        replay._award_contract(payload, '2026-10-15', expected, set())


@pytest.mark.parametrize('corruption', ['validation_error', 'altered_scope'])
def test_current_award_positive_requires_strict_validator_success(monkeypatch, corruption):
    from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
    replay, policy, payload, expected = _award_contract_control(monkeypatch, legacy=False)
    assert replay._award_contract(payload, '2026-10-26', expected, set())['current_proof_valid'] is True
    if corruption == 'altered_scope':
        payload['coverage']['attribution_scope']['counts'] = {'unresolved': 2}
    else:
        def reject(*a, **kw):
            raise SourceFetchError('USASpending verified-target scope evidence invalid', reason_code='invalid_response')
        monkeypatch.setattr(policy, 'validate_attribution_scope', reject)
    with pytest.raises((SourceFetchError, AcceptanceError)):
        replay._award_contract(payload, '2026-10-26', expected, set())


def test_manifest_cannot_remove_or_relabel_current_positive_case(tmp_path):
    manifest = json.loads(MANIFEST.read_text())
    positive = [case for case in manifest['private_captures']
                if case['expected'].get('award_contract', {}).get('classification') == 'current_positive']
    assert len(positive) == 1
    for relabel in (False, True):
        changed = json.loads(MANIFEST.read_text())
        if relabel:
            next(c for c in changed['private_captures'] if c['id'] == positive[0]['id'])['expected']['award_contract']['classification'] = 'obsolete_proof_rejection'
        else:
            changed['private_captures'] = [c for c in changed['private_captures'] if c['id'] != positive[0]['id']]
        path = tmp_path / 'manifest.json'
        path.write_text(json.dumps(changed))
        result = run_acceptance(path, PUBLIC)
        assert result['full_supported_scope_passed'] is False
        assert result['coverage_gaps'] == ['invalid_manifest']


def test_full_scope_requires_current_positive_attribution_and_all_source_contracts(monkeypatch):
    from historical import replay
    required = sorted(replay.REQUIRED_SOURCES)
    manifest = json.loads(MANIFEST.read_text())
    base = {'id': replay.CURRENT_AWARD_CASE, 'passed': True,
            'award_contract': {'classification': 'current_positive', 'current_proof_valid': True},
            'represented_sources': required}
    for missing in ('current_proof', 'source_contract'):
        row = json.loads(json.dumps(base))
        if missing == 'current_proof':
            row['award_contract']['current_proof_valid'] = False
        else:
            row['represented_sources'].remove('usaspending')
        rows = [{'id': case['id'], 'passed': True} for case in manifest['private_captures']
                if case['id'] != replay.CURRENT_AWARD_CASE]
        rows += [{'id': manifest['private_wire']['id'], 'passed': True}, row]
        monkeypatch.setattr(replay, '_private_cases', lambda *a, _rows=rows, **kw: _rows)
        report = run_acceptance(MANIFEST, PUBLIC, private_corpus=PUBLIC)
        assert report['public_scope_passed'] is True
        assert report['full_supported_scope_passed'] is False
