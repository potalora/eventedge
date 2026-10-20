"""Recent display-only gaps remain explicit and never excuse trading failures."""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import hashlib
import io
import json
import zipfile

import pytest

from test_congress_disclosure_audit import synthetic_raw, REVISION

CONFIG = {'congress_audit_continuity_policy': 'recent_snapshot_warning_v1',
    'congress_disclosure_policy': 'display_audit_only_v1',
    'stabilization_profile_policy': 'supported_sixteen_books_v1',
    'disabled_strategies': {'congressional_trades': 'display_audit_only_v1'}}
SESSION = '2026-10-19'
ACQUIRED = datetime(2026, 10, 20, 6, 50, tzinfo=timezone.utc)
PUBLISHED = datetime(2026, 10, 19, 12, 2, 23, 913000, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    import requests
    import socket
    def blocked(*args, **kwargs):
        pytest.fail('continuity policy is a pure accepted-evidence reader')
    monkeypatch.setattr(requests.sessions.Session, 'request', blocked)
    monkeypatch.setattr(socket.socket, 'connect', blocked)
    monkeypatch.setattr(socket, 'create_connection', blocked)


def payload(*, gap='missing', generated=PUBLISHED, checked=None):
    """Full native synthetic schema reproduces the observed Oct16 missing PTR."""
    from tradingagents.strategies.data_sources import congress_disclosure_audit as audit
    raw = synthetic_raw()
    manifest = json.loads(raw['snapshot.json'])
    manifest.update(generatedAt=generated.isoformat(), lastSourceCheckAt=(checked or generated).isoformat())
    raw['snapshot.json'] = json.dumps(manifest).encode()
    if gap is not None:
        with zipfile.ZipFile(io.BytesIO(raw['house-2026FD.ZIP'])) as source:
            xml, text = source.read('2026FD.xml'), source.read('2026FD.txt')
        if gap == 'missing':
            xml = xml.replace(b'</FinancialDisclosure>', b'<Member><DocID>20035490</DocID><FilingType>P</FilingType><FilingDate>10/16/2026</FilingDate></Member></FinancialDisclosure>')
        else:
            xml = b'<FinancialDisclosure></FinancialDisclosure>'
        out = io.BytesIO()
        with zipfile.ZipFile(out, 'w') as target:
            target.writestr('2026FD.xml', xml)
            target.writestr('2026FD.txt', text)
        raw['house-2026FD.ZIP'] = out.getvalue()
    prefix = f'{audit.HF}/datasets/{audit.DATASET}/resolve/{REVISION}'
    artifacts = [audit._record('revision_metadata', audit.REVISION_URL,
        json.dumps({'sha': REVISION, 'id': audit.DATASET}).encode()),
        audit._record('manifest', prefix + '/snapshot.json', raw['snapshot.json']),
        audit._record('house_index', 'https://disclosures-clerk.house.gov/public_disc/financial-pdfs/2026FD.ZIP', raw['house-2026FD.ZIP'])]
    for table, name in [('political_filings', 'filings-2026.parquet'), ('political_trades', 'trades-2026.parquet')]:
        artifacts.append(audit._record('parquet', prefix + f'/data/{table}/2026-00000-of-00001.parquet', raw[name]))
    result = audit._assemble(artifacts, revision=REVISION, start='2026-09-19', end=SESSION,
        acquired_at=(ACQUIRED - timedelta(seconds=10)).isoformat())
    result['_request_diagnostics'] = [{'provider': 'congress', 'operation': 'disclosure_audit',
        'reason_code': None, 'http_status': None, 'attempts': 1, 'recovered': False} for _ in artifacts]
    if gap is not None:
        result.update(error='Congress audit House filing reconciliation gap [invalid_response]',
            _coverage={'status': 'partial', 'reason_code': 'invalid_response', 'http_status': None, 'attempts': 0})
    return result


def test_policy_is_explicit_and_preserves_legacy_behavior():
    from tradingagents.strategies.orchestration.congress_audit_continuity import configured, warning_for_source
    assert configured({}) is False
    assert configured({'congress_audit_continuity_policy': None}) is False
    assert configured(CONFIG) is True
    assert warning_for_source(None, {}, SESSION, None) is None


@pytest.mark.parametrize('change', [
    {'congress_audit_continuity_policy': 'ignore_errors'},
    {'congress_audit_continuity_policy': True},
    {'congress_disclosure_policy': None}, {'stabilization_profile_policy': None},
    {'disabled_strategies': {}}, {'disabled_strategies': []},
    {'disabled_strategies': {'congressional_trades': 'anything'}},
])
def test_unknown_or_incomplete_policy_fails_closed(change):
    from tradingagents.strategies.orchestration.congress_audit_continuity import configured
    with pytest.raises(ValueError, match='congress_audit_continuity'):
        configured({**CONFIG, **change})


def test_yesterdays_missing_house_audit_becomes_visible_warning_without_mutation():
    from tradingagents.strategies.orchestration.congress_audit_continuity import warning_for_source
    from tradingagents.strategies.orchestration.source_inputs import SourceInputStore, source_codec_limits
    original = payload()
    before = deepcopy(original)
    warning = warning_for_source(original, CONFIG, SESSION, ACQUIRED)
    assert warning['policy'] == 'recent_snapshot_warning_v1'
    assert warning['source'] == 'congress'
    assert warning['house_missing_filings'] == 1 and warning['house_additional_filings'] == 0
    assert warning['house_missing_ids'] == ['20035490']
    assert warning['publisher_generated_at'] == PUBLISHED.isoformat()
    assert warning['publisher_age_seconds'] == (ACQUIRED - PUBLISHED).total_seconds()
    assert warning['freshness_limit_seconds'] == 172800
    assert warning['research_coverage_complete'] is False
    assert warning['signals_enabled'] is warning['model_context_enabled'] is warning['order_authority'] is False
    assert warning['source_payload_sha256'] == hashlib.sha256(SourceInputStore.encode(original,
        **source_codec_limits({'autoresearch': CONFIG}, source='congress')).encode()).hexdigest()
    assert original == before and original['coverage']['complete'] is False


def test_frozen_acquisition_clock_is_authoritative_not_report_wall_time(monkeypatch):
    from tradingagents.strategies.data_sources import congress_disclosure_audit as audit
    from tradingagents.strategies.orchestration.congress_audit_continuity import warning_for_source
    monkeypatch.setattr(audit, '_utcnow', lambda: pytest.fail('report clock must not reinterpret accepted evidence'))
    assert warning_for_source(payload(), CONFIG, SESSION, ACQUIRED)['house_missing_filings'] == 1
    assert warning_for_source(payload(), CONFIG, date.fromisoformat(SESSION), ACQUIRED.isoformat())['house_missing_filings'] == 1


def test_healthy_complete_or_unrelated_provider_failure_is_not_a_gap_warning():
    from tradingagents.strategies.orchestration.congress_audit_continuity import warning_for_source
    assert warning_for_source(payload(gap=None), CONFIG, SESSION, ACQUIRED) is None
    assert warning_for_source({'error': 'timeout', '_coverage': {'status': 'failed'}}, CONFIG, SESSION, ACQUIRED) is None
    assert warning_for_source(payload(gap='additional'), CONFIG, SESSION, ACQUIRED) is None


@pytest.mark.parametrize('change', [
    lambda p: p.update(error='arbitrary provider failure'),
    lambda p: p.update(errors={'timeout': True}),
    lambda p: p['_coverage'].update(reason_code='timeout'),
    lambda p: p['_coverage'].update(http_status=503),
    lambda p: p['_coverage'].update(attempts=1),
    lambda p: p['_request_diagnostics'][0].update(reason_code='http_error', http_status=503),
    lambda p: p['_request_diagnostics'][0].update(attempts=True),
    lambda p: p['_request_diagnostics'].append(dict(p['_request_diagnostics'][0])),
    lambda p: p.pop('_request_diagnostics'),
    lambda p: p['coverage'].update(complete=True),
    lambda p: p['coverage'].update(acquisition_complete=False),
    lambda p: p['coverage'].update(integrity_valid=False),
    lambda p: p['audit_snapshot'].update(content_sha256='0' * 64),
    lambda p: p['audit_snapshot'].update(model_context_enabled=True),
])
def test_forged_flags_errors_or_integrity_failures_never_become_warning(change):
    from tradingagents.strategies.orchestration.congress_audit_continuity import warning_for_source
    original = payload()
    change(original)
    with pytest.raises(ValueError):
        warning_for_source(original, CONFIG, SESSION, ACQUIRED)


@pytest.mark.parametrize('age, allowed', [(172800, True), (172800.000001, False)])
@pytest.mark.parametrize('field', ['generated', 'checked'])
def test_unchanged_48_hour_publisher_limits_are_inclusive(age, allowed, field):
    from tradingagents.strategies.orchestration.congress_audit_continuity import warning_for_source
    options = {field: ACQUIRED - timedelta(seconds=age)}
    original = payload(**options)
    if allowed:
        assert warning_for_source(original, CONFIG, SESSION, ACQUIRED)['house_missing_filings'] == 1
    else:
        with pytest.raises(ValueError):
            warning_for_source(original, CONFIG, SESSION, ACQUIRED)


@pytest.mark.parametrize('session, acquired', [
    ('2026-10-16', ACQUIRED), (SESSION, ACQUIRED.replace(tzinfo=None)),
    (SESSION, ACQUIRED - timedelta(seconds=11)),
])
def test_wrong_window_or_invalid_acquisition_clock_is_blocking(session, acquired):
    from tradingagents.strategies.orchestration.congress_audit_continuity import warning_for_source
    with pytest.raises(ValueError):
        warning_for_source(payload(), CONFIG, session, acquired)


@pytest.mark.parametrize('attempts', [2, 3, 5])
def test_successfully_recovered_native_request_stays_an_audit_gap_warning(attempts):
    from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
    from tradingagents.strategies.data_sources.request_policy import provider_budget, provider_call
    from tradingagents.strategies.orchestration.congress_audit_continuity import warning_for_source
    clock, calls, diagnostics = [0.], [], []
    def sleep(seconds):
        clock[0] += seconds
    def request():
        calls.append(1)
        if len(calls) < attempts:
            raise SourceFetchError('constructed transient timeout', reason_code='timeout')
        return b'constructed successful artifact bytes'
    with provider_budget('congress', 1000, clock=lambda: clock[0], sleep=sleep,
            random_fn=lambda: 0., max_attempts=attempts, limits=(), diagnostics=diagnostics):
        assert provider_call('congress', 'disclosure_audit', request) == b'constructed successful artifact bytes'
    assert diagnostics == [{'provider': 'congress', 'operation': 'disclosure_audit',
        'reason_code': None, 'http_status': None, 'attempts': attempts, 'recovered': True}]
    original = payload()
    original['_request_diagnostics'][0] = diagnostics[0]
    before = deepcopy(original)
    assert warning_for_source(original, CONFIG, SESSION, ACQUIRED)['house_missing_filings'] == 1
    assert original == before


@pytest.mark.parametrize('change', [
    {'attempts': 0}, {'attempts': 6, 'recovered': True}, {'attempts': True},
    {'attempts': 1.0}, {'attempts': '2', 'recovered': True},
    {'attempts': 2, 'recovered': False}, {'attempts': 1, 'recovered': True},
    {'attempts': 2, 'recovered': 1},
    {'attempts': 2, 'recovered': True, 'reason_code': 'timeout'},
    {'attempts': 2, 'recovered': True, 'reason_code': 'http_error', 'http_status': 503},
])
def test_bad_or_unresolved_recovery_diagnostics_are_blocking(change):
    from tradingagents.strategies.orchestration.congress_audit_continuity import warning_for_source
    original = payload()
    original['_request_diagnostics'][0].update(change)
    with pytest.raises(ValueError):
        warning_for_source(original, CONFIG, SESSION, ACQUIRED)
