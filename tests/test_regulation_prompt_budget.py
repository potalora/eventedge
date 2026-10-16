"""Offline planning measurements are a conservative proxy, never entitlement."""
import importlib
import socket
from copy import deepcopy

import pytest


def budget():
    return importlib.import_module('tradingagents.strategies.data_sources.regulation_prompt_budget')


def test_exact_ordinary_text_count_is_offline_and_hash_bound(monkeypatch):
    b = budget()
    monkeypatch.setattr(socket, 'socket', lambda *a, **k: pytest.fail('network forbidden'))
    proof = b.prepare_proof('Assess <|endoftext|> literally.', 'Source α\n$1.20')
    assert proof['status'] == 'measured'
    assert proof['input_tokens'] > 0
    assert proof['within_planning_limit'] is True
    assert proof['planning_limit'] == 475000
    assert proof['planning_window'] == 500000 and proof['reserved_tokens'] == 25000
    assert proof['not_transport_entitlement'] is True
    assert b.validate_proof('Assess <|endoftext|> literally.', 'Source α\n$1.20', proof) == proof
    b.require_dispatchable(proof)
    with pytest.raises(ValueError):
        b.validate_proof('Different', 'Source α\n$1.20', proof)


def test_over_budget_proof_preserves_measurement_and_refuses_dispatch():
    b = budget()
    proof = b.prepare_proof('Assess all.', 'word ' * 475001)
    assert proof['status'] == 'measured' and not proof['within_planning_limit']
    assert proof['input_tokens'] > 475000
    with pytest.raises(ValueError, match='invalid_regulation_prompt_planning_budget_exceeded'):
        b.require_dispatchable(proof)
    assert b.validate_proof('Assess all.', 'word ' * 475001, proof) == proof


@pytest.mark.parametrize('mutation', ['count', 'limit', 'hash', 'entitlement'])
def test_measured_proof_tamper_is_rejected(mutation):
    b = budget(); proof = b.prepare_proof('Assess.', 'Complete supplied source.')
    if mutation == 'count': proof['input_tokens'] += 1
    if mutation == 'limit': proof['planning_limit'] += 1
    if mutation == 'hash': proof['vocabulary_sha256'] = '0' * 64
    if mutation == 'entitlement': proof['not_transport_entitlement'] = False
    with pytest.raises(ValueError): b.validate_proof('Assess.', 'Complete supplied source.', proof)


@pytest.mark.parametrize('failure', ['missing', 'tampered', 'version'])
def test_unavailable_counter_retains_failed_proof_without_network(monkeypatch, failure):
    b = budget()
    if failure == 'missing':
        monkeypatch.setattr(b, '_vocabulary_bytes', lambda: (_ for _ in ()).throw(FileNotFoundError()))
    elif failure == 'tampered': monkeypatch.setattr(b, '_vocabulary_bytes', lambda: b'wrong')
    else: monkeypatch.setattr(b, '_installed_version', lambda: '0.11.0')
    proof = b.prepare_proof('Assess.', 'Complete source.')
    assert proof['status'] == 'unavailable' and proof['input_tokens'] is None
    assert proof['within_planning_limit'] is False and proof['failure_reason']
    with pytest.raises(ValueError): b.require_dispatchable(proof)
    assert b.validate_proof('Assess.', 'Complete source.', proof) == proof


@pytest.mark.parametrize('policy', ['O200K_base_planning_v1', 'o200k_base', '', True])
def test_policy_aliases_and_orphan_policy_are_rejected(policy):
    b = budget()
    with pytest.raises(ValueError): b.configured({'regulation_prompt_budget_policy': policy})


def test_policy_requires_v8_even_for_empty_corpus():
    b = budget()
    assert b.configured({}) is None
    with pytest.raises(ValueError): b.configured({'regulation_prompt_budget_policy': b.POLICY})
    assert b.configured({'regulation_prompt_budget_policy': b.POLICY,
        'regulation_evidence_policy': 'complete_document_text_v1',
        'graphics_execution_policy': 'single_target_compact_v1'}) == b.POLICY


def test_special_token_looking_printing_is_not_a_special_token():
    b = budget()
    proof = b.prepare_proof('', '<|endoftext|>')
    assert proof['input_tokens'] == 7


def test_missing_dependency_is_a_finite_failed_receipt(monkeypatch):
    b = budget()
    from importlib.metadata import PackageNotFoundError
    monkeypatch.setattr(b, '_installed_version', lambda: (_ for _ in ()).throw(PackageNotFoundError('tiktoken')))
    proof = b.prepare_proof('Assess.', 'Complete source.')
    assert proof['failure_reason'] == 'tokenizer_unavailable'
    assert proof['input_tokens'] is None
    with pytest.raises(ValueError, match='invalid_regulation_prompt_tokenizer_unavailable'):
        b.require_dispatchable(proof)


