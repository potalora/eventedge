"""Reviewed UEI ownership dispositions cannot turn unknown recipients into targets."""
from unittest.mock import patch

import pytest

from tradingagents.strategies.data_sources import award_identity as identity
from tradingagents.strategies.data_sources.usaspending_source import USASpendingSource


def contract(uei, **changes):
    return {'recipient_uei': uei, **changes}


def test_native_optum_recipient_resolves_to_listed_parent_not_name_guess():
    value = identity.resolve_award_issuer(contract('XMUZGJN98231', recipient_name='NAME IS NOT THE KEY'))
    assert value['status'] == 'verified_listed_target'
    assert value['verified'] is True and value['resolved'] is True
    assert value['ticker'] == 'UNH' and value['cik'] == '0000731766'
    assert value['matched_uei'] == 'XMUZGJN98231'
    assert len(value['provenance']) >= 2


def test_proven_family_owned_crowley_is_resolved_without_listed_target():
    value = identity.resolve_award_issuer(contract('VMEFT5X61JT9'))
    assert value['status'] == 'verified_no_listed_target'
    assert value['resolved'] is True and value['verified'] is False
    assert value['ticker'] == value['cik'] == ''
    assert value['reason'] == 'verified_no_listed_target'
    assert value['ultimate_owner']
    assert len(value['provenance']) >= 2


def test_unknown_recipient_and_supplied_private_flag_stay_unresolved():
    value = identity.resolve_award_issuer(contract('UNKNOWN00001', recipient_name='Crowley',
        issuer_attribution={'status': 'verified_no_listed_target', 'resolved': True}, private=True))
    assert value['status'] == 'unresolved'
    assert value['resolved'] is False and value['verified'] is False
    assert value['reason'] == 'unverified_recipient_issuer'


def test_private_subsidiary_of_public_parent_is_a_listed_target():
    value = identity.resolve_award_issuer(contract('XMUZGJN98231', private=True))
    assert value['status'] == 'verified_listed_target' and value['ticker'] == 'UNH'


def test_parent_only_no_listed_requires_same_award_native_binding():
    value = identity.resolve_award_issuer(contract('UNKNOWN00001', parent_recipient_uei='VMEFT5X61JT9'))
    assert value['status'] == 'unresolved'
    value = identity.resolve_award_issuer(contract('UNKNOWN00001', parent_recipient_uei='VMEFT5X61JT9',
        recipient_identity_status='native_award_verified', recipient_identity_source='native-award-url'))
    assert value['status'] == 'verified_no_listed_target'
    assert value['relationship'] == 'parent'


def test_conflicting_public_private_native_links_remain_unresolved():
    value = identity.resolve_award_issuer(contract('XMUZGJN98231', parent_recipient_uei='VMEFT5X61JT9',
        recipient_identity_status='native_award_verified'))
    assert value['status'] == 'unresolved'
    assert value['reason'] == 'conflicting_recipient_issuer'


def test_source_coverage_separates_listed_nonlisted_and_unknown():
    records = [contract('XMUZGJN98231'), contract('VMEFT5X61JT9'), contract('UNKNOWN00001')]
    for row in records:
        row['issuer_attribution'] = identity.resolve_award_issuer(row)
    value = USASpendingSource._identity_coverage(records)
    assert value['verified'] == 1 and value['verified_no_listed_target'] == 1 and value['unresolved'] == 1
    assert value['resolved'] == 2 and value['complete'] is False
    assert USASpendingSource._identity_coverage(records[:2])['complete'] is True


def test_coverage_cannot_trust_supplied_disposition_without_native_uei():
    value = USASpendingSource._identity_coverage([contract('UNKNOWN00001',
        issuer_attribution={'verified': True, 'status': 'verified_no_listed_target', 'resolved': True})])
    assert value['complete'] is False and value['unresolved'] == 1


def test_direct_resolved_nonlisted_does_not_repeat_detail_request():
    row = {'recipient_uei': 'VMEFT5X61JT9', 'recipient_id': 'known-native-id'}
    with patch('tradingagents.strategies.data_sources.usaspending_source.provider_request',
               side_effect=AssertionError('unexpected repeat detail request')):
        USASpendingSource()._enrich_recipient_identity(row)
    assert row['issuer_attribution']['status'] == 'verified_no_listed_target'
    assert row['recipient_identity_status'] == 'search_recipient'


