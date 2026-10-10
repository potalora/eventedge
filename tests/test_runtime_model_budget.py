"""Native transports and aggregate analysis must fit one wall-clock budget."""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from tradingagents.strategies.llm_utils import call_analysis_model


@pytest.fixture
def stalled_server():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.server.started.set()
            self.server.release.wait(timeout=15)

        def do_POST(self):
            self.do_GET()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    server.started = threading.Event()
    server.release = threading.Event()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_fred_native_transport_does_not_hold_subprocess_after_gather(stalled_server):
    script = '''
import time
from tradingagents.strategies.data_sources.fred_source import FREDSource
from tradingagents.strategies.data_sources.request_policy import provider_budget
from tradingagents.strategies.orchestration.multi_strategy_engine import _gather_with_timeout
source=FREDSource(api_key="offline")
source._base_url=%r
start=time.monotonic()
def fetch():
 with provider_budget("fred",time.monotonic()+1,max_attempts=1):
  return source.fetch_series("UNRATE","2026-10-01","2026-10-06")
result=_gather_with_timeout({"fred":(fetch,())},1.2)
assert "error" in result["fred"]
print(time.monotonic()-start)
''' % (f"http://127.0.0.1:{stalled_server.server_port}")
    started = time.monotonic()
    completed = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=5)
    assert completed.returncode == 0, completed.stderr
    assert stalled_server.started.is_set(), "must exercise the real FRED HTTP boundary"
    assert time.monotonic() - started < 2.5


def test_real_responses_transport_is_killed_at_total_deadline(stalled_server):
    from openai import OpenAI
    from tradingagents.strategies.runtime_deadline import ModelDeadlineExceeded, model_budget

    with OpenAI(api_key="offline", base_url=f"http://127.0.0.1:{stalled_server.server_port}/v1", max_retries=4) as client:
        started = time.monotonic()
        # Include cold subprocess imports on CI, then keep the actual HTTP
        # transport stalled past the deadline. No provider timeout can finish it.
        with model_budget(started + 5), pytest.raises(ModelDeadlineExceeded):
            call_analysis_model(client, model="gpt-6-luna", system="sys", prompt="hi", max_tokens=30, temperature=0, effort="high")
        assert stalled_server.started.is_set(), "must exercise the real Responses HTTP boundary"
        assert time.monotonic() - started < 6


def test_model_provenance_marks_returned_alias_without_revision_unpinned():
    response = SimpleNamespace(status="completed", output=[], output_text='{}', model="gpt-6-luna", id="response-1")
    client = SimpleNamespace(responses=SimpleNamespace(create=lambda **kwargs: response))
    provenance = {}
    assert call_analysis_model(client, model="gpt-6-luna", system="s", prompt="p", max_tokens=10, temperature=0, effort="high", provenance=provenance) == '{}'
    assert provenance == {"configured_model": "gpt-6-luna", "returned_model": "gpt-6-luna", "returned_revision": None, "identity_status": "unpinned", "response_id": "response-1", "reasoning_effort": "high"}


def test_expired_budget_skips_every_remaining_required_candidate_and_optional_fallback(tmp_path):
    from tradingagents.strategies.data_sources.registry import DataSourceRegistry
    from tradingagents.strategies.modules.base import Candidate
    from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine
    from tradingagents.strategies.runtime_deadline import model_budget

    engine = MultiStrategyEngine(config={"autoresearch": {"state_dir": str(tmp_path)}}, registry=DataSourceRegistry())
    def unexpected(*args, **kwargs):
        pytest.fail("No model call may start after aggregate budget exhaustion")
    engine._analyzer = SimpleNamespace(analyze_supply_chain=unexpected, analyze_commodity_macro=unexpected)
    candidates = [Candidate(ticker="DAL", date="2026-10-09", direction="long", score=.7, metadata={"needs_llm_analysis": True, "analysis_type": kind, "headline": "Factory closes", "deterministic_evidence_complete": True}) for kind in ("supply_chain", "commodity_macro")]
    with model_budget(time.monotonic() - 1):
        enriched = engine._enrich_with_llm(candidates, "supply_chain")
    assert len(enriched) == 2
    assert all(c.journal_only for c in enriched)
    assert all(c.metadata["analysis_failure_reason"] == "model_deadline_exhausted" for c in enriched)


