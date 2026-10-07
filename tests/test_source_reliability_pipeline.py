"""Current 16-book acceptance using real adapters, screens and ledger phases.

Only HTTP/SDK and model-response boundaries are replaced within the pipeline.
The subprocess boundary uses an in-process bridge to retain those fixtures;
this test does not claim OS process isolation. Runtime locking is real with a
temporary canonical lock path. Socket access is
forbidden, including for an accidentally unmocked provider or model client.
"""
from collections import Counter
from copy import deepcopy
from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
from pathlib import Path
import subprocess
from datetime import date, datetime, timedelta, timezone
import importlib
import json
import math
import socket
import sys
from types import ModuleType, SimpleNamespace

import pandas as pd
import pytest
import requests
import yfinance

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.strategies.orchestration.cohort_orchestrator import (
    CohortOrchestrator, build_default_cohorts,
)
from tradingagents.strategies.orchestration.run_outcome import (
    DAILY_RESULT_PREFIX, DAILY_RESULT_WIRE_VERSION,
)

SESSION = date(2026, 10, 2)
GENERATION = "gen_099"
COMMIT = "a" * 40


class InterruptedWorker(BaseException):
    """Simulate loss of the worker between durable cohort staging phases."""


class TransportFixture:
    """Representative provider payloads and controlled upstream failures."""

    def __init__(self):
        self.session = SESSION
        self.now = datetime(2026, 10, 2, 22, tzinfo=timezone.utc)
        self.calls = Counter()
        self.blocked_calls = Counter()
        self.request_trace = []
        self.model_calls = Counter()
        self.fault = None
        self.block_all = False
        self.interrupt_committee_at = None

    @staticmethod
    def response(payload, status=200, *, headers=None):
        response = requests.Response()
        response.status_code = status
        response._content = json.dumps(payload).encode()
        response._content_consumed = True
        response.headers.update(headers or {})
        return response

    def http(self, method, url, **kwargs):
        if self.block_all:
            self.blocked_calls["http"] += 1
            raise AssertionError("accepted session attempted another provider call")
        self.calls[url.split("?")[0]] += 1
        params = kwargs.get("params") or {}
        self.request_trace.append((url, dict(params)))
        if "data.alpaca.markets" in url:
            ticker = url.split("/")[-2]
            requested = params["start"][:10]
            opening = 184 if requested == "2026-10-05" else 182
            payload = {"symbol": ticker, "bars": [
                {"t": f"{requested}T04:00:00Z", "o": opening,
                 "h": opening + 4, "l": opening - 2, "c": opening + 2,
                 "v": 1_000_000, "n": 45_000, "vw": opening + 1}
            ], "next_page_token": None}
            if self.fault == "sip_incoherent" and ticker == "NVDA":
                payload["bars"][0]["l"] = opening + 1
            if self.fault == "sip_unsupported" and ticker == "NVDA":
                return self.response({}, 404)
            return self.response(payload)
        if "finnhub.io" in url:
            if url.endswith("/calendar/earnings"):
                return self.response({"earningsCalendar": [{
                    "symbol": "NVDA", "date": "2026-10-01", "year": 2026,
                    "quarter": 3, "epsActual": 1.25, "epsEstimate": 1.05,
                    "revenueActual": 46_000_000_000, "revenueEstimate": 44_000_000_000,
                }]})
            if url.endswith("/company-news"):
                news = [{"id": 901, "headline": "NVIDIA quarterly results exceed outlook",
                         "summary": "Revenue and earnings exceeded guidance; management raised its forecast.",
                         "source": "Reuters", "datetime": int(datetime(2026, 10, 2, 17, tzinfo=timezone.utc).timestamp()),
                         "url": "https://example.test/nvda-results", "category": "company"}]
                return self.response(news if params.get("symbol") == "NVDA" else [])
            if url.endswith("/stock/peers"):
                return self.response([])
        if "efts.sec.gov" in url:
            call = self.calls[url]
            if self.fault in {"edgar_500_then_200", "edgar_429_then_200"} and call == 1:
                status = 500 if self.fault == "edgar_500_then_200" else 429
                return self.response({}, status, headers={"Retry-After": "0"})
            if self.fault == "edgar_timeout":
                raise requests.Timeout("offline exhausted upstream timeout")
            if self.fault == "edgar_partial" and params.get("forms") == "10-K":
                good = {"_source": {"form": "10-K", "file_date": "2026-10-01",
                    "adsh": "0001045810-26-000001", "display_names": ["NVIDIA Corporation (NVDA)"], "ciks": ["0001045810"]}}
                return self.response({"hits": {"hits": [good, {"_source": {"form": "10-K"}}]}})
            if self.fault == "edgar_malformed":
                return self.response({"hits": {"hits": [{"_source": {"form": "10-K"}}]}})
            return self.response({"hits": {"hits": []}})
        if url.endswith("company_tickers.json"):
            symbols = ["AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSLA", "JPM"]
            return self.response({str(i): {"ticker": symbol, "title": symbol + " Corporation", "cik_str": i + 1000}
                                  for i, symbol in enumerate(symbols)})
        if "data.sec.gov/submissions" in url:
            return self.response({"filings": {"recent": {
                "form": [], "filingDate": [], "accessionNumber": [], "primaryDocument": [],
            }}})
        if "financialmodelingprep.com" in url:
            return self.response([])
        if "api.regulations.gov" in url:
            return self.response({"data": []})
        if "courtlistener.com" in url:
            return self.response({"results": [], "count": 0, "next": None})
        if "api.usaspending.gov" in url:
            return self.response({"results": [], "page_metadata": {"hasNext": False}})
        if "ncei.noaa.gov" in url:
            observation_date = params["enddate"]
            return self.response({"metadata": {"resultset": {"count": 3, "offset": 1}},
                                  "results": [{"date": observation_date + "T00:00:00", "datatype": datatype,
                                               "station": "GHCND:USC00130001", "value": value}
                                              for datatype, value in [("TMAX", 85), ("TMIN", 55), ("PRCP", 0.12)]]})
        if "quickstats.nass.usda.gov" in url:
            return self.response({"data": []})
        if "usdmdataservices.unl.edu" in url:
            return self.response([])
        raise AssertionError("unconfigured HTTP fixture endpoint: " + url)

    def yahoo(self, tickers, **kwargs):
        if self.block_all:
            self.blocked_calls["yahoo_sdk"] += 1
            raise AssertionError("accepted session attempted Yahoo SDK call")
        self.calls["yahoo_sdk"] += 1
        symbols = [tickers] if isinstance(tickers, str) else tickers
        # Provider end is exclusive; history has coherent varying observations.
        start = pd.Timestamp(kwargs.get("start", self.session - timedelta(days=90)))
        end = pd.Timestamp(kwargs.get("end", self.session + timedelta(days=1)))
        import exchange_calendars
        dates = pd.DatetimeIndex(list(exchange_calendars.get_calendar("XNYS").sessions_in_range(start, end - timedelta(days=1))))
        values = {}
        for symbol in symbols:
            base = 18 if symbol == "^VIX" else 182 if symbol == "NVDA" else 100
            closes = [base * (1 + .001 * math.sin(i * 1.7)) for i in range(len(dates))]
            for field in ("Open", "High", "Low", "Close", "Volume", "Dividends", "Stock Splits"):
                values[field, symbol] = (
                    [1_000_000] * len(dates) if field == "Volume" else
                    [0] * len(dates) if field in {"Dividends", "Stock Splits"} else
                    [value + (1 if field == "High" else -1 if field == "Low" else 0) for value in closes]
                )
        result = pd.DataFrame(values, index=dates)
        result.columns = pd.MultiIndex.from_tuples(result.columns)
        return result

    def fred(self, series_id, **kwargs):
        if self.block_all:
            self.blocked_calls["fred_sdk"] += 1
            raise AssertionError("accepted session attempted FRED SDK call")
        self.calls["fred_sdk"] += 1
        value = {"UNRATE": 4, "CPIAUCSL": 320, "PAYEMS": 160000, "ICSA": 210000,
                 "BAMLH0A0HYM2": 3.5, "BAMLC0A4CBBB": 1.2}.get(series_id, 2)
        frequency = "MS" if series_id in {"UNRATE", "CPIAUCSL", "PAYEMS", "FEDFUNDS"} else "W-THU" if series_id == "ICSA" else "B"
        return pd.Series(value, index=pd.date_range(kwargs["observation_start"], kwargs["observation_end"], freq=frequency))

    def cot(self, year, **kwargs):
        if self.block_all:
            self.blocked_calls["cftc_sdk"] += 1
            raise AssertionError("accepted session attempted CFTC SDK call")
        from tradingagents.strategies.data_sources.cftc_source import COMMODITY_CODES
        self.calls["cftc_sdk"] += 1
        # Last value is central rather than a contrarian extreme.
        nets = list(range(38)) + [19]
        return pd.DataFrame([
            {"Market_and_Exchange_Names": name, "Report_Date_as_YYYY-MM-DD": str(day.date()),
             "M_Money_Positions_Long_All": 1000 + net, "M_Money_Positions_Short_All": 1000}
            for name in COMMODITY_CODES.values()
            for day, net in zip(pd.date_range(f"{year}-01-06", periods=39, freq="7D"), nets)
        ])

    def model(self, client, *, system, prompt, **kwargs):
        if "portfolio manager" in system:
            self.model_calls["committee"] += 1
            if self.model_calls["committee"] == self.interrupt_committee_at:
                raise InterruptedWorker("worker stopped after earlier cohort staging")
            return json.dumps([{"ticker": "NVDA", "direction": "long", "position_size_pct": .05,
                                "confidence": .9, "rationale": "Material quarterly earnings and outlook surprise",
                                "contributing_strategies": ["earnings_call"], "regime_alignment": "aligned"}])
        self.model_calls["enrichment"] += 1
        return json.dumps({"direction": "long", "conviction": .9,
                           "rationale": "Quarterly earnings exceed forecast with raised guidance"})


