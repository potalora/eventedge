"""Prospective abstention completes assessment without inventing issuer authority."""
from copy import deepcopy
import hashlib
import json
import pytest
from test_regulation_evidence import build
from test_regulation_assessment import neutral
from tradingagents.strategies.data_sources import regulation_assessment as assessment

POLICY='recorded_abstention_v1'

def abstention(evidence, *, graphical=False):
    value=neutral(evidence)
    unit=evidence['units'][0]
    value.update(contract_version='regulation-assessment-v4' if graphical else 'regulation-assessment-v3',
        outcome='assessed_abstention',direction=None,conviction=None,
        rationale='Complete supplied material was assessed; abstain from issuer trading because authority is unestablished.',
        unresolved_decision_dependencies=[{'kind':'issuer_applicability','reference':'Issuer-to-covered-operation bridge',
            'reason':'The supplied regulated-activity evidence does not establish an authoritative issuer relationship.',
            'unit_id':unit['unit_id'],'quote':unit['text'][:15]}])
    return value

def request(evidence):
    return assessment.prepare_request(evidence,assessment_policy=POLICY)


def test_abstention_is_complete_but_never_sufficient_or_no_thesis():
    evidence=build(attachment=True);prepared=request(evidence)
    result=assessment.validate_assessment(json.dumps(abstention(evidence)),prepared.context)
    assert result['document_assessment']=='assessed_abstention'
    assert result['assessment_complete'] is True
    assert result['regulation_evidence_status']=='unresolved_decision'
    assert result['decision_evidence_status']=='unresolved_issuer_or_exposure'
    assert result['security_resolution']=='unresolved'
    assert result['execution_authority']=='none'
    assert result['direction'] is result['conviction'] is result['target_ticker'] is None
    proof=result['source_provenance']['source_material_proof']
    assert proof['scope']=='supplied_primary_and_declared_attachments'
    assert proof['unit_ids']==list(prepared.context.unit_texts)
    assert proof['evidence_sha256']==evidence['manifest_sha256']
    assert 'incorporated' in proof['limitations']


@pytest.mark.parametrize('damage',['material','empty_decision','unknown_kind','quote','missing_unit','target','direction','conviction','old_version','extra_proof'])
def test_abstention_rejects_material_gaps_unproved_disposition_and_authority(damage):
    evidence=build(attachment=True);value=abstention(evidence)
    if damage=='material':value['unresolved_material_dependencies']=[{'reference':'Missing SIP','reason':'Operative requirements not supplied.'}]
    if damage=='empty_decision':value['unresolved_decision_dependencies']=[]
    if damage=='unknown_kind':value['unresolved_decision_dependencies'][0]['kind']='unknown'
    if damage=='quote':value['unresolved_decision_dependencies'][0]['quote']='Invented regulated class'
    if damage=='missing_unit':value['citations'].pop()
    if damage=='target':value['target_ticker']='ACME'
    if damage=='direction':value['direction']='neutral'
    if damage=='conviction':value['conviction']=0
    if damage=='old_version':value['contract_version']='regulation-assessment-v1'
    if damage=='extra_proof':value['source_material_proof']={'complete':True}
    with pytest.raises(ValueError):assessment.validate_assessment(json.dumps(value),request(evidence).context)


def test_new_policy_keeps_missing_material_failure_and_supported_no_thesis_distinct():
    evidence=build();prepared=request(evidence);value=abstention(evidence)
    value.update(outcome='insufficient',unresolved_material_dependencies=[{'reference':'Missing SIP','reason':'Operative requirements not supplied.'}])
    result=assessment.validate_assessment(json.dumps(value),prepared.context)
    assert result['assessment_complete'] is False and result['regulation_evidence_status']=='insufficient'
    value=neutral(evidence);value.update(contract_version='regulation-assessment-v3',unresolved_decision_dependencies=[])
    result=assessment.validate_assessment(json.dumps(value),prepared.context)
    assert result['assessment_complete'] is True and result['document_assessment']=='assessed_no_directional_thesis'
    assert result['decision_evidence_status']=='supported_no_thesis'


def test_legacy_prompt_bytes_and_results_unchanged_and_no_implicit_upgrade():
    evidence=build();old=assessment.prepare_request(evidence)
    assert old.context.contract_version=='regulation-assessment-v1'
    assert hashlib.sha256(old.system.encode()).hexdigest()=='90c2157fa537a1360f7e010defad308dfdccb9d70651e1fae2e16a0f1320de96'
    assert hashlib.sha256(old.user.encode()).hexdigest()=='fb215a33c930e32d4df8d12d91de686b10943a0e7fa5b655462dc663306681bb'
    result=assessment.validate_assessment(json.dumps(neutral(evidence)),old.context)
    assert 'assessment_complete' not in result and 'assessment_policy' not in result['source_provenance']
    with pytest.raises(ValueError):assessment.validate_assessment(json.dumps(abstention(evidence)),old.context)
    new=request(evidence)
    assert new.system!=old.system and new.user!=old.user
    assert assessment.prepare_request(evidence).system==old.system