def test_committee_deadline_holds_cash_without_rule_selection(monkeypatch):
    from tradingagents.strategies.runtime_deadline import model_budget
    from tradingagents.strategies.trading.portfolio_committee import PortfolioCommittee
    committee = PortfolioCommittee({"autoresearch": {"autoresearch_model": "gpt-6-luna"}})
    monkeypatch.setattr(committee, "_get_client", lambda: SimpleNamespace())
    with model_budget(time.monotonic() - 1):
        recs = committee.synthesize([{"ticker": "DAL", "direction": "long", "score": 2.1, "strategy": "supply_chain"}])
    assert recs == []
    assert committee.last_decision_status["degraded"] is True
    assert committee.last_decision_status["reason"] == "model_deadline_exhausted"


def test_analyzer_selects_role_without_mutating_configured_models(monkeypatch):
    from tradingagents.strategies.learning.llm_analyzer import LLMAnalyzer
    analyzer = LLMAnalyzer({"autoresearch": {"autoresearch_model": "gpt-6-luna", "llm_effort": "high", "thesis_model": "gpt-6-astra", "thesis_effort": "high"}})
    seen = []
    monkeypatch.setattr(analyzer, "_get_client", lambda: SimpleNamespace())
    def call(client, **kwargs):
        seen.append((kwargs["model"], kwargs["effort"]))
        return '{"direction":"neutral","score":0.2}'
    monkeypatch.setattr("tradingagents.strategies.llm_utils.call_analysis_model", call)
    analyzer.analyze_filing_change("new", "prior", "DAL")
    analyzer.analyze_commodity_macro("GLD", "gold", {}, {})
    assert seen == [("gpt-6-astra", "high"), ("gpt-6-luna", "high")]
    assert analyzer._model_name == "gpt-6-luna"


def test_deadline_invalidates_completed_prefix_of_candidate_sample(tmp_path, monkeypatch):
    from tradingagents.strategies.data_sources.registry import DataSourceRegistry
    from tradingagents.strategies.modules.base import Candidate
    from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine
    from tradingagents.strategies.runtime_deadline import model_budget
    now = [100.0]
    monkeypatch.setattr("tradingagents.strategies.runtime_deadline.time.monotonic", lambda: now[0])
    engine = MultiStrategyEngine(config={"autoresearch": {"state_dir": str(tmp_path)}}, registry=DataSourceRegistry())
    calls = []
    def analyze(*args, **kwargs):
        calls.append(args)
        now[0] += 2
        return {"direction": "long", "conviction": .8, "rationale": "Retained disruption"}
    engine._analyzer = SimpleNamespace(analyze_supply_chain=analyze)
    candidates = [Candidate(ticker=ticker, date="2026-10-09", direction="long", score=.5,
                  metadata={"needs_llm_analysis": True, "analysis_type": "supply_chain", "headline": "Plant closes"}) for ticker in ("DAL", "UAL")]
    with model_budget(103):
        result = engine._enrich_with_llm(candidates, "supply_chain")
    assert len(calls) == 2
    assert all(c.journal_only for c in result)
    assert all(c.metadata["analysis_failure_reason"] == "model_deadline_exhausted" for c in result)


def test_rate_limit_backoff_cannot_exceed_shared_deadline(monkeypatch):
    from tradingagents.strategies.runtime_deadline import ModelDeadlineExceeded, ModelTransportError, model_budget
    from tradingagents.strategies.trading.portfolio_committee import PortfolioCommittee
    committee = PortfolioCommittee({})
    monkeypatch.setattr(committee, "_get_client", lambda: SimpleNamespace())
    def reject(*args, **kwargs):
        raise ModelTransportError("provider_error", 429)
    monkeypatch.setattr("tradingagents.strategies.llm_utils.call_analysis_model", reject)
    started = time.monotonic()
    with model_budget(started + .2), pytest.raises(ModelDeadlineExceeded):
        committee._call_llm(system="s", prompt="p")
    assert time.monotonic() - started < .15