def test_future_available_counter_does_not_relabel_historical_unavailable_proof(monkeypatch):
    b = budget()
    with monkeypatch.context() as patch:
        patch.setattr(b, '_vocabulary_bytes', lambda: b'tampered')
        failed = b.prepare_proof('Assess.', 'Complete source.')
    assert b.prepare_proof('Assess.', 'Complete source.')['status'] == 'measured'
    assert b.validate_proof('Assess.', 'Complete source.', failed) == failed
    with pytest.raises(ValueError): b.require_dispatchable(failed)


def test_no_tokenizer_registry_or_cache_fallback(monkeypatch,tmp_path):
    b = budget()
    import tiktoken
    monkeypatch.setattr(tiktoken,'get_encoding',lambda *a,**kw:pytest.fail('registry fallback'))
    monkeypatch.setattr(tiktoken,'encoding_for_model',lambda *a,**kw:pytest.fail('model alias fallback'))
    monkeypatch.setenv('TIKTOKEN_CACHE_DIR',str(tmp_path/'absent'))
    assert b.prepare_proof('Assess.', 'Complete source.')['status']=='measured'
    assert not (tmp_path/'absent').exists()


def test_assessment_config_rejects_orphan_budget_policy():
    from tradingagents.strategies.data_sources.regulation_assessment import configured
    with pytest.raises(ValueError):
        configured({'regulation_prompt_budget_policy':'o200k_base_planning_v1'})


def test_assessment_preparation_binds_final_system_user_and_preserves_legacy():
    import json,hashlib
    from test_regulation_evidence import build
    from tradingagents.strategies.data_sources.regulation_assessment import prepare_request
    evidence=build();legacy=prepare_request(evidence)
    prepared=prepare_request(evidence,prompt_budget_policy=budget().POLICY)
    provenance=json.loads(prepared.context.provenance_json)
    proof=provenance['prompt_budget_proof']
    assert proof['user_sha256']==hashlib.sha256(prepared.user.encode()).hexdigest()
    assert proof['system_sha256']==hashlib.sha256(prepared.system.encode()).hexdigest()
    assert proof['prompt_bytes']==provenance['prompt_bytes']
    assert json.loads(prepared.user)['units']==json.loads(legacy.user)['units']
    assert 'prompt_budget_proof' not in json.loads(legacy.context.provenance_json)
    assert prepare_request(evidence).user==legacy.user


def test_budget_unavailable_keeps_complete_prepared_source_provenance(monkeypatch):
    import json
    from test_regulation_evidence import build
    from tradingagents.strategies.data_sources.regulation_assessment import prepare_request
    b=budget();monkeypatch.setattr(b,'_vocabulary_bytes',lambda:b'tampered')
    evidence=build();prepared=prepare_request(evidence,prompt_budget_policy=b.POLICY,
        assessment_policy='recorded_abstention_v1')
    provenance=json.loads(prepared.context.provenance_json)
    assert provenance['source_material_proof']['evidence_sha256']==evidence['manifest_sha256']
    assert provenance['prompt_budget_proof']['failure_reason']=='vocabulary_hash'
    with pytest.raises(ValueError):b.require_dispatchable(provenance['prompt_budget_proof'])


def test_byte_limit_failure_retains_exact_prompt_binding_without_tokenization(monkeypatch):
    b=budget()
    monkeypatch.setattr(b,'_counter',lambda:pytest.fail('oversize input must not tokenize'))
    text='x'*(b.MAX_PROMPT_BYTES+1)
    proof=b.prepare_proof('',text)
    assert proof['status']=='rejected_bytes' and proof['input_tokens'] is None
    assert proof['prompt_bytes']==len(text)
    assert proof['failure_reason']=='byte_limit_exceeded'
    assert b.validate_proof('',text,proof)==proof
    with pytest.raises(ValueError,match='invalid_regulation_prompt_byte_limit_exceeded'):
        b.require_dispatchable(proof)


@pytest.mark.parametrize('damage',['limit','entitlement','encoding_alias','boolean_count','invented_status'])
def test_dispatch_guard_rejects_resealed_policy_or_type_tamper(damage):
    from tradingagents.strategies.data_sources.regulation_evidence import digest
    b=budget();proof=b.prepare_proof('Assess.','Complete source.')
    if damage=='limit':proof['planning_limit']+=1
    if damage=='entitlement':proof['not_transport_entitlement']=False
    if damage=='encoding_alias':proof['encoding']='o200k_harmony'
    if damage=='boolean_count':proof['input_tokens']=True
    if damage=='invented_status':proof['status']='accepted'
    proof['proof_sha256']=digest({k:v for k,v in proof.items()if k!='proof_sha256'})
    with pytest.raises(ValueError):b.require_dispatchable(proof)


def test_prior_undeployed_grid_policy_cannot_alias_compact_budget():
    b=budget()
    with pytest.raises(ValueError,match='invalid_regulation_prompt_budget_policy'):
        b.configured({'regulation_prompt_budget_policy':b.POLICY,
            'regulation_evidence_policy':'complete_document_text_v1',
            'graphics_execution_policy':'single_target_grid_v1'})
