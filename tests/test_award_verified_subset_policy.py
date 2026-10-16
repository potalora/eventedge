"""Opt-in award coverage never manufactures an issuer or hides a provider failure."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from tradingagents.strategies.data_sources.evidence import CoverageRecords
from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
from tradingagents.strategies.modules.govt_contracts import GovtContractsStrategy
from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine

POLICY = 'verified_listed_targets_v1'


def records():
    return [dict(award_id=f'AWARD-{i}', award_key=f'native-award-{i}', recipient_name=f'Recipient {i}',
        recipient_uei=uei, parent_recipient_uei='', recipient_identity_status='search_recipient',
        recipient_identity_source='https://api.usaspending.gov/api/v2/search/spending_by_award/',
        award_scope='new_awards_only', amount_basis='cumulative_award_obligations',
        base_obligation_date='2026-10-01', observed_at='2026-10-10T12:00:00+00:00', amount=100_000_000)
        for i,uei in enumerate(['XMUZGJN98231','VMEFT5X61JT9','UNKNOWN00001'])]


def acquisition(rows, **changes):
    coverage=dict(mode='exhaustive_window',complete=True,returned=len(rows),pages=1,
        date_from='2026-09-09',date_to='2026-10-09',date_type='new_awards_only',
        amount_basis='cumulative_award_obligations',
        issuer_attribution={'complete':False,'unresolved':1})
    coverage.update(changes)
    return CoverageRecords(rows,coverage=coverage)


def fetch(rows, policy=POLICY):
    engine=MultiStrategyEngine.__new__(MultiStrategyEngine)
    engine.ar_config={} if policy is None else {'award_attribution_policy':policy}
    engine.registry=SimpleNamespace(get=lambda _:SimpleNamespace(get_recent_large_contracts=lambda **_:rows))
    return engine._fetch_usaspending_data('2026-10-09')


def test_complete_acquisition_with_unknown_issuer_is_audited_subset_not_api_error():
    rows=acquisition(records()); original=deepcopy(rows); original_coverage=deepcopy(rows.coverage)
    payload=fetch(rows)
    assert 'error' not in payload
    assert payload['award_attribution_policy']==POLICY
    scope=payload['coverage']['attribution_scope']
    assert scope['acquisition_complete'] is True and scope['verified_listed_scope_complete'] is True
    assert scope['attribution_complete'] is False
    assert scope['counts']=={'verified_listed_target':1,'verified_no_listed_target':1,'unresolved':1}
    assert {row['award_key']:row['status'] for row in scope['awards']}=={
        'native-award-0':'verified_listed_target','native-award-1':'verified_no_listed_target','native-award-2':'unresolved'}
    assert scope['awards'][0]['attribution']['ticker']=='UNH'
    assert scope['awards'][2]['attribution']['reason']=='unverified_recipient_issuer'
    assert rows==original and rows.coverage==original_coverage
    assert payload['data']['contracts']==original


def test_absent_policy_preserves_strict_incomplete_attribution_error():
    assert fetch(acquisition(records()),policy=None)['error']=='USASpending issuer attribution incomplete'


@pytest.mark.parametrize('changes',[{'complete':False},{'returned':2},{'mode':'bounded_sample'},
    {'pages':0},{'date_to':'2026-10-08'},{'date_type':'modifications'}, {'complete':1}])
def test_subset_cannot_accept_malformed_or_incomplete_acquisition(changes):
    assert 'error' in fetch(acquisition(records(),**changes))


@pytest.mark.parametrize('mutation', ['duplicate','missing_key','nonfinite','wrong_window','naive_time','lookup_failure'])
def test_bad_award_metadata_is_not_relabelled_as_unknown_attribution(mutation):
    rows=records()
    if mutation=='duplicate':rows[1]['award_key']=rows[0]['award_key']
    elif mutation=='missing_key':rows[0].pop('award_key')
    elif mutation=='nonfinite':rows[0]['amount']=float('nan')
    elif mutation=='wrong_window':rows[0]['base_obligation_date']='2026-08-01'
    elif mutation=='naive_time':rows[0]['observed_at']='2026-10-10T12:00:00'
    else:rows[2]['recipient_identity_status']='lookup_failed'
    assert 'error' in fetch(acquisition(rows))


def test_supplied_flags_and_conflicting_parent_do_not_expand_verified_scope():
    rows=records(); rows[2]['issuer_attribution']={'verified':True,'status':'verified_listed_target','ticker':'BA'}
    rows[0].update(parent_recipient_uei='VMEFT5X61JT9',recipient_identity_status='native_award_verified')
    payload=fetch(acquisition(rows));scope=payload['coverage']['attribution_scope']
    assert scope['counts']=={'verified_listed_target':0,'verified_no_listed_target':1,'unresolved':2}
    assert scope['awards'][0]['attribution']['reason']=='conflicting_recipient_issuer'
    assert scope['awards'][2]['attribution']['reason']=='unverified_recipient_issuer'


def test_subset_screen_retains_unknown_discovery_but_only_verified_is_actionable():
    payload=fetch(acquisition(records()))
    population=GovtContractsStrategy().screen({'usaspending':payload},'2026-10-09',{'analysis_budget':10})
    assert len(population.admission_manifest['discovered'])==3
    assert [c.ticker for c in population if not c.journal_only]==['UNH']
    unknown=next(c for c in population if c.metadata['award_key']=='native-award-2')
    assert unknown.journal_only and unknown.ticker==''
    assert unknown.metadata['award_attribution_policy']==POLICY
    assert unknown.metadata['attribution_coverage_gap']=='unresolved_recipient_issuer'
    assert unknown.metadata['non_actionable_reason']=='unverified_recipient_issuer'
    assert not unknown.metadata.get('equity_universe_excluded')
    assert unknown.metadata['award_attribution_scope_sha256']==payload['coverage']['attribution_scope']['scope_sha256']


def test_strategy_rejects_modified_scope_evidence_instead_of_trusting_flags():
    payload=fetch(acquisition(records()))
    payload['coverage']['attribution_scope']['awards'][2]['status']='verified_no_listed_target'
    with pytest.raises((ValueError,SourceFetchError)):
        GovtContractsStrategy().screen({'usaspending':payload},'2026-10-09',{})


def test_unknown_policy_is_not_silently_accepted():
    assert 'error' in fetch(acquisition(records()),policy='unreviewed_policy')


def test_original_native_50_awards_remain_unchanged_with_exact_reviewed_partition():
    import hashlib
    import json
    from pathlib import Path
    from tradingagents.strategies.orchestration.source_inputs import SourceInputStore
    path=Path(__file__).parent/'fixtures/usaspending-native-50-awards.json'
    original_bytes=path.read_bytes();document=json.loads(original_bytes)
    encoded=document['encoded_native_payload']
    native=SourceInputStore.decode(encoded)
    assert hashlib.sha256(encoded.encode()).hexdigest()=='fbde445402b1db329cc6a12e8145c432a2940f0b5e445611edf9b3b74ebf696f'
    rows=CoverageRecords(native['data']['contracts'],coverage=native['coverage'])
    original=deepcopy(rows)
    payload=fetch(rows)
    assert 'error' not in payload
    scope=payload['coverage']['attribution_scope']
    assert scope['counts']=={'verified_listed_target':14,'verified_no_listed_target':13,'unresolved':23}
    assert len(scope['awards'])==50 and len({r['award_key'] for r in scope['awards']})==50
    assert len({r['recipient_uei'] for r in scope['awards']})==40
    assert payload['data']['contracts']==original and rows==original
    assert rows.coverage==native['coverage'] and path.read_bytes()==original_bytes
    assert native['coverage']['issuer_attribution']['verified']==0
    assert native['coverage']['issuer_attribution']['unresolved']==50


def test_scoped_complete_acquisition_can_be_cached_without_hiding_attribution_gap():
    from tradingagents.strategies.orchestration.source_inputs import successful_source
    payload=fetch(acquisition(records()))
    assert successful_source(payload)
    assert payload['issuer_attribution_evidence']=={'complete':False,'unresolved':1}
    assert payload['coverage']['attribution_scope']['attribution_complete'] is False


def test_policy_validation_failure_retains_every_acquired_award():
    rows=acquisition(records(),returned=2)
    payload=fetch(rows)
    assert 'error' in payload
    assert payload['data']['contracts']==rows


def test_provider_failure_cannot_be_accepted_by_subset_policy():
    engine=MultiStrategyEngine.__new__(MultiStrategyEngine)
    engine.ar_config={'award_attribution_policy':POLICY}
    def failed(**kwargs):raise SourceFetchError('fixed provider failure',reason_code='http_error',http_status=503)
    engine.registry=SimpleNamespace(get=lambda _:SimpleNamespace(get_recent_large_contracts=failed))
    payload=engine._fetch_usaspending_data('2026-10-09')
    assert 'http_error' in payload['error']
    assert 'attribution_scope' not in payload


@pytest.mark.parametrize('policy',['',False,0,'verified_listed_targets_v2'])
def test_invalid_configured_policy_fails_instead_of_falling_back_to_strict(policy):
    payload=fetch(acquisition(records(),issuer_attribution={'complete':True}),policy=policy)
    assert 'error' in payload


@pytest.mark.parametrize('changes',[{'additional_scope':{'complete':False}},
    {'additional_scope':{'status':'failed'}},{'error':'provider failed'}])
def test_other_provider_scope_failures_cannot_hide_under_verified_policy(changes):
    assert 'error' in fetch(acquisition(records(),**changes))


def test_unknown_award_scope_proof_survives_zero_admission_budget():
    payload=fetch(acquisition(records()))
    population=GovtContractsStrategy().screen({'usaspending':payload},'2026-10-09',{'analysis_budget':0})
    assert not population
    assert len(population.admission_manifest['discovered'])==3
    assert population.admission_manifest['award_attribution_scope']==payload['coverage']['attribution_scope']


def test_independent_scope_validation_binds_all_rows_not_supplied_counts():
    from tradingagents.strategies.data_sources.award_attribution_policy import validate_attribution_scope
    payload=fetch(acquisition(records()))
    assert validate_attribution_scope(payload,session='2026-10-09')['counts']['unresolved']==1
    payload['data']['contracts'][2]['recipient_uei']='XMUZGJN98231'
    with pytest.raises(SourceFetchError):validate_attribution_scope(payload,session='2026-10-09')
