"""Retired enrichment must change frozen identities, never historical evidence."""
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from tradingagents.strategies.orchestration.session_executor import SessionExecutor
from tradingagents.strategies.orchestration.source_inputs import (
    SourceInputError, SourceInputStore, public_source_configuration_declaration,
    public_source_configuration_fingerprint, source_configuration_fingerprint,
)
from tradingagents.strategies.state.portfolio_ledger import PortfolioLedger
from test_metric_epoch_runtime import _context, _policy


POLICY = {
    'enrichment_capability_policy': 'optional_enrichment_retirement_v1',
    'retired_enrichment_capabilities': {
        'commodity_futures_curves': 'maturity_identified_curve_evidence_unavailable'},
}
INVALID = [
    {'enrichment_capability_policy': POLICY['enrichment_capability_policy']},
    {'retired_enrichment_capabilities': POLICY['retired_enrichment_capabilities']},
    {**POLICY, 'enrichment_capability_policy': 'ignore_failed_models'},
    {**POLICY, 'retired_enrichment_capabilities': {'profiles': 'unavailable'}},
    {**POLICY, 'retired_enrichment_capabilities': {
        'commodity_futures_curves': 'temporary_provider_failure'}},
]


def semantic_document(tmp_path, ar):
    config = {'execution': {'mode': 'paper'}, 'autoresearch': {
        'state_dir': str(tmp_path), **deepcopy(ar)}}
    ledger = PortfolioLedger(tmp_path / 'portfolio.db', 'test', Decimal('5000'))
    try:
        return SessionExecutor(ledger, config).semantic_policy_document()
    finally:
        ledger.close()


def test_retirement_changes_execution_and_metric_identity(tmp_path):
    legacy = semantic_document(tmp_path, {})
    retired = semantic_document(tmp_path, POLICY)
    assert retired.get('enrichment_capability_policy') == 'optional_enrichment_retirement_v1'
    assert retired.get('retired_enrichment_capabilities') == {
        'commodity_futures_curves': 'maturity_identified_curve_evidence_unavailable'}
    before = _context(cohort_policies=(_policy(execution_policy=legacy),))
    after = _context(cohort_policies=(_policy(execution_policy=retired),))
    assert after.config_hash != before.config_hash
    assert after.behavior_hash == before.behavior_hash


@pytest.mark.parametrize('invalid', INVALID)
def test_executor_refuses_unknown_or_incomplete_retirement(tmp_path, invalid):
    with pytest.raises(ValueError, match='enrichment_capability_policy'):
        semantic_document(tmp_path, invalid)


@pytest.mark.parametrize('invalid', INVALID)
def test_metric_policy_schema_rejects_unknown_or_incomplete_retirement(tmp_path, invalid):
    policy = semantic_document(tmp_path, {})
    policy.update(deepcopy(invalid))
    with pytest.raises(ValueError, match='enrichment_capability_policy'):
        _context(cohort_policies=(_policy(execution_policy=policy),))


def test_generation_cannot_mix_cohort_retirement_scopes(tmp_path):
    legacy = semantic_document(tmp_path, {})
    retired = {**legacy, **deepcopy(POLICY)}
    with pytest.raises(ValueError, match='enrichment.*conflict'):
        _context(cohort_policies=(
            _policy('one', execution_policy=legacy),
            _policy('two', execution_policy=retired),
        ))


@pytest.mark.parametrize('fingerprint', [source_configuration_fingerprint,
    public_source_configuration_fingerprint, public_source_configuration_declaration])
@pytest.mark.parametrize('invalid', INVALID)
def test_source_identity_rejects_invalid_retirement_before_reuse(fingerprint, invalid):
    with pytest.raises(ValueError, match='enrichment_capability_policy'):
        fingerprint({'autoresearch': deepcopy(invalid)})


@pytest.mark.parametrize('fingerprint', [source_configuration_fingerprint,
    public_source_configuration_fingerprint])
def test_retirement_cannot_reuse_accepted_legacy_sources(tmp_path, fingerprint):
    original = {'generation': 'gen_001', 'session': '2026-10-19', 'commit': 'a' * 40,
        'configuration': fingerprint({'autoresearch': {}})}
    changed = {**original, 'configuration': fingerprint({'autoresearch': deepcopy(POLICY)})}
    store = SourceInputStore(tmp_path / 'cache', accepted_dir=tmp_path / 'accepted')
    payload = {'fred': {'rate': 1}}
    store.freeze(original, payload, acquired_at=datetime(2026, 10, 19, 20, tzinfo=timezone.utc))
    with pytest.raises(SourceInputError, match='identity mismatch'):
        store.freeze(changed, {'fred': {'rate': 2}})
    assert store.load_frozen(original) == payload


def test_absent_policy_preserves_legacy_public_fingerprint(tmp_path):
    assert public_source_configuration_fingerprint({'autoresearch': {}}) == (
        '32545ad85f6d0216d281a18e1019221ccc193bb7f862ee3e9b12333c9cf05680')
    legacy = semantic_document(tmp_path, {})
    assert not set(POLICY).intersection(legacy)


def test_public_declaration_binds_retirement_to_frozen_profile():
    ar = {'stabilization_profile_policy': 'supported_sixteen_books_v1',
        'paper_ledger': {'policy_id': 'foundation-generation-config-' + 'a' * 64}}
    original = public_source_configuration_declaration({'autoresearch': ar})
    changed = public_source_configuration_declaration({'autoresearch': {**ar, **deepcopy(POLICY)}})
    assert changed['_source_public_configuration']['profile_sha256'] == 'a' * 64
    assert changed['_source_public_configuration']['configuration_sha256'] != (
        original['_source_public_configuration']['configuration_sha256'])