@pytest.fixture
def pipeline(monkeypatch, tmp_path):
    fixture = TransportFixture()

    def forbid_socket(*args, **kwargs):
        raise AssertionError("live socket calls forbidden by acceptance harness")
    monkeypatch.setattr(socket.socket, "connect", forbid_socket)
    monkeypatch.setattr(socket, "create_connection", forbid_socket)
    monkeypatch.setattr(requests.sessions.Session, "request", lambda _self, method, url, **kwargs: fixture.http(method, url, **kwargs))
    monkeypatch.setattr(yfinance, "download", fixture.yahoo)
    import fredapi
    monkeypatch.setattr(fredapi.Fred, "get_series", lambda _self, series_id, **kwargs: fixture.fred(series_id, **kwargs))
    cot = ModuleType("cot_reports")
    cot.cot_year = fixture.cot
    monkeypatch.setitem(sys.modules, "cot_reports", cot)
    from tradingagents.strategies import llm_utils
    monkeypatch.setattr(llm_utils, "call_analysis_model", fixture.model)
    # Accelerate provider pacing while exercising the real request/retry policy.
    from tradingagents.strategies.data_sources import request_policy
    monkeypatch.setattr(request_policy, "PROVIDER_LIMITS", {})
    monkeypatch.setattr(request_policy.time, "sleep", lambda _: None)
    for key in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.setenv(key, "offline-dummy")

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixture.now.astimezone(tz) if tz else fixture.now.replace(tzinfo=None)

    for name in ("cohort_orchestrator", "multi_strategy_engine", "session_executor", "governed_market_data", "daily_pipeline", "generation_manager"):
        module = importlib.import_module("tradingagents.strategies.orchestration." + name)
        monkeypatch.setattr(module, "datetime", FixedDatetime)
    from tradingagents.strategies.execution import price_source
    monkeypatch.setattr(price_source, "datetime", FixedDatetime)
    monkeypatch.setenv("EVENTEDGE_SOURCE_CACHE_DIR", str(tmp_path / "source_cache"))
    monkeypatch.delenv("EVENTEDGE_RUNTIME_LOCK_FD", raising=False)
    monkeypatch.delenv("EVENTEDGE_RUNTIME_LOCK_MODE", raising=False)
    config = deepcopy(DEFAULT_CONFIG)
    config["autoresearch"]["state_dir"] = str(tmp_path / "data/generations" / GENERATION)
    config["autoresearch"]["finnhub_reliability"]["rate_delay_s"] = 0
    for key in ("finnhub_api_key", "fred_api_key", "regulations_api_key", "courtlistener_token", "noaa_cdo_token", "usda_nass_api_key", "fmp_api_key"):
        config["autoresearch"][key] = "offline-dummy"
    orchestrator = CohortOrchestrator(build_default_cohorts(config), config,
                                     generation_id=GENERATION, generation_commit=COMMIT)
    from tradingagents.strategies.orchestration import generation_manager, runtime_lock
    from scripts import run_cohorts
    monkeypatch.setattr(runtime_lock, "canonical_runtime_lock_path", lambda _: tmp_path / "runtime.lock")
    monkeypatch.setattr(generation_manager, "canonical_runtime_lock_path", lambda _: tmp_path / "runtime.lock")
    manifest_path = Path(config["autoresearch"]["state_dir"]).parent / "manifest.json"
    manifest_path.write_text(json.dumps({"generations": [{"gen_id": GENERATION, "git_commit": COMMIT,
        "state_dir": config["autoresearch"]["state_dir"], "worktree_path": str(tmp_path),
        "status": "active", "run_history": []}]}))

    def worker_process(cmd, *, env, **kwargs):
        # Keep transport/socket fixtures active while running the genuine worker
        # and inherited runtime lock. This replaces process isolation only.
        assert cmd[1] == "scripts/run_cohorts.py"
        assert kwargs["pass_fds"] == (int(env["EVENTEDGE_RUNTIME_LOCK_FD"]),)
        assert env["EVENTEDGE_GENERATION_ID"] == GENERATION
        assert env["EVENTEDGE_GENERATION_COMMIT"] == COMMIT
        requested = cmd[cmd.index("--date") + 1]
        stdout, stderr = StringIO(), StringIO()
        code = 0
        with monkeypatch.context() as child_env:
            for key in ("EVENTEDGE_RUNTIME_LOCK_FD", "EVENTEDGE_RUNTIME_LOCK_MODE"):
                child_env.setenv(key, env[key])
            with redirect_stdout(stdout), redirect_stderr(stderr):
                try:
                    run_cohorts._run_daily(SimpleNamespace(no_llm=False), config, requested, GENERATION, COMMIT)
                except SystemExit as error:
                    code = error.code
                except InterruptedWorker as error:
                    raise subprocess.TimeoutExpired(cmd, 1, output=stdout.getvalue(), stderr=stderr.getvalue()) from error
        return subprocess.CompletedProcess(cmd, code, stdout.getvalue(), stderr.getvalue())

    monkeypatch.setattr(generation_manager.subprocess, "run", worker_process)
    fixture.manager = generation_manager.GenerationManager(str(tmp_path))
    yield fixture, orchestrator, config, tmp_path
    for cohort in orchestrator.cohorts:
        cohort["ledger"].close()


