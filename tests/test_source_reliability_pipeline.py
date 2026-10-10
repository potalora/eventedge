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
        self.weather_disruption = False

    @staticmethod
    def response(payload, status=200, *, headers=None):
        response = requests.Response()
        response.status_code = status
        response._content = (payload if isinstance(payload, str) else json.dumps(payload)).encode()
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
            requested = params["start"][:10]
            opening = 184 if requested == "2026-10-05" else 182
            def bars(ticker):
                values = [{"t": f"{requested}T04:00:00Z", "o": opening,
                           "h": opening + 4, "l": opening - 2, "c": opening + 2,
                           "v": 1_000_000, "n": 45_000, "vw": opening + 1}]
                if self.fault == "sip_incoherent" and ticker == "NVDA":
                    values[0]["l"] = opening + 1
                return values
            if url == "https://data.alpaca.markets/v2/stocks/bars":
                payload = {"bars": {ticker: bars(ticker) for ticker in params["symbols"].split(",")
                                    if not (self.fault == "sip_unsupported" and ticker == "NVDA")},
                           "next_page_token": None}
            else:
                ticker = url.split("/")[-2]
                if self.fault == "sip_unsupported" and ticker == "NVDA":
                    return self.response({}, 404)
                payload = {"symbol": ticker, "bars": bars(ticker), "next_page_token": None}
            return self.response(payload)
        if "finnhub.io" in url:
            if url.endswith("/calendar/earnings"):
                earnings = [{"symbol": "NVDA", "date": "2026-10-01", "year": 2026,
                    "quarter": 3, "epsActual": 1.25, "epsEstimate": 1.05,
                    "revenueActual": 46_000_000_000, "revenueEstimate": 44_000_000_000}]
                if self.session > SESSION:
                    # A distinct issuer/fiscal event keeps later staging and
                    # crash-boundary tests active without replaying NVDA's event.
                    earnings.append(dict(earnings[0], symbol="AAPL", date=str(self.session)))
                return self.response({"earningsCalendar": earnings})
            if url.endswith("/company-news"):
                symbol = params.get("symbol")
                if symbol == "NVDA":
                    return self.response([{"id": 901, "headline": "NVIDIA quarterly results exceed outlook",
                        "summary": "Revenue and earnings exceeded guidance; management raised its forecast.",
                        "source": "Reuters", "datetime": int(datetime(2026, 10, 2, 17, tzinfo=timezone.utc).timestamp()),
                        "url": "https://example.test/nvda-results", "category": "company"}])
                if symbol == "AAPL" and self.session > SESSION:
                    return self.response([{"id": 902, "headline": "Apple quarterly results exceed outlook",
                        "summary": "Apple earnings exceeded guidance; management raised its forecast.",
                        "source": "Reuters", "datetime": int(datetime.combine(self.session, datetime.min.time(), tzinfo=timezone.utc).replace(hour=17).timestamp()),
                        "url": "https://example.test/aapl-results", "category": "company"}])
                return self.response([])
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
            return self.response({"hits": {"hits": [], "total":{"value":0,"relation":"eq"}}})
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
            return self.response({"data": [], "meta":{"totalElements":0,"totalPages":1}})
        if "courtlistener.com" in url:
            return self.response({"results": [], "count": 0, "next": None})
        if "api.usaspending.gov" in url:
            return self.response({"results": [], "page_metadata": {"page":1,"hasNext": False}})
        if "ncei.noaa.gov/pub/data/ghcn/daily" in url:
            from tradingagents.strategies.data_sources.noaa_source import AG_STATES
            stations = {f"USC{i:08}": state for i, state in enumerate(AG_STATES, 1)}
            if url.endswith("ghcnd-stations.txt"):
                # Native GHCN catalog fixed columns: ID 1-11, state 39-40.
                return self.response("\n".join(f"{station:<11}{'':27}{state} OFFLINE STATION"
                                                for station, state in stations.items()))
            assert url.endswith("ghcnd-inventory.txt")
            # Native inventory: element 32-35, first year 37-40, last 42-45.
            return self.response("\n".join(f"{station:<11}{'':20}{dtype} 2020 2026"
                for station in stations for dtype in ("TMAX", "TMIN", "PRCP")))
        if "ncei.noaa.gov/access/services/data/v1" in url:
            assert params["dataset"] == "daily-summaries"
            assert params["units"] == "standard" and params["includeAttributes"] == "true"
            assert params["dataTypes"] == "TMAX,TMIN,PRCP"
            days = pd.date_range(params["startDate"], params["endDate"], freq="D")
            stations = params["stations"].split(",")
            assert 1 <= len(stations) <= 100
            return self.response([{"STATION": station, "DATE": str(day.date()),
                "TMAX": "85", "TMIN": "55", "PRCP": "0.12",
                "TMAX_ATTRIBUTES": ",,W", "TMIN_ATTRIBUTES": ",,W", "PRCP_ATTRIBUTES": ",,W"}
                for station in stations for day in days])
        if "quickstats.nass.usda.gov" in url:
            from tradingagents.strategies.data_sources.usda_source import condition_scope
            prior_sunday = self.session - timedelta(days=(self.session.weekday()+1)%7)
            scope = condition_scope(params["commodity_desc"], str(self.session))
            return self.response({"data":[{"commodity_desc":params["commodity_desc"],
                "year":params["year"], "state_alpha":state, "class_desc":crop_class,
                "week_ending":str(prior_sunday - timedelta(weeks=week)), "unit_desc":unit,"Value":value}
                for crop_class, states in scope["class_states"].items()
                for state in states if state in params["state_alpha"] for week in (1,0)
                for unit,value in [("PCT GOOD","50"),("PCT EXCELLENT","20"),("PCT FAIR","20"),("PCT POOR","8"),("PCT VERY POOR","2")]]})
        if "usdmdataservices.unl.edu" in url:
            assert params["statisticsType"] == 2
            observation = self.session - timedelta(days=(self.session.weekday()-1)%7)
            from tradingagents.strategies.data_sources.drought_monitor_source import STATE_FIPS
            by_fips = {fips: state for state, fips in STATE_FIPS.items()}
            return self.response([{"stateAbbreviation":by_fips[fips],"mapDate":observation.strftime("%Y%m%d"),
                "statisticFormatID":2,"none":0 if self.weather_disruption else 100,
                "d0":0,"d1":0,"d2":0,"d3":100 if self.weather_disruption else 0,"d4":0}
                for fips in params["aoi"].split(",")])
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
        # A real annual archive, with current latest report central in a full
        # rolling window; prior-year rows are required near New Year.
        days = pd.date_range(f"{year}-01-01", f"{year}-12-31", freq="W-TUE")
        latest = self.session - timedelta(days=(self.session.weekday()-1)%7)
        return pd.DataFrame([
            {"Market_and_Exchange_Names":name, "Report_Date_as_YYYY-MM-DD":str(day.date()),
             "M_Money_Positions_Long_All":1000 + (19 if day.date()==latest else day.isocalendar().week),
             "M_Money_Positions_Short_All":1000}
            for name in COMMODITY_CODES.values() for day in days])

    def model(self, client, *, system, prompt, **kwargs):
        if "portfolio manager" in system:
            self.model_calls["committee"] += 1
            if self.model_calls["committee"] == self.interrupt_committee_at:
                raise InterruptedWorker("worker stopped after earlier cohort staging")
            # The deterministic fixture recommends only a thesis actually
            # admitted to this decision; repeated events may already be used.
            signal_section = prompt.split("Signals (all ", 1)[1].split("Current positions", 1)[0]
            admitted = [json.loads(line[line.index("{"):]) for line in signal_section.splitlines()
                        if line.startswith("  ") and "{" in line]
            if not any(row.get("ticker") == "NVDA" and row.get("direction") == "long"
                       and row.get("strategy") == "earnings_call" for row in admitted):
                return "[]"
            return json.dumps([{"ticker": "NVDA", "direction": "long", "position_size_pct": .05,
                                "confidence": .9, "rationale": "Material quarterly earnings and outlook surprise",
                                "contributing_strategies": ["earnings_call"], "regime_alignment": "aligned"}])
        self.model_calls["enrichment"] += 1
        return json.dumps({"direction": "long", "conviction": .9,
                           "rationale": "Quarterly earnings exceed forecast with raised guidance",
                           "evidence_claim": "Quarterly earnings exceed forecast with raised guidance"})


