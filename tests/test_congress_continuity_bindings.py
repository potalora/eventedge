"""Audit warnings require new frozen identities and explicit display-only scope."""
from copy import deepcopy
from datetime import datetime, timezone

import pytest

from test_enrichment_policy_bindings import semantic_document
from test_metric_epoch_runtime import _context, _policy
from tradingagents.strategies.orchestration.source_inputs import (
    SourceInputError, SourceInputStore, public_source_configuration_declaration,
    public_source_configuration_fingerprint, source_configuration_fingerprint,
)


KEY = 'congress_audit_continuity_policy'
VALUE = 'recent_snapshot_warning_v1'
PREREQUISITES = {
    'congress_disclosure_policy': 'display_audit_only_v1',
    'stabilization_profile_policy': 'supported_sixteen_books_v1',
    'disabled_strategies': {'congressional_trades': 'display_audit_only_v1'},
}
CONFIGURED = {**PREREQUISITES, KEY: VALUE}
INVALID = [
    {**CONFIGURED, KEY: 'ignore_provider_errors'},
    {**CONFIGURED, KEY: True},
    {KEY: VALUE},
    {key: deepcopy(value) for key, value in CONFIGURED.items() if key != 'congress_disclosure_policy'},
    {key: deepcopy(value) for key, value in CONFIGURED.items() if key != 'stabilization_profile_policy'},
    {**CONFIGURED, 'disabled_strategies': {}},
    {**CONFIGURED, 'disabled_strategies': {'congressional_trades': 'ignore_failures'}},
]


def context(document, *, disabled=PREREQUISITES['disabled_strategies']):
    return _context(strategies=('congressional_trades', 'litigation'), disabled_strategies=disabled,
        cohort_policies=(_policy(execution_policy=document),))


def test_warning_policy_rotates_executor_and_epoch_identity(tmp_path):
    old = semantic_document(tmp_path, PREREQUISITES)
    new = semantic_document(tmp_path, CONFIGURED)
    assert new.get(KEY) == VALUE
    assert context(old).config_hash != context(new).config_hash
    assert context(old).behavior_hash == context(new).behavior_hash


@pytest.mark.parametrize('invalid', INVALID)
def test_executor_rejects_unknown_policy_or_missing_prerequisite(tmp_path, invalid):
    with pytest.raises(ValueError, match='congress_audit_continuity_policy'):
        semantic_document(tmp_path, invalid)


@pytest.mark.parametrize('entrypoint', [source_configuration_fingerprint,
    public_source_configuration_fingerprint, public_source_configuration_declaration])
@pytest.mark.parametrize('invalid', INVALID)
def test_source_identity_rejects_unknown_policy_or_missing_prerequisite(entrypoint, invalid):
    with pytest.raises(ValueError, match='congress_audit_continuity_policy'):
        entrypoint({'autoresearch': deepcopy(invalid)})


@pytest.mark.parametrize('key_value', ['ignore_provider_errors', True, {}, []])
def test_metric_schema_rejects_forged_warning_policy(tmp_path, key_value):
    document = semantic_document(tmp_path, PREREQUISITES)
    document[KEY] = key_value
    with pytest.raises(ValueError, match='congress_audit_continuity_policy'):
        context(document)


def test_metric_warning_requires_stabilization_scope(tmp_path):
    document = semantic_document(tmp_path, {})
    document[KEY] = VALUE
    with pytest.raises(ValueError, match='congress_audit_continuity_policy'):
        context(document)


@pytest.mark.parametrize('disabled', [{}, {'congressional_trades': 'ignore_failures'}])
def test_metric_warning_requires_explicit_congress_exclusion(tmp_path, disabled):
    document = semantic_document(tmp_path, PREREQUISITES)
    document[KEY] = VALUE
    with pytest.raises(ValueError, match='congress_audit_continuity_policy'):
        context(document, disabled=disabled)


def test_generation_cannot_mix_warning_and_strict_cohort_policies(tmp_path):
    old = semantic_document(tmp_path, PREREQUISITES)
    new = {**old, KEY: VALUE}
    with pytest.raises(ValueError, match='congress.*conflict'):
        _context(strategies=('congressional_trades', 'litigation'),
            disabled_strategies=PREREQUISITES['disabled_strategies'], cohort_policies=(
                _policy('one', execution_policy=old), _policy('two', execution_policy=new)))


@pytest.mark.parametrize('fingerprint', [source_configuration_fingerprint,
    public_source_configuration_fingerprint])
def test_warning_cannot_reinterpret_accepted_strict_source_bundle(tmp_path, fingerprint):
    original = {'generation': 'gen_001', 'session': '2026-10-19', 'commit': 'a' * 40,
        'configuration': fingerprint({'autoresearch': deepcopy(PREREQUISITES)})}
    changed = {**original, 'configuration': fingerprint({'autoresearch': deepcopy(CONFIGURED)})}
    store = SourceInputStore(tmp_path / 'cache', accepted_dir=tmp_path / 'accepted')
    payload = {'congress': {'error': 'House filing reconciliation gap'}}
    store.freeze(original, payload, acquired_at=datetime(2026, 10, 19, 20, tzinfo=timezone.utc))
    with pytest.raises(SourceInputError, match='identity mismatch'):
        store.load_frozen(changed)
    assert store.load_frozen(original) == payload


def test_absent_warning_preserves_legacy_public_hash_and_semantics(tmp_path):
    assert public_source_configuration_fingerprint({'autoresearch': {}}) == (
        '32545ad85f6d0216d281a18e1019221ccc193bb7f862ee3e9b12333c9cf05680')
    old = semantic_document(tmp_path, PREREQUISITES)
    explicit_none = semantic_document(tmp_path, {**PREREQUISITES, KEY: None})
    assert KEY not in old and old == explicit_none
    assert context(old).config_hash == context(explicit_none).config_hash


def test_public_configuration_binds_new_warning_to_frozen_profile():
    ar = {**deepcopy(PREREQUISITES), 'paper_ledger': {'policy_id': 'reviewed-generation-config-' + 'a' * 64}}
    old = public_source_configuration_declaration({'autoresearch': ar})
    new = public_source_configuration_declaration({'autoresearch': {**ar, KEY: VALUE}})
    assert new['_source_public_configuration']['profile_sha256'] == 'a' * 64
    assert new['_source_public_configuration']['configuration_sha256'] != old['_source_public_configuration']['configuration_sha256']
