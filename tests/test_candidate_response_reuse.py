"""Exact, validated analysis reuse must retain governed candidate boundaries."""
from copy import deepcopy
from dataclasses import replace
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from openai import OpenAI

from tradingagents.strategies.modules.base import Candidate
from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine
from tradingagents.strategies.data_sources.registry import DataSourceRegistry
from tradingagents.strategies.runtime_deadline import model_budget


@pytest.fixture
def transport(monkeypatch):
    calls = []
    outcomes = []

    def send(payload, timeout):
        calls.append(deepcopy(payload))
        value = outcomes.pop(0) if outcomes else {"direction": "long", "conviction": .8, "rationale": "Retained filing evidence"}
        if isinstance(value, Exception):
            raise value
        return {"text": json.dumps(value) if not isinstance(value, str) else value,
                "provenance": {"configured_model": payload["request"]["model"],
                               "returned_model": payload["request"]["model"], "returned_revision": None,
                               "identity_status": "unpinned", "response_id": f"resp-{len(calls)}",
                               "reasoning_effort": payload["request"]["effort"]}}

    monkeypatch.setattr("tradingagents.strategies.llm_utils.bounded_transport", send)
    return calls, outcomes


def engine(tmp_path, **settings):
    cfg = {"autoresearch": {"state_dir": str(tmp_path), "autoresearch_model": "gpt-6-luna",
                            "thesis_model": "gpt-6-astra", "llm_effort": "high", "thesis_effort": "high"}}
    result = MultiStrategyEngine(config=cfg, registry=DataSourceRegistry(), use_llm=True)
    result._analyzer._client = OpenAI(api_key="offline-private", base_url="https://offline.invalid/v1", **settings)
    return result


def filing(**metadata):
    return Candidate(ticker="AAPL", date="2026-03-30", metadata={
        "needs_llm_analysis": True, "analysis_type": "filing_change", "current_text": "Retained filing",
        "discovery_id": "discovery_fixture", **metadata})


def enrich(worker, candidate=None):
    return worker._enrich_with_llm([candidate or filing()], "filing_analysis")[0]


def test_daily_four_horizons_validate_each_filing_with_one_physical_request(tmp_path, transport, monkeypatch):
    from test_30day_simulation import _authoritative_orchestrator, _cohort_config, _authoritative_committee
    from tradingagents.strategies.modules.filing_analysis import FilingAnalysisStrategy
    import tradingagents.strategies.learning.llm_analyzer as analyzer_module

    configs = [replace(_cohort_config(tmp_path, horizon), horizon=horizon, use_llm=True)
               for horizon in ("30d", "3m", "6m", "1y")]
    orch, _ = _authoritative_orchestrator(tmp_path, cohort_configs=configs,
                                         strategy_modules=[FilingAnalysisStrategy()], model="gpt-6-astra")
    inputs = {"yfinance": {}, "edgar": {"filings": [{"ticker": "AAPL", "form_type": "10-K",
              "file_date": "2026-03-30", "accession_number": "0000320193-26-000001",
              "current_text": "Retained filing", "file_url": "https://www.sec.gov/Archives/edgar/data/320193/000032019326000001/report.htm"}]}}
    validated = []
    parse = analyzer_module._parse_json_response
    monkeypatch.setattr(analyzer_module, "_parse_json_response", lambda text: (validated.append(text), parse(text))[1])
    for cohort in orch.cohorts:
        cohort["engine"]._fetch_all_data = lambda start, end: deepcopy(inputs)
        cohort["engine"]._analyzer._client = OpenAI(api_key="offline-private", base_url="https://offline.invalid/v1")
    try:
        with patch("tradingagents.strategies.trading.portfolio_committee.PortfolioCommittee.synthesize", side_effect=_authoritative_committee):
            results = orch.run_daily("2026-03-30")
        assert all(row["execution_valid"] for row in results.values()), results
        assert len(validated) == 4
        assert len(transport[0]) == 1
        records = [row for cohort in orch.cohorts for row in cohort["ledger"].read_signals()
                   if row.strategy == "filing_analysis"]
        assert len(records) == 4
        assert len({row.event_key for row in records}) == 1
        assert len({row.evidence_hash for row in records}) == 1
        from tradingagents.strategies.candidate_response_reuse import _MEMO, _CANDIDATE
        assert _MEMO.get() is None and _CANDIDATE.get() is None
    finally:
        for cohort in orch.cohorts:
            cohort["engine"]._analyzer._client.close()
            cohort["ledger"].close()


