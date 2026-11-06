"""Prospective timing reaches each image stage without extending Sol calls."""
import pytest

from test_regulation_graphics_runtime import harness, memo_client, candidate_run
from tradingagents.strategies.candidate_response_reuse import candidate_response_memo


def enable(harness):
    harness.analyzer.config['autoresearch'].update(
        model_runtime_policy='full_regulation_graphics_v1',
        regulation_retrieval_policy='verified_government_copy_v3')


def capture_caps(harness, monkeypatch):
    original=harness.analyzer._call_llm
    caps=[]
    def call(*args, **kwargs):
        caps.append((kwargs['role'],kwargs.pop('max_call_seconds',None)))
        return original(*args,**kwargs)
    monkeypatch.setattr(harness.analyzer,'_call_llm',call)
    return caps


def test_prospective_cap_reaches_every_recognition_and_review_but_not_sol(harness,monkeypatch):
    enable(harness);caps=capture_caps(harness,monkeypatch)
    assert harness.analyzer.analyze_regulation_evidence(harness.evidence)=={'accepted':True}
    assert caps==[('bounded',240)]*4+[('thesis',None)]


def test_legacy_graphics_keep_existing_call_interface(harness,monkeypatch):
    caps=capture_caps(harness,monkeypatch)
    assert harness.analyzer.analyze_regulation_evidence(harness.evidence)=={'accepted':True}
    assert caps==[('bounded',None)]*4+[('thesis',None)]


def test_invalid_timing_policy_starts_no_graphics_or_sol_call(harness,monkeypatch):
    enable(harness);harness.analyzer.config['autoresearch']['model_runtime_policy']='unknown'
    caps=capture_caps(harness,monkeypatch)
    with pytest.raises(ValueError,match='invalid_model_runtime_policy'):
        harness.analyzer.analyze_regulation_evidence(harness.evidence)
    assert caps==[]


def test_runtime_policy_change_cannot_reuse_earlier_graphics(harness,monkeypatch):
    memo_client(harness,monkeypatch);caps=capture_caps(harness,monkeypatch)
    with candidate_response_memo():
        candidate_run(harness)
        before=len(caps)
        enable(harness)
        candidate_run(harness)
    assert caps[before:]==[('bounded',240)]*4+[('thesis',None)]
