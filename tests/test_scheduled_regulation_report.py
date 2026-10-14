"""Empty regulatory discovery stays valid during an independent SEC outage."""
from copy import deepcopy
import json
import sqlite3

import pytest

pytestmark = pytest.mark.scheduled_acceptance

from tradingagents.strategies.data_sources.regulation_evidence import digest
from tradingagents.strategies.orchestration.regulation_validation import validate_regulation_health
from test_operational_report import native_evidence, _attempt, _report
from test_prospective_replay_integrity import rewrite_payload
from test_regulation_assessment import universe
from test_regulation_replay_report import completed, CONFIG, POLICIES, STRATEGIES


def empty_regulations():
    graph = {'policy': 'complete_document_text_v1', 'corpus': {}, 'acquisition_failures': {},
        'rows': [], 'coverage': {'discovery_complete': True, 'content_complete': True,
            'required_rows': 0, 'complete_rows': 0, 'failed_rows': 0, 'consumed_bytes': 0,
            'reserved_bytes': 0, 'byte_limit': 33554432, 'byte_budget_exhausted': False},
        'retrieval_policy': 'verified_government_copy_v1'}
    graph['manifest_sha256'] = digest(graph)
    return {'proposed_rules': [], 'coverage': {'complete': True}, 'regulation_evidence': graph}


def records_for_empty_source(data):
    from tradingagents.strategies.orchestration.regulation_inputs import validate_regulation_policy
    scope = validate_regulation_policy(data, {**CONFIG,
        'regulation_retrieval_policy': 'verified_government_copy_v1'})
    return [{'policy_id': policy, 'strategy': 'regulatory_pipeline',
        'status': 'legitimate_no_event', 'signal_count': 0, 'evidence': {
            'candidate_count': 0, 'data_sources': sorted(STRATEGIES['regulatory_pipeline']),
            'regulation_source_coverage': deepcopy(scope), 'regulation_assessments': [],
            'admission_manifest': {'admitted': [], 'discovered': [], 'excluded': []}}}
        for policy in sorted(POLICIES)]


@pytest.mark.parametrize('company_map', [None, {}])
def test_empty_complete_discovery_does_not_require_unrelated_sec_company_map(company_map):
    assets, _ = universe()
    edgar = {'error': 'synthetic SEC outage'}
    if company_map is not None:
        edgar['company_tickers'] = company_map
    data = {'regulations': empty_regulations(), 'edgar': edgar,
        'equity_universe': {'snapshot': assets.evidence}}
    result = validate_regulation_health(data, records_for_empty_source(data), POLICIES, STRATEGIES,
        {**CONFIG, 'regulation_retrieval_policy': 'verified_government_copy_v1'})
    assert result['acquisition_complete'] is True
    assert result['analysis_complete'] is True
    assert len(result['horizons']) == 4
    assert all(row['required_documents'] == row['assessed_documents'] == row['failed_documents'] ==
               row['missing_documents'] == row['excluded_candidates'] == 0 for row in result['horizons'])


def test_operational_report_retains_empty_regulatory_coverage_during_sec_outage(native_evidence):
    from tradingagents.strategies.orchestration.regulation_inputs import validate_regulation_policy
    repo, state, wire = native_evidence
    regulations = empty_regulations()
    scope = validate_regulation_policy({'regulations': regulations},
        {**CONFIG, 'regulation_retrieval_policy': 'verified_government_copy_v1'})
    assets, _ = universe()
    rewrite_payload(next((state / 'source_inputs').glob('*.json')), lambda payload: payload.update(
        regulations=regulations, edgar={'error': 'synthetic SEC outage'},
        equity_universe={'snapshot': assets.evidence}))
    with sqlite3.connect(state / 'metrics_v2.sqlite3') as connection:
        for identity, raw in connection.execute('SELECT health_id,payload_json FROM strategy_health').fetchall():
            record = json.loads(raw)
            if record['strategy'] not in {'commodity_macro', 'regulatory_pipeline'}:
                continue
            record['evidence']['regulation_source_coverage'] = deepcopy(scope)
            record['evidence']['regulation_assessments'] = []
            connection.execute('UPDATE strategy_health SET payload_json=? WHERE health_id=?',
                (json.dumps(record), identity))
    _attempt(repo, wire)
    report = _report(repo)
    assert {'code': 'regulation_evidence_conflict'} not in report['diagnostics']
    assert report['sources']['regulation_analysis_coverage']['analysis_complete'] is True
    assert len(report['sources']['regulation_analysis_coverage']['horizons']) == 4
    assert report['sources']['observations']['edgar'] == 'failed'
    assert report['input_coverage_valid'] is False


def test_missing_sec_map_still_rejects_nonempty_unproved_target_binding(tmp_path, monkeypatch):
    data, records = completed(tmp_path, monkeypatch)
    data['edgar'] = {'error': 'synthetic SEC outage'}
    provenance = records[0]['evidence']['regulation_assessments'][0]['model_attempt']['source_provenance']
    provenance['target_bindings'] = [{'ticker': 'ACME'}]
    with pytest.raises(ValueError, match='invalid_regulation_health') as failure:
        validate_regulation_health(data, records, POLICIES, STRATEGIES, CONFIG)
    assert str(failure.value.__cause__) == 'invalid_regulation_target_authority_unavailable'


def test_missing_sec_map_does_not_exempt_invalid_asset_snapshot():
    assets, _ = universe()
    snapshot = deepcopy(assets.evidence)
    snapshot['assets_sha256'] = '0' * 64
    data = {'regulations': empty_regulations(), 'edgar': {'error': 'synthetic SEC outage'},
        'equity_universe': {'snapshot': snapshot}}
    with pytest.raises(ValueError, match='invalid_regulation_health') as failure:
        validate_regulation_health(data, records_for_empty_source(data), POLICIES, STRATEGIES,
            {**CONFIG, 'regulation_retrieval_policy': 'verified_government_copy_v1'})
    assert str(failure.value.__cause__) == 'universe evidence binding mismatch'


@pytest.mark.parametrize('company_map', ['', [], 0, False, {'0': {'ticker': 'ACME'}}])
def test_absent_map_normalization_does_not_accept_malformed_sec_evidence(company_map):
    assets, _ = universe()
    data = {'regulations': empty_regulations(), 'edgar': {'company_tickers': company_map},
        'equity_universe': {'snapshot': assets.evidence}}
    with pytest.raises(ValueError, match='invalid_regulation_health'):
        validate_regulation_health(data, records_for_empty_source(data), POLICIES, STRATEGIES,
            {**CONFIG, 'regulation_retrieval_policy': 'verified_government_copy_v1'})