def test_valid_response_reuse_copies_original_provenance_between_engines(tmp_path, transport):
    from tradingagents.strategies.candidate_response_reuse import candidate_response_memo
    first, second = engine(tmp_path / "first"), engine(tmp_path / "second")
    try:
        with candidate_response_memo():
            original = enrich(first)
            reused = enrich(second)
            original.metadata["model_provenance"]["response_id"] = "mutated"
            original.metadata["model_provenance"]["request_reuse"]["request_digest"] = "mutated"
            reused.metadata["llm_analysis"]["rationale"] = "mutated"
            first._analyzer.last_call_failure = "model_deadline_exhausted"
            again = enrich(first)
        assert len(transport[0]) == 1
        assert reused.metadata["model_provenance"]["response_id"] == "resp-1"
        assert again.metadata["model_provenance"]["response_id"] == "resp-1"
        assert again.metadata["model_provenance"]["identity_status"] == "unpinned"
        assert again.metadata["model_provenance"]["returned_revision"] is None
        assert again.metadata["model_provenance"]["request_reuse"]["reused"] is True
        assert original.metadata["model_provenance"]["request_reuse"]["reused"] is False
        assert again.metadata["llm_analysis"]["rationale"] == "Retained filing evidence"
        assert first._analyzer.last_call_failure == ""
        assert again.metadata["model_provenance"]["request_reuse"]["request_digest"] != "mutated"
        assert "offline-private" not in json.dumps(again.metadata)
    finally:
        first._analyzer._client.close()
        second._analyzer._client.close()


@pytest.mark.parametrize("change", ["prompt", "override", "effort", "model", "temperature", "endpoint", "header", "credential", "organization", "project", "discovery", "validation_mode", "role", "max_tokens"])
def test_request_or_validation_changes_do_not_reuse(tmp_path, transport, change, monkeypatch):
    from tradingagents.strategies.candidate_response_reuse import candidate_response_memo
    worker = engine(tmp_path)
    old_client = worker._analyzer._client
    candidate = filing()
    try:
        with candidate_response_memo():
            assert enrich(worker).metadata["analysis_status"] == "validated"
            if change == "prompt":
                candidate.metadata["current_text"] += " changed"
            elif change == "override":
                worker._analyzer.set_prompt_override("filing_change", "Override system")
            elif change == "effort":
                worker._analyzer._thesis_effort = "medium"
            elif change == "model":
                worker._analyzer._thesis_model = "gpt-6.1-sol"
            elif change == "temperature":
                worker._analyzer._temperature = .5
            elif change in {"endpoint", "header", "credential", "organization", "project"}:
                settings = {"api_key": "offline-private", "base_url": "https://offline.invalid/v1"}
                settings.update({"endpoint": {"base_url": "https://different.invalid/v1"},
                                 "header": {"default_headers": {"X-Custom": "changed"}},
                                 "credential": {"api_key": "different-private"},
                                 "organization": {"organization": "different"},
                                 "project": {"project": "different"}}[change])
                worker._analyzer._client = OpenAI(**settings)
            elif change == "discovery":
                candidate.metadata["discovery_id"] = "different_discovery"
            elif change == "validation_mode":
                candidate.metadata.update(analysis_type="commodity_macro", deterministic_evidence_complete=True)
                monkeypatch.setattr(worker._analyzer, "analyze_commodity_macro", lambda **kwargs: json.loads(worker._analyzer._call_llm(
                    transport[0][0]["request"]["system"], transport[0][0]["request"]["prompt"])))
            else:
                request = deepcopy(transport[0][0]["request"])
                monkeypatch.setattr(worker._analyzer, "analyze_filing_change", lambda *args, **kwargs: json.loads(worker._analyzer._call_llm(
                    request["system"], request["prompt"], max_tokens=1024 if change == "max_tokens" else request["max_tokens"],
                    role="bounded" if change == "role" else "thesis")))
            assert enrich(worker, candidate).metadata["analysis_status"] == "validated"
        assert len(transport[0]) == 2
    finally:
        old_client.close()
        worker._analyzer._client.close()


def test_weather_holding_period_changes_each_request(tmp_path, transport):
    from tradingagents.strategies.candidate_response_reuse import candidate_response_memo
    worker = engine(tmp_path)
    try:
        with candidate_response_memo():
            for hold in (25, 90, 180, 300):
                candidate = filing(analysis_type="ag_weather", hold_days=hold)
                assert enrich(worker, candidate).metadata["analysis_status"] == "validated"
        assert len(transport[0]) == 4
        assert len({row["request"]["prompt"] for row in transport[0]}) == 4
    finally:
        worker._analyzer._client.close()