@pytest.fixture
def pipeline(monkeypatch, tmp_path):
    fixture = TransportFixture()
    # CLI dotenv loading in another test must not authorize shadow acquisition.
    # Dedicated shadow tests explicitly supply their own credentials afterward.
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN", raising=False)

    def forbid_socket(*args, **kwargs):
        raise AssertionError("live socket calls forbidden by acceptance harness")
    monkeypatch.setattr(socket.socket, "connect", forbid_socket)
    monkeypatch.setattr(socket, "create_connection", forbid_socket)
    monkeypatch.setattr(requests.sessions.Session, "request", lambda _self, method, url, **kwargs: fixture.http(method, url, **kwargs))
    monkeypatch.setattr(yfinance, "download", fixture.yahoo)
    from tradingagents.strategies.data_sources.fred_source import FREDSource
    monkeypatch.setattr(FREDSource, "_get_series", lambda _self, series_id, **kwargs: fixture.fred(series_id, **kwargs))
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

    for name in ("cohort_orchestrator", "multi_strategy_engine", "session_executor", "governed_market_data", "daily_pipeline", "generation_manager", "outcome_evidence"):
        module = importlib.import_module("tradingagents.strategies.orchestration." + name)
        monkeypatch.setattr(module, "datetime", FixedDatetime)
    # Fresh acquisitions run against the fixture's current New York date.
    for source_name in ("cftc", "noaa", "usda", "drought_monitor"):
        module = importlib.import_module("tradingagents.strategies.data_sources." + source_name + "_source")
        monkeypatch.setattr(module, "current_session_date", lambda:fixture.now.astimezone(__import__('zoneinfo').ZoneInfo('America/New_York')).date().isoformat())
        if hasattr(module, "acquisition_time"):
            monkeypatch.setattr(module, "acquisition_time", lambda:fixture.now.isoformat())
    from tradingagents.strategies.execution import price_source
    monkeypatch.setattr(price_source, "datetime", FixedDatetime)
    monkeypatch.setenv("EVENTEDGE_SOURCE_CACHE_DIR", str(tmp_path / "source_cache"))
    monkeypatch.delenv("EVENTEDGE_RUNTIME_LOCK_FD", raising=False)
    monkeypatch.delenv("EVENTEDGE_RUNTIME_LOCK_MODE", raising=False)
    config = deepcopy(DEFAULT_CONFIG)
    config["autoresearch"]["state_dir"] = str(tmp_path / "data/generations" / GENERATION)
    config["autoresearch"]["finnhub_reliability"]["rate_delay_s"] = 0
    for key in ("finnhub_api_key", "fred_api_key", "regulations_api_key", "courtlistener_token", "usda_nass_api_key", "fmp_api_key"):
        config["autoresearch"][key] = "offline-dummy"
    monkeypatch.delenv("NOAA_CDO_TOKEN", raising=False)
    config["autoresearch"]["noaa_cdo_token"] = ""
    orchestrator = CohortOrchestrator(build_default_cohorts(config), config,
                                     generation_id=GENERATION, generation_commit=COMMIT)
    from tradingagents.strategies.orchestration import generation_manager, runtime_lock
    from scripts import run_cohorts
    monkeypatch.setattr(runtime_lock, "canonical_runtime_lock_path", lambda _: tmp_path / "runtime.lock")
    monkeypatch.setattr(generation_manager, "canonical_runtime_lock_path", lambda _: tmp_path / "runtime.lock")
    manifest_path = Path(config["autoresearch"]["state_dir"]).parent / "manifest.json"
    manifest_path.write_text(json.dumps({"generations": [{"gen_id": GENERATION, "git_commit": COMMIT,
        "state_dir": config["autoresearch"]["state_dir"], "worktree_path": str(tmp_path),
        "status": "active", "git_branch": "fixture", "description": "offline provider contract",
        "created_at": "2026-10-05T20:00:00+00:00", "run_history": []}]}))

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


