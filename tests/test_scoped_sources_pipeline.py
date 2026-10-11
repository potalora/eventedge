"""Sixteen real books with scoped sources; HTTP and model boundaries are synthetic."""
from copy import deepcopy
from datetime import datetime, timezone
import json

import pytest

from test_source_reliability_pipeline import pipeline, run_campaign, SESSION, accepted_input_bytes
from test_prospective_pipeline import enable_activity_pipeline
from test_prospective_replay_integrity import evidence_file, rewrite_payload
from tradingagents.strategies.orchestration.source_inputs import SourceInputStore


def enable_scoped_pipeline(pipeline, monkeypatch, *, malformed_court=False):
    fixture, owner, config = enable_activity_pipeline(pipeline, monkeypatch)
    for target in (config, owner._base_config):
        target['autoresearch'].update(award_attribution_policy='verified_listed_targets_v1',
            courtlistener_scope_policy='focused_litigation_v1',
            courtlistener_focused={'watchlist': ['AAPL']})
    from tradingagents.strategies.data_sources import courtlistener_source, usaspending_source
    from tradingagents.strategies.orchestration import scoped_sources
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls.fromtimestamp(fixture.now.timestamp(), tz)
    for module in (courtlistener_source, usaspending_source, scoped_sources):
        monkeypatch.setattr(module, 'datetime', FixedDatetime)
    original = fixture.http
    def native_http(method, url, **kwargs):
        response = original(method, url, **kwargs)
        if 'courtlistener.com' in url:
            assert kwargs['params']['q'] == 'caseName:"AAPL Corporation"'
            return fixture.response({'results': [{'docket_id': 123, 'caseName': 'Investors v. AAPL Corporation',
                'court': 'cand', 'dateFiled': '2026-10-01', 'suitNature': 'Securities', 'cause': 'Fraud'}],
                'count': 1, 'next': 'bad' if malformed_court else None})
        if 'api.usaspending.gov' in url:
            return fixture.response({'results': [{'Award ID': 'OFFLINE-AWARD', 'Recipient Name': 'Unknown recipient',
                'Award Amount': 100_000_000, 'Base Obligation Date': '2026-10-01',
                'generated_internal_id': 'CONT_AWD_OFFLINE'}],
                'page_metadata': {'page': 1, 'hasNext': False}})
        return response
    fixture.http = native_http
    return fixture, owner, config


def test_scoped_sources_complete_all_books_with_honest_limits_and_read_only_replay(pipeline, monkeypatch):
    fixture, owner, config = enable_scoped_pipeline(pipeline, monkeypatch)
    result, wire, report = run_campaign(pipeline)
    assert result['outcome'] == 'clean', result
    limits = result['source_scope_limits']
    assert limits == report['source_scope_limits']
    assert limits['usaspending']['counts']['unresolved'] == 1
    assert limits['usaspending']['attribution_complete'] is False
    assert limits['courtlistener']['docket_count'] == 1
    assert limits['courtlistener']['marketwide_coverage'] is False
    assert all(row['input_coverage_valid'] and row['source_scope_limits'] == limits for row in wire.values())
    health = owner._metric_store.read_strategy_health(session=SESSION)
    litigation = [row for row in health if row.strategy == 'litigation']
    assert len(litigation) == 4
    assert all(row.status == 'legitimate_no_event' and row.signal_count == 0 for row in litigation)
    assert all(row.evidence['litigation_context']['docket_ids'] == [123] for row in litigation)
    assert not any(s['strategy'] == 'litigation' for row in wire.values() for s in row['signals'])
    source = SourceInputStore.decode(evidence_file(config, 'source').read_text())['payload']
    assert len(source['_courtlistener_targets']['manifest']['cohorts']) == 16
    assert source['usaspending']['issuer_attribution_evidence']['complete'] is False
    assert source['usaspending']['coverage']['complete'] is True
    # The committee sees the retained docket as qualified context, never as a short signal.
    decisions = SourceInputStore.decode(evidence_file(config, 'decision').read_text())['payload']
    from tradingagents.strategies.orchestration.scoped_sources import source_scope_evidence, litigation_context
    errors, _, proofs = source_scope_evidence(source, config['autoresearch'], str(SESSION),
        now=datetime.fromisoformat(decisions['context']['cutoff']))
    assert errors == {}
    assert litigation_context(source, proofs['courtlistener'])['actionable_from_docket_metadata'] is False
    before, calls = accepted_input_bytes(config), fixture.calls.copy()
    fixture.block_all = True
    fixture.now = datetime(2026, 10, 5, 16, tzinfo=timezone.utc)
    repeated, _, repeated_report = run_campaign(pipeline)
    assert repeated['outcome'] == 'clean' and repeated_report['source_scope_limits'] == limits
    assert accepted_input_bytes(config) == before and fixture.calls == calls and not fixture.blocked_calls


def test_failed_declared_court_query_is_never_reported_as_completed_scope(pipeline, monkeypatch):
    fixture, owner, _ = enable_scoped_pipeline(pipeline, monkeypatch, malformed_court=True)
    result, wire, report = run_campaign(pipeline)
    assert result['outcome'] == 'degraded'
    assert all(row['input_coverage_valid'] is False for row in wire.values())
    assert 'courtlistener' not in result['source_scope_limits']
    assert result['source_scope_limits']['usaspending']['counts']['unresolved'] == 1
    assert any(row['strategy'] == 'litigation' for row in result['source_health_failures'])


@pytest.mark.parametrize('mutation', ['issuer_name', 'roster', 'unknown_awards'])
def test_completed_scope_replay_rejects_rebound_evidence_without_new_queries(pipeline, monkeypatch, mutation):
    fixture, owner, config = enable_scoped_pipeline(pipeline, monkeypatch)
    assert run_campaign(pipeline)[0]['outcome'] == 'clean'
    path = evidence_file(config, 'source')
    def alter(data):
        if mutation == 'issuer_name':
            data['_courtlistener_targets']['scope']['issuers'][0]['legal_name'] = 'Different company'
        elif mutation == 'roster':
            data['_courtlistener_targets']['manifest']['cohorts'].pop()
        else:
            data['usaspending']['coverage']['attribution_scope']['counts']['unresolved'] = 0
    rewrite_payload(path, alter)
    before, calls = accepted_input_bytes(config), fixture.calls.copy()
    fixture.block_all = True
    epoch = owner.cohorts[0]['ledger'].read_policy_session_context(SESSION)['epoch_id']
    owner._epoch_id = epoch
    from tradingagents.strategies.orchestration.decision_clock import validate_completed_replay
    with pytest.raises(ValueError):
        validate_completed_replay(owner, SESSION, epoch, owner.cohorts)
    assert accepted_input_bytes(config) == before and fixture.calls == calls and not fixture.blocked_calls
