"""A Congress-only amendment preserves the existing sixteen ledger identities."""
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


PROFILE=Path(__file__).parents[1]/'config/profiles/stabilization-v3.json'
VERSION='congress-audit-retirement-v1'
REASON='congress_source_retired_v1'


def api():
    name='tradingagents.strategies.orchestration.source_retirement_profile'
    assert importlib.util.find_spec(name) is not None,'verified Congress retirement profile is missing'
    return __import__(name,fromlist=['profile_binding'])


def amendment(text=None):
    text=PROFILE.read_text() if text is None else text
    previous=json.loads(text);config=deepcopy(previous);ar=config['autoresearch']
    ar['disabled_strategies']['congressional_trades']=REASON
    ar['retired_sources']['congress']={'reason':REASON,'strategies':['congressional_trades']}
    ar['congress_disclosure_policy']=None;ar['congress_audit_continuity_policy']=None
    config['source_retirement']={'version':VERSION,'effective_session':'2026-11-11',
        'previous_config_sha256':hashlib.sha256(text.encode()).hexdigest(),'previous_config_text':text}
    return config


def test_legacy_profile_and_ledger_identity_remain_unchanged():
    from tradingagents.strategies.orchestration.operating_contract import profile_contract
    raw=PROFILE.read_bytes();config=json.loads(raw);digest=hashlib.sha256(raw).hexdigest()
    assert api().profile_binding(config) is None
    assert api().ledger_profile_sha256(config,digest)==digest
    contract=profile_contract(config,profile_sha256=digest)
    assert len(contract.roster)==16 and len(contract.strategies)==12
    assert contract.declared_exclusions['congressional_trades']=='display_audit_only_v1'


def test_exact_retirement_proof_retains_previous_bytes_and_new_report_digest():
    from tradingagents.strategies.orchestration.operating_contract import profile_contract
    config=amendment();original=deepcopy(config)
    binding=api().profile_binding(config,'a'*64)
    assert binding['previous_config_text']==PROFILE.read_text()
    assert binding['previous_config_sha256']==hashlib.sha256(PROFILE.read_bytes()).hexdigest()
    assert binding['previous_config']==json.loads(PROFILE.read_bytes())
    assert binding['effective_session']=='2026-11-11'
    old=profile_contract(binding['previous_config'],profile_sha256=binding['previous_config_sha256'])
    retired=profile_contract(config,profile_sha256='a'*64)
    assert retired.profile_sha256=='a'*64
    assert retired.ledger_policy_id==old.ledger_policy_id
    assert retired.horizon_policies==old.horizon_policies
    assert retired.roster==old.roster and retired.strategies==old.strategies
    assert retired.supported_strategies==old.supported_strategies
    assert retired.declared_exclusions['congressional_trades']==REASON
    assert retired.exclusion_scope_sha256!=old.exclusion_scope_sha256
    assert config==original


@pytest.mark.parametrize('change',[
    lambda c:c.update(execution={'mode':'live','execution_enabled':True}),
    lambda c:c['autoresearch'].update(max_positions=999),
    lambda c:c['autoresearch']['paper_ledger'].update(slippage_bps='0'),
    lambda c:c['autoresearch']['retired_sources'].update(fred={'reason':REASON,'strategies':['commodity_macro']}),
    lambda c:c['autoresearch']['retired_sources']['congress'].update(strategies=['commodity_macro']),
    lambda c:c['autoresearch']['disabled_strategies'].update(congressional_trades='display_audit_only_v1'),
    lambda c:c['autoresearch'].update(congress_disclosure_policy='display_audit_only_v1'),
    lambda c:c['autoresearch'].update(congress_audit_continuity_policy='recent_snapshot_warning_v1'),
    lambda c:c['source_retirement'].update(extra=True),
    lambda c:c['source_retirement'].update(previous_config_sha256='0'*64),
    lambda c:c['source_retirement'].update(effective_session='2026-11-31'),
    lambda c:c['source_retirement'].update(effective_session='2026-11-11T00:00:00Z'),
    lambda c:c['source_retirement'].update(version='anything'),
    lambda c:c.update(source_retirement=None),
    lambda c:c['execution'].update(execution_enabled=0),
])
def test_any_other_configuration_or_proof_change_is_rejected(change):
    config=amendment();change(config)
    with pytest.raises(ValueError,match='source_retirement'):api().profile_binding(config)