def accepted_input_bytes(config):
    directory = Path(config["autoresearch"]["state_dir"]) / "source_inputs"
    return {str(path.relative_to(directory)): path.read_bytes() for path in directory.rglob("*.json")}


def run_campaign(pipeline, session=SESSION):
    fixture, orchestrator, config, repo = pipeline
    from tradingagents.strategies.orchestration.generation_manager import _extract_cohort_results
    from tradingagents.strategies.orchestration.operational_report import build_operational_report
    fixture.now += timedelta(seconds=1)
    result = fixture.manager.run_daily(str(session))[GENERATION]
    artifact = json.loads(Path(result["evidence_path"]).read_text())
    wire = _extract_cohort_results(artifact["stdout"])
    assert wire is not None and len(wire) == 16
    assert artifact["process_return_code"] == (1 if result["outcome"] == "failed" else 0)
    assert artifact["result"]["outcome"] == result["outcome"]
    assert artifact["generation_id"] == GENERATION and artifact["generation_commit"] == COMMIT
    assert artifact["requested_session"] == str(session)
    report = build_operational_report(repo, GENERATION, str(session), snapshot_guaranteed=True)
    assert report["evidence_complete"], report["diagnostics"]
    assert report["outcome"] == result["outcome"]
    assert report["accounting_valid"] == result["execution_valid"]
    assert report["input_coverage_valid"] == result["input_coverage_valid"]
    assert report["source_health_failures"] == result["source_health_failures"]
    assert report["candidate_input_issues"] == result.get("candidate_input_issues", [])
    assert report["staging_valid"] == all(row["staging_valid"] for row in wire.values())
    if result["outcome"] in {"clean", "degraded"}:
        assert report["staging_complete"] is True
    assert artifact["process_status"] == "completed"
    assert artifact["stdout"].splitlines()[-1].startswith(DAILY_RESULT_PREFIX)
    assert json.loads(artifact["stdout"].splitlines()[-1].removeprefix(DAILY_RESULT_PREFIX))["wire_version"] == DAILY_RESULT_WIRE_VERSION
    return result, wire, report