def test_native_responses_success_preserves_public_identity_and_client_headers():
    from openai import OpenAI
    from tradingagents.strategies.runtime_deadline import model_budget
    observed = {}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            observed["headers"] = {key.lower(): value for key, value in self.headers.items()}
            observed["request"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            body = json.dumps({"id":"resp_local","object":"response","created_at":1,"status":"completed",
                               "model":"gpt-6-astra","model_revision":"provider-revision-7",
                               "output":[{"id":"msg_1","type":"message","status":"completed","role":"assistant","content":[{"type":"output_text","text":"{}","annotations":[]}]}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    server = ThreadingHTTPServer(("127.0.0.1",0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    provenance = {}
    try:
        with OpenAI(api_key="offline-private", base_url=f"http://127.0.0.1:{server.server_port}/v1", organization="org_fixture", project="proj_fixture", default_headers={"X-EventEdge-Test":"retained"}) as client:
            with model_budget(time.monotonic()+5):
                text = call_analysis_model(client, model="gpt-6-astra", system="sys", prompt="hi", max_tokens=10, temperature=0, effort="high", provenance=provenance)
        assert text == "{}"
        assert observed["headers"]["authorization"] == "Bearer offline-private"
        assert observed["headers"]["x-eventedge-test"] == "retained"
        assert observed["headers"]["openai-organization"] == "org_fixture"
        assert observed["headers"]["openai-project"] == "proj_fixture"
        assert observed["request"]["reasoning"] == {"effort":"high"}
        assert provenance["returned_revision"] == "provider-revision-7"
        assert provenance["identity_status"] == "pinned"
        assert "offline-private" not in json.dumps(provenance)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_fred_rest_contract_preserves_vintage_and_missing_marker(monkeypatch):
    from tradingagents.strategies.data_sources.fred_source import FREDSource
    observed = {}
    def transport(url, **options):
        observed.update(options)
        return SimpleNamespace(status_code=200, headers={}, json=lambda: {"observations":[
            {"date":"2026-10-01","value":"4.0"}, {"date":"2026-10-02","value":"."},
            {"date":"2026-10-05","value":"5.0"}]})
    monkeypatch.setattr(FREDSource, "_transport_get", staticmethod(transport))
    result = FREDSource(api_key="private-test-key").fetch_series("UNRATE", "2026-10-01", "2026-10-06", as_of="2026-10-07")
    assert result.iloc[0] == 4 and result.iloc[2] == 5
    assert result.isna().iloc[1]
    assert observed["params"]["realtime_start"] == observed["params"]["realtime_end"] == "2026-10-07"
    assert observed["params"]["file_type"] == "json"
    assert 0 < observed["timeout"] <= 15


def test_standalone_screen_installs_one_aggregate_budget_for_all_strategies(tmp_path, monkeypatch):
    from tradingagents.strategies.data_sources.registry import DataSourceRegistry
    from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine
    from tradingagents.strategies.runtime_deadline import current_model_deadline
    engine = MultiStrategyEngine(config={"autoresearch": {"state_dir": str(tmp_path)}}, registry=DataSourceRegistry())
    seen = []
    def screen(*args):
        seen.append(current_model_deadline())
        return []
    engine.paper_trade_strategies = [SimpleNamespace(name=name, data_sources=[], get_default_params=lambda **kwargs: {}, screen=screen) for name in ("supply_chain", "litigation")]
    monkeypatch.setattr(engine, "_build_regime_model", lambda data: {})
    engine.screen_and_enrich("2026-10-09", {}, epoch_id="fixture", policy_id="fixture")
    assert seen[0] is not None and seen[0] == seen[1]
    assert current_model_deadline() is None


def test_native_fred_trickle_body_is_killed_by_wall_clock_deadline():
    from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
    from tradingagents.strategies.data_sources.fred_source import FREDSource
    from tradingagents.strategies.data_sources.request_policy import provider_budget
    started = threading.Event()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "10000")
            self.end_headers()
            started.set()
            try:
                for _ in range(100):
                    self.wfile.write(b" ")
                    self.wfile.flush()
                    time.sleep(.03)  # Always faster than the socket inactivity timeout.
            except (BrokenPipeError, ConnectionResetError):
                pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    source = FREDSource(api_key="offline")
    source._base_url = f"http://127.0.0.1:{server.server_port}"
    began = time.monotonic()
    try:
        with provider_budget("fred", began + .7, max_attempts=1), pytest.raises(SourceFetchError) as exc:
            source.fetch_series("UNRATE", "2026-10-01", "2026-10-06")
        assert exc.value.reason_code == "timeout"
        assert started.is_set()
        assert time.monotonic() - began < 1.2
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_native_sdk_retries_are_disabled_even_when_parent_client_enables_them():
    from openai import OpenAI
    from tradingagents.strategies.runtime_deadline import ModelTransportError, model_budget
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            calls.append(1)
            self.rfile.read(int(self.headers["Content-Length"]))
            body = b'{"error":{"message":"private-provider-body","type":"rate_limit_error"}}'
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with OpenAI(api_key="offline", base_url=f"http://127.0.0.1:{server.server_port}/v1", max_retries=4) as client:
            with model_budget(time.monotonic()+5), pytest.raises(ModelTransportError) as exc:
                call_analysis_model(client, model="gpt-6-luna", system="s", prompt="p", max_tokens=10, temperature=0, effort="high")
        assert calls == [1]
        assert exc.value.status_code == 429
        assert "private-provider-body" not in str(exc.value)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_timeout_coverage_propagates_to_full_horizon_and_committee_status():
    from datetime import date
    from tradingagents.strategies.metrics.models import StrategyHealthRecord
    from tradingagents.strategies.orchestration.multi_strategy_engine import hold_incomplete_model_sample
    from tradingagents.strategies.trading.portfolio_committee import PortfolioCommittee
    signals = [{"ticker":"DAL", "metadata":{"analysis_status":"validated"}},
               {"ticker":"UAL", "metadata":{"analysis_status":"failed","analysis_failure_reason":"model_deadline_exhausted"}}]
    health = [StrategyHealthRecord("health_fixture", "epoch_fixture", date(2026,10,9), "policy_fixture", "supply_chain", "signals", 2, {"candidate_count":2})]
    held, degraded = hold_incomplete_model_sample(signals, health)
    assert len(held) == 2 and all(s["journal_only"] for s in held)
    assert degraded[0].status == "data_failure"
    assert degraded[0].evidence["model_coverage"] == {"complete":False,"reason":"model_deadline_exhausted"}
    committee = PortfolioCommittee({})
    assert committee.synthesize(held) == []
    assert committee.last_decision_status["status"] == "failed"
    assert committee.last_decision_status["reason"] == "model_deadline_exhausted"


def test_supported_sdk_subclasses_use_the_same_hard_transport_boundary(stalled_server):
    from openai import OpenAI
    from tradingagents.strategies.runtime_deadline import ModelDeadlineExceeded, model_budget
    class ProjectOpenAI(OpenAI):
        pass
    with ProjectOpenAI(api_key="offline", base_url=f"http://127.0.0.1:{stalled_server.server_port}/v1", timeout=.1, max_retries=0) as client:
        with model_budget(time.monotonic()+1.5), pytest.raises(ModelDeadlineExceeded):
            call_analysis_model(client, model="gpt-6-luna", system="s", prompt="p", max_tokens=10, temperature=0, effort="high")


def test_committee_failed_later_call_cannot_reuse_previous_response_identity(monkeypatch):
    from tradingagents.strategies.trading.portfolio_committee import PortfolioCommittee
    committee = PortfolioCommittee({})
    def first(*args, **kwargs):
        committee.last_call_provenance = {"configured_model":committee._model_name,"returned_model":"previous-model","returned_revision":None,"identity_status":"unpinned","response_id":"previous-response"}
        return []
    signal = {"ticker":"DAL", "direction":"long", "score":2.1,"strategy":"supply_chain"}
    monkeypatch.setattr(committee, "_llm_synthesize", first)
    committee.synthesize([signal])
    monkeypatch.setattr(committee, "_llm_synthesize", lambda *a, **kw: None)
    committee.synthesize([signal])
    assert committee.last_decision_status["model_provenance"]["returned_model"] is None
    assert committee.last_decision_status["model_provenance"]["response_id"] is None


def test_native_anthropic_transport_preserves_configured_client_and_provenance():
    from anthropic import Anthropic
    from tradingagents.strategies.runtime_deadline import model_budget
    observed = {}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            observed["headers"] = {key.lower(): value for key, value in self.headers.items()}
            observed["request"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            body = json.dumps({"id":"msg_local", "type":"message", "role":"assistant", "model":"claude-sonnet-5",
                "content":[{"type":"text","text":"{}"}], "stop_reason":"end_turn", "stop_sequence":None,
                "usage":{"input_tokens":1,"output_tokens":1}}).encode()
            self.send_response(200)
            self.send_header("Content-Type","application/json")
            self.send_header("Content-Length",str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    provenance = {}
    try:
        with Anthropic(api_key="offline-private", base_url=f"http://127.0.0.1:{server.server_port}", default_headers={"X-EventEdge-Test":"retained"}) as client:
            with model_budget(time.monotonic()+5):
                text = call_analysis_model(client, model="claude-sonnet-5", system="s", prompt="p", max_tokens=10, temperature=0, effort="high", provenance=provenance)
        assert text == "{}"
        assert observed["headers"]["x-api-key"] == "offline-private"
        assert observed["headers"]["x-eventedge-test"] == "retained"
        assert observed["request"]["output_config"] == {"effort":"high"}
        assert provenance["returned_model"] == "claude-sonnet-5"
        assert provenance["identity_status"] == "unpinned"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("filtered_kind", ["unresolved", "blocked"])
def test_filtered_timeout_horizon_holds_earlier_and_retained_late_samples(tmp_path, monkeypatch, filtered_kind):
    from datetime import date, datetime, timezone
    from tradingagents.strategies.data_sources.registry import DataSourceRegistry
    from tradingagents.strategies.metrics.models import StrategyHealthRecord
    from tradingagents.strategies.modules.base import Candidate
    from tradingagents.strategies.orchestration import daily_pipeline, source_inputs
    from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine
    from tradingagents.strategies.runtime_deadline import model_budget
    session = date(2026, 10, 9)
    config = {"autoresearch":{"state_dir":str(tmp_path), "blocked_tickers":["BLOCKED"]}}
    engine = MultiStrategyEngine(config=config, registry=DataSourceRegistry())
    candidate = Candidate(ticker="" if filtered_kind == "unresolved" else "BLOCKED", date=str(session), direction="long", score=.5,
                          metadata={"needs_llm_analysis":True,"analysis_type":"litigation","case_name":"Retained case"})
    engine.paper_trade_strategies = [SimpleNamespace(name="litigation",data_sources=[],get_default_params=lambda **kw:{},screen=lambda *a:[candidate])]
    monkeypatch.setattr(engine, "_build_regime_model", lambda _: {})
    with model_budget(time.monotonic()-1):
        failed = engine.screen_and_enrich(str(session), {}, epoch_id="epoch", policy_id="p30")
    assert failed[0] == []
    assert failed[2][0].evidence["model_coverage"]["complete"] is False
    earlier = {"ticker":"DAL","strategy":"fixture_strategy","journal_only":False,"metadata":{"event_key":"earlier","analysis_status":"validated"}}
    retained = {"ticker":"UAL","strategy":"fixture_strategy","journal_only":False,"metadata":{"event_key":"retained","analysis_status":"validated","retained_from_signal_id":"previous"}}
    early_health = [StrategyHealthRecord("h14","epoch",session,"p14","fixture_strategy","signals",1,{})]
    first_engine = SimpleNamespace(_price_cache={},pending_late_signals=lambda *a:[retained])
    monkeypatch.setattr(engine,"pending_late_signals",lambda *a:[])
    cohorts = [dict(config=SimpleNamespace(horizon=horizon,name="cohort_"+horizon),engine=which,
                    executor=SimpleNamespace(validated_execution_reference_bars=lambda *a:{})) for horizon,which in (("14d",first_engine),("30d",engine))]
    owner = SimpleNamespace(cohorts=cohorts,_base_config=config,
        _screen_for_horizon=lambda data,dt,horizon:([earlier],{},early_health) if horizon=="14d" else failed)
    state = daily_pipeline.DailyRunState(owner,str(session),session,datetime.now(timezone.utc),epoch_id="epoch",valid=cohorts)
    monkeypatch.setattr(source_inputs,"daily_source_store",lambda *a:(SimpleNamespace(load_frozen=lambda *a:{}),"identity"))
    persisted = []
    monkeypatch.setattr(daily_pipeline,"_persist_screen_health",lambda state:persisted.extend(list(state.horizon_signals.values())))
    assert daily_pipeline.run_horizon_screening(state) is None
    assert state.model_coverage == {"complete":False,"reason":"model_deadline_exhausted"}
    assert persisted
    all_signals = [s for signals,_,_ in persisted for s in signals]
    assert {s["ticker"] for s in all_signals} == {"DAL","UAL"}
    assert all(s["journal_only"] for s in all_signals)
    assert all(s["metadata"]["analysis_failure_reason"] == "model_deadline_exhausted" for s in all_signals)
    assert all(row.status=="data_failure" for _,_,health in persisted for row in health)