def test_previous_profile_must_be_secret_free_supported_and_not_previously_amended():
    for change in (lambda c:c.update(api_key='sensitive'),
            lambda c:c['autoresearch'].update(stabilization_profile_policy=None),
            lambda c:c.update(source_retirement={})):
        previous=json.loads(PROFILE.read_bytes());change(previous)
        config=amendment(json.dumps(previous))
        with pytest.raises(ValueError,match='source_retirement'):api().profile_binding(config)


def test_original_json_duplicates_and_different_bytes_cannot_satisfy_proof():
    config=amendment()
    config['source_retirement']['previous_config_text']+=' '
    with pytest.raises(ValueError,match='source_retirement'):api().profile_binding(config)
    text=PROFILE.read_text().replace('{','{"duplicate":1,"duplicate":2,',1)
    config=amendment(text)
    with pytest.raises(ValueError,match='source_retirement'):api().profile_binding(config)


def test_profile_factory_and_loader_keep_original_ledger_binding(tmp_path):
    from tradingagents.strategies.orchestration.generation_config import apply_generation_config,read_generation_config
    config=api().make_retired_profile(PROFILE.read_text(),'2026-11-11')
    assert config==amendment()
    path=tmp_path/'retired.json';path.write_text(json.dumps(config,sort_keys=True)+'\n')
    _,_,new_digest=read_generation_config(path)
    old_path=tmp_path/'original.json';old_path.write_bytes(PROFILE.read_bytes())
    previous=apply_generation_config({},old_path,hashlib.sha256(PROFILE.read_bytes()).hexdigest())
    current=apply_generation_config({},path,new_digest)
    assert current['autoresearch']['paper_ledger']['policy_id']==previous['autoresearch']['paper_ledger']['policy_id']
    assert current['source_retirement']['previous_config_sha256']!=new_digest


def test_public_source_identity_uses_actual_amended_profile_while_ledger_keeps_original(tmp_path):
    from tradingagents.strategies.orchestration.generation_config import apply_generation_config
    from tradingagents.strategies.orchestration.operating_contract import profile_contract
    from tradingagents.strategies.orchestration.source_inputs import public_source_configuration_declaration
    config=amendment();path=tmp_path/'retired.json';path.write_text(json.dumps(config,indent=2)+'\n')
    digest=hashlib.sha256(path.read_bytes()).hexdigest()
    runtime=apply_generation_config({},path,digest)
    contract=profile_contract(config,profile_sha256=digest)
    for effective in (runtime,contract.config):
        declaration=public_source_configuration_declaration(effective)['_source_public_configuration']
        assert declaration['profile_sha256']==digest
        assert effective['autoresearch']['paper_ledger']['policy_id'].endswith(config['source_retirement']['previous_config_sha256'])


def test_loader_rejects_an_unverified_delta_before_applying_it(tmp_path):
    from tradingagents.strategies.orchestration.generation_config import apply_generation_config,read_generation_config
    config=amendment();config['autoresearch']['paper_ledger']['slippage_bps']='0'
    path=tmp_path/'invalid-retirement.json';path.write_text(json.dumps(config))
    digest=hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ValueError,match='generation_config'):read_generation_config(path,digest)
    with pytest.raises(ValueError,match='generation_config'):apply_generation_config({},path,digest)


def test_actual_profile_digest_is_derived_and_cannot_be_overridden_in_frozen_input(tmp_path):
    from tradingagents.strategies.orchestration.generation_config import read_generation_config
    config=amendment();config['_source_retirement_profile_sha256']='f'*64
    path=tmp_path/'invalid-derived-digest.json';path.write_text(json.dumps(config))
    with pytest.raises(ValueError,match='generation_config'):
        read_generation_config(path,hashlib.sha256(path.read_bytes()).hexdigest())


