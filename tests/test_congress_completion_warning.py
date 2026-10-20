"""Recent display-only audit gaps preserve truthful coverage and paper continuity."""
import hashlib
import io
import json
from types import SimpleNamespace
import zipfile

import pytest

import test_stabilization_completion as completed
import test_congress_disclosure_audit as audit_tests
from test_operational_report import native_evidence, _report
from tradingagents.strategies.orchestration.operating_contract import profile_contract

POLICY = 'recent_snapshot_warning_v1'


@pytest.fixture
def audit_session(native_evidence, tmp_path, monkeypatch, request):
    policy, fault = getattr(request, 'param', (True, None))
    config = json.loads(completed.PROFILE.read_text())
    if policy:
        config['autoresearch']['congress_audit_continuity_policy'] = POLICY
    path = tmp_path / 'profile.json'
    body = (json.dumps(config, sort_keys=True, indent=2) + '\n').encode()
    path.write_bytes(body)
    contract = profile_contract(config, profile_sha256=hashlib.sha256(body).hexdigest())
    monkeypatch.setattr(completed, 'PROFILE', path)
    monkeypatch.setattr(completed, 'profile', lambda: contract)
    original_raw = audit_tests.synthetic_raw
    def gap_raw():
        raw = original_raw()
        with zipfile.ZipFile(io.BytesIO(raw['house-2026FD.ZIP'])) as archive:
            xml = archive.read('2026FD.xml').replace(b'</FinancialDisclosure>',
                b'<Member><DocID>99999</DocID><FilingType>P</FilingType>'
                b'<FilingDate>10/1/2026</FilingDate></Member></FinancialDisclosure>')
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w') as archive:
            archive.writestr('2026FD.xml', xml)
            archive.writestr('2026FD.txt', 'synthetic')
        raw['house-2026FD.ZIP'] = output.getvalue()
        return raw
    monkeypatch.setattr(audit_tests, 'synthetic_raw', gap_raw)
    original_payload = completed._payload
    def payload(*args, **kwargs):
        data, now = original_payload(*args, **kwargs)
        congress = data['congress']
        congress['error'] = 'Congress audit House filing reconciliation gap [invalid_response]'
        congress['_coverage'] = {'status': 'partial', 'reason_code': 'invalid_response',
                                'http_status': None, 'attempts': 0}
        congress['_request_diagnostics'] = [
            {'provider': 'congress', 'operation': 'disclosure_audit', 'reason_code': None,
             'http_status': None, 'attempts': 1, 'recovered': False}
            for _ in congress['audit_snapshot']['raw_artifacts']]
        if fault == 'other_source':
            data['finnhub']['error'] = 'provider unavailable'
        return data, now
    monkeypatch.setattr(completed, '_payload', payload)
    return completed.completed_supported_session.__wrapped__(
        native_evidence, tmp_path, SimpleNamespace(param=()))


def test_recent_audit_gap_allows_paper_continuity_with_explicit_warning(audit_session):
    from tradingagents.strategies.orchestration.operational_report import render_operational_report
    repo, state, contract = audit_session
    report = _report(repo)
    assert report['outcome'] == 'degraded'
    assert report['input_coverage_valid'] is False
    assert report['sources']['unresolved'] == [
        {'provider': 'congress', 'reason_code': 'invalid_response', 'http_status': None}]
    assert report['completion']['continuity_eligible'] is True, report
    assert report['completion']['workflow_complete'] is True
    warning, = report['completion']['audit_warnings']
    assert warning['policy'] == POLICY
    assert warning['house_missing_filings'] == 1
    assert 'Congress audit warning' in render_operational_report(report)


@pytest.mark.parametrize('audit_session', [(False, None), (True, 'other_source')], indirect=True)
def test_legacy_policy_and_required_source_failure_still_block(audit_session):
    repo, state, contract = audit_session
    report = _report(repo)
    assert report['completion']['continuity_eligible'] is False, report


def test_v3_profile_changes_only_congress_warning_policy():
    v2_path = completed.PROFILE.with_name('stabilization-v2.json')
    v3_path = completed.PROFILE.with_name('stabilization-v3.json')
    v2 = json.loads(v2_path.read_text())
    v3 = json.loads(v3_path.read_text())
    assert v3['autoresearch'].pop('congress_audit_continuity_policy') == POLICY
    assert v3 == v2
    v3['autoresearch']['congress_audit_continuity_policy'] = POLICY
    earlier = profile_contract(v2, profile_sha256=hashlib.sha256(v2_path.read_bytes()).hexdigest())
    newer = profile_contract(v3, profile_sha256=hashlib.sha256(v3_path.read_bytes()).hexdigest())
    assert earlier.exclusion_scope_sha256 != newer.exclusion_scope_sha256
    assert earlier.strategies == newer.strategies


def _accepted_completion_facts(repo, monkeypatch):
    """Capture the canonical reader's actual facts before adversarial mutation."""
    from tradingagents.strategies.orchestration import operating_contract as contract
    captured = {}
    original = contract.evaluate_completion
    def evaluate(profile, facts):
        captured.update(profile=profile, facts=facts)
        return original(profile, facts)
    monkeypatch.setattr(contract, 'evaluate_completion', evaluate)
    report = _report(repo)
    assert report['completion']['continuity_eligible'] is True, report
    return captured['profile'], captured['facts'], original