def test_current_full_pipeline_has_48_healthy_records_and_16_valid_books(pipeline):
    fixture, orchestrator, config, repo = pipeline
    result, wire, report = run_campaign(pipeline)
    assert result["outcome"] == "clean" and result["success"] is True
    assert all(not row["error"] and row["execution_valid"] and row["staging_valid"] and row["input_coverage_valid"] for row in wire.values())
    records = orchestrator._metric_store.read_strategy_health(session=SESSION)
    assert len(records) == 48
    assert all(row.status in {"signals", "legitimate_no_event"} for row in records), [(row.strategy, row.status, row.evidence) for row in records]
    assert {row.strategy for row in records} == {strategy.name for strategy in orchestrator.cohorts[0]["engine"].paper_trade_strategies}
    assert fixture.model_calls["enrichment"] >= 1
    assert fixture.model_calls["committee"] >= 1
    assert sum(len(cohort["ledger"].pending_intents(date(2026, 10, 5))) for cohort in orchestrator.cohorts) >= 1
    assert all(not cohort["ledger"].read_fills() for cohort in orchestrator.cohorts)
    assert all(float(signal["reference_close"]) == 184 for row in wire.values() for signal in row["signals"] if signal["ticker"] == "NVDA")
    requests = [(url, params) for url, params in fixture.request_trace if "data.alpaca.markets" in url]
    assert requests and all(params["feed"] == "sip" and params["adjustment"] == "raw" and params["timeframe"] == "1Day" for _, params in requests)
    assert {url.split("/")[-2] for url, _ in requests} >= {"SPY", "BIL", "NVDA"}
    for cohort in orchestrator.cohorts:
        context = cohort["ledger"]._connection.execute("SELECT economic_inputs_json FROM session_execution_contexts WHERE session=?", (str(SESSION),)).fetchone()
        frozen = json.loads(context[0])["market"]
        assert {bar["source"] for bar in frozen["raw_bars"]} == {"alpaca-sip-1d-raw"}
        assert {bar["source"] for bar in frozen["benchmarks"]} == {"yfinance-adjusted"}