def test_retired_declaration_excludes_payload_and_requires_four_zero_signal_health_records():
    from tradingagents.strategies.orchestration.operating_contract import profile_contract,configure_strategies
    from tradingagents.strategies.orchestration import source_lifecycle_policy as lifecycle
    from tradingagents.strategies.modules import get_paper_trade_strategies
    config=amendment();contract=profile_contract(config,profile_sha256='a'*64)
    strategies=configure_strategies(get_paper_trade_strategies(),contract.config['autoresearch'],full_config=config)
    declaration=lifecycle.declaration(contract.config['autoresearch'],strategies)
    scope=lifecycle.declared_scope(declaration,contract.config['autoresearch'],strategies)
    records=[]
    for policy_id in contract.horizon_policies.values():
        for name,reason in lifecycle.exclusions(scope).items():
            records.append({'policy_id':policy_id,'strategy':name,'status':'disabled_by_policy','signal_count':0,
                'evidence':{'reason':reason,'candidate_count':0,**lifecycle.health_evidence(scope,name)}})
    lifecycle.validate_health(scope,records,contract.horizon_policies.values())
    with pytest.raises(ValueError):lifecycle.declared_scope(declaration|{'congress':{}},contract.config['autoresearch'],strategies)
    with pytest.raises(ValueError):configure_strategies(get_paper_trade_strategies(),contract.config['autoresearch'])


def test_runtime_source_scope_accepts_derived_financial_fields_but_requires_exact_proof():
    from tradingagents.strategies.orchestration.operating_contract import profile_contract,configure_strategies
    from tradingagents.strategies.modules import get_paper_trade_strategies
    config=amendment();runtime=profile_contract(config,profile_sha256='a'*64).config
    ar=runtime['autoresearch'];ar.update(state_dir='/owned/runtime',total_capital=10000,risk_per_trade=.02)
    strategies=configure_strategies(get_paper_trade_strategies(),ar,full_config=runtime)
    assert len(strategies)==12
    runtime['source_retirement']['previous_config_sha256']='0'*64
    with pytest.raises(ValueError):configure_strategies(get_paper_trade_strategies(),ar,full_config=runtime)


def test_amendment_reopens_populated_ledger_without_changing_financial_bytes_or_pending_work(tmp_path):
    from datetime import date
    from decimal import Decimal
    from tradingagents.strategies.orchestration.generation_config import apply_generation_config
    from tradingagents.strategies.state.portfolio_ledger import PortfolioLedger
    from test_portfolio_ledger import _signal,_intent,_fill
    old_path=tmp_path/'original.json';old_path.write_bytes(PROFILE.read_bytes())
    new_path=tmp_path/'retired.json';new_path.write_text(json.dumps(amendment(),sort_keys=True)+'\n')
    previous=apply_generation_config({},old_path,hashlib.sha256(old_path.read_bytes()).hexdigest())
    current=apply_generation_config({},new_path,hashlib.sha256(new_path.read_bytes()).hexdigest())
    old_policy=previous['autoresearch']['paper_ledger'];new_policy=current['autoresearch']['paper_ledger']
    db=tmp_path/'portfolio.db'
    ledger=PortfolioLedger(db,'horizon_30d_size_5k',Decimal('5000'),paper_ledger_config=old_policy)
    signal=_signal(policy_id=old_policy['policy_id']);intent=_intent()
    ledger.record_signal(signal);ledger.stage_intent(intent);ledger.apply_fill(intent,_fill())
    pending_signal=_signal(signal_id='signal-pending',epoch_id='earlier-epoch',policy_id=old_policy['policy_id'])
    pending=_intent(intent_id='intent-pending',signal_ids=('signal-pending',),requested_qty=7)
    ledger.record_signal(pending_signal);ledger.stage_intent(pending)
    positions=ledger.open_positions();ledger.close();before=db.read_bytes()
    ledger=PortfolioLedger(db,'horizon_30d_size_5k',Decimal('5000'),paper_ledger_config=new_policy)
    assert ledger.opening_cash()==Decimal('5000') and ledger.open_positions()==positions
    assert ledger.pending_intents(date(2026,8,3))==[pending]
    assert ledger.read_signals(epoch_id='earlier-epoch')==[pending_signal]
    assert len(ledger.read_fills())==1
    ledger.close()
    assert db.read_bytes()==before