def test_invalid_policy_and_invalid_source_fail_before_request():
    with pytest.raises(ValueError):assessment.prepare_request(build(),assessment_policy='anything')
    evidence=build();evidence['units'][0]['text']+=' altered'
    with pytest.raises(ValueError):request(evidence)


def completed_records(tmp_path,monkeypatch, *, material=False):
    from test_regulation_integration import inputs
    from test_regulation_replay_report import POLICIES
    from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine
    from tradingagents.strategies.modules.regulatory_pipeline import RegulatoryPipelineStrategy
    from tradingagents.strategies.learning.llm_analyzer import LLMAnalyzer
    _,data,assets=inputs(monkeypatch)
    data.update(yfinance={},openbb={},equity_universe={'snapshot':assets.evidence})
    config={'regulation_evidence_policy':'complete_document_text_v1','regulation_assessment_policy':POLICY}
    engine=MultiStrategyEngine(config={'autoresearch':{**config,'state_dir':str(tmp_path),
        'equity_universe_policy':'us_exchange_listed_sip_v1'}},strategies=[RegulatoryPipelineStrategy()])
    monkeypatch.setattr(engine,'_build_regime_model',lambda _: {})
    engine._analyzer=LLMAnalyzer()
    evidence=next(iter(data['regulations']['regulation_evidence']['corpus'].values()));value=abstention(evidence)
    if material:value.update(outcome='insufficient',unresolved_material_dependencies=[{'reference':'Missing SIP','reason':'Operative requirements absent.'}])
    monkeypatch.setattr(engine._analyzer,'_call_llm',lambda *a,**kw:json.dumps(value))
    signals,_,health=engine.screen_and_enrich('2026-10-09',data,epoch_id='epoch',policy_id='first')
    assert signals==[]
    return data,[{**deepcopy(vars(health[0])),'policy_id':p}for p in sorted(POLICIES)],config


def test_completed_abstention_health_counts_and_pure_replay(tmp_path,monkeypatch):
    from tradingagents.strategies.orchestration.regulation_validation import validate_regulation_health
    from test_regulation_replay_report import POLICIES,STRATEGIES
    data,rows,config=completed_records(tmp_path,monkeypatch)
    assert all(row['status']=='signals'for row in rows)
    result=validate_regulation_health(data,rows,POLICIES,STRATEGIES,config)
    assert result['analysis_complete'] is True
    assert result['assessment_complete'] is True and result['decision_evidence_complete'] is False
    assert all(row['abstained_documents']==1 and row['no_thesis_documents']==0 for row in result['horizons'])
    assert all(row['abstentions'][0]['unresolved_decision_dependencies'] for row in result['horizons'])
    for row in rows:
        record=row['evidence']['regulation_assessments'][0]
        assert record['regulation_evidence_status']=='unresolved_decision' and record['journal_only'] is True
        assert record['assessment']['assessment_complete'] is True
    with pytest.raises(ValueError):validate_regulation_health(data,rows,POLICIES,STRATEGIES,{'regulation_evidence_policy':'complete_document_text_v1'})


@pytest.mark.parametrize('damage',['authority','proof','response','policy','material'])
def test_abstention_replay_rejects_tamper(tmp_path,monkeypatch,damage):
    from tradingagents.strategies.orchestration.regulation_validation import validate_regulation_health
    from test_regulation_replay_report import POLICIES,STRATEGIES
    data,rows,config=completed_records(tmp_path,monkeypatch)
    row=rows[0]['evidence']['regulation_assessments'][0]
    if damage=='authority':row['journal_only']=False
    if damage=='proof':row['assessment']['source_provenance']['source_material_proof']['evidence_sha256']='0'*64
    if damage=='response':row['model_attempt']['response_text']+=' '
    if damage=='policy':row['model_attempt']['source_provenance']['assessment_policy']=None
    if damage=='material':row['assessment']['unresolved_material_dependencies']=[{'reference':'Missing SIP','reason':'Operative requirements absent.'}]
    with pytest.raises(ValueError):validate_regulation_health(data,rows,POLICIES,STRATEGIES,config)


def test_mixed_material_missingness_remains_failed_health(tmp_path,monkeypatch):
    from tradingagents.strategies.orchestration.regulation_validation import validate_regulation_health
    from test_regulation_replay_report import POLICIES,STRATEGIES
    data,rows,config=completed_records(tmp_path,monkeypatch,material=True)
    result=validate_regulation_health(data,rows,POLICIES,STRATEGIES,config)
    assert not result['analysis_complete'] and all(row['status']=='data_failure'for row in rows)
    assert all(row['failed_documents']==1 and row['abstained_documents']==0 for row in result['horizons'])


