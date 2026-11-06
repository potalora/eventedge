"""Completed replay binds every durable disclosure summary to frozen raw evidence."""
from copy import deepcopy
from datetime import date
import io
import json
from types import SimpleNamespace
import zipfile

import pytest
import requests

from test_congress_disclosure_audit import synthetic_raw, REVISION, NOW
from tradingagents.strategies.data_sources import congress_disclosure_audit as audit
from tradingagents.strategies.orchestration import scoped_replay
from tradingagents.strategies.orchestration.congress_policy import declaration

SESSION = '2026-10-09'
CONFIG = {'congress_disclosure_policy': audit.POLICY,
          'disabled_strategies': {'congressional_trades': audit.POLICY}}


@pytest.fixture(autouse=True)
def no_external_calls(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('replay must not acquire provider or model evidence')
    monkeypatch.setattr(requests.Session, 'request', forbidden)
    monkeypatch.setattr(requests.Session, 'send', forbidden)
    monkeypatch.setattr(scoped_replay, 'validate_portfolio_targets', lambda *args: None)
    monkeypatch.setattr(scoped_replay, 'source_scope_evidence', lambda *args, **kwargs: ({}, {}, {}))


def frozen_payload(*, gap=False):
    raw = synthetic_raw()
    if gap:
        with zipfile.ZipFile(io.BytesIO(raw['house-2026FD.ZIP'])) as archive:
            xml = archive.read('2026FD.xml').replace(b'</FinancialDisclosure>',
                b'<Member><DocID>99999</DocID><FilingType>P</FilingType>'
                b'<FilingDate>10/1/2026</FilingDate></Member></FinancialDisclosure>')
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w') as archive:
            archive.writestr('2026FD.xml', xml)
            archive.writestr('2026FD.txt', 'synthetic')
        raw['house-2026FD.ZIP'] = output.getvalue()
    root = f'{audit.HF}/datasets/{audit.DATASET}/resolve/{REVISION}'
    artifacts = [audit._record('revision_metadata', audit.REVISION_URL,
        json.dumps({'id': audit.DATASET, 'sha': REVISION}).encode()),
        audit._record('manifest', root + '/snapshot.json', raw['snapshot.json'])]
    for table, name in [('political_filings', 'filings-2026.parquet'),
                        ('political_trades', 'trades-2026.parquet')]:
        artifacts.append(audit._record('parquet', root + f'/data/{table}/2026-00000-of-00001.parquet', raw[name]))
    artifacts.append(audit._record('house_index',
        'https://disclosures-clerk.house.gov/public_disc/financial-pdfs/2026FD.ZIP', raw['house-2026FD.ZIP']))
    return audit._assemble(artifacts, revision=REVISION, start='2026-09-09',
                           end=SESSION, acquired_at=NOW.isoformat())


def case(*, failed=False, gap=False):
    payload = {'error': 'audit acquisition failed'} if failed else frozen_payload(gap=gap)
    if gap:
        payload['error'] = 'retained House reconciliation gap'
    data = {**declaration(CONFIG), 'congress': payload}
    summary = None if failed else audit.audit_summary(payload)
    rows = []
    for horizon in ('30d', '3m'):
        evidence = {'reason': audit.POLICY, 'disclosure_policy': audit.POLICY,
                    'data_sources': ['congress', 'yfinance'], 'candidate_count': 0}
        if summary is not None:
            evidence['source_scope_limits'] = {'congress': deepcopy(summary)}
        rows.append(SimpleNamespace(epoch_id='epoch', session=date.fromisoformat(SESSION),
            policy_id='foundation-' + horizon, strategy='congressional_trades',
            status='disabled_by_policy', signal_count=0, evidence=evidence))
    reads = []
    def read(epoch, *, session, limit):
        assert (epoch, session, limit) == ('epoch', date.fromisoformat(SESSION), 1000)
        reads.append(1)
        return rows
    owner = SimpleNamespace(_base_config={'autoresearch': deepcopy(CONFIG)},
        _disabled_strategies=deepcopy(CONFIG['disabled_strategies']),
        cohorts=[{'config': SimpleNamespace(horizon=h)} for h in ('30d', '3m')],
        _policy_id_for_horizon=lambda horizon: 'foundation-' + horizon,
        _metric_store=SimpleNamespace(read_strategy_health=read))
    return data, owner, rows, reads


def replay(data, owner):
    scoped_replay.validate_replay_source_scopes(data, owner, SESSION, 'epoch', now=NOW)


@pytest.mark.parametrize('failed', [False, True])
def test_every_horizon_matches_frozen_audit_without_mutation_or_acquisition(failed):
    data, owner, rows, reads = case(failed=failed)
    before_data, before_rows = deepcopy(data), deepcopy(rows)
    replay(data, owner)
    assert reads == [1]
    assert data == before_data and rows == before_rows


@pytest.mark.parametrize('damage', ['missing_health', 'missing_summary', 'row_count',
    'digest', 'enabled', 'signal', 'candidate', 'reason', 'policy', 'sources',
    'epoch', 'session', 'duplicate', 'bool_count'])
def test_replay_rejects_missing_or_conflicting_congress_evidence_in_any_horizon(damage):
    data, owner, rows, _ = case()
    row = rows[-1]
    if damage == 'missing_health': rows.pop()
    if damage == 'missing_summary': row.evidence.pop('source_scope_limits')
    if damage == 'row_count': row.evidence['source_scope_limits']['congress']['printed_row_count'] = 999
    if damage == 'digest': row.evidence['source_scope_limits']['congress']['content_sha256'] = '0' * 64
    if damage == 'enabled': row.status = 'legitimate_no_event'
    if damage == 'signal': row.signal_count = 1
    if damage == 'candidate': row.evidence['candidate_count'] = 1
    if damage == 'reason': row.evidence['reason'] = 'other'
    if damage == 'policy': row.evidence.pop('disclosure_policy')
    if damage == 'sources': row.evidence['data_sources'] = ['congress']
    if damage == 'epoch': row.epoch_id = 'other'
    if damage == 'session': row.session = date(2026, 10, 8)
    if damage == 'duplicate': rows.append(deepcopy(row))
    if damage == 'bool_count': row.evidence['source_scope_limits']['congress']['printed_row_count'] = True
    with pytest.raises(ValueError, match='replay'):
        replay(data, owner)


def test_failed_acquisition_cannot_have_spurious_durable_audit_summary():
    data, owner, rows, _ = case(failed=True)
    rows[0].evidence['source_scope_limits'] = {'congress': audit.audit_summary(frozen_payload())}
    with pytest.raises(ValueError, match='replay'):
        replay(data, owner)


def test_retained_house_gap_keeps_exact_partial_summary_on_replay():
    data, owner, rows, reads = case(gap=True)
    summary = rows[0].evidence['source_scope_limits']['congress']
    assert summary['status'] == 'partial_audit_gap' and summary['house_missing_filings'] == 1
    replay(data, owner)
    assert reads == [1] and data['congress']['coverage']['complete'] is False


def test_enabled_audit_requires_at_least_one_configured_horizon():
    data, owner, _, _ = case()
    owner.cohorts = []
    with pytest.raises(ValueError, match='replay'):
        replay(data, owner)


def test_legacy_no_scope_errors_still_needs_no_health_store():
    replay({}, SimpleNamespace(_base_config={'autoresearch': {}}))


@pytest.mark.parametrize('failed', [False, True])
@pytest.mark.parametrize('congress', [False, True])
def test_existing_court_failure_proof_remains_required(failed, congress, monkeypatch):
    data, owner, rows, reads = case()
    if not congress:
        owner._base_config['autoresearch'] = {}
        owner._disabled_strategies = {}
        data = {}
        rows.clear()
    owner._base_config['autoresearch']['courtlistener_scope_policy'] = scoped_replay.COURT_POLICY
    data['courtlistener'] = {'error': 'original Court acquisition failed'}
    strategy = SimpleNamespace(name='litigation', data_sources=['courtlistener'])
    for cohort in owner.cohorts:
        cohort['engine'] = SimpleNamespace(paper_trade_strategies=[strategy])
        rows.append(SimpleNamespace(epoch_id='epoch', session=date.fromisoformat(SESSION),
            policy_id='foundation-' + cohort['config'].horizon, strategy='litigation',
            status='data_failure', signal_count=0, evidence={'data_sources': ['courtlistener'],
                'provider_errors': {} if failed else {'courtlistener': 'retained original failure'}}))
    monkeypatch.setattr(scoped_replay, 'source_scope_evidence',
                        lambda *args, **kwargs: ({'courtlistener': 'failure'}, {}, {}))
    if failed:
        with pytest.raises(ValueError, match='replay'):
            replay(data, owner)
    else:
        replay(data, owner)
    assert reads == [1]
