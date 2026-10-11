"""Concurrent memo operations cannot cross private settings or validation scopes."""
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from threading import Barrier, Event
import time

from openai import OpenAI

from tradingagents.strategies.candidate_parallel import analyze_candidates
from tradingagents.strategies.candidate_response_reuse import (
    _MEMO, _CANDIDATE, begin_candidate_response, candidate_response_memo,
    commit_candidate_response, end_candidate_response, retain_candidate_response,
    reused_candidate_response,
)
from tradingagents.strategies.learning.llm_analyzer import LLMAnalyzer
from tradingagents.strategies.runtime_deadline import model_budget


def lookup(client):
    return reused_candidate_response(client, model="gpt-6-luna", system="system", prompt="evidence",
        max_tokens=4096, temperature=0, effort="high", role="thesis")


def test_concurrent_namespace_assignment_cannot_cross_private_client_settings():
    # Force a legitimate scheduling point between append and namespace return.
    # Without atomic lookup/assignment both requests can receive namespace 1.
    first_appended, second_appended = Event(), Event()
    class ScheduledSettings(list):
        def append(self, item):
            super().append(item)
            if len(self) == 1:
                first_appended.set()
                second_appended.wait(.25)
            else:
                second_appended.set()
    clients = [OpenAI(api_key=key, base_url="https://offline.invalid/v1") for key in ("private-a", "private-b")]
    def publish(index):
        token = begin_candidate_response("filing", "filing_change", "same-discovery", False)
        try:
            assert lookup(clients[index]) is None
            retain_candidate_response(f"validated-{index}", {"response_id": f"id-{index}"})
            commit_candidate_response()
        finally:
            end_candidate_response(token)
    try:
        with model_budget(time.monotonic() + 30), candidate_response_memo():
            _MEMO.get().client_settings = ScheduledSettings()
            with ThreadPoolExecutor(max_workers=2) as executor:
                first = executor.submit(copy_context().run, publish, 0)
                assert first_appended.wait(5)
                second = executor.submit(copy_context().run, publish, 1)
                first.result(5)
                second.result(5)
            for i, client in enumerate(clients):
                token = begin_candidate_response("filing", "filing_change", "same-discovery", False)
                try:
                    accepted = lookup(client)
                    assert accepted is not None
                    assert accepted[0] == f"validated-{i}"
                    assert accepted[1]["response_id"] == f"id-{i}"
                finally:
                    end_candidate_response(token)
    finally:
        for client in clients:
            client.close()


def test_parallel_transactions_share_only_fully_validated_memo_entries(monkeypatch):
    parent = LLMAnalyzer({"autoresearch": {"autoresearch_model": "gpt-6-luna", "thesis_model": "gpt-6-luna", "llm_effort": "high"}})
    client = OpenAI(api_key="private", base_url="https://offline.invalid/v1")
    parent._client = client
    barrier = Barrier(2)
    calls = []
    def transport(payload, timeout):
        calls.append(payload["request"]["prompt"])
        barrier.wait(5)
        return {"text": "validated JSON", "provenance": {"response_id": "same-response"}}
    monkeypatch.setattr("tradingagents.strategies.llm_utils.bounded_transport", transport)
    def analyze(candidate, analyzer):
        token = begin_candidate_response("filing", "filing_change", "same-discovery", False)
        try:
            assert _CANDIDATE.get().pending is None
            candidate["text"] = analyzer._call_llm("system", "evidence")
            if candidate["valid"]:
                commit_candidate_response()  # stand-in for completed downstream schema/entity validation
            candidate["provenance"] = dict(analyzer.last_call_provenance)
        finally:
            end_candidate_response(token)
    try:
        with model_budget(time.monotonic() + 30), candidate_response_memo():
            outcomes = analyze_candidates([{"valid": True}, {"valid": False}], analyzer=parent,
                                          analyze_one=analyze, max_workers=2)
            assert len(calls) == 2  # simultaneous unvalidated misses cannot authorize reuse
            assert [row.failure for row in outcomes] == ["", ""]
            assert _CANDIDATE.get() is None
            later = {"valid": True}
            analyze(later, parent)
            assert len(calls) == 2
            assert later["provenance"]["request_reuse"]["reused"] is True
            later["provenance"]["response_id"] = "mutated"
            token = begin_candidate_response("filing", "filing_change", "same-discovery", False)
            try:
                assert lookup(client)[1]["response_id"] == "same-response"
            finally:
                end_candidate_response(token)
    finally:
        client.close()
