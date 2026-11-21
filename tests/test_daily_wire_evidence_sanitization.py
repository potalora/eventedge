"""Offline regression at the capture-to-report boundary; no external I/O."""
import json

import pytest

from tradingagents.strategies.orchestration import operational_report as report
from tradingagents.strategies.orchestration.run_evidence import persist_run_evidence
from tradingagents.strategies.orchestration.run_outcome import (
    DAILY_RESULT_PREFIX, DAILY_RESULT_WIRE_VERSION,
)

SESSION = '2026-11-18'
COMMIT = 'a' * 40


def _evidence(stdout):
    return {
        'schema_version': 1, 'generation_id': 'gen_001',
        'generation_commit': COMMIT, 'requested_session': SESSION,
        'action': 'daily', 'preflight_mode': None,
        'started_at': f'{SESSION}T22:00:00+00:00',
        'finished_at': f'{SESSION}T22:01:00+00:00',
        'process_return_code': 0, 'process_status': 'completed',
        'stdout': stdout, 'stderr': 'Authorization: Bearer synthetic-secret',
        'result': {'outcome': 'degraded', 'success': False,
                   'input_coverage_valid': True, 'execution_valid': True,
                   'source_health_failures': []},
    }


def _wire():
    rows = {name: {'error': False, 'execution_valid': True, 'staging_valid': True,
                   'degraded': True, 'input_coverage_valid': True,
                   'diagnostics': {'present': True, 'password': 'synthetic-secret',
                                   'detail': 'value=true'}}
            for name in report.EXPECTED_COHORTS}
    return DAILY_RESULT_PREFIX + json.dumps({
        'wire_version': DAILY_RESULT_WIRE_VERSION, 'cohort_results': rows,
    })


def test_persisted_daily_wire_keeps_boolean_tokens_and_redacts_strings(tmp_path, monkeypatch):
    monkeypatch.setenv('FIXTURE_CREDENTIAL', 'true')
    evidence = _evidence('before raw=true\n' + _wire() + '\n')
    raw = persist_run_evidence(tmp_path, evidence).read_text()
    stdout = json.loads(raw)['stdout']
    assert 'synthetic-secret' not in raw
    assert 'before raw=<redacted>' in stdout
    for row in report._wire(stdout).values():
        assert row['diagnostics'] == {'present': True, 'password': '<redacted>',
                                      'detail': 'value=<redacted>'}
    diagnostics = []
    _, latest = report._attempts(tmp_path, 'gen_001', SESSION, COMMIT,
                                 lambda *args: diagnostics.append(args))
    assert latest is not None and latest['outcome'] == 'degraded'
    assert diagnostics == []


@pytest.mark.parametrize('damage', ['invalid_json', 'invalid_lifecycle', 'not_final',
                                    'duplicate_marker'])
def test_persisted_invalid_daily_wire_is_not_promoted(tmp_path, monkeypatch, damage):
    monkeypatch.setenv('FIXTURE_CREDENTIAL', 'true')
    wire = _wire()
    if damage == 'invalid_json':
        wire = wire.replace('"present": true', '"present": <redacted>', 1)
    elif damage == 'invalid_lifecycle':
        wire = wire.replace('"execution_valid": true', '"execution_valid": "true"', 1)
    elif damage == 'not_final':
        wire += '\ntrailing diagnostic'
    elif damage == 'duplicate_marker':
        wire += '\n' + wire
    path = persist_run_evidence(tmp_path, _evidence(wire))
    diagnostics = []
    report._attempts(tmp_path, 'gen_001', SESSION, COMMIT,
                     lambda *args: diagnostics.append(args))
    assert diagnostics == [('attempt_wire_invalid', path.name)]
    assert 'synthetic-secret' not in path.read_text()