def test_no_listed_provenance_is_copied_not_shared():
    value = identity.resolve_award_issuer(contract('VMEFT5X61JT9'))
    original = list(value['provenance'])
    value['provenance'].append('injected')
    assert identity.resolve_award_issuer(contract('VMEFT5X61JT9'))['provenance'] == original


@pytest.mark.parametrize('uei,ticker,cik', [
    ('MT9AATHS7ZB5','TPC','0000077543'), ('RRFJZGASZJ41','VVX','0001601548'),
    ('HFK9V1G2B513','MSI','0000068505'), ('J64CSQTQNRC1','IBM','0000051143'),
    ('QGUQWSU5AHB4','PRM','0001880319'), ('C47BNA8GM833','ACN','0001467373'),
    ('DMPAKJ9N9K66','MDLN','0002046386'), ('HV8BH9BPG8Y9','LDOS','0001336920'),
    ('SMNWM6HN79X5','GD','0000040533'),
    ('JMLKZZ1NL2Z6','GEO','0000923796'),
])
def test_reviewed_native_recipient_listed_ownership(uei, ticker, cik):
    value = identity.resolve_award_issuer(contract(uei))
    assert value['status'] == 'verified_listed_target'
    assert value['verified'] is True and value['resolved'] is True
    assert (value['ticker'], value['cik']) == (ticker, cik)
    assert any('/api/v2/awards/' in source for source in value['provenance'])
    assert len(value['provenance']) >= 2


@pytest.mark.parametrize('uei', ['J7M9HPTGJ1S9','GPXRWUEUHZ19','VEP4UN7LDMK5',
    'H1KPDZLCMNR8','R6CPWWDD4AM1','SRFGXDGTHRU6','F3PQM5C4ATN8','MH2KKA8M75E9'])
def test_reviewed_nonlisted_ultimate_owner_is_nonactionable(uei):
    value = identity.resolve_award_issuer(contract(uei))
    assert value['status'] == 'verified_no_listed_target'
    assert value['resolved'] is True and value['verified'] is False
    assert value['ticker'] == value['cik'] == ''
    assert value['ultimate_owner'] and value['ownership_basis']
    assert any('/api/v2/awards/' in source for source in value['provenance'])


@pytest.mark.parametrize('uei', ['LFH4SUCVA379','FHKLJV1NP651','JY2BDT2K58Z6','VM5QV68M2NU4'])
def test_partial_employee_ownership_or_fund_manager_is_not_issuer_proof(uei):
    value = identity.resolve_award_issuer(contract(uei))
    assert value['status'] == 'unresolved' and not value['resolved']
    assert not value['verified']


def test_walsh_native_legal_recipient_has_current_family_owned_parent_proof():
    value = identity.resolve_award_issuer(contract('DMFWBVTL9324'))
    assert value['status'] == 'verified_no_listed_target'
    assert value['ultimate_owner'] == 'Walsh family through The Walsh Group'
    assert any('tcfdwalshgroupreportfy2025' in source for source in value['provenance'])


def test_review_date_is_explicit_and_does_not_claim_historical_ownership():
    value = identity.resolve_award_issuer(contract('XMUZGJN98231'))
    assert value['reviewed_on'] == '2026-10-10'
    assert value['temporal_scope'] == 'prospective_current_ownership'


def test_restructured_ketchum_without_current_legal_chain_stays_unresolved():
    value = identity.resolve_award_issuer(contract('CZJKHV86FSB2'))
    assert value['status'] == 'unresolved'
    assert value['resolved'] is False


def test_carahsoft_native_recipient_has_2026_individual_owner_disclosure():
    value = identity.resolve_award_issuer(contract('DT8KJHZXVJH5'))
    assert value['status'] == 'verified_no_listed_target'
    assert value['ultimate_owner'] == 'Craig P. Abod'
    assert value['ticker'] == value['cik'] == ''
    assert any('2026_05/26-0528-PR8.pdf' in source for source in value['provenance'])


def test_hitt_historical_disclosure_does_not_imply_current_ownership():
    assert identity.resolve_award_issuer(contract('RRKASFUKYMJ4'))['status'] == 'unresolved'