@pytest.mark.parametrize("fault", ["edgar_500_then_200", "edgar_429_then_200", "edgar_timeout", "edgar_malformed", "edgar_partial", "sip_incoherent", "sip_unsupported"])
def test_transport_faults_agree_across_health_accounting_wire_and_report(pipeline, fault):
    fixture, orchestrator, config, repo = pipeline
    fixture.fault = fault
    result, wire, report = run_campaign(pipeline)
    records = orchestrator._metric_store.read_strategy_health(session=SESSION)
    assert len(records) == 48
    from tradingagents.strategies.orchestration.source_inputs import SourceInputStore, configuration_fingerprint
    slot = configuration_fingerprint({"generation": GENERATION, "session": str(SESSION)})
    envelope = SourceInputStore.decode((Path(config["autoresearch"]["state_dir"]) / "source_inputs" / (slot + ".json")).read_text())
    diagnostics = envelope["payload"]["edgar"].get("_request_diagnostics", [])
    assert result["execution_valid"] is True
    assert all(row["execution_valid"] for row in wire.values())
    if fault.endswith("then_200"):
        assert result["outcome"] == "clean"
        assert result["input_coverage_valid"] is True
        assert not result["source_health_failures"]
        assert all(row.status in {"signals", "legitimate_no_event"} for row in records)
        assert any(row["provider"] == "edgar" and row["recovered"] and row["attempts"] == 2 and row["http_status"] == 200 for row in report["sources"]["recovered"])
        assert any(row["recovered"] and row["attempts"] == 2 for row in diagnostics)
    elif fault.startswith("edgar"):
        assert result["outcome"] == "degraded"
        assert result["input_coverage_valid"] is False
        failed = [row for row in records if row.status == "data_failure"]
        assert len(failed) == 12
        assert {row.strategy for row in failed} == {"insider_activity", "filing_analysis", "quantum_readiness"}
        assert all(row["sources"] == ["edgar"] and len(row["affected_cohorts"]) == 4 for row in result["source_health_failures"])
        assert report["sources"]["unresolved"]
        if fault == "edgar_timeout":
            assert any(row["reason_code"] == "timeout" and row["attempts"] == 3 for row in diagnostics)
        if fault == "edgar_partial":
            assert envelope["payload"]["edgar"]["filings"]
            assert envelope["payload"]["edgar"]["filings"][0]["ticker"] == "NVDA"
            assert envelope["payload"]["edgar"].get("error")
    else:
        assert result["outcome"] == "degraded"
        assert result["input_coverage_valid"] is True
        assert not result["source_health_failures"]
        assert result["candidate_bar_quarantines"] == ["NVDA"]
        assert all(not row["staging_valid"] and not row["intents_staged"] for row in wire.values())
        assert all(issue["ticker"] == "NVDA" and issue["dependency_kind"] == "reference_bar" for issue in result["candidate_input_issues"])


