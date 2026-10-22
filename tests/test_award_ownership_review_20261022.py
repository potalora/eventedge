"""Reviewed current ownership improves coverage without guessing unknown issuers."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from tradingagents.strategies.data_sources.award_attribution_policy import build_attribution_scope
from tradingagents.strategies.data_sources.award_identity import resolve_award_issuer
from tradingagents.strategies.data_sources.usaspending_source import USASpendingSource
from tradingagents.strategies.modules.govt_contracts import GovtContractsStrategy


def snapshot():
    return json.loads((Path(__file__).parent / 'fixtures' /
                       'usaspending-native-37-awards-2026-10-21.json').read_text())


def test_native_ketchum_award_resolves_to_omnicom_with_current_review_date():
    row = next(r for r in snapshot()['contracts'] if r['recipient_uei'] == 'CZJKHV86FSB2')
    value = resolve_award_issuer(row)
    assert value['status'] == 'verified_listed_target'
    assert (value['ticker'], value['cik']) == ('OMC', '0000029989')
    assert value['reviewed_on'] == '2026-10-22'
    assert value['relationship'] == 'recipient'
    assert row['recipient_identity_source'] in value['provenance']


@pytest.mark.parametrize('uei', [
    'QQBXQR679KM9', 'LFH4SUCVA379', 'W31DQKBPJCD3', 'XXNJCWMAKNH8',
    'MYEKECP4M5H4', 'LBM4GLJNDK31', 'N9NJK877QJK9', 'FHKLJV1NP651',
    'TWVGJRMK6Z78', 'SQLVDBFQ4SD8',
])
def test_reviewed_private_recipients_have_proved_nonactionable_dispositions(uei):
    row = next(r for r in snapshot()['contracts'] if r['recipient_uei'] == uei)
    row.update(ticker='VFC', issuer_attribution={'ticker': 'SMGZY', 'verified': True})
    value = resolve_award_issuer(row)
    assert value['status'] == 'verified_no_listed_target'
    assert value['resolved'] is True and value['verified'] is False
    assert value['ticker'] == value['cik'] == ''
    assert value['reviewed_on'] == '2026-10-22'
    assert value['temporal_scope'] == 'prospective_current_ownership'
    assert value['ultimate_owner'] and value['ownership_basis']
    assert any('/api/v2/awards/' in url for url in value['provenance'])


def test_oct21_population_retains_lydig_unknown_with_exact_reviewed_partition():
    doc = snapshot()
    original = deepcopy(doc)
    scope = build_attribution_scope(doc['contracts'], doc['coverage'], session='2026-10-21')
    assert scope['counts'] == {
        'verified_listed_target': 10, 'verified_no_listed_target': 26, 'unresolved': 1,
    }
    assert scope['acquisition_complete'] is True and scope['attribution_complete'] is False
    unknown = [r for r in scope['awards'] if r['status'] == 'unresolved']
    assert [r['recipient_uei'] for r in unknown] == ['T9G9DELGRKX5']
    coverage = USASpendingSource._identity_coverage(doc['contracts'])
    assert (coverage['verified'], coverage['verified_no_listed_target'], coverage['unresolved']) == (10, 26, 1)
    assert coverage['complete'] is False
    assert doc == original


@pytest.mark.parametrize('parent', ['QD34AGJ37D53', 'H7MCPFKLJLK3'])
def test_legacy_veritas_and_vf_named_parent_ids_do_not_gain_global_current_owner_mapping(parent):
    value = resolve_award_issuer({
        'recipient_uei': 'UNKNOWN00001', 'parent_recipient_uei': parent,
        'recipient_identity_status': 'native_award_verified',
    })
    assert value['status'] == 'unresolved' and value['resolved'] is False


def test_prior_review_dates_are_preserved_for_existing_crosswalk_entries():
    assert resolve_award_issuer({'recipient_uei': 'XMUZGJN98231'})['reviewed_on'] == '2026-10-10'
    assert resolve_award_issuer({'recipient_uei': 'VMEFT5X61JT9'})['reviewed_on'] == '2026-10-10'


def test_native_private_and_unresolved_awards_remain_distinct_in_screening():
    rows = [r for r in snapshot()['contracts'] if r['recipient_uei'] in {
        'CZJKHV86FSB2', 'SQLVDBFQ4SD8', 'T9G9DELGRKX5',
    }]
    population = GovtContractsStrategy().screen(
        {'usaspending': {'data': {'contracts': rows}}}, '2026-10-21', {'analysis_budget': 10})
    by_uei = {c.metadata['recipient_uei']: c for c in population}
    assert by_uei['CZJKHV86FSB2'].ticker == 'OMC'
    assert by_uei['CZJKHV86FSB2'].journal_only is False
    private, unknown = by_uei['SQLVDBFQ4SD8'], by_uei['T9G9DELGRKX5']
    assert private.journal_only and private.ticker == ''
    assert private.metadata['non_actionable_reason'] == 'verified_no_listed_target'
    assert unknown.journal_only and unknown.ticker == ''
    assert unknown.metadata['non_actionable_reason'] == 'unverified_recipient_issuer'


def test_recipient_name_alone_cannot_use_the_new_reviewed_crosswalk():
    value = resolve_award_issuer({'recipient_uei': 'UNKNOWN00001', 'recipient_name': 'KETCHUM INC.'})
    assert value['status'] == 'unresolved' and value['resolved'] is False
