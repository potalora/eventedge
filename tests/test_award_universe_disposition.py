"""Ownership-based universe decisions retain proof without hiding unknowns."""
from datetime import datetime, timezone
from copy import deepcopy

import pytest

from tradingagents.strategies.data_sources.equity_universe import EquityUniverse, normalize_assets
from tradingagents.strategies.modules.admission import admit_candidates, candidate_universe
from tradingagents.strategies.modules.govt_contracts import GovtContractsStrategy


def universe():
    return EquityUniverse(normalize_assets([{'class':'us_equity', 'symbol':'BA',
        'exchange':'NYSE', 'status':'active', 'tradable':True}],
        observed_at=datetime(2026,10,10,tzinfo=timezone.utc), response_sha256='a'*64))


def award(uei):
    return {'award_id':'NEW-AWARD', 'award_key':'CONT_AWD_NEW-AWARD',
        'recipient_name':'SYNTHETIC FIXTURE NAME', 'recipient_uei':uei,
        'amount':250_000_000, 'base_obligation_date':'2026-10-01',
        'award_scope':'new_awards_only', 'amount_basis':'cumulative_award_obligations',
        'observed_at':'2026-10-10T12:00:00+00:00'}


def screen(rows):
    return GovtContractsStrategy().screen({'usaspending':{'data':{'contracts':rows}}},
                                         '2026-10-09', {'analysis_budget':1})


def test_verified_no_listed_target_is_retained_as_proven_universe_exclusion():
    with candidate_universe(universe()):
        values = screen([award('VMEFT5X61JT9'), award('WZWRLY4G3PL8')])
    assert [c.ticker for c in values] == ['BA']
    manifest = values.admission_manifest
    assert len(manifest['discovered']) == 2
    assert len(manifest['excluded']) == len(manifest['admitted']) == 1
    excluded = manifest['excluded'][0]
    assert excluded['reason'] == 'equity_universe:outside_sip_exchange_universe'
    proof = excluded['universe']['ownership_disposition']
    assert proof['status'] == 'verified_no_listed_target'
    assert proof['matched_uei'] == 'VMEFT5X61JT9' and proof['provenance']


def test_no_universe_policy_preserves_nonlisted_discovery():
    values = screen([award('VMEFT5X61JT9')])
    assert len(values) == 1 and values[0].journal_only
    assert not values.admission_manifest['excluded']


@pytest.mark.parametrize('tamper', ['uei', 'proof', 'ticker', 'strategy'])
def test_forged_or_mismatched_disposition_cannot_exclude_discovery(tamper):
    candidate = deepcopy(screen([award('VMEFT5X61JT9')])[0])
    strategy = 'govt_contracts'
    if tamper == 'uei':
        candidate.metadata['recipient_uei'] = 'UNKNOWN00001'
    elif tamper == 'proof':
        candidate.metadata['issuer_attribution']['provenance'] = ['fabricated']
    elif tamper == 'ticker':
        candidate.ticker = 'BA'
    else:
        strategy = 'supply_chain'
    with candidate_universe(universe()):
        values = admit_candidates(strategy, [candidate], None)
    assert len(values) == 1
    assert not values.admission_manifest['excluded']