@pytest.mark.parametrize("failure", ["bad-json", {}, {"direction": "wrong", "conviction": .8}, {"direction": "long", "conviction": 9}, RuntimeError("safe fixture"), ""])
def test_failed_or_invalid_responses_are_not_memoized(tmp_path, transport, failure):
    from tradingagents.strategies.candidate_response_reuse import candidate_response_memo
    worker = engine(tmp_path)
    transport[1].append(failure)
    try:
        with candidate_response_memo():
            assert enrich(worker).metadata["analysis_status"] == "failed"
            assert enrich(worker).metadata["analysis_status"] == "validated"
        assert len(transport[0]) == 2
    finally:
        worker._analyzer._client.close()


def test_scope_clears_on_next_session_and_exception(tmp_path, transport):
    from tradingagents.strategies.candidate_response_reuse import candidate_response_memo
    worker = engine(tmp_path)
    try:
        with pytest.raises(RuntimeError, match="interrupt"):
            with candidate_response_memo():
                enrich(worker)
                raise RuntimeError("interrupt")
        enrich(worker)
        with candidate_response_memo():
            enrich(worker)
            enrich(worker)
        assert len(transport[0]) == 3
    finally:
        worker._analyzer._client.close()


def test_cached_hit_after_deadline_still_holds_entire_candidate_sample(tmp_path, transport, monkeypatch):
    from tradingagents.strategies.candidate_response_reuse import candidate_response_memo
    now = [100.]
    monkeypatch.setattr("tradingagents.strategies.runtime_deadline.time.monotonic", lambda: now[0])
    worker = engine(tmp_path)
    try:
        with candidate_response_memo(), model_budget(110.):
            enrich(worker)
            now[0] = 111.
            candidates = worker._enrich_with_llm([filing(), filing(discovery_id="second")], "filing_analysis")
        assert len(transport[0]) == 1
        assert all(c.journal_only and c.metadata["analysis_failure_reason"] == "model_deadline_exhausted" for c in candidates)
    finally:
        worker._analyzer._client.close()


def test_committee_and_standalone_requests_are_not_reused(tmp_path, transport):
    from tradingagents.strategies.candidate_response_reuse import candidate_response_memo
    from tradingagents.strategies.trading.portfolio_committee import PortfolioCommittee
    worker = engine(tmp_path)
    committee = PortfolioCommittee({"autoresearch": {"autoresearch_model": "gpt-6-astra"}})
    committee._client = worker._analyzer._client
    try:
        with candidate_response_memo():
            enrich(worker)
            for _ in range(2):
                committee._call_llm(system="sys", prompt="committee")
                worker._analyzer.analyze_filing_change("Retained filing", "", "AAPL")
        assert len(transport[0]) == 5
    finally:
        worker._analyzer._client.close()


def test_entity_failure_is_not_retained_and_hits_repeat_entity_validation(tmp_path, transport):
    from tradingagents.strategies.candidate_response_reuse import candidate_response_memo
    worker = engine(tmp_path)
    allowed, validations = [False], []
    worker.registry.register(SimpleNamespace(name="edgar", validate_ticker=lambda ticker: (validations.append(ticker), allowed[0])[1]))
    response = {"direction": "long", "conviction": .8, "rationale": "Retained filing", "affected_tickers": ["AAPL"]}
    transport[1].extend([response, response])
    def unresolved():
        candidate = filing()
        candidate.ticker = ""
        return candidate
    try:
        with candidate_response_memo():
            assert enrich(worker, unresolved()).metadata["analysis_failure_reason"] == "unresolved_issuer"
            allowed[0] = True
            assert enrich(worker, unresolved()).metadata["analysis_status"] == "validated"
            allowed[0] = False
            assert enrich(worker, unresolved()).metadata["analysis_failure_reason"] == "unresolved_issuer"
        assert len(transport[0]) == 2
        assert validations == ["AAPL"] * 3
    finally:
        worker._analyzer._client.close()


def test_deadline_expiring_during_hit_cannot_restore_actionability(tmp_path, transport, monkeypatch):
    import tradingagents.strategies.candidate_response_reuse as reuse
    now = [100.]
    monkeypatch.setattr("tradingagents.strategies.runtime_deadline.time.monotonic", lambda: now[0])
    worker = engine(tmp_path)
    original_lookup, hits = reuse.reused_candidate_response, []
    def lookup(*args, **kwargs):
        result = original_lookup(*args, **kwargs)
        if result is not None:
            hits.append(result)
            now[0] = 111.
        return result
    try:
        with reuse.candidate_response_memo(), model_budget(110.):
            enrich(worker)
            monkeypatch.setattr(reuse, "reused_candidate_response", lookup)
            failed = enrich(worker)
        assert len(hits) == 1 and len(transport[0]) == 1
        assert failed.journal_only
        assert failed.metadata["analysis_failure_reason"] == "model_deadline_exhausted"
        assert worker._analyzer.last_call_failure == "model_deadline_exhausted"
    finally:
        worker._analyzer._client.close()