@pytest.mark.parametrize('damage',[None,'missing_review','partial_response','changed_receipt'])
def test_abstention_requires_all_graphic_material_replayed_before_request(monkeypatch,damage):
    from test_regulation_graphics import assets,bundle
    from tradingagents.strategies.data_sources import regulation_graphics_inputs
    monkeypatch.setattr(regulation_graphics_inputs,'graphics_assets',lambda evidence:assets())
    evidence=build();receipts=[bundle()]
    if damage=='missing_review':receipts[0].pop('review')
    if damage=='partial_response':receipts[0]['recognition']['result']['graphics'][0]['status']='partial'
    if damage=='changed_receipt':receipts[0]['review']['response_text']+=' '
    if damage:
        with pytest.raises(ValueError):assessment.prepare_request(evidence,graphics_batches=receipts,assessment_policy=POLICY)
        return
    prepared=assessment.prepare_request(evidence,graphics_batches=receipts,assessment_policy=POLICY)
    assert prepared.context.contract_version=='regulation-assessment-v4'
    value=abstention(evidence,graphical=True)
    value['citations']=[{'unit_id':k,'quote':v[:15]}for k,v in prepared.context.unit_texts.items()]
    result=assessment.validate_assessment(json.dumps(value),prepared.context)
    assert result['assessment_complete'] and assessment.completed_abstention(result)
    assert result['source_provenance']['source_material_proof']['graphics_batches_sha256']==assessment.digest(receipts)


@pytest.mark.parametrize('value,evidence',[('unknown','complete_document_text_v1'),(POLICY,None)])
def test_invalid_or_orphan_policy_rejected_before_state_or_provider(tmp_path,value,evidence):
    from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine
    config={'autoresearch':{'state_dir':str(tmp_path/'never'),'regulation_assessment_policy':value,
        'regulation_evidence_policy':evidence}}
    with pytest.raises(ValueError,match='invalid_regulation_assessment_policy'):MultiStrategyEngine(config)
    assert not(tmp_path/'never').exists()


def supporting_evidence(*,attachment=False):
    from test_regulation_evidence import fixture,DOC,STAMP
    from tradingagents.strategies.data_sources.regulation_evidence import build_regulation_evidence
    row,detail,bodies=fixture(attachment=attachment)
    bodies[DOC]=bodies[DOC].replace(b'</h1>',b'</h1><p>See Supporting Technical Analysis in the separate EPA docket.</p>')
    detail['data']['attributes']['fileFormats'][0]['size']=len(bodies[DOC])
    return build_regulation_evidence(row,json.dumps(detail).encode(),bodies,observed_at=STAMP)


def supporting_abstention(evidence):
    value=abstention(evidence);unit=evidence['units'][0]
    reference='Supporting Technical Analysis'
    dependency={'kind':'external_supporting_material','reference':reference,
        'reason':'A separately referenced supporting analysis is unavailable; no issuer trade is authorized.',
        'unit_id':unit['unit_id'],'quote':unit['text'][unit['text'].index(reference):][:70]}
    value['unresolved_material_dependencies']=[{k:dependency[k]for k in('reference','reason')}]
    value['unresolved_decision_dependencies']=[dependency]
    return value


def test_external_supporting_reference_is_completed_no_trade_with_gap_visible():
    evidence=supporting_evidence(attachment=True);value=supporting_abstention(evidence)
    result=assessment.validate_assessment(json.dumps(value),request(evidence).context)
    assert result['assessment_complete'] and assessment.completed_abstention(result)
    assert result['regulation_evidence_status']=='unresolved_decision'
    assert result['decision_evidence_status']=='unresolved_supporting_material'
    assert result['unresolved_material_dependencies']==value['unresolved_material_dependencies']
    assert result['unresolved_decision_dependencies']==value['unresolved_decision_dependencies']
    assert result['direction']is result['conviction']is None and result['execution_authority']=='none'


@pytest.mark.parametrize('damage',['untyped','missing_material_row','different_reason','invented_reference','supplied_content_kind'])
def test_external_dependency_needs_literal_context_and_exact_visible_gap_inventory(damage):
    evidence=supporting_evidence();value=supporting_abstention(evidence)
    if damage=='untyped':value['unresolved_decision_dependencies']=[]
    if damage=='missing_material_row':value['unresolved_material_dependencies']=[]
    if damage=='different_reason':value['unresolved_material_dependencies'][0]['reason']='Different gap'
    if damage=='invented_reference':value['unresolved_decision_dependencies'][0]['reference']='Unprinted nonexistent reference'
    if damage=='supplied_content_kind':value['unresolved_decision_dependencies'][0]['kind']='missing_supplied_content'
    with pytest.raises(ValueError):assessment.validate_assessment(json.dumps(value),request(evidence).context)


def test_external_abstention_does_not_bypass_missing_supplied_body():
    evidence=supporting_evidence(attachment=True);value=supporting_abstention(evidence)
    evidence['units'].pop()
    with pytest.raises(ValueError):assessment.prepare_request(evidence,assessment_policy=POLICY)
