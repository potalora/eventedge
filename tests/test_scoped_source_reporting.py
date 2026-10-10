"""Declared source subsets stay visible after a durable completed resume."""
from copy import deepcopy
from dataclasses import replace
import json
import sqlite3

import pytest

from test_source_coverage_reporting import state_with_health
from test_operational_report import native_evidence, _attempt, _report, SESSION
from tradingagents.strategies.orchestration.operational_report import render_operational_report


def limits():
    return {
        'usaspending': {'policy':'verified_listed_targets_v1','scope_sha256':'a'*64,
            'counts':{'verified_listed_target':14,'verified_no_listed_target':13,'unresolved':23},
            'attribution_complete':False,'coverage_basis':'verified_listed_targets'},
        'courtlistener': {'policy':'focused_litigation_v1','scope_sha256':'b'*64,
            'target_manifest_sha256':'c'*64,'coverage_basis':'declared_issuer_and_case_queries',
            'content_kind':'docket_metadata_only','issuer_count':5,'case_count':2,'docket_count':4,
            'omitted_issuer_count':100,'target_search_complete':False,'marketwide_coverage':False}}


def scoped_state():
    from tradingagents.strategies.metrics.health import classify_strategy_run
    from tradingagents.strategies.modules.govt_contracts import GovtContractsStrategy
    from tradingagents.strategies.modules.litigation import LitigationStrategy
    state, results = state_with_health()
    native = {'sec_filings': GovtContractsStrategy(), 'weather_ag': LitigationStrategy()}
    provider = {'govt_contracts': 'usaspending', 'litigation': 'courtlistener'}
    rows = []
    for original in state.owner._metric_store.read_strategy_health(state.epoch_id, session=state.session):
        strategy = native[original.strategy]
        row = classify_strategy_run(epoch_id=original.epoch_id, session=original.session,
            policy_id=original.policy_id, strategy=strategy.name, data_sources=strategy.data_sources,
            candidates=[], provider_errors={}, exception=None)
        row.evidence['source_scope_limits'] = {provider[strategy.name]: limits()[provider[strategy.name]]}
        rows.append(row)
    state.owner._active_strategy_names = frozenset(provider)
    state.owner._metric_store.read_strategy_health = lambda *a, **k: rows
    return state, results


def test_completed_resume_preserves_declared_limits_without_false_degradation():
    state,results=scoped_state()
    final=state.finalize(results)
    assert all(row['source_scope_limits']==limits() for row in final.values())
    assert all(row['input_coverage_valid'] is True and row['degraded'] is False for row in final.values())
    final['book-30d']['source_scope_limits']['usaspending']['counts']['unresolved']=99
    assert final['book-90d']['source_scope_limits']==limits()


@pytest.mark.parametrize('mutation',['extra_field','secret_text','bad_digest','wrong_count','bool_count',
    'attribution_contradiction','marketwide','search_incomplete','unknown_provider','unknown_policy'])
def test_scoped_summary_rejects_unknown_or_contradictory_evidence(mutation):
    from tradingagents.strategies.orchestration.source_coverage import canonical_source_scope_limits
    value=limits()
    if mutation=='extra_field':value['usaspending']['extra']='provider body'
    elif mutation=='secret_text':value['courtlistener']['content_kind']='raw secret body'
    elif mutation=='bad_digest':value['usaspending']['scope_sha256']='bad'
    elif mutation=='wrong_count':value['usaspending']['counts']['unresolved']=-1
    elif mutation=='bool_count':value['courtlistener']['issuer_count']=True
    elif mutation=='attribution_contradiction':value['usaspending']['attribution_complete']=True
    elif mutation=='marketwide':value['courtlistener']['marketwide_coverage']=True
    elif mutation=='search_incomplete':value['courtlistener']['target_search_complete']=True
    elif mutation=='unknown_provider':value['unknown']=value.pop('courtlistener')
    else:value['usaspending']['policy']='silently_broader_policy'
    with pytest.raises(ValueError,match='source scope'):
        canonical_source_scope_limits(value)


