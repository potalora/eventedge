"""Explicit ten-identity permission cannot widen the original three policy."""
from copy import deepcopy

import pytest

from filing_ten_material_fixtures import TEN_POLICY, ROWS, framed, identity
from tradingagents.strategies.data_sources import filing_material_policy as material


def test_original_manifest_hash_defaults_and_exact_three_are_unchanged():
    assert material.POLICY=='retained_three_material_gaps_v1'
    assert material.policy_manifest_sha256()=='168b10bd85e34e4dea0da792e0b79bf51b4adc1a1e8831b131310deb4f222123'
    assert len(material.policy_manifest()['identities'])==3


def test_ten_manifest_is_separate_exact_union_with_seven_explicit_identities():
    old=material.policy_manifest();new=material.policy_manifest(TEN_POLICY)
    assert new['policy']==TEN_POLICY and len(new['identities'])==10
    assert new['identities'][:3]==old['identities']
    assert [x['accession'] for x in new['identities'][3:]]==[x[0] for x in ROWS]
    assert material.policy_manifest_sha256(TEN_POLICY)!=material.policy_manifest_sha256()
    for index,row in enumerate(new['identities'][3:],3):
        assert {k:row[k] for k in ('accession','form','filing_date','source_url')}==identity(index)
        assert material.approved_identity(identity(index)) is None


@pytest.mark.parametrize('index',range(10))
def test_ten_identity_full_inventory_quarantine_is_nonactionable_and_policy_bound(index):
    frame,raw=framed(index)
    result=material.quarantine_evidence(frame,submission_size=len(raw),policy=TEN_POLICY)
    assert result['material_quarantine']['policy']==TEN_POLICY
    assert result['material_quarantine']['policy_manifest_sha256']==material.policy_manifest_sha256(TEN_POLICY)
    assert result['structural_status']==result['analysis_adequacy']=='insufficient'
    assert result['structural_scope']=='complete_original_inventory_unselected'
    assert result['units']==result['dependencies']==[]
    assert material.validate_quarantined_evidence(result,policy=TEN_POLICY)==material.approved_identity(identity(index),policy=TEN_POLICY)
    if result['form']=='DEF 14A':assert 'ambiguous_primary_document' in result['material_quarantine']['gap_codes']
    with pytest.raises(ValueError):material.validate_quarantined_evidence(result)
    if index>=3:
        with pytest.raises(ValueError):material.quarantine_evidence(frame,submission_size=len(raw))


@pytest.mark.parametrize('index',range(3))
def test_old_envelopes_cannot_be_relabelled_or_consumed_under_ten_policy(index):
    frame,raw=framed(index);old=material.quarantine_evidence(frame,submission_size=len(raw))
    with pytest.raises(ValueError):material.validate_quarantined_evidence(old,policy=TEN_POLICY)
    forged=deepcopy(old);forged['material_quarantine']['policy']=TEN_POLICY
    with pytest.raises(ValueError):material.validate_quarantined_evidence(forged,policy=TEN_POLICY)


def test_ten_config_pairing_and_unknown_or_mismatched_identity():
    config={'filing_material_policy':TEN_POLICY,'filing_evidence_policy':'complete_submission_v1',
        'filing_acquisition_policy':'bounded_original_submission_v1','filing_parser_policy':'two_processes_v1'}
    assert material.configured(config)
    for key in ('filing_evidence_policy','filing_acquisition_policy','filing_parser_policy'):
        with pytest.raises(ValueError):material.configured({**config,key:None})
    assert material.approved_identity({**identity(3),'accession':'0001308179-26-999999'},policy=TEN_POLICY) is None
    with pytest.raises(ValueError):material.approved_identity({**identity(3),'filing_date':'2026-10-05'},policy=TEN_POLICY)