@pytest.mark.parametrize('unresolved', [None, [],
    [{'provider': 'congress', 'reason_code': 'timeout', 'http_status': None}],
    [{'provider': 'congress', 'reason_code': 'invalid_response', 'http_status': 503}],
    [{'provider': 'congress', 'reason_code': 'invalid_response', 'http_status': None}] * 2,
    [{'provider': 'congress', 'reason_code': 'invalid_response', 'http_status': None,
      'invented_warning': True}],
])
def test_warning_requires_exact_single_visible_unresolved_diagnostic(audit_session, monkeypatch, unresolved):
    from copy import deepcopy
    repo, state, contract = audit_session
    profile, facts, evaluate = _accepted_completion_facts(repo, monkeypatch)
    facts = {**facts, 'report': deepcopy(facts['report'])}
    if unresolved is None:
        facts['report']['sources'].pop('unresolved')
    else:
        facts['report']['sources']['unresolved'] = unresolved
    result = evaluate(profile, facts)
    assert result['workflow_complete'] is False
    assert result['continuity_eligible'] is False
    assert 'congress_audit_diagnostics_invalid' in result['reason_codes']


@pytest.mark.parametrize('fault', ['evidence', 'accounting', 'staging', 'candidate',
    'volatility', 'source_health', 'committee_model', 'health_roster', 'screening'])
def test_warning_cannot_bypass_required_completion_gates(audit_session, monkeypatch, fault):
    from copy import deepcopy
    repo, state, contract = audit_session
    profile, original, evaluate = _accepted_completion_facts(repo, monkeypatch)
    facts = {**original, 'report': deepcopy(original['report']),
        'committees': deepcopy(original['committees']), 'health': deepcopy(original['health'])}
    report = facts['report']
    if fault == 'evidence':
        report['evidence_complete'] = False
    elif fault == 'accounting':
        report['cohorts'][contract.roster[0]]['accounting_valid'] = False
    elif fault == 'staging':
        report['cohorts'][contract.roster[0]]['staging_complete'] = False
    elif fault == 'candidate':
        report['candidate_input_issues'] = [{'reason': 'invalid_execution_price'}]
    elif fault == 'volatility':
        report['volatility']['valid'] = False
    elif fault == 'source_health':
        report['source_health_failures'] = [{'sources': ['finnhub']}]
    elif fault == 'committee_model':
        facts['committees'][contract.roster[0]]['status']['model_coverage'] = {'complete': False}
    elif fault == 'health_roster':
        facts['health'].pop()
    else:
        facts.pop('screening')
    result = evaluate(profile, facts)
    assert result['workflow_complete'] is False
    assert result['continuity_eligible'] is False
    assert result['reason_codes']
    # A permitted audit warning never turns another gate failure into success.
    assert result['audit_warnings'][0]['house_missing_filings'] == 1


@pytest.mark.parametrize('fault', ['unknown_policy', 'missing_display_policy',
    'enabled_congress', 'live_execution', 'execution_enabled', 'decision_shadow'])
def test_warning_profile_keeps_paper_execution_and_exclusion_guards(fault):
    config = json.loads(completed.PROFILE.read_text())
    config['autoresearch']['congress_audit_continuity_policy'] = POLICY
    if fault == 'unknown_policy':
        config['autoresearch']['congress_audit_continuity_policy'] = 'ignore_audit_failures'
    elif fault == 'missing_display_policy':
        config['autoresearch'].pop('congress_disclosure_policy')
    elif fault == 'enabled_congress':
        config['autoresearch']['disabled_strategies'].pop('congressional_trades')
    elif fault == 'live_execution':
        config['execution']['mode'] = 'live'
    elif fault == 'execution_enabled':
        config['execution']['execution_enabled'] = True
    else:
        config['decision_shadow']['enabled'] = True
    with pytest.raises(ValueError, match='invalid_stabilization_profile'):
        profile_contract(config, profile_sha256='a' * 64)


def test_historical_warning_uses_frozen_acquisition_and_keeps_evidence_readonly(audit_session, monkeypatch):
    import gc
    from tradingagents.strategies.data_sources import congress_disclosure_audit as audit
    repo, state, contract = audit_session
    def unexpected_wallclock():
        raise AssertionError('historical report consulted the current provider clock')
    monkeypatch.setattr(audit, '_utcnow', unexpected_wallclock)
    gc.collect()
    # SQLite mode=ro may create coordination sidecars in this writable fixture;
    # authoritative database and accepted-input bytes must remain identical.
    before = {str(path.relative_to(repo)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in repo.rglob('*') if path.is_file() and not path.name.endswith(('-wal', '-shm'))}
    first = _report(repo)
    second = _report(repo)
    after = {str(path.relative_to(repo)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in repo.rglob('*') if path.is_file() and not path.name.endswith(('-wal', '-shm'))}
    assert before == after
    assert first == second
    warning, = first['completion']['audit_warnings']
    assert warning['envelope_acquired_at'] == '2026-10-06T23:00:00+00:00'
    assert warning['publisher_age_seconds'] == 0
    assert first['completion']['continuity_eligible'] is True


@pytest.mark.parametrize('field,value', [('outcome', 'clean'), ('input_coverage_valid', True)])
def test_warning_cannot_publish_contradictory_clean_or_full_coverage(audit_session, monkeypatch, field, value):
    from copy import deepcopy
    repo, state, contract = audit_session
    profile, facts, evaluate = _accepted_completion_facts(repo, monkeypatch)
    facts = {**facts, 'report': deepcopy(facts['report'])}
    facts['report'][field] = value
    result = evaluate(profile, facts)
    assert result['workflow_complete'] is False
    assert result['continuity_eligible'] is False
    assert 'congress_audit_diagnostics_invalid' in result['reason_codes']