def test_conflicting_durable_scope_summaries_cannot_pass_completed_resume():
    state,results=scoped_state();read=state.owner._metric_store.read_strategy_health
    def conflicting(*a,**k):
        rows=read(*a,**k)
        revised=[]
        for row in rows:
            value=deepcopy(row.evidence['source_scope_limits'])
            if row.strategy == 'litigation' and row.policy_id == 'foundation-90d':
                value['courtlistener']['scope_sha256']='d'*64
            revised.append(replace(row,evidence={**row.evidence,'source_scope_limits':value}))
        return revised
    state.owner._metric_store.read_strategy_health=conflicting
    from tradingagents.strategies.orchestration.source_coverage import aggregate_source_scope_limits
    with pytest.raises(ValueError,match='source scope'):
        aggregate_source_scope_limits(state.finalize(results))


@pytest.mark.parametrize('fault', ['unrelated_source', 'missing_sources', 'disabled', 'provider_failed'])
def test_scope_summary_requires_matching_source_provenance(fault):
    from tradingagents.strategies.orchestration.source_coverage import source_scope_limits_from_health
    state, _ = scoped_state()
    row = deepcopy(state.owner._metric_store.read_strategy_health()[0])
    provider = next(iter(row.evidence['source_scope_limits']))
    if fault == 'unrelated_source':
        row.evidence['data_sources'] = ['edgar']
    elif fault == 'missing_sources':
        row.evidence.pop('data_sources')
    elif fault == 'disabled':
        row = replace(row, status='disabled_by_policy')
    else:
        row.evidence['provider_errors'] = {provider: 'source failed'}
        row = replace(row, status='data_failure')
    with pytest.raises(ValueError, match='source scope'):
        source_scope_limits_from_health([row])


def test_valid_source_scope_survives_independent_analysis_failure():
    from tradingagents.strategies.orchestration.source_coverage import source_scope_limits_from_health
    state, _ = scoped_state()
    row = deepcopy(state.owner._metric_store.read_strategy_health()[0])
    row.evidence['provider_errors'] = {'analysis': 'required analysis unavailable'}
    row = replace(row, status='data_failure')
    assert source_scope_limits_from_health([row]) == row.evidence['source_scope_limits']


def _durable_limits(state):
    with sqlite3.connect(state/'metrics_v2.sqlite3') as connection:
        for key,raw in connection.execute('SELECT health_id,payload_json FROM strategy_health').fetchall():
            payload=json.loads(raw)
            provider={'govt_contracts':'usaspending','litigation':'courtlistener'}.get(payload['strategy'])
            if provider:
                payload['evidence']['source_scope_limits']={provider:limits()[provider]}
                connection.execute('UPDATE strategy_health SET payload_json=? WHERE health_id=?',(json.dumps(payload),key))


def test_report_renders_durable_partial_attribution_and_metadata_only_scope(native_evidence):
    repo,state,wire=native_evidence;_durable_limits(state)
    for row in wire.values():row['source_scope_limits']=deepcopy(limits())
    _attempt(repo,wire)
    report=_report(repo)
    assert report['source_scope_limits']==limits()
    assert report['outcome']=='clean' and report['input_coverage_valid'] is True
    markdown=render_operational_report(report)
    assert 'Source scope limitations' in markdown
    assert '23 unresolved' in markdown
    assert 'verified listed targets' in markdown
    assert 'docket metadata only' in markdown and 'not marketwide' in markdown
    assert '100 omitted issuers' in markdown


def test_report_detects_dropped_scope_field_in_worker_results(native_evidence):
    repo,state,wire=native_evidence;_durable_limits(state)
    _attempt(repo,wire)
    report=_report(repo)
    assert report['evidence_complete'] is False
    assert {'code':'wire_source_scope_conflict'} in report['diagnostics']


def test_court_all_targets_complete_requires_zero_omitted_and_explicit_boolean():
    from tradingagents.strategies.orchestration.source_coverage import canonical_source_scope_limits
    value=limits();value['courtlistener'].update(omitted_issuer_count=0,target_search_complete=True)
    assert canonical_source_scope_limits(value)==value
    value['courtlistener']['target_search_complete']=False
    with pytest.raises(ValueError,match='source scope'):
        canonical_source_scope_limits(value)


def test_cohort_scope_aggregation_rejects_silently_dropped_summary():
    from tradingagents.strategies.orchestration.source_coverage import aggregate_source_scope_limits
    results={'one':{'source_scope_limits':limits()},'two':{'source_scope_limits':limits()}}
    assert aggregate_source_scope_limits(results)==limits()
    results['two'].pop('source_scope_limits')
    with pytest.raises(ValueError,match='source scope'):
        aggregate_source_scope_limits(results)