def test_frozen_duplicate_and_next_session_fill_are_deterministic(pipeline):
    fixture, orchestrator, config, repo = pipeline
    result, wire, report = run_campaign(pipeline)
    accepted = accepted_input_bytes(config)
    call_counts, model_counts = fixture.calls.copy(), fixture.model_calls.copy()
    intent_ids = {cohort["config"].name: tuple(row.intent_id for row in cohort["ledger"].pending_intents(date(2026, 10, 5))) for cohort in orchestrator.cohorts}
    fixture.block_all = True
    repeated, repeat_wire, repeat_report = run_campaign(pipeline)
    assert repeated["outcome"] == "clean"
    assert fixture.calls == call_counts and fixture.model_calls == model_counts
    assert not fixture.blocked_calls
    assert repeat_report["attempt_counts"] == {"daily": 2, "preflight": 0}
    assert accepted_input_bytes(config) == accepted
    assert {cohort["config"].name: tuple(row.intent_id for row in cohort["ledger"].pending_intents(date(2026, 10, 5))) for cohort in orchestrator.cohorts} == intent_ids

    fixture.block_all = False
    fixture.session = date(2026, 10, 5)
    fixture.now = datetime(2026, 10, 5, 22, tzinfo=timezone.utc)
    filled, fill_wire, fill_report = run_campaign(pipeline, fixture.session)
    assert filled["outcome"] == "clean"
    fills = {cohort["config"].name: tuple(cohort["ledger"].read_fills()) for cohort in orchestrator.cohorts}
    assert all(len(rows) == 1 for rows in fills.values())
    assert all(rows[0].session == fixture.session and rows[0].side == "buy" and rows[0].reference_price == 184 and rows[0].intent_id in intent_ids[name] for name, rows in fills.items())
    assert all(row["fills"]["entries"] == 1 and row["fills"]["total"] == 1 for row in fill_report["cohorts"].values())
    assert all(row["account"]["valid"] and float(row["account"]["long_market_value"]) > 0 for row in fill_wire.values())
    fixture.block_all = True
    call_counts, model_counts = fixture.calls.copy(), fixture.model_calls.copy()
    again, again_wire, again_report = run_campaign(pipeline, fixture.session)
    assert again["outcome"] == "clean"
    assert fixture.calls == call_counts and fixture.model_calls == model_counts
    assert not fixture.blocked_calls
    assert {cohort["config"].name: tuple(cohort["ledger"].read_fills()) for cohort in orchestrator.cohorts} == fills