def test_offline_pipeline_isolates_inherited_shadow_credentials(monkeypatch, request):
    """CLI dotenv loading must not authorize a paid call in offline acceptance."""
    import os

    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "a" * 32)
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "offline-seeded-token")
    fixture, owner, config, _ = request.getfixturevalue("pipeline")
    account_present = bool(os.environ.get("CLOUDFLARE_ACCOUNT_ID"))
    token_present = bool(os.environ.get("CLOUDFLARE_API_TOKEN"))
    assert not account_present
    assert not token_present
    assert config["decision_shadow"]["enabled"] is True
    owner.run_daily(str(SESSION))
    sidecar = Path(config["autoresearch"]["state_dir"]) / "decision_shadow" / f"{SESSION}.json"
    assert json.loads(sidecar.read_text())["status"] == "missing_credentials"
    assert not fixture.blocked_calls


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


def test_current_full_pipeline_has_44_healthy_and_4_excluded_records_and_16_valid_books(pipeline):
    fixture, orchestrator, config, repo = pipeline
    result, wire, report = run_campaign(pipeline)
    assert result["outcome"] == "clean" and result["success"] is True
    assert all(not row["error"] and row["execution_valid"] and row["staging_valid"] and row["input_coverage_valid"] for row in wire.values())
    records = orchestrator._metric_store.read_strategy_health(session=SESSION)
    assert len(records) == 48
    enabled = [row for row in records if row.strategy != 'state_economics']
    excluded = [row for row in records if row.strategy == 'state_economics']
    assert len(enabled) == 44 and len(excluded) == 4
    assert all(row.status in {"signals", "legitimate_no_event"} for row in enabled), [(row.strategy, row.status, row.evidence) for row in enabled]
    assert all(row.status == 'disabled_by_policy' and row.signal_count == 0 for row in excluded)
    assert report['disabled_strategies'] == {'state_economics': 'unsupported_state_event_proxy'}
    assert {row.strategy for row in records} == {strategy.name for strategy in orchestrator.cohorts[0]["engine"].paper_trade_strategies}
    assert fixture.model_calls["enrichment"] >= 1
    assert fixture.model_calls["committee"] >= 1
    assert sum(len(cohort["ledger"].pending_intents(date(2026, 10, 5))) for cohort in orchestrator.cohorts) >= 1
    assert all(not cohort["ledger"].read_fills() for cohort in orchestrator.cohorts)
    assert all(float(signal["reference_close"]) == 184 for row in wire.values() for signal in row["signals"] if signal["ticker"] == "NVDA")
    requests = [(url, params) for url, params in fixture.request_trace if "data.alpaca.markets" in url]
    assert requests and all(params["feed"] == "sip" and params["adjustment"] == "raw" and params["timeframe"] == "1Day" for _, params in requests)
    requested_symbols = {symbol for url, params in requests
                         for symbol in (params["symbols"].split(",") if "symbols" in params
                                        else [url.split("/")[-2]])}
    assert requested_symbols >= {"SPY", "BIL", "NVDA"}
    for cohort in orchestrator.cohorts:
        context = cohort["ledger"]._connection.execute("SELECT economic_inputs_json FROM session_execution_contexts WHERE session=?", (str(SESSION),)).fetchone()
        frozen = json.loads(context[0])["market"]
        assert {bar["source"] for bar in frozen["raw_bars"]} == {"alpaca-sip-1d-raw"}
        assert {bar["source"] for bar in frozen["benchmarks"]} == {"yfinance-adjusted"}