def test_deadline_expiring_in_cached_candidate_entity_validation_holds_sample(tmp_path, transport, monkeypatch):
    from tradingagents.strategies.candidate_response_reuse import candidate_response_memo
    now = [100.]
    monkeypatch.setattr("tradingagents.strategies.runtime_deadline.time.monotonic", lambda: now[0])
    worker = engine(tmp_path)
    slow = [False]
    def validate(ticker):
        if slow[0]:
            now[0] = 111.
        return True
    worker.registry.register(SimpleNamespace(name="edgar", validate_ticker=validate))
    transport[1].append({"direction": "long", "conviction": .8, "rationale": "Retained filing", "affected_tickers": ["AAPL"]})
    def unresolved():
        candidate = filing()
        candidate.ticker = ""
        return candidate
    try:
        with candidate_response_memo(), model_budget(110.):
            assert enrich(worker, unresolved()).metadata["analysis_status"] == "validated"
            slow[0] = True
            result = worker._enrich_with_llm([unresolved()], "filing_analysis")
        assert len(transport[0]) == 1
        assert all(row.journal_only and row.metadata["analysis_status"] == "failed" for row in result)
        assert all(row.metadata["analysis_failure_reason"] == "model_deadline_exhausted" for row in result)
        assert all(row.ticker == "" for row in result)
    finally:
        worker._analyzer._client.close()


def test_digest_collision_cannot_authorize_different_requests(tmp_path, transport, monkeypatch):
    import tradingagents.strategies.candidate_response_reuse as reuse
    monkeypatch.setattr(reuse, "_request_digest", lambda key: "forced-collision")
    worker = engine(tmp_path)
    try:
        with reuse.candidate_response_memo():
            enrich(worker)
            enrich(worker, filing(current_text="Different retained filing"))
        assert len(transport[0]) == 2
    finally:
        worker._analyzer._client.close()


def test_transport_credentials_are_not_serialized_into_reuse_keys(tmp_path, transport, monkeypatch, caplog):
    import tradingagents.strategies.candidate_response_reuse as reuse
    worker = engine(tmp_path, default_headers={"X-Private": "header-private"})
    original_dumps = reuse.json.dumps
    serialized = []
    def encode(value, *args, **kwargs):
        output = original_dumps(value, *args, **kwargs)
        serialized.append(output)
        return output
    monkeypatch.setattr(reuse.json, "dumps", encode)
    try:
        with reuse.candidate_response_memo():
            enrich(worker)
            result = enrich(worker)
        assert len(transport[0]) == 1
        assert not any(secret in text for secret in ("offline-private", "header-private", "offline.invalid")
                       for text in serialized + [caplog.text, original_dumps(result.metadata)])
    finally:
        worker._analyzer._client.close()


def test_anthropic_auth_configuration_is_part_of_exact_client_namespace(tmp_path, transport):
    from anthropic import Anthropic
    from tradingagents.strategies.candidate_response_reuse import candidate_response_memo
    worker = engine(tmp_path)
    worker._analyzer._client.close()
    worker._analyzer._model_name = worker._analyzer._thesis_model = "claude-sonnet-5"
    clients = [Anthropic(api_key="offline-private", auth_token=token, base_url="https://offline.invalid")
               for token in ("token-one", "token-one", "token-two")]
    try:
        with candidate_response_memo():
            for client in clients:
                worker._analyzer._client = client
                assert enrich(worker).metadata["analysis_status"] == "validated"
        assert len(transport[0]) == 2
    finally:
        for client in clients:
            client.close()


@pytest.mark.parametrize("kind", ["missing_discovery", "unsupported_analysis", "injected_client"])
def test_unsupported_reuse_context_is_not_retained(tmp_path, transport, kind):
    from tradingagents.strategies.candidate_response_reuse import candidate_response_memo
    worker = engine(tmp_path)
    native = worker._analyzer._client
    candidate = filing()
    calls = []
    if kind == "missing_discovery":
        candidate.metadata.pop("discovery_id")
    elif kind == "unsupported_analysis":
        candidate.metadata["analysis_type"] = "unsupported"
    else:
        def create(**kwargs):
            calls.append(kwargs)
            return SimpleNamespace(status="completed", output=[], output_text=json.dumps({"direction": "long", "conviction": .8, "rationale": "Fixture"}))
        worker._analyzer._client = SimpleNamespace(responses=SimpleNamespace(create=create))
    try:
        with candidate_response_memo():
            results = [enrich(worker, deepcopy(candidate)) for _ in range(2)]
        if kind == "unsupported_analysis":
            assert all(row.metadata["analysis_status"] == "failed" for row in results)
            assert not transport[0] and not calls
        else:
            assert all(row.metadata["analysis_status"] == "validated" for row in results)
            assert len(transport[0]) + len(calls) == 2
    finally:
        native.close()