def prepare_interrupted_staging(pipeline, *, committee_ordinal=5):
    fixture, orchestrator, config, repo = pipeline
    run_campaign(pipeline)
    fixture.session = date(2026, 10, 5)
    fixture.now = datetime(2026, 10, 5, 22, tzinfo=timezone.utc)
    fixture.interrupt_committee_at = fixture.model_calls["committee"] + committee_ordinal
    interrupted = fixture.manager.run_daily(str(fixture.session))[GENERATION]
    assert interrupted["outcome"] == "failed" and interrupted["success"] is False
    artifact = json.loads(Path(interrupted["evidence_path"]).read_text())
    assert artifact["process_status"] == "timeout"
    assert artifact["process_return_code"] is None
    assert sum(cohort["ledger"]._connection.execute("SELECT COUNT(*) FROM staging_runs WHERE session=?", (str(fixture.session),)).fetchone()[0] for cohort in orchestrator.cohorts) == committee_ordinal - 1
    before = {cohort["config"].name: tuple(cohort["ledger"].read_fills()) for cohort in orchestrator.cohorts}
    assert all(len(rows) == 1 for rows in before.values())
    accepted = accepted_input_bytes(config)
    calls = fixture.calls.copy()
    fixture.interrupt_committee_at = None
    fixture.block_all = True
    assert any(name.startswith("staging_volatility/") for name in accepted)
    return before, accepted, calls


def test_interrupted_staging_resumes_frozen_sources_without_duplicate_fills(pipeline):
    fixture, orchestrator, config, repo = pipeline
    before, accepted, calls = prepare_interrupted_staging(pipeline)
    result, wire, report = run_campaign(pipeline, fixture.session)
    assert result["outcome"] == "clean"
    assert fixture.calls == calls
    assert not fixture.blocked_calls
    assert accepted_input_bytes(config) == accepted
    assert {cohort["config"].name: tuple(cohort["ledger"].read_fills()) for cohort in orchestrator.cohorts} == before
    assert report["accounting_valid"] and report["staging_complete"]
    assert report["attempt_counts"] == {"daily": 2, "preflight": 0}
    assert any(attempt["process_status"] == "timeout" for attempt in report["attempts"])


@pytest.mark.parametrize("damage,committee_ordinal,failed_count", [
    pytest.param("corrupt", 5, 12, id="corrupt"),
    pytest.param("missing", 5, 12, id="missing"),
    pytest.param("missing", 1, 16, id="missing_before_first_staging"),
])
def test_damaged_accepted_volatility_blocks_resume_without_refetch_or_new_fills(pipeline, damage, committee_ordinal, failed_count):
    fixture, orchestrator, config, repo = pipeline
    before, accepted, calls = prepare_interrupted_staging(pipeline, committee_ordinal=committee_ordinal)
    from tradingagents.strategies.orchestration.source_inputs import SourceInputStore
    from tradingagents.strategies.orchestration.generation_manager import _extract_cohort_results
    from tradingagents.strategies.orchestration.operational_report import build_operational_report
    folder = Path(config["autoresearch"]["state_dir"]) / "source_inputs" / "staging_volatility"
    paths = [path for path in folder.glob("*.json") if SourceInputStore.decode(path.read_text())["identity"]["session"] == str(fixture.session)]
    assert len(paths) == 1
    if damage == "corrupt":
        paths[0].write_text("{}")
    else:
        paths[0].unlink()
    fixture.now += timedelta(seconds=1)
    result = fixture.manager.run_daily(str(fixture.session))[GENERATION]
    assert result["outcome"] == "failed" and result["success"] is False
    assert result["execution_valid"] is True
    artifact = json.loads(Path(result["evidence_path"]).read_text())
    assert artifact["process_status"] == "completed" and artifact["process_return_code"] == 1
    wire = _extract_cohort_results(artifact["stdout"])
    assert len(wire) == 16
    failed = [row for row in wire.values() if row["error"]]
    assert len(failed) == failed_count and all(row["staging_valid"] is False for row in failed)
    assert all("staging volatility" in row["invalid_reason"] for row in failed)
    assert fixture.calls == calls
    assert not fixture.blocked_calls
    if damage == "missing":
        assert not paths[0].exists()
    else:
        assert paths[0].read_text() == "{}"
    assert {cohort["config"].name: tuple(cohort["ledger"].read_fills()) for cohort in orchestrator.cohorts} == before
    current = accepted_input_bytes(config)
    assert {key: value for key, value in current.items() if not key.startswith("staging_volatility/")} == {key: value for key, value in accepted.items() if not key.startswith("staging_volatility/")}
    report = build_operational_report(repo, GENERATION, str(fixture.session), snapshot_guaranteed=True)
    assert report["outcome"] == "failed" and report["accounting_valid"] is True
    assert report["evidence_complete"] is False and report["performance_claims_withheld"] is True
    assert any("volatility" in row["code"] for row in report["diagnostics"])