def test_late_weather_survives_a_moving_window_before_native_candidate_pricing(pipeline):
    fixture, orchestrator, config, _ = pipeline
    fixture.weather_disruption = True
    first_result, first_wire, _ = run_campaign(pipeline)
    assert first_result["outcome"] == "clean"
    old = {name: {row["event_key"]: row for row in result["signals"]
                  if row["strategy"] == "weather_ag"}
           for name, result in first_wire.items()}
    assert all(rows for rows in old.values()), "native environmental inputs must produce candidates"
    assert all(row["signal_id"] in first_wire[name]["cutoff_late"]
               for name, rows in old.items() for row in rows.values())
    accepted = accepted_input_bytes(config)

    fixture.session = date(2026, 10, 5)
    fixture.now = datetime(2026, 10, 5, 22, tzinfo=timezone.utc)
    fixture.interrupt_committee_at = fixture.model_calls["committee"] + 5
    interrupted = fixture.manager.run_daily(str(fixture.session))[GENERATION]
    assert interrupted["outcome"] == "failed"
    frozen_at_interrupt = accepted_input_bytes(config)
    fixture.interrupt_committee_at = None
    fixture.block_all = True
    second_result, second_wire, _ = run_campaign(pipeline, fixture.session)
    assert second_result["outcome"] == "clean"
    assert accepted_input_bytes(config) == frozen_at_interrupt and not fixture.blocked_calls
    for name, result in second_wire.items():
        offered = {row["event_key"]: row for row in result["signals"]
                   if row["strategy"] == "weather_ag" and row["signal_id"] not in result["cutoff_late"]}
        assert set(old[name]) <= set(offered), "the original weather window must reach its first eligible decision"
        for event_key, original in old[name].items():
            assert offered[event_key]["observed_at"] == original["observed_at"]
            assert offered[event_key]["reference_session"] == "2026-10-05"
    assert all(accepted_input_bytes(config)[key] == value for key, value in accepted.items())
    # A same-session resume must preserve offers and accepted inputs, with no IO.
    after = accepted_input_bytes(config)
    fixture.block_all = True
    replay_result, replay_wire, _ = run_campaign(pipeline, fixture.session)
    assert replay_result["outcome"] == "clean"
    assert {name: {signal["signal_id"]: signal for signal in row["signals"]}
            for name, row in replay_wire.items()} == {
                name: {signal["signal_id"]: signal for signal in row["signals"]}
                for name, row in second_wire.items()}
    assert accepted_input_bytes(config) == after and not fixture.blocked_calls


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
        assert all(row.status in {"signals", "legitimate_no_event"} for row in records if row.strategy != "state_economics")
        assert any(row["provider"] == "edgar" and row["recovered"] and row["attempts"] == 2 and row["http_status"] == 200 for row in report["sources"]["recovered"])
        assert any(row["recovered"] and row["attempts"] == 2 for row in diagnostics)
    elif fault.startswith("edgar"):
        assert result["outcome"] == "degraded"
        assert result["input_coverage_valid"] is False
        failed = [row for row in records if row.status == "data_failure"]
        assert len(failed) == 12
        assert {row.strategy for row in failed} == {"insider_activity", "filing_analysis", "quantum_readiness"}
        assert all("edgar" in row["sources"] and set(row["sources"]) <= {"edgar", "analysis"} and len(row["affected_cohorts"]) == 4 for row in result["source_health_failures"])
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
