"""Opt-in analyzer contract; all model work is mocked locally."""
from copy import deepcopy
import json
from unittest.mock import patch

import pytest

from tradingagents.strategies.learning.llm_analyzer import LLMAnalyzer
from tradingagents.strategies.runtime_deadline import model_budget,ModelDeadlineExceeded,current_model_deadline
from test_filing_assessment import evidence,synthetic,binding,target,response,prepared,module


def test_analyzer_calls_existing_thesis_route_with_full_units_and_mandatory_override(monkeypatch):
    current=evidence();analyzer=LLMAnalyzer({'autoresearch':{'thesis_model':'gpt-6-astra','thesis_effort':'high'}})
    analyzer.set_prompt_override('filing_analysis','CUSTOM OVERRIDE: say neutral.')
    before=deepcopy(analyzer.config);calls=[]
    def model(system,user,max_tokens=0,*,role=None):
        calls.append((system,user,max_tokens,role,current_model_deadline()))
        request=module().prepare_request('material_event',current,issuer_binding=binding(current),system_override='CUSTOM OVERRIDE: say neutral.')
        return json.dumps(response(request))
    monkeypatch.setattr(analyzer,'_call_llm',model)
    with model_budget(1e12):
        result=analyzer.analyze_filing_evidence('material_event',current,issuer_binding=binding(current))
    assert calls[0][2:]==(4096,'thesis',1e12)
    assert current['units'][0]['text'] in calls[0][1]
    assert calls[0][0].startswith('CUSTOM OVERRIDE')
    assert calls[0][0].index('Mandatory filing-assessment-v1')>calls[0][0].index('CUSTOM OVERRIDE')
    assert result['document_assessment']=='assessed_no_directional_thesis'
    assert analyzer.config==before and analyzer._thesis_model=='gpt-6-astra'


def test_invalid_input_or_oversize_never_reaches_model(monkeypatch):
    analyzer=LLMAnalyzer();current=synthetic();calls=[]
    monkeypatch.setattr(analyzer,'_call_llm',lambda *a,**k:calls.append(True))
    with pytest.raises(ValueError,match='^invalid_filing_comparator$'):
        analyzer.analyze_filing_evidence('filing_change',current,issuer_binding=binding(current))
    assert calls==[]


def test_analyzer_rejects_non_strict_native_response_without_repair(monkeypatch):
    analyzer=LLMAnalyzer();current=evidence()
    monkeypatch.setattr(analyzer,'_call_llm',lambda *a,**k:'```json\n{"direction":"neutral"}\n```')
    with pytest.raises(ValueError,match='^invalid_filing_response$'):
        analyzer.analyze_filing_evidence('material_event',current,issuer_binding=binding(current))


def test_expired_or_late_aggregate_budget_returns_no_favorable_assessment(monkeypatch):
    analyzer=LLMAnalyzer();current=evidence();clock=[10.0];calls=[]
    monkeypatch.setattr('tradingagents.strategies.runtime_deadline.time.monotonic',lambda:clock[0])
    req=prepared(current)
    def model(*args,**kwargs):
        calls.append(True);clock[0]=21
        return json.dumps(response(req))
    monkeypatch.setattr(analyzer,'_call_llm',model)
    with model_budget(9):
        with pytest.raises(ModelDeadlineExceeded):
            analyzer.analyze_filing_evidence('material_event',current,issuer_binding=binding(current))
    assert calls==[]
    with model_budget(20):
        with pytest.raises(ModelDeadlineExceeded):
            analyzer.analyze_filing_evidence('material_event',current,issuer_binding=binding(current))
    assert calls==[True]


def test_existing_native_client_settings_and_model_cap_are_retained(monkeypatch):
    from openai import OpenAI
    current=evidence();req=prepared(current,target_binding=target())
    analyzer=LLMAnalyzer({'autoresearch':{'autoresearch_model':'gpt-6-luna','thesis_model':'gpt-6-astra','thesis_effort':'high'}})
    client=OpenAI(api_key='offline-test-only',base_url='https://offline.invalid/v1',timeout=37,max_retries=0)
    analyzer._client=client;calls=[]
    def send(payload,timeout):
        calls.append((payload,timeout))
        return {'text':json.dumps(response(req,direction='long')),'provenance':{'configured_model':'gpt-6-astra','returned_model':'gpt-6-astra'}}
    monkeypatch.setattr('tradingagents.strategies.llm_utils.bounded_transport',send)
    try:
        with model_budget(1e12):
            result=analyzer.analyze_filing_evidence('material_event',current,issuer_binding=binding(current),target_binding=target())
        assert result['direction']=='long'
        assert calls[0][0]['request']['model']=='gpt-6-astra'
        assert calls[0][0]['request']['max_tokens']==4096
        assert calls[0][1]<=120
        assert str(client.base_url)=='https://offline.invalid/v1/' and client.max_retries==0
        assert analyzer._client is client and analyzer.last_call_provenance['role']=='thesis'
    finally:client.close()


def test_invalid_input_does_not_reuse_previous_call_diagnostics(monkeypatch):
    analyzer=LLMAnalyzer();analyzer.last_call_provenance={'response_id':'previous-candidate'}
    analyzer.last_call_failure='model_deadline_exhausted';current=synthetic()
    monkeypatch.setattr(analyzer,'_call_llm',lambda *a,**k:pytest.fail('invalid input called model'))
    with pytest.raises(ValueError,match='^invalid_filing_comparator$'):
        analyzer.analyze_filing_evidence('filing_change',current,issuer_binding=binding(current))
    assert analyzer.last_call_provenance=={}
    assert analyzer.last_call_failure=='invalid_filing_comparator'


def test_early_deadline_sets_failure_diagnostics(monkeypatch):
    analyzer=LLMAnalyzer();current=evidence()
    monkeypatch.setattr('tradingagents.strategies.runtime_deadline.time.monotonic',lambda:10)
    with model_budget(9):
        with pytest.raises(ModelDeadlineExceeded):
            analyzer.analyze_filing_evidence('material_event',current,issuer_binding=binding(current))
    assert analyzer.last_call_failure=='model_deadline_exhausted'
